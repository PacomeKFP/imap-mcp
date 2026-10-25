"""Parsing and header helpers for raw messages."""

from __future__ import annotations

import email
from collections.abc import Iterable
from datetime import datetime, timezone
from email import policy
from email.errors import MessageError
from email.header import decode_header, make_header
from email.message import EmailMessage, Message
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any

_MIN_UTC = datetime.min.replace(tzinfo=timezone.utc)


def parse(raw: bytes) -> EmailMessage:
    return email.message_from_bytes(raw, policy=policy.default)


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
    return [addr for value in values if value for _, addr in getaddresses([str(value)]) if addr]


def _unique(addresses: Iterable[str]) -> list[str]:
    """Drop repeated addresses, comparing them case-insensitively."""
    seen: set[str] = set()
    result: list[str] = []
    for addr in addresses:
        key = addr.lower()
        if key not in seen:
            seen.add(key)
            result.append(addr)
    return result


def all_recipients(msg: Message) -> list[str]:
    return _bare_addresses(msg.get("To"), msg.get("Cc"), msg.get("Bcc"))


def reply_target(original: Message, reply_all: bool, me: str) -> tuple[list[str], list[str]]:
    """Recipients (To, Cc) of a reply; `me` is left out of the copies."""
    targets = _unique(_bare_addresses(original.get("Reply-To") or original.get("From")))
    if not reply_all:
        return targets, []
    excluded = {me.lower(), *(addr.lower() for addr in targets)}
    others = _unique(_bare_addresses(original.get("To"), original.get("Cc")))
    return targets, [addr for addr in others if addr.lower() not in excluded]


def sort_key(date: object) -> datetime:
    """UTC instant of a summary date (ISO string or RFC 2822); the minimum when unparseable.
    Naive dates are taken as UTC."""
    text = str(date or "")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return _MIN_UTC
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        return parsed.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        return _MIN_UTC


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
