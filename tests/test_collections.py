"""Contents selection stays within explicit sections and never crawls grandchildren."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

from einkrelayer.capture.collections import book_sources, contents_links, discover
from einkrelayer.capture.http import Response

BASE = "https://example.org/book/index"


class ContentsSelectionTests(unittest.TestCase):
    def links(self, markup, **kwargs):
        return contents_links(markup.encode(), BASE, **kwargs)

    def test_heading_lists_include_subsections_and_stop_at_next_section(self):
        markup = (
            "<h2>Table of contents</h2><p>Choose a chapter.</p>"
            '<h3>Part One</h3><ul><li><a href="one.php">One</a></li></ul>'
            '<h3>Part Two</h3><ol><li><a href="two.aspx">Two</a></li></ol>'
            '<h2>Related reading</h2><ul><li><a href="other">Other</a></li></ul>'
        )
        self.assertEqual(
            self.links(markup),
            ["https://example.org/book/one.php", "https://example.org/book/two.aspx"],
        )

    def test_separate_and_nested_marked_sections_preserve_dom_order_once(self):
        self.assertEqual(
            self.links(
                '<div class="toc"><a href="one">One</a>'
                '<nav role="doc-toc"><a href="two">Two</a></nav></div>'
                '<div id="contents"><a href="three">Three</a><a href="one#again">One</a></div>'
            ),
            [
                "https://example.org/book/one",
                "https://example.org/book/two",
                "https://example.org/book/three",
            ],
        )

    def test_utility_links_are_excluded_without_excluding_scoped_chapter(self):
        self.assertEqual(
            self.links(
                '<nav id="toc"><a href="one">One</a>'
                '<a href="/account">Account</a><a href="/search?q=book">Search</a>'
                '<a href="/auth">Sign in</a><a href="/privacy">Privacy policy</a>'
                '<a href="/">Home</a><a href="account">Account models</a></nav>'
            ),
            ["https://example.org/book/one", "https://example.org/book/account"],
        )

    def test_dynamic_remote_chapter_extensions_are_supported(self):
        for suffix in ("php", "asp", "aspx", "jsp", "cgi"):
            with self.subTest(suffix=suffix):
                self.assertEqual(
                    self.links(f'<nav id="toc"><a href="chapter.{suffix}">Chapter</a></nav>'),
                    [f"https://example.org/book/chapter.{suffix}"],
                )

    def test_unsupported_chapter_is_reported_instead_of_silently_omitted(self):
        with self.assertRaisesRegex(ValueError, "unsupported chapter"):
            self.links('<nav id="toc"><a href="one">One</a><a href="two.epub">Two</a></nav>')

    def test_other_hosts_protocols_and_unrelated_links_are_not_selected(self):
        self.assertEqual(
            self.links(
                '<a href="outside">Outside</a><nav id="toc"><a href="one">One</a>'
                '<a href="http://example.org/book/two">Two</a>'
                '<a href="https://other.example/three">Three</a></nav>'
            ),
            ["https://example.org/book/one"],
        )

    def test_plain_article_lists_are_not_contents(self):
        self.assertIsNone(
            self.links('<h2>Sources</h2><ul><li><a href="one">One</a></li></ul>', optional=True)
        )
        with self.assertRaisesRegex(ValueError, "No explicit contents"):
            self.links('<ul><li><a href="one">One</a></li></ul>')

    def test_substantial_chapter_with_book_sidebar_stays_a_single_article(self):
        markup = (
            '<nav class="toc"><a href="one">One</a><a href="two">Two</a></nav><main><h1>Chapter</h1><p>'
            + "Reading content. " * 110
            + "</p></main>"
        )
        self.assertIsNone(self.links(markup, optional=True))
        # Explicit CLI contents selection remains an instruction to follow the list.
        self.assertEqual(len(self.links(markup)), 2)

    def test_empty_heading_reports_missing_contents(self):
        with self.assertRaisesRegex(ValueError, "no adjacent chapter list"):
            self.links("<h2>Contents</h2><p>Load with JavaScript</p>", optional=True)

    def test_in_page_section_links_stay_an_article(self):
        self.assertIsNone(
            self.links('<h2>Contents</h2><ul><li><a href="#one">One</a></li></ul>', optional=True)
        )

    def test_later_empty_contents_section_is_not_silently_omitted(self):
        with self.assertRaisesRegex(ValueError, "no adjacent chapter list"):
            self.links(
                '<div class="toc"><a href="one">One</a></div><h2>Chapters</h2><p>Unavailable</p>'
            )

    def test_interrupted_heading_lists_require_explicit_selection(self):
        with self.assertRaisesRegex(ValueError, "interrupted"):
            self.links(
                '<h2>Contents</h2><ul><li><a href="one">One</a></li></ul><p>Unclear boundary</p><ul><li><a href="two">Two</a></li></ul>'
            )

    def test_query_based_chapters_at_site_root_are_not_home_links(self):
        self.assertEqual(
            self.links(
                '<nav id="toc"><a href="/?chapter=1">One</a><a href="/?chapter=2">Two</a></nav>'
            ),
            ["https://example.org/?chapter=1", "https://example.org/?chapter=2"],
        )

    def test_heading_before_marked_navigation_with_its_own_heading(self):
        self.assertEqual(
            self.links(
                '<h1>Contents</h1><nav id="toc"><h2>Contents</h2><a href="one">One</a></nav>'
            ),
            ["https://example.org/book/one"],
        )

    def test_multiple_sections_share_a_single_limit(self):
        with self.assertRaisesRegex(ValueError, "chapter limit"):
            self.links(
                '<div class="toc"><a href="one">One</a></div><div class="toc"><a href="two">Two</a></div>',
                limit=1,
            )

    def test_local_directory_escape_stays_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            base = (Path(directory) / "index.html").as_uri()
            with self.assertRaisesRegex(ValueError, "escapes"):
                contents_links(b'<nav id="toc"><a href="../secret.html">Chapter</a></nav>', base)


class DiscoveryDepthTests(unittest.TestCase):
    def response(self, url, markup):
        return Response(markup.encode(), url, "text/html", "utf-8")

    def test_nested_index_fails_without_fetching_grandchildren(self):
        pages = {
            BASE: '<nav id="toc"><a href="part">Part one</a></nav>',
            "https://example.org/book/part": '<h1>Chapters</h1><ul><li><a href="grandchild">Chapter one</a></li></ul>',
        }
        with patch(
            "einkrelayer.capture.collections.fetch",
            side_effect=lambda url, **_: self.response(url, pages[url]),
        ) as fetch:
            with self.assertRaisesRegex(ValueError, "nested chapter index"):
                book_sources(BASE)
            self.assertEqual([call.args[0] for call in fetch.call_args_list], list(pages))

    def test_selected_chapter_redirect_outside_origin_is_refused(self):
        root = self.response(BASE, '<nav id="toc"><a href="one">One</a></nav>')
        redirected = self.response("https://other.example/unrelated", "<article>Other</article>")
        with patch("einkrelayer.capture.collections.fetch", side_effect=[root, redirected]):
            with self.assertRaisesRegex(ValueError, "redirects outside"):
                book_sources(BASE)

    def test_chapter_sidebar_is_not_mistaken_for_nested_index(self):
        root = self.response(BASE, '<nav id="toc"><a href="one">One</a></nav>')
        chapter = self.response(
            "https://example.org/book/one",
            '<nav id="toc"><a href="two">Two</a></nav><article><h1>One</h1><p>'
            + "Reading content. " * 110
            + "</p></article>",
        )
        with patch("einkrelayer.capture.collections.fetch", side_effect=[root, chapter]) as fetch:
            self.assertEqual(book_sources(BASE), ["https://example.org/book/one"])
            self.assertEqual(fetch.call_count, 2)

    def test_unreadable_chapter_cannot_silently_turn_into_a_partial_selection(self):
        root = self.response(BASE, '<nav id="toc"><a href="one">One</a></nav>')
        with patch(
            "einkrelayer.capture.collections.fetch", side_effect=[root, URLError("offline")]
        ):
            with self.assertRaises(URLError):
                book_sources(BASE)

    def test_checked_chapters_are_reused_across_inputs(self):
        root = '<nav id="toc"><a href="one">One</a></nav>'
        checked = {}
        with patch(
            "einkrelayer.capture.collections.fetch",
            side_effect=lambda url, **_: self.response(
                url, root if url == BASE else '<nav id="toc"><a href="#section">Section</a></nav>'
            ),
        ) as fetch:
            for _ in range(2):
                self.assertEqual(
                    book_sources(BASE, checked=checked),
                    ["https://example.org/book/one"],
                )
            self.assertEqual(len(fetch.call_args_list), 3)
            self.assertTrue(all("cookies" not in call.kwargs for call in fetch.call_args_list))

    def test_cli_also_rejects_nested_local_indexes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "index.html").write_text('<nav id="toc"><a href="part.html">Part</a></nav>')
            (root / "part.html").write_text(
                '<nav id="toc"><a href="chapter.html">Chapter</a></nav>'
            )
            with self.assertRaisesRegex(ValueError, "nested chapter index"):
                discover(str(root / "index.html"))

    def test_cli_discovery_uses_anonymous_requests(self):
        root = self.response(BASE, '<nav id="toc"><a href="one">One</a></nav>')
        chapter = self.response("https://example.org/book/one", "<article>Reading</article>")
        with patch("einkrelayer.capture.collections.fetch", side_effect=[root, chapter]) as fetch:
            self.assertEqual(discover(BASE), ["https://example.org/book/one"])
            self.assertTrue(all("cookies" not in call.kwargs for call in fetch.call_args_list))


if __name__ == "__main__":
    unittest.main()
