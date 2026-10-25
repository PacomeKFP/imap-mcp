"""Credentials are read from the system keyring only."""

from __future__ import annotations

import pytest
from keyring.errors import KeyringError

from imap_mcp import config
from imap_mcp.errors import ConfigError

ADDRESS = "me@example.com"
CREDENTIAL = "s3cr3t-test-value"
ROTATED = "rotated-test-value"


class FakeKeyring:
    """In-memory replacement for the `keyring` module."""

    def __init__(self, entries: dict[tuple[str, str], str] | None = None) -> None:
        self.entries: dict[tuple[str, str], str] = dict(entries or {})
        self.calls = 0

    def get_password(self, service: str, username: str) -> str | None:
        self.calls += 1
        return self.entries.get((service, username))


def _install_keyring(monkeypatch: pytest.MonkeyPatch, fake: FakeKeyring) -> None:
    monkeypatch.setattr(config, "keyring", fake)


def _stored() -> FakeKeyring:
    return FakeKeyring(
        {
            (config.SERVICE, config.ACCOUNT_KEY): ADDRESS,
            (config.SERVICE, ADDRESS): CREDENTIAL,
        }
    )


def test_reads_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_keyring(monkeypatch, _stored())
    assert config.get_credentials() == (ADDRESS, CREDENTIAL)


def test_environment_variables_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAIL_USER", "env@example.com")
    monkeypatch.setenv("MAIL_PASSWORD", "env-only-value")
    _install_keyring(monkeypatch, FakeKeyring())
    with pytest.raises(ConfigError) as info:
        config.get_credentials()
    assert info.value.code == "config"
    assert "imap-mcp setup" in str(info.value)
    assert "env-only-value" not in str(info.value)


def test_environment_variables_do_not_override_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAIL_USER", "env@example.com")
    monkeypatch.setenv("MAIL_PASSWORD", "env-only-value")
    _install_keyring(monkeypatch, _stored())
    assert config.get_credentials() == (ADDRESS, CREDENTIAL)


def test_missing_account_raises_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_keyring(monkeypatch, FakeKeyring())
    with pytest.raises(ConfigError) as info:
        config.get_credentials()
    assert info.value.code == "config"
    assert "imap-mcp setup" in str(info.value)


def test_missing_password_raises_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeKeyring({(config.SERVICE, config.ACCOUNT_KEY): ADDRESS})
    _install_keyring(monkeypatch, fake)
    with pytest.raises(ConfigError, match="imap-mcp setup"):
        config.get_credentials()


def test_keyring_error_is_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    class BrokenKeyring(FakeKeyring):
        def get_password(self, service: str, username: str) -> str | None:
            raise KeyringError("backend unavailable")

    _install_keyring(monkeypatch, BrokenKeyring())
    with pytest.raises(ConfigError) as info:
        config.get_credentials()
    assert info.value.code == "config"
    assert "backend unavailable" in str(info.value)
    assert isinstance(info.value.__cause__, KeyringError)


def test_result_is_cached_until_cache_cleared(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _stored()
    _install_keyring(monkeypatch, fake)
    config.get_credentials()
    calls_after_first = fake.calls
    config.get_credentials()
    assert fake.calls == calls_after_first

    fake.entries[(config.SERVICE, ADDRESS)] = ROTATED
    assert config.get_credentials() == (ADDRESS, CREDENTIAL)
    config.get_credentials.cache_clear()
    assert config.get_credentials() == (ADDRESS, ROTATED)


def test_failures_are_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeKeyring()
    _install_keyring(monkeypatch, fake)
    with pytest.raises(ConfigError):
        config.get_credentials()
    fake.entries.update(_stored().entries)
    assert config.get_credentials() == (ADDRESS, CREDENTIAL)
