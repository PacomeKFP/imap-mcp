"""IMAP move path: UID MOVE, COPY + UID EXPUNGE fallback, refusal without UIDPLUS, and no unscoped purge."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from imap_mcp import imap
from imap_mcp.errors import MailError


class FakeConn:
    def __init__(self, capabilities: tuple[str, ...]) -> None:
        self.capabilities = capabilities
        self.calls: list[tuple[str, tuple]] = []

    def uid(self, command: str, *args):
        self.calls.append((command, args))
        return "OK", [None]


def test_move_uses_uid_move_when_the_server_has_it():
    conn = FakeConn(("IMAP4rev1", "MOVE"))
    imap.move(conn, "1,2", "Agent Trash")
    assert [command for command, _ in conn.calls] == ["MOVE"]


def test_move_without_move_copies_then_expunges_only_the_given_uids():
    conn = FakeConn(("IMAP4rev1", "UIDPLUS"))
    imap.move(conn, "1,2", "Agent Trash")
    assert [command for command, _ in conn.calls] == ["COPY", "STORE", "EXPUNGE"]
    assert conn.calls[2][1] == ("1,2",)


def test_move_refuses_without_move_or_uidplus():
    conn = FakeConn(("IMAP4rev1",))
    with pytest.raises(MailError) as info:
        imap.move(conn, "1", "Agent Trash")
    assert info.value.code == "move_unsupported"
    assert conn.calls == []


def test_source_never_purges_without_a_uid_scope():
    root = Path(__file__).resolve().parents[1] / "src" / "imap_mcp"
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert ".expunge(" not in text, path
        assert len(re.findall(r'"EXPUNGE"', text)) == len(re.findall(r'uid\("EXPUNGE"', text)), path
