"""IMAP connection and low-level operations: folders, search, messages, flags."""
from __future__ import annotations

import imaplib
import re
import ssl
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any

from . import mime, utf7
from .config import TIMEOUT, get_credentials, require, settings
from .errors import MailError

Connection = imaplib.IMAP4_SSL
imaplib.Commands.setdefault("MOVE", ("SELECTED",))  # RFC 6851; imaplib does not know MOVE

# Role -> fallback folder names, used when the server does not expose SPECIAL-USE.
SPECIAL_ROLES: dict[str, tuple[str, ...]] = {
    "sent": ("Sent", "Éléments envoyés"),
    "drafts": ("Drafts", "Brouillons"),
    "trash": ("Trash", "Corbeille"),
    "junk": ("Junk", "Spam"),
    "archive": ("Archive", "Archives"),
}
_LIST_LINE = re.compile(
    r'\((?P<attrs>[^)]*)\)\s+(?:"[^"]*"|NIL)\s+(?P<name>.+)$')
_SUMMARY_FIELDS = "(UID FLAGS RFC822.SIZE BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE MESSAGE-ID)])"
_SEARCH_ATOM = re.compile(r"[A-Za-z0-9][A-Za-z0-9.:*,\-]*")
_SEARCH_QUOTED = re.compile(r'"(?:[^"\\\x00-\x1f\x7f]|\\[^\x00-\x1f\x7f])*"')
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SEPARATOR_LINE = re.compile(rb'\([^)]*\)\s+(?P<sep>"[^"]*"|NIL)')


@dataclass(frozen=True)
class Folder:
    name: str
    attributes: frozenset[str]

    @property
    def role(self) -> str | None:
        return next((attr[1:] for attr in self.attributes if attr[1:] in SPECIAL_ROLES), None)


def _reject_control(value: str) -> str:
    """Refuse control characters, which would let a value end the command line."""
    if _CONTROL.search(value):
        raise MailError("Control characters are not allowed in IMAP values.",
                        code="invalid_argument")
    return value


def quote(name: str) -> str:
    """Folder name in IMAP form (modified UTF-7, in double quotes)."""
    return '"' + utf7.encode(_reject_control(name)).replace("\\", "\\\\").replace('"', '\\"') + '"'


