"""Source-folder checks of the organizing tools, with the guard and IMAP faked out."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager

import pytest

from imap_mcp import imap, rights
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
        if fn is organizing._move_to_role:
            return {"count": 1, "folder": "SRC", "destination": "Archive"}
        return "done"

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
    assert result == {"count": 1, "folder": "SRC", "destination": "Archive"}


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


@pytest.fixture
def imap_calls(monkeypatch):
    """Record the IMAP writes of the organizing helpers without touching a server."""
    stored: list[tuple[str, bool]] = []
    moved: list[tuple[str, str]] = []
    ensured: list[str] = []
    sessions: list[tuple[str | None, bool]] = []

    @contextmanager
    def fake_session(folder=None, readonly=True):
        sessions.append((folder, readonly))
        yield object()

    monkeypatch.setattr(imap, "session", fake_session)
    monkeypatch.setattr(imap, "store", lambda conn, ids, flag, value: stored.append((flag, value)))
    monkeypatch.setattr(
        imap, "move", lambda conn, ids, destination: moved.append((ids, destination))
    )
    monkeypatch.setattr(imap, "ensure_folder", lambda conn, name: ensured.append(name))
    return stored, moved, ensured, sessions


def test_mark_reports_only_the_states_that_were_set(imap_calls):
    stored, _, _, sessions = imap_calls
    result = organizing._mark_mails(["1", "2"], "SRC", True, None, False)
    assert result == {"count": 2, "folder": "SRC", "changes": {"read": True, "answered": False}}
    assert stored == [(r"\Seen", True), (r"\Answered", False)]
    assert sessions == [("SRC", False)]


def test_trash_checks_delete_right_and_moves_to_agent_trash(calls, imap_calls):
    recorded, ran = calls
    run(organizing.trash_mails(None, ["1"], folder="SRC"))
    assert recorded == [("mail.delete", ["SRC"])]
    assert ran == ["_trash_mails"]


def test_trash_moves_to_agent_trash_and_reports_it(imap_calls):
    _, moved, ensured, _ = imap_calls
    trash = organizing.settings.agent_trash
    result = organizing._trash_mails(["1"], "SRC")
    assert result == {"count": 1, "folder": "SRC", "destination": trash}
    assert ensured == [trash]
    assert moved == [(imap.uid_set(["1"]), trash)]
