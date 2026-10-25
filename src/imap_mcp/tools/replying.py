"""Reply and forward tools. They create a draft unless send=True."""
from __future__ import annotations

from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any

from mcp.server.mcpserver import Context

from .. import imap, mime, rights
from ..app import in_thread, mcp
from ..compose import build_message, copy_attachments, prefixed, quote_reply
from ..content import read_content
from ..permissions import guard
from .common import (
    after_send,
    authorize_attachments,
    deliver,
    load_message,
    save_draft,
    sender,
    with_warnings,
)


@dataclass(frozen=True)
class _Reply:
    message: EmailMessage
    to: list[str]
    cc: list[str]
    subject: str


@mcp.tool()
async def reply_mail(ctx: Context, uid: str, body: str, folder: str = "INBOX",
                     reply_all: bool = False, send: bool = False, html: str | None = None,
                     attachments: list[str] | None = None, include_quote: bool = True) -> dict[str, Any]:
    """Reply to a mail (In-Reply-To, References and 'Re:' handled). Creates a DRAFT by default;
    send=True sends now. reply_all=True keeps the other recipients in copy."""
    await guard.authorize(ctx, "mail.read", targets=[folder])
    await authorize_attachments(ctx, attachments)
    reply = await in_thread(_build_reply, uid, folder, body, reply_all, html, attachments,
                            include_quote)
    if send:
        recipients = mime.all_recipients(reply.message)
        await guard.authorize(ctx, "mail.send", recipients=recipients)
        warnings = await in_thread(_send_reply, reply.message, recipients, folder, uid)
        return with_warnings(
            {"sent": True, "to": reply.to, "cc": reply.cc, "subject": reply.subject}, warnings)
    await guard.authorize(ctx, "mail.create", targets=[rights.DRAFTS])
    drafts, draft_uid = await in_thread(save_draft, reply.message)
    return {"sent": False, "draft_folder": drafts, "draft_uid": draft_uid,
            "to": reply.to, "cc": reply.cc, "subject": reply.subject}


def _build_reply(uid: str, folder: str, body: str, reply_all: bool, html: str | None,
                 attachments: list[str] | None, include_quote: bool) -> _Reply:
    author = sender()
    original = load_message(uid, folder)
    to, cc = mime.reply_target(original, reply_all, author)
    subject = prefixed(mime.decode_text(
        original["Subject"]), "Re:", r"(?i)^(re|réf)\s*:")
    message_id = mime.text_header(original, "Message-ID")
    previous = mime.text_header(original, "References")
    references = " ".join(value for value in (previous, message_id) if value)
    quoted = quote_reply(original, read_content(
        original).text) if include_quote else ""
    message = build_message(author, to, subject, body + quoted, cc=cc, html=html,
                            attachments=attachments, in_reply_to=message_id, references=references)
    return _Reply(message, to, cc, subject)


def _send_reply(msg: EmailMessage, recipients: list[str], folder: str, uid: str) -> list[str]:
    """Send the reply, then flag the original as answered. Returns the post-send warnings."""
    warnings = deliver(msg, recipients, save_copy=True)
    after_send(warnings, "marking the original mail as answered",
               lambda: _mark_answered(folder, uid))
    return warnings


def _mark_answered(folder: str, uid: str) -> None:
    with imap.session(folder, readonly=False) as conn:
        imap.store(conn, imap.uid_set([uid]), r"\Answered \Seen", True)


@mcp.tool()
async def forward_mail(ctx: Context, uid: str, to: list[str], body: str = "",
                       folder: str = "INBOX", cc: list[str] | None = None, send: bool = False,
                       include_attachments: bool = True) -> dict[str, Any]:
    """Forward a mail with its body and, by default, its attachments.
    Creates a DRAFT unless send=True."""
    await guard.authorize(ctx, "mail.read", targets=[folder])
    msg, subject = await in_thread(_build_forward, uid, folder, to, body, cc, include_attachments)
    if send:
        recipients = mime.all_recipients(msg)
        await guard.authorize(ctx, "mail.send", recipients=recipients)
        warnings = await in_thread(deliver, msg, recipients, True)
        return with_warnings({"sent": True, "to": to, "subject": subject}, warnings)
    await guard.authorize(ctx, "mail.create", targets=[rights.DRAFTS])
    drafts, draft_uid = await in_thread(save_draft, msg)
    return {"sent": False, "draft_folder": drafts, "draft_uid": draft_uid,
            "to": to, "subject": subject}


def _build_forward(uid: str, folder: str, to: list[str], body: str, cc: list[str] | None,
                   include_attachments: bool) -> tuple[EmailMessage, str]:
    original = load_message(uid, folder)
    content = read_content(original)
    subject = prefixed(mime.decode_text(
        original["Subject"]), "Fwd:", r"(?i)^(tr|fwd?)\s*:")
    text = body + _forward_header(original) + content.text
    msg = build_message(sender(), to, subject, text, cc=cc)
    if include_attachments:
        copy_attachments(original, msg, content.attachments)
    return msg, subject


def _forward_header(original: EmailMessage) -> str:
    # The forward header is mail content, kept in French on purpose.
    return (
        "\n\n---------- Message transféré ----------\n"
        f"De : {mime.decode_text(original['From'])}\n"
        f"Date : {mime.decode_text(original['Date'])}\n"
        f"Objet : {mime.decode_text(original['Subject'])}\n"
        f"À : {mime.decode_text(original['To'])}\n\n"
    )
