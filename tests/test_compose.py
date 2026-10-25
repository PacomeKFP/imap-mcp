"""Outgoing message construction, quoting, attachments and header helpers."""

from __future__ import annotations

from email.message import EmailMessage, Message
from pathlib import Path

import pytest

from imap_mcp import mime
from imap_mcp.compose import (
    add_bytes,
    attach_files,
    build_message,
    copy_attachments,
    prefixed,
    quote_reply,
)
from imap_mcp.content import Attachment, get_part, part_bytes, read_content
from imap_mcp.errors import MailError


def _parsed(headers: str, body: str = "body\n") -> Message:
    raw = (headers + "\n\n" + body).replace("\n", "\r\n").encode("ascii")
    return mime.parse(raw)


def test_build_message_message_id_uses_sender_domain() -> None:
    msg = build_message("Me <me@example.org>", ["you@example.net"], "Hi", "text")
    message_id = str(msg["Message-ID"])
    assert message_id.startswith("<")
    assert message_id.endswith("@example.org>")


def test_build_message_message_id_falls_back_to_localhost() -> None:
    msg = build_message("no-at-sign", ["you@example.net"], "Hi", "text")
    assert str(msg["Message-ID"]).endswith("@localhost>")


def test_build_message_omits_threading_headers_when_not_given() -> None:
    msg = build_message("me@example.org", ["you@example.net"], "Hi", "text")
    assert msg["In-Reply-To"] is None
    assert msg["References"] is None


def test_build_message_sets_threading_headers_when_given() -> None:
    msg = build_message(
        "me@example.org",
        ["you@example.net"],
        "Re: Hi",
        "text",
        in_reply_to="<parent@example.net>",
        references="<root@example.net> <parent@example.net>",
    )
    assert msg["In-Reply-To"] == "<parent@example.net>"
    assert msg["References"] == "<root@example.net> <parent@example.net>"


def test_build_message_joins_recipients_and_keeps_bcc() -> None:
    msg = build_message(
        "me@example.org",
        ["a@example.net", "b@example.net"],
        "Hi",
        "text",
        cc=["c@example.net"],
        bcc=["hidden@example.net"],
    )
    assert msg["To"] == "a@example.net, b@example.net"
    assert msg["Cc"] == "c@example.net"
    assert msg["Bcc"] == "hidden@example.net"
    assert msg["Subject"] == "Hi"


def test_build_message_omits_empty_recipient_headers() -> None:
    msg = build_message("me@example.org", [], "Hi", "text")
    assert msg["To"] is None
    assert msg["Cc"] is None
    assert msg["Bcc"] is None


def test_build_message_plain_text_is_not_multipart() -> None:
    msg = build_message("me@example.org", ["you@example.net"], "Hi", "plain body")
    assert not msg.is_multipart()
    assert msg.get_content().strip() == "plain body"


def test_build_message_html_creates_alternative_pair() -> None:
    msg = build_message(
        "me@example.org",
        ["you@example.net"],
        "Hi",
        "plain body",
        html="<p>rich body</p>",
    )
    assert msg.get_content_type() == "multipart/alternative"
    plain = msg.get_body(preferencelist=("plain",))
    html = msg.get_body(preferencelist=("html",))
    assert plain is not None and html is not None
    assert plain.get_content().strip() == "plain body"
    assert html.get_content().strip() == "<p>rich body</p>"


def test_build_message_attachments_wrap_body_in_mixed(tmp_path: Path) -> None:
    report = tmp_path / "report.txt"
    report.write_bytes(b"quarterly numbers\n")
    msg = build_message(
        "me@example.org",
        ["you@example.net"],
        "Hi",
        "see attached",
        attachments=[str(report)],
    )
    assert msg.get_content_type() == "multipart/mixed"
    attachments = list(msg.iter_attachments())
    assert len(attachments) == 1
    assert attachments[0].get_filename() == "report.txt"
    assert attachments[0].get_content_type() == "text/plain"
    assert attachments[0].get_payload(decode=True) == b"quarterly numbers\n"
    body = msg.get_body(preferencelist=("plain",))
    assert body is not None
    assert body.get_content().strip() == "see attached"


