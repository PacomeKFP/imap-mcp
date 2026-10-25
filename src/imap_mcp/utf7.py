"""Modified UTF-7 encoding (RFC 3501) of IMAP folder names."""

from __future__ import annotations

import base64
import binascii
import re

_ENCODED = re.compile(r"&([^-]*)-")


def encode(text: str) -> str:
    out: list[str] = []
    pending: list[str] = []

    def flush() -> None:
        if pending:
            raw = "".join(pending).encode("utf-16-be")
            out.append("&" + base64.b64encode(raw).decode().rstrip("=").replace("/", ",") + "-")
            pending.clear()

    for char in text:
        if 0x20 <= ord(char) <= 0x7E:
            flush()
            out.append("&-" if char == "&" else char)
        else:
            pending.append(char)
    flush()
    return "".join(out)


def decode(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        if match[1] == "":
            return "&"
        b64 = match[1].replace(",", "/")
        b64 += "=" * (-len(b64) % 4)
        try:
            return base64.b64decode(b64).decode("utf-16-be")
        except (binascii.Error, UnicodeDecodeError):
            return match[0]

    return _ENCODED.sub(repl, text)