def search_literal(value: str) -> str:
    """Double-quoted string for SEARCH."""
    return '"' + _reject_control(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def uid_set(uids: Iterable[str]) -> str:
    """Validate a list of UIDs and return them in IMAP form (1,2,3)."""
    clean = [str(uid).strip() for uid in uids]
    if not clean or not all(re.fullmatch(r"\d+", uid) for uid in clean):
        raise MailError("uids must be a non-empty list of integers.", code="invalid_uids")
    return ",".join(clean)


def expect_ok(status: str, data: list[Any], action: str) -> None:
    if status != "OK":
        detail = data[0].decode(errors="replace") if data and isinstance(
            data[0], bytes) else data
        raise MailError(f"{action} refused by server: {detail}", code="server_refused")


def tls_context() -> ssl.SSLContext:
    """Verifying TLS context; unverified only when MAIL_TLS_INSECURE is set."""
    context = ssl.create_default_context()
    if settings.tls_insecure:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


@contextmanager
def session(folder: str | None = None, readonly: bool = True) -> Iterator[Connection]:
    """Open an authenticated connection and select a folder when one is given."""
    user, password = get_credentials()
    conn = Connection(require(settings.imap_host, "MAIL_HOST"),
                      settings.imap_port, timeout=TIMEOUT, ssl_context=tls_context())
    try:
        try:
            conn.login(user, password)
        except imaplib.IMAP4.error as exc:
            raise MailError(f"Authentication refused: {exc}", code="auth_failed") from exc
        refresh_capabilities(conn)
        if folder is not None:
            select(conn, folder, readonly)
        yield conn
    finally:
        with suppress(imaplib.IMAP4.error, OSError):
            conn.logout()


def select(conn: Connection, folder: str, readonly: bool = True) -> int:
    """Select a folder and return its message count."""
    status, data = conn.select(quote(folder), readonly=readonly)
    if status != "OK":
        raise MailError(f"Folder not found or not accessible: {folder}", code="folder_not_found")
    return int(data[0])


def refresh_capabilities(conn: Connection) -> None:
    """Re-read server capabilities; many servers only advertise MOVE/UIDPLUS after login.

    imaplib's capability() returns the data without updating conn.capabilities,
    so the attribute is set here. Failures keep the previously known list.
    """
    try:
        _, data = conn.capability()
    except (imaplib.IMAP4.error, OSError):
        return
    if data:
        conn.capabilities = tuple(data[-1].upper().split())


def has_capability(conn: Connection, name: str) -> bool:
    caps = {(cap.decode() if isinstance(cap, bytes) else cap).upper()
            for cap in conn.capabilities}
    return name.upper() in caps


def list_folders(conn: Connection) -> list[Folder]:
    _, rows = conn.list()
    folders: list[Folder] = []
    for row in rows or []:
        if not isinstance(row, bytes):
            continue
        match = _LIST_LINE.match(row.decode(errors="replace"))
        if match:
            name = utf7.decode(match["name"].strip().strip('"'))
            attributes = frozenset(attr.lower()
                                   for attr in match["attrs"].split())
            folders.append(Folder(name, attributes))
    return folders


def special_folder(conn: Connection, role: str) -> str:
    """Special folder name (sent, drafts, trash, junk, archive), by attribute then by name."""
    folders = list_folders(conn)
    for folder in folders:
        if folder.role == role:
            return folder.name
    by_lower = {folder.name.lower(): folder.name for folder in folders}
    for fallback in SPECIAL_ROLES[role]:
        if fallback.lower() in by_lower:
            return by_lower[fallback.lower()]
    raise MailError(f"Special folder not found: {role}", code="special_folder_missing")


def folder_status(conn: Connection, folder: str) -> tuple[int, int] | None:
    """(messages, unread) of a folder, or None when unavailable."""
    status, data = conn.status(quote(folder), "(MESSAGES UNSEEN)")
    if status != "OK" or not data or not data[0]:
        return None
    match = re.search(r"MESSAGES (\d+) UNSEEN (\d+)",
                      data[0].decode(errors="replace"))
    return (int(match[1]), int(match[2])) if match else None


def search_uids(conn: Connection, criteria: list[str]) -> list[str]:
    """UIDs matching the SEARCH criteria, which are ANDed keyword and value tokens.

    Each token must be an atom or a quoted string without control characters. A
    non-ASCII quoted value is sent as an IMAP literal under CHARSET UTF-8. imaplib
    sends at most one literal per command, so a search may hold one such value at
    most. The value and its keyword (HEADER takes a field name too) move to the end.
    """
    for part in criteria:
        if not (_SEARCH_ATOM.fullmatch(part) or _SEARCH_QUOTED.fullmatch(part)):
            raise MailError(f"Invalid search criterion: {part!r}", code="invalid_argument")
    literal_at = [i for i, part in enumerate(criteria) if not part.isascii()]
    if len(literal_at) > 1:
        raise MailError("Only one non-ASCII search value is supported per search.",
                        code="invalid_argument")
    if literal_at:
        i = literal_at[0]
        start = i - 2 if i >= 2 and criteria[i - 2].upper() == "HEADER" else i - 1
        if start < 0:
            raise MailError("A non-ASCII search value needs a keyword before it.",
                            code="invalid_argument")
        conn.literal = re.sub(r"\\(.)", r"\1", criteria[i][1:-1]).encode("utf-8")
        tokens = criteria[:start] + criteria[i + 1:] + criteria[start:i]
        _, data = conn.uid("SEARCH", "CHARSET", "UTF-8", *tokens)
    else:
        _, data = conn.uid("SEARCH", *(criteria or ["ALL"]))
    return data[0].decode().split() if data and data[0] else []


def fetch_raw(conn: Connection, uid: str) -> tuple[bytes, str]:
    """Raw message and flags, without marking the message as read."""
    _, parts = conn.uid("FETCH", uid_set([uid]), "(FLAGS BODY.PEEK[])")
    for part in parts or []:
        if isinstance(part, tuple):
            flags = re.search(rb"FLAGS \(([^)]*)\)", part[0])
            return part[1], flags[1].decode() if flags else ""
    raise MailError(f"Message not found: uid {uid}", code="not_found")


def fetch_summaries(conn: Connection, uids: list[str]) -> list[dict[str, Any]]:
    """Header summaries for a list of UIDs, in the requested order."""
    if not uids:
        return []
    _, parts = conn.uid("FETCH", ",".join(uids), _SUMMARY_FIELDS)
    found: dict[str, dict[str, Any]] = {}
    for part in parts or []:
        if not isinstance(part, tuple):
            continue
        head = part[0].decode(errors="replace")
        uid = re.search(r"UID (\d+)", head)
        if not uid:
            continue
        flags = re.search(r"FLAGS \(([^)]*)\)", head)
        size = re.search(r"RFC822\.SIZE (\d+)", head)
        found[uid[1]] = mime.summary(
            uid[1],
            mime.parse(part[1]),
            flags[1] if flags else "",
            int(size[1]) if size else None,
        )
    return [found[uid] for uid in uids if uid in found]


def store(conn: Connection, uids: str, flags: str, value: bool) -> None:
    """Add (value=True) or remove IMAP flags on UIDs that were already validated."""
    op = "+FLAGS.SILENT" if value else "-FLAGS.SILENT"
    status, data = conn.uid("STORE", uids, op, f"({flags})")
    expect_ok(status, data, "Flag update")


def copy(conn: Connection, uids: str, destination: str) -> None:
    status, data = conn.uid("COPY", uids, quote(destination))
    expect_ok(status, data, "Copy")


def move(conn: Connection, uids: str, destination: str) -> None:
    """Moves messages by UID. Never expunges anything beyond the given UIDs."""
    if has_capability(conn, "MOVE"):
        status, data = conn.uid("MOVE", uids, quote(destination))
        expect_ok(status, data, "Move")
        return
    if not has_capability(conn, "UIDPLUS"):
        raise MailError("The server supports neither MOVE nor UIDPLUS; nothing was moved.",
                        code="move_unsupported")
    copy(conn, uids, destination)
    status, data = conn.uid("STORE", uids, "+FLAGS.SILENT", r"(\Deleted)")
    expect_ok(status, data, "Move")
    status, data = conn.uid("EXPUNGE", uids)
    expect_ok(status, data, "Move")


def ensure_folder(conn: Connection, name: str) -> None:
    """Creates and subscribes to a folder if it does not exist yet."""
    if name in {folder.name for folder in list_folders(conn)}:
        return
    status, data = conn.create(quote(name))
    expect_ok(status, data, f"Create {name}")
    conn.subscribe(quote(name))


def folder_separator() -> str:
    """Hierarchy separator of the server, such as '/' or '.'."""
    with session() as conn:
        _, rows = conn.list('""', '""')
    for row in rows or []:
        match = _SEPARATOR_LINE.match(row) if isinstance(row, bytes) else None
        if match and match["sep"] != b"NIL":
            return match["sep"].decode().strip('"')
    return "/"


def append(conn: Connection, folder: str, message: EmailMessage, flags: str = "") -> None:
    status, data = conn.append(
        quote(folder),
        f"({flags})" if flags else None,
        imaplib.Time2Internaldate(time.time()),
        message.as_bytes(),
    )
    expect_ok(status, data, f"Append to {folder}")


def uid_by_message_id(conn: Connection, folder: str, message_id: str) -> str | None:
    """UID of the message with this Message-ID in the folder, or None."""
    select(conn, folder, readonly=True)
    message_id = re.sub(r"\s+", "", message_id)  # unfold a folded header; msg-ids hold no spaces
    uids = search_uids(conn, ["HEADER", "Message-ID", search_literal(message_id)])
    return uids[-1] if uids else None