def test_build_message_html_and_attachment_structure(tmp_path: Path) -> None:
    blob = tmp_path / "data.bin"
    blob.write_bytes(b"\x00\x01\xfe\xff")
    msg = build_message(
        "me@example.org",
        ["you@example.net"],
        "Hi",
        "plain",
        html="<b>html</b>",
        attachments=[str(blob)],
    )
    assert msg.get_content_type() == "multipart/mixed"
    alternative = msg.get_body(preferencelist=("html",))
    assert alternative is not None
    assert alternative.get_content_type() == "text/html"
    attachment = next(iter(msg.iter_attachments()))
    assert attachment.get_filename() == "data.bin"
    assert attachment.get_content_type() == "application/octet-stream"
    assert attachment.get_payload(decode=True) == b"\x00\x01\xfe\xff"


def test_attach_files_missing_path_raises_not_found(tmp_path: Path) -> None:
    msg = EmailMessage()
    missing = tmp_path / "absent.pdf"
    with pytest.raises(MailError) as info:
        attach_files(msg, [str(missing)])
    assert info.value.code == "not_found"
    assert "absent.pdf" in str(info.value)


def test_attach_files_none_is_a_no_op() -> None:
    msg = EmailMessage()
    attach_files(msg, None)
    attach_files(msg, [])
    assert not msg.is_multipart()


def test_attach_files_guesses_content_type_from_extension(tmp_path: Path) -> None:
    picture = tmp_path / "photo.png"
    picture.write_bytes(b"\x89PNG\r\n")
    msg = EmailMessage()
    attach_files(msg, [str(picture)])
    attachment = next(iter(msg.iter_attachments()))
    assert attachment.get_content_type() == "image/png"


def test_quote_reply_prefixes_every_line() -> None:
    original = _parsed("From: Alice <alice@example.org>\nDate: Mon, 01 Jan 2024 10:00:00 +0000")
    quoted = quote_reply(original, "first line\nsecond line\n\nfourth line")
    lines = quoted.splitlines()
    assert lines[0] == ""
    assert lines[1] == ""
    assert lines[2].startswith(
        "Le Mon, 01 Jan 2024 10:00:00 +0000, Alice <alice@example.org> a écrit :"
    )
    assert lines[3:] == ["> first line", "> second line", "> ", "> fourth line"]


def test_quote_reply_decodes_rfc2047_sender_name() -> None:
    original = _parsed(
        "From: =?utf-8?q?Ren=C3=A9?= <rene@example.org>\nDate: Tue, 2 Jan 2024 09:30:00 +0000"
    )
    quoted = quote_reply(original, "ok")
    assert "René <rene@example.org> a écrit :" in quoted


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ("Re: Budget", "Re: Budget"),
        ("RE : Budget", "RE : Budget"),
        ("Réf : Budget", "Réf : Budget"),
        ("Budget", "Re: Budget"),
    ],
)
def test_prefixed_reply_variants(subject: str, expected: str) -> None:
    assert prefixed(subject, "Re:", r"(?i)^(re|réf)\s*:") == expected


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ("Tr: Agenda", "Tr: Agenda"),
        ("TR : Agenda", "TR : Agenda"),
        ("Fw: Agenda", "Fw: Agenda"),
        ("Fwd: Agenda", "Fwd: Agenda"),
        ("Agenda", "Fwd: Agenda"),
    ],
)
def test_prefixed_forward_variants(subject: str, expected: str) -> None:
    assert prefixed(subject, "Fwd:", r"(?i)^(tr|fwd?)\s*:") == expected


def test_copy_attachments_round_trip_preserves_bytes_and_filename(tmp_path: Path) -> None:
    blob = tmp_path / "résumé.bin"
    payload = bytes(range(256))
    blob.write_bytes(payload)
    built = build_message(
        "me@example.org", ["you@example.net"], "Hi", "body", attachments=[str(blob)]
    )
    parsed = mime.parse(built.as_bytes())
    attachments = read_content(parsed).attachments
    assert len(attachments) == 1

    target = EmailMessage()
    target["Subject"] = "Forward"
    copy_attachments(parsed, target, attachments)

    copied = list(target.iter_attachments())
    assert len(copied) == 1
    assert copied[0].get_filename() == "résumé.bin"
    assert copied[0].get_payload(decode=True) == payload


