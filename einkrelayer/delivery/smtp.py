#!/usr/bin/env python3
"""Prepare Kindle email; use --send to transmit it using settings in .env."""

import argparse
import math
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formatdate, make_msgid, parseaddr
from pathlib import Path
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile

from dotenv import dotenv_values

from einkrelayer.books.validation import validate
from einkrelayer.diagnostics import clean

ROOT = Path.cwd()


def default_env_file():
    return ROOT / ".env"


def settings(env_file, *, recipients=True):
    values = {**dotenv_values(env_file, interpolate=False), **os.environ}
    if not recipients:
        # Relay recipients come from users.toml; an empty/unrelated KINDLE_EMAIL
        # in the standalone-send template must not prevent a relay run.
        values["KINDLE_EMAIL"] = values.get("SMTP_FROM") or ""
    required = ("KINDLE_EMAIL", "SMTP_HOST", "SMTP_FROM")
    missing = [key for key in required if not (values.get(key) or "").strip()]
    if missing:
        raise ValueError("Fill in " + ", ".join(missing) + " in .env")
    recipients = list(
        dict.fromkeys(address.strip() for address in values["KINDLE_EMAIL"].split(","))
    )
    for key, address in [("KINDLE_EMAIL", address) for address in recipients] + [
        ("SMTP_FROM", values["SMTP_FROM"])
    ]:
        if (
            any(c in address for c in "\r\n")
            or parseaddr(address)[1] != address
            or "@" not in address
        ):
            raise ValueError(f"{key} must be one plain email address")
    mode = (values.get("SMTP_SECURITY") or "starttls").strip().lower()
    if mode not in ("starttls", "ssl"):
        raise ValueError("SMTP_SECURITY must be starttls or ssl")
    port = int(values.get("SMTP_PORT") or (465 if mode == "ssl" else 587))
    timeout = float(values.get("SMTP_TIMEOUT") or 30)
    limit = float(values.get("SMTP_MAX_MESSAGE_MB") or 20)
    if (
        not 0 < port < 65536
        or not math.isfinite(timeout)
        or not math.isfinite(limit)
        or timeout <= 0
        or limit <= 0
    ):
        raise ValueError("SMTP port, timeout, or size limit is invalid")
    username = values.get("SMTP_USERNAME") or ""
    password = values.get("SMTP_PASSWORD") or ""
    if bool(username) != bool(password):
        raise ValueError("Set both SMTP_USERNAME and SMTP_PASSWORD, or neither for a trusted relay")
    subject = values.get("EMAIL_SUBJECT") or "einkrelayer documents"
    if "\r" in subject or "\n" in subject:
        raise ValueError("EMAIL_SUBJECT cannot contain line breaks")
    return {
        "recipients": recipients,
        "sender": values["SMTP_FROM"],
        "host": values["SMTP_HOST"],
        "port": port,
        "mode": mode,
        "username": username,
        "password": password,
        "timeout": timeout,
        "max_bytes": int(limit * 1_000_000),
        "subject": subject,
    }


def select_recipients(config, selection=None, all_recipients=False):
    configured = config["recipients"]
    if all_recipients:
        return dict(config)
    if selection:
        selected = []
        for value in selection.split(","):
            value = value.strip()
            if value.isdigit() and 1 <= int(value) <= len(configured):
                address = configured[int(value) - 1]
            elif value in configured:
                address = value
            else:
                raise ValueError(
                    "Unknown recipient selection; use --list-recipients to see configured addresses"
                )
            if address not in selected:
                selected.append(address)
        return {**config, "recipients": selected}
    if len(configured) == 1:
        return dict(config)
    raise ValueError("Multiple Kindle addresses configured; choose --to 1,2 or --all-recipients")


def compose(config, paths, *, subject=None, body="Documents attached for your Kindle library."):
    if not paths or len(paths) > 25:
        raise ValueError("Supply between 1 and 25 EPUB attachments")
    message = EmailMessage(policy=SMTP)
    message["From"] = config["sender"]
    message["To"] = ", ".join(config["recipients"])
    message["Subject"] = subject or config["subject"]
    message["Date"] = formatdate(localtime=False)
    message["Message-ID"] = make_msgid()
    message.set_content(body)
    for path in paths:
        if path.suffix.lower() != ".epub" or not path.is_file():
            raise ValueError(f"Not an EPUB file: {path.name}")
        validate(path)
        message.add_attachment(
            path.read_bytes(), maintype="application", subtype="epub+zip", filename=path.name
        )
    if len(message.as_bytes()) > limit(config):
        raise ValueError(
            "Encoded email exceeds the configured message size limit; send fewer books"
        )
    return message


