"""Bounded PDF/DOCX readers. The public parse entry uses a timed child process."""

import base64
import io
import json
import re
import subprocess
import sys
import tempfile
from collections import Counter
from contextlib import redirect_stdout
from dataclasses import asdict
from html import escape
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

import mammoth
import pymupdf

from einkrelayer.capture.html import MAX_ASSET_BYTES, MAX_ASSETS, extract, image_bytes
from einkrelayer.capture.http import digest
from einkrelayer.models import Document


def _asset(document, data):
    content, suffix = image_bytes(data)
    name = f"assets/{digest(content)}.{suffix}"
    if name not in document.assets:
        if (
            len(document.assets) >= MAX_ASSETS
            or sum(map(len, document.assets.values())) + len(content) > MAX_ASSET_BYTES
        ):
            raise ValueError("Document exceeds asset limits")
        document.assets[name] = content
    return name


def _block_text(block):
    return "\n".join(
        "".join(span["text"] for span in line["spans"]) for line in block.get("lines", [])
    )


TOC_LINE = re.compile(r"(?:\.\s*){4,}\s*[\w-]*\s*$")
BLANK_PAGE = re.compile(r"(?:this\s+)?page\s+(?:is\s+)?intentionally\s+(?:left\s+)?blank\.?", re.I)
CAPTION = re.compile(r"(?:figure|fig\.|table|chart|exhibit)\s+[\w.-]+\s*[.:\u2013\u2014-]", re.I)
MARKER = re.compile(r"(?:[\u2022\u25cf\u25aa\u25e6\u2023\u2219\u00b7]|o(?=\s)|\(?\d{1,2}[.)])\s*")
MARKER_ONLY = re.compile(r"[\u2022\u25cf\u25aa\u25e6\u2023\u2219\u00b7o]|\(?\d{1,2}[.)]")
SENTENCE_END = re.compile(r"[.!?:;)\]\"\u201d\u2019]$")


def _edge_key(text):
    """Running headers/footers repeat with changing page numbers."""
    return re.sub(r"\b(?:\d+|[ivxlcdm]+)\b", "#", re.sub(r"\s+", " ", text).strip().lower())


def _line_text(line):
    out, last = "", None
    for span in line["spans"]:
        text = span["text"]
        gap = last is not None and span["bbox"][0] - last > max(2.0, span["size"] * 0.22)
        if gap and text and not text[0].isspace() and out and not out[-1].isspace():
            out += " "
        out += text
        last = span["bbox"][2]
    return re.sub(r"\s+", " ", out.replace("\u00ad", "")).strip()


def _lines(block):
    lines = []
    for line in block.get("lines", []):
        spans = [span for span in line["spans"] if span["text"].strip()]
        text = _line_text(line)
        if spans and text:
            bold = all("bold" in span["font"].lower() or span["flags"] & 16 for span in spans)
            lines.append((text, max(span["size"] for span in spans), bold))
    return lines


def _join(texts):
    out = ""
    for text in texts:
        if out.endswith("-") and text[:1].islower():
            out = out[:-1] + text
        else:
            out += (" " if out else "") + text
    return out


def _body(texts):
    merged = []
    for text in texts:
        if merged and MARKER_ONLY.fullmatch(merged[-1]):
            merged[-1] += " " + text
        else:
            merged.append(text)
    letter_bullets = sum(text.startswith("o ") for text in merged)

    def item(text):
        match = MARKER.match(text)
        if match and (not text.startswith("o") or letter_bullets >= 2) and text[match.end() :]:
            return match
        return None

    if item(merged[0]):
        entries = []
        for text in merged:
            match = item(text)
            if match:
                entries.append(text[match.end() :].strip())
            else:
                entries[-1] = _join([entries[-1], text])
        number = re.match(r"\(?(\d+)", merged[0])
        return [("ol" if number else "ul", entries, int(number[1]) if number else 1)]
    text = _join(merged)
    return [("caption" if CAPTION.match(text) else "p", text)]


