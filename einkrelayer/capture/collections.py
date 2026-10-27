"""One-level, explicitly marked contents lists; never a recursive site crawler."""

import re
from pathlib import Path
from urllib.parse import unquote, urldefrag, urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from einkrelayer.books.compose import identity
from einkrelayer.capture.http import fetch, normalized_url

CONTENTS = 'nav[aria-label*="contents" i], [role~="doc-toc"], #contents, .toc, #toc'
HEADING = re.compile(r"(?:table of contents|contents|chapters|chapter list)\s*:?", re.I)
UTILITY_PATHS = {
    "account",
    "login",
    "signin",
    "sign-in",
    "signup",
    "sign-up",
    "register",
    "logout",
    "search",
    "subscribe",
    "privacy",
    "terms",
    "contact",
    "cart",
    "checkout",
}
UTILITY_LABELS = {
    "log in",
    "login",
    "sign in",
    "sign up",
    "log out",
    "logout",
    "subscribe",
    "privacy policy",
}
DOCUMENT_SUFFIXES = {
    "",
    ".html",
    ".htm",
    ".xhtml",
    ".pdf",
    ".txt",
    ".md",
    ".markdown",
    ".docx",
}
DYNAMIC_SUFFIXES = {".php", ".asp", ".aspx", ".jsp", ".cgi"}


def contents_anchors(soup):
    """Combine marked sections and bounded lists immediately under contents headings."""
    roots = list(soup.select(CONTENTS))
    headings = [
        h
        for h in soup.find_all(re.compile(r"^h[1-6]$"))
        if HEADING.fullmatch(h.get_text(" ", strip=True))
    ]
    for heading in headings:
        found = False
        section_found = False
        interrupted = False
        level = int(heading.name[1])
        for sibling in heading.next_siblings:
            if not isinstance(sibling, Tag):
                continue
            if re.fullmatch(r"h[1-6]", sibling.name):
                if int(sibling.name[1]) <= level or re.match(
                    r"(?:related|see also|navigation|references)\b",
                    sibling.get_text(" ", strip=True),
                    re.I,
                ):
                    break
                found = False
                interrupted = False
                continue
            if any(sibling is root for root in roots) or sibling.select(CONTENTS):
                if interrupted:
                    raise ValueError(
                        "Contents list is interrupted before another list; supply chapter URLs directly"
                    )
                found = section_found = True
                continue
            lists = (
                [sibling]
                if sibling.name in {"ul", "ol", "dl", "table"}
                else sibling.find_all(["ul", "ol", "dl", "table"])
                if not sibling.find(re.compile(r"^h[1-6]$"))
                else []
            )
            if lists:
                if interrupted:
                    raise ValueError(
                        "Contents list is interrupted before another list; supply chapter URLs directly"
                    )
                roots.extend(lists)
                found = section_found = True
            elif found or sibling.name != "p":
                interrupted = True
        if not section_found and not any(
            parent is root for parent in heading.parents for root in roots
        ):
            # A second empty/unrecognized contents section must not be hidden by
            # a successfully selected first section.
            raise ValueError("Contents list heading has no adjacent chapter list")
    if not roots:
        if headings:
            raise ValueError("Contents list heading has no adjacent chapter list")
        return None
    selected = {id(a) for root in roots for a in root.find_all("a", href=True)}
    # DOM order across all sections; nested marked containers must not duplicate links.
    return [a for a in soup.find_all("a", href=True) if id(a) in selected]


def utility_link(anchor, parsed):
    label = anchor.get_text(" ", strip=True).casefold()
    parts = unquote(parsed.path).strip("/").casefold().split("/")
    return label in UTILITY_LABELS or (
        parsed.scheme in {"http", "https"}
        and (parts[0] in UTILITY_PATHS or (not parsed.path.strip("/") and not parsed.query))
    )


def has_reading_content(soup):
    """A substantial chapter can retain a book-wide contents sidebar."""
    reading = soup.select_one("article, main")
    if reading is None:
        return False
    reading = BeautifulSoup(str(reading), "html.parser")
    for node in reading.select(f"{CONTENTS}, nav, aside, header, footer"):
        node.decompose()
    paragraphs = reading.find_all(["p", "pre", "blockquote"])
    return (
        sum(
            len(node.get_text(" ", strip=True).split())
            for node in paragraphs
            if not node.find_parent(["p", "pre", "blockquote"])
        )
        >= 200
    )


