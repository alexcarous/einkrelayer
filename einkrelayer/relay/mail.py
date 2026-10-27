"""Read submitted mail: sender, DKIM alignment, and the links it carries."""

import email
import email.policy
import hashlib
import json
import logging
import re
from email.utils import getaddresses
from urllib.parse import urlsplit, urlunsplit

import dkim
import dns.exception
import dns.rdatatype
import dns.resolver
from bs4 import BeautifulSoup

from einkrelayer.capture.network import normalized_url
from einkrelayer.diagnostics import clean, event

# The DKIM library logs each failed check; the relay writes its own log line instead.
QUIET = logging.getLogger("einkrelayer.dkim")
QUIET.addHandler(logging.NullHandler())
QUIET.propagate = False

MAX_MESSAGE_BYTES = 40_000_000  # room for attachments, which email encoding makes a third larger
MAX_ATTACHMENT_BYTES = 30_000_000
MAX_ATTACHMENTS = 20
DOCUMENT_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".html": "text/html",
    ".htm": "text/html",
}
MAX_LINKS = 50
# Printable ASCII only, so characters such as U+FFFC from share sheets end a URL.
URL = re.compile(r"https?://[!#-;=?-~]+", re.I)
TRAILING = ".,;:!?]}>'\""


def safe_url(url):
    """A URL for logs: query strings and fragments can carry access tokens."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if parts.port:
            host += f":{parts.port}"
        hidden = "?…" if parts.query else ""
        return urlunsplit((parts.scheme, host, parts.path, "", "")) + hidden
    except ValueError:
        return "[invalid URL]"


def redact(text):
    return clean(URL.sub(lambda m: safe_url(m.group(0)), text))


def parse(raw):
    return email.message_from_bytes(raw, policy=email.policy.default)


def message_key(raw, message, *, authenticated=True):
    """Bind identity to content, ignoring transport headers on authenticated redeliveries.

    Rejected mail uses its raw bytes so it can never reserve an accepted message's key.
    Callers must authenticate even a recognized accepted message before filing it.
    """
    if not authenticated:
        return hashlib.sha256(b"rejected\0" + raw).hexdigest()
    headers = [
        (name, [str(value) for value in message.get_all(name, [])])
        for name in (
            "From",
            "To",
            "Subject",
            "Message-ID",
            "Content-Type",
            "Content-Transfer-Encoding",
            "MIME-Version",
        )
    ]
    body = re.split(rb"\r?\n\r?\n", raw, maxsplit=1)[-1].replace(b"\r\n", b"\n").rstrip(b"\n")
    return hashlib.sha256(json.dumps(headers).encode() + b"\0" + body).hexdigest()


def sender(message):
    """The single From address, or "" when there is none or more than one."""
    headers = message.get_all("From") or []
    addresses = getaddresses([str(h) for h in headers])
    if len(headers) != 1 or len(addresses) != 1 or "@" not in addresses[0][1]:
        return ""
    return addresses[0][1].lower()


def recipient(message):
    """Require one explicit To recipient; never infer users from transport headers."""
    headers = message.get_all("To") or []
    addresses = getaddresses([str(h) for h in headers])
    if (
        len(headers) != 1
        or len(addresses) != 1
        or message.get_all("Cc")
        or message.get_all("Bcc")
        or getattr(headers[0], "defects", ())
    ):
        return ""
    return addresses[0][1].lower()


class LookupUnavailable(Exception):
    """A DKIM key could not be looked up just now; the check should be tried again later."""


def txt_record(name, timeout=5):
    """A DNS TXT record, or None when the name has none. Raises LookupUnavailable when DNS
    times out or fails, which a later try may not."""
    try:
        answer = dns.resolver.resolve(
            name.decode("ascii"), "TXT", raise_on_no_answer=False, lifetime=timeout
        )
    except (UnicodeDecodeError, dns.resolver.NXDOMAIN):
        return None
    except dns.exception.DNSException as error:  # a timeout, failing servers, no resolver
        raise LookupUnavailable(type(error).__name__) from error
    for record in answer.response.answer:
        if record.rdtype == dns.rdatatype.TXT:
            return b"".join(next(iter(record)).strings)
    return None


def dkim_aligned(raw, address, dnsfunc=None):
    """True when aligned DKIM covers the body, submission subject, and message identity.
    Raises LookupUnavailable when no signature passed and a key lookup failed for now."""
    domain = address.rpartition("@")[2].lower()
    if not domain:
        return False
    lookup = dnsfunc or txt_record
    unavailable = []

    def guarded(name, timeout=5):
        try:
            return lookup(name, timeout=timeout)
        except (LookupUnavailable, dkim.DnsTimeoutError) as error:
            unavailable.append(error)
            raise dkim.DnsTimeoutError(str(error)) from error

    verifier = dkim.DKIM(raw, logger=QUIET)
    signatures = [v for k, v in verifier.headers if k.lower() == b"dkim-signature"]
    for index, value in enumerate(signatures):
        try:
            tags = dkim.util.parse_tag_value(value)
        except dkim.util.InvalidTagValueList:
            continue
        signer = tags.get(b"d", b"").decode("ascii", "replace").lower()
        # A body-length limit (l=) lets text be appended after signing.
        if b"l" in tags or not (domain == signer or domain.endswith("." + signer)):
            continue
        signed_headers = [name.strip().lower() for name in tags.get(b"h", b"").split(b":")]
        if b"to" not in signed_headers:
            continue
        # DKIM selects repeated headers from the bottom. An extra unsigned Subject
        # above the signed one could otherwise inject links into an authenticated mail.
        if any(
            sum(key.lower() == name for key, _ in verifier.headers) > signed_headers.count(name)
            for name in (b"to", b"subject", b"message-id")
        ):
            continue
        try:
            if verifier.verify(idx=index, dnsfunc=guarded):
                return True
        except dkim.DKIMException:
            continue
    if unavailable:
        raise LookupUnavailable(str(unavailable[0]))
    return False


def _trim(url):
    url = url.rstrip(TRAILING)
    while url.endswith(")") and url.count(")") > url.count("("):
        url = url[:-1].rstrip(TRAILING)
    return url


def command(message):
    """Explicit subject commands only; replies and ordinary subjects remain digests."""
    subject = str(message.get("Subject", "")).strip()
    if subject.casefold() == "send now":
        return "send_now", ""
    if subject.casefold() == "book":
        return "book", ""
    if subject[:5].casefold() == "book:":
        title = subject[5:].strip()
        if not title or len(title) > 200 or any(not c.isprintable() for c in title):
            raise ValueError(
                "Book title must be 1-200 printable characters; use 'book' for automatic naming"
            )
        return "book", title
    return "digest", ""


def links(message, *, strict=False):
    """Submitted links in order: subject and plain text first, else HTML anchors."""
    text = str(message.get("Subject", "")) + "\n"
    plain = message.get_body(preferencelist=("plain",))
    if plain is not None:
        text += plain.get_content()
    found = [_trim(u) for u in URL.findall(text)]
    if not found:
        part = message.get_body(preferencelist=("html",))
        if part is not None:
            soup = BeautifulSoup(part.get_content(), "html.parser")
            found = [a["href"].strip() for a in soup.find_all("a", href=True)]
            found = [u for u in found if u.lower().startswith(("http://", "https://"))]
    result = []
    for url in found:
        try:
            url = normalized_url(url)
        except ValueError:
            continue
        if url not in result:
            result.append(url)
    if strict and len(result) > MAX_LINKS:
        raise ValueError("Book email exceeds the 50-link limit")
    return result[:MAX_LINKS]


def safe_filename(name):
    """A plain file name: no folders, control characters or leading dots."""
    name = re.split(r"[/\\]", (name or "").strip())[-1]
    name = re.sub(r"[\x00-\x1f:*?\"<>|]+", "_", name).strip(" .")
    return name[-120:] or "document"


def attachments(message):
    """Attached documents of a supported type, as (file name, bytes), in order.
    The message text, inline images, signatures and unsupported files are skipped."""
    found = []
    for part in message.walk():
        disposition = part.get_content_disposition()
        # Some mail apps mark the message text "inline"; an inline file always has a name.
        if part.is_multipart() or disposition not in {"attachment", "inline"}:
            continue
        if disposition == "inline" and not part.get_filename():
            continue
        name = safe_filename(part.get_filename() or "")
        suffix = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if suffix not in DOCUMENT_TYPES and part.get_content_type() in DOCUMENT_TYPES.values():
            suffix = next(k for k, v in DOCUMENT_TYPES.items() if v == part.get_content_type())
            name += suffix
        if suffix not in DOCUMENT_TYPES:
            event("attachment", "", "skipped", "Unsupported attachment type")
            continue
        data = part.get_payload(decode=True) or b""
        if data and len(data) <= MAX_ATTACHMENT_BYTES:
            found.append((name, data))
        else:
            event("attachment", "", "skipped", "Empty attachment or exceeds 30 MB")
    if len(found) > MAX_ATTACHMENTS:
        event("attachment", "", "skipped", "Attachment count exceeds 20")
    return found[:MAX_ATTACHMENTS]
