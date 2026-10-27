"""Capture diagnostics explain fallback choices without leaking request secrets."""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from einkrelayer import diagnostics

SOURCE = "https://example.org/long-story?token=PRIVATE#FRAGMENT"


class DiagnosticsTests(unittest.TestCase):
    def test_cli_errors_redact_urls_and_control_characters(self):
        from einkrelayer.cli import main

        output = io.StringIO()
        with (
            patch(
                "einkrelayer.books.validation.validate",
                side_effect=ValueError(
                    "Bad https://user:PASS@example.org/page?key=TOKEN#SECRET\nextra"
                ),
            ),
            redirect_stderr(output),
            self.assertRaises(SystemExit),
        ):
            main(["validate", "unused.epub"])
        text = output.getvalue()
        for secret in ("PASS", "TOKEN", "SECRET"):
            self.assertNotIn(secret, text)
        self.assertEqual(len(text.splitlines()), 1)

    def test_redaction_and_single_line(self):
        with self.assertLogs(diagnostics.LOGGER, level="INFO") as logs:
            diagnostics.event(
                "rss\nFAKE",
                "https://user:PASS@example.org/feed?key=TOKEN#SECRET",
                "failed",
                "Bad URL https://example.org/a?secret=OTHER\nINJECT",
            )
            diagnostics.event(
                "http",
                SOURCE,
                "failed",
                diagnostics.reason(HTTPError(SOURCE, 403, "PASSWORD", {}, None)),
            )
        text = " ".join(logs.output)
        for secret in ("PASS", "TOKEN", "SECRET", "OTHER", "PRIVATE", "FRAGMENT"):
            self.assertNotIn(secret, text)
        self.assertNotIn("\n", text)
        self.assertIn("HTTP 403", text)

    def test_cli_logs_on_stderr_not_json_stdout(self):
        logger = diagnostics.LOGGER
        saved = logger.handlers[:], logger.level, logger.propagate
        logger.handlers = []
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                diagnostics.configure()
                diagnostics.event("direct", SOURCE, "success")
                print('{"valid": true}')
            self.assertEqual(out.getvalue().strip(), '{"valid": true}')
            self.assertIn("direct |", err.getvalue())
        finally:
            for handler in logger.handlers:
                handler.close()
            logger.handlers, logger.level, logger.propagate = saved

    def test_build_json_excludes_external_checker_output(self):
        from einkrelayer.cli import main

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "reading.txt"
            source.write_text("A readable document. " * 40)
            destination = Path(directory) / "book.epub"
            output = io.StringIO()
            with (
                patch(
                    "einkrelayer.books.compose.epubcheck",
                    return_value=[sys.executable, "-c", "print('checker diagnostic')"],
                ),
                patch("dotenv.dotenv_values", return_value={}),
                redirect_stdout(output),
                self.assertLogs(diagnostics.LOGGER, level="INFO") as logs,
            ):
                self.assertEqual(main(["build", str(source), "--output", str(destination)]), 0)
            self.assertEqual(json.loads(output.getvalue())["epub"], str(destination))
            self.assertIn("epubcheck", " ".join(logs.output))
            self.assertIn("checker diagnostic", " ".join(logs.output))
