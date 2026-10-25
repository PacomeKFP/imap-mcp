"""Message bodies and attachments: text and HTML reading, attachment lookup, excerpts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from email.message import Message
from html.parser import HTMLParser

from .mime import decode_text

_BLOCK_TAGS = frozenset(
    {
        "p",
        "div",
        "br",
        "tr",
        "li",
        "h1",
        "h2",
        "h3",
        "h4",
        "table",
        "blockquote",
    }
)
_SKIP_TAGS = frozenset({"script", "style", "head"})


_RFC822 = "message/rfc822"
_BODY_TYPES = frozenset({"text/plain", "text/html"})


@dataclass(frozen=True)
class Attachment:
    index: int
    filename: str
    content_type: str
    size: int
    content_id: str | None = None
    inline: bool = False


@dataclass(frozen=True)
class Content:
    text: str
    html: str
    attachments: list[Attachment]


class _HTMLText(HTMLParser):
    """Collects visible text and drops script, style and head content."""

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


def payload_bytes(part: Message) -> bytes:
    """Decoded payload of a non-multipart part; empty when it is not bytes."""
    payload = part.get_payload(decode=True)
    return payload if isinstance(payload, bytes) else b""


def part_bytes(part: Message) -> bytes:
    """Bytes of an attachment part: the decoded payload, or the serialized sub-message
    for a message/rfc822 part."""
    if part.get_content_type() == _RFC822:
        payload = part.get_payload()
        if isinstance(payload, list) and payload and isinstance(payload[0], Message):
            return payload[0].as_bytes()
    return payload_bytes(part)


def _decoded_size(part: Message) -> int:
    """Size of the decoded payload. Base64 is computed from its length; other encodings are decoded."""
    if part.get_content_type() == _RFC822:
        return len(part_bytes(part))
    encoding = str(part.get("Content-Transfer-Encoding", "")).strip().lower()
    raw = part.get_payload()
    if encoding == "base64" and isinstance(raw, str):
        digits = re.sub(r"[^A-Za-z0-9+/]", "", raw)
        if len(digits) % 4 != 1:
            return len(digits) * 3 // 4
    return len(payload_bytes(part))


def _content_id(part: Message) -> str | None:
    value = str(part.get("Content-ID", "")).strip().strip("<>").strip()
    return value or None


def _is_attachment(part: Message) -> bool:
    if part.get_filename() or part.get_content_disposition() == "attachment":
        return True
    if part.get_content_type() in _BODY_TYPES:
        return False
    return part.get_content_disposition() == "inline" or _content_id(part) is not None


def _is_inline(part: Message) -> bool:
    disposition = part.get_content_disposition()
    return disposition == "inline" or (
        _content_id(part) is not None and disposition != "attachment"
    )


def _forwarded_name(part: Message) -> str:
    """File name for a forwarded message without one: its Subject with an .eml extension."""
    payload = part.get_payload()
    subject = ""
    if isinstance(payload, list) and payload and isinstance(payload[0], Message):
        subject = decode_text(payload[0].get("Subject"))
    cleaned = re.sub(r'[\\/:*?"<>|\s]+', " ", subject).strip()
    return f"{cleaned or 'message'}.eml"


def _forwarded_descendants(msg: Message) -> set[int]:
    """ids of the parts nested in message/rfc822 parts, whose text bodies are not used."""
    nested: set[int] = set()
    for part in msg.walk():
        if part.get_content_type() == _RFC822:
            nested.update(id(child) for child in part.walk() if child is not part)
    return nested


def excerpt(text: str, limit: int) -> str:
    """Cut text to limit characters, with a marker when it was cut."""
    return text if len(text) <= limit else text[:limit] + "\n[truncated]"


def _text_of(part: Message) -> str:
    try:
        return str(part.get_content())  # type: ignore[attr-defined]
    except (LookupError, ValueError):
        return payload_bytes(part).decode("utf-8", errors="replace")


def read_content(msg: Message) -> Content:
    """Text and HTML bodies (text rebuilt from the HTML when missing) and attachments.
    Forwarded messages (message/rfc822) are listed as attachments; their text is not used."""
    text = html = ""
    attachments: list[Attachment] = []
    forwarded = _forwarded_descendants(msg)
    for index, part in enumerate(msg.walk()):
        content_type = part.get_content_type()
        is_forwarded = content_type == _RFC822
        if part.is_multipart() and not is_forwarded:
            continue
        if is_forwarded or _is_attachment(part):
            name = decode_text(part.get_filename()) or (
                _forwarded_name(part) if is_forwarded else f"part-{index}"
            )
            attachments.append(
                Attachment(
                    index,
                    name,
                    content_type,
                    _decoded_size(part),
                    _content_id(part),
                    _is_inline(part),
                )
            )
        elif id(part) in forwarded:
            continue
        elif content_type == "text/plain" and not text:
            text = _text_of(part)
        elif content_type == "text/html" and not html:
            html = _text_of(part)
    if not text and html:
        text = html_to_text(html)
    return Content(text, html, attachments)


def get_part(msg: Message, index: int) -> Message | None:
    """Part at the index numbered by read_content: a leaf part or a message/rfc822 part."""
    for i, part in enumerate(msg.walk()):
        if i == index:
            is_leaf = not part.is_multipart() or part.get_content_type() == _RFC822
            return part if is_leaf else None
    return None
