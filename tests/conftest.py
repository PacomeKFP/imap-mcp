"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from imap_mcp.config import Settings

SettingsFactory = Callable[..., Settings]


@pytest.fixture
def make_settings() -> SettingsFactory:
    """Return a factory building test `Settings` rooted in a temporary directory.

    Keyword arguments override any field, e.g. `make_settings(tmp_path, read_only=True)`.
    """

    def factory(root: Path, read_only: bool = False, **overrides: Any) -> Settings:
        fields: dict[str, Any] = {
            "imap_host": "imap.test",
            "imap_port": 993,
            "smtp_host": "smtp.test",
            "smtp_port": 587,
            "read_only": read_only,
            "download_dir": root / "downloads",
            "max_body": 1000,
            "agent_trash": "Agent Trash",
            "project_dir": root / "project",
            "config_dir": root / "config",
        }
        fields.update(overrides)
        return Settings(**fields)

    return factory
