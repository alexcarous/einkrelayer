"""Mail intake, the durable queue, digest runs and delivery tools, with local fakes only."""

import base64
import copy
import io
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import date, timedelta
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

import dkim

from einkrelayer.capture.readers import read
from einkrelayer.relay import config as relay_config
from einkrelayer.relay import mail, run, store, tools

ROOT = Path(__file__).parent
ARTICLE = read(ROOT / "corpus/html/essay.html", cache=None)


def key_pair(directory):
    private = Path(directory) / "dkim.pem"
    subprocess.run(
        ["openssl", "genrsa", "-out", str(private), "2048"], check=True, capture_output=True
    )
    public = subprocess.run(
        ["openssl", "rsa", "-in", str(private), "-pubout", "-outform", "DER"],
        check=True,
        capture_output=True,
    ).stdout
    record = b"v=DKIM1; k=rsa; p=" + base64.b64encode(public)
    return private.read_bytes(), record


class Signer:
    _keys = None

    def __init__(self):
        if Signer._keys is None:
            with tempfile.TemporaryDirectory() as directory:
                Signer._keys = key_pair(directory)
        self.private, self.record = Signer._keys

    def dns(self, name, timeout=5):
        return self.record if name == b"sel._domainkey.example.org." else None

    def message(
        self,
        sender,
        body,
        *,
        subject="links",
        domain=b"example.org",
        length=False,
        files=(),
        headers=None,
        recipient="eink+ana@example.org",
    ):
        message = EmailMessage()
        message["From"] = sender
        message["To"] = recipient
        message["Subject"] = subject
        message["Message-ID"] = make_msgid()
        message.set_content(body)
        for name, data, kind in files:
            maintype, subtype = kind.split("/")
            message.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
        raw = message.as_bytes()
        signature = dkim.sign(
            raw,
            b"sel",
            domain,
            self.private,
            include_headers=headers
            if headers is not None
            else [b"from", b"to", b"subject", b"message-id"],
            length=length,
        )
        return signature + raw


class FakeMailbox:
    def __init__(self, messages):
        self.messages = dict(enumerate(messages, 1))
        self.folders = {}

    def folder(self, *parts):
        return "/".join(parts)

    def uids(self):
        return list(self.messages)

    def size(self, uid):
        return len(self.messages[uid])

    def fetch(self, uid):
        return self.messages[uid]

    def move(self, uid, folder):
        self.folders.setdefault(folder, []).append(self.messages.pop(uid))

    def delete(self, uid):
        if getattr(self, "fail_delete", False):
            self.fail_delete = False
            raise OSError("connection dropped")
        self.deleted = getattr(self, "deleted", []) + [self.messages.pop(uid)]

    def close(self):
        pass


def settings_for(state, minimum=2, days=()):
    reader = relay_config.Reader(
        "ana", ["ana@example.org"], ["ana@kindle.com"], "Ana", minimum, list(days)
    )
    return relay_config.RelayConfig(
        "imap.example.net",
        993,
        "relay",
        "secret",
        "INBOX",
        Path(state),
        {"ana": reader},
        "Digest",
        "eink@example.org",
    )


SMTP = {
    "recipients": [],
    "sender": "relay@example.net",
    "host": "smtp.example.net",
    "port": 465,
    "mode": "ssl",
    "username": "relay",
    "password": "secret",
    "timeout": 5,
    "max_bytes": 20_000_000,
    "subject": "Digest",
}


def article(url):
    document = copy.deepcopy(ARTICLE)
    document.source = url
    return document


