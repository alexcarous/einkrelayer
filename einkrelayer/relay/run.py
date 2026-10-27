"""One relay cycle: file new mail, fetch queued links, build due digests, send them."""

import json
import logging
import socket
import ssl
from datetime import UTC, date, datetime
from html import escape
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError

from einkrelayer.books.compose import compose
from einkrelayer.delivery import smtp
from einkrelayer.diagnostics import event
from einkrelayer.models import BookJob, Document
from einkrelayer.relay import mail
from einkrelayer.relay.config import DAYS
from einkrelayer.relay.store import ArticleCache, Attachments, Store

MAX_ARTICLES = 50
PERMANENT = (
    "Access restricted:",
    "Expected an HTTP(S) URL",
    "Refusing a non-public destination",
    "Unsupported document format",
    "Unsupported response content type",
    "Too large to email",
    "The attached document is no longer stored",
    "Input exceeds byte limit",
    "Book exceeds",
    "Contents list",
)


def log(text):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {mail.redact(text)}", flush=True)


def shown(entry):
    """How a queue entry appears in logs and pages: a link, or an attached file's name."""
    return entry.rpartition("/")[2] if Attachments.is_attachment(entry) else entry


def intake(config, store, mailbox, attachments, *, dry_run=False, dnsfunc=None):
    """Record each submission's links and attached documents before filing the message, so the
    inbox is not the queue. A message that brought attachments is deleted, not filed, so the
    mailbox doesn't fill up; the files are deleted once their digest is sent."""
    counts = {"messages": 0, "links": 0, "rejected": 0, "postponed": 0}
    rejected = mailbox.folder("Processed", "rejected")
    for uid in mailbox.uids():
        size = mailbox.size(uid)
        if size is None:
            log("couldn't check a message's size right now; leaving it in the inbox")
            counts["postponed"] += 1
            continue
        raw = None if size > mail.MAX_MESSAGE_BYTES else mailbox.fetch(uid)
        if size > mail.MAX_MESSAGE_BYTES or raw is not None and len(raw) > mail.MAX_MESSAGE_BYTES:
            log("rejected a message: too large")
            counts["rejected"] += 1
            if not dry_run:
                mailbox.move(uid, rejected)
            continue
        if raw is None:
            log("couldn't read a message right now; leaving it in the inbox")
            counts["postponed"] += 1
            continue
        message = mail.parse(raw)
        address = mail.sender(message)
        reader = config.reader_for(address, mail.recipient(message)) if address else None
        try:
            signed = reader is not None and mail.dkim_aligned(raw, address, dnsfunc)
        except mail.LookupUnavailable:  # left in the inbox and checked again next run
            log(f"couldn't check a message from {address} right now (DNS lookup failed)")
            counts["postponed"] += 1
            continue
        if not signed:
            key = mail.message_key(raw, message, authenticated=False)
            why = (
                "recipient/user or sender allowlist mismatch"
                if reader is None
                else "DKIM check failed"
            )
            log(f"rejected a message from {address or 'an unreadable sender'} ({why})")
            store.record_message(key, None, rejected, [])
            counts["rejected"] += 1
            if not dry_run:
                mailbox.move(uid, rejected)
            continue
        key = mail.message_key(raw, message)
        folder = store.seen(key)
        if folder is not None:  # recorded by an earlier run that stopped before filing it
            log(f"[{reader.display}] already recorded message {key[:12]}; completing filing")
            if not dry_run:
                mailbox.move(uid, folder) if folder else mailbox.delete(uid)
            continue
        try:
            kind, title = mail.command(message)
            urls = mail.links(message, strict=kind == "book")
            submitted_files = mail.attachments(message)
            if kind == "send_now" and (urls or submitted_files):
                raise ValueError("Send now must not contain links or document attachments")
            if kind == "book" and not (urls or submitted_files):
                raise ValueError("Book email needs links or document attachments")
        except ValueError as error:
            store.record_message(key, None, rejected, [])
            counts["rejected"] += 1
            log(f"[{reader.display}] rejected command: {error}")
            if not dry_run:
                mailbox.move(uid, rejected)
            continue
        files = [attachments.save(name, data) for name, data in submitted_files]
        folder = "" if files else mailbox.folder("Processed", reader.name)
        added = store.record_message(key, reader.name, folder, urls + files, kind=kind, title=title)
        if not dry_run:
            mailbox.delete(uid) if files else mailbox.move(uid, folder)
        counts["messages"] += 1
        counts["links"] += added
        extra = f" and {len(files)} attached document(s)" if files else ""
        log(
            f"[{reader.display}] queued {kind}: {added} new item(s), {len(urls)} link(s){extra}; message {key[:12]}"
        )
    return counts


