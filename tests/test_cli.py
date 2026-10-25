"""Command-line entry point: help and error handling without tracebacks."""

from __future__ import annotations

import pytest

from imap_mcp import cli, config
from imap_mcp.config import SERVICE, load_settings


class FakeKeyring:
    """Keyring without any stored entry."""

    def get_password(self, service: str, username: str) -> None:
        return None


class MemoryKeyring:
    """Keyring that keeps entries in a dict, shared by `cli` and `config`."""

    def __init__(self) -> None:
        self.entries: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.entries.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.entries[(service, username)] = password


@pytest.fixture
def keyring_store(monkeypatch: pytest.MonkeyPatch) -> MemoryKeyring:
    store = MemoryKeyring()
    monkeypatch.setattr(cli, "keyring", store)
    monkeypatch.setattr(config, "keyring", store)
    return store


@pytest.fixture
def answers(monkeypatch: pytest.MonkeyPatch, keyring_store: MemoryKeyring) -> list[str]:
    """Scripted answers for the setup prompts; the connection check always succeeds."""
    responses: list[str] = []
    monkeypatch.setattr("builtins.input", lambda _prompt="": responses.pop(0))
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt="": "s3cret")
    monkeypatch.setattr(cli, "check_connection", lambda: 0)
    return responses


@pytest.fixture
def server_calls(monkeypatch: pytest.MonkeyPatch) -> list[None]:
    """Replace the MCP server with a recorder so that no stdio server starts."""
    calls: list[None] = []
    monkeypatch.setattr(cli, "run_server", lambda: calls.append(None))
    return calls


def test_help_does_not_load_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAIL_PORT", "abc")
    with pytest.raises(SystemExit) as info:
        cli.main(["--help"])
    assert info.value.code == 0


def test_check_reports_invalid_settings(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MAIL_PORT", "abc")
    assert cli.main(["check"]) == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("Failed: [config] MAIL_PORT must be an integer")
    assert "Traceback" not in captured.err


def test_check_reports_missing_credentials(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MAIL_HOST", "imap.example.com")
    monkeypatch.setattr(config, "keyring", FakeKeyring())
    assert cli.main(["check"]) == 1
    captured = capsys.readouterr()
    assert "Failed: [config] Credentials missing" in captured.err
    assert "imap-mcp setup" in captured.err


def test_server_start_reports_invalid_settings(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    server_calls: list[None],
) -> None:
    monkeypatch.setenv("MAIL_MAX_BODY", "0")
    assert cli.main([]) == 1
    assert server_calls == []
    assert capsys.readouterr().err.startswith(
        "Failed: [config] MAIL_MAX_BODY must be a positive integer"
    )


def test_default_command_starts_server(
    monkeypatch: pytest.MonkeyPatch, server_calls: list[None]
) -> None:
    monkeypatch.setenv("MAIL_HOST", "imap.example.com")
    assert cli.main([]) == 0
    assert server_calls == [None]


def test_setup_stores_account_with_suggested_servers(
    keyring_store: MemoryKeyring, answers: list[str]
) -> None:
    answers.extend(["alice@example.com", "", ""])
    assert cli.setup_account() == 0
    assert keyring_store.get_password(SERVICE, "user") == "alice@example.com"
    assert keyring_store.get_password(SERVICE, "alice@example.com") == "s3cret"
    assert keyring_store.get_password(SERVICE, "alice@example.com:imap-host") == (
        "imap.example.com"
    )
    assert keyring_store.get_password(SERVICE, "alice@example.com:smtp-host") == (
        "smtp.example.com"
    )


def test_setup_accepts_custom_servers(keyring_store: MemoryKeyring, answers: list[str]) -> None:
    answers.extend(["bob@corp.test", "mail.corp.test", "out.corp.test"])
    assert cli.setup_account() == 0
    assert keyring_store.get_password(SERVICE, "bob@corp.test:imap-host") == "mail.corp.test"
    assert keyring_store.get_password(SERVICE, "bob@corp.test:smtp-host") == "out.corp.test"


def test_setup_rejects_invalid_address(
    keyring_store: MemoryKeyring, answers: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    answers.append("not-an-address")
    assert cli.setup_account() == 1
    assert keyring_store.entries == {}
    assert "valid email address" in capsys.readouterr().err


def test_settings_fall_back_to_keyring_servers(
    monkeypatch: pytest.MonkeyPatch, keyring_store: MemoryKeyring
) -> None:
    monkeypatch.delenv("MAIL_HOST", raising=False)
    monkeypatch.delenv("MAIL_SMTP_HOST", raising=False)
    keyring_store.set_password(SERVICE, "user", "bob@corp.test")
    keyring_store.set_password(SERVICE, "bob@corp.test", "s3cret")
    keyring_store.set_password(SERVICE, "bob@corp.test:imap-host", "mail.corp.test")
    settings = load_settings()
    assert settings.imap_host == "mail.corp.test"
    assert settings.smtp_host == "mail.corp.test"