def _classify(block, body):
    """Turn one PDF text block into reading items using document-wide font evidence."""
    lines = _lines(block)
    if not lines:
        return []
    texts = [line[0] for line in lines]
    whole = _join(texts)
    if BLANK_PAGE.fullmatch(whole):
        return []
    if sum(bool(TOC_LINE.search(text)) for text in texts) * 2 >= len(texts):
        return [("toc",)]  # A printed contents page; the book builds its own navigation.
    size = max(line[1] for line in lines)
    top, bottom = block["bbox"][1], block["bbox"][3]
    if len(whole) <= 200 and len(lines) <= 3 and not CAPTION.match(whole):
        if size >= body * 1.12:
            return [("h2", whole, size, top, bottom)]
        if all(line[2] for line in lines) and size >= body * 0.95:
            return [("h3", whole, size, top, bottom)]
    first, rest = lines[0], lines[1:]
    if first[2] and rest and not rest[0][2] and len(first[0]) <= 120:
        if not CAPTION.match(first[0]) and not MARKER.match(first[0]):
            return [("h3", first[0], first[1], top, bottom)] + _body([line[0] for line in rest])
    return _body(texts)


def _clean_table(rows):
    """Undo PDF layout artifacts: wrapped-cell rows, empty and shifted columns."""
    width = max(map(len, rows), default=0)
    header = sum(bool(cell and cell.strip()) for cell in rows[0]) if rows else 0
    table = []
    for row in rows:
        cells = [(cell or "").strip() for cell in row] + [""] * (width - len(row))
        filled = sum(bool(cell) for cell in cells)
        if table and None in row and filled < header:
            for index, cell in enumerate(cells):
                if cell:
                    table[-1][index] = _join([table[-1][index], cell]).strip()
            continue
        if filled:
            table.append(cells)
    columns = [i for i in range(width) if any(row[i] for row in table)]
    table = [[row[i] for i in columns] for row in table]
    index = 0
    while table and index < len(table[0]) - 1:
        if not any(row[index] and row[index + 1] for row in table):
            for row in table:
                row[index] = row[index] or row[index + 1]
                del row[index + 1]
        else:
            index += 1
    return table


def _render(items):
    """Join paragraphs and lists split by blocks or pages, attach captions, emit HTML."""
    merged = []

    def previous():
        return next((item for item in reversed(merged) if item[0] != "anchor"), None)

    for item in items:
        last = previous()
        kind = item[0]
        adjacent = merged[-1] if merged else None
        if kind in {"h2", "h3"} and adjacent and adjacent[0] == kind:
            close = 0 <= item[3] - adjacent[4] <= item[2] * 0.8
            if close and abs(adjacent[2] - item[2]) < 0.3 and not SENTENCE_END.search(adjacent[1]):
                adjacent[1] = _join([adjacent[1], item[1]])
                adjacent[4] = item[4]
                continue
        if kind == "p" and last and item[1][:1].islower():
            if last[0] == "p" and not SENTENCE_END.search(last[1]):
                last[1] = _join([last[1], item[1]])
                continue
            if last[0] in {"ul", "ol"}:
                last[1][-1] = _join([last[1][-1], item[1]])
                continue
        if kind in {"ul", "ol"} and last and last[0] == kind:
            last[1].extend(item[1])
            continue
        if kind == "table" and last and last[0] == "table" and last[1][0] == item[1][0]:
            last[1].extend(item[1][1:])  # A table continued on the next page.
            continue
        if kind == "caption" and last and last[0] == "figure" and not last[3]:
            last[3] = item[1]
            continue
        if kind == "figure" and last and last[0] == "caption":
            merged.remove(last)
            item = [*item[:3], last[1]]
        merged.append(list(item))
    html = []
    for item in merged:
        kind = item[0]
        if kind == "anchor":
            html.append(f'<span id="pdf-page-{item[1]}"></span>')
        elif kind in {"h2", "h3", "p"}:
            html.append(f"<{kind}>{escape(item[1])}</{kind}>")
        elif kind == "caption":
            html.append(f'<p class="figure-caption">{escape(item[1])}</p>')
        elif kind in {"ul", "ol"}:
            start = f' start="{item[2]}"' if kind == "ol" and item[2] != 1 else ""
            entries = "".join(f"<li>{escape(entry)}</li>" for entry in item[1])
            html.append(f"<{kind}{start}>{entries}</{kind}>")
        elif kind == "table":
            rows = "".join(
                "<tr>" + "".join(f"<{tag}>{escape(cell)}</{tag}>" for cell in row) + "</tr>"
                for tag, row in [("th", item[1][0])] + [("td", row) for row in item[1][1:]]
            )
            html.append(f"<table>{rows}</table>")
        elif kind == "figure":
            caption = f"<figcaption>{escape(item[3])}</figcaption>" if item[3] else ""
            alt = escape(item[3] or item[2], quote=True)
            html.append(f'<figure><img src="{item[1]}" alt="{alt}"/>{caption}</figure>')
        else:
            html.append(item[1])
    return "".join(html)


