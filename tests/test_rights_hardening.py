"""Hardening: alias equivalence, session hints, UTC timestamps, atomic saves and recipient validation."""

from __future__ import annotations

import asyncio
import imaplib
import json
import re
from argparse import ArgumentParser, Namespace
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.shared.exceptions import MCPError

from imap_mcp import approval, journal, permissions, rights, rights_cli
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
    layers = layer(
        rights.Rule("mail.read", rights.ASK, "Drafts"),
        rights.Rule("mail.read", rights.DENY, "@drafts"),
    )
    assert effective_decision(layers, "mail.read", "Drafts", ROLES, "/")[0] == rights.DENY


def test_default_is_kept_when_no_name_has_a_rule() -> None:
    assert effective_decision([], "mail.create", "@drafts", ROLES, "/") == (rights.ALLOW, "default")
    assert effective_decision([], "mail.create", "Drafts", ROLES, "/") == (rights.ASK, "default")


def test_guard_denies_alias_when_real_name_is_denied(
    tmp_path: Path, make_settings: SettingsFactory
) -> None:
    guard = make_guard(make_settings, tmp_path)
    rights.save(
        rights.local_file(guard.config),
        rights.RightsFile([rights.Rule("mail.read", rights.DENY, "Drafts")]),
    )
    with pytest.raises(MailError) as info:
        authorize(guard, "mail.read", targets=["@drafts"])
    assert info.value.code == "denied"


