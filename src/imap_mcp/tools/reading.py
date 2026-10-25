"""Search, read and download tools."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date
from email.message import Message
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import Context

from .. import imap, mime, rights
from ..app import in_thread, mcp
from ..config import settings
from ..content import excerpt, get_part, payload_bytes, read_content
from ..errors import MailError
from ..permissions import guard
from .common import load_message, unique_path

_BYTES_PER_KB = 1024
_THREAD_FIELDS = ("Message-ID", "References", "In-Reply-To")
_MAX_PAGE_SIZE = 100
_MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024
_IMAP_DATE = re.compile(r"([0-9]{1,2})-([A-Za-z]{3})-([0-9]{4})")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_UNSAFE_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_FILENAMES = frozenset({
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
})


@dataclass(frozen=True)
class SearchQuery:
    """Criteria of search_mails. Empty strings, None and zero mean no filter."""

    folder: str
    text: str
    sender: str
    to: str
    cc: str
    subject: str
    body: str
    since: str
    before: str
    unread: bool | None
    flagged: bool | None
    larger_kb: int
    smaller_kb: int
    limit: int
    offset: int


@mcp.tool()
async def search_mails(ctx: Context, folder: str = "INBOX", text: str = "", sender: str = "", to: str = "",
                       cc: str = "", subject: str = "", body: str = "", since: str = "", before: str = "",
                       unread: bool | None = None, flagged: bool | None = None,
                       larger_kb: int = 0, smaller_kb: int = 0, limit: int = 20, offset: int = 0) -> dict[str, Any]:
    """Multi-criteria search, newest first.

    text searches headers and body. since/before use DD-Mon-YYYY (e.g. 01-Oct-2026).
    unread / flagged: True or False to filter, None to ignore.
    Paging with limit (max 100) and offset. Returns total and results (uid, date, from, to, subject, flags, size).
    """
    await guard.authorize(ctx, "mail.read", targets=[folder])
    query = SearchQuery(folder, text, sender, to, cc, subject, body, since, before,
                        unread, flagged, larger_kb, smaller_kb, limit, offset)
    return await in_thread(_search_mails, query)


def _search_mails(query: SearchQuery) -> dict[str, Any]:
    criteria = _criteria(query)  # validated before the connection opens
    limit, offset = _page(query.limit, query.offset)
    with imap.session(query.folder) as conn:
        uids = imap.search_uids(conn, criteria)[::-1]
        page = uids[offset:offset + limit]
        return {
            "folder": query.folder,
            "total": len(uids),
            "offset": offset,
            "results": imap.fetch_summaries(conn, page),
        }


def _page(limit: int, offset: int) -> tuple[int, int]:
    """Limit clamped to 1..100; a negative offset is refused."""
    if offset < 0:
        raise MailError("offset must be 0 or greater.", code="invalid_argument")
    return max(1, min(limit, _MAX_PAGE_SIZE)), offset


def _criteria(query: SearchQuery) -> list[str]:
    return _header_criteria(query) + _state_criteria(query)


def _header_criteria(query: SearchQuery) -> list[str]:
    criteria: list[str] = []
    for key, value in (("TEXT", query.text), ("FROM", query.sender), ("TO", query.to),
                       ("CC", query.cc), ("SUBJECT", query.subject), ("BODY", query.body)):
        if value:
            criteria += [key, imap.search_literal(value)]
    if query.since:
        criteria += ["SINCE", _imap_date(query.since, "since")]
    if query.before:
        criteria += ["BEFORE", _imap_date(query.before, "before")]
    return criteria


def _state_criteria(query: SearchQuery) -> list[str]:
    criteria: list[str] = []
    if query.unread is not None:
        criteria.append("UNSEEN" if query.unread else "SEEN")
    if query.flagged is not None:
        criteria.append("FLAGGED" if query.flagged else "UNFLAGGED")
    if query.larger_kb:
        criteria += ["LARGER", str(query.larger_kb * _BYTES_PER_KB)]
    if query.smaller_kb:
        criteria += ["SMALLER", str(query.smaller_kb * _BYTES_PER_KB)]
    return criteria


def _imap_date(value: str, name: str) -> str:
    """Normalized IMAP date (D-Mon-YYYY) of a real calendar day; MailError otherwise."""
    match = _IMAP_DATE.fullmatch(value)
    month = match.group(2).title() if match else ""
    if match is None or month not in _MONTHS:
        raise _bad_date(name)
    day, year = int(match.group(1)), int(match.group(3))
    try:
        date(year, _MONTHS.index(month) + 1, day)
    except ValueError:
        raise _bad_date(name) from None
    return f"{day}-{month}-{year}"


def _bad_date(name: str) -> MailError:
    return MailError(f"{name} must be a real date like DD-Mon-YYYY (e.g. 01-Oct-2026).",
                     code="invalid_argument")


@mcp.tool()
async def read_mail(ctx: Context, uid: str, folder: str = "INBOX", include_html: bool = False,
                    mark_read: bool = False) -> dict[str, Any]:
    """Read a mail: full headers, text body and attachments (use the index with download_attachment).

    Does not mark the message as read unless mark_read=True.
    """
    await guard.authorize(ctx, "mail.update" if mark_read else "mail.read", targets=[folder])
    return await in_thread(_read_mail, uid, folder, include_html, mark_read)


def _read_mail(uid: str, folder: str, include_html: bool, mark_read: bool) -> dict[str, Any]:
    with imap.session(folder, readonly=not mark_read) as conn:
        raw, flags = imap.fetch_raw(conn, uid)
        if mark_read:
            imap.store(conn, imap.uid_set([uid]), r"\Seen", True)
    msg = mime.parse(raw)
    content = read_content(msg)
    info = mime.summary(uid, msg, flags)
    info.update(
        from_list=mime.addresses(msg["From"]),
        to_list=mime.addresses(msg["To"]),
        cc_list=mime.addresses(msg["Cc"]),
        reply_to=mime.addresses(msg["Reply-To"]),
        message_id=mime.text_header(msg, "Message-ID"),
        in_reply_to=mime.text_header(msg, "In-Reply-To"),
        references=mime.text_header(msg, "References"),
        body=excerpt(content.text, settings.max_body),
        attachments=[asdict(att) for att in content.attachments],
    )
    if include_html:
        info["html"] = excerpt(content.html, settings.max_body)
    return info


@mcp.tool()
async def get_thread(ctx: Context, uid: str, folder: str = "INBOX") -> list[dict[str, Any]]:
    """Conversation thread of a message (Message-ID/References) across the folder and Sent, oldest first."""
    await guard.authorize(ctx, "mail.read", targets=[folder])
    return await in_thread(_get_thread, uid, folder)


def _get_thread(uid: str, folder: str) -> list[dict[str, Any]]:
    with imap.session(folder) as conn:
        raw, _ = imap.fetch_raw(conn, uid)
        sent = imap.special_folder(conn, "sent")
    message_ids = _message_ids(mime.parse(raw))
    thread: list[dict[str, Any]] = []
    for name in dict.fromkeys([folder, sent]):
        with imap.session(name) as conn:
            thread += _thread_items(conn, name, message_ids)
    return sorted(thread, key=lambda item: mime.sort_key(item["date"]))


def _message_ids(msg: Message) -> set[str]:
    """Message-IDs in the headers of a message: its own and those of its parents."""
    header_values = " ".join(str(msg.get(field) or "")
                             for field in _THREAD_FIELDS)
    return set(re.findall(r"<[^>]+>", header_values))


def _thread_items(conn: imap.Connection, name: str, message_ids: set[str]) -> list[dict[str, Any]]:
    uids: set[str] = set()
    for message_id in message_ids:
        for field in _THREAD_FIELDS:
            uids.update(imap.search_uids(
                conn, ["HEADER", field, imap.search_literal(message_id)]))
    return [{"folder": name, **item} for item in imap.fetch_summaries(conn, sorted(uids))]


@mcp.tool()
async def download_attachment(ctx: Context, uid: str, index: int, folder: str = "INBOX",
                              dest_dir: str = "") -> dict[str, Any]:
    """Save an attachment to disk (index from read_mail). Default folder: Downloads/imap-mcp."""
    await guard.authorize(ctx, "mail.read", targets=[folder])
    target_dir, inside = await in_thread(_resolve_target_dir, dest_dir)
    await guard.authorize(ctx, "file.download", targets=[rights.DOWNLOADS] if inside else [],
                          detail=str(target_dir))
    return await in_thread(_download_attachment, uid, index, folder, target_dir)


def safe_filename(name: str, fallback: str) -> str:
    """File name that is valid on Windows: no separators or reserved characters, no trailing
    dots or spaces, and no reserved device name (CON, COM1...) with or without an extension."""
    cleaned = _UNSAFE_FILENAME_CHARS.sub("_", name).strip().rstrip(". ")
    if not cleaned:
        return fallback
    stem = cleaned.split(".", 1)[0].strip().upper()
    return "_" + cleaned if stem in _RESERVED_FILENAMES else cleaned


def _resolve_target_dir(dest_dir: str) -> tuple[Path, bool]:
    """Resolved download directory, and whether it lies inside the default download folder."""
    base = settings.download_dir.resolve()
    target = (Path(dest_dir).expanduser() if dest_dir else base).resolve()
    return target, target.is_relative_to(base)


def _download_attachment(uid: str, index: int, folder: str, target_dir: Path) -> dict[str, Any]:
    part = get_part(load_message(uid, folder), index)
    if part is None:
        raise MailError(f"No attachment at index {index}.", code="not_found")
    payload = payload_bytes(part)
    if len(payload) > _MAX_ATTACHMENT_BYTES:
        raise MailError(f"Attachment exceeds the {_MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB limit.",
                        code="too_large")
    target_dir.mkdir(parents=True, exist_ok=True)
    name = safe_filename(mime.decode_text(part.get_filename()), f"attachment-{index}")
    target = unique_path(target_dir / name)
    target.write_bytes(payload)
    return {"path": str(target), "size": len(payload), "type": part.get_content_type()}
