"""Clutter around an article goes; everything that belongs to the article stays."""

import io
import unittest

from bs4 import BeautifulSoup
from PIL import Image

from einkrelayer.capture.html import extract

SOURCE = "https://news.example.com/2026/10/harbour-plan"
PARAGRAPH = "The committee met for three hours and agreed on a revised plan for the harbour. " * 3


def picture(href, counter=[0]):
    counter[0] += 1
    buffer = io.BytesIO()
    Image.new("RGB", (40 + counter[0], 30), "white").save(buffer, "PNG")
    return buffer.getvalue()


def read(body):
    html = f"<html><head><title>Harbour plan</title></head><body><article>{body}</article></body></html>"
    document = extract(html.encode(), SOURCE, picture)
    return document, BeautifulSoup(document.html, "lxml").get_text(" ", strip=True)


ARTICLE = "".join(f"<p>{PARAGRAPH}</p>" for _ in range(6))
RELATED = (
    '<section class="related-stories"><h2>Related</h2><ul>'
    + "".join(
        f'<li><a href="/story/{n}"><img src="/t{n}.jpg" alt="">Other story {n}</a></li>'
        for n in range(4)
    )
    + "</ul></section>"
)
MOST_READ = (
    "<h2>Most read</h2><div>"
    + "".join(f'<a href="/popular/{n}">Popular headline number {n}</a>' for n in range(5))
    + "</div>"
)


class ClutterTests(unittest.TestCase):
    def test_related_lists_share_bars_and_sign_ups_are_removed(self):
        body = (
            '<h1>Harbour plan</h1><div class="share-tools"><a href="#">Facebook</a><a href="#">X</a></div>'
            f"{ARTICLE}<p>Share this article</p>"
            '<div class="newsletter-signup">Sign up for our daily newsletter</div>'
            f"{RELATED}{MOST_READ}"
        )
        document, text = read(body)
        self.assertIn("revised plan", text)
        for clutter in [
            "Facebook",
            "Share this",
            "newsletter",
            "Other story",
            "Popular headline",
            "Most read",
        ]:
            self.assertNotIn(clutter, text)
        self.assertEqual(document.html.count("<img"), 0)  # thumbnails went with their list

    def test_byline_date_figures_tables_and_notes_are_kept(self):
        body = (
            '<header><h1>Harbour plan</h1><div class="meta share-bar"><span class="byline">By A. Writer</span>'
            '<time datetime="2026-10-23">23 October 2026</time><a href="#">Share</a></div></header>'
            f'{ARTICLE}<figure><img src="/harbour.jpg" alt="The harbour"><figcaption>The harbour at dawn.'
            "</figcaption></figure><table><tr><th>Year</th><th>Ships</th></tr><tr><td>2026</td><td>40</td>"
            '</tr></table><p>Ships arrive daily.<sup><a href="#n1">1</a></sup></p>'
            '<h2>References</h2><ol><li><a href="https://doi.org/10.1/a">Smith 2020</a></li>'
            '<li><a href="https://doi.org/10.1/b">Jones 2021</a></li><li><a href="https://doi.org/10.1/c">'
            'Lee 2022</a></li></ol><ol><li id="n1">Port records. <a href="#r1">↩</a></li></ol>'
        )
        document, text = read(body)
        for kept in [
            "By A. Writer",
            "23 October 2026",
            "The harbour at dawn",
            "Ships",
            "Smith 2020",
            "Port records",
        ]:
            self.assertIn(kept, text)
        self.assertNotIn("Share", text)
        self.assertEqual(document.html.count("<img"), 1)

    def test_unlabelled_related_cards_side_boxes_and_promo_figures_are_removed(self):
        cards = "".join(
            f'<li><a href="https://news.example.com/2026/10/other-{n}"><img src="/o{n}.jpg" alt="Other">'
            f"Another headline {n}</a></li>"
            for n in range(2)
        )
        body = (
            f"<h1>Harbour plan</h1><p>{SOURCE}</p>{ARTICLE}"
            '<figure><a href="/newsletters">Sign up for the Morning Briefing email</a></figure>'
            f"<section><h2>Related Content</h2><section><h3>Editors’ Picks</h3><ul>{cards}</ul></section></section>"
            '<aside><h2>Subjects</h2><ul><li><a href="/subjects/ports">Ports</a></li>'
            '<li><a href="/subjects/shipping">Shipping</a></li></ul></aside>'
            "<aside><p>“A harbour is a promise,” the mayor said.</p></aside>"
        )
        document, text = read(body)
        for clutter in [SOURCE, "Sign up", "Related", "Another headline", "Subjects", "Ports"]:
            self.assertNotIn(clutter, text)
        self.assertIn("A harbour is a promise", text)  # a pull quote stays
        self.assertEqual(document.html.count("<img"), 0)

    def test_link_roundups_to_other_sites_and_labelled_further_reading_are_kept(self):
        roundup = "".join(
            f'<li><a href="https://elsewhere{n}.example/post">A post worth reading {n}</a></li>'
            for n in range(4)
        )
        reading = "".join(
            f'<li><a href="/2025/0{n}/older-story">Earlier story {n}</a>, by A. Writer</li>'
            for n in range(1, 4)
        )
        body = f"<h1>Harbour plan</h1>{ARTICLE}<h2>Elsewhere</h2><ul>{roundup}</ul><p>Further reading:</p><ul>{reading}</ul>"
        _, text = read(body)
        self.assertIn("A post worth reading 3", text)
        self.assertIn("Earlier story 3", text)

    def test_icons_and_repeated_images_are_dropped(self):
        body = (
            '<h1>Harbour plan</h1><p><svg width="16" height="16"><path d="M0 0h16v16z"/></svg>Photo</p>'
            '<img src="/lead.jpg" alt="Lead"><img src="/lead.jpg" alt="Lead">'
            f"{ARTICLE}"
        )
        document, _ = read(body)
        self.assertEqual(document.html.count("<img"), 1)
        self.assertIn("Icon omitted", document.warnings)


if __name__ == "__main__":
    unittest.main()