def test_copy_attachments_skips_indexes_that_no_longer_exist() -> None:
    source = _parsed("Subject: plain")
    target = EmailMessage()
    ghost = Attachment(index=7, filename="ghost.txt", content_type="text/plain", size=0)
    copy_attachments(source, target, [ghost])
    assert list(target.iter_attachments()) == []
    assert get_part(source, 7) is None


def test_reply_target_uses_reply_to_over_from() -> None:
    original = _parsed(
        "From: Sender <sender@example.org>\nReply-To: list@example.net\nTo: me@example.com"
    )
    to, cc = mime.reply_target(original, reply_all=False, me="me@example.com")
    assert to == ["list@example.net"]
    assert cc == []


def test_reply_target_reply_all_excludes_me_and_duplicates() -> None:
    original = _parsed(
        "From: Sender <sender@example.org>\n"
        "To: Me <ME@example.com>, Bob <bob@example.net>, bob@example.net\n"
        "Cc: carol@example.net, Sender <sender@example.org>"
    )
    to, cc = mime.reply_target(original, reply_all=True, me="me@example.com")
    assert to == ["sender@example.org"]
    assert cc == ["bob@example.net", "carol@example.net"]


def test_all_recipients_includes_bcc_and_deduplicates_nothing() -> None:
    msg = _parsed("To: a@example.org, Named <b@example.org>\nCc: c@example.org\nBcc: d@example.org")
    assert mime.all_recipients(msg) == [
        "a@example.org",
        "b@example.org",
        "c@example.org",
        "d@example.org",
    ]


def test_summary_flags_drive_unread_and_flagged() -> None:
    msg = _parsed("From: a@example.org\nSubject: S\nDate: Mon, 1 Jan 2024 10:00:00 +0000")
    seen = mime.summary("5", msg, flags="\\Seen \\Flagged", size=120)
    assert seen["unread"] is False
    assert seen["flagged"] is True
    assert seen["size"] == 120
    fresh = mime.summary("6", msg, flags="")
    assert fresh["unread"] is True
    assert fresh["flagged"] is False
    assert "size" not in fresh


def test_summary_normalises_rfc2822_date_to_iso() -> None:
    msg = _parsed("From: a@example.org\nDate: Mon, 1 Jan 2024 10:00:00 +0100")
    info = mime.summary("1", msg)
    assert info["date"] == "2024-01-01T10:00:00+01:00"


def test_summary_keeps_malformed_date_raw() -> None:
    msg = _parsed("From: a@example.org\nDate: not a real date")
    assert mime.summary("1", msg)["date"] == "not a real date"


def test_summary_missing_date_is_empty_string() -> None:
    msg = _parsed("From: a@example.org")
    assert mime.summary("1", msg)["date"] == ""


def test_sort_key_orders_mixed_timezones_as_instants() -> None:
    earlier = mime.sort_key("2024-01-01T10:00:00+01:00")
    later = mime.sort_key("2024-01-01T09:30:00+00:00")
    assert earlier < later


def test_sort_key_treats_naive_dates_as_utc() -> None:
    naive = mime.sort_key("2024-03-01T12:00:00")
    aware = mime.sort_key("2024-03-01T12:00:00+00:00")
    assert naive == aware


def test_sort_key_accepts_rfc2822_dates() -> None:
    assert mime.sort_key("Mon, 1 Jan 2024 10:00:00 +0100") == mime.sort_key(
        "2024-01-01T09:00:00+00:00"
    )


@pytest.mark.parametrize("value", ["", None, "garbage"])
def test_sort_key_unparseable_is_minimum(value: object) -> None:
    assert mime.sort_key(value).year == 1
    assert mime.sort_key(value) < mime.sort_key("2000-01-01T00:00:00+00:00")


