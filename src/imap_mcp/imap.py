"""IMAP connection and low-level operations: folders, search, messages, flags."""

from __future__ import annotations

import imaplib
import re
import ssl
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any, cast

from . import mime, utf7
from .config import TIMEOUT, get_credentials, require, settings
from .errors import ConfigError, MailError

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
_LIST_LINE = re.compile(r'\((?P<attrs>[^)]*)\)\s+(?:"(?:[^"\\]|\\.)*"|NIL)\s+(?P<name>.+?)\s*$')
_QUOTED_ESCAPE = re.compile(r"\\(.)")
_SUMMARY_FIELDS = (
    "(UID FLAGS RFC822.SIZE BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE MESSAGE-ID)])"
)
_SEARCH_ATOM = re.compile(r"[A-Za-z0-9][A-Za-z0-9.:*,\-]*")
_SEARCH_QUOTED = re.compile(r'"(?:[^"\\\x00-\x1f\x7f]|\\[^\x00-\x1f\x7f])*"')
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SEPARATOR_LINE = re.compile(rb'\([^)]*\)\s+(?P<sep>"(?:[^"\\]|\\.)*"|NIL)')


@dataclass(frozen=True)
class Folder:
    """A mailbox from LIST: its decoded name and the attributes the server reported (e.g. \\Sent)."""

    name: str
    attributes: frozenset[str]

    @property
    def role(self) -> str | None:
        return next((attr[1:] for attr in self.attributes if attr[1:] in SPECIAL_ROLES), None)


def _reject_control(value: str) -> str:
    """Refuse control characters, which would let a value end the command line."""
    if _CONTROL.search(value):
        raise MailError(
            "Control characters are not allowed in IMAP values.", code="invalid_argument"
        )
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
    """Raise MailError(server_refused) unless the command returned OK; `action` names the step."""
    if status != "OK":
        detail = data[0].decode(errors="replace") if data and isinstance(data[0], bytes) else data
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
    conn = Connection(
        require(settings.imap_host, "MAIL_HOST"),
        settings.imap_port,
        timeout=getattr(settings, "timeout", TIMEOUT),
        ssl_context=tls_context(),
    )
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
    if status != "OK" or not data or data[0] is None:
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
    """Whether the server advertises a capability (case-insensitive), as last refreshed."""
    # Typeshed types capabilities as str, but refresh/imaplib paths can hold bytes.
    advertised: Iterable[str | bytes] = conn.capabilities
    caps = {(cap.decode() if isinstance(cap, bytes) else cap).upper() for cap in advertised}
    return name.upper() in caps


def _list_entry(line: str, literal: bytes | None) -> Folder | None:
    """Parse one LIST response; the name is a literal's bytes when literal is given."""
    match = _LIST_LINE.match(line)
    if not match:
        return None
    if literal is not None:
        raw_name = literal.decode(errors="replace")
    else:
        raw_name = match["name"]
        if len(raw_name) >= 2 and raw_name[0] == raw_name[-1] == '"':
            raw_name = _QUOTED_ESCAPE.sub(r"\1", raw_name[1:-1])
    attributes = frozenset(attr.lower() for attr in match["attrs"].split())
    return Folder(utf7.decode(raw_name), attributes)


def list_folders(conn: Connection) -> list[Folder]:
    """Folders from LIST, including rows whose name arrives as a literal (imaplib gives a tuple)."""
    _, rows = conn.list()
    folders: list[Folder] = []
    for row in rows or []:
        if isinstance(row, tuple):
            header, literal = row[0], row[1] if len(row) > 1 else b""
            line = header.decode(errors="replace")
            entry = _list_entry(re.sub(r"\{\d+\}$", '""', line), literal)
        elif isinstance(row, bytes):
            entry = _list_entry(row.decode(errors="replace"), None)
        else:
            continue
        if entry is not None:
            folders.append(entry)
    return folders


# Folder roles and the hierarchy separator change rarely, so they are cached per account.
CACHE_TTL_SECONDS = 300
_clock: Callable[[], float] = time.monotonic
_cache_lock = threading.Lock()
_CacheKey = tuple[str, str, object]
_role_cache: dict[_CacheKey, tuple[float, dict[str, str | None]]] = {}
_separator_cache: dict[_CacheKey, tuple[float, str]] = {}


