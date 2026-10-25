"""Newest-first UID search (SORT with a descending-UID fallback) and UIDVALIDITY, on a fake connection."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from imap_mcp import imap
from imap_mcp.errors import MailError


class FakeConn:
    """Scripts UID command replies and records each command with the literal sent alongside it."""

    def __init__(
        self,
        capabilities: tuple[str, ...] = ("IMAP4REV1", "SORT"),
        replies: dict[str, tuple[str, list[Any]]] | None = None,
    ) -> None:
        self.capabilities = capabilities
        self.replies = replies or {}
        self.calls: list[tuple[str, ...]] = []
        self.literals: list[bytes | None] = []
        self.literal: bytes | None = None
        self.untagged_responses: dict[str, list[Any]] = {}

    def uid(self, command: str, *args: str) -> tuple[str, list[Any]]:
        self.calls.append((command, *args))
        self.literals.append(self.literal)
        return self.replies.get(command, ("OK", [b""]))

    def select(self, mailbox: str, readonly: bool = True) -> tuple[str, list[bytes]]:
        # imaplib clears the untagged responses at the start of every SELECT.
        self.untagged_responses = {}
        return ("OK", [b"3"])


def test_newest_first_uses_sort_reverse_date_when_advertised() -> None:
    conn = FakeConn(replies={"SORT": ("OK", [b"3 2 1"])})

    assert imap.search_uids(conn, ["UNSEEN"], newest_first=True) == ["3", "2", "1"]
    assert conn.calls == [("SORT", "(REVERSE DATE)", "UTF-8", "UNSEEN")]
    assert conn.literals == [None]


def test_newest_first_default_criteria_sorts_all_messages() -> None:
    conn = FakeConn(replies={"SORT": ("OK", [b"9 4"])})

    assert imap.search_uids(conn, [], newest_first=True) == ["9", "4"]
    assert conn.calls == [("SORT", "(REVERSE DATE)", "UTF-8", "ALL")]


def test_newest_first_empty_sort_response_yields_no_uids() -> None:
    conn = FakeConn(replies={"SORT": ("OK", [None])})

    assert imap.search_uids(conn, ["UNSEEN"], newest_first=True) == []
    assert [call[0] for call in conn.calls] == ["SORT"]


def test_newest_first_without_sort_falls_back_to_descending_uid() -> None:
    conn = FakeConn(
        capabilities=("IMAP4REV1",),
        replies={"SEARCH": ("OK", [b"1 5 3"])},
    )

    assert imap.search_uids(conn, ["UNSEEN"], newest_first=True) == ["5", "3", "1"]
    assert conn.calls == [("SEARCH", "UNSEEN")]


def test_newest_first_falls_back_when_sort_is_refused() -> None:
    conn = FakeConn(
        replies={"SORT": ("NO", [b"SORT not allowed"]), "SEARCH": ("OK", [b"2 9"])},
    )

    assert imap.search_uids(conn, ["UNSEEN"], newest_first=True) == ["9", "2"]
    assert [call[0] for call in conn.calls] == ["SORT", "SEARCH"]


def test_default_search_keeps_server_order_and_skips_sort() -> None:
    conn = FakeConn(replies={"SEARCH": ("OK", [b"4 2 8"])})

    assert imap.search_uids(conn, ["UNSEEN"]) == ["4", "2", "8"]
    assert conn.calls == [("SEARCH", "UNSEEN")]


def test_non_ascii_value_is_sent_as_literal_on_sort_path() -> None:
    conn = FakeConn(replies={"SORT": ("OK", [b"7"])})

    assert imap.search_uids(conn, ["SUBJECT", '"Café"'], newest_first=True) == ["7"]
    assert conn.calls == [("SORT", "(REVERSE DATE)", "UTF-8", "SUBJECT")]
    assert conn.literals == ["Café".encode()]


def test_header_non_ascii_value_keeps_field_name_before_literal() -> None:
    conn = FakeConn(replies={"SORT": ("OK", [b"6"])})

    imap.search_uids(conn, ["HEADER", "Subject", '"Café"'], newest_first=True)

    assert conn.calls == [("SORT", "(REVERSE DATE)", "UTF-8", "HEADER", "Subject")]
    assert conn.literals == ["Café".encode()]


def test_non_ascii_value_on_search_path_declares_charset() -> None:
    conn = FakeConn(
        capabilities=("IMAP4REV1",),
        replies={"SEARCH": ("OK", [b"5"])},
    )

    assert imap.search_uids(conn, ["SUBJECT", '"Café"'], newest_first=True) == ["5"]
    assert conn.calls == [("SEARCH", "CHARSET", "UTF-8", "SUBJECT")]
    assert conn.literals == ["Café".encode()]


def test_only_one_non_ascii_value_is_allowed() -> None:
    conn = FakeConn()

    with pytest.raises(MailError) as excinfo:
        imap.search_uids(conn, ["SUBJECT", '"Café"', "FROM", '"Ünï"'], newest_first=True)
    assert excinfo.value.code == "invalid_argument"
    assert conn.calls == []


def test_non_ascii_value_without_keyword_is_rejected() -> None:
    conn = FakeConn()

    with pytest.raises(MailError) as excinfo:
        imap.search_uids(conn, ['"Café"'])
    assert excinfo.value.code == "invalid_argument"


def test_criteria_with_control_characters_are_rejected() -> None:
    conn = FakeConn()

    with pytest.raises(MailError) as excinfo:
        imap.search_uids(conn, ["UNSEEN\r\nA1 LOGOUT"], newest_first=True)
    assert excinfo.value.code == "invalid_argument"
    assert conn.calls == []


def test_uidvalidity_reads_value_from_select_response() -> None:
    conn = FakeConn()
    conn.untagged_responses = {"OK": [b"[UIDVALIDITY 12345] UIDs valid"]}

    assert imap.uidvalidity(conn) == 12345


def test_uidvalidity_is_read_after_select() -> None:
    class SelectingConn(FakeConn):
        def select(self, mailbox: str, readonly: bool = True) -> tuple[str, list[bytes]]:
            super().select(mailbox, readonly)
            self.untagged_responses = {"OK": [b"[UIDVALIDITY 77] UIDs valid"]}
            return ("OK", [b"12"])

    conn = SelectingConn()

    assert imap.select(conn, "INBOX") == 12
    assert imap.uidvalidity(conn) == 77


def test_uidvalidity_is_none_when_server_omits_it() -> None:
    conn = FakeConn()
    conn.untagged_responses = {"OK": [b"[UNSEEN 4] first unseen"]}

    assert imap.uidvalidity(conn) is None


def test_uidvalidity_is_none_without_untagged_responses_attribute() -> None:
    conn: Any = SimpleNamespace()

    assert imap.uidvalidity(conn) is None


def test_uidvalidity_prefers_latest_value_and_does_not_consume_it() -> None:
    conn = FakeConn()
    conn.untagged_responses = {
        "OK": [b"[UIDVALIDITY 1] stale", b"[uidvalidity 7] current"],
    }

    assert imap.uidvalidity(conn) == 7
    assert imap.uidvalidity(conn) == 7
    assert conn.untagged_responses["OK"] == [b"[UIDVALIDITY 1] stale", b"[uidvalidity 7] current"]
