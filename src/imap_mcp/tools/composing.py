"""Draft and send tools. Drafts are reversible; sending is not."""

from __future__ import annotations

from dataclasses import dataclass
from email.message import EmailMessage, Message
from typing import Any

from mcp.server.mcpserver import Context

from .. import imap, mime, rights, smtp
from ..app import in_thread, mcp
from ..compose import attach_files, build_message, copy_attachments
from ..config import settings
from ..content import Attachment, get_part, part_bytes, read_content
from ..errors import MailError
from ..permissions import guard
from .common import (
    after_send,
    authorize_attachments,
    deliver,
    drafts_folder,
    save_draft,
    save_to_sent,
    sender,
    with_warnings,
)

_TRASH_STEP = "moving the draft to the Agent Trash"
_SENT_STEP = "saving a copy to Sent"


@dataclass(frozen=True)
class DraftEdits:
    """Draft fields to change; None keeps the current value."""

    to: list[str] | None = None
    subject: str | None = None
    body: str | None = None
    cc: list[str] | None = None
    bcc: list[str] | None = None
    # added to the attachments the draft already has
    attachments: list[str] | None = None


def _build_message(
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None,
    bcc: list[str] | None,
    html: str | None,
    attachments: list[str] | None,
) -> EmailMessage:
    return build_message(
        sender(), to, subject, body, cc=cc, bcc=bcc, html=html, attachments=attachments
    )


@mcp.tool()
async def create_draft(
    ctx: Context,
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    html: str | None = None,
    attachments: list[str] | None = None,
) -> dict[str, Any]:
    """Create a draft in Drafts. Attachments are local file paths. Returns the draft uid."""
    await authorize_attachments(ctx, attachments)
    await guard.authorize(ctx, "mail.create", targets=[rights.DRAFTS])
    msg = await in_thread(_build_message, to, subject, body, cc, bcc, html, attachments)
    drafts, uid = await in_thread(save_draft, msg)
    return {"folder": drafts, "uid": uid, "message_id": str(msg["Message-ID"])}


@mcp.tool()
async def update_draft(
    ctx: Context,
    uid: str,
    to: list[str] | None = None,
    subject: str | None = None,
    body: str | None = None,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    attachments: list[str] | None = None,
) -> dict[str, Any]:
    """Edit a draft: given fields replace the old values, others are kept.
    Existing attachments are kept; `attachments` adds new ones.
    The old version goes to the Agent Trash. Returns the new uid."""
    await authorize_attachments(ctx, attachments)
    await guard.authorize(ctx, "mail.update", targets=[rights.DRAFTS])
    await guard.authorize(ctx, "mail.delete", targets=[rights.DRAFTS])
    edits = DraftEdits(to, subject, body, cc, bcc, attachments)
    return await in_thread(_update_draft, uid, edits)


def _update_draft(uid: str, edits: DraftEdits) -> dict[str, Any]:
    drafts = drafts_folder()
    with imap.session(drafts, readonly=False) as conn:
        raw, _ = imap.fetch_raw(conn, uid)
        msg = _revised_draft(mime.parse(raw), edits)
        imap.append(conn, drafts, msg, r"\Draft")
        imap.ensure_folder(conn, settings.agent_trash)
        imap.move(conn, imap.uid_set([uid]), settings.agent_trash)
        new_uid = imap.uid_by_message_id(conn, drafts, str(msg["Message-ID"]))
    return {"folder": drafts, "uid": new_uid}


def _revised_draft(old: EmailMessage, edits: DraftEdits) -> EmailMessage:
    """Build the new version of a draft; fields left as None come from the old message."""
    current = read_content(old)
    html = None if edits.body is not None else (current.html or None)
    msg = build_message(
        sender(),
        edits.to if edits.to is not None else mime.emails(old["To"]),
        edits.subject if edits.subject is not None else mime.decode_text(old["Subject"]),
        edits.body if edits.body is not None else current.text,
        cc=edits.cc if edits.cc is not None else mime.emails(old["Cc"]),
        bcc=edits.bcc if edits.bcc is not None else mime.emails(old["Bcc"]),
        html=html,
        in_reply_to=mime.text_header(old, "In-Reply-To"),
        references=mime.text_header(old, "References"),
    )
    html_text = html or ""
    orphans = [a for a in current.attachments if _is_orphan_inline(a, html_text)]
    copy_attachments(old, msg, [a for a in current.attachments if a not in orphans])
    _keep_inline_parts(old, msg, orphans)
    attach_files(msg, edits.attachments)
    return msg


