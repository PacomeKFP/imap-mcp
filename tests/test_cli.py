"""Command-line entry point: help and error handling without tracebacks."""

from __future__ import annotations

import pytest

from imap_mcp import cli, config


class FakeKeyring:
    """Keyring without any stored entry."""

    def get_password(self, service: str, username: str) -> None:
        return None


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
