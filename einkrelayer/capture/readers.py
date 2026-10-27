"""One bounded input path for remote documents and explicitly selected local files."""

import base64
import json
import mimetypes
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, unquote_to_bytes, urljoin, urlsplit

from einkrelayer.capture.html import extract
from einkrelayer.capture.http import atomic_write, digest, fetch, write_json
from einkrelayer.diagnostics import event, reason

MAX_INPUT_BYTES = 30_000_000
PLACEHOLDER_TITLES = {"PDF document", "Word document", "Untitled page", ""}
DOCUMENT_MEDIA = {
    "text/html",
    "application/xhtml+xml",
    "text/plain",
    "text/markdown",
    "text/x-markdown",
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/octet-stream",
}


def local_bytes(path, root):
    path = Path(path).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Local asset escapes the selected asset directory")
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise ValueError("Input exceeds byte limit")
    return path.read_bytes()


def read(source, *, source_url=None, asset_root=None, cache=Path("input/captured")):
    """Read a public URL or a user-supplied local document."""
    try:
        document = _read(source, source_url=source_url, asset_root=asset_root, cache=cache)
    except (ValueError, OSError) as error:
        event("direct", source, "failed", reason(error))
        raise
    event("direct", source, "success")
    return document


def loader(base, remote, root, source_url, cache):
    """Fetch an asset a document refers to, within the request and size limits."""
    remote_assets = 0

    def load(href):
        nonlocal remote_assets
        if href.startswith("data:"):
            if len(href) > 40_000_000:
                raise ValueError("Inline asset exceeds byte limit")
            header, content = href.split(",", 1)
            return (
                base64.b64decode(content, validate=True)
                if ";base64" in header
                else unquote_to_bytes(content)
            )
        absolute = urljoin(base, href.replace(" ", "%20"))
        parsed = urlsplit(absolute)
        if parsed.scheme in {"http", "https"}:
            remote_assets += 1
            if remote_assets > 100:
                raise ValueError("Document exceeds remote asset request limit")
            asset_key = digest(absolute.encode())
            metadata = Path(cache) / "assets" / f"{asset_key}.json" if cache else None
            if metadata is not None and metadata.exists():
                entry = json.loads(metadata.read_text())
                asset_path = metadata.parent / entry["sha256"]
                if asset_path.exists() and digest(asset_path.read_bytes()) == entry["sha256"]:
                    return asset_path.read_bytes()
            result = fetch(absolute).data
            if metadata is not None:
                sha256 = digest(result)
                atomic_write(metadata.parent / sha256, result)
                write_json(metadata, {"url": absolute, "sha256": sha256})
            return result
        if parsed.scheme == "file" and not remote and not source_url and not parsed.netloc:
            return local_bytes(Path(unquote(parsed.path)), root)
        raise ValueError("Unsupported or unsafe asset URL")

    return load


def _read(source, *, source_url=None, asset_root=None, cache=Path("input/captured")):
    value = str(source)
    remote = urlsplit(value).scheme in {"http", "https"}
    charset = None
    if remote:
        response = fetch(value, media_types=DOCUMENT_MEDIA)
        data, media, charset = response.data, response.content_type, response.charset
        base = response.final_url
        suffix = Path(urlsplit(base).path).suffix.lower()
    else:
        path = Path(source).resolve()
        root = Path(asset_root).resolve() if asset_root else path.parent
        data = local_bytes(path, root)
        media = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        suffix = path.suffix.lower()
        base = source_url or path.as_uri()
    if cache is not None:
        extension = (
            suffix
            if suffix in {".html", ".htm", ".xhtml", ".pdf", ".txt", ".md", ".docx"}
            else ".bin"
        )
        atomic_write(Path(cache) / f"{digest(data)}{extension}", data)

    load = loader(base, remote, root if not remote else None, source_url, cache)

    if suffix == ".pdf" or media == "application/pdf" or data.startswith(b"%PDF-"):
        from einkrelayer.capture.documents import parse

        document = parse("pdf", data, base)
    elif suffix == ".docx" or media.endswith("wordprocessingml.document") or data.startswith(b"PK"):
        from einkrelayer.capture.documents import parse

        document = parse("docx", data, base)
    elif suffix in {".md", ".markdown"} or media in {"text/markdown", "text/x-markdown"}:
        from markdown_it import MarkdownIt

        rendered = MarkdownIt("commonmark").render(data.decode(charset or "utf-8-sig"))
        document = extract(rendered.encode(), base, load)
    elif media == "text/plain" or suffix == ".txt":
        from html import escape

        text = data.decode(charset or "utf-8-sig")
        document = extract(f"<body><pre>{escape(text)}</pre></body>".encode(), base, load)
        document.html = document.html.replace("<pre>", '<pre class="plain-text">', 1)
    elif media in {"text/html", "application/xhtml+xml"} or suffix in {".html", ".htm", ".xhtml"}:
        document = extract(data, base, load, charset=charset)
    else:
        raise ValueError("Unsupported document format")
    if document.title in PLACEHOLDER_TITLES:  # no title of its own: name it after its file
        stem = Path(unquote(urlsplit(base).path)).stem.replace("_", " ").replace("-", " ").strip()
        if stem:
            document.title = stem[0].upper() + stem[1:]
    # The submitted URL remains the identity; redirect locations are provenance.
    if remote:
        document.source = value
        if base != value:
            document.aliases.append(base)
    document.input_sha256 = digest(data)
    if not document.captured:
        document.captured = datetime.now(UTC).isoformat()
    return document