class ConfigTests(unittest.TestCase):
    def write(self, text):
        handle = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
        self.addCleanup(os.unlink, handle.name)
        handle.write(text)
        handle.close()
        return handle.name

    def test_readers_load_with_defaults(self):
        path = self.write(
            '[ana]\nlabel = "Ana"\nsenders = ["Ana@Example.org"]\nkindle = "ana@kindle.com"\n'
        )
        reader = relay_config.load_readers(path)["ana"]
        self.assertEqual(
            (reader.senders, reader.kindle, reader.min_articles),
            (["ana@example.org"], ["ana@kindle.com"], 10),
        )

    def test_receiving_mailbox_is_configurable(self):
        config = settings_for("unused")
        config.relay_address = "Reading.List@example.net"
        self.assertIs(
            config.reader_for("ANA@example.org", "READING.LIST+aNA@EXAMPLE.NET"),
            config.readers["ana"],
        )
        for recipient in (
            "readingXlist+ana@example.net",
            "reading.list+ana@example.org",
            "eink+ana@example.net",
            "reading.list@example.net",
        ):
            self.assertIsNone(config.reader_for("ana@example.org", recipient))

    def test_load_requires_an_untagged_receiving_address(self):
        path = self.write(
            '[ana]\nlabel="Ana"\nsenders=["ana@example.org"]\nkindle="reader@kindle.com"\n'
        )
        values = {
            "IMAP_HOST": "imap.example.org",
            "IMAP_USERNAME": "reader",
            "IMAP_PASSWORD": "test-only",
            "RELAY_USERS_FILE": path,
        }
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(relay_config, "dotenv_values", return_value=values),
        ):
            with self.assertRaisesRegex(ValueError, "RELAY_ADDRESS"):
                relay_config.load()
            values["RELAY_ADDRESS"] = "readings+ana@example.org"
            with self.assertRaisesRegex(ValueError, "base mailbox"):
                relay_config.load()
            values["RELAY_ADDRESS"] = "Readings@Example.org"
            self.assertEqual(relay_config.load().relay_address, "readings@example.org")

    def test_usernames_are_case_insensitive_and_sender_lists_are_per_user(self):
        base = 'label = "Reader"\nsenders = ["ana@example.org"]\nkindle = "k@kindle.com"\n'
        readers = relay_config.load_readers(self.write("[Ana]\n" + base + "[bo]\n" + base))
        config = settings_for("unused")
        config.readers = readers
        self.assertIs(config.reader_for("ANA@example.org", "EINK+aNA@EXAMPLE.ORG"), readers["Ana"])
        self.assertIs(config.reader_for("ana@example.org", "eink+bo@example.org"), readers["bo"])
        self.assertIsNone(config.reader_for("eve@example.org", "eink+Ana@example.org"))
        with self.assertRaisesRegex(ValueError, "Duplicate reader"):
            relay_config.load_readers(self.write("[Ana]\n" + base + "[ana]\n" + base))

    def test_bad_reader_lists_are_refused(self):
        cases = [
            '[ana]\nsenders = ["a@example.org"]\nkindle = "k@kindle.com"\n'
            '[bo]\nsenders = ["a@example.org"]\nkindle = "b@kindle.com"\n',
            '["a/b"]\nsenders = ["a@example.org"]\nkindle = "k@kindle.com"\n',
            '[ana]\nsenders = ["a@example.org"]\nkindle = "k@kindle.com"\nmin_articles = 0\n',
            '[ana]\nsenders = ["a@example.org"]\nkindle = "k@kindle.com"\ndays = ["someday"]\n',
            '[ana]\nsenders = ["not an address"]\nkindle = "k@kindle.com"\n',
            '[ana]\nsenders = 123\nkindle = "k@kindle.com"\n',
            '[ana]\nsenders = [123]\nkindle = "k@kindle.com"\n',
            '[ana]\nsenders = ["a@example.org"]\nkindle = 123\n',
            '[ana]\nsenders = ["a@example.org"]\nkindle = "k@kindle.com"\ndays = 123\n',
        ]
        for text in cases:
            with self.assertRaises(ValueError):
                relay_config.load_readers(self.write(text))

    def test_labels_preserve_flow_identity_and_title(self):
        base = '[ana]\nsenders = ["ana@example.org"]\nkindle = "ana@kindle.com"\n'
        original = relay_config.load_readers(self.write(base + 'label = "Ana"\n'))["ana"]
        self.assertEqual(original.display, "Ana (ana)")
        for label in ("Priya", "Priya Patel"):
            reader = relay_config.load_readers(
                self.write(base + f'label = "{label}"\ntitle = "Readings"\n')
            )["ana"]
            self.assertEqual(reader.display, f"{label} (ana)")
            self.assertEqual(
                (reader.name, reader.senders, reader.kindle, reader.title),
                (original.name, original.senders, original.kindle, "Readings"),
            )

    def test_missing_label_is_refused(self):
        with self.assertRaisesRegex(ValueError, "label"):
            relay_config.load_readers(
                self.write('[ana]\nsenders = ["ana@example.org"]\nkindle = "ana@kindle.com"\n')
            )

    def test_invalid_labels_are_refused(self):
        base = '[ana]\nsenders = ["ana@example.org"]\nkindle = "ana@kindle.com"\n'
        for value in ("123", '""', '"  "', '"line\\nline"', '"line\\tline"'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                relay_config.load_readers(self.write(base + f"label = {value}\n"))

    def test_queue_and_history_labels_do_not_change_stored_identity(self):
        with tempfile.TemporaryDirectory() as state:
            config = settings_for(state)
            with store.Store(config.state) as database:
                database.record_message(
                    "submission", "ana", "Processed/ana", ["https://example.org"]
                )
                link_id = database.queue("ana")[0]["id"]
                database.start_delivery(
                    "ana", Path(state) / "book.epub", ["ana@kindle.com"], [link_id], [], 100
                )
            for label in ("Priya", "Priya Patel"):
                config.readers["ana"].label = label
                self.assertIn(f"{label} (ana)", tools.queue(config, "ana")[0])
                self.assertIn(f"{label} (ana)", tools.history(config)[0])
            config.readers.clear()
            self.assertIn("ana", tools.history(config)[0])


class MailTests(unittest.TestCase):
    def message(self, body, html=None, subject="links"):
        message = EmailMessage()
        message["From"] = "ana@example.org"
        message["Subject"] = subject
        message.set_content(body)
        if html:
            message.add_alternative(html, subtype="html")
        return mail.parse(message.as_bytes())

    def test_links_are_trimmed_ordered_and_unique(self):
        found = mail.links(
            self.message(
                "Read https://example.com/a. And (https://example.com/b) or "
                "https://en.wikipedia.org/wiki/Cat_(animal), https://example.com/a",
                subject="see https://example.com/s",
            )
        )
        self.assertEqual(
            found,
            [
                "https://example.com/s",
                "https://example.com/a",
                "https://example.com/b",
                "https://en.wikipedia.org/wiki/Cat_(animal)",
            ],
        )

    def test_html_links_are_used_when_text_has_none(self):
        found = mail.links(
            self.message(
                "no links",
                '<p><a href="https://example.com/x">x</a><a href="mailto:a@b.c">m</a></p>',
            )
        )
        self.assertEqual(found, ["https://example.com/x"])

    def test_logs_hide_query_strings(self):
        text = mail.redact("got https://example.com/a?gift=SECRET#frag")
        self.assertEqual(text, "got https://example.com/a?…")

    def test_sender_needs_exactly_one_address(self):
        message = EmailMessage()
        message["From"] = "a@example.org, b@example.org"
        self.assertEqual(mail.sender(message), "")


class DkimTests(unittest.TestCase):
    def setUp(self):
        self.signer = Signer()

    def test_aligned_signature_passes(self):
        raw = self.signer.message("ana@example.org", "https://example.com/a")
        self.assertTrue(mail.dkim_aligned(raw, "ana@example.org", self.signer.dns))
        self.assertTrue(mail.dkim_aligned(raw, "ana@mail.example.org", self.signer.dns))

    def test_other_domain_tampering_and_length_limits_fail(self):
        raw = self.signer.message("ana@example.org", "https://example.com/a")
        self.assertFalse(mail.dkim_aligned(raw, "ana@example.com", self.signer.dns))
        tampered = raw.replace(b"https://example.com/a", b"https://example.com/b")
        self.assertFalse(mail.dkim_aligned(tampered, "ana@example.org", self.signer.dns))
        limited = self.signer.message("ana@example.org", "https://example.com/a", length=True)
        self.assertFalse(mail.dkim_aligned(limited, "ana@example.org", self.signer.dns))

    def test_temporary_dns_failure_is_not_a_failed_check(self):
        raw = self.signer.message("ana@example.org", "https://example.com/a")
        for error in (mail.LookupUnavailable("Timeout"), dkim.DnsTimeoutError("timeout")):
            with self.assertRaises(mail.LookupUnavailable):
                mail.dkim_aligned(raw, "ana@example.org", Mock(side_effect=error))

    def test_unsigned_subject_or_message_identity_is_refused(self):
        for headers in ([b"from", b"message-id"], [b"from", b"subject"]):
            raw = self.signer.message("ana@example.org", "https://example.com/a", headers=headers)
            self.assertTrue(dkim.verify(raw, dnsfunc=self.signer.dns))
            self.assertFalse(mail.dkim_aligned(raw, "ana@example.org", self.signer.dns))

    def test_extra_unsigned_subject_cannot_inject_a_link(self):
        raw = self.signer.message("ana@example.org", "https://example.com/a")
        forged = b"Subject: https://attacker.example/injected\r\n" + raw
        self.assertTrue(dkim.verify(forged, dnsfunc=self.signer.dns))
        self.assertFalse(mail.dkim_aligned(forged, "ana@example.org", self.signer.dns))

    def test_dns_errors_from_the_resolver_mean_try_again(self):
        with patch.object(mail.dns.resolver, "resolve", side_effect=mail.dns.exception.Timeout):
            with self.assertRaises(mail.LookupUnavailable):
                mail.txt_record(b"sel._domainkey.example.org.")
        missing = mail.dns.resolver.NXDOMAIN
        with patch.object(mail.dns.resolver, "resolve", side_effect=missing):
            self.assertIsNone(mail.txt_record(b"sel._domainkey.example.org."))


class RetryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = store.Store(directory.name)
        self.addCleanup(self.store.close)
        self.store.record_message("m", "ana", "Processed/ana", ["https://example.com/a"])
        self.link = self.store.queue()[0]["id"]

    def test_failures_wait_7_14_28_days_then_give_up(self):
        today = date(2026, 10, 25)
        waits = []
        for _ in range(4):
            status, next_try = self.store.record_failure(self.link, "Network", today)
            waits.append((next_try - today).days if next_try else status)
        self.assertEqual(waits, [7, 14, 28, "failed"])

    def test_busy_site_retries_tomorrow_a_few_times(self):
        today = date(2026, 10, 25)
        for _ in range(store.BUSY_RETRIES):
            _, next_try = self.store.record_failure(self.link, "Busy", today, busy=True)
            self.assertEqual(next_try, today + timedelta(days=1))
        _, next_try = self.store.record_failure(self.link, "Busy", today, busy=True)
        self.assertEqual(next_try, today + timedelta(days=7))

    def test_resubmitting_an_open_link_is_ignored(self):
        added = self.store.record_message("n", "ana", "Processed/ana", ["https://example.com/a"])
        self.assertEqual(added, 0)

    def test_error_classes(self):
        self.assertEqual(run.classify(HTTPError("u", 429, "", {}, None))[1:], (True, True))
        self.assertEqual(run.classify(HTTPError("u", 404, "", {}, None))[2], False)
        self.assertEqual(run.classify(URLError("down"))[0], "Network request failed")
        reason = run.classify(ValueError("Refusing a non-public destination"))
        self.assertFalse(reason[2])


class RunTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.state = self.root / "state"
        self.signer = Signer()
        cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, cwd)
        self.fetched = []
        self.sent = []
        self.transmit_result = ("sent", {"ana@kindle.com": "accepted"}, "")
        self.failing = set()
        patches = [
            patch("einkrelayer.capture.readers.read", side_effect=self.read),
            patch("einkrelayer.relay.run.smtp.transmit", side_effect=self.transmit),
            patch("einkrelayer.relay.run.smtp.settings", return_value=dict(SMTP)),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def read(self, url, cache=None, **_):
        self.fetched.append(str(url))
        if url in self.failing:
            raise URLError("down")
        if isinstance(url, Path):  # an attached document, read from its stored file
            document = article(url.as_uri())
            document.html = f"<body><p>{url.read_text()}</p></body>"
            return document
        return article(url)

    def transmit(self, settings, message):
        self.sent.append((settings["recipients"], message))
        if isinstance(self.transmit_result, BaseException):
            raise self.transmit_result
        return self.transmit_result

    def submit(self, *urls, sender="ana@example.org"):
        return self.signer.message(sender, "\n".join(urls))

    def cycle(self, box, config=None, **options):
        config = config or settings_for(self.state)
        with redirect_stdout(io.StringIO()) as output:
            result = run.run(config, mailbox=box, dnsfunc=self.signer.dns, **options)
        return result, output.getvalue()

    def test_links_wait_for_the_threshold_then_send_once(self):
        box = FakeMailbox([self.submit("https://example.com/a")])
        result, _ = self.cycle(box)
        self.assertEqual(result, {"ana": "waiting"})
        self.assertEqual(len(box.folders["Processed/ana"]), 1)
        box.messages[9] = self.submit("https://example.com/b")
        result, log = self.cycle(box)
        self.assertEqual(result, {"ana": "sent"})
        self.assertIn("from cache", log)
        self.assertEqual(self.fetched, ["https://example.com/a", "https://example.com/b"])
        self.assertEqual(self.sent[0][0], ["ana@kindle.com"])
        result, _ = self.cycle(box)
        self.assertEqual(result, {"ana": "waiting"})
        self.assertEqual(len(self.sent), 1)
        lines = tools.history(settings_for(self.state))
        self.assertIn("sent  2 article(s)", lines[0])

    def test_invalid_recipients_cannot_queue_even_with_valid_sender_dkim(self):
        recipients = [
            "eink@example.org",
            "eink+unknown@example.org",
            "eink+ana@other.example",
            "other+ana@example.org",
            "",
            "eink+ana@example.org, eink+bo@example.org",
        ]
        messages = [
            self.signer.message("ana@example.org", "https://example.com/a", recipient=r)
            for r in recipients
        ]
        box = FakeMailbox(messages)
        self.cycle(box)
        self.assertEqual(box.folders["Processed/rejected"], messages)
        self.assertEqual(self.store_queue(), [])

    def test_user_allowlist_and_signed_recipient_prevent_cross_user_routing(self):
        config = settings_for(self.state, minimum=10)
        config.readers["bo"] = relay_config.Reader(
            "bo", ["bo@example.org"], ["bo@kindle.com"], "Bo"
        )
        wrong_user = self.signer.message(
            "ana@example.org", "https://example.com/a", recipient="eink+bo@example.org"
        )
        unsigned_to = self.signer.message(
            "ana@example.org", "https://example.com/a", headers=[b"from", b"subject", b"message-id"]
        )
        valid = self.signer.message("ana@example.org", "https://example.com/a")
        tampered = valid.replace(b"eink+ana@", b"eink+bo@")
        config.readers["bo"].senders.append("ana@example.org")
        # Even a shared allowlisted sender cannot have its signed To changed.
        box = FakeMailbox([unsigned_to, tampered])
        self.cycle(box, config)
        self.assertEqual(len(box.folders["Processed/rejected"]), 2)
        config.readers["bo"].senders.remove("ana@example.org")
        box = FakeMailbox([wrong_user])
        self.cycle(box, config)
        self.assertEqual(box.folders["Processed/rejected"], [wrong_user])
        self.assertEqual(self.store_queue(), [])

    def test_same_signed_content_routes_independently_for_shared_sender(self):
        config = settings_for(self.state, minimum=10)
        config.readers["bo"] = relay_config.Reader(
            "bo", ["ana@example.org"], ["bo@kindle.com"], "Bo", 10
        )
        first = self.signer.message(
            "ana@example.org", "https://example.com/a", recipient="EINK+ANA@EXAMPLE.ORG"
        )
        raw = first[first.index(b"From:") :].replace(
            b"EINK+ANA@EXAMPLE.ORG", b"eink+bo@example.org"
        )
        second = (
            dkim.sign(
                raw,
                b"sel",
                b"example.org",
                self.signer.private,
                include_headers=[b"from", b"to", b"subject", b"message-id"],
            )
            + raw
        )
        box = FakeMailbox([first, second])
        self.cycle(box, config)
        self.assertEqual(box.folders["Processed/ana"], [first])
        self.assertEqual(box.folders["Processed/bo"], [second])
        with store.Store(self.state) as database:
            self.assertEqual(len(database.queue("ana")), 1)
            self.assertEqual(len(database.queue("bo")), 1)

    def test_unknown_or_unsigned_senders_are_rejected(self):
        forged = self.submit("https://example.com/a").replace(
            b"https://example.com/a", b"https://example.com/z"
        )
        stranger = self.submit("https://example.com/a", sender="eve@example.org")
        box = FakeMailbox([forged, stranger])
        self.cycle(box)
        self.assertEqual(len(box.folders["Processed/rejected"]), 2)
        self.assertEqual(tools.queue(settings_for(self.state)), ["Queue is empty"])

    def test_dns_failure_leaves_the_message_for_the_next_run(self):
        box = FakeMailbox([self.submit("https://example.com/a")])
        with patch.object(self.signer, "dns", side_effect=mail.LookupUnavailable("Timeout")):
            _, log = self.cycle(box)
        self.assertIn("couldn't check a message", log)
        self.assertEqual((len(box.messages), box.folders), (1, {}))
        self.assertEqual(tools.queue(settings_for(self.state)), ["Queue is empty"])
        self.cycle(box)
        self.assertEqual(len(box.folders["Processed/ana"]), 1)

    def test_shared_message_id_does_not_let_a_forgery_hide_a_valid_submission(self):
        raw = self.submit("https://example.com/a")
        forged = raw.replace(b"https://example.com/a", b"https://example.com/z")
        box = FakeMailbox([forged, raw])
        self.cycle(box)
        self.assertEqual(box.folders["Processed/rejected"], [forged])
        self.assertEqual(box.folders["Processed/ana"], [raw])
        self.assertEqual(len(tools.queue(settings_for(self.state))), 1)

    def test_shared_message_id_does_not_bypass_authentication_after_acceptance(self):
        raw = self.submit("https://example.com/a")
        forged = raw.replace(b"https://example.com/a", b"https://example.com/z")
        box = FakeMailbox([raw, forged])
        self.cycle(box)
        self.assertEqual(box.folders["Processed/rejected"], [forged])
        self.assertEqual(box.folders["Processed/ana"], [raw])

    def test_redelivery_with_new_transport_headers_is_filed_without_requeuing(self):
        raw = self.submit("https://example.com/a")
        box = FakeMailbox([raw])
        self.cycle(box)
        with store.Store(self.state) as relay:
            relay.db.execute("UPDATE links SET status = 'sent'")
        replay = b"Received: from mail.example.org by relay.example.net; today\r\n" + raw
        box = FakeMailbox([replay])
        self.cycle(box)
        self.assertEqual(box.folders["Processed/ana"], [replay])
        self.assertEqual(tools.queue(settings_for(self.state)), ["Queue is empty"])

    def test_identical_content_without_its_signature_is_still_rejected(self):
        raw = self.submit("https://example.com/a")
        unsigned = raw[raw.index(b"From:") :]
        box = FakeMailbox([raw, unsigned])
        self.cycle(box)
        self.assertEqual(box.folders["Processed/rejected"], [unsigned])
        self.assertEqual(box.folders["Processed/ana"], [raw])

    def test_transient_size_or_fetch_failure_does_not_reject_the_message(self):
        raw = self.submit("https://example.com/a")
        for method in ("size", "fetch"):
            box = FakeMailbox([raw])
            with patch.object(box, method, return_value=None):
                _, log = self.cycle(box)
            self.assertIn("leaving it in the inbox", log)
            self.assertEqual(len(box.messages), 1)
            self.assertEqual(box.folders, {})
            self.cycle(box)
            self.assertEqual(box.folders["Processed/ana"], [raw])

    def test_message_recorded_before_a_crash_is_filed_not_requeued(self):
        raw = self.submit("https://example.com/a")
        box = FakeMailbox([raw])
        with patch.object(FakeMailbox, "move", side_effect=OSError("connection lost")):
            with self.assertRaises(OSError):
                self.cycle(box)
        self.cycle(box)
        self.assertEqual(box.folders["Processed/ana"], [raw])
        self.assertEqual(len(tools.queue(settings_for(self.state))), 1)

    def test_crash_while_sending_is_never_resent_automatically(self):
        box = FakeMailbox([self.submit("https://example.com/a", "https://example.com/b")])
        self.transmit_result = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.cycle(box)
        self.transmit_result = ("sent", {}, "")
        result, log = self.cycle(box)
        self.assertIn("marked uncertain", log)
        self.assertEqual(result, {"ana": "waiting"})
        self.assertEqual(len(self.sent), 1)
        message = tools.resolve(settings_for(self.state), 1, "failed")
        self.assertIn("return to the queue", message)
        result, _ = self.cycle(box)
        self.assertEqual(result, {"ana": "sent"})

    def test_refused_send_returns_links_to_the_queue(self):
        box = FakeMailbox([self.submit("https://example.com/a", "https://example.com/b")])
        self.transmit_result = ("failed", {}, "SMTP authentication failed")
        result, _ = self.cycle(box)
        self.assertEqual(result, {"ana": "failed"})
        self.transmit_result = ("sent", {}, "")
        result, _ = self.cycle(box)
        self.assertEqual(result, {"ana": "sent"})

    def test_failed_links_retry_then_appear_on_couldnt_fetch_page(self):
        self.failing = {"https://example.com/bad"}
        box = FakeMailbox([self.submit("https://example.com/bad")])
        self.cycle(box)
        relay = store.Store(self.state)
        self.addCleanup(relay.close)
        relay.db.execute("UPDATE links SET status = 'failed'")
        box.messages[5] = self.submit("https://example.com/a", "https://example.com/b")
        self.cycle(box)
        from zipfile import ZipFile

        epub = Path(relay.deliveries()[0]["epub"])
        with ZipFile(epub) as book:
            text = "".join(book.read(n).decode() for n in book.namelist() if n.endswith("xhtml"))
        self.assertIn("Couldn't fetch", text)
        self.assertIn("https://example.com/bad", text)
        self.assertEqual(tools.queue(settings_for(self.state)), ["Queue is empty"])

    def test_retry_schedule_skips_links_until_due(self):
        self.failing = {"https://example.com/a"}
        box = FakeMailbox([self.submit("https://example.com/a")])
        self.cycle(box)
        self.cycle(box)
        self.assertEqual(self.fetched, ["https://example.com/a"])
        self.assertIn("next try", tools.queue(settings_for(self.state))[0])

    def test_dry_run_changes_nothing(self):
        box = FakeMailbox([self.submit("https://example.com/a", "https://example.com/b")])
        result, _ = self.cycle(box, dry_run=True)
        self.assertEqual(result, {"ana": "preview"})
        self.assertTrue((self.root / "output/preview/ana.epub").is_file())
        self.assertEqual(box.folders, {})
        self.assertFalse((self.state / "relay.sqlite3").exists())
        self.assertFalse((self.state / "articles").exists())
        self.assertEqual(self.sent, [])

    def test_send_now_ignores_the_threshold_and_days(self):
        config = settings_for(self.state, minimum=10, days=["mon"])
        box = FakeMailbox([self.submit("https://example.com/a")])
        with patch("einkrelayer.relay.run.date") as clock:
            clock.today.return_value = date(2026, 10, 25)  # a Sunday
            result, _ = self.cycle(box, config)
            self.assertEqual(result, {"ana": "skipped"})
            self.assertEqual(self.fetched, ["https://example.com/a"])  # fetched early, cached
            result, _ = self.cycle(box, config, send_now="ana")
        self.assertEqual(result, {"ana": "sent"})
        self.assertEqual(self.fetched, ["https://example.com/a"])

    def submit_files(self, *files, links=""):
        return self.signer.message("ana@example.org", links or "See attached.", files=files)

    def test_attached_documents_are_queued_sent_then_deleted(self):
        box = FakeMailbox(
            [
                self.submit_files(
                    ("notes.md", b"# Notes\n\nHarbour plan notes.", "text/markdown"),
                    ("photo.jpg", b"\xff\xd8\xff", "image/jpeg"),
                    links="https://example.com/a",
                )
            ]
        )
        result, log = self.cycle(box)
        self.assertEqual(result, {"ana": "sent"})
        self.assertIn("1 link(s) and 1 attached document(s)", log)
        self.assertEqual(len(box.deleted), 1)  # deleted, not filed, so the mailbox stays clear
        self.assertEqual(box.folders, {})
        self.assertTrue(any(path.endswith("/notes.md") for path in self.fetched))
        self.assertFalse(any("photo.jpg" in path for path in self.fetched))
        attachments = self.state / "attachments"
        self.assertEqual(list(attachments.iterdir()) if attachments.exists() else [], [])
        self.assertEqual(list((self.state / "articles").glob("*.json")), [])

    def test_dry_run_keeps_attachments_out_of_state(self):
        box = FakeMailbox(
            [self.submit_files(("a.txt", b"one", "text/plain"), ("b.txt", b"two", "text/plain"))]
        )
        result, _ = self.cycle(box, dry_run=True)
        self.assertEqual(result, {"ana": "preview"})
        self.assertFalse((self.state / "attachments").exists())
        self.assertEqual(len(box.messages), 1)  # a dry run changes nothing in the mailbox

    def test_dry_run_reads_attachments_queued_by_earlier_runs(self):
        box = FakeMailbox([self.submit_files(("notes.md", b"# Notes", "text/markdown"))])
        self.cycle(box, settings_for(self.state, minimum=5))
        shutil.rmtree(self.state / "articles")  # not cached, so the stored file must be read
        result, log = self.cycle(box, dry_run=True, send_now="ana")
        self.assertEqual(result, {"ana": "preview"})
        self.assertIn("got notes.md", log)
        self.assertNotIn("couldn't fetch", log)

    def test_attachment_message_seen_again_after_a_failed_delete_is_deleted(self):
        box = FakeMailbox([self.submit_files(("a.txt", b"one", "text/plain"))])
        box.fail_delete = True
        config = settings_for(self.state, minimum=5)
        with self.assertRaises(OSError):
            self.cycle(box, config)
        self.assertEqual(len(list((self.state / "attachments").iterdir())), 1)  # kept: still queued
        result, _ = self.cycle(box, config)
        self.assertEqual(result, {"ana": "waiting"})
        self.assertEqual(len(box.deleted), 1)
        self.assertEqual(len(self.store_queue()), 1)  # recorded once

    def store_queue(self):
        from einkrelayer.relay.store import Store

        with Store(self.state, dry_run=True) as store:
            return store.queue("ana")

    def test_oversized_digest_leaves_trailing_articles_queued(self):
        box = FakeMailbox([self.submit("https://example.com/a", "https://example.com/b")])
        real = run.smtp.compose
        calls = []

        def compose(settings, paths, **options):
            calls.append(1)
            if len(calls) == 1:
                raise ValueError("Encoded email exceeds the configured message size limit")
            return real(settings, paths, **options)

        with patch("einkrelayer.relay.run.smtp.compose", side_effect=compose):
            config = settings_for(self.state, minimum=1)
            result, _ = self.cycle(box, config)
        self.assertEqual(result, {"ana": "sent"})
        queued = tools.queue(config)
        self.assertEqual(len(queued), 1)
        self.assertIn("example.com/b", queued[0])

    def test_old_books_are_deleted_unless_their_delivery_is_uncertain(self):
        box = FakeMailbox([self.submit("https://example.com/a", "https://example.com/b")])
        self.cycle(box)
        folder = self.state / "deliveries"
        sent = sorted(folder.iterdir())
        self.assertEqual([path.suffixes for path in sent], [[".epub"], [".report", ".json"]])
        long_ago = time.time() - (store.CACHE_DAYS + 1) * 86400
        uncertain = folder / "ana.b-uncertain.epub"
        uncertain.write_bytes(b"book")
        report = folder / "ana.b-uncertain.report.json"
        report.write_text("{}")
        recent = folder / "ana-recent.epub"
        recent.write_bytes(b"book")
        for path in [*sent, uncertain, report]:
            os.utime(path, (long_ago, long_ago))
        with store.Store(self.state) as relay:
            delivery = relay.start_delivery("ana", uncertain, ["ana@kindle.com"], [], [], 4)
            relay.finish_delivery(delivery, "uncertain")
            relay.prune_books()
        self.assertEqual(sorted(folder.iterdir()), [recent, uncertain, report])

    def test_one_run_at_a_time(self):
        with store.run_lock(self.state):
            with self.assertRaises(store.Locked):
                self.cycle(FakeMailbox([]))

    def test_resend_reuses_the_stored_book(self):
        box = FakeMailbox([self.submit("https://example.com/a", "https://example.com/b")])
        self.cycle(box)
        config = settings_for(self.state)
        with patch("einkrelayer.relay.tools.smtp.transmit", side_effect=self.transmit):
            self.assertIn("ready to resend", tools.resend(config, 1))
            self.assertIn("sent", tools.resend(config, 1, send=True))
        self.assertEqual(len(self.sent), 2)
        subjects = [message["Subject"] for _, message in self.sent]
        self.assertEqual(subjects, [f"Digest — {date.today().isoformat()}"] * 2)
        with store.Store(self.state) as relay:
            self.assertEqual(len(relay.deliveries()), 2)


if __name__ == "__main__":
    unittest.main()
