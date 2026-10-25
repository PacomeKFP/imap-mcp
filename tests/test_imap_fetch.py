"""IMAP helpers exercised against scripted fake connections (no network)."""

from __future__ import annotations

import re
from collections.abc import Callable
from email.message import EmailMessage
from pathlib import Path
from typing import Any, cast

import pytest

from imap_mcp import imap
from imap_mcp.config import Settings
from imap_mcp.errors import MailError
from imap_mcp.imap import Connection

SettingsFactory = Callable[..., Settings]
Response = tuple[str, list[Any]]


class FakeConn:
    """Scripted stand-in for imaplib.IMAP4_SSL that records every call."""

    literal: bytes | None = None

    def __init__(
        self,
        *,
        uid: dict[str, Response] | None = None,
        rows: list[Any] | None = None,
        capabilities: tuple[str, ...] = ("IMAP4REV1",),
        select: Response | None = None,
        create: Response | None = None,
        append: Response | None = None,
    ) -> None:
        self.uid_responses: dict[str, Response] = uid or {}
        self.rows: list[Any] = rows or []
        self.capabilities: tuple[str, ...] = capabilities
        self.select_response: Response = select or ("OK", [b"3"])
        self.create_response: Response = create or ("OK", [b"Create done"])
        self.append_response: Response = append or ("OK", [b"APPEND done"])
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def _record(self, name: str, *args: Any) -> None:
        self.calls.append((name, args))

    def calls_named(self, name: str) -> list[tuple[Any, ...]]:
        return [args for called, args in self.calls if called == name]

    def login(self, user: str, password: str) -> Response:
        self._record("login", user)
        return ("OK", [b"logged in"])

    def logout(self) -> Response:
        self._record("logout")
        return ("BYE", [b"bye"])

    def capability(self) -> Response:
        self._record("capability")
        return ("OK", [" ".join(self.capabilities).encode()])

    def select(self, mailbox: str, readonly: bool = True) -> Response:
        self._record("select", mailbox, readonly)
        return self.select_response

    def list(self, *args: str) -> Response:
        self._record("list", *args)
        return ("OK", list(self.rows))

    def create(self, mailbox: str) -> Response:
        self._record("create", mailbox)
        return self.create_response

    def subscribe(self, mailbox: str) -> Response:
        self._record("subscribe", mailbox)
        return ("OK", [b"subscribed"])

    def append(self, mailbox: str, flags: str | None, date: str, message: bytes) -> Response:
        self._record("append", mailbox, flags, date, message)
        return self.append_response

    def uid(self, command: str, *args: Any) -> Response:
        self._record("uid", command, *args)
        return self.uid_responses.get(command, ("OK", []))


def as_conn(fake: FakeConn) -> Connection:
    return cast(Connection, fake)


def _fetch_item(seq: int, uid: int, flags: str, size: int, header: bytes) -> list[Any]:
    head = f"{seq} (UID {uid} FLAGS ({flags}) RFC822.SIZE {size} BODY[] {{{len(header)}}}"
    return [(head.encode(), header), b")"]


def _header(subject: str, sender: str = "Alice <alice@example.org>") -> bytes:
    return (
        f"From: {sender}\r\nTo: bob@example.net\r\nSubject: {subject}\r\n"
        "Date: Mon, 01 Jan 2024 10:00:00 +0100\r\nMessage-ID: <m@example.org>\r\n\r\n"
    ).encode()


def test_fetch_summaries_preserves_requested_order() -> None:
    data = [
        *_fetch_item(1, 9, "\\Seen", 300, _header("Nine")),
        *_fetch_item(2, 7, "", 120, _header("Seven")),
    ]
    fake = FakeConn(uid={"FETCH": ("OK", data)})
    summaries = imap.fetch_summaries(as_conn(fake), ["7", "9"])
    assert [item["uid"] for item in summaries] == ["7", "9"]
    assert [item["subject"] for item in summaries] == ["Seven", "Nine"]


def test_fetch_summaries_skips_uids_missing_from_server() -> None:
    data = [
        *_fetch_item(1, 1, "", 10, _header("One")),
        *_fetch_item(2, 3, "", 10, _header("Three")),
    ]
    fake = FakeConn(uid={"FETCH": ("OK", data)})
    summaries = imap.fetch_summaries(as_conn(fake), ["1", "2", "3"])
    assert [item["uid"] for item in summaries] == ["1", "3"]


