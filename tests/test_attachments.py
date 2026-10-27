"""Documents sent as email attachments: which are taken, and how they are named and stored."""

import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

from einkrelayer.relay import mail, run
from einkrelayer.relay.store import Attachments


def message(*files):
    msg = EmailMessage()
    msg["From"] = "ana@example.org"
    msg.set_content("See attached.")
    for name, data, kind in files:
        maintype, subtype = kind.split("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return mail.parse(msg.as_bytes())


class AttachmentTests(unittest.TestCase):
    def test_only_supported_documents_are_taken_with_safe_names(self):
        found = mail.attachments(
            message(
                ("report.pdf", b"%PDF-1.7 report", "application/pdf"),
                ("../../secret notes.MD", b"# Notes", "text/markdown"),
                ("logo.png", b"\x89PNG", "image/png"),
                (
                    "contract",
                    b"PK\x03\x04",
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                ),
                ("empty.txt", b"", "text/plain"),
            )
        )
        self.assertEqual(
            [name for name, _ in found], ["report.pdf", "secret notes.MD", "contract.docx"]
        )

    def test_message_text_marked_inline_is_not_an_attachment(self):
        msg = EmailMessage()
        msg["From"] = "ana@example.org"
        msg.set_content("https://example.com/a")
        msg.add_alternative("<p><a href='https://example.com/a'>a</a></p>", subtype="html")
        for part in msg.iter_parts():
            part["Content-Disposition"] = "inline"
        msg.add_attachment(
            b"# Notes",
            maintype="text",
            subtype="markdown",
            filename="notes.md",
            disposition="inline",
        )
        found = mail.attachments(mail.parse(msg.as_bytes()))
        self.assertEqual(found, [("notes.md", b"# Notes")])

    def test_oversized_attachments_are_skipped(self):
        with patch.object(mail, "MAX_ATTACHMENT_BYTES", 10):
            found = mail.attachments(message(("big.txt", b"x" * 11, "text/plain")))
        self.assertEqual(found, [])

    def test_stored_files_are_private_and_pruned_when_no_longer_waiting(self):
        with tempfile.TemporaryDirectory() as state:
            store = Attachments(Path(state))
            kept = store.save("a.txt", b"one")
            gone = store.save("b.txt", b"two")
            self.assertEqual(store.path(kept).read_bytes(), b"one")
            self.assertEqual(store.path(kept).stat().st_mode & 0o777, 0o600)
            store.prune([kept])
            self.assertTrue(store.path(kept).exists())
            self.assertFalse(store.path(gone).exists())
            for bad in ["attachment:../x", "attachment:" + "0" * 64 + "/../x", "attachment:abc/x"]:
                with self.assertRaises(ValueError):
                    store.path(bad)

    def test_couldnt_fetch_page_names_an_attachment_without_a_link(self):
        rows = [{"url": "attachment:" + "a" * 64 + "/report.pdf", "reason": "Unreadable PDF"}]
        page = run.couldnt_fetch(rows)
        self.assertIn("Attached document: report.pdf", page.html)
        self.assertNotIn("href", page.html)


if __name__ == "__main__":
    unittest.main()
