"""Remove the clutter around an article while keeping everything that belongs to it.

Three signals mark clutter: the page labels a block as related stories, sharing, newsletters or ads
(by class, id or role); a block is almost all links, like a "Most read" list; or a short block is a
sharing or sign-up call to action. A block is kept whenever it holds a real share of the article's
text, contains its main heading, or is a figure, table, footnote or reference list. When in doubt,
keep it.
"""

import re

LABELLED = re.compile(
    r"related|recirc|recommend|more-?(?:stories|from|on|in|news)|most-?(?:popular|viewed|read|shared)"
    r"|popular|trending|newsletter|sign-?up|subscri|promo|sharing|share-?(?:bar|tools|buttons|links)"
    r"|social|comments?(?:-|_|$|\s)|disqus|advert|sponsor|outbrain|taboola|read-?(?:next|more)"
    r"|up-?next|breadcrumb|cookie|consent|tags-?list|topic-?list|byline-?share|toolbar|popover"
    r"|follow-?(?:button|topic|author|category)|(?:posts?|stories|articles?)-?(?:carousel|list|grid)"
    r"|text-?settings|font-?size|dropdown|(?:^|[\s_-])menu(?:$|[\s_-])",
    re.I,
)
LABELLED_WORD = re.compile(r"(?:^|[\s_-])(?:ad|ads|share|tags|topics)(?:$|[\s_-])", re.I)
KEEP_LABEL = re.compile(
    r"article-?body|story-?body|entry-?content|post-?content|caption|figure|footnote|endnote"
    r"|reference|bibliograph|citation|math|equation|table",
    re.I,
)
CALL_TO_ACTION = re.compile(
    r"(?:share|copy) (?:this|full) (?:article|story|link)|^share(?: this)?$|^copy link$"
    r"|follow us|add us on google|add [\w ]{1,20} on google|sign up for (?:our|the|a) [\w ]{0,30}newsletter"
    r"|subscribe to (?:our|the) [\w ]{0,30}newsletter|^read more$|^most (?:read|viewed|popular)$"
    r"|^related(?: (?:stories|articles|content|coverage))?$|^more (?:stories|from|on) .{0,40}$"
    r"|^\d+ comments?$|^comments?$|^view comments$|^loading[\w ]{0,20}(?:\.\.\.|…)?$"
    r"|posts from this \w+ will be added to your|^sign up for (?:the |our )?[\w ]{0,40}(?:email|newsletter)$",
    re.I,
)
BYLINE = re.compile(r"byline|author|dateline|publish|timestamp|(?:^|[\s_-])date", re.I)
RELATED_HEADING = re.compile(
    r"^(?:related(?: content| stories| articles| coverage| reading)?|more (?:in|from|on|stories|to read)\b.*"
    r"|most (?:read|viewed|popular|shared)(?: now)?|editors[’']? picks|recommended(?: for you)?"
    r"|you (?:may|might) also like|read next|up next|trending(?: now)?|popular(?: now)?"
    r"|what to read next|don[’']t miss|latest (?:news|stories))$",
    re.I,
)
SHARE_LINK = re.compile(
    r"^(?:mailto:|sms:|whatsapp:)|facebook\.com/(?:sharer|dialog/share)|(?:twitter|x)\.com/(?:intent|share)"
    r"|linkedin\.com/(?:share|sharing|shareArticle)|reddit\.com/submit|wa\.me/|api\.whatsapp\.com/send"
    r"|threads\.(?:net|com)/intent|bsky\.app/intent|pinterest\.com/pin/create|t\.me/share|telegram\.me/share"
    r"|news\.ycombinator\.com/submitlink|flipboard\.com/bookmarklet",
    re.I,
)
NOTES = re.compile(
    r"notes|references|sources|bibliography|citations|further reading|recommended reading|see also",
    re.I,
)
PROTECTED = {"table", "math", "blockquote", "pre", "code"}
MEDIA = ["img", "svg", "picture", "table", "math", "video", "audio", "iframe", "pre", "code"]
CONTAINERS = ["div", "section", "aside", "ul", "ol", "header", "p", "span", "a", "li"]


