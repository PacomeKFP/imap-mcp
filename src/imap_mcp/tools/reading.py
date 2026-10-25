"""Search, read and download tools."""
from __future__ import annotations

import re
from dataclasses import asdict
from pathlib import Path

from mcp.server.mcpserver import Context

from .. import imap, mime, rights
from ..app import in_thread, mcp
from ..config import settings
from ..errors import MailError
from ..permissions import guard
from .common import load_message, unique_path

_IMAP_DATE = re.compile(r"\d{1,2}-[A-Za-z]{3}-\d{4}")
_UNSAFE_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


@mcp.tool()
async def search_mails(ctx: Context, folder: str = "INBOX", text: str = "", sender: str = "", to: str = "",
                       cc: str = "", subject: str = "", body: str = "", since: str = "", before: str = "",
                       unread: bool | None = None, flagged: bool | None = None,
                       larger_kb: int = 0, smaller_kb: int = 0, limit: int = 20, offset: int = 0) -> dict:
    """Multi-criteria search, newest first.

    text searches headers and body. since/before use DD-Mon-YYYY (e.g. 01-Oct-2026).
    unread / flagged: True or False to filter, None to ignore.
    Paging with limit (max 100) and offset. Returns total and results (uid, date, from, to, subject, flags, size).
    """
    await guard.authorize(ctx, "mail.read", targets=[folder])
    return await in_thread(
        _search_mails, folder, text, sender, to, cc, subject, body, since, before,
        unread, flagged, larger_kb, smaller_kb, limit, offset)


def _search_mails(folder: str, text: str, sender: str, to: str, cc: str, subject: str, body: str,
                  since: str, before: str, unread: bool | None, flagged: bool | None,
                  larger_kb: int, smaller_kb: int, limit: int, offset: int) -> dict:
    limit = max(1, min(limit, 100))
    criteria: list[str] = []
    for key, value in (("TEXT", text), ("FROM", sender), ("TO", to), ("CC", cc),
                       ("SUBJECT", subject), ("BODY", body)):
        if value:
            criteria += [key, imap.search_literal(value)]
    if since:
        criteria += ["SINCE", _imap_date(since, "since")]
    if before:
        criteria += ["BEFORE", _imap_date(before, "before")]
    if unread is not None:
        criteria.append("UNSEEN" if unread else "SEEN")
    if flagged is not None:
        criteria.append("FLAGGED" if flagged else "UNFLAGGED")
    if larger_kb:
        criteria += ["LARGER", str(larger_kb * 1024)]
    if smaller_kb:
        criteria += ["SMALLER", str(smaller_kb * 1024)]
    with imap.session(folder) as conn:
        uids = imap.search_uids(conn, criteria)[::-1]
        page = uids[offset:offset + limit]
        return {
            "folder": folder,
            "total": len(uids),
            "offset": offset,
            "results": imap.fetch_summaries(conn, page),
        }


def _imap_date(value: str, name: str) -> str:
    if value and not _IMAP_DATE.fullmatch(value):
        raise MailError(f"{name} must look like DD-Mon-YYYY (e.g. 01-Oct-2026).", code="invalid_argument")
    return value


@mcp.tool()
async def read_mail(ctx: Context, uid: str, folder: str = "INBOX", include_html: bool = False,
                    mark_read: bool = False) -> dict:
    """Read a mail: full headers, text body and attachments (use the index with download_attachment).

    Does not mark the message as read unless mark_read=True.
    """
    await guard.authorize(ctx, "mail.update" if mark_read else "mail.read", targets=[folder])
    return await in_thread(_read_mail, uid, folder, include_html, mark_read)


def _read_mail(uid: str, folder: str, include_html: bool, mark_read: bool) -> dict:
    with imap.session(folder, readonly=not mark_read) as conn:
        raw, flags = imap.fetch_raw(conn, uid)
        if mark_read:
            imap.store(conn, imap.uid_set([uid]), r"\Seen", True)
    msg = mime.parse(raw)
    content = mime.read_content(msg)
    info = mime.summary(uid, msg, flags)
    info.update(
        from_list=mime.addresses(msg["From"]),
        to_list=mime.addresses(msg["To"]),
        cc_list=mime.addresses(msg["Cc"]),
        reply_to=mime.addresses(msg["Reply-To"]),
        message_id=mime.text_header(msg, "Message-ID"),
        in_reply_to=mime.text_header(msg, "In-Reply-To"),
        references=mime.text_header(msg, "References"),
        body=mime.excerpt(content.text, settings.max_body),
        attachments=[asdict(att) for att in content.attachments],
    )
    if include_html:
        info["html"] = mime.excerpt(content.html, settings.max_body)
    return info


@mcp.tool()
async def get_thread(ctx: Context, uid: str, folder: str = "INBOX") -> list[dict]:
    """Conversation thread of a message (Message-ID/References) across the folder and Sent, oldest first."""
    await guard.authorize(ctx, "mail.read", targets=[folder])
    return await in_thread(_get_thread, uid, folder)


def _get_thread(uid: str, folder: str) -> list[dict]:
    with imap.session(folder) as conn:
        raw, _ = imap.fetch_raw(conn, uid)
        sent = imap.special_folder(conn, "sent")
    msg = mime.parse(raw)
    header_values = " ".join(str(msg.get(h) or "")
                             for h in ("Message-ID", "References", "In-Reply-To"))
    ids = set(re.findall(r"<[^>]+>", header_values))
    thread: list[dict] = []
    for name in dict.fromkeys([folder, sent]):
        with imap.session(name) as conn:
            uids: set[str] = set()
            for message_id in ids:
                for field in ("Message-ID", "References", "In-Reply-To"):
                    uids.update(imap.search_uids(
                        conn, ["HEADER", field, imap.search_literal(message_id)]))
            thread += [{"folder": name, **item}
                       for item in imap.fetch_summaries(conn, sorted(uids))]
    return sorted(thread, key=lambda item: item["date"])


@mcp.tool()
async def download_attachment(ctx: Context, uid: str, index: int, folder: str = "INBOX",
                              dest_dir: str = "") -> dict:
    """Save an attachment to disk (index from read_mail). Default folder: Downloads/imap-mcp."""
    await guard.authorize(ctx, "mail.read", targets=[folder])
    target_dir = (Path(dest_dir).expanduser() if dest_dir else settings.download_dir).resolve()
    inside = target_dir.is_relative_to(settings.download_dir.resolve())
    await guard.authorize(ctx, "file.download", targets=[rights.DOWNLOADS] if inside else [],
                          detail=str(target_dir))
    return await in_thread(_download_attachment, uid, index, folder, target_dir)


def _download_attachment(uid: str, index: int, folder: str, target_dir: Path) -> dict:
    part = mime.get_part(load_message(uid, folder), index)
    if part is None:
        raise MailError(f"No attachment at index {index}.", code="not_found")
    payload = part.get_payload(decode=True) or b""
    target_dir.mkdir(parents=True, exist_ok=True)
    name = _UNSAFE_FILENAME_CHARS.sub("_", mime.decode_text(
        part.get_filename()) or f"attachment-{index}").strip()
    target = unique_path(target_dir / (name or "attachment"))
    target.write_bytes(payload)
    return {"path": str(target), "size": len(payload), "type": part.get_content_type()}
