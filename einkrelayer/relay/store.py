"""Durable relay state: submitted links, deliveries, the article cache, and the run lock."""

import base64
import dataclasses
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
from contextlib import closing, contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from einkrelayer.capture.http import atomic_write
from einkrelayer.models import Document

RETRIES = 3  # retries after the first failed try
RETRY_DAYS = 7  # waits 7, 14, then 28 days
BUSY_RETRIES = 5  # next-day retries while a site is limiting requests
CACHE_DAYS = 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    key TEXT PRIMARY KEY,
    reader TEXT,
    folder TEXT NOT NULL,
    received TEXT NOT NULL,
    links INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS links (
    id INTEGER PRIMARY KEY,
    reader TEXT NOT NULL,
    url TEXT NOT NULL,
    added TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    fails INTEGER NOT NULL DEFAULT 0,
    busy INTEGER NOT NULL DEFAULT 0,
    next_try TEXT,
    reason TEXT,
    delivery INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS open_links ON links(reader, url)
    WHERE status IN ('pending', 'held', 'failed');
CREATE TABLE IF NOT EXISTS deliveries (
    id INTEGER PRIMARY KEY,
    reader TEXT NOT NULL,
    created TEXT NOT NULL,
    status TEXT NOT NULL,
    epub TEXT NOT NULL,
    recipients TEXT NOT NULL,
    articles INTEGER NOT NULL,
    bytes INTEGER,
    message_id TEXT,
    outcome TEXT,
    error TEXT,
    finished TEXT,
    resend_of INTEGER
);
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY,
    message_key TEXT NOT NULL UNIQUE,
    reader TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    sources TEXT NOT NULL,
    selection TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    fails INTEGER NOT NULL DEFAULT 0,
    next_try TEXT,
    reason TEXT NOT NULL DEFAULT '',
    delivery INTEGER
);
"""


def now():
    return datetime.now(UTC).isoformat(timespec="seconds")


class Locked(RuntimeError):
    pass


@contextmanager
def run_lock(state):
    """One relay run at a time; the lock is released if the process dies."""
    state.mkdir(parents=True, exist_ok=True)
    with open(state / "run.lock", "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Locked("Another relay run is in progress") from None
        yield


class Store:
    """SQLite state. A dry run works on an in-memory copy, so nothing on disk changes."""

    def __init__(self, state, *, dry_run=False):
        self.state = Path(state)
        path = self.state / "relay.sqlite3"
        if dry_run:
            self.db = sqlite3.connect(":memory:", isolation_level=None)
            if path.exists():
                with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as source:
                    source.backup(self.db)
        else:
            self.state.mkdir(parents=True, exist_ok=True)
            os.chmod(self.state, 0o700)
            self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(deliveries)")}
        if "resend_of" not in columns:
            self.db.execute("ALTER TABLE deliveries ADD COLUMN resend_of INTEGER")

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        self.db.execute("COMMIT")

    # ----- intake -----

    def seen(self, key):
        row = self.db.execute("SELECT folder FROM messages WHERE key = ?", (key,)).fetchone()
        return row["folder"] if row else None

    def record_message(self, key, reader, folder, urls, *, kind="digest", title=""):
        """Store a message and its links together, before the message is moved."""
        with self.transaction() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO messages VALUES (?, ?, ?, ?, ?)",
                (key, reader, folder, now(), len(urls)),
            )
            if not cursor.rowcount:
                return 0
            if kind != "digest":
                db.execute(
                    "INSERT INTO requests (message_key, reader, kind, title, sources)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (key, reader, kind, title, json.dumps(urls)),
                )
                return len(urls)
            added = 0
            for url in urls:
                cursor = db.execute(
                    "INSERT OR IGNORE INTO links (reader, url, added) VALUES (?, ?, ?)",
                    (reader, url, now()),
                )
                added += cursor.rowcount
            return added

    # ----- queue -----

    def queue(self, reader=None):
        sql = "SELECT * FROM links WHERE status IN ('pending', 'held', 'failed')"
        args = ()
        if reader:
            sql += " AND reader = ?"
            args = (reader,)
        return self.db.execute(sql + " ORDER BY id", args).fetchall()

    def due(self, reader, today):
        return self.db.execute(
            "SELECT * FROM links WHERE reader = ? AND status = 'pending'"
            " AND (next_try IS NULL OR next_try <= ?) ORDER BY id",
            (reader, today.isoformat()),
        ).fetchall()

    def waiting_attachments(self):
        """Attachment entries still needed: queued, or held by a delivery in progress."""
        rows = self.db.execute(
            "SELECT url FROM links WHERE status IN ('pending', 'held') AND url LIKE 'attachment:%'"
        ).fetchall()
        entries = [row["url"] for row in rows]
        for row in self.db.execute(
            "SELECT sources FROM requests WHERE status IN ('pending', 'held')"
        ):
            entries.extend(u for u in json.loads(row["sources"]) if Attachments.is_attachment(u))
        return entries

    def requests(self, reader=None, *, kind=None, today=None):
        sql = "SELECT * FROM requests WHERE status = 'pending'"
        args = []
        if reader:
            sql += " AND reader = ?"
            args.append(reader)
        if kind:
            sql += " AND kind = ?"
            args.append(kind)
        if today:
            sql += " AND (next_try IS NULL OR next_try <= ?)"
            args.append(today.isoformat())
        return self.db.execute(sql + " ORDER BY id", args).fetchall()

    def complete_requests(self, ids, status, reason=""):
        with self.transaction() as db:
            db.executemany(
                "UPDATE requests SET status = ?, reason = ? WHERE id = ? AND status = 'pending'",
                [(status, reason, request) for request in ids],
            )

    def book_selection(self, request, sources, title):
        self.db.execute(
            "UPDATE requests SET selection = ?, title = ? WHERE id = ?",
            (json.dumps(sources), title, request),
        )

    def request_failure(self, request, reason, today, *, busy=False, permanent=False):
        row = self.db.execute("SELECT fails FROM requests WHERE id = ?", (request,)).fetchone()
        fails = row["fails"] + 1
        status = "failed" if permanent or fails > RETRIES else "pending"
        next_try = (
            (today + timedelta(days=1 if busy else RETRY_DAYS * 2 ** (fails - 1))).isoformat()
            if status == "pending"
            else None
        )
        self.db.execute(
            "UPDATE requests SET status = ?, fails = ?, next_try = ?, reason = ? WHERE id = ?",
            (status, fails, next_try, reason, request),
        )
        return status, next_try

    def delivery_request(self, delivery):
        return self.db.execute(
            "SELECT * FROM requests WHERE delivery = ? AND kind = 'book'", (delivery,)
        ).fetchone()

    def request_history(self, reader=None, limit=20):
        sql = "SELECT * FROM requests"
        args = []
        if reader:
            sql += " WHERE reader = ?"
            args.append(reader)
        return self.db.execute(sql + " ORDER BY id DESC LIMIT ?", [*args, limit]).fetchall()

    def queued_requests(self, reader=None):
        sql = "SELECT * FROM requests WHERE status IN ('pending', 'held', 'failed')"
        args = []
        if reader:
            sql += " AND reader = ?"
            args.append(reader)
        return self.db.execute(sql + " ORDER BY id", args).fetchall()

    def failed(self, reader):
        return self.db.execute(
            "SELECT * FROM links WHERE reader = ? AND status = 'failed'"
            " AND delivery IS NULL ORDER BY id",
            (reader,),
        ).fetchall()

    def record_failure(self, link_id, reason, today, *, busy=False, permanent=False):
        """Schedule the next try: tomorrow while a site is busy, else 7, 14, 28 days."""
        row = self.db.execute("SELECT * FROM links WHERE id = ?", (link_id,)).fetchone()
        fails, waits = row["fails"], row["busy"]
        if busy and waits < BUSY_RETRIES and not permanent:
            waits += 1
            status, next_try = "pending", today + timedelta(days=1)
        else:
            fails += 1
            if permanent or fails > RETRIES:
                status, next_try = "failed", None
            else:
                status, next_try = "pending", today + timedelta(days=RETRY_DAYS * 2 ** (fails - 1))
        with self.transaction() as db:
            db.execute(
                "UPDATE links SET status = ?, fails = ?, busy = ?, next_try = ?, reason = ?"
                " WHERE id = ?",
                (status, fails, waits, next_try and next_try.isoformat(), reason, link_id),
            )
        return status, next_try

    # ----- deliveries -----

    def start_delivery(
        self,
        reader,
        epub,
        recipients,
        article_ids,
        failed_ids,
        size,
        *,
        message_id="",
        resend_of=None,
        request_ids=(),
        article_count=None,
    ):
        """Record the send before it happens, so a crash leaves it uncertain, never resent."""
        with self.transaction() as db:
            cursor = db.execute(
                "INSERT INTO deliveries (reader, created, status, epub, recipients, articles,"
                " bytes, message_id, resend_of) VALUES (?, ?, 'sending', ?, ?, ?, ?, ?, ?)",
                (
                    reader,
                    now(),
                    str(epub),
                    json.dumps(recipients),
                    self.delivery(resend_of)["articles"]
                    if resend_of
                    else (article_count if article_count is not None else len(article_ids)),
                    size,
                    message_id,
                    resend_of,
                ),
            )
            delivery = cursor.lastrowid
            db.executemany(
                "UPDATE requests SET delivery = ?, status = 'held'"
                " WHERE id = ? AND reader = ? AND status = 'pending'",
                [(delivery, request, reader) for request in request_ids],
            )
            for link_id in [*article_ids, *failed_ids]:
                db.execute(
                    "UPDATE links SET delivery = ?, status = CASE status"
                    " WHEN 'failed' THEN 'failed' ELSE 'held' END WHERE id = ?",
                    (delivery, link_id),
                )
            return delivery

    def finish_delivery(self, delivery, status, *, message_id="", outcome=None, error=""):
        """sent/partial: links are done. failed: links return to the queue. uncertain: held."""
        with self.transaction() as db:
            row = self.delivery(delivery)
            if status == "failed" and row["resend_of"] is None:
                unresolved = db.execute(
                    "SELECT id FROM deliveries WHERE resend_of = ?"
                    " AND status IN ('sending', 'uncertain')",
                    (delivery,),
                ).fetchall()
                if unresolved:
                    attempts = ", ".join(str(attempt["id"]) for attempt in unresolved)
                    raise ValueError(
                        f"Resolve the uncertain resend attempts ({attempts}) before releasing articles"
                    )
            db.execute(
                "UPDATE deliveries SET status = ?, message_id = ?, outcome = ?, error = ?,"
                " finished = ? WHERE id = ?",
                (
                    status,
                    message_id or row["message_id"],
                    json.dumps(outcome) if outcome is not None else row["outcome"] or "{}",
                    error,
                    now(),
                    delivery,
                ),
            )
            if status in {"sent", "partial"}:
                root = row["resend_of"] or delivery
                db.execute(
                    "UPDATE requests SET status = ?, reason = '' WHERE delivery = ?",
                    (status, root),
                )
                if row["resend_of"]:
                    db.execute(
                        "UPDATE deliveries SET status = ?, finished = ?, error = ''"
                        " WHERE id = ? AND status = 'uncertain'",
                        (status, now(), root),
                    )
                db.execute(
                    "UPDATE links SET status = CASE status WHEN 'failed' THEN 'reported'"
                    " ELSE 'sent' END WHERE delivery = ? AND status IN ('held', 'failed')",
                    (root,),
                )
            elif status == "failed":
                # Failed book deliveries retry as whole books. A send-now request
                # authorizes one delivery attempt, not future automatic forced sends.
                db.execute(
                    "UPDATE requests SET status = CASE kind WHEN 'book' THEN 'pending'"
                    " ELSE 'failed' END, delivery = NULL, next_try = ?, reason = ?"
                    " WHERE delivery = ?",
                    ((date.today() + timedelta(days=1)).isoformat(), error, delivery),
                )
                db.execute(
                    "UPDATE links SET status = 'pending', delivery = NULL"
                    " WHERE delivery = ? AND status = 'held'",
                    (delivery,),
                )
                db.execute(
                    "UPDATE links SET delivery = NULL WHERE delivery = ? AND status = 'failed'",
                    (delivery,),
                )

    def interrupted(self):
        """Deliveries a crash left mid-send become uncertain."""
        rows = self.db.execute("SELECT id FROM deliveries WHERE status = 'sending'").fetchall()
        for row in rows:
            self.finish_delivery(row["id"], "uncertain", error="Run stopped while sending")
        return [row["id"] for row in rows]

    def deliveries(self, limit=20):
        return self.db.execute(
            "SELECT * FROM deliveries ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    def delivery(self, delivery):
        row = self.db.execute("SELECT * FROM deliveries WHERE id = ?", (delivery,)).fetchone()
        if row is None:
            raise ValueError(f"No delivery {delivery}")
        return row

    def prune_books(self):
        """Delete sent books and their reports after CACHE_DAYS, so `resend` works until then.
        Books of uncertain deliveries are kept until the delivery is settled."""
        folder = self.state / "deliveries"
        rows = self.db.execute(
            "SELECT epub FROM deliveries WHERE status IN ('uncertain', 'sending')"
        ).fetchall()
        kept = {Path(row["epub"]).resolve() for row in rows}
        cutoff = time.time() - CACHE_DAYS * 86400
        for path in folder.iterdir() if folder.is_dir() else []:
            stem = path.name.removesuffix(".report.json").removesuffix(".epub")
            book = path.resolve().with_name(stem + ".epub")
            if book not in kept and path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)

    def uncertain(self, reader):
        return self.db.execute(
            "SELECT id FROM deliveries WHERE reader = ? AND status = 'uncertain'", (reader,)
        ).fetchall()

    def prune_sources(self, delivery):
        """Remove completed source files after a management command settles a delivery."""
        row = self.delivery(delivery)
        root = row["resend_of"] or delivery
        urls = self.db.execute(
            "SELECT url FROM links WHERE delivery = ? AND status IN ('sent', 'reported')",
            (root,),
        ).fetchall()
        ArticleCache(self.state).prune([entry["url"] for entry in urls])
        request = self.delivery_request(root)
        if request and request["status"] in {"sent", "partial"}:
            ArticleCache(self.state).prune(json.loads(request["selection"] or "[]"))
        Attachments(self.state).prune(self.waiting_attachments())


class Attachments:
    """Documents that arrived as email attachments, kept only until their digest is sent or they
    fail for good. A dry run reads files already stored but keeps new ones in a temporary
    folder that is removed afterwards."""

    PREFIX = "attachment:"

    def __init__(self, state, *, dry_run=False):
        self.temporary = dry_run
        self.stored = Path(state) / "attachments"
        self.root = Path(tempfile.mkdtemp()) if dry_run else self.stored

    def save(self, name, data):
        """Store one file and return its queue entry, attachment:<hash>/<file name>."""
        key = hashlib.sha256(data).hexdigest()
        path = self.root / key / name
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        if not path.exists():
            atomic_write(path, data)
            os.chmod(path, 0o600)
        return f"{self.PREFIX}{key}/{name}"

    @classmethod
    def is_attachment(cls, entry):
        return entry.startswith(cls.PREFIX)

    def path(self, entry):
        key, _, name = entry.removeprefix(self.PREFIX).partition("/")
        if (
            not re.fullmatch(r"[0-9a-f]{64}", key)
            or not name
            or "/" in name
            or name.startswith(".")
        ):
            raise ValueError("Unsupported document format")
        path = self.root / key / name
        if self.temporary and not path.exists():
            return self.stored / key / name
        return path

    def prune(self, keep):
        """Delete every stored file whose queue entry is no longer waiting."""
        wanted = {entry.removeprefix(self.PREFIX).partition("/")[0] for entry in keep}
        for folder in self.root.iterdir() if self.root.is_dir() else []:
            if folder.name not in wanted:
                shutil.rmtree(folder, ignore_errors=True)

    def close(self):
        if self.temporary:
            shutil.rmtree(self.root, ignore_errors=True)


class ArticleCache:
    """Fetched articles kept while a digest waits; files are named by hash, not URL."""

    def __init__(self, state, *, dry_run=False):
        self.root = Path(state) / "articles"
        self.read_only = dry_run

    def path(self, url):
        return self.root / (hashlib.sha256(url.encode()).hexdigest() + ".json")

    def load(self, url):
        try:
            data = json.loads(self.path(url).read_text())
            data["assets"] = {k: base64.b64decode(v) for k, v in data["assets"].items()}
            return Document(**data)
        except (OSError, ValueError, TypeError, KeyError):
            return None

    def save(self, url, document):
        if self.read_only:
            return
        data = dataclasses.asdict(document)
        data["assets"] = {k: base64.b64encode(v).decode() for k, v in data["assets"].items()}
        self.root.mkdir(parents=True, exist_ok=True)
        atomic_write(self.path(url), json.dumps(data).encode())

    def prune(self, sent=()):
        if self.read_only:
            return
        for url in sent:
            self.path(url).unlink(missing_ok=True)
        cutoff = time.time() - CACHE_DAYS * 86400
        for entry in self.root.glob("*.json") if self.root.is_dir() else []:
            if entry.stat().st_mtime < cutoff:
                entry.unlink(missing_ok=True)


def today():
    return date.today()
