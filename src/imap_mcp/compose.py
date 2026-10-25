"""Outgoing messages: headers, text and HTML bodies, attachments and quoted replies."""
from __future__ import annotations

import mimetypes
import re
from email.message import EmailMessage, Message
from email.utils import formatdate, make_msgid
from pathlib import Path

from .content import Attachment, get_part
from .errors import MailError
from .mime import decode_text


def add_bytes(msg: EmailMessage, data: bytes, content_type: str, filename: str) -> None:
    maintype, _, subtype = content_type.partition("/")
    msg.add_attachment(
        data,
        maintype=maintype or "application",
        subtype=subtype or "octet-stream",
        filename=filename,
    )


def copy_attachments(source: Message, target: EmailMessage, attachments: list[Attachment]) -> None:
    for attachment in attachments:
        part = get_part(source, attachment.index)
        if part is not None:
            add_bytes(target, part.get_payload(decode=True) or b"",
                      attachment.content_type, attachment.filename)


def attach_files(msg: EmailMessage, paths: list[str] | None) -> None:
    for raw_path in paths or []:
        path = Path(raw_path).expanduser()
        if not path.is_file():
            raise MailError(
                f"Attachment not found: {raw_path}", code="not_found")
        content_type = mimetypes.guess_type(
            path.name)[0] or "application/octet-stream"
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


def quote_reply(original: Message, text: str) -> str:
    """Quote the original text under a reply header."""
    who = decode_text(original["From"])
    when = decode_text(original["Date"])
    quoted = "\n".join("> " + line for line in text.splitlines())
    # The reply header is mail content, kept in French on purpose.
    return f"\n\nLe {when}, {who} a écrit :\n{quoted}"


def prefixed(subject: str, prefix: str, pattern: str) -> str:
    """Add the prefix (Re:, Fwd:) unless the subject already starts with it."""
    return subject if re.match(pattern, subject) else f"{prefix} {subject}"
