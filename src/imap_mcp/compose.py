"""Outgoing messages: headers, text and HTML bodies, attachments and quoted replies."""

from __future__ import annotations

import mimetypes
import re
from email.message import EmailMessage, Message
from email.utils import formatdate, make_msgid
from pathlib import Path

from .content import Attachment, get_part, part_bytes
from .errors import MailError
from .mime import decode_text, parse
from .templates import quote_header


def add_bytes(msg: EmailMessage, data: bytes, content_type: str, filename: str) -> None:
    maintype, _, subtype = content_type.partition("/")
    if content_type == "message/rfc822":
        msg.add_attachment(parse(data), filename=filename)
        return
    msg.add_attachment(
        data,
        maintype=maintype or "application",
        subtype=subtype or "octet-stream",
        filename=filename,
    )


def _references_cid(html: str, content_id: str) -> bool:
    return f"cid:{content_id}".lower() in html.lower()


def copy_attachments(source: Message, target: EmailMessage, attachments: list[Attachment]) -> None:
    """Copy attachments to target. An inline part whose Content-ID the HTML body references
    (cid:) is added to that HTML part as multipart/related; every other part is a regular
    attachment."""
    html_body = target.get_body(("html",))
    html_text = str(html_body.get_content()) if html_body is not None else ""
    for attachment in attachments:
        part = get_part(source, attachment.index)
        if part is None:
            continue
        data = part_bytes(part)
        content_id = attachment.content_id
        if (
            html_body is not None
            and attachment.inline
            and content_id
            and not attachment.content_type.startswith("message/")
            and _references_cid(html_text, content_id)
        ):
            maintype, _, subtype = attachment.content_type.partition("/")
            html_body.add_related(
                data,
                maintype=maintype or "application",
                subtype=subtype or "octet-stream",
                cid=f"<{content_id}>",
                filename=attachment.filename,
                disposition="inline",
            )
        else:
            add_bytes(target, data, attachment.content_type, attachment.filename)


def attach_files(msg: EmailMessage, paths: list[str] | None) -> None:
    for raw_path in paths or []:
        path = Path(raw_path).expanduser()
        if not path.is_file():
            raise MailError(f"Attachment not found: {raw_path}", code="not_found")
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        add_bytes(msg, path.read_bytes(), content_type, path.name)


def build_message(
    sender: str,
    to: list[str],
    subject: str,
    body: str,
    *,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    html: str | None = None,
    attachments: list[str] | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    if to:
        msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if bcc:
        msg["Bcc"] = ", ".join(bcc)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    domain = sender.split("@", 1)[1] if "@" in sender else "localhost"
    msg["Message-ID"] = make_msgid(domain=domain)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    attach_files(msg, attachments)
    return msg


def quote_reply(original: Message, text: str, locale: str = "fr") -> str:
    """Quote the original text under a reply header in the given locale."""
    who = decode_text(original["From"])
    when = decode_text(original["Date"])
    quoted = "\n".join("> " + line for line in text.splitlines())
    return f"\n\n{quote_header(locale, when, who)}\n{quoted}"


def prefixed(subject: str, prefix: str, pattern: str) -> str:
    """Add the prefix (Re:, Fwd:) unless the subject already starts with it."""
    return subject if re.match(pattern, subject) else f"{prefix} {subject}"
