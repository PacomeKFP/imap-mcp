"""Configuration lue dans les variables d'environnement, et identifiants (variables ou trousseau)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import keyring
from keyring.errors import KeyringError

from .errors import ConfigError, MailError

SERVICE = "imap-mcp"
TIMEOUT = 40


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    imap_host: str
    imap_port: int
    smtp_host: str
    smtp_port: int
    read_only: bool
    download_dir: Path
    max_body: int


def load_settings() -> Settings:
    imap_host = os.environ.get("MAIL_HOST", "").strip()
    return Settings(
        imap_host=imap_host,
        imap_port=int(os.environ.get("MAIL_PORT", "993")),
        smtp_host=os.environ.get("MAIL_SMTP_HOST", "").strip() or imap_host,
        smtp_port=int(os.environ.get("MAIL_SMTP_PORT", "587")),
        read_only=_flag("MAIL_READONLY"),
        download_dir=Path(os.environ.get("MAIL_DOWNLOAD_DIR")
                          or Path.home() / "Downloads" / SERVICE),
        max_body=int(os.environ.get("MAIL_MAX_BODY", "20000")),
    )


settings = load_settings()


def require(value: str, variable: str) -> str:
    if not value:
        raise ConfigError(
            f"Variable d'environnement {variable} manquante (ex. imap.example.com).")
    return value


def ensure_writable() -> None:
    if settings.read_only:
        raise MailError("Serveur en lecture seule (MAIL_READONLY=1).")


def get_credentials() -> tuple[str, str]:
    """Adresse et mot de passe : variables d'environnement, sinon trousseau système."""
    user = os.environ.get("MAIL_USER") or None
    password = os.environ.get("MAIL_PASSWORD") or None
    if not (user and password):
        user, password = _from_keyring(user, password)
    if not (user and password):
        raise ConfigError(
            "Identifiants absents : définir MAIL_USER et MAIL_PASSWORD, ou lancer `imap-mcp setup`.")
    return user, password


def _from_keyring(user: str | None, password: str | None) -> tuple[str | None, str | None]:
    try:
        user = user or keyring.get_password(SERVICE, "user")
        if user and not password:
            password = keyring.get_password(SERVICE, user)
    except KeyringError:
        pass
    return user, password