def _vector_regions(page, table_boxes):
    groups = []
    drawings = page.get_drawings()
    if len(drawings) > 5000:
        raise ValueError("PDF exceeds vector drawing limit")
    for drawing in drawings:
        box = pymupdf.Rect(drawing["rect"])
        meaningful = any(item[0] != "re" for item in drawing["items"])
        if not meaningful and box.get_area() > page.rect.get_area() * 0.5:
            continue  # Large page backgrounds are not reading figures.
        if any(table.intersects(box) or table.contains(box) for table in table_boxes):
            continue
        padded = pymupdf.Rect(box.x0 - 12, box.y0 - 12, box.x1 + 12, box.y1 + 12)
        matches = [group for group in groups if padded.intersects(group[0])]
        for group in matches:
            other = group[0]
            box = pymupdf.Rect(
                min(box.x0, other.x0),
                min(box.y0, other.y0),
                max(box.x1, other.x1),
                max(box.y1, other.y1),
            )
            meaningful |= group[1]
            groups.remove(group)
        groups.append((box, meaningful))
    return [
        pymupdf.Rect(box.x0 - 8, box.y0 - 8, box.x1 + 8, box.y1 + 8) & page.rect
        for box, meaningful in groups
        if meaningful and box.width > 8 and box.height > 8
    ]


