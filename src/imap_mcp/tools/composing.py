"""Compose, draft, send and reply tools. Drafts are reversible; sending is not."""
from __future__ import annotations

from email.message import EmailMessage

from mcp.server.mcpserver import Context

from .. import imap, mime, rights, smtp
from ..app import in_thread, mcp
from ..config import settings
from ..errors import MailError
from ..permissions import guard
from .common import deliver, load_message, save_draft, save_to_sent, sender


async def _check_files(ctx: Context, attachments: list[str] | None) -> None:
    if attachments:
        await guard.authorize(ctx, "file.read", detail=", ".join(attachments))


def _build(to: list[str] | None, subject: str, body: str, cc: list[str] | None,
           bcc: list[str] | None, html: str | None, attachments: list[str] | None) -> EmailMessage:
    return mime.build_message(sender(), to, subject, body, cc=cc, bcc=bcc, html=html,
                              attachments=attachments)


@mcp.tool()
async def create_draft(ctx: Context, to: list[str], subject: str, body: str, cc: list[str] | None = None,
                       bcc: list[str] | None = None, html: str | None = None,
                       attachments: list[str] | None = None) -> dict:
    """Create a draft in Drafts. Attachments are local file paths. Returns the draft uid."""
    await _check_files(ctx, attachments)
    await guard.authorize(ctx, "mail.create", targets=[rights.DRAFTS])
    msg = await in_thread(_build, to, subject, body, cc, bcc, html, attachments)
    drafts, uid = await in_thread(save_draft, msg)
    return {"folder": drafts, "uid": uid, "message_id": str(msg["Message-ID"])}


@mcp.tool()
async def update_draft(ctx: Context, uid: str, to: list[str] | None = None, subject: str | None = None,
                       body: str | None = None, cc: list[str] | None = None, bcc: list[str] | None = None,
                       attachments: list[str] | None = None) -> dict:
    """Edit a draft: given fields replace the old values, others are kept. Existing attachments are kept;
    `attachments` adds new ones. The old version goes to the Agent Trash. Returns the new uid."""
    await _check_files(ctx, attachments)
    await guard.authorize(ctx, "mail.update", targets=[rights.DRAFTS])
    return await in_thread(_update_draft, uid, to, subject, body, cc, bcc, attachments)


def _update_draft(uid: str, to: list[str] | None, subject: str | None, body: str | None,
                  cc: list[str] | None, bcc: list[str] | None, attachments: list[str] | None) -> dict:
    author = sender()
    with imap.session() as conn:
        drafts = imap.special_folder(conn, "drafts")
    with imap.session(drafts, readonly=False) as conn:
        raw, _ = imap.fetch_raw(conn, uid)
        old = mime.parse(raw)
        content = mime.read_content(old)
        msg = mime.build_message(
            author,
            to if to is not None else mime.emails(old["To"]),
            subject if subject is not None else mime.decode_text(old["Subject"]),
            body if body is not None else content.text,
            cc=cc if cc is not None else mime.emails(old["Cc"]),
            bcc=bcc if bcc is not None else mime.emails(old["Bcc"]),
            html=None if body is not None else (content.html or None),
            in_reply_to=mime.text_header(old, "In-Reply-To"),
            references=mime.text_header(old, "References"),
        )
        mime.copy_attachments(old, msg, content.attachments)
        mime.attach_files(msg, attachments)
        imap.append(conn, drafts, msg, r"\Draft")
        imap.ensure_folder(conn, settings.agent_trash)
        imap.move(conn, imap.uid_set([uid]), settings.agent_trash)
        new_uid = imap.uid_by_message_id(conn, drafts, str(msg["Message-ID"]))
    return {"folder": drafts, "uid": new_uid}


@mcp.tool()
async def send_mail(ctx: Context, to: list[str], subject: str, body: str, cc: list[str] | None = None,
                    bcc: list[str] | None = None, html: str | None = None,
                    attachments: list[str] | None = None, save_copy: bool = True) -> dict:
    """Send a mail over SMTP, keeping a copy in Sent. Irreversible: check recipients and content first."""
    if not (to or cc or bcc):
        raise MailError("At least one recipient is required.", code="no_recipients")
    await _check_files(ctx, attachments)
    msg = await in_thread(_build, to, subject, body, cc, bcc, html, attachments)
    recipients = mime.all_recipients(msg)
    await guard.authorize(ctx, "mail.send", recipients=recipients)
    await in_thread(deliver, msg, recipients, save_copy)
    return {"sent": True, "message_id": str(msg["Message-ID"]), "recipients": recipients}


@mcp.tool()
async def send_draft(ctx: Context, uid: str, save_copy: bool = True) -> dict:
    """Send an existing draft, then move it to the Agent Trash. Irreversible."""
    await guard.authorize(ctx, "mail.read", targets=[rights.DRAFTS])
    msg, recipients, drafts = await in_thread(_load_draft, uid)
    await guard.authorize(ctx, "mail.send", recipients=recipients)
    await in_thread(_send_draft, msg, recipients, drafts, uid, save_copy)
    return {"sent": True, "message_id": str(msg["Message-ID"]), "recipients": recipients}


