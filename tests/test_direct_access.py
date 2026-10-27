"""Direct capture, access restrictions, and whole-book failure behavior."""

import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request

from einkrelayer.capture import collections, http, readers
from einkrelayer.capture.access import AccessRestricted
from einkrelayer.capture.html import extract
from einkrelayer.capture.network import _Redirects
from einkrelayer.relay.run import classify

SOURCE = "https://example.org/story"
TEXT = "Ordinary readable article text with a useful explanation. " * 40


class DirectAccessTests(unittest.TestCase):
    def capture(self, body, head=""):
        return extract(
            f"<html><head><title>Story</title>{head}</head><body>{body}</body></html>".encode(),
            SOURCE,
            lambda _: self.fail("Unexpected asset request"),
        )

    def test_public_article_with_newsletter_and_signin_link(self):
        document = self.capture(
            f'<nav><a href="/signin">Sign in</a></nav><article><h1>Story</h1><p>{TEXT}</p></article><aside>Subscribe to our newsletter</aside>'
        )
        self.assertIn("Ordinary readable", document.html)

    def test_newsletter_dialog_and_captcha_example_do_not_block_public_article(self):
        document = self.capture(
            f'<article><h1>Story</h1><p>{TEXT}</p><code id="captcha-example">captcha()</code></article>'
            '<div role="dialog">Subscribe to our newsletter</div>'
        )
        self.assertIn("Ordinary readable", document.html)

    def test_paid_metadata_rejects_even_a_complete_delivered_body(self):
        for metadata in (
            {
                "@type": "NewsArticle",
                "url": SOURCE,
                "isAccessibleForFree": False,
                "articleBody": TEXT,
            },
            {"@type": "Article", "url": SOURCE, "hasPart": {"isAccessibleForFree": "False"}},
        ):
            with self.subTest(metadata=metadata), self.assertRaises(AccessRestricted):
                self.capture(
                    f"<article><p>{TEXT}</p></article>",
                    '<script type="application/ld+json">' + json.dumps(metadata) + "</script>",
                )

    def test_unrelated_paid_recommendation_does_not_block_public_article(self):
        metadata = {
            "@type": "Article",
            "url": "https://example.org/other",
            "headline": "Other",
            "isAccessibleForFree": False,
        }
        document = self.capture(
            f"<article><p>{TEXT}</p></article>",
            '<script type="application/ld+json">' + json.dumps(metadata) + "</script>",
        )
        self.assertIn("Ordinary readable", document.html)

    def test_gate_overlays_are_rejected_not_removed(self):
        for gate in (
            "<p>Subscribe to continue reading</p>",
            '<div class="paywall">Unlock this article with a subscription</div>',
        ):
            with self.subTest(gate=gate), self.assertRaises(AccessRestricted):
                self.capture(f"<article><p>{TEXT}</p></article>{gate}")

    def test_hidden_markup_and_script_bodies_are_never_reconstructed(self):
        body = f'<article><p>{TEXT}</p><div hidden>HIDDEN ONE</div><p aria-hidden="true">HIDDEN TWO</p><p style="display: none !important">HIDDEN THREE</p><div class="concealed">HIDDEN FOUR</div></article>'
        document = self.capture(body, "<style>.concealed { visibility: hidden }</style>")
        self.assertNotIn("HIDDEN", document.html)
        metadata = {"@type": "Article", "url": SOURCE, "articleBody": TEXT}
        with self.assertRaisesRegex(ValueError, "No substantive"):
            self.capture(
                "<article><h1>Story</h1></article>",
                '<script type="application/ld+json">' + json.dumps(metadata) + "</script>",
            )

    def test_blocked_http_stops_after_one_request_and_is_terminal(self):
        for status in (401, 403, 407, 451):
            with (
                self.subTest(status=status),
                patch.object(
                    http, "urlopen", side_effect=HTTPError(SOURCE, status, "no", {}, None)
                ) as opener,
                self.assertRaises(AccessRestricted) as error,
            ):
                readers.read(SOURCE, cache=None)
            self.assertEqual(opener.call_count, 1)
            self.assertFalse(classify(error.exception)[2])

    def test_login_redirect_stops_before_requesting_login_page(self):
        with self.assertRaises(AccessRestricted):
            _Redirects().redirect_request(
                Request(SOURCE), None, 302, "Found", {}, "https://example.org/login?next=/story"
            )

    def test_restricted_contents_page_never_expands_its_links(self):
        body = '<nav id="toc"><a href="chapter">Chapter</a></nav><p>Subscribe to read</p>'
        response = http.Response(body.encode(), SOURCE, "text/html", None)
        with patch.object(collections, "fetch", return_value=response) as fetch:
            with self.assertRaises(AccessRestricted):
                collections.book_sources(SOURCE)
            self.assertEqual(fetch.call_count, 1)

    def test_restricted_chapter_fails_entire_selection(self):
        contents = http.Response(
            b'<nav id="toc"><a href="chapter">Chapter</a></nav>', SOURCE, "text/html", None
        )
        chapter = http.Response(b"<p>Sign in to read</p>", SOURCE + "/chapter", "text/html", None)
        with patch.object(collections, "fetch", side_effect=[contents, chapter]):
            with self.assertRaises(AccessRestricted):
                collections.book_sources(SOURCE)

    def test_anonymous_network_api_has_no_cookie_argument(self):
        import inspect

        from einkrelayer.capture import network

        self.assertNotIn("cookies", inspect.signature(network.urlopen).parameters)
        self.assertNotIn("cookies", inspect.signature(readers.read).parameters)
