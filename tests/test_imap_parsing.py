"""Parsing of raw LIST and STATUS responses, as imaplib hands them over."""
from __future__ import annotations

from typing import Any

import pytest

from imap_mcp import imap


class ParseConn:
    """Serves canned LIST and STATUS data in the shape imaplib returns it."""

    def __init__(self, rows: list[Any] | None = None,
                 status: tuple[str, list[Any]] | None = None) -> None:
        self.rows = rows or []
        self.status_reply = status
        self.status_args: tuple[Any, ...] | None = None

    def list(self):
        return "OK", self.rows

    def status(self, mailbox: str, items: str):
        self.status_args = (mailbox, items)
        return self.status_reply


def names(folders: list[imap.Folder]) -> list[str]:
    return [folder.name for folder in folders]


def test_list_parses_plain_and_non_ascii_modified_utf7_names():
    conn = ParseConn(rows=[
        b'(\\HasNoChildren) "/" "INBOX"',
        b'(\\HasNoChildren \\Sent) "/" "&AMk-l&AOk-ments envoy&AOk-s"',
        b'(\\HasNoChildren) "/" "&AMk-t&AOk-"',
    ])
    folders = imap.list_folders(conn)
    assert names(folders) == ["INBOX", "Éléments envoyés", "Été"]
    assert folders[1].role == "sent"
    assert "\\hasnochildren" in folders[0].attributes


def test_list_unescapes_quoted_names():
    conn = ParseConn(rows=[
        b'(\\HasNoChildren) "/" "Dossier \\"Projets\\""',
        b'(\\HasNoChildren) "/" "C:\\\\Temp"',
    ])
    assert names(imap.list_folders(conn)) == ['Dossier "Projets"', "C:\\Temp"]


def test_list_keeps_rows_delivered_as_literals():
    # A literal name arrives as (header ending in {N}, name bytes); the closing
    # paren of the line may follow as a separate bytes item.
    conn = ParseConn(rows=[
        (b'(\\HasNoChildren) "/" {11}', b"&AMk-t&AOk-"),
        b")",
        (b'(\\HasNoChildren \\Trash) "/" {5}', b"Trash"),
        b"",
    ])
    folders = imap.list_folders(conn)
    assert names(folders) == ["Été", "Trash"]
    assert folders[1].role == "trash"


def test_list_skips_unparseable_rows():
    conn = ParseConn(rows=[b"garbage", None, b'(\\HasNoChildren) "/" "INBOX"'])
    assert names(imap.list_folders(conn)) == ["INBOX"]


@pytest.mark.parametrize("reply, expected", [
    (b'"INBOX" (MESSAGES 231 UNSEEN 12)', (231, 12)),
    (b'"INBOX" (UNSEEN 12 MESSAGES 231)', (231, 12)),
    (b'"Sent Items" (UIDNEXT 9 MESSAGES 4 UNSEEN 0)', (4, 0)),
    (b'"INBOX" (MESSAGES 3)', None),
    (b'"INBOX" (UNSEEN 3)', None),
])
def test_folder_status_reads_each_count_independently(reply, expected):
    conn = ParseConn(status=("OK", [reply]))
    assert imap.folder_status(conn, "INBOX") == expected


def test_folder_status_accepts_literal_mailbox_name():
    conn = ParseConn(status=("OK", [(b"{5}", b"INBOX"), b" (UNSEEN 2 MESSAGES 7)"]))
    assert imap.folder_status(conn, "INBOX") == (7, 2)


def test_folder_status_returns_none_when_unavailable():
    assert imap.folder_status(ParseConn(status=("NO", [b"no such mailbox"])), "X") is None
    assert imap.folder_status(ParseConn(status=("OK", [])), "X") is None


def test_folder_status_quotes_the_mailbox_name():
    conn = ParseConn(status=("OK", [b'"&AMk-t&AOk-" (MESSAGES 1 UNSEEN 0)']))
    assert imap.folder_status(conn, "Été") == (1, 0)
    assert conn.status_args == ('"&AMk-t&AOk-"', "(MESSAGES UNSEEN)")
