import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from zipfile import ZIP_STORED, ZipFile

from einkrelayer.books.validation import validate
from einkrelayer.cli import main


def epub(path, *, duplicate_id=False, broken_link=False):
    manifest = '<item id="chapter" href="chapter.html" media-type="application/xhtml+xml"/>'
    if duplicate_id:
        manifest += manifest
    with ZipFile(path, "w", compression=ZIP_STORED) as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr(
            "META-INF/container.xml",
            '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="content.opf"/></rootfiles></container>',
        )
        archive.writestr(
            "content.opf",
            '<package xmlns="http://www.idpf.org/2007/opf"><manifest>'
            + manifest
            + '</manifest><spine><itemref idref="chapter"/></spine></package>',
        )
        archive.writestr(
            "chapter.html",
            '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
            + ('<a href="missing.xhtml">Missing</a>' if broken_link else '<p id="text">Text</p>')
            + "</body></html>",
        )


class CliValidationTests(unittest.TestCase):
    def test_send_help_is_forwarded_and_does_not_mutate_process_arguments(self):
        original = list(sys.argv)
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as result:
            main(["send", "--help"])
        self.assertEqual(result.exception.code, 0)
        self.assertIn("--all-recipients", output.getvalue())
        self.assertEqual(sys.argv, original)

    def test_validation_reports_success_and_invalid_archive_without_traceback(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "book.epub"
            epub(path)
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["validate", str(path)]), 0)
            self.assertIn('"valid": true', output.getvalue())
            path.write_bytes(b"not a ZIP archive")
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as result:
                main(["validate", str(path)])
            self.assertEqual(result.exception.code, 1)

    def test_duplicate_manifest_ids_and_html_extension_links_are_checked(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "book.epub"
            epub(path, duplicate_id=True)
            with self.assertRaisesRegex(ValueError, "duplicate IDs"):
                validate(path)
            epub(path, broken_link=True)
            with self.assertRaisesRegex(ValueError, "Missing target"):
                validate(path)

    def test_send_arguments_are_forwarded_without_global_state(self):
        original = list(sys.argv)
        with patch("einkrelayer.delivery.smtp.main") as send:
            self.assertEqual(main(["send", "--to", "1", "book.epub"]), 0)
        send.assert_called_once_with(["--to", "1", "book.epub"])
        self.assertEqual(sys.argv, original)