def _source_with_inline_logo() -> Message:
    source = EmailMessage()
    source["From"] = "ann@x.org"
    source["Subject"] = "Logo"
    source.set_content("text")
    source.add_alternative('<p><img src="cid:logo@x"></p>', subtype="html")
    source.get_body(("html",)).add_related(  # type: ignore[union-attr]
        b"PNGDATA",
        maintype="image",
        subtype="png",
        cid="<logo@x>",
        filename="logo.png",
        disposition="inline",
    )
    source.add_attachment(b"%PDF-1.4", maintype="application", subtype="pdf", filename="doc.pdf")
    return mime.parse(source.as_bytes())


def test_copy_attachments_adds_referenced_inline_image_as_related_to_html() -> None:
    source = _source_with_inline_logo()
    target = build_message(
        "bob@x.org", ["carol@x.org"], "Fwd", "text", html='<p><img src="cid:logo@x"></p>'
    )

    copy_attachments(source, target, read_content(source).attachments)

    parsed = mime.parse(target.as_bytes())
    related = [p for p in parsed.walk() if p.get_content_type() == "multipart/related"]
    assert len(related) == 1
    parts = list(related[0].iter_parts())
    assert [p.get_content_type() for p in parts] == ["text/html", "image/png"]
    image = parts[1]
    assert image["Content-ID"] == "<logo@x>"
    assert image.get_content_disposition() == "inline"
    assert image.get_payload(decode=True) == b"PNGDATA"
    assert "doc.pdf" not in [p.get_filename() for p in related[0].walk()]
    attachments = read_content(parsed).attachments
    assert [a.filename for a in attachments] == ["logo.png", "doc.pdf"]
    assert attachments[0].inline is True
    assert attachments[1].inline is False


def test_copy_attachments_inline_image_without_html_body_is_a_regular_attachment() -> None:
    source = _source_with_inline_logo()
    target = build_message("bob@x.org", ["carol@x.org"], "Fwd", "text")
    logo = [a for a in read_content(source).attachments if a.filename == "logo.png"]

    copy_attachments(source, target, logo)

    parsed = mime.parse(target.as_bytes())
    assert not any(p.get_content_type() == "multipart/related" for p in parsed.walk())
    attachments = read_content(parsed).attachments
    assert [(a.filename, a.content_type) for a in attachments] == [("logo.png", "image/png")]


def test_copy_attachments_unreferenced_inline_image_is_a_regular_attachment() -> None:
    source = _source_with_inline_logo()
    target = build_message("bob@x.org", ["carol@x.org"], "Fwd", "text", html="<p>no image</p>")
    logo = [a for a in read_content(source).attachments if a.filename == "logo.png"]

    copy_attachments(source, target, logo)

    parsed = mime.parse(target.as_bytes())
    assert not any(p.get_content_type() == "multipart/related" for p in parsed.walk())


def test_add_bytes_attaches_message_rfc822_as_a_forwarded_message() -> None:
    target = build_message("bob@x.org", ["carol@x.org"], "Fwd", "text")
    inner = b"Subject: Inner\r\nFrom: ann@x.org\r\n\r\ninner body\r\n"

    add_bytes(target, inner, "message/rfc822", "inner.eml")

    parsed = mime.parse(target.as_bytes())
    attachments = read_content(parsed).attachments
    assert [(a.filename, a.content_type) for a in attachments] == [("inner.eml", "message/rfc822")]
    part = get_part(parsed, attachments[0].index)
    assert part is not None
    assert b"inner body" in part_bytes(part)


def test_quote_reply_uses_the_requested_locale() -> None:
    original = mime.parse(
        b"From: Ann <ann@x.org>\r\nDate: Mon, 1 Jan 2024 10:00:00 +0000\r\n"
        b"Subject: s\r\n\r\nbody\r\n"
    )

    assert quote_reply(original, "hi") == quote_reply(original, "hi", locale="fr")
    assert quote_reply(original, "hi") == (
        "\n\nLe Mon, 01 Jan 2024 10:00:00 +0000, Ann <ann@x.org> a écrit :\n> hi"
    )
    assert quote_reply(original, "hi", locale="en") == (
        "\n\nOn Mon, 01 Jan 2024 10:00:00 +0000, Ann <ann@x.org> wrote:\n> hi"
    )
