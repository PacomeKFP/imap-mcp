"""Credentials come from the system keyring only; settings are validated and read lazily."""

from __future__ import annotations

import dataclasses
import os
import ssl
import subprocess
import sys
from collections.abc import Callable
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest
from keyring.errors import KeyringError

from imap_mcp import config, smtp
from imap_mcp.config import Settings, get_settings
from imap_mcp.errors import ConfigError

SettingsFactory = Callable[..., Settings]

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


def test_import_reads_no_environment() -> None:
    env = {**os.environ, "MAIL_PORT": "abc"}
    result = subprocess.run(
        [sys.executable, "-c", "import imap_mcp.config, imap_mcp.cli"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_settings_proxy_forwards_to_loaded_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAIL_HOST", "imap.example.com")
    get_settings.cache_clear()
    assert config.settings.imap_host == "imap.example.com"
    assert config.settings == get_settings()


def test_settings_proxy_supports_dataclass_replace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAIL_HOST", "imap.example.com")
    get_settings.cache_clear()
    replaced = dataclasses.replace(config.settings, tls_insecure=True)
    assert type(replaced) is Settings
    assert replaced.tls_insecure is True
    assert replaced.imap_host == "imap.example.com"


def test_settings_are_cached_until_cache_cleared(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAIL_HOST", "first.example.com")
    get_settings.cache_clear()
    assert get_settings().imap_host == "first.example.com"
    monkeypatch.setenv("MAIL_HOST", "second.example.com")
    assert get_settings().imap_host == "first.example.com"
    get_settings.cache_clear()
    assert get_settings().imap_host == "second.example.com"


def test_timeout_defaults_to_module_constant() -> None:
    assert config.TIMEOUT == 40
    assert get_settings().timeout == config.TIMEOUT


def test_timeout_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAIL_TIMEOUT", "12")
    get_settings.cache_clear()
    assert get_settings().timeout == 12


@pytest.mark.parametrize("value", ["0", "-5", "soon"])
def test_timeout_must_be_positive_integer(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("MAIL_TIMEOUT", value)
    with pytest.raises(ConfigError, match="MAIL_TIMEOUT"):
        config.load_settings()


@pytest.mark.parametrize("name", ["MAIL_PORT", "MAIL_SMTP_PORT"])
@pytest.mark.parametrize("value", ["0", "65536", "-1"])
def test_ports_must_be_in_range(monkeypatch: pytest.MonkeyPatch, name: str, value: str) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigError, match=name):
        config.load_settings()


def test_port_must_be_integer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAIL_PORT", "abc")
    with pytest.raises(ConfigError, match="MAIL_PORT must be an integer"):
        config.load_settings()


def test_port_upper_bound_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAIL_PORT", "65535")
    assert config.load_settings().imap_port == 65535


@pytest.mark.parametrize("value", ["0", "-1"])
def test_max_body_must_be_positive(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("MAIL_MAX_BODY", value)
    with pytest.raises(ConfigError, match="MAIL_MAX_BODY"):
        config.load_settings()


def test_download_dir_expands_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAIL_DOWNLOAD_DIR", "~/imap-mcp-downloads")
    assert config.load_settings().download_dir == Path.home() / "imap-mcp-downloads"


class FakeSMTPServer:
    """Records the constructor arguments of smtplib.SMTP / SMTP_SSL."""

    def __init__(
        self, host: str, port: int, *, timeout: float, context: ssl.SSLContext | None = None
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.context = context

    def __enter__(self) -> FakeSMTPServer:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def starttls(self, *, context: ssl.SSLContext) -> None:
        return None

    def login(self, user: str, password: str) -> None:
        return None

    def send_message(self, message: EmailMessage, *, from_addr: str, to_addrs: list[str]) -> None:
        return None


@pytest.mark.parametrize("port", [587, 465])
def test_smtp_uses_configured_timeout(
    monkeypatch: pytest.MonkeyPatch,
    make_settings: SettingsFactory,
    tmp_path: Path,
    port: int,
) -> None:
    created: list[FakeSMTPServer] = []

    class Recorder(FakeSMTPServer):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            created.append(self)

    monkeypatch.setattr(smtp.smtplib, "SMTP", Recorder)
    monkeypatch.setattr(smtp.smtplib, "SMTP_SSL", Recorder)
    monkeypatch.setattr(
        smtp,
        "settings",
        make_settings(tmp_path, smtp_host="smtp.test", smtp_port=port, timeout=7),
    )
    _install_keyring(monkeypatch, _stored())
    smtp.send(EmailMessage(), [ADDRESS])
    assert len(created) == 1
    assert created[0].timeout == 7
    assert created[0].port == port