def test_fetch_summaries_reads_header_only_literal() -> None:
    fake = FakeConn(
        uid={
            "FETCH": (
                "OK",
                _fetch_item(1, 5, "\\Seen \\Flagged", 2048, _header("Quarterly report")),
            )
        }
    )
    (summary,) = imap.fetch_summaries(as_conn(fake), ["5"])
    assert summary["subject"] == "Quarterly report"
    assert summary["from"] == "Alice <alice@example.org>"
    assert summary["date"] == "2024-01-01T10:00:00+01:00"
    assert summary["flags"] == "\\Seen \\Flagged"
    assert summary["unread"] is False
    assert summary["flagged"] is True
    assert summary["size"] == 2048


def test_fetch_summaries_empty_uid_list_makes_no_call() -> None:
    fake = FakeConn()
    assert imap.fetch_summaries(as_conn(fake), []) == []
    assert fake.calls == []


def test_fetch_summaries_requests_header_fields_without_body() -> None:
    fake = FakeConn(uid={"FETCH": ("OK", [])})
    imap.fetch_summaries(as_conn(fake), ["2", "4"])
    ((command, *args),) = fake.calls_named("uid")
    assert command == "FETCH"
    assert args[0] == "2,4"
    assert "HEADER.FIELDS" in args[1]
    assert "BODY[]" not in args[1]


def test_fetch_raw_returns_body_and_flags() -> None:
    raw = b"Subject: hi\r\n\r\nbody\r\n"
    data = [(b"1 (UID 4 FLAGS (\\Seen \\Flagged) BODY[] {22}", raw), b")"]
    fake = FakeConn(uid={"FETCH": ("OK", data)})
    body, flags = imap.fetch_raw(as_conn(fake), "4")
    assert body == raw
    assert flags == "\\Seen \\Flagged"
    assert fake.calls_named("uid") == [("FETCH", "4", "(FLAGS BODY.PEEK[])")]


def test_fetch_raw_without_flags_returns_empty_flags() -> None:
    data = [(b"1 (UID 4 BODY[] {3}", b"abc"), b")"]
    fake = FakeConn(uid={"FETCH": ("OK", data)})
    assert imap.fetch_raw(as_conn(fake), "4") == (b"abc", "")


def test_fetch_raw_missing_message_raises_not_found() -> None:
    fake = FakeConn(uid={"FETCH": ("OK", [None])})
    with pytest.raises(MailError) as info:
        imap.fetch_raw(as_conn(fake), "99")
    assert info.value.code == "not_found"


def test_fetch_raw_rejects_non_numeric_uid_before_any_call() -> None:
    fake = FakeConn()
    with pytest.raises(MailError) as info:
        imap.fetch_raw(as_conn(fake), "4; DELETE")
    assert info.value.code == "invalid_uids"
    assert fake.calls == []


def test_search_uids_sends_ascii_criteria_unchanged() -> None:
    fake = FakeConn(uid={"SEARCH": ("OK", [b"3 8"])})
    assert imap.search_uids(as_conn(fake), ["UNSEEN", "FROM", '"bob"']) == ["3", "8"]
    assert fake.calls_named("uid") == [("SEARCH", "UNSEEN", "FROM", '"bob"')]


def test_search_uids_defaults_to_all_without_criteria() -> None:
    fake = FakeConn(uid={"SEARCH": ("OK", [b"1 2"])})
    assert imap.search_uids(as_conn(fake), []) == ["1", "2"]
    assert fake.calls_named("uid") == [("SEARCH", "ALL")]


def test_search_uids_empty_result_is_empty_list() -> None:
    fake = FakeConn(uid={"SEARCH": ("OK", [None])})
    assert imap.search_uids(as_conn(fake), ["ALL"]) == []


def test_search_uids_non_ascii_value_is_sent_as_utf8_literal() -> None:
    fake = FakeConn(uid={"SEARCH": ("OK", [b"6"])})
    assert imap.search_uids(as_conn(fake), ["SUBJECT", '"résumé"']) == ["6"]
    assert fake.literal == "résumé".encode()
    assert fake.calls_named("uid") == [("SEARCH", "CHARSET", "UTF-8", "SUBJECT")]


def test_search_uids_moves_keyword_before_literal_with_header_field() -> None:
    fake = FakeConn(uid={"SEARCH": ("OK", [b"2"])})
    imap.search_uids(as_conn(fake), ["UNSEEN", "HEADER", "Subject", '"café"'])
    assert fake.literal == "café".encode()
    assert fake.calls_named("uid") == [
        ("SEARCH", "CHARSET", "UTF-8", "UNSEEN", "HEADER", "Subject")
    ]


