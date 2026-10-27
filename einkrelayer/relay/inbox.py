"""The shared IMAP mailbox: list submissions and file them under Processed."""

import imaplib
import re
import ssl


class Mailbox:
    def __init__(self, config, connection=None):
        self.config = config
        if connection is None:
            connection = imaplib.IMAP4_SSL(
                config.imap_host,
                config.imap_port,
                ssl_context=ssl.create_default_context(),
                timeout=config.timeout,
            )
        self.imap = connection
        self.imap.login(config.imap_username, config.imap_password)
        typ, _ = self.imap.select(self.quote(config.folder))
        if typ != "OK":
            raise ValueError("Could not open the IMAP folder")
        self.created = set()
        self._separator = None

    @staticmethod
    def quote(name):
        return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def separator(self):
        if self._separator is None:
            _, data = self.imap.list('""', '""')
            match = re.search(rb'\(.*?\) "(.)"', (data or [b""])[0] or b"")
            self._separator = match.group(1).decode() if match else "/"
        return self._separator

    def folder(self, *parts):
        return self.separator().join(parts)

    def uids(self):
        typ, data = self.imap.uid("SEARCH", None, "UNDELETED")
        if typ != "OK":
            raise ValueError("Could not list IMAP messages")
        return data[0].split() if data and data[0] else []

    def size(self, uid):
        typ, data = self.imap.uid("FETCH", uid, "(RFC822.SIZE)")
        match = re.search(rb"RFC822\.SIZE (\d+)", data[0] if data and data[0] else b"")
        return int(match.group(1)) if typ == "OK" and match else None

    def fetch(self, uid):
        """The raw message, without marking it read."""
        typ, data = self.imap.uid("FETCH", uid, "(BODY.PEEK[])")
        parts = [item for item in data or [] if isinstance(item, tuple)]
        if typ != "OK" or not parts:
            return None
        return parts[0][1]

    def move(self, uid, folder):
        name = self.quote(folder)
        if folder not in self.created:
            self.imap.create(name)  # an existing folder only returns NO
            self.imap.subscribe(name)
            self.created.add(folder)
        if self.optional("MOVE", uid, name) == "OK":
            return
        typ, _ = self.imap.uid("COPY", uid, name)
        if typ != "OK":
            raise ValueError("Could not file a processed message")
        self.delete(uid)

    def delete(self, uid):
        """Delete only this UID. Without UIDPLUS, leave it flagged for later cleanup.

        A plain EXPUNGE would also delete unrelated mail flagged by another client.
        """
        typ, _ = self.imap.uid("STORE", uid, "+FLAGS", r"(\Deleted)")
        if typ != "OK":
            raise ValueError("Could not delete a processed message")
        self.optional("EXPUNGE", uid)  # UIDPLUS; never use mailbox-wide EXPUNGE

    def optional(self, command, *args):
        """Run a UID command the server may not support; a refusal returns "NO".
        imaplib raises on a BAD reply, but a dropped connection still stops the run."""
        try:
            typ, _ = self.imap.uid(command, *args)
        except imaplib.IMAP4.abort:
            raise
        except imaplib.IMAP4.error:
            return "NO"
        return typ

    def close(self):
        try:
            self.imap.logout()
        except (OSError, imaplib.IMAP4.error):
            pass
