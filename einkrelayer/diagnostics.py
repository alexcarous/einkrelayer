"""Bounded, single-line diagnostics without URL credentials or query tokens."""

import logging
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit

LOGGER = logging.getLogger("einkrelayer.capture")
URL = re.compile(r"https?://[^\s<>\"']+", re.I)


def safe_url(value):
    try:
        parts = urlsplit(str(value))
        host = parts.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        if parts.port:
            host += f":{parts.port}"
        return urlunsplit((parts.scheme, host, parts.path, "", "")) + ("?…" if parts.query else "")
    except ValueError:
        return "[invalid URL]"


def clean(value):
    text = URL.sub(lambda match: safe_url(match[0]), str(value))
    return "".join(c if c.isprintable() else " " for c in text)[:500]


def reason(error):
    if isinstance(error, HTTPError):
        return f"HTTP {error.code}"
    if isinstance(error, (URLError, OSError)):
        return f"Network or file error ({type(error).__name__})"
    if isinstance(error, ValueError):
        return clean(error) or "Invalid content"
    return type(error).__name__


def event(stage, source, outcome, detail="", *, level=logging.INFO):
    LOGGER.log(
        level,
        "%s | %s | %s | %s",
        clean(stage),
        clean(safe_url(source)),
        clean(outcome),
        clean(detail),
    )


def configure():
    # Configure our logger only; leave application/root logging alone.
    if not LOGGER.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S"))
        LOGGER.addHandler(handler)
        LOGGER.setLevel(logging.INFO)
        LOGGER.propagate = False
