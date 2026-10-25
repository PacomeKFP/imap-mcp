"""MIME helpers on small in-code RFC 822 samples."""

from __future__ import annotations

from email.message import EmailMessage

from imap_mcp import content as content_mod
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
    msg = _message(
        To="Alice <alice@example.org>, bob@example.org",
        Cc="carol@example.org",
        Bcc="dave@example.org",
    )
    assert mime.all_recipients(msg) == [
        "alice@example.org",
        "bob@example.org",
        "carol@example.org",
        "dave@example.org",
    ]


def test_all_recipients_empty_when_no_recipient_header() -> None:
    assert mime.all_recipients(_message(Subject="Hi")) == []


def test_reply_target_keeps_every_reply_to_address_without_duplicates() -> None:
    original = _message(
        Reply_To="Ann <ann@example.org>, Ben <BEN@example.org>, ann@EXAMPLE.org",
        From="sender@example.org",
    )
    assert mime.reply_target(original, reply_all=False, me="me@example.org") == (
        ["ann@example.org", "BEN@example.org"],
        [],
    )


def test_reply_target_falls_back_to_from() -> None:
    original = _message(From="Sender <sender@example.org>")
    assert mime.reply_target(original, reply_all=False, me="me@example.org") == (
        ["sender@example.org"],
        [],
    )


def test_reply_all_excludes_me_and_repeated_recipients() -> None:
    original = _message(
        From="sender@example.org",
        To="Me <ME@example.org>, carol@example.org",
        Cc="sender@example.org, Dave <dave@example.org>, carol@EXAMPLE.org",
    )
    to, cc = mime.reply_target(original, reply_all=True, me="me@example.org")
    assert to == ["sender@example.org"]
    assert cc == ["carol@example.org", "dave@example.org"]


def test_sort_key_orders_across_time_zones() -> None:
    earlier = mime.sort_key("2024-01-01T10:00:00+02:00")  # 08:00 UTC
    later = mime.sort_key("2024-01-01T09:00:00+00:00")
    assert earlier < later
    assert earlier.utcoffset() is not None and earlier.utcoffset().total_seconds() == 0


def test_sort_key_reads_rfc2822_and_treats_naive_as_utc() -> None:
    assert mime.sort_key("Mon, 01 Jan 2024 10:00:00 +0200") == mime.sort_key(
        "2024-01-01T08:00:00+00:00"
    )
    assert mime.sort_key("2024-01-01T08:00:00") == mime.sort_key("2024-01-01T08:00:00+00:00")


def test_sort_key_falls_back_to_minimum_when_unparseable() -> None:
    assert mime.sort_key("not a date") == mime.sort_key("")
    assert mime.sort_key("not a date") < mime.sort_key("2000-01-01T00:00:00+00:00")


def test_parse_returns_headers_and_body() -> None:
    raw = b"From: a@example.org\r\nSubject: Hi\r\n\r\nHello\r\n"
    msg = mime.parse(raw)
    assert msg["Subject"] == "Hi"
    assert msg.get_content().strip() == "Hello"


_MULTIPART = (
    b"From: a@example.org\r\n"
    b"Subject: Report\r\n"
    b"MIME-Version: 1.0\r\n"
    b'Content-Type: multipart/mixed; boundary="XX"\r\n'
    b"\r\n"
    b"--XX\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"\r\n"
    b"Hello there\r\n"
    b"--XX\r\n"
    b'Content-Type: application/octet-stream; name="data.bin"\r\n'
    b'Content-Disposition: attachment; filename="data.bin"\r\n'
    b"Content-Transfer-Encoding: base64\r\n"
    b"\r\n"
    b"AAECAwQF\r\n"
    b"--XX--\r\n"
)


def test_read_content_reports_attachment_size_after_decoding() -> None:
    content = content_mod.read_content(mime.parse(_MULTIPART))
    assert content.text.strip() == "Hello there"
    assert len(content.attachments) == 1
    attachment = content.attachments[0]
    assert attachment.filename == "data.bin"
    assert attachment.content_type == "application/octet-stream"
    assert attachment.size == 6


def test_payload_bytes_returns_decoded_bytes_for_a_part() -> None:
    part = mime.parse(_MULTIPART).get_payload()[1]
    assert content_mod.payload_bytes(part) == bytes([0, 1, 2, 3, 4, 5])


def test_payload_bytes_is_empty_for_multipart() -> None:
    assert content_mod.payload_bytes(mime.parse(_MULTIPART)) == b""