def _load_draft(uid: str) -> tuple[EmailMessage, list[str], str]:
    with imap.session() as conn:
        drafts = imap.special_folder(conn, "drafts")
    with imap.session(drafts) as conn:
        raw, _ = imap.fetch_raw(conn, uid)
    msg = mime.parse(raw)
    recipients = mime.all_recipients(msg)
    if not recipients:
        raise MailError("The draft has no recipient.", code="no_recipients")
    return msg, recipients, drafts


def _send_draft(msg: EmailMessage, recipients: list[str], drafts: str, uid: str, save_copy: bool) -> None:
    smtp.send(msg, recipients)
    with imap.session(drafts, readonly=False) as conn:
        imap.ensure_folder(conn, settings.agent_trash)
        imap.move(conn, imap.uid_set([uid]), settings.agent_trash)
    if save_copy:
        save_to_sent(msg)


@mcp.tool()
async def reply_mail(ctx: Context, uid: str, body: str, folder: str = "INBOX", reply_all: bool = False,
                     send: bool = False, html: str | None = None, attachments: list[str] | None = None,
                     include_quote: bool = True) -> dict:
    """Reply to a mail (In-Reply-To, References and 'Re:' handled). Creates a DRAFT by default;
    send=True sends now. reply_all=True keeps the other recipients in copy."""
    await guard.authorize(ctx, "mail.read", targets=[folder])
    await _check_files(ctx, attachments)
    msg, to, cc, subject = await in_thread(
        _build_reply, uid, folder, body, reply_all, html, attachments, include_quote)
    if send:
        recipients = mime.all_recipients(msg)
        await guard.authorize(ctx, "mail.send", recipients=recipients)
        await in_thread(_send_reply, msg, recipients, folder, uid)
        return {"sent": True, "to": to, "cc": cc, "subject": subject}
    await guard.authorize(ctx, "mail.create", targets=[rights.DRAFTS])
    drafts, draft_uid = await in_thread(save_draft, msg)
    return {"sent": False, "draft_folder": drafts, "draft_uid": draft_uid,
            "to": to, "cc": cc, "subject": subject}


def _build_reply(uid: str, folder: str, body: str, reply_all: bool, html: str | None,
                 attachments: list[str] | None, include_quote: bool) -> tuple[EmailMessage, list[str], list[str], str]:
    author = sender()
    original = load_message(uid, folder)
    to, cc = mime.reply_target(original, reply_all, author)
    subject = mime.prefixed(mime.decode_text(original["Subject"]), "Re:", r"(?i)^(re|réf)\s*:")
    message_id = mime.text_header(original, "Message-ID")
    references = " ".join(x for x in (mime.text_header(original, "References"), message_id) if x)
    quoted = mime.quote_reply(original, mime.read_content(original).text) if include_quote else ""
    msg = mime.build_message(author, to, subject, body + quoted, cc=cc, html=html,
                             attachments=attachments, in_reply_to=message_id, references=references)
    return msg, to, cc, subject


def _send_reply(msg: EmailMessage, recipients: list[str], folder: str, uid: str) -> None:
    deliver(msg, recipients, save_copy=True)
    with imap.session(folder, readonly=False) as conn:
        imap.store(conn, imap.uid_set([uid]), r"\Answered \Seen", True)


@mcp.tool()
async def forward_mail(ctx: Context, uid: str, to: list[str], body: str = "", folder: str = "INBOX",
                       cc: list[str] | None = None, send: bool = False,
                       include_attachments: bool = True) -> dict:
    """Forward a mail (quoted body, original attachments kept). Creates a DRAFT by default; send=True sends."""
    await guard.authorize(ctx, "mail.read", targets=[folder])
    msg, subject = await in_thread(_build_forward, uid, folder, to, body, cc, include_attachments)
    if send:
        recipients = mime.all_recipients(msg)
        await guard.authorize(ctx, "mail.send", recipients=recipients)
        await in_thread(deliver, msg, recipients, True)
        return {"sent": True, "to": to, "subject": subject}
    await guard.authorize(ctx, "mail.create", targets=[rights.DRAFTS])
    drafts, draft_uid = await in_thread(save_draft, msg)
    return {"sent": False, "draft_folder": drafts, "draft_uid": draft_uid,
            "to": to, "subject": subject}


def _build_forward(uid: str, folder: str, to: list[str], body: str, cc: list[str] | None,
                   include_attachments: bool) -> tuple[EmailMessage, str]:
    original = load_message(uid, folder)
    content = mime.read_content(original)
    subject = mime.prefixed(mime.decode_text(original["Subject"]), "Fwd:", r"(?i)^(tr|fwd?)\s*:")
    header = (
        "\n\n---------- Message transféré ----------\n"
        f"De : {mime.decode_text(original['From'])}\n"
        f"Date : {mime.decode_text(original['Date'])}\n"
        f"Objet : {mime.decode_text(original['Subject'])}\n"
        f"À : {mime.decode_text(original['To'])}\n\n"
    )
    msg = mime.build_message(sender(), to, subject, body + header + content.text, cc=cc)
    if include_attachments:
        mime.copy_attachments(original, msg, content.attachments)
    return msg, subject
