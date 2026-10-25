"""Body and attachment reading from fixture messages, plus HTML-to-text and excerpts."""

from __future__ import annotations

from email.message import EmailMessage
from pathlib import Path

import pytest

from imap_mcp import mime
from imap_mcp.content import (
    excerpt,
    get_part,
    html_to_text,
    part_bytes,
    payload_bytes,
    read_content,
)

DATA = Path(__file__).parent / "data"


def load(name: str) -> EmailMessage:
    return mime.parse((DATA / name).read_bytes())


def test_alternative_prefers_plain_text_and_keeps_html() -> None:
    content = read_content(load("alternative.eml"))

    assert content.text == "Plain text version."
    assert "HTML version." in content.html
    assert content.attachments == []


def test_html_only_message_rebuilds_text_without_script_or_head() -> None:
    content = read_content(load("html_only.eml"))

    lines = [line.strip() for line in content.text.splitlines() if line.strip()]
    assert lines == ["First", "Second & last"]
    assert "<p>Second" in content.html
    assert "alert" not in content.text
    assert "hidden" not in content.text
    assert "Ignored" not in content.text


def test_mixed_attachments_are_listed_with_indexes_matching_get_part() -> None:
    msg = load("mixed_attachments.eml")
    content = read_content(msg)

    assert content.text == "Please see the attachments."
    assert [(a.filename, a.content_type) for a in content.attachments] == [
        ("notes.txt", "text/plain"),
        ("data.bin", "application/octet-stream"),
    ]
    for attachment in content.attachments:
        part = get_part(msg, attachment.index)
        assert part is not None
        assert part.get_filename() == attachment.filename
        assert attachment.size == len(payload_bytes(part))

    notes = get_part(msg, content.attachments[0].index)
    binary = get_part(msg, content.attachments[1].index)
    assert notes is not None and binary is not None
    assert payload_bytes(notes) == b"Meeting notes"
    assert payload_bytes(binary) == b"hello world\n"
    assert content.attachments[1].size == len(b"hello world\n")


def test_get_part_returns_none_for_multipart_out_of_range_and_negative() -> None:
    msg = load("mixed_attachments.eml")

    assert get_part(msg, 0) is None  # the multipart/mixed container
    assert get_part(msg, 999) is None
    assert get_part(msg, -1) is None


def test_latin1_body_is_decoded() -> None:
    content = read_content(load("latin1.eml"))

    assert content.text.rstrip() == "Café crème, brûlée à midi."


def test_windows1252_body_maps_the_0x80_range() -> None:
    content = read_content(load("windows1252.eml"))

    # 0x80 is the euro sign and 0x93/0x94/0x92 are curly quotes only in windows-1252.
    assert content.text.rstrip() == "Prix : 5\u20ac \u201ccitation\u201d l\u2019hiver"


def test_rfc2047_subject_sender_and_filenames_are_decoded() -> None:
    msg = load("rfc2047.eml")
    content = read_content(msg)

    assert mime.decode_text(msg["Subject"]) == "Café crème"
    assert mime.decode_text(msg["From"]) == "René Dupont <rene@example.org>"
    assert [a.filename for a in content.attachments] == ["résumé.txt", "éte.txt"]
    assert content.text == "Encoded names follow."


def test_inner_attachment_of_forwarded_message_is_indexed_for_get_part() -> None:
    msg = load("nested_rfc822.eml")
    content = read_content(msg)

    assert content.text == "See the forwarded message."
    inner = [a for a in content.attachments if a.filename == "inner.txt"]
    assert len(inner) == 1
    part = get_part(msg, inner[0].index)
    assert part is not None
    assert part.get_filename() == "inner.txt"
    assert payload_bytes(part) == b"Inner attachment."


def test_forwarded_message_is_listed_as_attachment() -> None:
    content = read_content(load("nested_rfc822.eml"))

    assert "forwarded.eml" in [a.filename for a in content.attachments]


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        ("<html><head><title>T</title></head><body>Body</body></html>", "Body"),
        ("<p>a</p><script>var x = 1;</script><p>b</p>", "a\nb"),
        ("<style>p { color: red; }</style>visible", "visible"),
        ("<script>one</script><script>two</script>after", "after"),
    ],
)
def test_html_to_text_skips_script_style_and_head(html: str, expected: str) -> None:
    assert html_to_text(html) == expected


def test_html_to_text_turns_block_tags_into_line_breaks() -> None:
    assert html_to_text("<div><div>a</div><div>b</div></div>") == "a\nb"
    assert html_to_text("line1<br>line2") == "line1\nline2"
    assert html_to_text("<p>a</p><p></p><p></p><p>b</p>") == "a\n\nb"


def test_html_to_text_decodes_entities_and_collapses_spaces() -> None:
    assert html_to_text("&lt;tag&gt; &amp; &quot;q&quot; &#233;") == '<tag> & "q" é'
    assert html_to_text("a    b\t\tc") == "a b c"


def test_excerpt_keeps_short_text_and_marks_cut_text() -> None:
    assert excerpt("abc", 3) == "abc"
    assert excerpt("abc", 10) == "abc"
    assert excerpt("abcdef", 3) == "abc\n[truncated]"


