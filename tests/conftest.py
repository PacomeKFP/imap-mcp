"""Shared pytest fixtures."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from imap_mcp.config import Settings, get_credentials, get_settings

SettingsFactory = Callable[..., Settings]


@pytest.fixture(autouse=True)
def isolated_mail_config(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Remove MAIL_* variables and reset the cached credentials and settings around every test."""
    for name in [key for key in os.environ if key.startswith("MAIL_")]:
        monkeypatch.delenv(name, raising=False)
    get_credentials.cache_clear()
    get_settings.cache_clear()
    yield
    get_credentials.cache_clear()
    get_settings.cache_clear()


@pytest.fixture
def make_settings() -> SettingsFactory:
    """Return a factory building test `Settings` rooted in a temporary directory.

    Keyword arguments override any field, e.g. `make_settings(tmp_path, agent_trash="Trash")`.
    """

    def factory(root: Path, **overrides: Any) -> Settings:
        fields: dict[str, Any] = {
            "imap_host": "imap.test",
            "imap_port": 993,
            "smtp_host": "smtp.test",
            "smtp_port": 587,
            "download_dir": root / "downloads",
            "max_body": 1000,
            "agent_trash": "Agent Trash",
            "project_dir": root / "project",
            "config_dir": root / "config",
        }
        fields.update(overrides)
        return Settings(**fields)

    return factory
