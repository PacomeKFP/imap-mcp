"""Helpers shared by the tool modules: loading, storing and delivering messages."""
from __future__ import annotations

from email.message import EmailMessage
from pathlib import Path

from mcp.server.mcpserver import Context

from .. import imap, mime, smtp
from ..config import get_credentials
from ..permissions import guard


def sender() -> str:
    return get_credentials()[0]


async def authorize_attachments(ctx: Context, attachments: list[str] | None) -> None:
    """Check file.read on the local files a tool is asked to attach."""
    if attachments:
        await guard.authorize(ctx, "file.read", detail=", ".join(attachments))


def drafts_folder() -> str:
    with imap.session() as conn:
        return imap.special_folder(conn, "drafts")


def load_message(uid: str, folder: str) -> EmailMessage:
    with imap.session(folder) as conn:
        raw, _ = imap.fetch_raw(conn, uid)
    return mime.parse(raw)


def save_draft(msg: EmailMessage) -> tuple[str, str | None]:
    with imap.session() as conn:
        drafts = imap.special_folder(conn, "drafts")
        imap.append(conn, drafts, msg, r"\Draft")
        uid = imap.uid_by_message_id(conn, drafts, str(msg["Message-ID"]))
    return drafts, uid


def save_to_sent(msg: EmailMessage) -> None:
    with imap.session() as conn:
        sent = imap.special_folder(conn, "sent")
        imap.append(conn, sent, msg, r"\Seen")


def deliver(msg: EmailMessage, recipients: list[str], save_copy: bool) -> None:
    smtp.send(msg, recipients)
    if save_copy:
        save_to_sent(msg)


def unique_path(path: Path) -> Path:
    candidate, number = path, 1
    while candidate.exists():
        candidate = path.with_name(f"{path.stem}-{number}{path.suffix}")
        number += 1
    return candidate
