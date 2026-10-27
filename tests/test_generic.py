import io
import json
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

import pymupdf
from bs4 import BeautifulSoup

from einkrelayer.books.compose import compose, identity
from einkrelayer.books.review import review
from einkrelayer.capture.collections import contents_links, discover
from einkrelayer.capture.http import Response
from einkrelayer.capture.readers import read
from einkrelayer.models import BookJob, Document
from tools.corpus_checks import check_expectations
from tools.document_samples import create_samples

CORPUS = Path(__file__).parent / "corpus"


def normalized(text):
    return re.sub(r"\s+", " ", text).strip()


def check_expected(test, case, document):
    test.assertEqual(check_expectations(case, document), [], case["file"])


class GenericCorpusTests(unittest.TestCase):
    def test_long_code_identifiers_fit_all_layouts_without_altering_source_text(self):
        identifier = "posix_spawn_file_actions_addclosefrom_np()"
        markup = (
            "<body><h1>Long code</h1><ul><li><p>A nested inline identifier: "
            f'<code>{identifier}</code>.</p><ul><li><p><a href="https://example.com/">'
            f"<code>{identifier}</code></a>.</p></li></ul></li></ul>"
            f"<pre><code>{identifier}</code></pre></body>"
        )
        document = Document("https://example.com/code", "Long code", markup)
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "code.epub"
            compose(BookJob("Long code", [document]), destination)
            with ZipFile(destination) as archive:
                chapter = BeautifulSoup(archive.read("OEBPS/chapter-001.xhtml"), "xml")
                self.assertEqual([c.get_text() for c in chapter.find_all("code")], [identifier] * 3)
            with pymupdf.open(destination) as book:
                for width, height, font in [
                    (420, 640, 11),
                    (360, 640, 11),
                    (420, 640, 17),
                    (640, 420, 11),
                ]:
                    book.layout(width=width, height=height, fontsize=font)
                    for page in book:
                        for block in page.get_text("dict")["blocks"]:
                            for line in block.get("lines", []):
                                x0, y0, x1, y1 = line["bbox"]
                                self.assertGreaterEqual(x0, -1)
                                self.assertGreaterEqual(y0, -1)
                                self.assertLessEqual(x1, width + 1)
                                self.assertLessEqual(y1, height + 1)

    def test_standalone_images_reflow_without_changing_inline_images_or_source(self):
        document = read(CORPUS / "html/images.html", asset_root=CORPUS, cache=None)
        asset = next(iter(document.assets))
        document.html = (
            f'<body><p id="standalone"><img src="{asset}" alt="Chart"/></p>'
            f'<p id="inline">An inline <img src="{asset}" alt="Symbol"/> stays in text.</p>'
            f'<div><div><img id="boxed" src="{asset}" alt="Chart"/><div></div></div></div>'
            f'<p><a href="#standalone">Return to chart</a></p></body>'
        )
        original = document.html
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "book.epub"
            compose(BookJob("Images in context", [document]), destination)
            with ZipFile(destination) as archive:
                chapter = BeautifulSoup(archive.read("OEBPS/chapter-001.xhtml"), "xml")
                self.assertEqual(chapter.find(id="standalone").name, "figure")
                self.assertEqual(chapter.find(id="inline").name, "p")
                self.assertEqual(chapter.find(id="boxed").parent.name, "figure")
                self.assertEqual(chapter.find(id="inline").get_text(), "An inline  stays in text.")
                self.assertEqual(archive.read("OEBPS/" + asset), document.assets[asset])
        self.assertEqual(document.html, original)

    def test_verified_redirect_aliases_localize_cross_page_links(self):
        payload = b'<main><h1>Target</h1><p id="topic">Target content.</p></main>'
        with patch(
            "einkrelayer.capture.readers.fetch",
            return_value=Response(payload, "https://example.com/final", "text/html", "utf-8"),
        ):
            target = read("https://example.com/original", cache=None)
        self.assertEqual(target.aliases, ["https://example.com/final"])
        first = read(CORPUS / "html/blog.html", cache=None)
        first.html = (
            '<body><p><a href="https://example.com/final#topic">Read target.</a></p></body>'
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "book.epub"
            compose(BookJob("Linked pages", [first, target]), output)
            with ZipFile(output) as archive:
                self.assertIn(b"chapter-002.xhtml#topic", archive.read("OEBPS/chapter-001.xhtml"))

    def test_media_only_is_held_but_article_with_login_widget_is_readable(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "page.html"
            path.write_text(
                '<main><h1>A video</h1><video src="https://example.com/video.mp4"></video></main>'
            )
            with self.assertRaisesRegex(ValueError, "Media-only"):
                read(path, cache=None)
            path.write_text(
                '<main><h1>A public article</h1><p>The full article remains readable.</p><form><input type="password"/></form><video src="https://example.com/video.mp4"></video></main>'
            )
            doc = read(path, cache=None)
            self.assertIn("The full article remains readable.", doc.html)
            self.assertIn("Original embedded media", doc.html)

    def test_altered_chart_is_rejected(self):
        doc = read(CORPUS / "html/news.html", asset_root=CORPUS, cache=None)
        name = next(iter(doc.assets))
        doc.assets[name] = b"altered chart"
        with (
            tempfile.TemporaryDirectory() as temporary,
            self.assertRaisesRegex(ValueError, "hash mismatch"),
        ):
            compose(BookJob("Chart", [doc]), Path(temporary) / "book.epub")

    def test_explicit_contents_order_query_identity_and_local_cross_links(self):
        data = b'<nav aria-label="Contents"><a href="/chapter?part=2">Two</a><a href="/chapter?part=1">One</a><a href="/chapter?part=2#again">Duplicate</a><a href="https://other.example/">Other</a></nav>'
        self.assertEqual(
            contents_links(data, "https://example.com/book"),
            ["https://example.com/chapter?part=2", "https://example.com/chapter?part=1"],
        )
        with self.assertRaises(ValueError):
            contents_links(data, "https://example.com/book", limit=1)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            toc = root / "toc.html"
            toc.write_text(
                '<nav id="contents"><a href="one.html">One</a><a href="two.html">Two</a></nav>'
            )
            (root / "one.html").write_text(
                '<main><h1>One</h1><p><a href="two.html#target">Read two</a></p></main>'
            )
            (root / "two.html").write_text(
                '<main><h1>Two</h1><p id="target">The second page.</p></main>'
            )
            pages = [read(path, cache=None) for path in discover(str(toc))]
            with redirect_stdout(io.StringIO()):
                compose(BookJob("Ordered pages", pages), root / "book.epub")
            with ZipFile(root / "book.epub") as archive:
                self.assertIn(b"chapter-002.xhtml#target", archive.read("OEBPS/chapter-001.xhtml"))

    def test_wide_table_reflow_and_audit_calibration(self):
        document = read(CORPUS / "html/blog.html", cache=None)
        document.html = "<body><h1>Orders</h1><table><tr><th>ID</th><th>Date</th><th>Product</th><th>Quantity</th><th>Total</th></tr><tr><td>ORDER-1</td><td>2026-10-22</td><td>Printer</td><td>12</td><td>$42</td></tr></table><p>Every value remains associated with its label.</p></body>"
        with tempfile.TemporaryDirectory() as temporary, redirect_stdout(io.StringIO()):
            output = Path(temporary) / "wide.epub"
            compose(BookJob("Orders", [document]), output)
            self.assertEqual(review(output, Path(temporary) / "review"), 0)
            with ZipFile(output) as archive:
                chapter = BeautifulSoup(archive.read("OEBPS/chapter-001.xhtml"), "xml")
            values = chapter.find("dl")
            self.assertEqual(
                [
                    (dt.get_text(), dt.find_next_sibling("dd").get_text())
                    for dt in values.find_all("dt")
                ],
                [
                    ("ID", "ORDER-1"),
                    ("Date", "2026-10-22"),
                    ("Product", "Printer"),
                    ("Quantity", "12"),
                    ("Total", "$42"),
                ],
            )
        case = {"required_text": ["Every value remains associated with its label."]}
        document.html = "<body><p>Content was removed.</p></body>"
        self.assertTrue(check_expectations(case, document))
        document.html = '<body><p>Chart</p><img src="missing.png"/></body>'
        self.assertIn("Missing captured image", check_expectations({}, document))

    def test_broken_internal_link_cannot_be_published(self):
        document = read(CORPUS / "html/blog.html", cache=None)
        document.html = '<body><p><a href="#missing">Missing anchor</a></p></body>'
        with (
            tempfile.TemporaryDirectory() as temporary,
            self.assertRaisesRegex(ValueError, "Missing anchor"),
        ):
            compose(BookJob("Broken", [document]), Path(temporary) / "book.epub")

    def test_offline_html_corpus_extraction_composition_and_four_layouts(self):
        manifest = json.loads((CORPUS / "manifest.json").read_text())
        self.assertEqual(sum(case["outcome"] == "success" for case in manifest), 20)
        self.assertEqual(sum(case["outcome"] == "held" for case in manifest), 8)
        for case in manifest:
            with self.subTest(sample=case["id"]):
                if case["outcome"] == "held":
                    with self.assertRaises(ValueError):
                        read(CORPUS / case["file"], asset_root=CORPUS, cache=None)
                    continue
                document = read(CORPUS / case["file"], asset_root=CORPUS, cache=None)
                check_expected(self, case, document)
                with tempfile.TemporaryDirectory() as temporary, redirect_stdout(io.StringIO()):
                    destination = Path(temporary) / "book.epub"
                    compose(BookJob(document.title, [document]), destination)
                    self.assertEqual(review(destination, Path(temporary) / "review"), 0)
                    with ZipFile(destination) as archive:
                        self.assertIn(b"<p>1 article</p>", archive.read("OEBPS/titlepage.xhtml"))
                        chapter = archive.read("OEBPS/chapter-001.xhtml").decode()
                        self.assertNotIn("javascript:", chapter)

    def test_document_corpus(self):
        with tempfile.TemporaryDirectory() as temporary:
            cases = create_samples(Path(temporary) / "inputs")
            for case in cases:
                with self.subTest(sample=Path(case["file"]).name):
                    if case["outcome"] == "held":
                        with self.assertRaises(ValueError):
                            read(case["file"], cache=None)
                        continue
                    document = read(case["file"], cache=None)
                    check_expected(self, case, document)
                    self.assertNotIn("[1]", document.title)
                    with tempfile.TemporaryDirectory() as output, redirect_stdout(io.StringIO()):
                        destination = Path(output) / "book.epub"
                        compose(BookJob(document.title, [document]), destination)
                        self.assertEqual(review(destination, Path(output) / "review"), 0)

    def test_conservative_identity_and_mixed_book_digest(self):
        a = read(CORPUS / "html/blog.html", cache=None)
        b = read(CORPUS / "html/short.html", cache=None)
        a.source = "https://example.com:443/story?version=1#top"
        b.source = "https://example.com/story?version=2"
        self.assertNotEqual(identity(a.source), identity(b.source))
        for mode in ["book", "digest"]:
            with (
                self.subTest(mode=mode),
                tempfile.TemporaryDirectory() as temporary,
                redirect_stdout(io.StringIO()),
            ):
                job = BookJob("Mixed reading", [a, a, b], mode=mode, edition_date="2026-10-22")
                result = compose(job, Path(temporary) / "mixed.epub")
                self.assertEqual(len(result["documents"]), 2)
                self.assertEqual(review(Path(result["epub"]), Path(temporary) / "review"), 0)

    def test_heading_markers_stay_out_of_titles(self):
        markup = (
            '<html><body><article><h1>Guide<a href="#guide" id="guide">\u00b6</a>'
            '<sup><a href="#note-1">[1]</a></sup></h1>'
            "<p>Substantive guide text that remains readable for this sample.</p>"
            '<h2>Steps<a class="headerlink" href="#steps">\u00b6</a></h2>'
            '<p>Follow each step in order.</p><p id="note-1">A note.</p>'
            "</article></body></html>"
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "guide.html"
            path.write_text(markup)
            document = read(path, cache=None)
        self.assertEqual(document.title, "Guide")
        self.assertNotIn("\u00b6", document.html)
        self.assertIn('id="guide"', document.html)
        self.assertIn("[1]", document.html)
        intro = "<p>" + "An introduction explains the whole page. " * 8 + "</p>"
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "uses.html"
            path.write_text(
                f"<html><head><title>Appropriate uses</title></head><body><main>{intro}"
                "<h1>1. First situation</h1><p>Details of the first situation.</p>"
                "</main></body></html>"
            )
            self.assertEqual(read(path, cache=None).title, "Appropriate uses")

    def test_answer_keys_move_to_linked_chapter(self):
        document = read(CORPUS / "html/blog.html", cache=None)
        document.html = (
            "<body><h1>Quiz</h1><h2>Passage</h2><p>Read the passage first.</p>"
            '<h2>Questions</h2><p id="q1">1. What grows? <a href="#a1">Answer</a></p>'
            '<h2>Answer key</h2><p id="a1">1. Seeds grow. <a href="#q1">Back</a></p></body>'
        )
        with tempfile.TemporaryDirectory() as temporary, redirect_stdout(io.StringIO()):
            output = Path(temporary) / "quiz.epub"
            result = compose(BookJob("Quiz", [document], answer_keys=True), output)
            self.assertEqual(result["answer_keys"], 1)
            self.assertEqual(review(output, Path(temporary) / "review"), 0)
            with ZipFile(output) as archive:
                chapter = archive.read("OEBPS/chapter-001.xhtml").decode()
                answers = archive.read("OEBPS/chapter-001-answers.xhtml").decode()
                nav = archive.read("OEBPS/nav.xhtml").decode()
            self.assertIn('href="chapter-001-answers.xhtml#a1"', chapter)
            self.assertNotIn("Seeds grow", chapter)
            self.assertIn('href="chapter-001.xhtml#q1"', answers)
            self.assertIn("chapter-001-answers.xhtml", nav)
            self.assertIn('href="chapter-001.xhtml#section-1"', nav)

    def test_pdf_structure_comes_from_document_wide_evidence(self):
        book = pymupdf.open()
        pages = [
            [(72, 80, "Contents", 16, True)]
            + [
                (72, 120 + 20 * i, f"Section {i} " + "." * 40 + f" {i}", 12, False)
                for i in (1, 2, 3)
            ],
            [(72, 80, "This page intentionally left blank.", 11, False)],
            [
                (72, 80, "Getting Started", 16, True),
                (72, 120, "Setup steps", 12, True),
                (72, 160, "\u2022", 12, False),
                (90, 160, "Clear the bench", 12, False),
                (72, 180, "\u2022", 12, False),
                (90, 180, "Check the tools", 12, False),
                (72, 220, "Then prepare the workspace before you", 12, False),
            ],
            [(72, 80, "begin the first task.", 12, False)],
        ]
        for number, lines in enumerate(pages, 1):
            page = book.new_page(width=612, height=792)
            for x, y, text, size, bold in lines:
                page.insert_text((x, y), text, fontsize=size, fontname="hebo" if bold else "helv")
            page.insert_text((72, 770), f"Field Guide | {number}", fontsize=10, fontname="helv")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "guide.pdf"
            book.save(path)
            document = read(path, cache=None)
        soup = BeautifulSoup(document.html, "html.parser")
        text = normalized(soup.get_text(" "))
        self.assertEqual([h.get_text() for h in soup.find_all("h2")], ["Getting Started"])
        self.assertEqual([h.get_text() for h in soup.find_all("h3")], ["Setup steps"])
        self.assertEqual(
            [li.get_text() for li in soup.find_all("li")], ["Clear the bench", "Check the tools"]
        )
        self.assertIn("Then prepare the workspace before you begin the first task.", text)
        for absent in ["Field Guide", "intentionally left blank", "Section 1", "Page 1"]:
            self.assertNotIn(absent, text)
        self.assertEqual(len(soup.find_all("p")), 1)

    def test_pdf_table_layout_artifacts_are_removed(self):
        from einkrelayer.capture.documents import _clean_table

        rows = [
            ["", "Term", "", "", "Meaning", ""],
            ["", "AC", "", "", "Advisory Circular", ""],
            ["14 CFR part 48", None, None, "", "Registration and Marking", ""],
            [None, None, None, None, "Requirements", None],
        ]
        self.assertEqual(
            _clean_table(rows),
            [
                ["Term", "Meaning"],
                ["AC", "Advisory Circular"],
                ["14 CFR part 48", "Registration and Marking Requirements"],
            ],
        )

    def test_local_asset_reads_are_confined(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "selected").mkdir()
            (root / "outside.svg").write_bytes((CORPUS / "assets/chart.svg").read_bytes())
            source = root / "selected/page.html"
            source.write_text(
                '<main><p>Required illustration follows.</p><img src="../outside.svg"/></main>'
            )
            with self.assertRaisesRegex(ValueError, "asset"):
                read(source, cache=None)
