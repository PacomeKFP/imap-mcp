"""Guard behaviour: read-only mode, deny, ask, session memory, allowlists and the journal."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from imap_mcp import rights
from imap_mcp.config import Settings
from imap_mcp.errors import MailError
from imap_mcp.permissions import Guard

SESSION = "20260101-000000-aaaa"
GuardFactory = Callable[..., Guard]


class FakeContext:
    """Stands in for the MCP context: answers elicitation with a fixed choice."""

    def __init__(
        self, choice: str = "once", action: str = "accept", supported: bool = True
    ) -> None:
        self.choice = choice
        self.action = action
        self.supported = supported
        self.messages: list[str] = []

    async def elicit(self, message: str, schema: Callable[..., Any]) -> SimpleNamespace:
        if not self.supported:
            raise RuntimeError("elicitation not supported")
        self.messages.append(message)
        if self.action != "accept":
            return SimpleNamespace(action=self.action)
        return SimpleNamespace(action="accept", data=schema(choice=self.choice))


@pytest.fixture
def make_guard(make_settings: Callable[..., Settings]) -> GuardFactory:
    """Return a factory building a `Guard` whose settings live under the given root."""

    def factory(root: Path, read_only: bool = False) -> Guard:
        guard = Guard(make_settings(root, read_only=read_only), SESSION)
        guard.separator = "/"  # avoids a network call in tests
        return guard

    return factory


def authorize(guard: Guard, ctx: FakeContext, action: str, **kwargs: Any) -> None:
    asyncio.run(guard.authorize(ctx, action, **kwargs))


def write_local(guard: Guard, content: rights.RightsFile) -> None:
    rights.save(rights.local_file(guard.config), content)


def test_reads_are_allowed_without_question(tmp_path: Path, make_guard: GuardFactory) -> None:
    ctx = FakeContext()
    authorize(make_guard(tmp_path), ctx, "mail.read", targets=["INBOX"])
    assert ctx.messages == []


def test_read_only_blocks_writes_before_any_question(
    tmp_path: Path, make_guard: GuardFactory
) -> None:
    ctx = FakeContext()
    with pytest.raises(MailError) as info:
        authorize(
            make_guard(tmp_path, read_only=True), ctx, "mail.move", targets=["INBOX", "Archive"]
        )
    assert info.value.code == "read_only"
    assert ctx.messages == []


def test_local_deny_blocks_the_action(tmp_path: Path, make_guard: GuardFactory) -> None:
    guard = make_guard(tmp_path)
    write_local(guard, rights.RightsFile([rights.Rule("mail.move", rights.DENY, "Clients")]))
    with pytest.raises(MailError) as info:
        authorize(guard, FakeContext(), "mail.move", targets=["Clients"])
    assert info.value.code == "denied"


def test_once_is_not_remembered(tmp_path: Path, make_guard: GuardFactory) -> None:
    guard = make_guard(tmp_path)
    authorize(guard, FakeContext("once"), "mail.move", targets=["INBOX"])
    assert not rights.session_file(guard.config, SESSION).exists()
    assert not rights.local_file(guard.config).exists()


def test_session_choice_applies_to_later_calls_of_that_session_only(
    tmp_path: Path, make_guard: GuardFactory
) -> None:
    guard = make_guard(tmp_path)
    authorize(guard, FakeContext("session"), "mail.move", targets=["INBOX"])
    refusing = FakeContext(action="decline")
    authorize(guard, refusing, "mail.move", targets=["INBOX"])
    assert refusing.messages == []
    assert not rights.global_file(guard.config).exists()

    other = Guard(guard.config, "20260202-000000-bbbb")
    other.separator = "/"
    with pytest.raises(MailError) as info:
        authorize(other, FakeContext(action="decline"), "mail.move", targets=["INBOX"])
    assert info.value.code == "refused"


def test_refusal_is_reported(tmp_path: Path, make_guard: GuardFactory) -> None:
    with pytest.raises(MailError) as info:
        authorize(make_guard(tmp_path), FakeContext("refuse"), "mail.delete", targets=["INBOX"])
    assert info.value.code == "refused"


def test_client_without_elicitation_gets_a_clear_code(
    tmp_path: Path, make_guard: GuardFactory
) -> None:
    with pytest.raises(MailError) as info:
        authorize(
            make_guard(tmp_path), FakeContext(supported=False), "mail.delete", targets=["INBOX"]
        )
    assert info.value.code == "permission_required"


def test_send_to_allowlisted_recipients_needs_no_question(
    tmp_path: Path, make_guard: GuardFactory
) -> None:
    guard = make_guard(tmp_path)
    write_local(guard, rights.RightsFile(recipients=["@client.com"]))
    ctx = FakeContext(supported=False)
    authorize(guard, ctx, "mail.send", recipients=["anna@client.com"])
    assert ctx.messages == []


def test_send_to_other_recipients_asks(tmp_path: Path, make_guard: GuardFactory) -> None:
    guard = make_guard(tmp_path)
    ctx = FakeContext("once")
    authorize(guard, ctx, "mail.send", recipients=["anna@client.com", "x@other.org"])
    assert len(ctx.messages) == 1


def test_session_allowlist_covers_recipients_for_this_session(
    tmp_path: Path, make_guard: GuardFactory
) -> None:
    guard = make_guard(tmp_path)
    authorize(guard, FakeContext("session"), "mail.send", recipients=["x@other.org"])
    refusing = FakeContext(action="decline")
    authorize(guard, refusing, "mail.send", recipients=["x@other.org"])
    assert refusing.messages == []
    assert rights.load(rights.session_file(guard.config, SESSION)).recipients == ["x@other.org"]


def test_writes_are_journaled_and_reads_are_not(tmp_path: Path, make_guard: GuardFactory) -> None:
    guard = make_guard(tmp_path)
    authorize(guard, FakeContext(), "mail.read", targets=["INBOX"])
    authorize(guard, FakeContext("once"), "mail.move", targets=["INBOX"])
    journal = rights.session_dir(guard.config, SESSION) / rights.JOURNAL_FILE
    entries = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
    assert [entry["action"] for entry in entries] == ["mail.move"]
    assert entries[0]["outcome"] == "once"
