import io
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from bs4 import BeautifulSoup
from PIL import Image

from einkrelayer.capture import http as capture
from einkrelayer.capture.access import AccessRestricted
from einkrelayer.capture.assets import image_source
from einkrelayer.capture.documents import pdf
from einkrelayer.capture.html import image_bytes


def response(
    data=b"<main>Content</main>", media="text/html", length=None, final="https://example.com/guide"
):
    result = MagicMock()
    result.__enter__.return_value = result
    result.status = 200
    result.geturl.return_value = final
    result.headers = Message()
    result.headers["Content-Type"] = media
    result.headers["Content-Length"] = str(len(data) if length is None else length)
    result.read.return_value = data
    return result


class FetchTests(unittest.TestCase):
    def test_linked_documents_allow_100mb_but_assets_keep_30mb_limit(self):
        for media_types, size, accepted in (
            ({"application/pdf"}, 100_000_000, True),
            ({"application/pdf"}, 100_000_001, False),
            (None, 30_000_001, False),
        ):
            case = response(media="application/pdf", length=size)
            case.read.return_value = MagicMock()
            case.read.return_value.__len__.return_value = size
            with patch("einkrelayer.capture.http.urlopen", return_value=case):
                if accepted:
                    capture.fetch("https://example.com/file.pdf", media_types=media_types)
                    case.read.assert_called_once_with(100_000_001)
                else:
                    with self.assertRaisesRegex(ValueError, "size limit"):
                        capture.fetch("https://example.com/file.pdf", media_types=media_types)

    def test_retries_transient_errors_but_not_access_denials(self):
        error = HTTPError("https://example.com/guide", 503, "busy", None, None)
        with (
            patch("einkrelayer.capture.http.urlopen", side_effect=[error, response()]) as opener,
            patch("einkrelayer.capture.http.time.sleep"),
        ):
            self.assertEqual(
                capture.fetch("https://example.com/guide", html=True).content_type,
                "text/html",
            )
            self.assertEqual(opener.call_count, 2)
        with patch(
            "einkrelayer.capture.http.urlopen",
            side_effect=HTTPError("https://example.com/guide", 403, "denied", None, None),
        ) as opener:
            with self.assertRaises(AccessRestricted):
                capture.fetch("https://example.com/guide", html=True)
            self.assertEqual(opener.call_count, 1)

    def test_rejects_truncation_downgrade_wrong_type_and_oversize(self):
        cases = [
            response(length=100),
            response(media="application/pdf"),
            response(length=21_000_000),
        ]
        for case in cases:
            with (
                self.subTest(),
                patch("einkrelayer.capture.http.urlopen", return_value=case),
                patch("einkrelayer.capture.http.time.sleep"),
            ):
                with self.assertRaises((ValueError, capture.HTTPException)):
                    capture.fetch("https://example.com/guide", html=True, attempts=1)

    def test_rejects_non_web_urls_and_embedded_credentials(self):
        for url in (
            "file:///etc/passwd",
            "https://user:secret@example.com/guide",
            "javascript:alert(1)",
        ):
            with self.assertRaises(ValueError):
                capture.normalized_url(url)

    def test_atomic_write_failure_preserves_previous_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "page.html"
            path.write_bytes(b"previous")
            with patch("einkrelayer.capture.http.os.replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    capture.atomic_write(path, b"new")
            self.assertEqual(path.read_bytes(), b"previous")
            self.assertEqual(list(path.parent.glob("*.part")), [])

    def test_picture_lazy_and_malformed_srcsets(self):
        soup = BeautifulSoup(
            '<picture><source type="image/webp" srcset="small.webp 400w, large.webp 1200w"/><img src="fallback.jpg"/></picture>',
            "html.parser",
        )
        self.assertEqual(image_source(soup.img), "large.webp")
        soup = BeautifulSoup(
            '<img data-src="real.jpg" src="placeholder.jpg" srcset="bad.jpg auto"/>', "html.parser"
        )
        self.assertEqual(image_source(soup.img), "real.jpg")

    def test_assets_are_validated_instead_of_trusting_content_type(self):
        with self.assertRaises(Exception):
            image_bytes(b"<html>Access denied</html>")
        with self.assertRaisesRegex(ValueError, "PDF"):
            pdf(b"not a PDF", "https://example.com/file")
        buffer = io.BytesIO()
        Image.new("RGB", (10, 10), "white").save(buffer, "PNG")
        image_bytes(buffer.getvalue())

    def test_svg_cannot_fetch_external_assets_or_run_active_content(self):
        for svg in (
            '<svg xmlns="http://www.w3.org/2000/svg"><image href="https://example.com/pixel.png"/></svg>',
            '<svg xmlns="http://www.w3.org/2000/svg"><script>bad()</script></svg>',
            '<svg xmlns="http://www.w3.org/2000/svg"><path fill="URL(https://example.com/fill)"/></svg>',
            '<svg xmlns="http://www.w3.org/2000/svg"><style>@IMPORT "https://example.com/style";</style></svg>',
            '<svg xmlns="http://www.w3.org/2000/svg"><style>path{fill:u\\72l("https://example.com/fill")}</style></svg>',
        ):
            with self.assertRaises(ValueError):
                capture.verify_svg(svg.encode())


class PhotoFormatTests(unittest.TestCase):
    def test_web_photos_become_jpeg_and_transparent_images_stay_png(self):
        from einkrelayer.capture.html import image_bytes as convert

        photo = io.BytesIO()
        Image.effect_noise((64, 64), 40).convert("RGB").save(photo, "WEBP")
        self.assertEqual(convert(photo.getvalue())[1], "jpg")
        clear = io.BytesIO()
        Image.new("RGBA", (8, 8), (0, 0, 0, 0)).save(clear, "WEBP", lossless=True)
        self.assertEqual(convert(clear.getvalue())[1], "png")
        drawing = io.BytesIO()
        Image.new("RGB", (8, 8), "white").save(drawing, "PNG")
        self.assertEqual(convert(drawing.getvalue())[1], "png")


if __name__ == "__main__":
    unittest.main()
