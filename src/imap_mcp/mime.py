"""Lecture et construction de messages : en-têtes, MIME, pièces jointes, réponses."""
from __future__ import annotations

import email
import mimetypes
import re
from dataclasses import dataclass
from email import policy
from email.errors import MessageError
from email.header import decode_header, make_header
from email.message import EmailMessage, Message
from email.utils import formatdate, getaddresses, make_msgid, parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, cast

from .errors import MailError

_BLOCK_TAGS = frozenset({"p", "div", "br", "tr", "li",
                        "h1", "h2", "h3", "h4", "table", "blockquote"})
_SKIP_TAGS = frozenset({"script", "style", "head"})


@dataclass(frozen=True)
class Attachment:
    index: int
    filename: str
    content_type: str
    size: int


@dataclass(frozen=True)
class Content:
    text: str
    html: str
    attachments: list[Attachment]


def parse(raw: bytes) -> EmailMessage:
    return cast(EmailMessage, email.message_from_bytes(raw, policy=policy.default))


def decode_text(value: object) -> str:
    """Décode un en-tête RFC 2047 ; renvoie la valeur brute si le décodage échoue."""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(str(value))))
    except (ValueError, LookupError, UnicodeError, MessageError):
        return str(value)


def text_header(msg: Message, name: str) -> str | None:
    value = msg.get(name)
    return str(value) if value else None


def addresses(value: object) -> list[dict[str, str]]:
    return [
        {"name": decode_text(name), "email": addr}
        for name, addr in getaddresses([decode_text(value)])
        if addr
    ]


def emails(value: object) -> list[str]:
    return [item["email"] for item in addresses(value)]


def all_recipients(msg: Message) -> list[str]:
    fields = [str(msg.get(header) or "") for header in ("To", "Cc", "Bcc")]
    return [addr for _, addr in getaddresses(fields) if addr]


def reply_target(original: Message, reply_all: bool, me: str) -> tuple[list[str], list[str]]:
    """Destinataires (To, Cc) d'une réponse ; `me` est exclu des copies."""
    sender = parseaddr(str(original.get("Reply-To")
                       or original.get("From") or ""))[1]
    if not reply_all:
        return [sender], []
    others = [
        addr
        for _, addr in getaddresses([str(original.get("To") or ""), str(original.get("Cc") or "")])
        if addr
    ]
    excluded = {me.lower(), sender.lower()}
    return [sender], [addr for addr in others if addr.lower() not in excluded]


def summary(uid: str, msg: Message, flags: str = "", size: int | None = None) -> dict[str, Any]:
    raw_date = str(msg.get("Date") or "")
    try:
        date = parsedate_to_datetime(raw_date).isoformat()
    except (TypeError, ValueError):
        date = raw_date
    info: dict[str, Any] = {
        "uid": uid,
        "date": date,
        "from": decode_text(msg.get("From")),
        "to": decode_text(msg.get("To")),
        "subject": decode_text(msg.get("Subject")),
        "flags": flags,
        "unread": "\\Seen" not in flags,
        "flagged": "\\Flagged" in flags,
    }
    if size is not None:
        info["size"] = size
    return info


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self._chunks.append(data)

    def text(self) -> str:
        return "".join(self._chunks)


def html_to_text(html: str) -> str:
    parser = _HTMLText()
    parser.feed(html)
    text = re.sub(r"[ \t\r\f\v]+", " ", parser.text())
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()


def excerpt(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "\n[tronqué]"


def _text_of(part: Message) -> str:
    try:
        return str(part.get_content())  # type: ignore[attr-defined]
    except (LookupError, ValueError):
        return (part.get_payload(decode=True) or b"").decode("utf-8", errors="replace")


def read_content(msg: Message) -> Content:
    """Corps texte et HTML (texte reconstruit depuis le HTML si absent) et pièces jointes."""
    text = html = ""
    attachments: list[Attachment] = []
    for index, part in enumerate(msg.walk()):
        if part.is_multipart():
            continue
        content_type = part.get_content_type()
        if part.get_filename() or part.get_content_disposition() == "attachment":
            payload = part.get_payload(decode=True) or b""
            name = decode_text(part.get_filename()) or f"piece-{index}"
            attachments.append(Attachment(
                index, name, content_type, len(payload)))
        elif content_type == "text/plain" and not text:
            text = _text_of(part)
        elif content_type == "text/html" and not html:
            html = _text_of(part)
    if not text and html:
        text = html_to_text(html)
    return Content(text, html, attachments)


def get_part(msg: Message, index: int) -> Message | None:
    """Partie non multipart à l'index donné (index tel que renvoyé par read_content)."""
    for i, part in enumerate(msg.walk()):
        if i == index:
            return None if part.is_multipart() else part
    return None


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
            raise MailError(f"Pièce jointe introuvable : {raw_path}")
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
    who = decode_text(original["From"])
    when = decode_text(original["Date"])
    quoted = "\n".join("> " + line for line in text.splitlines())
    return f"\n\nLe {when}, {who} a écrit :\n{quoted}"


def prefixed(subject: str, prefix: str, pattern: str) -> str:
    """Ajoute le préfixe (Re:, Fwd:) sauf si le sujet le porte déjà."""
    return subject if re.match(pattern, subject) else f"{prefix} {subject}"