def test_search_uids_keeps_tokens_after_literal_value() -> None:
    fake = FakeConn(uid={"SEARCH": ("OK", [b"2"])})
    imap.search_uids(as_conn(fake), ["UNSEEN", "SUBJECT", '"é"', "FROM", "x"])
    assert fake.literal == "é".encode()
    assert fake.calls_named("uid") == [
        ("SEARCH", "CHARSET", "UTF-8", "UNSEEN", "FROM", "x", "SUBJECT")
    ]


def test_search_uids_unescapes_quoted_literal() -> None:
    fake = FakeConn(uid={"SEARCH": ("OK", [])})
    imap.search_uids(as_conn(fake), ["SUBJECT", '"é\\"x"'])
    assert fake.literal == 'é"x'.encode()


def test_search_uids_rejects_two_non_ascii_values() -> None:
    fake = FakeConn()
    with pytest.raises(MailError) as info:
        imap.search_uids(as_conn(fake), ["SUBJECT", '"é"', "FROM", '"ö"'])
    assert info.value.code == "invalid_argument"
    assert fake.calls == []


def test_search_uids_rejects_non_ascii_value_without_keyword() -> None:
    fake = FakeConn()
    with pytest.raises(MailError) as info:
        imap.search_uids(as_conn(fake), ['"é"'])
    assert info.value.code == "invalid_argument"


@pytest.mark.parametrize("criterion", ["a b", "x\r\nDELETE", '"unterminated', "(OR"])
def test_search_uids_rejects_malformed_criteria(criterion: str) -> None:
    fake = FakeConn()
    with pytest.raises(MailError) as info:
        imap.search_uids(as_conn(fake), ["FROM", criterion])
    assert info.value.code == "invalid_argument"
    assert fake.calls == []


def test_uid_by_message_id_returns_last_matching_uid() -> None:
    fake = FakeConn(uid={"SEARCH": ("OK", [b"5 9 12"])})
    assert imap.uid_by_message_id(as_conn(fake), "INBOX", "<id@example.org>") == "12"
    assert fake.calls_named("select") == [('"INBOX"', True)]
    assert fake.calls_named("uid") == [("SEARCH", "HEADER", "Message-ID", '"<id@example.org>"')]


def test_uid_by_message_id_without_match_returns_none() -> None:
    fake = FakeConn(uid={"SEARCH": ("OK", [b""])})
    assert imap.uid_by_message_id(as_conn(fake), "INBOX", "<gone@example.org>") is None


def test_uid_by_message_id_unfolds_folded_header_value() -> None:
    fake = FakeConn(uid={"SEARCH": ("OK", [])})
    imap.uid_by_message_id(as_conn(fake), "INBOX", "<a@b\r\n c>")
    assert fake.calls_named("uid") == [("SEARCH", "HEADER", "Message-ID", '"<a@bc>"')]


def test_uid_by_message_id_missing_folder_raises() -> None:
    fake = FakeConn(select=("NO", [b"no such mailbox"]))
    with pytest.raises(MailError) as info:
        imap.uid_by_message_id(as_conn(fake), "Nope", "<id@example.org>")
    assert info.value.code == "folder_not_found"


def test_ensure_folder_creates_and_subscribes_when_missing() -> None:
    rows = [b'(\\HasNoChildren) "/" "INBOX"']
    fake = FakeConn(rows=rows)
    imap.ensure_folder(as_conn(fake), "Projets")
    assert fake.calls_named("create") == [('"Projets"',)]
    assert fake.calls_named("subscribe") == [('"Projets"',)]


def test_ensure_folder_does_nothing_when_folder_exists() -> None:
    rows = [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren) "/" "Projets"']
    fake = FakeConn(rows=rows)
    imap.ensure_folder(as_conn(fake), "Projets")
    assert fake.calls_named("create") == []
    assert fake.calls_named("subscribe") == []


def test_ensure_folder_encodes_non_ascii_name_in_utf7() -> None:
    fake = FakeConn(rows=[])
    imap.ensure_folder(as_conn(fake), "é")
    assert fake.calls_named("create") == [('"&AOk-"',)]


