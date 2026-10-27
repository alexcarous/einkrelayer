"""Relay settings from .env and the reader list in users.toml."""

import math
import os
import re
import tomllib
from dataclasses import dataclass, field
from email.utils import parseaddr
from pathlib import Path

from dotenv import dotenv_values

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
NAME = re.compile(r"[A-Za-z0-9_-]{1,40}")


@dataclass
class Reader:
    name: str
    senders: list[str]
    kindle: list[str]
    label: str
    min_articles: int = 10
    days: list[str] = field(default_factory=list)
    title: str = ""

    @property
    def display(self):
        return f"{self.label} ({self.name})" if self.label != self.name else self.label


@dataclass
class RelayConfig:
    imap_host: str
    imap_port: int
    imap_username: str
    imap_password: str
    folder: str
    state: Path
    readers: dict[str, Reader]
    digest_title: str
    relay_address: str
    timeout: float = 30

    def reader_for(self, sender, recipient):
        local, domain = self.relay_address.lower().rsplit("@", 1)
        match = re.fullmatch(
            re.escape(local) + r"\+([a-z0-9_-]{1,40})@" + re.escape(domain), recipient.lower()
        )
        if not match:
            return None
        reader = next((r for r in self.readers.values() if r.name.lower() == match[1]), None)
        return reader if reader and sender.lower() in reader.senders else None


def plain_address(value, label):
    if not isinstance(value, str):
        raise ValueError(f"{label} must be one plain email address")
    value = value.strip()
    if any(c in value for c in "\r\n") or parseaddr(value)[1] != value or "@" not in value:
        raise ValueError(f"{label} must be one plain email address")
    return value


def load_readers(path):
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"Create {path.name} with one table per reader")
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    readers, names = {}, set()
    for name, table in data.items():
        if not NAME.fullmatch(name) or name.lower() == "rejected" or not isinstance(table, dict):
            raise ValueError(f"Reader name {name!r} must use letters, digits, _ or -")
        if name.lower() in names:
            raise ValueError(f"Duplicate reader name (case-insensitive): {name}")
        names.add(name.lower())
        senders = table.get("senders") or []
        kindle = table.get("kindle") or []
        kindle = [kindle] if isinstance(kindle, str) else kindle
        if not isinstance(senders, list) or not isinstance(kindle, list):
            raise ValueError(f"Reader {name}: senders and kindle must be address lists")
        if not senders or not kindle:
            raise ValueError(f"Reader {name} needs senders and kindle")
        senders = [plain_address(s, f"{name} sender").lower() for s in senders]
        minimum = table.get("min_articles", 10)
        if type(minimum) is not int or not 1 <= minimum <= 50:
            raise ValueError(f"Reader {name}: min_articles must be a whole number from 1 to 50")
        days = table.get("days", [])
        if not isinstance(days, list) or any(not isinstance(d, str) for d in days):
            raise ValueError(f"Reader {name}: days must be a list of day names")
        days = [d.lower()[:3] for d in days]
        if any(d not in DAYS for d in days):
            raise ValueError(f"Reader {name}: days must be names like mon or sun")
        title = str(table.get("title", "")).strip()
        label = table.get("label")
        if (
            not isinstance(label, str)
            or not label.strip()
            or any(ord(c) < 32 or ord(c) == 127 or c in "\u0085\u2028\u2029" for c in label)
        ):
            raise ValueError(f"Reader {name}: label must be nonempty single-line text")
        readers[name] = Reader(
            name,
            senders,
            [plain_address(k, f"{name} kindle") for k in kindle],
            label.strip(),
            minimum,
            days,
            title,
        )
    if not readers:
        raise ValueError(f"{path.name} has no readers")
    return readers


def load(env_file=Path(".env")):
    values = {**dotenv_values(env_file, interpolate=False), **os.environ}
    missing = [
        k
        for k in ("IMAP_HOST", "IMAP_USERNAME", "IMAP_PASSWORD", "RELAY_ADDRESS")
        if not values.get(k)
    ]
    if missing:
        raise ValueError("Fill in " + ", ".join(missing) + " in .env")
    relay_address = plain_address(values["RELAY_ADDRESS"].strip(), "RELAY_ADDRESS").lower()
    if "+" in relay_address.split("@", 1)[0]:
        raise ValueError("RELAY_ADDRESS must be a base mailbox without a plus tag")
    port = int(values.get("IMAP_PORT") or 993)
    if not 0 < port < 65536:
        raise ValueError("IMAP_PORT is invalid")
    timeout = float(values.get("SMTP_TIMEOUT") or 30)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("SMTP_TIMEOUT must be a positive finite number")
    return RelayConfig(
        imap_host=values["IMAP_HOST"].strip(),
        imap_port=port,
        imap_username=values["IMAP_USERNAME"],
        imap_password=values["IMAP_PASSWORD"],
        folder=(values.get("IMAP_FOLDER") or "INBOX").strip(),
        state=Path(values.get("RELAY_STATE_DIR") or "state"),
        readers=load_readers(values.get("RELAY_USERS_FILE") or "users.toml"),
        digest_title=(values.get("DIGEST_TITLE") or "").strip() or "einkrelayer Digest",
        timeout=timeout,
        relay_address=relay_address,
    )
