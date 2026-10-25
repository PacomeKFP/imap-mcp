"""IMAP usage of the reading, organizing and account tools, with IMAP faked out."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from imap_mcp import imap
from imap_mcp.tools import account, organizing, reading


class FakeConn:
    """Stands in for imap.Connection; folder-changing commands always succeed."""

    def create(self, name: str) -> tuple[str, list[bytes]]:
        return "OK", [b""]

    def subscribe(self, name: str) -> tuple[str, list[bytes]]:
        return "OK", [b""]

    def rename(self, old: str, new: str) -> tuple[str, list[bytes]]:
        return "OK", [b""]


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list[str | None]:
    """Record the folder argument of every imap.session call."""
    folders: list[str | None] = []

    @contextmanager
    def fake_session(folder: str | None = None, readonly: bool = True) -> Iterator[FakeConn]:
        folders.append(folder)
        yield FakeConn()

    monkeypatch.setattr(imap, "session", fake_session)
    return folders


def _query(limit: int = 20, offset: int = 0) -> reading.SearchQuery:
    return reading.SearchQuery(
        folder="INBOX",
        text="",
        sender="",
        to="",
        cc="",
        subject="",
        body="",
        since="",
        before="",
        unread=None,
        flagged=None,
        larger_kb=0,
        smaller_kb=0,
        limit=limit,
        offset=offset,
    )


def test_search_is_newest_first_and_reports_uidvalidity(
    monkeypatch: pytest.MonkeyPatch, opened: list[str | None]
) -> None:
    newest_first_flags: list[bool] = []

    def fake_search(conn: Any, criteria: list[str], newest_first: bool = False) -> list[str]:
        newest_first_flags.append(newest_first)
        return ["3", "2", "1"]

    monkeypatch.setattr(imap, "search_uids", fake_search)
    monkeypatch.setattr(imap, "uidvalidity", lambda conn: 987)
    monkeypatch.setattr(imap, "fetch_summaries", lambda conn, uids: [{"uid": u} for u in uids])

    result = reading._search_mails(_query(limit=2))

    assert newest_first_flags == [True]
    assert opened == ["INBOX"]
    assert result["uidvalidity"] == 987
    assert result["total"] == 3
    assert [item["uid"] for item in result["results"]] == ["3", "2"]


def test_move_to_role_uses_cached_folder_in_one_session(
    monkeypatch: pytest.MonkeyPatch, opened: list[str | None]
) -> None:
    lookups: list[str] = []
    moves: list[tuple[str, str]] = []

    def fake_cached(role: str) -> str:
        lookups.append(role)
        return "Junk Mail"

    def fake_move(conn: Any, ids: str, destination: str) -> None:
        moves.append((ids, destination))

    monkeypatch.setattr(imap, "cached_special_folder", fake_cached)
    monkeypatch.setattr(imap, "move", fake_move)

    destination = organizing._move_to_role(["1", "2"], "SRC", "junk")

    assert destination == "Junk Mail"
    assert lookups == ["junk"]
    assert opened == ["SRC"]
    assert moves == [(imap.uid_set(["1", "2"]), "Junk Mail")]


def test_create_and_rename_folder_clear_the_role_cache(
    monkeypatch: pytest.MonkeyPatch, opened: list[str | None]
) -> None:
    cleared: list[bool] = []
    monkeypatch.setattr(imap, "clear_caches", lambda: cleared.append(True))

    assert account._create_folder("Projects") == "Folder created: Projects"
    assert account._rename_folder("Old", "New") == "Old -> New"
    assert cleared == [True, True]