def _forwarded_mail(subject: str = "Quarterly: report/2024") -> bytes:
    inner = EmailMessage()
    inner["From"] = "ann@x.org"
    inner["Subject"] = subject
    inner.set_content("inner secret text")
    outer = EmailMessage()
    outer["From"] = "bob@x.org"
    outer["Subject"] = "Fwd"
    outer.set_content("outer body")
    outer.add_attachment(inner)
    return outer.as_bytes()


def test_forwarded_message_is_listed_with_its_subject_as_file_name() -> None:
    content = read_content(mime.parse(_forwarded_mail()))

    assert [(a.filename, a.content_type) for a in content.attachments] == [
        ("Quarterly report 2024.eml", "message/rfc822")
    ]


def test_forwarded_message_without_subject_gets_a_generic_name() -> None:
    content = read_content(mime.parse(_forwarded_mail(subject="")))

    assert content.attachments[0].filename == "message.eml"


def test_text_of_forwarded_message_is_not_used_as_body() -> None:
    content = read_content(mime.parse(_forwarded_mail()))

    assert content.text.strip() == "outer body"
    assert "inner secret" not in content.text
    assert "inner secret" not in content.html


def test_forwarded_message_size_is_the_serialized_sub_message() -> None:
    msg = mime.parse(_forwarded_mail())
    attachment = read_content(msg).attachments[0]
    data = part_bytes(get_part(msg, attachment.index))  # type: ignore[arg-type]

    assert attachment.size == len(data)
    assert b"Subject: Quarterly: report/2024" in data
    assert b"inner secret text" in data


def test_part_bytes_of_forwarded_message_is_its_serialized_form() -> None:
    msg = mime.parse(_forwarded_mail())
    part = get_part(msg, read_content(msg).attachments[0].index)

    assert part is not None
    assert part.get_content_type() == "message/rfc822"
    assert b"inner secret text" in part_bytes(part)


@pytest.mark.parametrize("size", [0, 1, 2, 3, 4, 57, 58, 76, 1000, 4096])
def test_base64_attachment_size_matches_decoded_length(size: int) -> None:
    data = bytes(i % 256 for i in range(size))
    msg = EmailMessage()
    msg.set_content("text")
    msg.add_attachment(data, maintype="application", subtype="octet-stream", filename="b.bin")

    attachment = read_content(mime.parse(msg.as_bytes())).attachments[0]

    assert attachment.size == size


_QUOTED_PRINTABLE_MESSAGE = (
    b"Content-Type: multipart/mixed; boundary=b\r\n\r\n"
    b"--b\r\nContent-Type: text/plain\r\n\r\nhello\r\n"
    b'--b\r\nContent-Type: application/octet-stream; name="q.bin"\r\n'
    b"Content-Transfer-Encoding: quoted-printable\r\n\r\n"
    b"caf=C3=A9=\r\n!\r\n--b--\r\n"
)


def test_quoted_printable_attachment_size_is_the_decoded_length() -> None:
    attachment = read_content(mime.parse(_QUOTED_PRINTABLE_MESSAGE)).attachments[0]

    assert attachment.size == len("café!".encode())


@pytest.mark.parametrize("path", sorted(DATA.glob("*.eml")), ids=lambda p: p.name)
def test_attachment_sizes_match_decoded_payloads_on_fixtures(path: Path) -> None:
    msg = mime.parse(path.read_bytes())

    for attachment in read_content(msg).attachments:
        part = get_part(msg, attachment.index)
        assert part is not None
        assert attachment.size == len(part_bytes(part))


def _inline_image_mail(with_pdf: bool = False) -> bytes:
    msg = EmailMessage()
    msg["From"] = "ann@x.org"
    msg["Subject"] = "Logo"
    msg.set_content("text")
    msg.add_alternative('<p><img src="cid:logo@x"></p>', subtype="html")
    msg.get_body(("html",)).add_related(  # type: ignore[union-attr]
        b"PNG",
        maintype="image",
        subtype="png",
        cid="<logo@x>",
        filename="logo.png",
        disposition="inline",
    )
    if with_pdf:
        msg.add_attachment(b"%PDF", maintype="application", subtype="pdf", filename="doc.pdf")
    return msg.as_bytes()


def test_inline_image_is_detected_by_content_id() -> None:
    attachments = read_content(mime.parse(_inline_image_mail())).attachments

    assert len(attachments) == 1
    logo = attachments[0]
    assert logo.filename == "logo.png"
    assert logo.content_type == "image/png"
    assert logo.content_id == "logo@x"
    assert logo.inline is True


def test_regular_attachment_is_not_inline() -> None:
    attachments = read_content(mime.parse(_inline_image_mail(with_pdf=True))).attachments
    pdf = next(a for a in attachments if a.filename == "doc.pdf")

    assert pdf.content_id is None
    assert pdf.inline is False


def test_part_bytes_returns_decoded_leaf_payload() -> None:
    msg = mime.parse(_inline_image_mail())
    logo = read_content(msg).attachments[0]

    assert part_bytes(get_part(msg, logo.index)) == b"PNG"  # type: ignore[arg-type]
