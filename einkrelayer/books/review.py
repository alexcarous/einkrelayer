#!/usr/bin/env python3
"""Render EPUB review samples and audit reflow using MuPDF, not Kindle Previewer."""

import argparse
import hashlib
import json
import posixpath
import re
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pymupdf

from einkrelayer.books.validation import validate

ROOT = Path.cwd()
LAYOUTS = (
    ("ereader", 420, 640, 11),
    ("phone", 360, 640, 11),
    ("large-text", 420, 640, 17),
    ("landscape", 640, 420, 11),
)


def sample_locations(epub):
    """Select samples by EPUB structure, not by chapter position or wording."""
    ns = {
        "c": "urn:oasis:names:tc:opendocument:xmlns:container",
        "o": "http://www.idpf.org/2007/opf",
    }
    with ZipFile(epub) as archive:
        container = ET.fromstring(archive.read("META-INF/container.xml"))
        package_path = container.find(".//c:rootfile", ns).get("full-path")
        package = ET.fromstring(archive.read(package_path))
        items = {item.get("id"): item for item in package.findall("o:manifest/o:item", ns)}
        samples = {}
        for chapter, ref in enumerate(package.findall("o:spine/o:itemref", ns)):
            item = items[ref.get("idref")]
            path = posixpath.normpath(
                posixpath.join(posixpath.dirname(package_path), item.get("href"))
            )
            root = ET.fromstring(archive.read(path))
            if chapter == 0:
                samples["cover"] = (chapter, "")
            if any("titlepage" in el.get("class", "").split() for el in root.iter()):
                samples["title"] = (chapter, "")
            if "nav" in item.get("properties", "").split():
                samples["contents"] = (chapter, "")
            reading = any("source-note" in el.get("class", "").split() for el in root.iter())
            if reading and "reading" not in samples:
                samples["reading"] = (chapter, "")
            if chapter == 0:
                continue
            for element in root.iter():
                kind = element.tag.rsplit("}", 1)[-1]
                category = {"table": "table", "pre": "code", "figure": "figure"}.get(kind)
                if category == "figure" and not any(
                    el.tag.rsplit("}", 1)[-1] == "img" for el in element.iter()
                ):
                    category = None
                if "math-inline" in element.get("class", "") or "math-display" in element.get(
                    "class", ""
                ):
                    category = "equation"
                if "answer-key" in element.get("class", "").split():
                    category = "answer-key"
                if element.get("dir") == "rtl":
                    samples.setdefault("rtl", (chapter, ""))
                if element.get("lang", "").startswith(("zh", "ja", "ko")):
                    samples.setdefault("cjk", (chapter, ""))
                if category:
                    text = "".join(element.itertext()).strip()
                    if category == "code":
                        text = next((line for line in text.splitlines() if line.strip()), text)
                    if kind == "table":
                        first_cell = next(
                            (
                                el
                                for el in element.iter()
                                if el.tag.rsplit("}", 1)[-1] in {"th", "td"}
                            ),
                            None,
                        )
                        if first_cell is not None:
                            text = "".join(first_cell.itertext()).strip()
                    marker = re.sub(r"\s+", "", text)[: 16 if category == "code" else 32]
                    if category in {"figure", "equation"}:
                        marker = "@image"
                    samples.setdefault(category, (chapter, marker))
        if "reading" not in samples:
            samples["reading"] = (max(chapter, 0), "")
        return samples


def review(epub, output):
    validate(epub)
    output.mkdir(parents=True, exist_ok=True)
    report = {
        "epub_sha256": hashlib.sha256(epub.read_bytes()).hexdigest(),
        "renderer": "MuPDF " + pymupdf.VersionBind,
        "scope": "Responsive layout checks; not an Amazon conversion or device preview",
        "layouts": [],
    }
    locations = sample_locations(epub)
    preview = pymupdf.open()
    with pymupdf.open(epub) as book:
        for label, width, height, font in LAYOUTS:
            book.layout(width=width, height=height, fontsize=font)
            clipped_text = []
            clipped_images = []
            starts = {
                name: book.page_number_from_location((chapter, 0))
                for name, (chapter, _) in locations.items()
            }
            selected = {name: start for name, start in starts.items() if not locations[name][1]}
            missing_glyphs = []
            for number, page in enumerate(book):
                data = page.get_text("dict")
                for block in data["blocks"]:
                    boxes = (
                        [line["bbox"] for line in block["lines"]]
                        if block["type"] == 0
                        else [block["bbox"]]
                    )
                    for x0, y0, x1, y1 in boxes:
                        if x0 < -1 or y0 < -1 or x1 > width + 1 or y1 > height + 1:
                            (clipped_text if block["type"] == 0 else clipped_images).append(
                                number + 1
                            )
                text = re.sub(r"\s+", "", page.get_text())
                if "\ufffd" in text:
                    missing_glyphs.append(number + 1)
                for category, (chapter, marker) in locations.items():
                    if category in selected:
                        continue
                    start = starts[category]
                    matches = (
                        any(block["type"] == 1 for block in data["blocks"])
                        if marker == "@image"
                        else marker in text
                    )
                    if start <= number < start + book.chapter_page_count(chapter) and matches:
                        selected[category] = number
            for name, number in selected.items():
                page = book[number]
                filename = f"{label}-{name}.png"
                pixmap = page.get_pixmap(
                    matrix=pymupdf.Matrix(1.5, 1.5),
                    colorspace=pymupdf.csGRAY
                    if label == "ereader" and name != "cover"
                    else pymupdf.csRGB,
                )
                pixmap.save(str(output / filename))
                if label == "ereader":
                    sheet = preview.new_page(width=width + 24, height=height + 40)
                    sheet.insert_text(
                        (12, 16), f"MuPDF reflow sample: {name} / page {number + 1}", fontsize=9
                    )
                    sheet.insert_image(pymupdf.Rect(12, 28, width + 12, height + 28), pixmap=pixmap)
            entry = {
                "name": label,
                "width": width,
                "height": height,
                "base_font_size": font,
                "pages": book.page_count,
                "cover_pages": book.chapter_page_count(0),
                "clipped_text_pages": sorted(set(clipped_text)),
                "clipped_image_pages": sorted(set(clipped_images)),
                "missing_glyph_pages": missing_glyphs,
                "unmatched_sample_categories": sorted(set(locations) - set(selected)),
                "samples": {
                    name: {"page": number + 1, "file": f"{label}-{name}.png"}
                    for name, number in selected.items()
                },
            }
            report["layouts"].append(entry)
    preview.save(output / "reading-samples.pdf")
    preview.close()
    (output / "layout-report.json").write_text(json.dumps(report, indent=2) + "\n")
    failures = [
        r["name"]
        for r in report["layouts"]
        if r["clipped_text_pages"]
        or r["clipped_image_pages"]
        or r["cover_pages"] != 1
        or r["missing_glyph_pages"]
        or r["unmatched_sample_categories"]
    ]
    print(
        json.dumps(
            {"layouts": len(LAYOUTS), "failures": failures, "review_directory": str(output)},
            indent=2,
        )
    )
    return 1 if failures else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--epub", type=Path, default=ROOT / "output" / "book.epub")
    parser.add_argument("--output", type=Path, default=ROOT / "output" / "review")
    args = parser.parse_args()
    raise SystemExit(review(args.epub, args.output))
