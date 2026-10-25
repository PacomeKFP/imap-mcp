"""Parsing and header helpers for raw messages."""
from __future__ import annotations

import email
from email import policy
from email.errors import MessageError
from email.header import decode_header, make_header
from email.message import EmailMessage, Message
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from typing import Any, cast


def parse(raw: bytes) -> EmailMessage:
    return cast(EmailMessage, email.message_from_bytes(raw, policy=policy.default))


def decode_text(value: object) -> str:
    """Decode an RFC 2047 header; return the raw value when decoding fails."""
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


def _bare_addresses(*values: object) -> list[str]:
    """Addresses of several header values; each value is parsed on its own, since
    getaddresses over a list drops entries when a value is empty."""
    return [addr for value in values if value
            for _, addr in getaddresses([str(value)]) if addr]


def all_recipients(msg: Message) -> list[str]:
    return _bare_addresses(msg.get("To"), msg.get("Cc"), msg.get("Bcc"))


def reply_target(original: Message, reply_all: bool, me: str) -> tuple[list[str], list[str]]:
    """Recipients (To, Cc) of a reply; `me` is left out of the copies."""
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
