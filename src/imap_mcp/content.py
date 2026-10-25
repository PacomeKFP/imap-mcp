"""Message bodies and attachments: text and HTML reading, attachment lookup, excerpts."""
from __future__ import annotations

import re
from dataclasses import dataclass
from email.message import Message
from html.parser import HTMLParser

from .mime import decode_text

_BLOCK_TAGS = frozenset({
    "p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "table", "blockquote",
})
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


def excerpt(text: str, limit: int) -> str:
    """Cut text to limit characters, with a marker when it was cut."""
    return text if len(text) <= limit else text[:limit] + "\n[truncated]"


def _text_of(part: Message) -> str:
    try:
        return str(part.get_content())  # type: ignore[attr-defined]
    except (LookupError, ValueError):
        return payload_bytes(part).decode("utf-8", errors="replace")


def read_content(msg: Message) -> Content:
    """Text and HTML bodies (text rebuilt from the HTML when missing) and attachments."""
    text = html = ""
    attachments: list[Attachment] = []
    for index, part in enumerate(msg.walk()):
        if part.is_multipart():
            continue
        content_type = part.get_content_type()
        if part.get_filename() or part.get_content_disposition() == "attachment":
            payload = payload_bytes(part)
            name = decode_text(part.get_filename()) or f"part-{index}"
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
    """Non-multipart part at the index numbered by read_content."""
    for i, part in enumerate(msg.walk()):
        if i == index:
            return None if part.is_multipart() else part
    return None