def pdf(data, source):
    if not data.startswith(b"%PDF-"):
        raise ValueError("Invalid PDF file signature")
    result = Document(source=source, title="PDF document", html="")
    try:
        book = pymupdf.open(stream=data, filetype="pdf")
    except (RuntimeError, ValueError) as error:
        raise ValueError("Corrupt or truncated PDF") from error
    with book:
        if book.needs_pass:
            raise ValueError("Encrypted PDF is unsupported")
        if not 0 < book.page_count <= 300:
            raise ValueError("PDF exceeds page limit")
        result.title = book.metadata.get("title") or result.title
        result.author = book.metadata.get("author") or ""
        edges, sizes = Counter(), Counter()
        for page in book:
            for block in page.get_text("dict")["blocks"]:
                if block["type"] != 0:
                    continue
                for line in block["lines"]:
                    for span in line["spans"]:
                        if span["text"].strip():
                            sizes[round(span["size"], 1)] += len(span["text"])
                box = block["bbox"]
                if box[3] < page.rect.height * 0.07 or box[1] > page.rect.height * 0.93:
                    edges[_edge_key(_block_text(block))] += 1
        repeated = {key for key, count in edges.items() if count > 1}
        body = sizes.most_common(1)[0][0] if sizes else 11
        items, links = [], {}
        total_text = 0
        for number, page in enumerate(book, 1):
            blocks = page.get_text("dict")["blocks"]
            if not any(_block_text(b).strip() for b in blocks if b["type"] == 0):
                raise ValueError(f"PDF page {number} requires OCR or a readable static export")
            items.append(("anchor", number))
            tables = [
                table
                for table in page.find_tables().tables
                if any(cell and cell.strip() for row in table.extract() for cell in row)
            ]
            table_boxes = [pymupdf.Rect(table.bbox) for table in tables]
            entries = []
            for table in tables:
                rows = table.extract()
                if sum(map(len, rows)) > 50_000:
                    raise ValueError("PDF table exceeds cell limit")
                rows = _clean_table(rows)
                if rows:
                    entries.append((pymupdf.Rect(table.bbox), ("table", rows)))
            for block in blocks:
                box = pymupdf.Rect(block["bbox"])
                if any(table.contains(box) for table in table_boxes):
                    continue
                if block["type"] == 1:
                    name = _asset(result, block["image"])
                    alt = f"Figure from PDF page {number}"
                    entries.append((box, ("figure", name, alt, "")))
                    continue
                text = _block_text(block).strip()
                edge = box.y1 < page.rect.height * 0.07 or box.y0 > page.rect.height * 0.93
                if not text or (edge and _edge_key(text) in repeated):
                    continue
                if "\ufffd" in text:
                    raise ValueError("PDF contains undecodable text glyphs")
                total_text += len(text)
                if total_text > 2_000_000:
                    raise ValueError("PDF exceeds extracted text limit")
                classified = _classify(block, body)
                if classified and number == 1 and result.title == "PDF document":
                    if classified[0][0] == "h2":
                        result.title = classified[0][1]
                for item in classified:
                    entries.append((box, item))
            prose = any(entry[1][0] == "p" and len(entry[1][1]) > 200 for entry in entries)
            if number == 1 and book.page_count > 3 and not prose:
                # A title page: keep its lines as text, not as book sections.
                entries = [
                    (box, ("p", item[1]) if item[0] in {"h2", "h3"} else item)
                    for box, item in entries
                ]
            text_items = [entry for entry in entries if entry[1][0] not in {"figure", "table"}]
            contents = sum(entry[1][0] == "toc" for entry in text_items)
            if contents >= 3 and contents * 2 >= len(text_items):
                entries = [entry for entry in entries if entry[1][0] in {"figure", "table"}]
            entries = [entry for entry in entries if entry[1][0] != "toc"]
            # PDF charts are often vector paths rather than embedded images.
            # Preserve those regions as zoomable figures instead of dropping them.
            for box in _vector_regions(page, table_boxes):
                if box.width * box.height * 4 > 40_000_000:
                    raise ValueError("PDF vector figure exceeds pixel limit")
                image = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), clip=box).tobytes("png")
                name = _asset(result, image)
                entries = [
                    entry
                    for entry in entries
                    if not box.contains(entry[0]) or entry[1][0] == "caption"
                ]
                alt = f"Vector figure from PDF page {number}"
                entries.append((box, ("figure", name, alt, "")))
            # Clear two-column layouts are read down the left column then right.
            # Full-width elements before/after the columns retain their position.
            midpoint = page.rect.width / 2
            left = [entry for entry in entries if entry[0].x1 < midpoint]
            right = [entry for entry in entries if entry[0].x0 > midpoint]
            if len(left) >= 2 and len(right) >= 2:
                middle = [entry for entry in entries if entry not in left and entry not in right]
                first_y = min(entry[0].y0 for entry in left + right)
                last_y = max(entry[0].y1 for entry in left + right)
                if any(first_y < entry[0].y0 < last_y for entry in middle):
                    raise ValueError("Ambiguous PDF column reading order requires review")
                entries = (
                    sorted(
                        [entry for entry in middle if entry[0].y0 <= first_y], key=lambda e: e[0].y0
                    )
                    + sorted(left, key=lambda e: e[0].y0)
                    + sorted(right, key=lambda e: e[0].y0)
                    + sorted(
                        [entry for entry in middle if entry[0].y0 > last_y], key=lambda e: e[0].y0
                    )
                )
            else:
                entries.sort(key=lambda entry: (entry[0].y0, entry[0].x0))
            items.extend(item for _, item in entries)
            for link in page.get_links():
                href = link.get("uri") or ""
                if href.startswith(("http://", "https://", "mailto:")) and href not in links:
                    label = re.sub(r"\s+", " ", page.get_textbox(link["from"])).strip()
                    links[href] = label or href
        content = [_render(items)]
        if links:
            content.append("<h2>Links in this document</h2><ul>")
            content.extend(
                f'<li><a href="{escape(href, quote=True)}">{escape(label)}</a></li>'
                for href, label in links.items()
            )
            content.append("</ul>")
        result.html = "<body>" + "".join(content) + "</body>"
        if repeated:
            result.warnings.append("Repeated running headers/footers omitted")
    return result


