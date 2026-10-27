"""Email books and commands use real queue state with local mail/capture/delivery fakes."""

import json
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

import test_relay as fixtures

from einkrelayer.capture import collections
from einkrelayer.capture.collections import book_sources as discover_book_sources
from einkrelayer.capture.http import Response
from einkrelayer.relay import config as relay_config
from einkrelayer.relay import mail, run, store, tools


class EmailCommandTests(unittest.TestCase):
    read = fixtures.RunTests.read
    transmit = fixtures.RunTests.transmit
    cycle = fixtures.RunTests.cycle
    submit = fixtures.RunTests.submit

    def setUp(self):
        fixtures.RunTests.setUp(self)
        discovery = patch.object(
            collections, "book_sources", side_effect=lambda source, **_: [source]
        )
        self.discovery = discovery.start()
        self.addCleanup(discovery.stop)
        composer = patch.object(run, "compose", wraps=run.compose)
        self.composer = composer.start()
        self.addCleanup(composer.stop)

    def command(self, subject, body="", **kwargs):
        return self.signer.message("ana@example.org", body, subject=subject, **kwargs)

    def requests(self):
        with store.Store(self.state) as database:
            return database.request_history()

    def test_restricted_item_is_terminal_without_blocking_other_mail(self):
        from einkrelayer.capture.access import AccessRestricted

        def direct(source, **kwargs):
            if str(source).endswith("restricted"):
                raise AccessRestricted("HTTP 403")
            return fixtures.RunTests.read(self, source, **kwargs)

        with patch("einkrelayer.capture.readers.read", side_effect=direct):
            self.cycle(
                fixtures.FakeMailbox(
                    [
                        self.command(
                            "book: Blocked book",
                            "https://example.com/a\nhttps://example.com/restricted",
                        ),
                        self.command("book: Good book", "https://example.com/good"),
                        self.submit("https://example.com/restricted\nhttps://example.com/digest"),
                        self.command("send now"),
                    ]
                )
            )
        self.assertEqual(len(self.sent), 2)
        books = {r["title"]: r for r in self.requests() if r["kind"] == "book"}
        self.assertEqual(books["Blocked book"]["status"], "failed")
        self.assertIsNone(books["Blocked book"]["next_try"])
        self.assertEqual(books["Good book"]["status"], "sent")
        self.assertNotIn(
            "Blocked book", [call.args[0].title for call in self.composer.call_args_list]
        )

    def test_subject_commands_are_explicit(self):
        for subject, expected in [
            (" BOOK ", ("book", "")),
            ("Book: My Readings", ("book", "My Readings")),
            (" SEND NOW ", ("send_now", "")),
            ("Re: book", ("digest", "")),
            ("Please send now", ("digest", "")),
        ]:
            message = mail.parse(self.command(subject))
            self.assertEqual(mail.command(message), expected)

    def test_one_book_per_email_with_order_title_and_no_digest_items(self):
        box = fixtures.FakeMailbox(
            [
                self.command("book: Reading list", "https://example.com/a\nhttps://example.com/b"),
                self.command("book: Another book", "https://example.com/a"),
            ]
        )
        result, _ = self.cycle(box)
        self.assertEqual(result, {"ana": "sent"})
        self.assertEqual(len(self.sent), 2)
        jobs = [call.args[0] for call in self.composer.call_args_list]
        self.assertEqual([job.mode for job in jobs], ["book", "book"])
        self.assertEqual(jobs[0].title, "Reading list")
        self.assertEqual(
            [d.source for d in jobs[0].documents],
            ["https://example.com/a", "https://example.com/b"],
        )
        self.assertEqual([r["status"] for r in self.requests()], ["sent", "sent"])
        with store.Store(self.state) as database:
            self.assertEqual(database.queue(), [])
            self.assertEqual([d["articles"] for d in database.deliveries()], [1, 2])
        self.cycle(box)
        self.assertEqual(len(self.sent), 2)

    def test_contents_are_expanded_once_in_order_and_deduplicated(self):
        self.discovery.side_effect = lambda source, **_: (
            ["https://example.com/ch1", "https://example.com/ch2"]
            if source.endswith("contents")
            else [source]
        )
        self.cycle(
            fixtures.FakeMailbox(
                [
                    self.command(
                        "book",
                        "https://example.com/first\nhttps://example.com/contents\nhttps://example.com/ch1",
                    )
                ]
            )
        )
        job = self.composer.call_args.args[0]
        self.assertEqual(job.title, "Collected readings")
        self.assertEqual(
            [d.source for d in job.documents],
            [
                "https://example.com/first",
                "https://example.com/ch1",
                "https://example.com/ch2",
            ],
        )

    def test_real_discovery_builds_heading_based_chapters_without_utility_links(self):
        self.discovery.side_effect = discover_book_sources
        pages = {
            "https://example.com/index": '<h1>Contents</h1><ul><li><a href="one.php">One</a></li><li><a href="/account">Account</a></li></ul><div class="toc"><a href="two">Two</a></div>',
            "https://example.com/one.php": "<article><h1>One</h1><p>Chapter one.</p></article>",
            "https://example.com/two": "<article><h1>Two</h1><p>Chapter two.</p></article>",
        }
        with patch.object(
            collections,
            "fetch",
            side_effect=lambda source, **_: Response(
                pages[source].encode(), source, "text/html", "utf-8"
            ),
        ):
            self.cycle(
                fixtures.FakeMailbox(
                    [self.command("book: Complete book", "https://example.com/index")]
                )
            )
        self.assertEqual(self.fetched, ["https://example.com/one.php", "https://example.com/two"])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.requests()[0]["status"], "sent")

    def test_real_discovery_rejects_nested_index_before_building_or_sending(self):
        self.discovery.side_effect = discover_book_sources
        pages = {
            "https://example.com/index": '<nav id="toc"><a href="part">Part one</a></nav>',
            "https://example.com/part": '<nav id="toc"><a href="grandchild">Chapter one</a></nav>',
        }
        with patch.object(
            collections,
            "fetch",
            side_effect=lambda source, **_: Response(
                pages[source].encode(), source, "text/html", "utf-8"
            ),
        ) as fetch:
            self.cycle(fixtures.FakeMailbox([self.command("book", "https://example.com/index")]))
        self.assertEqual(len(fetch.call_args_list), 2)
        self.assertEqual(self.sent, [])
        self.composer.assert_not_called()
        request = self.requests()[0]
        self.assertEqual(request["status"], "failed")
        self.assertIn("nested chapter index", request["reason"])

    def test_attachments_and_urls_share_one_book_and_files_are_cleaned(self):
        box = fixtures.FakeMailbox(
            [
                self.command(
                    "book",
                    "https://example.com/a",
                    files=[("notes.txt", b"Attached reading", "text/plain")],
                )
            ]
        )
        self.cycle(box)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(self.composer.call_args.args[0].documents), 2)
        self.assertEqual(len(box.deleted), 1)
        self.assertEqual(list((self.state / "attachments").iterdir()), [])

    def test_automatic_single_document_title(self):
        self.cycle(fixtures.FakeMailbox([self.command("book", "https://example.com/a")]))
        job = self.composer.call_args.args[0]
        self.assertEqual(job.title, job.documents[0].title)
        self.assertEqual(self.sent[0][1]["Subject"], job.title)

    def test_send_now_coalesces_and_includes_other_intake_but_only_its_flow(self):
        config = fixtures.settings_for(self.state, minimum=50, days=["never"])
        config.readers["mateo"] = relay_config.Reader(
            "mateo", ["mateo@example.org"], ["mateo@kindle.com"], "Mateo", 50
        )
        box = fixtures.FakeMailbox(
            [
                self.command("send now"),
                self.command("SEND NOW"),
                self.submit("https://example.com/a"),
                self.submit("https://example.com/b", sender="mateo@example.org"),
            ]
        )
        result, _ = self.cycle(box, config)
        self.assertEqual(result, {"ana": "sent", "mateo": "waiting"})
        self.assertEqual([recipients for recipients, _ in self.sent], [["ana@kindle.com"]])
        self.assertEqual([r["status"] for r in self.requests()], ["sent", "sent"])

    def test_empty_send_now_does_not_force_future_submissions(self):
        self.cycle(fixtures.FakeMailbox([self.command("send now")]))
        self.assertEqual(self.requests()[0]["status"], "empty")
        self.cycle(fixtures.FakeMailbox([self.submit("https://example.com/a")]))
        self.assertEqual(self.sent, [])
        self.assertTrue(
            any("empty" in line for line in tools.history(fixtures.settings_for(self.state)))
        )

    def test_send_now_respects_retry_dates(self):
        with store.Store(self.state) as database:
            database.record_message("old", "ana", "Processed/ana", ["https://example.com/a"])
            database.record_failure(database.queue()[0]["id"], "Network", date.today())
        self.cycle(fixtures.FakeMailbox([self.command("send now")]))
        self.assertEqual(self.sent, [])
        self.assertEqual(self.fetched, [])
        self.assertEqual(self.requests()[0]["status"], "empty")

    def test_invalid_and_unsigned_commands_are_rejected(self):
        messages = [
            self.command("book"),
            self.command("book:"),
            self.command("send now", "https://example.com/a"),
            self.command("send now", files=[("notes.txt", b"notes", "text/plain")]),
            self.command("book", "https://example.com/a", domain=b"other.example"),
            self.signer.message("stranger@example.org", "", subject="send now"),
        ]
        box = fixtures.FakeMailbox(messages)
        self.cycle(box)
        self.assertEqual(len(box.folders["Processed/rejected"]), len(messages))
        self.assertEqual(self.requests(), [])
        self.assertEqual(self.sent, [])

    def test_book_failure_keeps_whole_book_and_its_selection_until_retry(self):
        self.failing.add("https://example.com/b")
        self.cycle(
            fixtures.FakeMailbox(
                [self.command("book", "https://example.com/a\nhttps://example.com/b")]
            )
        )
        self.assertEqual(self.sent, [])
        request = self.requests()[0]
        self.assertEqual(request["status"], "pending")
        self.assertEqual(len(json.loads(request["selection"])), 2)
        self.assertEqual(request["next_try"], (date.today() + timedelta(days=7)).isoformat())
        self.failing.clear()
        self.discovery.reset_mock()
        self.cycle(fixtures.FakeMailbox([]))
        self.assertEqual(self.sent, [])
        with store.Store(self.state) as database:
            database.db.execute("UPDATE requests SET next_try = NULL")
        self.cycle(fixtures.FakeMailbox([]))
        self.assertEqual(len(self.sent), 1)
        self.discovery.assert_not_called()

    def test_chapter_limit_never_sends_a_partial_book(self):
        self.discovery.side_effect = lambda source, **_: [
            f"https://example.com/{n}" for n in range(51)
        ]
        self.cycle(fixtures.FakeMailbox([self.command("book", "https://example.com/contents")]))
        self.assertEqual(self.sent, [])
        self.assertEqual(self.requests()[0]["status"], "failed")

    def test_book_mail_size_limit_never_splits_the_book(self):
        with patch.object(run.smtp, "compose", side_effect=ValueError("email size limit exceeded")):
            self.cycle(fixtures.FakeMailbox([self.command("book", "https://example.com/a")]))
        self.assertEqual(self.sent, [])
        self.assertEqual(self.requests()[0]["status"], "failed")

    def test_crash_during_book_send_holds_request_until_resolution(self):
        self.transmit_result = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.cycle(
                fixtures.FakeMailbox([self.command("book: Saved title", "https://example.com/a")])
            )
        self.cycle(fixtures.FakeMailbox([]))
        self.assertEqual(len(self.sent), 1)
        request = self.requests()[0]
        self.assertEqual(request["status"], "held")
        config = fixtures.settings_for(self.state)
        with patch.object(run.smtp, "transmit", return_value=("sent", {}, "")):
            tools.resend(config, request["delivery"], send=True)
        self.assertEqual(self.requests()[0]["status"], "sent")

    def test_definite_smtp_failure_retries_book_but_consumes_send_now(self):
        self.transmit_result = ("failed", {}, "SMTP refused")
        self.cycle(
            fixtures.FakeMailbox(
                [
                    self.command("book", "https://example.com/book"),
                    self.command("send now"),
                    self.submit("https://example.com/digest"),
                ]
            )
        )
        requests = {r["kind"]: r for r in self.requests()}
        self.assertEqual(requests["book"]["status"], "pending")
        self.assertEqual(requests["send_now"]["status"], "failed")

    def test_filing_failure_does_not_duplicate_book_request(self):
        box = fixtures.FakeMailbox(
            [self.command("book", files=[("notes.txt", b"notes", "text/plain")])]
        )
        box.fail_delete = True
        with self.assertRaises(OSError):
            self.cycle(box)
        self.cycle(box)
        self.assertEqual(len(self.requests()), 1)
        self.assertEqual(len(self.sent), 1)

    def test_dry_run_previews_without_recording_commands_or_sending(self):
        box = fixtures.FakeMailbox(
            [self.command("book", "https://example.com/a"), self.command("send now")]
        )
        self.cycle(box, dry_run=True)
        self.assertEqual(len(box.messages), 2)
        self.assertEqual(self.sent, [])
        self.assertFalse((self.state / "relay.sqlite3").exists())
        self.assertFalse((self.state / "articles").exists())
        self.assertFalse((self.state / "attachments").exists())
        self.assertTrue(Path("output/preview/ana-book-1.epub").exists())

    def test_digest_preparation_failure_does_not_block_a_book(self):
        with patch.object(run, "fit", side_effect=ValueError("Digest build failed")):
            self.cycle(
                fixtures.FakeMailbox(
                    [
                        self.submit("https://example.com/a", "https://example.com/b"),
                        self.command("book", "https://example.com/book"),
                    ]
                )
            )
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.requests()[0]["status"], "sent")

    def test_book_and_digest_with_same_url_have_independent_queues(self):
        self.cycle(
            fixtures.FakeMailbox(
                [
                    self.submit("https://example.com/a"),
                    self.command("book", "https://example.com/a"),
                ]
            )
        )
        self.assertEqual(len(self.sent), 1)
        with store.Store(self.state) as database:
            self.assertEqual(database.queue()[0]["status"], "pending")

    def test_send_now_crash_is_not_automatically_forced_again(self):
        self.transmit_result = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.cycle(
                fixtures.FakeMailbox(
                    [
                        self.submit("https://example.com/a"),
                        self.command("send now"),
                    ]
                )
            )
        self.cycle(fixtures.FakeMailbox([]))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.requests()[0]["status"], "held")

    def test_shared_attachment_survives_until_all_book_requests_finish(self):
        files = store.Attachments(self.state)
        source = files.save("notes.txt", b"Shared reading")
        with store.Store(self.state) as database:
            for key in ("first", "second"):
                database.record_message(key, "ana", "", [source], kind="book")
            requests = database.requests("ana")
            delivery = database.start_delivery(
                "ana",
                self.state / "book.epub",
                ["ana@kindle.com"],
                [],
                [],
                100,
                request_ids=[requests[0]["id"]],
                article_count=1,
            )
            database.finish_delivery(delivery, "sent")
            files.prune(database.waiting_attachments())
            self.assertTrue(files.path(source).is_file())
            database.complete_requests([requests[1]["id"]], "failed", "Cannot convert")
            files.prune(database.waiting_attachments())
            self.assertFalse(files.path(source).exists())


