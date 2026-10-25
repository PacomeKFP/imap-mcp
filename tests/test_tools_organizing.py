"""Source-folder checks of the organizing tools, with the guard and IMAP faked out."""

from __future__ import annotations

import asyncio

import pytest

from imap_mcp import rights
from imap_mcp.errors import MailError
from imap_mcp.tools import organizing


@pytest.fixture
def calls(monkeypatch):
    """Record guard.authorize calls and stub in_thread so no IMAP call happens."""
    recorded: list[tuple[str, list[str]]] = []
    ran: list[str] = []

    async def authorize(ctx, action, targets=None, recipients=None, detail=None):
        recorded.append((action, list(targets or [])))

    async def in_thread(fn, *args):
        ran.append(fn.__name__)
        return "Archive" if fn is organizing._move_to_role else "done"

    monkeypatch.setattr(organizing.guard, "authorize", authorize)
    monkeypatch.setattr(organizing, "in_thread", in_thread)
    return recorded, ran


def run(coro):
    return asyncio.run(coro)


def test_copy_checks_source_read_then_destination_create(calls):
    recorded, ran = calls
    run(organizing.copy_mails(None, ["1"], "DST", folder="SRC"))
    assert recorded == [("mail.read", ["SRC"]), ("mail.create", ["DST"])]
    assert ran == ["_copy_mails"]


def test_move_checks_source_read_then_move(calls):
    recorded, ran = calls
    run(organizing.move_mails(None, ["1"], "DST", folder="SRC"))
    assert recorded == [("mail.read", ["SRC"]), ("mail.move", ["SRC", "DST"])]
    assert ran == ["_move_mails"]


def test_archive_checks_source_read_then_move_to_archive(calls):
    recorded, ran = calls
    result = run(organizing.archive_mails(None, ["1"], folder="SRC"))
    assert recorded == [("mail.read", ["SRC"]), ("mail.move", ["SRC", rights.ARCHIVE])]
    assert ran == ["_move_to_role"]
    assert result == "1 message(s) archived in Archive."


def test_spam_checks_source_read_then_move_to_junk(calls):
    recorded, ran = calls
    run(organizing.mark_spam(None, ["1"], folder="SRC"))
    assert recorded == [("mail.read", ["SRC"]), ("mail.move", ["SRC", rights.JUNK])]
    assert ran == ["_move_to_role"]


@pytest.mark.parametrize(
    "tool, kwargs",
    [
        (organizing.copy_mails, {"destination": "DST"}),
        (organizing.move_mails, {"destination": "DST"}),
        (organizing.archive_mails, {}),
        (organizing.mark_spam, {}),
    ],
)
def test_denied_source_read_stops_before_io(monkeypatch, tool, kwargs):
    ran: list[str] = []

    async def authorize(ctx, action, targets=None, recipients=None, detail=None):
        if action == "mail.read":
            raise MailError("denied", code="permission_required")

    async def in_thread(fn, *args):
        ran.append(fn.__name__)
        return "done"

    monkeypatch.setattr(organizing.guard, "authorize", authorize)
    monkeypatch.setattr(organizing, "in_thread", in_thread)
    with pytest.raises(MailError):
        run(tool(None, ["1"], folder="SRC", **kwargs))
    assert ran == []
