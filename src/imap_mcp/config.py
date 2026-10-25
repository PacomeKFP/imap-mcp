"""Configuration read from environment variables, and credentials read from the system keyring."""

from __future__ import annotations

import functools
import os
from dataclasses import Field, dataclass
from pathlib import Path
from typing import Any, ClassVar, cast

import keyring
from keyring.errors import KeyringError

from .errors import ConfigError

SERVICE = "imap-mcp"
TIMEOUT = 40
AGENT_DIR = ".imap-mcp"
ACCOUNT_KEY = "user"  # keyring entry that holds the account address
HOST_SUFFIX = ":imap-host"  # keyring entry, suffixed by the address, that holds the IMAP server
SMTP_SUFFIX = ":smtp-host"  # keyring entry, suffixed by the address, that holds the SMTP server
SETUP_HINT = "run `imap-mcp setup` to store them in the system keyring"


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: str, *, maximum: int | None = None) -> int:
    """Positive integer from the environment, optionally capped at `maximum`."""
    raw = os.environ.get(name, "").strip() or default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}.") from None
    if value < 1 or (maximum is not None and value > maximum):
        bounds = "a positive integer" if maximum is None else f"between 1 and {maximum}"
        raise ConfigError(f"{name} must be {bounds}, got {value}.")
    return value


def _user_config_dir() -> Path:
    base = (
        os.environ.get("APPDATA")
        or os.environ.get("XDG_CONFIG_HOME")
        or str(Path.home() / ".config")
    )
    return Path(base) / SERVICE


@dataclass(frozen=True)
class Settings:
    imap_host: str
    imap_port: int
    smtp_host: str
    smtp_port: int
    download_dir: Path
    max_body: int
    agent_trash: str
    project_dir: Path
    config_dir: Path
    tls_insecure: bool = False  # disables certificate verification; only for trusted test servers
    timeout: int = TIMEOUT  # seconds, for IMAP and SMTP connections


def _stored_server(suffix: str) -> str:
    """Server name saved by `imap-mcp setup` for the account in the keyring, or empty."""
    try:
        user = keyring.get_password(SERVICE, ACCOUNT_KEY)
        return (keyring.get_password(SERVICE, f"{user}{suffix}") or "") if user else ""
    except KeyringError:
        return ""


def load_settings() -> Settings:
    imap_host = os.environ.get("MAIL_HOST", "").strip() or _stored_server(HOST_SUFFIX)
    return Settings(
        imap_host=imap_host,
        imap_port=_int("MAIL_PORT", "993", maximum=65535),
        smtp_host=(
            os.environ.get("MAIL_SMTP_HOST", "").strip() or _stored_server(SMTP_SUFFIX) or imap_host
        ),
        smtp_port=_int("MAIL_SMTP_PORT", "587", maximum=65535),
        download_dir=Path(
            os.environ.get("MAIL_DOWNLOAD_DIR") or Path.home() / "Downloads" / SERVICE
        ).expanduser(),
        max_body=_int("MAIL_MAX_BODY", "20000"),
        agent_trash=os.environ.get("MAIL_AGENT_TRASH", "").strip() or "Agent Trash",
        project_dir=Path(os.environ.get("MAIL_PROJECT_DIR") or Path.cwd()),
        config_dir=_user_config_dir(),
        tls_insecure=_flag("MAIL_TLS_INSECURE"),
        timeout=_int("MAIL_TIMEOUT", str(TIMEOUT)),
    )


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Settings read from the environment once per process.

    Call `get_settings.cache_clear()` to read the environment again.
    """
    return load_settings()


class _LazySettings:
    """Forwards attribute access to `get_settings()`, so importing this module reads no environment.

    `dataclasses.replace(settings, ...)` works too: `__class__` reports `Settings`.
    """

    __dataclass_fields__: ClassVar[dict[str, Field[Any]]] = Settings.__dataclass_fields__

    @property  # type: ignore[misc]
    def __class__(self) -> type[Settings]:  # type: ignore[override]
        return Settings

    def __getattr__(self, name: str) -> Any:
        return getattr(get_settings(), name)

    def __repr__(self) -> str:
        return repr(get_settings())


settings: Settings = cast(Settings, _LazySettings())


def require(value: str, variable: str) -> str:
    if not value:
        raise ConfigError(f"Environment variable {variable} is missing (e.g. imap.example.com).")
    return value


@functools.lru_cache(maxsize=1)
def get_credentials() -> tuple[str, str]:
    """Address and password from the system keyring only, cached for the process.

    Call `get_credentials.cache_clear()` to read the keyring again.
    """
    try:
        user = keyring.get_password(SERVICE, ACCOUNT_KEY)
        password = keyring.get_password(SERVICE, user) if user else None
    except KeyringError as exc:
        raise ConfigError(
            f"Credentials unavailable from the system keyring ({exc}): {SETUP_HINT}."
        ) from exc
    if not (user and password):
        raise ConfigError(f"Credentials missing: {SETUP_HINT}.")
    return user, password