def classify(error):
    """(readable reason, site busy, worth retrying)."""
    if isinstance(error, HTTPError):
        if error.code == 429:
            return "Site is limiting requests", True, True
        return f"HTTP error {error.code}", False, error.code not in {404, 410}
    if isinstance(error, URLError | TimeoutError | HTTPException | ConnectionError | socket.error):
        return "Network request failed", False, True
    if isinstance(error, ValueError):
        reason = mail.redact(str(error))[:200] or "Could not read the page"
        return reason, False, not reason.startswith(PERMANENT)
    return f"Could not read the page ({type(error).__name__})", False, True


def fetch(url, cache, attachments=None):
    document = cache.load(url)
    if document is not None:
        event("cache", url, "reused", level=logging.DEBUG)
        return document, True
    from einkrelayer.capture.readers import read

    if Attachments.is_attachment(url):
        path = attachments.path(url) if attachments else None
        if path is None or not path.is_file():
            raise ValueError("The attached document is no longer stored")
        document = read(path, cache=None)
        document.source = url
    else:
        document = read(url, cache=None)
    cache.save(url, document)
    return document, False


def couldnt_fetch(rows):
    def entry(url):
        if Attachments.is_attachment(url):
            return f"Attached document: {escape(shown(url))}"
        return f'<a href="{escape(url, quote=True)}">{escape(url)}</a>'

    items = "".join(
        f"<li><p>{entry(row['url'])}</p>"
        f"<p>{escape(row['reason'] or 'Could not read the page')}</p></li>"
        for row in rows
    )
    html = (
        "<body><h1>Couldn't fetch</h1><p>These links could not be read after several tries."
        f" Open them on another device if you still want them.</p><ul>{items}</ul></body>"
    )
    stamp = datetime.now(UTC).isoformat()
    return Document(
        f"urn:einkrelayer:couldnt-fetch:{stamp}", "Couldn't fetch", html, captured=stamp
    )


def subject(config, reader, day):
    """The digest email's subject: its title and the day it was built."""
    title = reader.title if reader and reader.title else config.digest_title
    return f"{title} — {day.isoformat()}"


def build(title, articles, failed, destination):
    documents = [document for _, document in articles]
    if failed:
        documents.append(couldnt_fetch(failed))
    return compose(BookJob(title, documents, mode="digest"), destination)


def fit(title, articles, failed, destination, settings, heading):
    """Build the digest, leaving trailing articles queued until the email fits the size limit."""
    while articles:
        build(title, articles, failed, destination)
        try:
            message = smtp.compose(
                settings, [destination], subject=heading, body="Your digest is attached."
            )
            return articles, message
        except ValueError as error:
            if "size limit" not in str(error):
                raise
        articles = articles[:-1]
    return [], None