def _cache_key() -> _CacheKey:
    """(IMAP host, user, connection factory) that scopes the caches.

    The user is empty when no credentials are stored. The factory is constant in production
    (``Connection``); including it keeps entries from leaking between connection doubles.
    """
    try:
        user, _ = get_credentials()
    except ConfigError:
        user = ""
    return settings.imap_host, user, Connection


def _is_fresh(stored_at: float) -> bool:
    return _clock() - stored_at < CACHE_TTL_SECONDS


def clear_caches() -> None:
    """Forget cached folder roles and separators, e.g. after folders were created or renamed."""
    with _cache_lock:
        _role_cache.clear()
        _separator_cache.clear()


def _roles_in(folders: list[Folder]) -> dict[str, str | None]:
    """Folder name of every special role: by SPECIAL-USE attribute, then by fallback name."""
    by_lower = {folder.name.lower(): folder.name for folder in folders}
    roles: dict[str, str | None] = {}
    for role, fallbacks in SPECIAL_ROLES.items():
        found = next((folder.name for folder in folders if folder.role == role), None)
        if found is None:
            found = next(
                (by_lower[name.lower()] for name in fallbacks if name.lower() in by_lower), None
            )
        roles[role] = found
    return roles


def _role_name(roles: dict[str, str | None], role: str) -> str:
    if role not in SPECIAL_ROLES:
        raise MailError(f"Unknown special folder role: {role}", code="invalid_argument")
    name = roles.get(role)
    if name is None:
        raise MailError(f"Special folder not found: {role}", code="special_folder_missing")
    return name


def special_folder(conn: Connection, role: str) -> str:
    """Special folder name (sent, drafts, trash, junk, archive), by attribute then by name.

    One LIST resolves every role, so the result also refreshes the role cache.
    """
    key = _cache_key()
    roles = _roles_in(list_folders(conn))
    with _cache_lock:
        _role_cache[key] = (_clock(), roles)
    return _role_name(roles, role)


def cached_special_folder(role: str) -> str:
    """Special folder name, served from the cache; opens a session only on a miss or after the TTL."""
    key = _cache_key()
    with _cache_lock:
        entry = _role_cache.get(key)
        if entry is not None and _is_fresh(entry[0]):
            return _role_name(entry[1], role)
    with session() as conn:
        return special_folder(conn, role)


def folder_status(conn: Connection, folder: str) -> tuple[int, int] | None:
    """(messages, unread) of a folder, or None when unavailable.

    Each count is read on its own, so the order of the STATUS items does not matter.
    """
    status, data = conn.status(quote(folder), "(MESSAGES UNSEEN)")
    if status != "OK" or not data:
        return None
    text = " ".join(
        part.decode(errors="replace")
        if isinstance(part, bytes)
        else " ".join(p.decode(errors="replace") for p in part)
        for part in data
        if part
    )
    items = re.search(r"\(([^()]*)\)\s*$", text)
    if not items:
        return None
    messages = re.search(r"\bMESSAGES\s+(\d+)\b", items[1])
    unread = re.search(r"\bUNSEEN\s+(\d+)\b", items[1])
    if not messages or not unread:
        return None
    return int(messages[1]), int(unread[1])


def _search_tokens(criteria: list[str]) -> tuple[list[str], bytes | None]:
    """Validate SEARCH criteria; return the command tokens and the non-ASCII literal, if any.

    The literal's keyword (HEADER's field name included) moves to the end of the tokens,
    so the literal, which imaplib appends after the last token, follows its keyword.
    """
    for part in criteria:
        if not (_SEARCH_ATOM.fullmatch(part) or _SEARCH_QUOTED.fullmatch(part)):
            raise MailError(f"Invalid search criterion: {part!r}", code="invalid_argument")
    literal_at = [i for i, part in enumerate(criteria) if not part.isascii()]
    if len(literal_at) > 1:
        raise MailError(
            "Only one non-ASCII search value is supported per search.", code="invalid_argument"
        )
    if not literal_at:
        return list(criteria or ["ALL"]), None
    i = literal_at[0]
    start = i - 2 if i >= 2 and criteria[i - 2].upper() == "HEADER" else i - 1
    if start < 0:
        raise MailError(
            "A non-ASCII search value needs a keyword before it.", code="invalid_argument"
        )
    literal = re.sub(r"\\(.)", r"\1", criteria[i][1:-1]).encode("utf-8")
    return criteria[:start] + criteria[i + 1 :] + criteria[start:i], literal


