"""Configuration read from environment variables, and credentials (variables or keyring)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import keyring
from keyring.errors import KeyringError

from .errors import ConfigError

SERVICE = "imap-mcp"
TIMEOUT = 40
AGENT_DIR = ".imap-mcp"
ACCOUNT_KEY = "user"  # keyring entry that holds the account address


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: str) -> int:
    raw = os.environ.get(name, "").strip() or default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}.") from None


def _user_config_dir() -> Path:
    base = os.environ.get("APPDATA") or os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / SERVICE


@dataclass(frozen=True)
class Settings:
    imap_host: str
    imap_port: int
    smtp_host: str
    smtp_port: int
    read_only: bool
    download_dir: Path
    max_body: int
    agent_trash: str
    project_dir: Path
    config_dir: Path
    tls_insecure: bool = False  # disables certificate verification; only for trusted test servers


def load_settings() -> Settings:
    imap_host = os.environ.get("MAIL_HOST", "").strip()
    return Settings(
        imap_host=imap_host,
        imap_port=_int("MAIL_PORT", "993"),
        smtp_host=os.environ.get("MAIL_SMTP_HOST", "").strip() or imap_host,
        smtp_port=_int("MAIL_SMTP_PORT", "587"),
        read_only=_flag("MAIL_READONLY"),
        download_dir=Path(os.environ.get("MAIL_DOWNLOAD_DIR")
                          or Path.home() / "Downloads" / SERVICE),
        max_body=_int("MAIL_MAX_BODY", "20000"),
        agent_trash=os.environ.get("MAIL_AGENT_TRASH", "").strip() or "Agent Trash",
        project_dir=Path(os.environ.get("MAIL_PROJECT_DIR") or Path.cwd()),
        config_dir=_user_config_dir(),
        tls_insecure=_flag("MAIL_TLS_INSECURE"),
    )


settings = load_settings()


def require(value: str, variable: str) -> str:
    if not value:
        raise ConfigError(f"Environment variable {variable} is missing (e.g. imap.example.com).")
    return value


def get_credentials() -> tuple[str, str]:
    """Address and password: environment variables first, then the system keyring."""
    user = os.environ.get("MAIL_USER") or None
    password = os.environ.get("MAIL_PASSWORD") or None
    if not (user and password):
        user, password = _from_keyring(user, password)
    if not (user and password):
        raise ConfigError(
            "Credentials missing: set MAIL_USER and MAIL_PASSWORD, or run `imap-mcp setup`.")
    return user, password


def _from_keyring(user: str | None, password: str | None) -> tuple[str | None, str | None]:
    try:
        user = user or keyring.get_password(SERVICE, ACCOUNT_KEY)
        if user and not password:
            password = keyring.get_password(SERVICE, user)
    except KeyringError:
        pass
    return user, password