def process(
    config,
    reader,
    store,
    cache,
    settings,
    *,
    today,
    dry_run,
    send_now,
    output,
    attachments=None,
    request_ids=(),
):
    name = reader.name
    for row in store.uncertain(name):
        log(
            f"[{reader.display}] delivery {row['id']} may or may not have arrived; check the Kindle, then"
            f" run 'einkrelayer resolve {row['id']} sent' or 'resolve {row['id']} failed'"
        )
    articles = []
    for row in store.due(name, today):
        if len(articles) >= MAX_ARTICLES:
            break
        try:
            document, cached = fetch(row["url"], cache, attachments)
        except Exception as error:  # one bad link never stops the others
            reason, busy, retry = classify(error)
            status, next_try = store.record_failure(
                row["id"], reason, today, busy=busy, permanent=not retry
            )
            when = f"retry on {next_try}" if next_try else "giving up"
            log(f"[{reader.display}] couldn't fetch {shown(row['url'])} ({reason}); {when}")
            continue
        log(f"[{reader.display}] got {shown(row['url'])}" + (" from cache" if cached else ""))
        articles.append((row, document))
    # Links are fetched on every run, so pages that change are caught early.
    if reader.days and DAYS[today.weekday()] not in reader.days and not send_now:
        log(f"[{reader.display}] not a digest day; {len(articles)} article(s) ready")
        return "skipped"
    failed = store.failed(name)
    needed = 1 if send_now else reader.min_articles
    if len(articles) < needed:
        if request_ids:
            store.complete_requests(request_ids, "empty", "No digest articles ready on this run")
        log(f"[{reader.display}] waiting: {len(articles)}/{needed} articles")
        return "waiting"
    title = reader.title or config.digest_title
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    if dry_run:
        destination = output / f"{name}.epub"
        destination.parent.mkdir(parents=True, exist_ok=True)
        build(title, articles, failed, destination)
        log(f"[{reader.display}] preview built from {len(articles)} article(s): {destination}")
        return "preview"
    destination = config.state / "deliveries" / f"{name}-{stamp}.epub"
    destination.parent.mkdir(parents=True, exist_ok=True)
    recipients = {**settings, "recipients": reader.kindle}
    heading = subject(config, reader, today)
    kept, message = fit(title, articles, failed, destination, recipients, heading)
    if not kept:
        store.complete_requests(request_ids, "failed", "Too large to email")
        row = articles[0][0]
        store.record_failure(row["id"], "Too large to email", today, permanent=True)
        log(
            f"[{reader.display}] {shown(row['url'])} is too large to email on its own; giving up on it"
        )
        return "failed"
    delivery = store.start_delivery(
        name,
        destination,
        reader.kindle,
        [row["id"] for row, _ in kept],
        [row["id"] for row in failed],
        len(message.as_bytes()),
        message_id=message["Message-ID"],
        request_ids=request_ids,
    )
    status, outcome, error = smtp.transmit(recipients, message)
    store.finish_delivery(
        delivery, status, message_id=message["Message-ID"], outcome=outcome, error=error
    )
    if status in {"sent", "partial"}:
        cache.prune([row["url"] for row, _ in kept])
    detail = f" ({error})" if error else ""
    log(f"[{reader.display}] delivery {delivery} {status}: {len(kept)} article(s){detail}")
    return status