def _uid_command(conn: Connection, literal: bytes | None, *args: str) -> tuple[str, list[Any]]:
    """Run a UID command, sending the literal (if any) as its last argument."""
    # imaplib sends a bytes literal unchanged; typeshed declares the attribute as str.
    cast(Any, conn).literal = literal
    return conn.uid(*args)


def _uids(data: list[Any]) -> list[str]:
    """UIDs from the untagged SEARCH or SORT response."""
    return data[0].decode().split() if data and data[0] else []


def search_uids(conn: Connection, criteria: list[str], newest_first: bool = False) -> list[str]:
    """UIDs matching the SEARCH criteria, which are ANDed keyword and value tokens.

    Each token must be an atom or a quoted string without control characters. A
    non-ASCII quoted value is sent as an IMAP literal under CHARSET UTF-8. imaplib
    sends at most one literal per command, so a search may hold one such value at
    most. The value and its keyword (HEADER takes a field name too) move to the end.

    With newest_first, UIDs come newest first by date: UID SORT (REVERSE DATE) when the
    server advertises SORT, otherwise descending UID order (which follows arrival order,
    not the Date header).
    """
    tokens, literal = _search_tokens(criteria)
    if newest_first and has_capability(conn, "SORT"):
        status, data = _uid_command(conn, literal, "SORT", "(REVERSE DATE)", "UTF-8", *tokens)
        if status == "OK":
            return _uids(data)
    charset = ["CHARSET", "UTF-8"] if literal is not None else []
    _, data = _uid_command(conn, literal, "SEARCH", *charset, *tokens)
    uids = _uids(data)
    return sorted(uids, key=int, reverse=True) if newest_first else uids


_UIDVALIDITY = re.compile(r"\[UIDVALIDITY (\d+)\]", re.IGNORECASE)


def uidvalidity(conn: Connection) -> int | None:
    """UIDVALIDITY of the folder selected last, or None when the server did not report it.

    Reads the untagged "OK [UIDVALIDITY n]" response that imaplib keeps since SELECT,
    without consuming it.
    """
    responses: dict[str, list[Any]] = getattr(conn, "untagged_responses", None) or {}
    for line in reversed(responses.get("OK", [])):
        if isinstance(line, bytes) and (
            match := _UIDVALIDITY.search(line.decode(errors="replace"))
        ):
            return int(match[1])
    return None


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
    """Copy messages by UID (already validated) into another folder."""
    status, data = conn.uid("COPY", uids, quote(destination))
    expect_ok(status, data, "Copy")


def move(conn: Connection, uids: str, destination: str) -> None:
    """Moves messages by UID. Never expunges anything beyond the given UIDs."""
    if has_capability(conn, "MOVE"):
        status, data = conn.uid("MOVE", uids, quote(destination))
        expect_ok(status, data, "Move")
        return
    if not has_capability(conn, "UIDPLUS"):
        raise MailError(
            "The server supports neither MOVE nor UIDPLUS; nothing was moved.",
            code="move_unsupported",
        )
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
    with _cache_lock:
        _role_cache.pop(_cache_key(), None)


def _separator_from_row(row: Any) -> str | None:
    """Separator of one LIST row; a literal name arrives as a tuple whose first item is the header."""
    header = row[0] if isinstance(row, tuple) and row else row
    if not isinstance(header, bytes):
        return None
    match = _SEPARATOR_LINE.match(header)
    if not match or match["sep"] == b"NIL":
        return None
    return _QUOTED_ESCAPE.sub(r"\1", match["sep"][1:-1].decode(errors="replace"))


def folder_separator() -> str:
    """Hierarchy separator of the server, such as '/' or '.'. Cached like the folder roles."""
    key = _cache_key()
    with _cache_lock:
        entry = _separator_cache.get(key)
        if entry is not None and _is_fresh(entry[0]):
            return entry[1]
    with session() as conn:
        _, rows = conn.list('""', '""')
    separator = next(
        (found for row in rows or [] if (found := _separator_from_row(row)) is not None), "/"
    )
    with _cache_lock:
        _separator_cache[key] = (_clock(), separator)
    return separator


def append(conn: Connection, folder: str, message: EmailMessage, flags: str = "") -> None:
    """Append a message to a folder, stamped with the current time; `flags` is a space-separated list."""
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