def test_guard_without_folder_targets_never_resolves_roles(
    tmp_path: Path, make_settings: SettingsFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard = Guard(make_settings(tmp_path), SESSION)
    guard.separator = "/"

    def fail() -> dict[str, str]:
        raise AssertionError("IMAP must not be called")

    monkeypatch.setattr(permissions, "_special_folders", fail)
    authorize(guard, "mail.read")
    authorize(guard, "file.download", targets=["@downloads"])
    assert guard.roles is None


@pytest.mark.parametrize(
    "error", [MailError("down", code="imap"), OSError("refused"), imaplib.IMAP4.error("bye")]
)
def test_failed_role_lookup_falls_back_and_is_retried(
    tmp_path: Path,
    make_settings: SettingsFactory,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
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


def test_roles_are_resolved_once_and_cached(
    tmp_path: Path, make_settings: SettingsFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
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


def test_permission_hint_carries_the_current_session(
    tmp_path: Path, make_settings: SettingsFactory
) -> None:
    guard = make_guard(make_settings, tmp_path)
    with pytest.raises(MailError) as info:
        asyncio.run(guard.authorize(NoElicitation(), "mail.delete", targets=["INBOX"]))
    assert info.value.code == "permission_required"
    assert f"--session {SESSION}" in str(info.value)


def test_hint_names_the_session_for_folder_and_send_rules() -> None:
    assert permissions._hint("mail.move", SESSION) == (
        f"imap-mcp rights allow mail.move [--folder FOLDER] --session {SESSION}"
    )
    assert permissions._hint("mail.send", SESSION) == (
        f"imap-mcp rights allow mail.send --recipient ADDRESS --session {SESSION}"
    )


def test_session_ids_use_utc_and_the_expected_format() -> None:
    session = permissions.new_session_id()
    assert re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{4}", session)
    stamp = datetime.strptime(session[:15], "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)
    assert abs(datetime.now(timezone.utc) - stamp) < timedelta(minutes=1)


def test_journal_timestamps_are_timezone_aware(
    tmp_path: Path, make_settings: SettingsFactory
) -> None:
    config = make_settings(tmp_path)
    journal.record(config, SESSION, "mail.move", [("INBOX", rights.ASK, "default")], [], "refused")
    entry = json.loads(
        (rights.session_dir(config, SESSION) / rights.JOURNAL_FILE).read_text("utf-8")
    )
    assert datetime.fromisoformat(entry["time"]).utcoffset() == timedelta(0)


def test_save_leaves_no_temporary_file(tmp_path: Path) -> None:
    path = tmp_path / "rights.toml"
    rights.save(path, rights.RightsFile([rights.Rule("mail.move", rights.ALLOW, "Clients")]))
    assert rights.load(path).rules == [rights.Rule("mail.move", rights.ALLOW, "Clients")]
    assert not path.with_suffix(".tmp").exists()


def test_failed_save_keeps_the_previous_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "rights.toml"
    previous = [rights.Rule("mail.move", rights.ALLOW, "Clients")]
    rights.save(path, rights.RightsFile(previous))

    def disk_full(self: Path, target: str | Path) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(Path, "replace", disk_full)
    with pytest.raises(OSError):
        rights.save(path, rights.RightsFile())
    monkeypatch.undo()
    assert rights.load(path).rules == previous
    assert not path.with_suffix(".tmp").exists()


class ElicitingContext:
    """Context whose elicit call fails with the given exception."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    async def elicit(self, message: str, schema: Callable[..., Any]) -> None:
        raise self.error


@pytest.mark.parametrize(
    "error",
    [MCPError(-32601, "elicitation unsupported"), RuntimeError("test double without elicitation")],
    ids=["mcp-error", "runtime-error"],
)
def test_clients_that_cannot_elicit_map_to_none(error: Exception) -> None:
    result = asyncio.run(
        approval.ask_user(ElicitingContext(error), "mail.delete", ["INBOX"], [], "")
    )
    assert result is None


def test_unexpected_elicitation_errors_propagate() -> None:
    with pytest.raises(KeyError):
        asyncio.run(
            approval.ask_user(ElicitingContext(KeyError("bug")), "mail.delete", ["INBOX"], [], "")
        )


def parse(*argv: str) -> Namespace:
    parser = ArgumentParser(prog="imap-mcp")
    rights_cli.add_parser(parser.add_subparsers(dest="command"))
    return parser.parse_args(["rights", *argv])


@pytest.mark.parametrize(
    "entry",
    [
        "bob",
        "@",
        "@@x.org",
        "a@b@c.org",
        "@b@c",
        "a@",
        "a@@b.org",
        "bob@x.org@",
        "@a b.org",
        "a @b.org",
    ],
)
def test_invalid_recipient_entries_are_rejected(
    tmp_path: Path, make_settings: SettingsFactory, entry: str
) -> None:
    config = make_settings(tmp_path)
    assert rights_cli.run(parse("allow", "mail.send", "--recipient", entry, "--local"), config) == 1
    assert not rights.local_file(config).exists()


def test_invalid_recipient_error_has_invalid_argument_code() -> None:
    with pytest.raises(MailError) as info:
        rights_cli._recipient_entry("bob")
    assert info.value.code == "invalid_argument"


def test_recipient_entries_are_stored_lowercase_and_revoked_case_insensitively(
    tmp_path: Path, make_settings: SettingsFactory
) -> None:
    config = make_settings(tmp_path)
    grant = parse(
        "allow",
        "mail.send",
        "--recipient",
        "Anna@Client.COM",
        "--recipient",
        "@Client.com",
        "--local",
    )
    assert rights_cli.run(grant, config) == 0
    assert rights.load(rights.local_file(config)).recipients == ["anna@client.com", "@client.com"]
    revoke = parse("revoke", "mail.send", "--recipient", "ANNA@client.com", "--local")
    assert rights_cli.run(revoke, config) == 0
    assert rights.load(rights.local_file(config)).recipients == ["@client.com"]


def test_separator_lookup_failure_falls_back_to_slash_and_is_retried(
    tmp_path: Path,
    make_settings: SettingsFactory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    guard = Guard(make_settings(tmp_path), SESSION)
    guard.roles = dict(ROLES)
    calls: list[int] = []

    def down() -> str:
        calls.append(1)
        raise MailError("server down", code="imap")

    monkeypatch.setattr(permissions.imap, "folder_separator", down)
    authorize(guard, "mail.read", targets=["INBOX"])
    authorize(guard, "mail.read", targets=["INBOX"])
    assert guard.separator is None
    assert len(calls) == 2
    assert "assuming '/'" in capsys.readouterr().err

    monkeypatch.setattr(permissions.imap, "folder_separator", lambda: ".")
    authorize(guard, "mail.read", targets=["INBOX"])
    assert guard.separator == "."


def test_failed_journal_write_keeps_the_action_and_warns(
    tmp_path: Path, make_settings: SettingsFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    guard = make_guard(make_settings, tmp_path)
    sessions = rights.sessions_dir(guard.config)
    sessions.parent.mkdir(parents=True)
    sessions.write_text("not a folder", encoding="utf-8")  # makes every session write fail
    authorize(guard, "mail.create", targets=[rights.DRAFTS])
    assert "cannot write the audit journal" in capsys.readouterr().err
    assert not (sessions / SESSION / rights.JOURNAL_FILE).exists()