def test_ensure_folder_create_refusal_raises_server_refused() -> None:
    fake = FakeConn(rows=[], create=("NO", [b"over quota"]))
    with pytest.raises(MailError) as info:
        imap.ensure_folder(as_conn(fake), "Projets")
    assert info.value.code == "server_refused"
    assert fake.calls_named("subscribe") == []


def test_append_passes_flags_and_exact_message_bytes() -> None:
    message = EmailMessage()
    message["Subject"] = "Draft"
    message.set_content("draft body")
    fake = FakeConn()
    imap.append(as_conn(fake), "Drafts", message, "\\Seen \\Draft")
    ((mailbox, flags, date, payload),) = fake.calls_named("append")
    assert mailbox == '"Drafts"'
    assert flags == "(\\Seen \\Draft)"
    assert re.fullmatch(r'"\d{2}-[A-Za-z]{3}-\d{4} \d{2}:\d{2}:\d{2} [+-]\d{4}"', date)
    assert payload == message.as_bytes()


def test_append_without_flags_sends_no_flag_list() -> None:
    fake = FakeConn()
    imap.append(as_conn(fake), "Sent", EmailMessage())
    ((_, flags, _, _),) = fake.calls_named("append")
    assert flags is None


def test_append_refusal_names_the_folder() -> None:
    fake = FakeConn(append=("NO", [b"quota exceeded"]))
    with pytest.raises(MailError) as info:
        imap.append(as_conn(fake), "Sent", EmailMessage())
    assert info.value.code == "server_refused"
    assert "Append to Sent" in str(info.value)


def test_special_folder_prefers_special_use_attribute() -> None:
    rows = [
        b'(\\HasNoChildren) "/" "Sent"',
        b'(\\HasNoChildren \\Sent) "/" "Outbox"',
    ]
    fake = FakeConn(rows=rows)
    assert imap.special_folder(as_conn(fake), "sent") == "Outbox"


@pytest.mark.parametrize(
    ("role", "server_name", "expected"),
    [
        ("trash", "Trash", "Trash"),
        ("junk", "spam", "spam"),
        ("archive", "ARCHIVES", "ARCHIVES"),
        ("drafts", "Brouillons", "Brouillons"),
        ("sent", "&AMk-l&AOk-ments envoy&AOk-s", "Éléments envoyés"),
    ],
)
def test_special_folder_falls_back_to_names(role: str, server_name: str, expected: str) -> None:
    rows = [b'(\\HasNoChildren) "/" "INBOX"', f'(\\HasNoChildren) "/" "{server_name}"'.encode()]
    fake = FakeConn(rows=rows)
    assert imap.special_folder(as_conn(fake), role) == expected


def test_special_folder_missing_raises() -> None:
    fake = FakeConn(rows=[b'(\\HasNoChildren) "/" "INBOX"'])
    with pytest.raises(MailError) as info:
        imap.special_folder(as_conn(fake), "trash")
    assert info.value.code == "special_folder_missing"


@pytest.fixture
def patched_session(
    monkeypatch: pytest.MonkeyPatch, make_settings: SettingsFactory, tmp_path: Path
) -> FakeConn:
    """Route imap.session() to a FakeConn, with fixed settings and credentials."""
    settings: Settings = make_settings(tmp_path, imap_host="imap.example.org")
    fake = FakeConn()

    def connect(*args: Any, **kwargs: Any) -> FakeConn:
        return fake

    monkeypatch.setattr(imap, "settings", settings)
    monkeypatch.setattr(imap, "Connection", connect)
    monkeypatch.setattr(imap, "get_credentials", lambda: ("user@example.org", "pw-for-test"))
    return fake


def test_folder_separator_reads_bytes_row(patched_session: FakeConn) -> None:
    patched_session.rows = [b'(\\Noselect) "." ""']
    assert imap.folder_separator() == "."
    assert patched_session.calls_named("list") == [('""', '""')]


def test_folder_separator_nil_falls_back_to_slash(patched_session: FakeConn) -> None:
    patched_session.rows = [b'(\\Noselect) NIL ""']
    assert imap.folder_separator() == "/"


def test_folder_separator_without_rows_falls_back_to_slash(patched_session: FakeConn) -> None:
    patched_session.rows = []
    assert imap.folder_separator() == "/"


def test_folder_separator_reads_literal_row(patched_session: FakeConn) -> None:
    patched_session.rows = [(b'(\\Noselect) "." {0}', b"")]
    assert imap.folder_separator() == "."