def text_length(node):
    return len(" ".join(node.get_text(" ").split()))


def words(node):
    return len(node.get_text(" ").split())


def link_length(node):
    return sum(text_length(link) for link in node.find_all("a"))


def label(node):
    return " ".join([*node.get("class", []), node.get("id") or "", node.get("data-testid") or ""])


def protected(node, total, heading):
    if node.name in PROTECTED or node.find_parent(PROTECTED) is not None:
        return True
    figure = node if node.name == "figure" else node.find_parent("figure")
    if figure is not None and figure.find(MEDIA) is not None:  # a real figure, not a promo box
        return True
    if heading is not None and (node is heading or heading in node.descendants):
        return True
    if KEEP_LABEL.search(label(node)) or node.get("role") in {"doc-endnotes", "doc-footnote"}:
        return True
    return total > 0 and text_length(node) > total * 0.25


def notes_list(node):
    """Footnotes and reference lists are link-heavy but belong to the article."""
    links = node.find_all("a", href=True)
    internal = sum(link["href"].startswith("#") for link in links)
    if links and internal >= len(links) / 2:
        return True
    # A list introduced as notes, sources or further reading, by a heading or a short label line.
    labels = [node.find_previous_sibling(re.compile("^h[2-6]$")), node.find_previous_sibling(True)]
    first = next((child for child in node.children if str(child).strip()), None)
    if first is not None and getattr(first, "name", None):
        labels.append(first)
    return any(
        tag is not None
        and len(tag.get_text(" ", strip=True)) <= 60
        and NOTES.search(tag.get_text(" ", strip=True))
        for tag in labels
    )


def link_list(node, source=""):
    """A block that is almost all links to the same site or to sharing pages, like a "Most read"
    list or a share bar. Lists of links to other sites are content, such as a reading roundup."""
    if node.name not in {"div", "section", "aside", "ul", "ol"} or notes_list(node):
        return False
    hrefs = [a["href"] for a in node.find_all("a", href=True)]
    home = site_of(source)
    local = sum(
        SHARE_LINK.search(h) is not None
        or h.startswith(("/", "#"))
        and not h.startswith("//")
        or (home is not None and site_of(h) == home)
        for h in hrefs
    )
    if hrefs and local < len(hrefs) * 0.75:
        return False
    length = text_length(node)
    links = node.find_all("a")
    if length == 0 or len(links) < 3:
        return False
    prose = any(words(p) - sum(words(a) for a in p.find_all("a")) >= 25 for p in node.find_all("p"))
    return not prose and link_length(node) >= length * 0.75


def holds_byline(node):
    """Whether a block carries the article's author or date."""
    if node.find("time") is not None or BYLINE.search(label(node)):
        return True
    return any(BYLINE.search(label(child)) for child in node.find_all(True))


def call_to_action(text):
    """A short sharing or sign-up prompt that makes up most of the block."""
    match = CALL_TO_ACTION.search(text) if len(text) <= 120 else None
    return match is not None and len(match.group(0)) >= len(text) * 0.5


def site_of(url):
    match = re.match(r"(?i)https?://(?:www\.)?([^/:?#]+)", url or "")
    return match[1].lower() if match else None


def card_list(node, source):
    """Three or more short items that each link to another page of the same site, with no prose:
    related-story cards, "more in" lists, job ads. Lists linking elsewhere are kept."""
    if node.name not in {"div", "section", "aside", "ul", "ol"} or notes_list(node):
        return False
    items = [child for child in node.find_all(True, recursive=False) if child.get_text(strip=True)]
    if len(items) < 3 or any(words(item) > 45 for item in items):
        return False
    if any(words(p) - sum(words(a) for a in p.find_all("a")) >= 25 for p in node.find_all("p")):
        return False
    home = site_of(source)
    linked = 0
    for item in items:
        for link in item.find_all("a", href=True):
            href = link["href"]
            target = site_of(href) or (
                home if href.startswith("/") and not href.startswith("//") else None
            )
            if target == home and href.split("#")[0].rstrip("/") not in {"", source.rstrip("/")}:
                linked += 1
                break
    return home is not None and linked >= len(items) * 0.75


