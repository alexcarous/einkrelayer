import io
import os
import smtplib
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from bs4 import BeautifulSoup

from einkrelayer.books import answers, xhtml
from einkrelayer.books.tables import reflow_wide_tables
from einkrelayer.delivery import smtp as delivery


class ContentTests(unittest.TestCase):
    def test_xhtml_preserves_text_before_first_element(self):
        soup = BeautifulSoup(
            "<body>Leading prose.<p>Paragraph.</p>Trailing prose.</body>", "html.parser"
        )
        content = xhtml.document("Title", xhtml.to_xml(soup.body))
        root = xhtml.ET.fromstring(content)
        body = root.find("{" + xhtml.XHTML + "}body")
        self.assertEqual("".join(body.itertext()), "Leading prose.Paragraph.Trailing prose.")

    def test_separate_answer_documents_keep_round_trip_links(self):
        soup = BeautifulSoup(
            '<body><h1>Lesson</h1><section id="question-q"><h3>Question</h3><a href="#answer-q">Answer</a></section><section class="answer-key"><h2 id="answers">Practice answer key</h2><section id="answer-q"><p>Explanation</p><a href="#question-q">Back</a></section></section></body>',
            "html.parser",
        )
        documents, keys = answers.separate_answer_keys(
            {"chapter.xhtml": xhtml.document("Lesson", xhtml.to_xml(soup.body))}
        )
        self.assertEqual(keys["chapter.xhtml"], "chapter-answers.xhtml")
        self.assertIn(b'href="chapter-answers.xhtml#answer-q"', documents["chapter.xhtml"])
        self.assertNotIn(b'id="answer-q"', documents["chapter.xhtml"])
        self.assertIn(b'href="chapter.xhtml#question-q"', documents["chapter-answers.xhtml"])
        self.assertIn(b'id="answer-q"', documents["chapter-answers.xhtml"])

    def test_irregular_tables_are_not_silently_truncated(self):
        soup = BeautifulSoup(
            "<body><table><tr><td>First</td><td>Value</td></tr><tr><td>Footnote preserved</td></tr></table></body>",
            "html.parser",
        )
        reflow_wide_tables(soup)
        self.assertIsNotNone(soup.table)
        self.assertIn("Footnote preserved", soup.get_text())

    def test_xhtml_preserves_text_around_children_and_escapes(self):
        soup = BeautifulSoup(
            "<body><p>A &amp; B <strong>bold</strong> tail.</p></body>", "html.parser"
        )
        content = xhtml.document("Title", xhtml.to_xml(soup.body))
        root = xhtml.ET.fromstring(content)
        paragraph = root.find(".//{" + xhtml.XHTML + "}p")
        self.assertEqual("".join(paragraph.itertext()), "A & B bold tail.")


class EmailTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = self.root / ".env"
        self.env.write_text(
            'KINDLE_EMAIL=reader@kindle.com\nSMTP_FROM=sender@example.com\nSMTP_HOST=smtp.example.com\nSMTP_USERNAME=sender\nSMTP_PASSWORD="literal${PASSWORD}"\n'
        )
        self.environ = patch.dict(os.environ, {}, clear=True)
        self.environ.start()
        self.addCleanup(self.environ.stop)
        self.config = delivery.settings(self.env)
        self.book = self.root / "book.epub"
        with zipfile.ZipFile(self.book, "w") as archive:
            archive.writestr("mimetype", b"application/epub+zip")
            archive.writestr(
                "META-INF/container.xml",
                '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="content.opf"/></rootfiles></container>',
            )
            archive.writestr(
                "content.opf",
                '<package xmlns="http://www.idpf.org/2007/opf"><manifest><item id="chapter" href="chapter.xhtml"/></manifest><spine><itemref idref="chapter"/></spine></package>',
            )
            archive.writestr(
                "chapter.xhtml",
                '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Test</title></head><body><p>Test book</p></body></html>',
            )

    def test_mime_attachment_matches_file_and_password_is_literal(self):
        message = delivery.compose(self.config, [self.book])
        attachment = next(message.iter_attachments())
        self.assertEqual(attachment.get_content_type(), "application/epub+zip")
        self.assertEqual(attachment.get_payload(decode=True), self.book.read_bytes())
        self.assertEqual(self.config["password"], "literal${PASSWORD}")

    def test_reads_only_the_dotenv_file(self):
        with patch("einkrelayer.delivery.smtp.ROOT", self.root):
            self.assertEqual(delivery.default_env_file(), self.env)
            (self.root / "env").write_text(self.env.read_text())
            self.env.unlink()
            with (
                patch("sys.argv", ["einkrelayer", str(self.book)]),
                patch("einkrelayer.delivery.smtp.deliver") as deliver,
                redirect_stdout(io.StringIO()),
                patch("sys.stderr", new_callable=io.StringIO) as error,
            ):
                with self.assertRaises(SystemExit) as result:
                    delivery.main()
                self.assertEqual(result.exception.code, 1)
                self.assertIn("Create .env from .env.example", error.getvalue())
                deliver.assert_not_called()

    def smtp_client(self, client):
        smtp = client.return_value
        smtp.mail.return_value = (250, b"OK")
        smtp.rcpt.return_value = (250, b"OK")
        smtp.data.return_value = (250, b"OK")
        return smtp

    def test_starttls_precedes_login(self):
        with patch("einkrelayer.delivery.smtp.smtplib.SMTP") as client:
            smtp = self.smtp_client(client)
            delivery.deliver(self.config, delivery.compose(self.config, [self.book]))
            names = [call[0] for call in smtp.method_calls]
            self.assertLess(names.index("starttls"), names.index("login"))
            self.assertLess(names.index("login"), names.index("data"))

    def test_tls_failure_never_authenticates_or_sends(self):
        with patch("einkrelayer.delivery.smtp.smtplib.SMTP") as client:
            smtp = self.smtp_client(client)
            smtp.starttls.side_effect = smtplib.SMTPNotSupportedError("No TLS")
            with self.assertRaises(ValueError):
                delivery.deliver(self.config, MagicMock())
            smtp.login.assert_not_called()
            smtp.data.assert_not_called()

    def test_ssl_mode_uses_implicit_tls(self):
        self.config["mode"] = "ssl"
        self.config["port"] = 465
        with patch("einkrelayer.delivery.smtp.smtplib.SMTP_SSL") as client:
            smtp = self.smtp_client(client)
            delivery.deliver(self.config, MagicMock())
            client.assert_called_once()
            smtp.starttls.assert_not_called()

    def test_outcomes_tell_refused_failed_and_uncertain_apart(self):
        self.config["recipients"] = ["one@kindle.com", "two@kindle.com"]
        message = delivery.compose(self.config, [self.book])
        with patch("einkrelayer.delivery.smtp.smtplib.SMTP") as client:
            smtp = self.smtp_client(client)
            smtp.rcpt.side_effect = [(250, b"OK"), (550, b"No such user")]
            status, outcome, _ = delivery.transmit(self.config, message)
            self.assertEqual(status, "partial")
            self.assertEqual(outcome["two@kindle.com"], "refused (550)")
            smtp.rcpt.side_effect = None
            smtp.data.side_effect = smtplib.SMTPDataError(552, b"Too big")
            self.assertEqual(delivery.transmit(self.config, message)[0], "failed")
            smtp.data.side_effect = smtplib.SMTPServerDisconnected("gone")
            self.assertEqual(delivery.transmit(self.config, message)[0], "uncertain")
            smtp.rcpt.return_value = (550, b"No")
            smtp.data.reset_mock()
            self.assertEqual(delivery.transmit(self.config, message)[0], "failed")
            smtp.data.assert_not_called()

    def test_default_cli_does_not_connect(self):
        with (
            patch("einkrelayer.delivery.smtp.ROOT", self.root),
            patch("sys.argv", ["einkrelayer", str(self.book)]),
            patch("einkrelayer.delivery.smtp.deliver") as deliver,
            redirect_stdout(io.StringIO()),
        ):
            delivery.main()
            deliver.assert_not_called()

    def test_size_limit_uses_encoded_message_size(self):
        self.config["max_bytes"] = 10
        with self.assertRaisesRegex(ValueError, "size limit"):
            delivery.compose(self.config, [self.book])

    def test_rejects_zip_with_no_epub_package_before_sending(self):
        with zipfile.ZipFile(self.book, "w") as archive:
            archive.writestr("mimetype", b"application/epub+zip")
        with self.assertRaisesRegex(ValueError, "missing its container"):
            delivery.compose(self.config, [self.book])

    def test_multiple_recipients_require_selection(self):
        self.env.write_text(
            self.env.read_text().replace(
                "reader@kindle.com", "one@kindle.com, two@kindle.com,three@kindle.com"
            )
        )
        config = delivery.settings(self.env)
        with self.assertRaisesRegex(ValueError, "Multiple Kindle"):
            delivery.select_recipients(config)
        selected = delivery.select_recipients(config, "1,three@kindle.com,1")
        self.assertEqual(selected["recipients"], ["one@kindle.com", "three@kindle.com"])
        self.assertEqual(
            config["recipients"], ["one@kindle.com", "two@kindle.com", "three@kindle.com"]
        )
        self.assertEqual(
            delivery.select_recipients(config, all_recipients=True)["recipients"],
            config["recipients"],
        )
        with self.assertRaisesRegex(ValueError, "Unknown recipient"):
            delivery.select_recipients(config, "unconfigured@kindle.com")

    def test_only_selected_recipients_reach_smtp(self):
        config = {**self.config, "recipients": ["one@kindle.com", "two@kindle.com"]}
        selected = delivery.select_recipients(config, "2")
        message = delivery.compose(selected, [self.book])
        self.assertEqual(str(message["To"]), "two@kindle.com")
        with patch("einkrelayer.delivery.smtp.smtplib.SMTP") as client:
            smtp = self.smtp_client(client)
            delivery.deliver(selected, message)
            self.assertEqual([c.args[0] for c in smtp.rcpt.call_args_list], ["two@kindle.com"])

    def test_blank_address_in_list_is_rejected(self):
        with patch.dict(os.environ, {"KINDLE_EMAIL": "reader@kindle.com,"}):
            with self.assertRaisesRegex(ValueError, "plain email"):
                delivery.settings(self.env)

    def test_rejects_plaintext_security_and_header_injection(self):
        self.env.write_text(self.env.read_text() + "SMTP_SECURITY=none\n")
        with self.assertRaisesRegex(ValueError, "SMTP_SECURITY"):
            delivery.settings(self.env)
        with patch.dict(os.environ, {"KINDLE_EMAIL": "a@kindle.com\nBcc: b@example.com"}):
            with self.assertRaisesRegex(ValueError, "plain email"):
                delivery.settings(self.env)

    def test_valueless_setting_reports_missing_configuration(self):
        self.env.write_text("KINDLE_EMAIL\nSMTP_HOST\nSMTP_FROM\n")
        with self.assertRaisesRegex(ValueError, "Fill in KINDLE_EMAIL, SMTP_HOST, SMTP_FROM"):
            delivery.settings(self.env)

    def test_nonfinite_timeout_and_message_limits_are_rejected(self):
        for key in ("SMTP_TIMEOUT", "SMTP_MAX_MESSAGE_MB"):
            for value in ("nan", "inf"):
                with self.subTest(key=key, value=value), patch.dict(os.environ, {key: value}):
                    with self.assertRaises(ValueError):
                        delivery.settings(self.env)

    def test_relay_does_not_require_standalone_kindle_recipients(self):
        for value in ("", "not-an-address"):
            with patch.dict(os.environ, {"KINDLE_EMAIL": value}):
                config = delivery.settings(self.env, recipients=False)
                self.assertEqual(config["recipients"], ["sender@example.com"])


class EpubcheckLookupTests(unittest.TestCase):
    def test_installed_tool_comes_first_then_the_local_toolchain(self):
        from einkrelayer.books import validation

        with tempfile.TemporaryDirectory() as root:
            tools = Path(root)
            with (
                patch.object(validation, "TOOLCHAINS", tools),
                patch.object(validation.shutil, "which", return_value=None),
            ):
                self.assertIsNone(validation.epubcheck())
                jar = tools / "epubcheck-5.4.0/epubcheck.jar"
                java = tools / "java/bin/java"
                for path in (jar, java):
                    path.parent.mkdir(parents=True)
                    path.write_bytes(b"")
                self.assertEqual(
                    validation.epubcheck(), [str(java.resolve()), "-jar", str(jar.resolve())]
                )
            with patch.object(validation.shutil, "which", return_value="/usr/bin/epubcheck"):
                self.assertEqual(validation.epubcheck(), ["/usr/bin/epubcheck"])


if __name__ == "__main__":
    unittest.main()