class BookDiscoveryTests(unittest.TestCase):
    def test_failed_probe_still_allows_normal_reader_fallbacks(self):
        with patch.object(collections, "fetch", side_effect=URLError("Site refuses direct access")):
            self.assertEqual(
                collections.book_sources("https://example.com/article"),
                ["https://example.com/article"],
            )

    def discover(self, markup, content_type="text/html"):
        response = Response(markup, "https://example.com/index", content_type, "utf-8")

        def fetch(source, **kwargs):
            return (
                response
                if source == "https://example.com/index"
                else Response(b"<article>A chapter</article>", source, "text/html", "utf-8")
            )

        with patch.object(collections, "fetch", side_effect=fetch):
            return collections.book_sources("https://example.com/index")

    def test_regular_article_and_same_page_contents_remain_one_source(self):
        self.assertEqual(
            self.discover(b"<article>Reading</article>"), ["https://example.com/index"]
        )
        self.assertEqual(
            self.discover(b'<nav id="toc"><a href="#section">Section</a></nav>'),
            ["https://example.com/index"],
        )
        self.assertEqual(
            self.discover(b"%PDF-1.0", "application/pdf"), ["https://example.com/index"]
        )

    def test_explicit_contents_select_same_site_chapters_in_order(self):
        self.assertEqual(
            self.discover(
                b'<nav id="toc"><a href="two">Two</a><a href="one">One</a>'
                b'<a href="https://other.example/a">Other</a></nav>'
            ),
            ["https://example.com/two", "https://example.com/one"],
        )


if __name__ == "__main__":
    unittest.main()