def process_books(config, reader, store, cache, settings, *, today, dry_run, attachments):
    """One email is one whole book; preparation failures never send a partial book."""
    from einkrelayer.books.compose import identity
    from einkrelayer.capture.collections import book_sources

    results = []
    for request in store.requests(reader.name, kind="book", today=today):
        request_id = request["id"]
        try:
            sources = json.loads(request["sources"])
            selected = json.loads(request["selection"]) if request["selection"] else []
            if not request["selection"]:
                seen = set()
                checked = {}
                for source in sources:
                    expanded = (
                        [source]
                        if Attachments.is_attachment(source)
                        else book_sources(source, checked=checked)
                    )
                    for chapter in expanded:
                        key = identity(chapter)
                        if key not in seen:
                            selected.append(chapter)
                            seen.add(key)
                        if len(selected) > MAX_ARTICLES:
                            raise ValueError("Book exceeds the 50-chapter limit")
                store.book_selection(request_id, selected, request["title"])
            documents = [fetch(source, cache, attachments)[0] for source in selected]
            title = request["title"] or (
                documents[0].title if len(documents) == 1 else "Collected readings"
            )
            store.book_selection(request_id, selected, title)
            destination = (
                Path("output/preview") / f"{reader.name}-book-{request_id}.epub"
                if dry_run
                else config.state / "deliveries" / f"{reader.name}-book-{request_id}.epub"
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            compose(BookJob(title, documents, mode="book"), destination)
            if dry_run:
                log(f"[{reader.display}] book {request_id} preview: {destination}")
                results.append("preview")
                continue
            recipients = {**settings, "recipients": reader.kindle}
            try:
                message = smtp.compose(
                    recipients, [destination], subject=title, body="Your book is attached."
                )
            except ValueError as error:
                if "size limit" in str(error):
                    raise ValueError("Too large to email as one book") from error
                raise
            delivery = store.start_delivery(
                reader.name,
                destination,
                reader.kindle,
                [],
                [],
                len(message.as_bytes()),
                message_id=message["Message-ID"],
                request_ids=[request_id],
                article_count=len(documents),
            )
        except Exception as error:
            reason, busy, retry = classify(error)
            _, next_try = store.request_failure(
                request_id, reason, today, busy=busy, permanent=not retry
            )
            when = f"retry on {next_try}" if next_try else "giving up"
            log(f"[{reader.display}] couldn't prepare book {request_id}: {reason}; {when}")
            results.append("failed")
            continue
        # Once a delivery is recorded, a crash leaves it held for explicit resolution.
        status, outcome, error = smtp.transmit(recipients, message)
        store.finish_delivery(
            delivery, status, message_id=message["Message-ID"], outcome=outcome, error=error
        )
        if status in {"sent", "partial"}:
            cache.prune(selected)
        detail = f" ({error})" if error else ""
        log(f"[{reader.display}] book {request_id}, delivery {delivery}: {status}{detail}")
        results.append(status)
    return results


def run(config, *, env_file=Path(".env"), dry_run=False, send_now=None, dnsfunc=None, mailbox=None):
    from einkrelayer.relay.store import run_lock

    if send_now and send_now not in config.readers:
        raise ValueError(f"Unknown reader: {send_now}")
    settings = None if dry_run else smtp.settings(env_file, recipients=False)
    with run_lock(config.state), Store(config.state, dry_run=dry_run) as store:
        return cycle(config, store, settings, dry_run, send_now, dnsfunc, mailbox)


def cycle(config, store, settings, dry_run, send_now, dnsfunc, mailbox):
    from einkrelayer.relay.inbox import Mailbox

    cache = ArticleCache(config.state, dry_run=dry_run)
    for delivery in store.interrupted():
        log(f"delivery {delivery} was interrupted while sending; marked uncertain")
    try:
        box = mailbox or Mailbox(config)
    except (OSError, ssl.SSLError) as error:
        raise ValueError(f"Could not reach the mailbox ({type(error).__name__})") from error
    attachments = Attachments(config.state, dry_run=dry_run)
    try:
        try:
            counts = intake(config, store, box, attachments, dry_run=dry_run, dnsfunc=dnsfunc)
        finally:
            box.close()
        if not any(counts[kind] for kind in ("messages", "rejected", "postponed")):
            log("no new mail")
        else:
            log("intake: " + ", ".join(f"{value} {key}" for key, value in counts.items()))
        results = {}
        today = date.today()
        for name, reader in config.readers.items():
            if send_now and name != send_now:
                continue
            try:
                commands = store.requests(name, kind="send_now", today=today)
                digest_result = process(
                    config,
                    reader,
                    store,
                    cache,
                    settings,
                    today=today,
                    dry_run=dry_run,
                    send_now=bool(send_now) or bool(commands),
                    output=Path("output/preview"),
                    attachments=attachments,
                    request_ids=[request["id"] for request in commands],
                )
            except Exception as error:
                digest_result = "failed"
                log(
                    f"[{reader.display}] digest failed: {mail.redact(str(error)) or type(error).__name__}"
                )
            try:
                books = process_books(
                    config,
                    reader,
                    store,
                    cache,
                    settings,
                    today=today,
                    dry_run=dry_run,
                    attachments=attachments,
                )
            except Exception as error:  # one reader's failure never blocks the others
                books = ["failed"]
                log(
                    f"[{reader.display}] book failed: {mail.redact(str(error)) or type(error).__name__}"
                )
            outcomes = [digest_result, *books]
            results[name] = next(
                (
                    status
                    for status in ("uncertain", "failed", "partial", "sent", "preview")
                    if status in outcomes
                ),
                digest_result,
            )
        cache.prune()
        if not dry_run:
            store.prune_books()
        return results
    finally:
        if not dry_run:  # attached files are kept only while their entries wait
            attachments.prune(store.waiting_attachments())
        attachments.close()
