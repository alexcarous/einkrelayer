"""Crash recovery and mutual exclusion for delivery management, without sending mail."""

import sqlite3
import tempfile
import unittest
from contextlib import closing
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from einkrelayer.relay import tools
from einkrelayer.relay.store import SCHEMA, Attachments, Locked, Store, run_lock


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state = Path(directory.name)
        self.book = self.state / "digest.epub"
        self.book.write_bytes(b"composer is mocked; no EPUB build needed here")
        self.config = SimpleNamespace(state=self.state, readers={}, digest_title="Digest")
        message = EmailMessage()
        message["Message-ID"] = "<resend@example.com>"
        message.set_content("Digest")
        self.message = message
        for name, result in [
            ("settings", {"recipients": ["reader@example.com"]}),
            ("compose", message),
        ]:
            patcher = patch.object(tools.smtp, name, return_value=result)
            patcher.start()
            self.addCleanup(patcher.stop)

    def original(self, status="uncertain", url="https://example.com/a"):
        with Store(self.state) as store:
            store.record_message("submission", "reader", "Processed/reader", [url])
            link = store.queue()[0]["id"]
            delivery = store.start_delivery(
                "reader",
                self.book,
                ["reader@example.com"],
                [link],
                [],
                100,
                message_id="<original@example.com>",
            )
            store.finish_delivery(delivery, status)
        return delivery

    def resend(self, delivery, result=("sent", {}, "")):
        def transmit(*args):
            # Inspect through a separate connection: the attempt must already be committed.
            with Store(self.state) as store:
                attempt = store.deliveries()[0]
                self.assertEqual(attempt["status"], "sending")
                self.assertEqual(attempt["message_id"], self.message["Message-ID"])
                self.assertEqual(attempt["articles"], 1)
            with self.assertRaises(Locked):
                with run_lock(self.state):
                    pass
            if isinstance(result, BaseException):
                raise result
            return result

        with patch.object(tools.smtp, "transmit", side_effect=transmit):
            return tools.resend(self.config, delivery, send=True)

    def test_crash_during_resend_is_recorded_and_recovers_without_sending(self):
        original = self.original("sent")
        with self.assertRaises(KeyboardInterrupt):
            self.resend(original, KeyboardInterrupt())
        with Store(self.state) as store:
            attempts = store.deliveries()
            self.assertEqual(len(attempts), 2)
            self.assertEqual(attempts[0]["resend_of"], original)
            self.assertEqual(store.interrupted(), [attempts[0]["id"]])
            self.assertEqual(
                store.delivery(attempts[0]["id"])["message_id"], self.message["Message-ID"]
            )
            self.assertEqual(store.delivery(original)["status"], "sent")
            self.assertEqual(store.due("reader", tools.datetime.now().date()), [])

    def test_successful_resend_settles_original_held_links(self):
        original = self.original()
        self.resend(original)
        with Store(self.state) as store:
            self.assertEqual(store.delivery(original)["status"], "sent")
            self.assertEqual(store.queue(), [])
            self.assertEqual(len(store.deliveries()), 2)

    def test_failed_resend_does_not_release_an_uncertain_original(self):
        original = self.original()
        self.resend(original, ("failed", {}, "login refused"))
        with Store(self.state) as store:
            self.assertEqual(store.delivery(original)["status"], "uncertain")
            self.assertEqual(store.queue()[0]["status"], "held")
        tools.resolve(self.config, original, "failed")
        with Store(self.state) as store:
            self.assertEqual(store.queue()[0]["status"], "pending")

    def test_uncertain_resend_must_be_resolved_before_releasing_original(self):
        original = self.original()
        self.resend(original, ("uncertain", {}, "connection lost"))
        with self.assertRaisesRegex(ValueError, "resend attempts"):
            tools.resolve(self.config, original, "failed")
        with Store(self.state) as store:
            attempt = store.deliveries()[0]["id"]
            self.assertEqual(store.queue()[0]["status"], "held")
        self.assertIn(
            "original delivery state is retained", tools.resolve(self.config, attempt, "failed")
        )
        tools.resolve(self.config, original, "failed")
        with Store(self.state) as store:
            self.assertEqual(store.queue()[0]["status"], "pending")

    def test_resolving_crashed_resend_as_sent_settles_original(self):
        original = self.original()
        with self.assertRaises(KeyboardInterrupt):
            self.resend(original, KeyboardInterrupt())
        with Store(self.state) as store:
            attempt = store.deliveries()[0]["id"]
        tools.resolve(self.config, attempt, "sent")
        with Store(self.state) as store:
            self.assertEqual(store.delivery(original)["status"], "sent")
            self.assertEqual(store.queue(), [])

    def test_resending_a_retry_tracks_root_and_uses_original_edition_date(self):
        original = self.original()
        with Store(self.state) as store:
            store.db.execute("UPDATE deliveries SET created = '2020-01-02T12:00:00+00:00'")
        self.resend(original, ("failed", {}, "connection refused"))
        with Store(self.state) as store:
            attempt = store.deliveries()[0]["id"]
        self.resend(attempt)
        with Store(self.state) as store:
            self.assertEqual(store.deliveries()[0]["resend_of"], original)
        self.assertEqual(tools.smtp.compose.call_args.kwargs["subject"], "Digest — 2020-01-02")

    def test_mutating_commands_cannot_run_under_another_relay_lock(self):
        original = self.original()
        with run_lock(self.state), patch.object(tools.smtp, "transmit") as transmit:
            for action in (
                lambda: tools.resend(self.config, original, send=True),
                lambda: tools.resolve(self.config, original, "failed"),
            ):
                with self.assertRaises(Locked):
                    action()
            transmit.assert_not_called()
        with Store(self.state) as store:
            self.assertEqual(store.delivery(original)["status"], "uncertain")

    def test_preview_does_not_create_or_recover_attempts(self):
        original = self.original("sending")
        with patch.object(tools.smtp, "transmit") as transmit:
            self.assertIn("ready to resend", tools.resend(self.config, original))
            transmit.assert_not_called()
        with Store(self.state) as store:
            self.assertEqual(len(store.deliveries()), 1)
            self.assertEqual(store.delivery(original)["status"], "sending")

    def test_old_database_migrates_without_losing_history(self):
        path = self.state / "relay.sqlite3"
        with closing(sqlite3.connect(path)) as old, old:
            old.executescript(
                SCHEMA.replace("finished TEXT,\n    resend_of INTEGER", "finished TEXT")
            )
            old.execute(
                "INSERT INTO deliveries (reader,created,status,epub,recipients,articles)"
                " VALUES ('reader','2020-01-02','sent','book.epub','[]',3)"
            )
        before = path.read_bytes()
        with Store(self.state, dry_run=True) as store:
            self.assertIsNone(store.delivery(1)["resend_of"])
        self.assertEqual(path.read_bytes(), before)
        with Store(self.state) as store:
            self.assertEqual(store.delivery(1)["articles"], 3)
            self.assertIsNone(store.delivery(1)["resend_of"])

    def test_resend_and_resolution_remove_completed_attachments_but_keep_uncertain_ones(self):
        files = Attachments(self.state)
        url = files.save("notes.txt", b"private attached reading")
        path = files.path(url)
        original = self.original(url=url)
        self.resend(original, ("failed", {}, "connection refused"))
        self.assertTrue(path.is_file())
        self.resend(original, ("uncertain", {}, "connection lost"))
        self.assertTrue(path.is_file())
        with Store(self.state) as store:
            attempt = store.deliveries()[0]["id"]
        tools.resolve(self.config, attempt, "sent")
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
