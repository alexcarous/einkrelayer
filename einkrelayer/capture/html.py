"""Generic static HTML extraction with bounded, localized reading resources."""

import copy
import io
import re
from datetime import UTC, datetime
from urllib.parse import unquote, urldefrag, urljoin, urlsplit
from xml.etree import ElementTree as ET

import cairosvg
import ziamath
from bs4 import BeautifulSoup
from PIL import Image

from einkrelayer.capture import clutter
from einkrelayer.capture.access import AccessRestricted, check_page, strip_hidden
from einkrelayer.capture.assets import image_source
from einkrelayer.capture.http import digest, normalized_url, verify_svg
from einkrelayer.models import Document

MAX_ASSETS = 1024
MAX_ASSET_BYTES = 64_000_000
MAX_IMAGE_PIXELS = 40_000_000
AD_LABEL = re.compile(
    r"(?i)(?:skip )?advertisement|ad|sponsored|(?:story|article) continues below(?: this)? advertisement"
)
PHOTO_FORMATS = {"WEBP", "AVIF", "HEIF", "MPO"}
ALLOWED = set(
    "a abbr address article aside b bdi bdo blockquote br caption cite code col colgroup dd del details dfn div dl dt em figcaption figure h1 h2 h3 h4 h5 h6 header hr i img ins kbd li main mark ol p pre q rp rt ruby s samp section small span strong sub summary sup table tbody td tfoot th thead time tr u ul var wbr".split()
)


def reading_href(href, source):
    if href.startswith("#"):
        return href
    absolute = urljoin(source, href.replace(" ", "%20"))
    parsed = urlsplit(absolute)
    if parsed.scheme in {"http", "https"}:
        base, fragment = urldefrag(absolute)
        try:
            base = normalized_url(base)
        except ValueError:
            return None
        return base + ("#" + fragment if fragment else "")
    if parsed.scheme in {"mailto", "tel"} and not any(ord(c) < 32 for c in absolute):
        return absolute
    if parsed.scheme == "file" and urlsplit(source).scheme == "file" and not parsed.netloc:
        return absolute
    return None


def image_bytes(data):
    if data.lstrip().startswith((b"<svg", b"<?xml")):
        verify_svg(data)
        root = ET.fromstring(data)
        viewbox = root.get("viewBox", "").replace(",", " ").split()
        dimensions = []
        for index, key in enumerate(["width", "height"]):
            value = root.get(key) or (viewbox[index + 2] if len(viewbox) == 4 else "")
            match = re.fullmatch(r"([0-9.]+)(px|pt|pc|in|cm|mm)?", value.strip())
            if not match and len(viewbox) == 4:
                value = viewbox[index + 2]
                root.set(key, value)
                match = re.fullmatch(r"([0-9.]+)(px|pt|pc|in|cm|mm)?", value.strip())
            if not match:
                raise ValueError("SVG needs bounded explicit dimensions or viewBox")
            units = {"px": 1, "pt": 96 / 72, "pc": 16, "in": 96, "cm": 96 / 2.54, "mm": 96 / 25.4}
            dimensions.append(float(match[1]) * units.get(match[2], 1))
        if min(dimensions) <= 0 or dimensions[0] * dimensions[1] * 4 > MAX_IMAGE_PIXELS:
            raise ValueError("SVG exceeds pixel limit")
        data = cairosvg.svg2png(bytestring=ET.tostring(root), scale=2)
    with Image.open(io.BytesIO(data)) as image:
        if image.width * image.height > MAX_IMAGE_PIXELS:
            raise ValueError("Image exceeds pixel limit")
        image.load()
        if image.format == "JPEG":
            return data, "jpg"
        output = io.BytesIO()
        transparent = "A" in image.getbands() or "transparency" in image.info
        if image.format in PHOTO_FORMATS and not transparent:
            # Photos in lossy web formats stay photos; PNG would make each one several times larger.
            image.convert("RGB").save(output, "JPEG", quality=88, optimize=True)
            return output.getvalue(), "jpg"
        image.convert("RGBA" if transparent else "RGB").save(output, "PNG")
        return output.getvalue(), "png"


def heading_permalink(link):
    """A heading's own "link to this section" marker, such as a trailing pilcrow."""
    return (
        link.get("href", "").startswith("#")
        and link.get_text(strip=True)
        in {
            "",
            "\u00b6",
            "\u00a7",
            "#",
            "\U0001f517",
        }
        and not link.find("img")
    )


