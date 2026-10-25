"""Pure helpers of the reading tools: date and paging validation, safe file names, thread connections."""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from imap_mcp import imap
from imap_mcp.errors import MailError
from imap_mcp.tools import reading
from imap_mcp.tools.reading import _imap_date, _page, safe_filename


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("01-Oct-2026", "1-Oct-2026"),
        ("1-oct-2026", "1-Oct-2026"),
        ("29-FEB-2028", "29-Feb-2028"),
        ("31-Dec-1999", "31-Dec-1999"),
    ],
)
def test_imap_date_accepts_real_days(value: str, expected: str) -> None:
    assert _imap_date(value, "since") == expected


@pytest.mark.parametrize(
    "value",
    [
        "99-Xyz-2026",
        "01-Foo-2026",
        "31-Feb-2026",
        "29-Feb-2026",
        "00-Jan-2026",
        "32-Jan-2026",
        "2026-10-01",
        "1-Oct-26",
        "01 Oct 2026",
        " 01-Oct-2026",
    ],
)
def test_imap_date_rejects_invalid_values(value: str) -> None:
    with pytest.raises(MailError) as excinfo:
        _imap_date(value, "before")
    assert excinfo.value.code == "invalid_argument"
    assert "before" in str(excinfo.value)


def test_page_clamps_limit_to_range() -> None:
    assert _page(0, 0) == (1, 0)
    assert _page(-5, 3) == (1, 3)
    assert _page(20, 0) == (20, 0)
    assert _page(500, 40) == (100, 40)


def test_page_rejects_negative_offset() -> None:
    with pytest.raises(MailError) as excinfo:
        _page(20, -1)
    assert excinfo.value.code == "invalid_argument"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("report.pdf", "report.pdf"),
        ("a/b\\c:d*e?.txt", "a_b_c_d_e_.txt"),
        ("  spaced name.doc  ", "spaced name.doc"),
        ("trailing dots...", "trailing dots"),
        ("ends with space . ", "ends with space"),
        ("CON", "_CON"),
        ("con.txt", "_con.txt"),
        ("NUL.tar.gz", "_NUL.tar.gz"),
        ("com1", "_com1"),
        ("LPT9.log", "_LPT9.log"),
        ("COM10", "COM10"),
        ("console.txt", "console.txt"),
        ("", "fallback"),
        ("...", "fallback"),
    ],
)
def test_safe_filename(name: str, expected: str) -> None:
    assert safe_filename(name, "fallback") == expected


RAW_MESSAGE = b"Message-ID: <a@example.test>\r\nSubject: hi\r\n\r\nbody\r\n"


def test_get_thread_uses_one_connection_and_selects_sent_on_it(monkeypatch) -> None:
    sessions: list[str | None] = []
    selected: list[str] = []
    searched: list[str] = []
    conn = object()

    @contextmanager
    def fake_session(folder=None, readonly=True):
        sessions.append(folder)
        yield conn

    def fake_search(connection, criteria, newest_first=False):
        assert connection is conn
        searched.append(criteria[-1])
        return ["1"]

    def fake_select(connection, folder, readonly=True):
        assert connection is conn
        selected.append(folder)
        return 0

    monkeypatch.setattr(imap, "session", fake_session)
    monkeypatch.setattr(imap, "fetch_raw", lambda connection, uid: (RAW_MESSAGE, ""))
    monkeypatch.setattr(imap, "special_folder", lambda connection, role: "Sent")
    monkeypatch.setattr(imap, "select", fake_select)
    monkeypatch.setattr(imap, "search_uids", fake_search)
    monkeypatch.setattr(
        imap,
        "fetch_summaries",
        lambda connection, uids: [{"date": "2026-01-02T10:00:00+00:00", "uid": uids[0]}],
    )

    thread = reading._get_thread("1", "INBOX")

    assert sessions == ["INBOX"]
    assert selected == ["Sent"]
    assert {item["folder"] for item in thread} == {"INBOX", "Sent"}
    assert searched


def test_get_thread_does_not_reselect_when_sent_is_the_same_folder(monkeypatch) -> None:
    selected: list[str] = []
    monkeypatch.setattr(imap, "session", lambda folder=None, readonly=True: _connection())
    monkeypatch.setattr(imap, "fetch_raw", lambda connection, uid: (RAW_MESSAGE, ""))
    monkeypatch.setattr(imap, "special_folder", lambda connection, role: "INBOX")
    monkeypatch.setattr(
        imap, "select", lambda connection, folder, readonly=True: selected.append(folder)
    )
    monkeypatch.setattr(imap, "search_uids", lambda connection, criteria, newest_first=False: [])
    monkeypatch.setattr(imap, "fetch_summaries", lambda connection, uids: [])

    assert reading._get_thread("1", "INBOX") == []
    assert selected == []


@contextmanager
def _connection():
    yield object()