def docx(data, source):
    properties = {}
    try:
        with ZipFile(io.BytesIO(data)) as archive:
            info = archive.infolist()
            if len(info) > 5000 or sum(item.file_size for item in info) > 128_000_000:
                raise ValueError("DOCX exceeds container resource limits")
            if any(item.flag_bits & 1 for item in info):
                raise ValueError("Encrypted DOCX is unsupported")
            if "word/document.xml" not in archive.namelist():
                raise ValueError("Unsupported ZIP container; expected DOCX")
            if any(name.endswith("vbaProject.bin") for name in archive.namelist()):
                raise ValueError("Macro-enabled documents are unsupported")
            # Fail rather than silently lose content python-docx does not expose.
            xml = archive.read("word/document.xml")
            if len(xml) > 8_000_000:
                raise ValueError("DOCX exceeds text XML resource limit")
            tree = ET.fromstring(xml)
            if any(
                element.tag.rsplit("}", 1)[-1]
                in {
                    "altChunk",
                    "OLEObject",
                    "oMath",
                    "oMathPara",
                    "txbxContent",
                    "pict",
                    "ins",
                    "del",
                }
                for element in tree.iter()
            ):
                raise ValueError("Unsupported embedded DOCX content requires review")
            if "docProps/core.xml" in archive.namelist():
                metadata = ET.fromstring(archive.read("docProps/core.xml"))
                dc = "{http://purl.org/dc/elements/1.1/}"
                properties = {
                    name: metadata.findtext(dc + name) or ""
                    for name in ["title", "creator", "language"]
                }
    except (BadZipFile, KeyError, ET.ParseError) as error:
        raise ValueError("Corrupt or truncated DOCX") from error
    resources = Document(source=source, title="", html="")

    def convert_image(image):
        with image.open() as stream:
            return {"src": _asset(resources, stream.read())}

    converted = mammoth.convert_to_html(
        io.BytesIO(data),
        convert_image=mammoth.images.img_element(convert_image),
        style_map="p[style-name='Title'] => h1:fresh\np[style-name='Subtitle'] => p:fresh",
        external_file_access=False,
        include_embedded_style_map=False,
    )
    for message in converted.messages:
        style_warning = message.message.startswith(
            ("Unrecognised paragraph style", "Unrecognised run style")
        )
        if not style_warning:
            raise ValueError("DOCX contains unsupported substantive content: " + message.message)
    result = extract(converted.value.encode(), source, lambda href: resources.assets[href])
    result.title = properties.get("title") or (
        result.title if result.title != "Untitled page" else "Word document"
    )
    result.author = properties.get("creator", "")
    result.language = properties.get("language") or result.language
    if result.language.split("-")[0] in {"ar", "he", "fa", "ur"}:
        result.direction = "rtl"
    result.warnings.extend(message.message for message in converted.messages)
    return result


def parse(kind, data, source):
    with tempfile.TemporaryDirectory(prefix="einkrelayer-parse-") as temporary:
        path = Path(temporary) / "input"
        path.write_bytes(data)
        try:
            process = subprocess.run(
                [sys.executable, "-m", __name__, kind, str(path)], capture_output=True, timeout=60
            )
        except subprocess.TimeoutExpired as error:
            raise ValueError("Document parsing exceeded 60 seconds") from error
    if process.returncode:
        # Child emits a bounded, non-secret reason rather than a parser traceback.
        raise ValueError(process.stderr.decode(errors="replace")[:500] or "Document parsing failed")
    result = json.loads(process.stdout)
    result["assets"] = {name: base64.b64decode(value) for name, value in result["assets"].items()}
    result["source"] = source
    return Document(**result)


if __name__ == "__main__":
    try:
        if sys.platform != "win32":
            import resource

            resource.setrlimit(resource.RLIMIT_AS, (1_073_741_824, 1_073_741_824))
        kind, path = sys.argv[1:]
        with redirect_stdout(io.StringIO()):
            result = (pdf if kind == "pdf" else docx)(Path(path).read_bytes(), "")
        value = asdict(result)
        value["assets"] = {
            name: base64.b64encode(data).decode() for name, data in result.assets.items()
        }
        print(json.dumps(value))
    except Exception as error:
        # The synchronous parser process is a failure boundary: unexpected
        # library errors hold the input, never publish partially extracted text.
        message = (
            str(error)
            if isinstance(error, (ValueError, OSError, RuntimeError, KeyError))
            else f"Unsupported document content ({type(error).__name__})"
        )
        print(message[:500], file=sys.stderr)
        raise SystemExit(1)