def limit(config):
    return min(config["max_bytes"], 50_000_000)


def connect(config):
    context = ssl.create_default_context()
    arguments = {"host": config["host"], "port": config["port"], "timeout": config["timeout"]}
    if config["mode"] == "ssl":
        connection = smtplib.SMTP_SSL(**arguments, context=context)
    else:
        connection = smtplib.SMTP(**arguments)
    try:
        if config["mode"] == "starttls":
            connection.ehlo()
            connection.starttls(context=context)
            connection.ehlo()
        if config["username"]:
            connection.login(config["username"], config["password"])
    except BaseException:
        connection.close()
        raise
    return connection


def transmit(config, message):
    """Send one step at a time so the outcome is known.

    Returns (status, outcome, error). status is "sent", "partial" (some recipients
    refused), "failed" (the server did not accept the message), or "uncertain"
    (the connection broke after the message was handed over).
    """
    outcome = {}
    try:
        smtp = connect(config)
    except smtplib.SMTPAuthenticationError:
        return "failed", outcome, "SMTP authentication failed"
    except (OSError, smtplib.SMTPException) as error:
        return "failed", outcome, type(error).__name__
    with smtp:
        try:
            code, _ = smtp.mail(config["sender"])
            if code != 250:
                return "failed", outcome, f"Sender refused ({code})"
            for address in config["recipients"]:
                code, _ = smtp.rcpt(address)
                outcome[address] = "accepted" if code in (250, 251) else f"refused ({code})"
            if not any(v == "accepted" for v in outcome.values()):
                return "failed", outcome, "Every recipient was refused"
        except (OSError, smtplib.SMTPException) as error:
            return "failed", outcome, type(error).__name__
        try:
            smtp.data(message.as_bytes())
        except smtplib.SMTPDataError as error:
            return "failed", outcome, f"Message refused ({error.smtp_code})"
        except (OSError, smtplib.SMTPException) as error:
            return "uncertain", outcome, type(error).__name__
    if all(v == "accepted" for v in outcome.values()):
        return "sent", outcome, ""
    return "partial", outcome, "Some recipients were refused"


def deliver(config, message):
    status, _, error = transmit(config, message)
    if status == "partial":
        raise ValueError(
            "SMTP server refused one or more recipients; other recipients may have been accepted"
        )
    if status == "uncertain":
        raise ValueError("Connection lost after sending; the email may have been delivered")
    if status != "sent":
        raise ValueError(error)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="*", type=Path)
    parser.add_argument(
        "--send", action="store_true", help="Actually send the email; otherwise validate locally"
    )
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument(
        "--to", help="Comma-separated configured email addresses or their numbers, such as 1,3"
    )
    choice.add_argument(
        "--all-recipients", action="store_true", help="Select every configured Kindle address"
    )
    choice.add_argument(
        "--list-recipients",
        action="store_true",
        help="List configured addresses without connecting to SMTP",
    )
    args = parser.parse_args(argv)
    try:
        env_file = default_env_file()
        if not env_file.is_file():
            raise ValueError("Create .env from .env.example and fill in your settings")
        config = settings(env_file)
        if args.list_recipients:
            for index, address in enumerate(config["recipients"], 1):
                print(f"{index}. {address}")
            return
        config = select_recipients(config, args.to, args.all_recipients)
        files = args.files or [ROOT / "output" / "book.epub"]
        message = compose(config, files)
        print(
            f"Prepared {len(files)} EPUB attachment(s), {len(message.as_bytes()):,} encoded bytes"
        )
        print("Selected recipients: " + ", ".join(config["recipients"]))
        if args.send:
            deliver(config, message)
            print(
                "SMTP server accepted the email. Kindle processing and delivery occur separately."
            )
        else:
            print("Local check passed. Use --send to transmit the email.")
    except (ValueError, OSError, BadZipFile, KeyError, ParseError, smtplib.SMTPException) as error:
        # Avoid logging SMTP replies or configuration values that may contain secrets.
        detail = str(error) if isinstance(error, ValueError) else type(error).__name__
        parser.exit(1, f"Email failed: {clean(detail)}\n")


if __name__ == "__main__":
    main()
