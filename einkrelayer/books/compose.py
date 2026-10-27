"""Compose generic captured documents into one validated reading edition."""

import io
import json
import math
import re
import subprocess
import tempfile
from dataclasses import replace
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from urllib.parse import urldefrag, urlsplit
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont, features

from einkrelayer.books.answers import mark_answer_key, separate_answer_keys
from einkrelayer.books.design import CSS
from einkrelayer.books.tables import reflow_wide_tables
from einkrelayer.books.validation import epubcheck, validate
from einkrelayer.books.xhtml import document, to_xml, xml
from einkrelayer.capture.http import digest, normalized_url, write_json
from einkrelayer.diagnostics import event

OPF = "http://www.idpf.org/2007/opf"
DC = "http://purl.org/dc/elements/1.1/"
X = "http://www.w3.org/1999/xhtml"
NCX = "http://www.daisy.org/z3986/2005/ncx/"


def identity(source):
    if urlsplit(source).scheme in {"http", "https"}:
        url = normalized_url(source)
        parsed = urlsplit(url)
        if parsed.port == (443 if parsed.scheme == "https" else 80):
            host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
            url = parsed._replace(netloc=host).geturl()
        return url
    return urldefrag(source)[0]


def cover(job):
    image = Image.new("RGB", (1600, 2560), "#f6f2e8")
    draw = ImageDraw.Draw(image)
    ink = "#292929"
    cjk = any("\u2e80" <= c <= "\u9fff" or "\uac00" <= c <= "\ud7af" for c in job.title)
    if any("\u0590" <= c <= "\u08ff" for c in job.title) and not features.check_feature("raqm"):
        raise ValueError("Pillow complex text shaping is required for RTL covers")

    @cache
    def font(size, bold=False, title=False):
        if cjk and title:
            candidates = [
                "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
                str(Path.cwd() / ".cache/fonts/NotoSansCJKsc-Regular.otf"),
                "DroidSansFallbackFull.ttf",
            ]
            for path in candidates:
                try:
                    face = ImageFont.truetype(path, size)
                except OSError:
                    continue
                missing = bytes(face.getmask("\uffff"))
                if all(not c.strip() or bytes(face.getmask(c)) != missing for c in set(job.title)):
                    return face
            raise ValueError("Install fonts-noto-cjk for complete CJK cover glyphs")
        return ImageFont.truetype("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf", size)

    def lines(text, size, width=1320):
        face = font(size, True, title=True)
        output, line = [], ""
        for word in text.split():
            while draw.textlength(word, font=face) > width:
                if line:
                    output.append(line)
                    line = ""
                cut = max(1, int(len(word) * width / draw.textlength(word, font=face)))
                while cut > 1 and draw.textlength(word[:cut], font=face) > width:
                    cut -= 1
                output.append(word[:cut])
                word = word[cut:]
            candidate = (line + " " + word).strip()
            if draw.textlength(candidate, font=face) > width:
                output.append(line)
                line = word
            else:
                line = candidate
        return [*output, line] if line else output

    size = 110 if job.mode == "book" else 66
    max_height = 1500 if job.mode == "book" else 430
    while size > 28 and len(lines(job.title, size)) * size * 1.4 > max_height:
        size -= 4
    title_lines = lines(job.title, size)
    if len(title_lines) * size * 1.4 > max_height:
        raise ValueError("Title is too long to fit the cover legibly")
    y = 210 if job.mode == "digest" else 420
    for line in title_lines:
        draw.text((140, y), line, font=font(size, True, title=True), fill=ink)
        y += int(size * 1.4)
    draw.line(
        (140, 760 if job.mode == "digest" else 2050, 1460, 760 if job.mode == "digest" else 2050),
        fill=ink,
        width=4,
    )
    if job.mode == "digest":
        date = datetime.strptime(job.edition_date, "%Y-%m-%d")
        draw.text((140, 980), date.strftime("%b").upper(), font=font(140, True), fill=ink)
        draw.text((125, 1130), str(date.day), font=font(470, True), fill=ink)
        draw.text((150, 1760), str(date.year), font=font(120), fill=ink)
        if job.edition_number > 1:
            draw.text((150, 1980), f"Edition {job.edition_number}", font=font(66), fill=ink)
    draw.text(
        (140, 2260),
        f"{len(job.documents)} {'article' if len(job.documents) == 1 else 'articles'}",
        font=font(64),
        fill=ink,
    )
    output = io.BytesIO()
    image.save(output, "JPEG", quality=94)
    return output.getvalue()


