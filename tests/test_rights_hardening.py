"""Hardening: alias equivalence, session hints, UTC timestamps, atomic saves and recipient validation."""
from __future__ import annotations

import asyncio
import imaplib
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from imap_mcp import permissions, rights
from imap_mcp.config import Settings
from imap_mcp.errors import MailError
from imap_mcp.permissions import Guard, counterparts, effective_decision

SESSION = "20260101-000000-aaaa"
ROLES = {rights.DRAFTS: "Drafts", rights.ARCHIVE: "Archive", rights.JUNK: "Junk"}
SettingsFactory = Callable[..., Settings]


def layer(*rules: rights.Rule, source: str = "local") -> list[rights.Layer]:
    return [(source, rights.RightsFile(list(rules)))]


def make_guard(make_settings: SettingsFactory, tmp_path: Path, read_only: bool = False) -> Guard:
    guard = Guard(make_settings(tmp_path, read_only=read_only), SESSION)
    guard.separator = "/"
    guard.roles = dict(ROLES)
    return guard


def authorize(guard: Guard, action: str, **kwargs: Any) -> None:
    asyncio.run(guard.authorize(SimpleNamespace(), action, **kwargs))


def test_counterparts_map_real_names_and_aliases_both_ways() -> None:
    assert counterparts("Drafts", ROLES) == ["Drafts", "@drafts"]
    assert counterparts("@archive", ROLES) == ["@archive", "Archive"]
    assert counterparts("INBOX", ROLES) == ["INBOX"]
    assert counterparts(None, ROLES) == [None]


def test_deny_on_real_name_blocks_alias_target() -> None:
    layers = layer(rights.Rule("mail.read", rights.DENY, "Drafts"))
    assert effective_decision(layers, "mail.read", "@drafts", ROLES, "/") == (rights.DENY, "local")


def test_deny_on_alias_blocks_real_name_target() -> None:
    layers = layer(rights.Rule("mail.move", rights.DENY, "@archive"))
    assert effective_decision(layers, "mail.move", "Archive", ROLES, "/") == (rights.DENY, "local")


def test_strictest_explicit_verdict_wins_across_names() -> None:
    layers = layer(rights.Rule("mail.read", rights.ASK, "Drafts"),
                   rights.Rule("mail.read", rights.DENY, "@drafts"))
    assert effective_decision(layers, "mail.read", "Drafts", ROLES, "/")[0] == rights.DENY


def test_default_is_kept_when_no_name_has_a_rule() -> None:
    assert effective_decision([], "mail.create", "@drafts", ROLES, "/") == (rights.ALLOW, "default")
    assert effective_decision([], "mail.create", "Drafts", ROLES, "/") == (rights.ASK, "default")


def test_guard_denies_alias_when_real_name_is_denied(tmp_path: Path,
                                                    make_settings: SettingsFactory) -> None:
    guard = make_guard(make_settings, tmp_path)
    rights.save(rights.local_file(guard.config),
                rights.RightsFile([rights.Rule("mail.read", rights.DENY, "Drafts")]))
    with pytest.raises(MailError) as info:
        authorize(guard, "mail.read", targets=["@drafts"])
    assert info.value.code == "denied"


def test_guard_without_folder_targets_never_resolves_roles(tmp_path: Path, make_settings: SettingsFactory,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    guard = Guard(make_settings(tmp_path), SESSION)
    guard.separator = "/"

    def fail() -> dict[str, str]:
        raise AssertionError("IMAP must not be called")

    monkeypatch.setattr(permissions, "_special_folders", fail)
    authorize(guard, "mail.read")
    authorize(guard, "file.download", targets=["@downloads"])
    assert guard.roles is None


@pytest.mark.parametrize("error", [MailError("down", code="imap"), OSError("refused"),
                                   imaplib.IMAP4.error("bye")])
def test_failed_role_lookup_falls_back_and_is_retried(tmp_path: Path, make_settings: SettingsFactory,
                                                      monkeypatch: pytest.MonkeyPatch,
                                                      error: Exception) -> None:
    guard = Guard(make_settings(tmp_path), SESSION)
    guard.separator = "/"
    calls: list[int] = []

    def flaky() -> dict[str, str]:
        calls.append(1)
        raise error

    monkeypatch.setattr(permissions, "_special_folders", flaky)
    authorize(guard, "mail.read", targets=["Drafts"])  # default allow for reads
    authorize(guard, "mail.read", targets=["Drafts"])
    assert guard.roles is None
    assert len(calls) == 2


def test_roles_are_resolved_once_and_cached(tmp_path: Path, make_settings: SettingsFactory,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    guard = Guard(make_settings(tmp_path), SESSION)
    guard.separator = "/"
    calls: list[int] = []

    def resolve() -> dict[str, str]:
        calls.append(1)
        return dict(ROLES)

    monkeypatch.setattr(permissions, "_special_folders", resolve)
    authorize(guard, "mail.read", targets=["INBOX"])
    authorize(guard, "mail.read", targets=["Drafts"])
    assert len(calls) == 1
    assert guard.roles == ROLES


class NoElicitation:
    """Client that cannot elicit: the call raises, as the test client does."""

    async def elicit(self, message: str, schema: Callable[..., Any]) -> None:
        raise RuntimeError("elicitation not supported")


def test_permission_hint_carries_the_current_session(tmp_path: Path, make_settings: SettingsFactory) -> None:
    guard = make_guard(make_settings, tmp_path)
    with pytest.raises(MailError) as info:
        asyncio.run(guard.authorize(NoElicitation(), "mail.delete", targets=["INBOX"]))
    assert info.value.code == "permission_required"
    assert f"--session {SESSION}" in str(info.value)


def test_hint_names_the_session_for_folder_and_send_rules() -> None:
    assert permissions._hint("mail.move", SESSION) == (
        f"imap-mcp rights allow mail.move [--folder FOLDER] --session {SESSION}")
    assert permissions._hint("mail.send", SESSION) == (
        f"imap-mcp rights allow mail.send --recipient ADDRESS --session {SESSION}")
