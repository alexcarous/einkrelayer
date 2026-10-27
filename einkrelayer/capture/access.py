"""Detect access gates and select static, visible page content."""

import json
import re
from urllib.parse import urljoin, urlsplit

from soupsieve import SelectorSyntaxError
from tinycss2 import parse_declaration_list, parse_stylesheet, serialize

PROMPT = re.compile(
    r"\b(?:subscribe|sign\s*in|log\s*in|register|purchase a subscription)"
    r"\s+(?:(?:now|today)\s+)?to\s+(?:continue|keep|read|access|unlock)\b"
    r"|\b(?:this|the|full) (?:article|story|post|content) is (?:only )?"
    r"(?:available )?(?:exclusively |only )?(?:to|for) (?:[\w'’ ]{0,40} )?(?:subscribers|members)\b"
    r"|\byou(?:’|')?ve reached (?:your|the) (?:free )?(?:article|reading) limit\b"
    r"|\baccess through your institution\b",
    re.I,
)
ARTICLE_TYPES = {
    "Article",
    "NewsArticle",
    "BlogPosting",
    "Report",
    "ScholarlyArticle",
    "TechArticle",
    "ReportageNewsArticle",
    "AnalysisNewsArticle",
    "OpinionNewsArticle",
}


class AccessRestricted(ValueError):
    def __init__(self, reason):
        super().__init__("Access restricted: " + reason)


def identity(url):
    parts = urlsplit(url)
    return (parts.hostname or "").removeprefix("www."), parts.path.rstrip("/")


def objects(value):
    pending = [value]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            yield node
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)


def marked_paid(soup, source):
    titles = {n.get_text(" ", strip=True).casefold() for n in soup.select("title,h1")}
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(script.string or "{}")
        except (ValueError, TypeError, RecursionError):
            continue
        for node in objects(value):
            types = node.get("@type", [])
            types = [types] if isinstance(types, str) else types
            if not isinstance(types, list) or not any(
                isinstance(t, str) and t.rsplit("/", 1)[-1] in ARTICLE_TYPES for t in types
            ):
                continue
            addresses = []
            for key in ("url", "@id", "mainEntityOfPage"):
                address = node.get(key)
                if isinstance(address, dict):
                    address = address.get("@id") or address.get("url")
                if isinstance(address, str) and not address.startswith("#"):
                    addresses.append(identity(urljoin(source, address)))
            headline = str(node.get("headline", "")).casefold()
            if addresses and identity(source) not in addresses and headline not in titles:
                continue
            if not addresses and headline and titles and headline not in titles:
                continue
            parts = node.get("hasPart", [])
            parts = [parts] if isinstance(parts, dict) else parts if isinstance(parts, list) else []
            for item in [node, *parts]:
                if (
                    isinstance(item, dict)
                    and str(item.get("isAccessibleForFree", "")).lower() == "false"
                ):
                    return True
    return False


def check_page(soup, source):
    title = soup.title.get_text(" ", strip=True).lower() if soup.title else ""
    if any(text in title for text in ("just a moment", "access denied", "verify you are human")):
        raise AccessRestricted("access challenge")
    if title.strip() in {"captcha", "captcha challenge", "security check"} or soup.select_one(
        "#challenge-form, #cf-challenge-running"
    ):
        raise AccessRestricted("access challenge")
    if marked_paid(soup, source):
        raise AccessRestricted("article requires a subscription")
    if soup.select_one('input[type="password"]') and re.fullmatch(
        r"(?:sign in|login|log in)(?:\s.*)?", title.strip(), re.I
    ):
        raise AccessRestricted("login required")
    for node in soup.find_all(["p", "h1", "h2", "h3", "div", "span", "button"]):
        if node.find_parent(["pre", "code", "blockquote", "script", "style"]):
            continue
        text = node.get_text(" ", strip=True)
        if len(text) <= 350 and PROMPT.search(text):
            raise AccessRestricted("subscription or login required")
    for node in soup.select('.paywall, #paywall, [data-testid="paywall"]'):
        text = node.get_text(" ", strip=True)
        if text and re.search(r"\b(subscribe|subscription|sign in|log in|unlock)\b", text, re.I):
            raise AccessRestricted("subscription or login required")


def hidden_style(style):
    return any(
        token.type == "declaration"
        and (
            token.lower_name == "display"
            and serialize(token.value).strip().lower() == "none"
            or token.lower_name == "visibility"
            and serialize(token.value).strip().lower() == "hidden"
        )
        for token in parse_declaration_list(style, skip_comments=True, skip_whitespace=True)
    )


def strip_hidden(soup):
    # Static markup and inline stylesheet rules only; do not run scripts or fetch stylesheets.
    hidden = list(soup.select('[hidden], [inert], [aria-hidden="true"]'))
    hidden.extend(node for node in soup.select("[style]") if hidden_style(node["style"]))
    for style in soup.find_all("style"):
        for rule in parse_stylesheet(style.get_text(), skip_comments=True, skip_whitespace=True):
            if rule.type != "qualified-rule" or not hidden_style(serialize(rule.content)):
                continue
            try:
                hidden.extend(soup.select(serialize(rule.prelude)))
            except SelectorSyntaxError:
                continue
    for node in hidden:
        if node.parent is not None:
            node.decompose()