def _wide_image(data):
    """Large raster images are figures; small icons and badges stay inline."""
    try:
        with Image.open(io.BytesIO(data)) as picture:
            return picture.width >= 300
    except (OSError, ValueError, Image.DecompressionBombError):
        return False


def _html(title, markup, *, language="en", direction=""):
    soup = BeautifulSoup("<body>" + markup + "</body>", "html.parser")
    return document(title, to_xml(soup.body), language=language, direction=direction)


def compose(job, destination):
    if job.mode not in {"book", "digest"} or not job.documents or len(job.documents) > 100:
        raise ValueError("Supply between 1 and 100 documents in book or digest mode")
    if not job.title.strip() or len(job.title) > 2000:
        raise ValueError("Supply a nonempty title of at most 2000 characters")
    seen, documents, duplicates = set(), [], []
    for item in job.documents:
        keys = {identity(source) for source in [item.source, *item.aliases]}
        if not keys & seen:
            seen.update(keys)
            documents.append(item)
        else:
            duplicates.append(
                {"source": item.source, "reason": "Same URL or verified redirect target"}
            )
    job = replace(job, documents=documents)
    if job.mode == "digest" and not job.edition_date:
        job.edition_date = datetime.now(UTC).date().isoformat()
    identifier = job.identifier or "urn:sha256:" + digest(
        json.dumps(
            {
                "title": job.title,
                "mode": job.mode,
                "date": job.edition_date,
                "edition": job.edition_number,
                "sources": [(d.source, d.input_sha256, digest(d.html.encode())) for d in documents],
            },
            sort_keys=True,
        ).encode()
    )
    job.identifier = identifier
    display_title = job.title
    if job.mode == "digest":
        display_title += " — " + job.edition_date
        if job.edition_number > 1:
            display_title += f" — Edition {job.edition_number}"
    names = {
        identity(source): f"chapter-{i:03d}.xhtml"
        for i, item in enumerate(documents, 1)
        for source in [item.source, *item.aliases]
    }
    chapters, assets, report, math_styles = {}, {}, [], {}
    from html import escape

    entries = []
    for index, item in enumerate(documents, 1):
        warnings = list(item.warnings)
        filename = names[identity(item.source)]
        soup = BeautifulSoup(item.html, "html.parser")
        body = soup.body or soup
        if not body.find("h1"):
            heading = soup.new_tag("h1")
            heading.string = item.title
            body.insert(0, heading)
        for image in body.find_all("img"):
            if image.get("src") not in item.assets:
                raise ValueError(f"Captured document has a missing image: {item.title}")
            # Standalone Word/Markdown images deserve the same responsive figure
            # layout as HTML/PDF figures; inline images and equations stay inline.
            parent = image.parent
            if (
                not image.get("data-em-width")
                and parent.name == "p"
                and not parent.get_text(strip=True)
                and parent.find_all(True) == [image]
            ):
                parent.name = "figure"
            elif (
                not image.get("data-em-width")
                and parent.name in {"div", "section", "article", "body"}
                and not any(isinstance(node, str) and node.strip() for node in parent.children)
                and _wide_image(item.assets[image["src"]])
            ):
                # A large image placed directly in a layout box is a figure too.
                image.wrap(soup.new_tag("figure"))
            if image.get("data-em-width"):
                width = float(image["data-em-width"])
                depth = float(image.get("data-em-depth", "0"))
                if (
                    not math.isfinite(width)
                    or not math.isfinite(depth)
                    or not 0 < width < 10000
                    or abs(depth) > 1000
                ):
                    raise ValueError("Invalid captured equation geometry")
                cls = "math-" + digest(f"{width:.4f}:{depth:.4f}".encode())[:16]
                image["class"] = [*image.get("class", []), cls]
                math_styles[cls] = (
                    f".{cls}{{width:{width:.4f}em;height:auto;max-width:100%;vertical-align:{-depth:.4f}em}}"
                )
        for anchor in body.find_all("a", href=True):
            target, fragment = urldefrag(anchor["href"])
            if target and identity(target) in names:
                anchor["href"] = names[identity(target)] + ("#" + fragment if fragment else "")
            elif urlsplit(target).scheme == "file":
                del anchor["href"]
                warnings.append("Unselected local-file link omitted; source text retained")
        words = len(body.get_text(" ", strip=True).split())
        cjk_chars = sum(
            "\u2e80" <= c <= "\u9fff" or "\uac00" <= c <= "\ud7af" for c in body.get_text()
        )
        reading_time = max(1, math.ceil(words / 200 + cjk_chars / 400))
        note = soup.new_tag("p", attrs={"class": "source-note"})
        note.string = f"Estimated reading time: {reading_time} min"
        if item.author:
            note.append(" · " + item.author)
        if item.published:
            note.append(" · " + item.published)
        if urlsplit(item.source).scheme in {"http", "https"}:
            link = soup.new_tag("a", href=item.source)
            link.string = "Original source"
            note.append(" · ")
            note.append(link)
        else:
            note.append(" · Saved source: " + Path(urlsplit(item.source).path).name)
        body.find("h1").insert_after(note)
        if job.answer_keys:
            mark_answer_key(soup, body)
        # Long readings list their main sections in the book contents.
        sections = []
        headings = [h for h in body.find_all("h2") if not h.find_parent(class_="answer-key")]
        if 2 <= len(headings) <= 80:
            taken = {node["id"] for node in body.find_all(id=True)}
            for number, heading in enumerate(headings, 1):
                if not heading.get("id"):
                    candidate = f"section-{number}"
                    while candidate in taken:
                        candidate += "-x"
                    heading["id"] = candidate
                    taken.add(candidate)
                label = heading.get_text(" ", strip=True)
                if label:
                    sections.append((heading["id"], label))
        navigation = soup.new_tag("p", attrs={"class": "chapter-links"})
        for title, href in (
            ([("Previous", f"chapter-{index - 1:03d}.xhtml")] if index > 1 else [])
            + [("Contents", "nav.xhtml")]
            + ([("Next", f"chapter-{index + 1:03d}.xhtml")] if index < len(documents) else [])
        ):
            if navigation.contents:
                navigation.append(" · ")
            link = soup.new_tag("a", href=href)
            link.string = title
            navigation.append(link)
        body.append(navigation)
        for heading in list(body.find_all(["h2", "h3", "h4", "h5", "h6"])):
            lead = heading.find_next_sibling()
            if lead is not None and lead.name == "p" and len(lead.get_text().split()) <= 120:
                group = soup.new_tag("div", attrs={"class": "heading-lead"})
                heading.insert_before(group)
                group.append(heading.extract())
                group.append(lead.extract())
        original_tables = [str(table) for table in body.find_all("table")]
        reflowed = reflow_wide_tables(soup)
        content = document(
            item.title, to_xml(body), language=item.language, direction=item.direction
        )
        # Composition audit compares captured text with the finished chapter.
        original_soup = BeautifulSoup(item.html, "html.parser")
        if reflowed:
            for table in original_soup.find_all("table"):
                table.decompose()
        captured = original_soup.get_text(" ", strip=True)
        audit_root = ET.fromstring(content)
        for parent in audit_root.iter():
            for child in list(parent):
                if (
                    child.get("class") in {"source-note", "chapter-links"}
                    or (reflowed and child.tag == f"{{{X}}}table")
                    or (reflowed and child.get("class") == "table-records")
                ):
                    if child.tail:
                        previous = list(parent).index(child) - 1
                        if previous >= 0:
                            parent[previous].tail = (parent[previous].tail or "") + child.tail
                        else:
                            parent.text = (parent.text or "") + child.tail
                    parent.remove(child)
        finished = " ".join(audit_root.itertext())

        def compact(s):
            return re.sub(r"\s+", "", s)

        if compact(captured) not in compact(finished):
            raise ValueError(f"Composition altered captured text: {item.title}")
        if reflowed:
            final_text = compact("".join(ET.fromstring(content).itertext()))
            for table in original_tables:
                for cell in BeautifulSoup(table, "html.parser").find_all(["th", "td", "caption"]):
                    if compact(cell.get_text()) not in final_text:
                        raise ValueError("Table reflow lost source content")
            warnings.append(f"Reflowed {reflowed} wide tables as labelled rows")
        chapters[filename] = content
        if sum(map(len, chapters.values())) > 32_000_000:
            raise ValueError("Book exceeds chapter text limit")
        for name, data in item.assets.items():
            if not name.startswith("assets/") or ".." in name.split("/"):
                raise ValueError("Unsafe captured asset name")
            if name in assets and assets[name] != data:
                raise ValueError("Captured asset name collision")
            if re.fullmatch(r"[a-f0-9]{64}", Path(name).stem) and digest(data) != Path(name).stem:
                raise ValueError("Captured asset hash mismatch")
            assets[name] = data
        if sum(map(len, assets.values())) > 200_000_000:
            raise ValueError("Book exceeds asset byte limit")
        entries.append((filename, item.title, sections))
        report.append(
            {
                "source": item.source,
                "aliases": item.aliases,
                "title": item.title,
                "input_sha256": item.input_sha256,
                "captured": item.captured,
                "assets": {n: digest(d) for n, d in item.assets.items()},
                "warnings": warnings,
                "reading_minutes": reading_time,
            }
        )
    answer_files = {}
    if job.answer_keys:
        chapters, answer_files = separate_answer_keys(chapters)
    toc = '<h1>Contents</h1><nav epub:type="toc" id="toc"><ol>'
    for filename, title, sections in entries:
        toc += f'<li><a href="{filename}">{escape(title)}</a>'
        children = [(f"{filename}#{anchor}", label) for anchor, label in sections]
        if filename in answer_files:
            children.append((answer_files[filename], "Answers"))
        if children:
            toc += "<ol>"
            toc += "".join(
                f'<li><a href="{href}">{escape(label)}</a></li>' for href, label in children
            )
            toc += "</ol>"
        toc += "</li>"
    toc += '</ol></nav><nav epub:type="landmarks" hidden="hidden"><ol><li><a epub:type="cover" href="cover.xhtml">Cover</a></li><li><a epub:type="bodymatter" href="chapter-001.xhtml">Reading</a></li></ol></nav>'
    # Serialization only permits explicit attributes; add EPUB nav roles afterward.
    nav = ET.fromstring(_html("Contents", toc))
    navs = list(nav.iter(f"{{{X}}}nav"))
    for node, role in zip(navs, ["toc", "landmarks"], strict=True):
        node.set("{http://www.idpf.org/2007/ops}type", role)
    for anchor, role in zip(navs[1].iter(f"{{{X}}}a"), ["cover", "bodymatter"], strict=True):
        anchor.set("{http://www.idpf.org/2007/ops}type", role)
    chapters = {
        "cover.xhtml": _html(
            "Cover",
            '<div class="cover-page"><p><img src="assets/cover.jpg" alt="Cover"/></p></div>',
        ),
        "titlepage.xhtml": _html(
            display_title,
            f'<div class="titlepage"><h1>{escape(display_title)}</h1><p>{len(documents)} {"article" if len(documents) == 1 else "articles"}</p></div>',
        ),
        "nav.xhtml": xml(nav),
        **chapters,
    }
    assets["assets/cover.jpg"] = cover(job)
    package = ET.Element(
        f"{{{OPF}}}package", {"version": "3.0", "unique-identifier": "book-id", "xml:lang": "en"}
    )
    metadata = ET.SubElement(package, f"{{{OPF}}}metadata")
    ET.SubElement(metadata, f"{{{DC}}}identifier", {"id": "book-id"}).text = identifier
    ET.SubElement(metadata, f"{{{DC}}}title").text = display_title
    ET.SubElement(metadata, f"{{{DC}}}language").text = documents[0].language
    ET.SubElement(metadata, f"{{{DC}}}creator").text = "einkrelayer"
    ET.SubElement(metadata, f"{{{OPF}}}meta", {"property": "dcterms:modified"}).text = datetime.now(
        UTC
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    ET.SubElement(metadata, f"{{{OPF}}}meta", {"name": "cover", "content": "cover-image"})
    manifest = ET.SubElement(package, f"{{{OPF}}}manifest")
    spine = ET.SubElement(package, f"{{{OPF}}}spine", {"toc": "ncx"})
    for i, name in enumerate(chapters):
        attrs = {"id": f"doc-{i}", "href": name, "media-type": "application/xhtml+xml"}
        if name == "nav.xhtml":
            attrs["properties"] = "nav"
        ET.SubElement(manifest, f"{{{OPF}}}item", attrs)
        ET.SubElement(spine, f"{{{OPF}}}itemref", {"idref": f"doc-{i}"})
    ET.SubElement(
        manifest, f"{{{OPF}}}item", {"id": "css", "href": "reader.css", "media-type": "text/css"}
    )
    for i, name in enumerate(assets):
        attrs = {
            "id": "cover-image" if name == "assets/cover.jpg" else f"asset-{i}",
            "href": name,
            "media-type": "image/jpeg" if name.endswith(".jpg") else "image/png",
        }
        if name == "assets/cover.jpg":
            attrs["properties"] = "cover-image"
        ET.SubElement(manifest, f"{{{OPF}}}item", attrs)
    ET.SubElement(
        manifest,
        f"{{{OPF}}}item",
        {"id": "ncx", "href": "toc.ncx", "media-type": "application/x-dtbncx+xml"},
    )
    ncx = ET.Element(f"{{{NCX}}}ncx", {"version": "2005-1"})
    head = ET.SubElement(ncx, f"{{{NCX}}}head")
    ET.SubElement(head, f"{{{NCX}}}meta", {"name": "dtb:uid", "content": identifier})
    ET.SubElement(ET.SubElement(ncx, f"{{{NCX}}}docTitle"), f"{{{NCX}}}text").text = display_title
    navmap = ET.SubElement(ncx, f"{{{NCX}}}navMap")
    order = 0

    def navpoint(parent, label, src):
        nonlocal order
        order += 1
        point = ET.SubElement(
            parent, f"{{{NCX}}}navPoint", {"id": f"p{order}", "playOrder": str(order)}
        )
        ET.SubElement(ET.SubElement(point, f"{{{NCX}}}navLabel"), f"{{{NCX}}}text").text = label
        ET.SubElement(point, f"{{{NCX}}}content", {"src": src})
        return point

    for filename, title, sections in entries:
        point = navpoint(navmap, title, filename)
        for anchor, label in sections:
            navpoint(point, label, f"{filename}#{anchor}")
        if filename in answer_files:
            navpoint(navmap, "Answers — " + title, answer_files[filename])
    container = b'<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>'
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".einkrelayer-", dir=destination.parent) as temporary:
        candidate = Path(temporary) / "book.epub"
        with ZipFile(candidate, "w", compression=ZIP_DEFLATED) as archive:
            archive.writestr("mimetype", "application/epub+zip", compress_type=ZIP_STORED)
            archive.writestr("META-INF/container.xml", container)
            for name, data in {
                "content.opf": xml(package),
                "reader.css": (CSS + "\n" + "\n".join(math_styles.values())).encode(),
                "toc.ncx": xml(ncx),
                **chapters,
                **assets,
            }.items():
                archive.writestr("OEBPS/" + name, data)
        validate(candidate)
        checker = epubcheck()
        if checker:
            try:
                completed = subprocess.run(
                    [*checker, str(candidate)], check=True, timeout=120, capture_output=True
                )
                detail = ((completed.stdout or b"") + (completed.stderr or b""))[-500:]
                event("epubcheck", destination, "success", detail.decode("utf-8", errors="replace"))
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
                detail = (error.stderr or error.stdout or b"")[-1000:]
                if isinstance(detail, bytes):
                    detail = detail.decode("utf-8", errors="replace")
                event("epubcheck", destination, "failed", detail)
                raise ValueError(
                    "EPUBCheck rejected the book or exceeded its time limit"
                ) from error
        else:
            event("epubcheck", destination, "skipped", "Not installed; structural checks passed")
        candidate.chmod(0o600)
        candidate.replace(destination)
    result = {
        "epub": str(destination),
        "sha256": digest(destination.read_bytes()),
        "identifier": identifier,
        "title": display_title,
        "mode": job.mode,
        "epubcheck": bool(checker),
        "documents": report,
        "duplicates": duplicates,
        "answer_keys": len(answer_files),
    }
    write_json(destination.with_suffix(".report.json"), result)
    return result
