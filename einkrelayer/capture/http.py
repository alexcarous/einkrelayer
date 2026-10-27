"""Bounded, retryable HTTP fetching and atomic writes for source capture."""

import gzip
import hashlib
import io
import json
import os
import tempfile
import time
from dataclasses import dataclass
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup
from tinycss2 import parse_component_value_list

from einkrelayer.capture.access import AccessRestricted, check_page
from einkrelayer.capture.network import normalized_url, urlopen
from einkrelayer.diagnostics import event, reason


def digest(data):
    return hashlib.sha256(data).hexdigest()


# Fragment links and embedded raster images need no network request.
SELF_CONTAINED = ("#", "data:image/png;", "data:image/jpeg;", "data:image/gif;", "data:image/webp;")


def verify_svg(data):
    root = ET.fromstring(data)
    if root.tag.rsplit("}", 1)[-1] != "svg":
        raise ValueError("Invalid SVG document")
    for node in root.iter():
        kind = node.tag.rsplit("}", 1)[-1]
        if kind in {"script", "foreignObject"}:
            raise ValueError("Unsupported active SVG content")
        for attribute, value in node.attrib.items():
            if attribute.rsplit("}", 1)[-1] in {"href", "src"} and not value.startswith(
                SELF_CONTAINED
            ):
                raise ValueError("SVG has an external resource dependency")
        # CSS tokenization recognizes case variations and escaped URL/import
        # spellings, including presentation attributes such as fill/filter.
        for value in [*node.attrib.values(), node.text or "" if kind == "style" else ""]:
            verify_svg_css(parse_component_value_list(value))


def verify_svg_css(tokens):
    for token in tokens:
        if token.type == "at-keyword" and token.lower_value == "import":
            raise ValueError("SVG styling has an external resource dependency")
        if token.type == "url" and not token.value.startswith("#"):
            raise ValueError("SVG styling has an external resource dependency")
        if token.type == "function":
            if token.lower_name == "url":
                values = [t for t in token.arguments if t.type not in {"whitespace", "comment"}]
                if len(values) != 1 or not getattr(values[0], "value", "").startswith("#"):
                    raise ValueError("SVG styling has an external resource dependency")
            verify_svg_css(token.arguments)
        if hasattr(token, "content"):
            verify_svg_css(token.content)


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".part", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def write_json(path, value):
    atomic_write(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())


@dataclass
class Response:
    data: bytes
    final_url: str
    content_type: str
    charset: str | None


def fetch(url, *, html=False, timeout=30, attempts=3, media_types=None):
    url = normalized_url(url)
    limit = 20_000_000 if html else (100_000_000 if media_types is not None else 30_000_000)
    for attempt in range(attempts):
        try:
            request = Request(
                url,
                headers={
                    "User-Agent": "einkrelayer/0.1",
                    "Accept-Encoding": "identity",
                    "Accept": ",".join(media_types)
                    if media_types
                    else ("text/html,application/xhtml+xml" if html else "image/*,application/pdf"),
                },
            )
            with urlopen(request, timeout=timeout) as response:
                final_url = normalized_url(response.geturl())
                if response.status != 200:
                    raise ValueError(f"Unexpected HTTP status: {response.status}")
                media = response.headers.get_content_type()
                if html and media not in {"text/html", "application/xhtml+xml"}:
                    raise ValueError(f"Expected HTML; received {media}")
                if media_types is not None and media not in media_types:
                    raise ValueError(f"Unsupported response content type: {media}")
                if (
                    not html
                    and media_types is None
                    and not (media.startswith("image/") or media == "application/pdf")
                ):
                    raise ValueError(f"Expected an image or PDF; received {media}")
                declared = response.headers.get("Content-Length")
                length = int(declared) if declared and declared.isdigit() else None
                if length is not None and length > limit:
                    raise ValueError("Response exceeds capture size limit")
                data = response.read(limit + 1)
                if len(data) > limit:
                    raise ValueError("Response exceeds capture size limit")
                if length is not None and length != len(data):
                    raise HTTPException("Truncated HTTP response")
                encoding = response.headers.get("Content-Encoding", "identity").lower()
                if encoding == "gzip":
                    with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
                        data = stream.read(limit + 1)
                    if len(data) > limit:
                        raise ValueError("Decompressed response exceeds capture size limit")
                elif encoding != "identity":
                    raise ValueError(f"Unsupported content encoding: {encoding}")
                if not data:
                    raise ValueError("Empty response")
                if media in {"text/html", "application/xhtml+xml"}:
                    check_page(BeautifulSoup(data, "lxml"), final_url)
                return Response(data, final_url, media, response.headers.get_content_charset())
        except HTTPError as error:
            if error.code in {401, 403, 407, 451}:
                event("http", url, "restricted", reason(error))
                raise AccessRestricted(f"HTTP {error.code}") from error
            retryable = error.code in {408, 429, 500, 502, 503, 504}
            delay = error.headers.get("Retry-After", "") if error.headers else ""
            if not retryable or attempt + 1 >= attempts:
                event("http", url, "failed", reason(error))
                raise
            event("http", url, "retry", f"{reason(error)}; attempt {attempt + 2}/{attempts}")
            time.sleep(min(float(delay), 15) if delay.isdigit() else 2**attempt)
        except (URLError, TimeoutError, HTTPException, ConnectionError) as error:
            if attempt + 1 >= attempts:
                event("http", url, "failed", reason(error))
                raise
            event("http", url, "retry", f"{reason(error)}; attempt {attempt + 2}/{attempts}")
            time.sleep(2**attempt)
    raise ValueError("No fetch attempts configured")