def _is_orphan_inline(attachment: Attachment, html: str) -> bool:
    """An inline image that the HTML body no longer references, e.g. after a body edit."""
    return (
        attachment.inline
        and bool(attachment.content_id)
        and not attachment.content_type.startswith("message/")
        and f"cid:{attachment.content_id}".lower() not in html.lower()
    )


def _keep_inline_parts(
    source: Message, target: EmailMessage, attachments: list[Attachment]
) -> None:
    """Re-add inline images with their Content-ID and inline disposition, not as plain attachments."""
    for attachment in attachments:
        part = get_part(source, attachment.index)
        if part is None:
            continue
        maintype, _, subtype = attachment.content_type.partition("/")
        target.add_attachment(
            part_bytes(part),
            maintype=maintype or "application",
            subtype=subtype or "octet-stream",
            filename=attachment.filename,
            disposition="inline",
            cid=f"<{attachment.content_id}>",
        )


@mcp.tool()
async def send_mail(
    ctx: Context,
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    html: str | None = None,
    attachments: list[str] | None = None,
    save_copy: bool = True,
) -> dict[str, Any]:
    """Send a mail over SMTP and keep a copy in Sent.
    Irreversible: check recipients and content first."""
    if not (to or cc or bcc):
        raise MailError("At least one recipient is required.", code="no_recipients")
    await authorize_attachments(ctx, attachments)
    msg = await in_thread(_build_message, to, subject, body, cc, bcc, html, attachments)
    recipients = mime.all_recipients(msg)
    await guard.authorize(ctx, "mail.send", recipients=recipients)
    warnings = await in_thread(deliver, msg, recipients, save_copy)
    return with_warnings(
        {"sent": True, "message_id": str(msg["Message-ID"]), "recipients": recipients}, warnings
    )


@mcp.tool()
async def send_draft(ctx: Context, uid: str, save_copy: bool = True) -> dict[str, Any]:
    """Send an existing draft, then move it to the Agent Trash. Irreversible."""
    await guard.authorize(ctx, "mail.read", targets=[rights.DRAFTS])
    msg, recipients, drafts = await in_thread(_load_draft, uid)
    await guard.authorize(ctx, "mail.send", recipients=recipients)
    await guard.authorize(ctx, "mail.delete", targets=[rights.DRAFTS])
    warnings = await in_thread(_send_draft, msg, recipients, drafts, uid, save_copy)
    return with_warnings(
        {"sent": True, "message_id": str(msg["Message-ID"]), "recipients": recipients}, warnings
    )


def _load_draft(uid: str) -> tuple[EmailMessage, list[str], str]:
    drafts = drafts_folder()
    with imap.session(drafts) as conn:
        raw, _ = imap.fetch_raw(conn, uid)
    msg = mime.parse(raw)
    recipients = mime.all_recipients(msg)
    if not recipients:
        raise MailError("The draft has no recipient.", code="no_recipients")
    return msg, recipients, drafts


def _send_draft(
    msg: EmailMessage, recipients: list[str], drafts: str, uid: str, save_copy: bool
) -> list[str]:
    """Send the draft, then trash it and copy it to Sent over one connection.

    Returns the post-send warnings.
    """
    smtp.send(msg, recipients)
    warnings: list[str] = []

    def mailbox_steps() -> None:
        with imap.session(drafts, readonly=False) as conn:
            after_send(warnings, _TRASH_STEP, lambda: _move_to_trash(conn, uid))
            if save_copy:
                after_send(warnings, _SENT_STEP, lambda: save_to_sent(msg, conn))

    after_send(warnings, "updating the mailbox", mailbox_steps)
    return warnings


def _move_to_trash(conn: imap.Connection, uid: str) -> None:
    """Move the draft to the Agent Trash; `conn` must have the drafts folder selected."""
    imap.ensure_folder(conn, settings.agent_trash)
    imap.move(conn, imap.uid_set([uid]), settings.agent_trash)