def contents_links(data, base, limit=50, *, optional=False):
    soup = BeautifulSoup(data, "html.parser")
    from einkrelayer.capture.access import check_page, strip_hidden

    check_page(soup, base)
    strip_hidden(soup)
    anchors = contents_anchors(soup)
    if anchors is None:
        if optional:
            return None
        raise ValueError("No explicit contents list; supply the chapter URLs instead")
    if optional and has_reading_content(soup):
        return None
    origin = urlsplit(base)
    selected, seen = [], set()
    candidates = 0
    for anchor in anchors:
        target = urldefrag(urljoin(base, anchor["href"]))[0]
        parsed = urlsplit(target)
        if utility_link(anchor, parsed):
            continue
        if parsed.scheme not in {"http", "https", "file"} or parsed.netloc != origin.netloc:
            continue
        if parsed.scheme != origin.scheme:
            continue
        if parsed.scheme == "file":
            path = Path(unquote(parsed.path)).resolve()
            if not path.is_relative_to(Path(unquote(origin.path)).resolve().parent):
                raise ValueError("Contents link escapes the selected directory")
        key = identity(target)
        if key == identity(base) or key in seen:
            continue
        candidates += 1
        suffixes = DOCUMENT_SUFFIXES | (DYNAMIC_SUFFIXES if parsed.scheme != "file" else set())
        if Path(parsed.path).suffix.lower() not in suffixes:
            raise ValueError(
                "Contents list contains an unsupported chapter link; supply supported chapter URLs instead"
            )
        selected.append(str(path) if parsed.scheme == "file" else normalized_url(target))
        seen.add(key)
        if len(selected) > limit:
            raise ValueError("Contents list exceeds chapter limit; no pages were discarded")
    if not selected:
        if (
            optional
            and not candidates
            and any(urldefrag(urljoin(base, a["href"]))[0] == urldefrag(base)[0] for a in anchors)
        ):
            return None  # an article's own section links, not separate chapters
        raise ValueError("Contents list has no supported chapter links")
    return selected


def check_chapters(selected, base, *, checked=None):
    """Inspect selected pages for nested indexes; never fetch their grandchildren."""
    from einkrelayer.capture.readers import DOCUMENT_MEDIA

    checked = {} if checked is None else checked
    origin = urlsplit(base)
    for source in selected:
        key = (origin.scheme, origin.netloc, source)
        if key in checked:
            continue
        if origin.scheme == "file":
            path = Path(source).resolve()
            if path.stat().st_size > 20_000_000:
                raise ValueError("Contents list chapter exceeds byte limit")
            if path.suffix.lower() not in {".html", ".htm", ".xhtml"}:
                checked[key] = True
                continue
            data, final_url = path.read_bytes(), path.as_uri()
        else:
            response = fetch(source, media_types=DOCUMENT_MEDIA)
            final = urlsplit(response.final_url)
            if (final.scheme, final.netloc) != (origin.scheme, origin.netloc):
                raise ValueError(
                    "Contents list chapter redirects outside the selected site or protocol"
                )
            if response.content_type not in {"text/html", "application/xhtml+xml"}:
                checked[key] = True
                continue
            data, final_url = response.data, response.final_url
        if contents_links(data, final_url, optional=True):
            raise ValueError(
                "Contents list contains a nested chapter index; supply its chapter URLs directly instead"
            )
        checked[key] = True


def book_sources(source, *, checked=None):
    """Expand a remote explicit contents list, otherwise retain the single input."""
    from einkrelayer.capture.access import AccessRestricted
    from einkrelayer.capture.readers import DOCUMENT_MEDIA

    try:
        response = fetch(source, media_types=DOCUMENT_MEDIA)
    except AccessRestricted:
        raise
    except (ValueError, OSError) as error:
        from einkrelayer.diagnostics import event, reason

        event("contents", source, "unavailable", reason(error) + "; trying document capture")
        # Let direct document capture report a probe failure; never omit observed chapters.
        return [source]
    if response.content_type not in {"text/html", "application/xhtml+xml"}:
        return [source]
    selected = contents_links(response.data, response.final_url, optional=True)
    if selected:
        check_chapters(selected, response.final_url, checked=checked)
    return selected or [source]


def discover(source):
    if urlsplit(source).scheme in {"http", "https"}:
        response = fetch(source, html=True)
        selected = contents_links(response.data, response.final_url)
        check_chapters(selected, response.final_url)
        return selected
    path = Path(source).resolve()
    if path.stat().st_size > 20_000_000:
        raise ValueError("Contents page exceeds byte limit")
    selected = contents_links(path.read_bytes(), path.as_uri())
    check_chapters(selected, path.as_uri())
    return selected
