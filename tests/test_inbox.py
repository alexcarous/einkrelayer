"""Filing and deleting mail on IMAP servers, including ones without MOVE or UIDPLUS."""

import imaplib
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from einkrelayer.relay.inbox import Mailbox

CONFIG = SimpleNamespace(imap_username="relay", imap_password="secret", folder="INBOX")


class BasicServer:
    """An IMAP connection that answers BAD to MOVE and UID EXPUNGE, as older servers do."""

    def __init__(self, unsupported=("MOVE", "EXPUNGE")):
        self.unsupported = unsupported
        self.calls = []

    def login(self, *_):
        return "OK", []

    def select(self, *_):
        return "OK", []

    def list(self, *_):
        return "OK", [b'(\\HasNoChildren) "." "INBOX"']

    def create(self, *_):
        return "NO", []

    def subscribe(self, *_):
        return "OK", []

    def uid(self, command, *args):
        self.calls.append((command, *args))
        if command in self.unsupported:
            raise imaplib.IMAP4.error(f"{command} command error: BAD [b'Unknown command']")
        return "OK", []

    def expunge(self):
        self.calls.append(("expunge",))
        return "OK", []


class FallbackTests(unittest.TestCase):
    def test_move_falls_back_to_copy_and_delete(self):
        server = BasicServer()
        box = Mailbox(CONFIG, server)
        box.move(b"7", box.folder("Processed", "ana"))
        self.assertEqual([call[0] for call in server.calls], ["MOVE", "COPY", "STORE", "EXPUNGE"])
        self.assertEqual(server.calls[1], ("COPY", b"7", '"Processed.ana"'))

    def test_no_uidplus_leaves_only_the_target_flagged_without_expunge(self):
        server = BasicServer()
        Mailbox(CONFIG, server).delete(b"7")
        self.assertEqual([call[0] for call in server.calls], ["STORE", "EXPUNGE"])
        self.assertEqual(server.calls[0][1:], (b"7", "+FLAGS", r"(\Deleted)"))

    def test_uidplus_expunge_targets_only_the_processed_message(self):
        server = BasicServer(unsupported=())
        Mailbox(CONFIG, server).delete(b"7")
        self.assertEqual(server.calls[-1], ("EXPUNGE", b"7"))
        self.assertNotIn(("expunge",), server.calls)

    def test_failed_store_never_expunges(self):
        server = BasicServer()
        original = server.uid
        server.uid = lambda command, *args: (
            ("NO", []) if command == "STORE" else original(command, *args)
        )
        for action in (lambda box: box.delete(b"7"), lambda box: box.move(b"7", "Processed")):
            with self.assertRaisesRegex(ValueError, "Could not delete"):
                action(Mailbox(CONFIG, server))
        self.assertFalse(any(c[0].lower() == "expunge" for c in server.calls))

    def test_deferred_deletions_are_not_intaken_again(self):
        server = BasicServer()
        server.uid = Mock(return_value=("OK", [b"8 9"]))
        self.assertEqual(Mailbox(CONFIG, server).uids(), [b"8", b"9"])
        server.uid.assert_called_once_with("SEARCH", None, "UNDELETED")

    def test_a_dropped_connection_still_stops_the_run(self):
        server = BasicServer(unsupported=())
        server.uid = lambda *_: (_ for _ in ()).throw(imaplib.IMAP4.abort("socket error"))
        with self.assertRaises(imaplib.IMAP4.abort):
            Mailbox(CONFIG, server).move(b"7", "Processed")


if __name__ == "__main__":
    unittest.main()
