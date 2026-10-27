"""Inspect and manage the relay: queue, delivery history, resend, resolve, setup check."""

import json
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path

from einkrelayer.delivery import smtp
from einkrelayer.relay.mail import redact, safe_url
from einkrelayer.relay.store import Store, run_lock


def reader_display(config, name):
    reader = config.readers.get(name)
    return reader.display if reader else name


def queue(config, reader=None):
    with Store(config.state, dry_run=True) as store:
        rows = store.queue(reader)
        requests = store.queued_requests(reader)
    lines = []
    for row in rows:
        when = f", next try {row['next_try']}" if row["next_try"] else ""
        tries = f", {row['fails']} failed tries" if row["fails"] else ""
        reason = f" — {row['reason']}" if row["reason"] else ""
        lines.append(
            f"{row['id']:>5}  {reader_display(config, row['reader'])}  {row['status']}{tries}{when}  "
            f"{shown(row['url'])}{reason}"
        )
    for request in requests:
        lines.append(
            f"request {request['id']}  {reader_display(config, request['reader'])}  "
            f"{request['kind']}  {request['status']}  "
            f"{redact(request['title'])}  {redact(request['reason'])}"
            + (f"; next try {request['next_try']}" if request["next_try"] else "")
        )
    return lines or ["Queue is empty"]


def local_time(stamp):
    """A stored UTC time as this computer's local time, to match the run log."""
    try:
        moment = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return str(stamp)[:16]
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone().strftime("%Y-%m-%d %H:%M")


def shown(entry):
    if entry.startswith("attachment:"):
        return "attached document: " + entry.rpartition("/")[2]
    return safe_url(entry)


def history(config, limit=20):
    with Store(config.state, dry_run=True) as store:
        rows = store.deliveries(limit)
        requests = store.request_history(limit=limit)
    lines = []
    for row in rows:
        outcome = json.loads(row["outcome"] or "{}")
        refused = [f"{a}: {v}" for a, v in outcome.items() if v != "accepted"]
        size = f", {row['bytes'] / 1_000_000:.1f} MB" if row["bytes"] else ""
        error = f" — {row['error']}" if row["error"] else ""
        resend = f" (resend of {row['resend_of']})" if row["resend_of"] else ""
        lines.append(
            f"{row['id']:>4}  {local_time(row['created'])}  "
            f"{reader_display(config, row['reader'])}{resend}  "
            f"{row['status']}  {row['articles']} article(s){size}{error}"
        )
        lines.extend(f"        refused {item}" for item in refused)
    for request in requests:
        lines.append(
            f"request {request['id']}  {reader_display(config, request['reader'])}  "
            f"{request['kind']}  {request['status']}"
            + (f"; delivery {request['delivery']}" if request["delivery"] else "")
            + (f"; {redact(request['reason'])}" if request["reason"] else "")
        )
    return lines or ["No deliveries yet"]


def resolve(config, delivery, outcome):
    """Settle an uncertain delivery once the reader has checked their Kindle."""
    if outcome not in {"sent", "failed"}:
        raise ValueError("Resolve a delivery as sent or failed")
    with run_lock(config.state), Store(config.state) as store:
        store.interrupted()
        row = store.delivery(delivery)
        if row["status"] != "uncertain":
            raise ValueError(f"Delivery {delivery} is {row['status']}, not uncertain")
        store.finish_delivery(delivery, outcome, message_id=row["message_id"] or "")
        store.prune_sources(delivery)
    if outcome == "failed":
        if row["resend_of"]:
            return f"Resend attempt {delivery} marked failed; original delivery state is retained"
        return f"Delivery {delivery} marked failed; its articles return to the queue"
    return f"Delivery {delivery} marked sent"


def resend(config, delivery, *, env_file=Path(".env"), send=False):
    with run_lock(config.state) if send else nullcontext():
        with Store(config.state, dry_run=not send) as store:
            if send:
                store.interrupted()
            return _resend(config, store, delivery, env_file, send)


def _resend(config, store, delivery, env_file, send):
    from einkrelayer.relay.run import subject

    row = store.delivery(delivery)
    original = store.delivery(row["resend_of"]) if row["resend_of"] else row
    if original["status"] == "failed":
        raise ValueError(f"Delivery {delivery} failed and its articles are back in the queue")
    epub = Path(row["epub"])
    if not epub.is_file():
        raise ValueError(f"The book for delivery {delivery} is no longer stored")
    settings = {
        **smtp.settings(env_file, recipients=False),
        "recipients": json.loads(row["recipients"]),
    }
    built = datetime.fromisoformat(original["created"]).astimezone().date()
    request = store.delivery_request(original["id"])
    heading = (
        request["title"] if request else subject(config, config.readers.get(row["reader"]), built)
    )
    message = smtp.compose(
        settings,
        [epub],
        subject=heading,
        body="Your book is attached." if request else "Your digest is attached.",
    )
    summary = f"{len(message.as_bytes()):,} bytes to {', '.join(settings['recipients'])}"
    if not send:
        return f"Delivery {delivery} is ready to resend ({summary}). Add --send to send it."
    new = store.start_delivery(
        row["reader"],
        epub,
        settings["recipients"],
        [],
        [],
        len(message.as_bytes()),
        message_id=message["Message-ID"],
        resend_of=original["id"],
    )
    status, outcome, error = smtp.transmit(settings, message)
    store.finish_delivery(
        new, status, message_id=message["Message-ID"], outcome=outcome, error=error
    )
    store.prune_sources(new)
    return f"Resend of delivery {delivery} (attempt {new}): {status}" + (
        f" ({error})" if error else ""
    )


def check(env_file=Path(".env")):
    """Check settings, the reader list, the mailbox and SMTP logins, and that books can be
    format-checked. Sends nothing."""
    from einkrelayer.relay import config as relay_config
    from einkrelayer.relay.inbox import Mailbox

    results = []

    def step(label, action):
        try:
            detail = action()
            results.append((True, label + (f": {detail}" if detail else "")))
            return True
        except Exception as error:  # report every problem, not only the first
            detail = str(error) if isinstance(error, ValueError) else type(error).__name__
            results.append((False, f"{label}: {redact(detail)}"))
            return False

    holder = {}

    def load():
        holder["config"] = relay_config.load(env_file)
        return f"{len(holder['config'].readers)} reader(s)"

    def mailbox():
        box = Mailbox(holder["config"])
        try:
            return f"{len(box.uids())} message(s) waiting"
        finally:
            box.close()

    def outgoing():
        holder["smtp"] = smtp.settings(env_file, recipients=False)
        with smtp.connect(holder["smtp"]):
            return "login accepted"

    def checker():
        from einkrelayer.books.validation import epubcheck

        if epubcheck() is None:
            raise ValueError("EPUBCheck not found; books would be sent without a format check")
        return "found"

    if step("Relay settings and readers", load):
        step("Mailbox", mailbox)
    step("Outgoing mail", outgoing)
    step("Book format check", checker)
    return results