def small_icon(svg):
    """An inline drawing 32 pixels or smaller, like a camera or share icon."""
    sizes = []
    viewbox = (svg.get("viewbox") or svg.get("viewBox") or "").replace(",", " ").split()
    for index, key in enumerate(["width", "height"]):
        value = re.fullmatch(r"\s*([0-9.]+)(?:px)?\s*", svg.get(key) or "")
        if value:
            sizes.append(float(value[1]))
        elif len(viewbox) == 4:
            try:
                sizes.append(float(viewbox[index + 2]))
            except ValueError:
                return False
    return len(sizes) == 2 and max(sizes) <= 32


def plain(text):
    return re.sub(r"\W+", " ", text).strip().lower()


def without_site_name(title, soup):
    """Drop a trailing " - Site Name" from a page title when the page names its site."""
    names = {
        plain(tag.get("content", ""))
        for tag in soup.select('meta[property="og:site_name"], meta[name="application-name"]')
    }
    match = re.fullmatch(r"(.+?)\s+[-|–—:·]\s+([^-|–—:·]+)", title.strip())
    if match and plain(match[2]) in names - {""}:
        return match[1]
    return title


def extract(data, source, loader, *, charset=None):
    if re.search(rb"<[A-Za-z/!][^>]*$", data.rstrip()):
        raise ValueError("Truncated or unrecoverable HTML content")
    soup = BeautifulSoup(data, "lxml", from_encoding=charset)
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    password_form = soup.select_one('input[type="password"]') is not None
    check_page(soup, source)
    strip_hidden(soup)

    # Prefer semantic structure, with a few widely used document containers.
    root = soup.select_one(
        '[itemprop="articleBody"], .entry-content, .document .body, .main-page-content, .ltx_page_content'
    )
    root = root or soup.find("main") or soup.find("article") or soup.body or soup
    if root is soup.body and soup.h1:
        body_length = len(root.get_text(strip=True))
        for parent in soup.h1.parents:
            if parent is root:
                break
            if (
                parent.name in {"div", "section", "article", "main"}
                and len(parent.get_text(strip=True)) >= body_length * 0.65
            ):
                root = parent
                break
    if root.name == "main":
        articles = root.find_all("article", recursive=False)
        if len(articles) == 1:
            root = articles[0]
    if password_form:
        outside_forms = copy.copy(root)
        for element in outside_forms.find_all(["form", "h1", "h2", "h3", "script", "style"]):
            if element.parent is not None:
                element.decompose()
        if not outside_forms.get_text(strip=True) or re.fullmatch(
            r"(?:sign in|login|log in)(?: to .*)?", title.strip(), re.I
        ):
            raise AccessRestricted("login required")
    language = root.get("lang") or (soup.html.get("lang") if soup.html else None) or "en"
    direction = root.get("dir") or (soup.html.get("dir") if soup.html else None) or ""
    heading = root.find("h1")
    if heading and title:
        # A heading that follows substantial introductory text names a section,
        # not the page; keep the document title in that case.
        before = sum(
            len(text.strip())
            for text in heading.find_all_previous(string=True)
            if text.find_parent("head") is None and root in text.parents
        )
        # The page's own title confirms its main heading, whatever share or ad text precedes it.
        if before > 200 and plain(heading.get_text(" ", strip=True)) not in plain(title):
            heading = None
    if heading:
        # Footnote markers belong in the chapter, not in the book title.
        heading = copy.copy(heading)
        for link in heading.find_all("a"):
            if heading_permalink(link):
                link.decompose()
        for mark in heading.find_all("sup"):
            links = mark.find_all("a")
            if (
                len(links) == 1
                and links[0].get("href", "").startswith("#")
                and mark.get_text(strip=True) == links[0].get_text(strip=True)
            ):
                mark.decompose()
    heading_text = heading.get_text(" ", strip=True) if heading else ""
    title = heading_text or without_site_name(title, soup)
    title = title or urlsplit(source).path.rsplit("/", 1)[-1] or "Untitled page"
    author = soup.select_one('meta[name="author"]')
    published = soup.select_one('meta[property="article:published_time"], time[datetime]')
    document = Document(
        source=source,
        title=title,
        html="",
        author=author.get("content", "") if author else "",
        published=(published.get("content") or published.get("datetime", "")) if published else "",
        captured=datetime.now(UTC).isoformat(),
        language=language,
        direction=direction,
    )

    # Bring referenced footnotes into the extracted body without copying unrelated
    # end-of-page material. Their backlinks retain the original fragment IDs.
    copied_notes = set()
    for link in list(root.find_all("a", href=True)):
        destination, fragment = urldefrag(urljoin(source, link["href"]))
        if not fragment or (
            not link["href"].startswith("#") and destination != urldefrag(source)[0]
        ):
            continue
        target = soup.find(id=unquote(fragment)) or soup.find(
            "a", attrs={"name": unquote(fragment)}
        )
        if (
            target is not None
            and target is not root
            and root not in target.parents
            and target not in root.parents
        ):
            note = target.find_parent(attrs={"role": "doc-endnotes"}) or target
            if id(note) not in copied_notes:
                root.append(copy.copy(note))
                copied_notes.add(id(note))
    root = copy.copy(root)
    for node in list(
        root.select(
            "script,style,link,noscript,nav,footer,form,input,button,select,textarea,template"
        )
    ):
        if node.parent is not None:
            node.decompose()
    for heading in root.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        for link in list(heading.find_all("a")):
            if heading_permalink(link):
                if link.get("id") and not heading.get("id"):
                    heading["id"] = link["id"]
                link.decompose()
    for aside in list(root.find_all("aside")):
        if aside.get("role") == "complementary" or any(
            x in " ".join(aside.get("class", []))
            for x in ["sidebar", "advertisement", "related-posts"]
        ):
            aside.decompose()
    for node in list(
        root.select(
            '[role="navigation"], [role="banner"], [role="contentinfo"], .sidebar, .related-posts, .social-share'
        )
    ):
        if node.parent is not None:
            node.decompose()
    # Ad slot labels and empty headings are page furniture, not reading content.
    for node in list(root.find_all(["p", "div", "span", "aside", "section", "a"])):
        if node.parent is not None and AD_LABEL.fullmatch(" ".join(node.get_text(" ").split())):
            node.decompose()
    for heading in list(root.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])):
        if not heading.get_text(strip=True) and not heading.find(["img", "svg", "math"]):
            heading.decompose()
    clutter.clean(root, root.find("h1"), source=source)
    media_nodes = list(root.find_all(["iframe", "video", "audio", "canvas", "embed", "object"]))
    if media_nodes:
        static = copy.copy(root)
        for node in static.find_all(
            [
                "iframe",
                "video",
                "audio",
                "canvas",
                "embed",
                "object",
                "h1",
                "h2",
                "h3",
                "h4",
                "h5",
                "h6",
            ]
        ):
            if node.parent is not None:
                node.decompose()
        if not static.get_text(strip=True):
            raise ValueError("Media-only page has no substantive static reading content")
    for media in media_nodes:
        href = media.get("src") or media.get("data")
        if media.name == "canvas" or (media.name in {"object", "embed"} and not href):
            raise ValueError("Required interactive content has no readable static representation")
        if href:
            replacement = soup.new_tag("a", href=urljoin(source, href))
            replacement.string = "Original embedded media (online)"
            media.replace_with(replacement)
        else:
            media.decompose()
        document.warnings.append("Embedded media playback omitted; original remains online")
    text = root.get_text(" ", strip=True)
    reading = copy.copy(root)
    for heading_node in list(reading.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])):
        heading_node.decompose()
    if not reading.get_text(strip=True) and not reading.find(["img", "svg", "math"]):
        raise ValueError("No substantive static reading content")
    if len(text) > 2_000_000:
        raise ValueError("Document exceeds extracted text limit")
    if not text or not any(character.isalnum() for character in text):
        raise ValueError("No substantive static reading content")

    total = 0
    cache = {}

    def asset(raw):
        nonlocal total
        content, suffix = image_bytes(raw)
        name = f"assets/{digest(content)}.{suffix}"
        if name not in document.assets:
            if len(document.assets) >= MAX_ASSETS or total + len(content) > MAX_ASSET_BYTES:
                raise ValueError("Document exceeds asset count/byte limits")
            document.assets[name] = content
            total += len(content)
        return name

    if len(root.find_all(["math", "svg", "img"])) > MAX_ASSETS:
        raise ValueError("Document exceeds image/equation count limit")
    formula_cache = {}
    math_geometry = {}
    for table in root.find_all("table"):
        classes = " ".join(table.get("class", []))
        if table.find("math") and (
            table.get("role") == "presentation"
            or "ltx_equation" in classes
            or "ltx_eqn_table" in classes
        ):
            table.name = "div"
            table["class"] = ["equation-layout"]
            for row in table.find_all("tr"):
                row.name = "div"
                row["class"] = ["equation-row"]
            for cell in table.find_all(["td", "th"]):
                cell.name = "span"
            for wrapper in table.find_all(["tbody", "thead", "tfoot"]):
                wrapper.unwrap()
    for formula in list(root.find_all("math")):
        try:
            clean = copy.copy(formula)
            for element in [clean, *clean.find_all(True)]:
                for key in list(element.attrs):
                    if key in {
                        "id",
                        "class",
                        "href",
                        "title",
                        "alttext",
                        "altimg",
                        "altimg-width",
                        "altimg-height",
                        "altimg-valign",
                        "display",
                    }:
                        del element[key]
            markup = str(clean)
            if markup not in formula_cache:
                svg = ziamath.Math(markup, size=24).svg().encode()
                svg_root = ET.fromstring(svg)
                view = list(map(float, svg_root.get("viewBox").split()))
                formula_cache[markup] = (
                    asset(svg),
                    float(svg_root.get("width")) / 24,
                    (view[1] + view[3]) / 24,
                )
            name, em_width, em_depth = formula_cache[markup]
        except (ValueError, KeyError, NotImplementedError, AttributeError) as error:
            raise ValueError("Required equation could not be rendered faithfully") from error
        replacement = soup.new_tag("img", src=name, alt=formula.get("alttext") or "Equation")
        replacement["class"] = (
            "math-display" if formula.get("display") == "block" else "math-inline"
        )
        math_geometry[id(replacement)] = (em_width, em_depth)
        formula.replace_with(replacement)
    for svg in list(root.find_all("svg")):
        style = re.sub(r"\s+", "", svg.get("style", "")).lower()
        if (
            svg.get("aria-hidden") == "true"
            or svg.get("role") == "presentation"
            or "display:none" in style
            or "0" in {svg.get("width"), svg.get("height")}
        ):
            svg.decompose()
            document.warnings.append("Decorative hidden SVG omitted")
            continue
        drawing = [
            "path",
            "rect",
            "circle",
            "ellipse",
            "line",
            "polyline",
            "polygon",
            "text",
            "image",
        ]
        if svg.find("use") and not svg.find(drawing) or small_icon(svg):
            svg.decompose()  # an icon that only points at a sprite draws nothing itself
            document.warnings.append("Icon omitted")
            continue
        if svg.get("viewbox"):
            svg["viewBox"] = svg.attrs.pop("viewbox")
        # HTML parsing drops namespace declarations; restore xlink and drop other prefixed names.
        for element in [svg, *svg.find_all(True)]:
            for key in list(element.attrs):
                if ":" in key and not key.startswith(("xlink:", "xmlns")) and key != "xml:space":
                    del element[key]
        if "xlink:" in str(svg) and not svg.get("xmlns:xlink"):
            svg["xmlns:xlink"] = "http://www.w3.org/1999/xlink"
        replacement = soup.new_tag(
            "img", src=asset(str(svg).encode()), alt=svg.get("aria-label") or "Diagram"
        )
        svg.replace_with(replacement)
    for image in list(root.find_all("img")):
        if image.get("src") in document.assets:
            continue
        href = image_source(image)
        decorative = image.get("role") == "presentation" or image.get("aria-hidden") == "true"
        if decorative:
            image.decompose()
            document.warnings.append("Decorative image omitted")
            continue
        if not href:  # nothing any reader could load, such as an empty ad placeholder
            image.decompose()
            document.warnings.append("Image with no source omitted")
            continue
        try:
            if href not in cache:
                cache[href] = asset(loader(href))
            image["src"] = cache[href]
            if "alt" not in image.attrs:
                image["alt"] = image.get("title") or "Image from source"
        except (OSError, ValueError) as error:
            if not decorative:
                raise ValueError(f"Required image asset unavailable: {href}") from error
            image.decompose()
            document.warnings.append("Unavailable decorative image omitted")
    # Pages often repeat an image for different screen sizes; keep one of each run.
    repeated, last_source = [], None
    for node in root.descendants:
        if getattr(node, "name", None) == "img":
            if node.get("src") == last_source:
                repeated.append(node)
            last_source = node.get("src")
        elif node.name is None and node.strip():  # text between images ends a run
            last_source = None
    for image in repeated:
        image.decompose()
    for details in root.find_all("details"):
        details["open"] = "open"
    for caption in root.find_all("figcaption"):
        if caption.parent.name != "figure":
            caption.name = "div"
            caption["class"] = ["figure-caption"]
    for anchor in root.find_all("a", attrs={"name": True}):
        if not anchor.get("id"):
            anchor["id"] = anchor["name"]
    # Tolerate legacy pages that use list elements as indentation containers.
    # Keep their reading blocks in order, grouping genuine items into valid lists.
    for listing in list(root.find_all(["ul", "ol", "menu"])):
        kind = "ol" if listing.name == "ol" else "ul"
        listing.name = kind
        if all(
            getattr(child, "name", None) == "li"
            or (getattr(child, "name", None) is None and not str(child).strip())
            for child in listing.contents
        ):
            continue
        attrs = {key: listing[key] for key in ["type", "reversed"] if listing.get(key) is not None}
        try:
            start = int(listing.get("start", 1))
        except ValueError:
            start = 1
        seen_items = 0
        listing.name = "div"
        for key in ["start", "type", "reversed"]:
            listing.attrs.pop(key, None)
        group = None
        for child in list(listing.contents):
            if getattr(child, "name", None) == "li":
                if group is None:
                    group = soup.new_tag(
                        kind,
                        attrs={**attrs, "start": str(start + seen_items)} if kind == "ol" else {},
                    )
                    child.insert_before(group)
                group.append(child.extract())
                seen_items += 1
            elif getattr(child, "name", None) is not None or str(child).strip():
                group = None
    for item in list(root.find_all("li")):
        if item.parent.name not in {"ul", "ol", "menu"}:
            group = soup.new_tag("ul")
            item.insert_before(group)
            group.append(item.extract())
    for node in list(root.find_all(True)):
        if node.parent is None:
            continue
        if node.name not in ALLOWED:
            node.unwrap()
            continue
        if node.get("href") and node.name != "a":
            href = reading_href(node["href"], source)
            if href and not node.find("a"):
                link = soup.new_tag("a", href=href)
                for child in list(node.contents):
                    link.append(child.extract())
                node.append(link)
            del node["href"]
        if node.name in {"p", "span"} and node.find(
            ["div", "section", "p", "table", "ul", "ol", "figure", "blockquote", "pre"]
        ):
            node.name = "div"
        preserved_class = node.get("class")
        if isinstance(preserved_class, str):
            preserved_class = preserved_class.split()
        specific = {
            "a": {"href"},
            "img": {"src", "alt"},
            "ol": {"start", "type", "reversed"},
            "li": {"value"},
            "td": {"colspan", "rowspan", "headers"},
            "th": {"colspan", "rowspan", "headers", "scope"},
            "time": {"datetime"},
        }
        allowed_attrs = {"id", "lang", "dir", "title"} | specific.get(node.name, set())
        for key in list(node.attrs):
            if key not in allowed_attrs:
                del node[key]
        if preserved_class:
            classes = [
                x
                for x in preserved_class
                if x
                in {
                    "math-inline",
                    "math-display",
                    "figure-caption",
                    "equation-layout",
                    "equation-row",
                }
            ]
            if classes:
                node["class"] = classes
        if id(node) in math_geometry:
            em_width, em_depth = math_geometry[id(node)]
            node["data-em-width"] = str(em_width)
            node["data-em-depth"] = str(em_depth)
        if node.name == "a" and node.get("href"):
            href = reading_href(node["href"], source)
            if href:
                node["href"] = href
            else:
                del node["href"]
    wrapper = BeautifulSoup("<body></body>", "html.parser")
    wrapper.body["lang"] = language
    if direction:
        wrapper.body["dir"] = direction
    if root.name in {"body", "[document]"}:
        for child in list(root.contents):
            wrapper.body.append(child.extract())
    else:
        wrapper.body.append(root)
    document.html = str(wrapper.body)
    return document
