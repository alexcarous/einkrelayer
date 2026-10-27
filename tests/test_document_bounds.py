import io
import struct
import subprocess
import unittest
from unittest.mock import patch
from zipfile import ZipFile

import pymupdf

from einkrelayer.capture.documents import docx, parse, pdf
from einkrelayer.capture.html import image_bytes


class DocumentBoundsTests(unittest.TestCase):
    def test_container_declared_expansion_is_rejected_before_docx_parsing(self):
        buffer = io.BytesIO()
        with ZipFile(buffer, "w") as archive:
            archive.writestr("word/document.xml", "<document/>")
        data = bytearray(buffer.getvalue())
        position = data.index(b"PK\x01\x02")
        struct.pack_into("<I", data, position + 24, 256_000_000)
        with self.assertRaisesRegex(ValueError, "resource limits"):
            docx(bytes(data), "fixture")

    def test_pdf_page_bound_and_svg_pixel_bound(self):
        with pymupdf.open() as book:
            for _ in range(301):
                book.new_page()
            data = book.tobytes()
        with self.assertRaisesRegex(ValueError, "page limit"):
            pdf(data, "fixture")
        with self.assertRaisesRegex(ValueError, "pixel limit"):
            image_bytes(b'<svg xmlns="http://www.w3.org/2000/svg" width="22000" height="22000"/>')

    def test_native_parser_timeout_is_visible_and_does_not_publish_partial_output(self):
        with (
            patch(
                "einkrelayer.capture.documents.subprocess.run",
                side_effect=subprocess.TimeoutExpired("parser", 60),
            ),
            self.assertRaisesRegex(ValueError, "60 seconds"),
        ):
            parse("pdf", b"%PDF-1.7\nfixture", "fixture")
