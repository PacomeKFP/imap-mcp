"""MIME helpers on small in-code RFC 822 samples."""
from __future__ import annotations

from email.message import EmailMessage

from imap_mcp import mime


def _message(**headers: str) -> EmailMessage:
    msg = EmailMessage()
    for name, value in headers.items():
        msg[name.replace("_", "-")] = value
    msg.set_content("Body")
    return msg


def test_all_recipients_keeps_to_when_cc_and_bcc_are_missing() -> None:
    msg = _message(To="alice@example.org")
    assert mime.all_recipients(msg) == ["alice@example.org"]


def test_all_recipients_collects_to_cc_and_bcc() -> None:
    msg = _message(To="Alice <alice@example.org>, bob@example.org",
                   Cc="carol@example.org", Bcc="dave@example.org")
    assert mime.all_recipients(msg) == [
        "alice@example.org", "bob@example.org", "carol@example.org", "dave@example.org",
    ]


def test_all_recipients_empty_when_no_recipient_header() -> None:
    assert mime.all_recipients(_message(Subject="Hi")) == []