def related_section(node):
    """A section that opens with a heading like "Related content" or "More in Science" and
    holds only short items, no prose."""
    if node.name not in {"div", "section", "aside"}:
        return False
    first = next((child for child in node.find_all(True) if child.get_text(strip=True)), None)
    if first is None or len(first.get_text(" ", strip=True)) > 40:
        return False
    if not RELATED_HEADING.match(" ".join(first.get_text(" ").split())):
        return False
    return not any(
        words(p) - sum(words(a) for a in p.find_all("a")) >= 25 for p in node.find_all("p")
    )


def side_box(node):
    """A side box with its own headings and links but no prose, like "Subjects" or "Jobs".
    Pull quotes, which have no headings, are kept."""
    return (
        node.name == "aside"
        and node.find(re.compile("^h[2-6]$")) is not None
        and len(node.find_all("a")) >= 2
        and not any(
            words(p) - sum(words(a) for a in p.find_all("a")) >= 25 for p in node.find_all("p")
        )
    )


def clean(root, heading=None, log=None, source=""):
    """Remove clutter from an article root in place. Returns how many blocks were removed;
    `log`, if given, collects (reason, text) for each removal."""
    total = text_length(root)
    removed = 0
    order = {id(element): index for index, element in enumerate(root.find_all(True))}
    body_start = next((p for p in root.find_all("p") if words(p) >= 40), None)

    def above_body(node):
        return (
            body_start is not None
            and order.get(id(node), 0) < order.get(id(body_start), 0)
            and body_start not in node.descendants
        )

    for node in list(root.find_all(CONTAINERS)):
        if node.parent is None or node.decomposed or protected(node, total, heading):
            continue
        if above_body(node) and holds_byline(node):
            continue  # only the clutter inside the article header goes, not the byline
        tag_label = label(node)
        text = " ".join(node.get_text(" ").split())
        if source and text.rstrip("/") == source.split("#")[0].rstrip("/"):
            node.decompose()  # the page's own address, left over from a share box
            removed += 1
            continue
        clutter = (
            (tag_label and (LABELLED.search(tag_label) or LABELLED_WORD.search(tag_label)))
            or link_list(node, source)
            or card_list(node, source)
            or related_section(node)
            or side_box(node)
            or call_to_action(text)
        )
        if clutter:
            if log is not None:
                reason = (
                    "labelled"
                    if tag_label and (LABELLED.search(tag_label) or LABELLED_WORD.search(tag_label))
                    else "links"
                    if link_list(node)
                    else "action"
                )
                log.append((reason, text[:160]))
            node.decompose()
            removed += 1
    # A heading left with nothing after it introduced a block that was removed.
    while True:
        texts = [t for t in root.find_all(string=True) if t.strip()]
        last = texts[-1] if texts else None
        heading_tag = last.find_parent(["h2", "h3", "h4", "h5", "h6"]) if last else None
        if heading_tag is None or root not in heading_tag.parents:
            break
        if log is not None:
            log.append(("trailing heading", heading_tag.get_text(" ", strip=True)[:160]))
        heading_tag.decompose()
        removed += 1
    # Decorative images (empty alt text) after the last words were thumbnails of removed lists.
    texts = [t for t in root.find_all(string=True) if t.strip()]
    if texts:
        for image in texts[-1].find_all_next("img"):
            if (
                root in image.parents
                and image.get("alt") == ""
                and image.find_parent("figure") is None
            ):
                if log is not None:
                    log.append(("trailing image", image.get("src", "")[:160]))
                image.decompose()
                removed += 1
    return removed
