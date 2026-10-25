"""Session registration, activity-based ordering and pruning."""

from __future__ import annotations

import argparse
import asyncio
import os
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from imap_mcp import rights, rights_cli
from imap_mcp.config import Settings
from imap_mcp.errors import MailError
from imap_mcp.permissions import Guard

SettingsFactory = Callable[..., Settings]
DAY = 86_400


def set_age(path: Path, seconds_ago: float) -> None:
    """Set the modification time of a file or folder to `seconds_ago` in the past."""
    stamp = time.time() - seconds_ago
    os.utime(path, (stamp, stamp))


def make_session(config: Settings, name: str, age: float) -> Path:
    """A session whose active marker was last touched `age` seconds ago."""
    folder = rights.session_dir(config, name)
    folder.mkdir(parents=True)
    marker = folder / rights.ACTIVE_FILE
    marker.write_text("", encoding="utf-8")
    set_age(marker, age)
    return folder


def run_prune(config: Settings, days: int) -> int:
    """Run `rights prune` with the given retention and return its exit code."""
    args = argparse.Namespace(days=days, func=rights_cli._prune)
    return rights_cli.run(args, config)


def remaining(config: Settings) -> set[str]:
    """Names of the session folders still on disk."""
    return {entry.name for entry in rights.sessions_dir(config).iterdir() if entry.is_dir()}


def test_start_creates_the_session_folder_and_marker(
    tmp_path: Path, make_settings: SettingsFactory
) -> None:
    config = make_settings(tmp_path)
    Guard(config, "s1").start()
    assert (rights.session_dir(config, "s1") / rights.ACTIVE_FILE).is_file()


def test_start_is_idempotent(tmp_path: Path, make_settings: SettingsFactory) -> None:
    config = make_settings(tmp_path)
    guard = Guard(config, "s1")
    guard.start()
    guard.start()
    assert rights.session_ids(config) == ["s1"]


def test_first_authorize_registers_the_session(
    tmp_path: Path, make_settings: SettingsFactory
) -> None:
    config = make_settings(tmp_path)
    asyncio.run(Guard(config, "s1").authorize(None, rights.Action.MAIL_READ))  # type: ignore[arg-type]
    assert (rights.session_dir(config, "s1") / rights.ACTIVE_FILE).is_file()


def test_every_authorize_refreshes_the_marker(
    tmp_path: Path, make_settings: SettingsFactory
) -> None:
    config = make_settings(tmp_path)
    guard = Guard(config, "s1")
    guard.start()
    marker = rights.session_dir(config, "s1") / rights.ACTIVE_FILE
    set_age(marker, 3 * DAY)
    asyncio.run(guard.authorize(None, rights.Action.MAIL_READ))  # type: ignore[arg-type]
    assert time.time() - marker.stat().st_mtime < 60


def test_read_only_refusal_still_marks_the_session_active(
    tmp_path: Path, make_settings: SettingsFactory
) -> None:
    config = make_settings(tmp_path, read_only=True)
    with pytest.raises(MailError):
        asyncio.run(
            Guard(config, "s1").authorize(
                None,  # type: ignore[arg-type]
                rights.Action.MAIL_MOVE,
                targets=["INBOX"],
            )
        )
    assert (rights.session_dir(config, "s1") / rights.ACTIVE_FILE).is_file()


def test_marker_failure_is_reported_and_does_not_block(
    tmp_path: Path,
    make_settings: SettingsFactory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = make_settings(tmp_path)

    def broken(*_args: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(rights, "touch_session", broken)
    asyncio.run(Guard(config, "s1").authorize(None, rights.Action.MAIL_READ))  # type: ignore[arg-type]
    assert "cannot mark session s1 active" in capsys.readouterr().err


def test_session_activity_is_the_latest_marker_journal_or_rights_mtime(
    tmp_path: Path, make_settings: SettingsFactory
) -> None:
    config = make_settings(tmp_path)
    folder = make_session(config, "s1", age=100 * DAY)
    journal = folder / rights.JOURNAL_FILE
    journal.write_text("{}\n", encoding="utf-8")
    set_age(journal, 10)
    rules = folder / rights.RIGHTS_FILE
    rules.write_text("", encoding="utf-8")
    set_age(rules, 1000 * DAY)
    assert rights.session_activity(folder) == journal.stat().st_mtime


def test_session_activity_falls_back_to_folder_mtime_without_files(
    tmp_path: Path, make_settings: SettingsFactory
) -> None:
    config = make_settings(tmp_path)
    folder = rights.session_dir(config, "bare")
    folder.mkdir(parents=True)
    set_age(folder, 5 * DAY)
    assert rights.session_activity(folder) == folder.stat().st_mtime


def test_latest_session_follows_activity_not_the_id(
    tmp_path: Path, make_settings: SettingsFactory
) -> None:
    config = make_settings(tmp_path)
    make_session(config, "20260101-000000-zzzz", age=1000)
    make_session(config, "20250101-000000-aaaa", age=10)
    assert rights.latest_session(config) == "20250101-000000-aaaa"


def test_latest_session_breaks_ties_by_id(tmp_path: Path, make_settings: SettingsFactory) -> None:
    config = make_settings(tmp_path)
    stamp = time.time() - 50
    for name in ("20250101-000000-aaaa", "20250102-000000-bbbb"):
        folder = make_session(config, name, age=0)
        os.utime(folder / rights.ACTIVE_FILE, (stamp, stamp))
    assert rights.latest_session(config) == "20250102-000000-bbbb"


def test_latest_session_is_none_without_sessions(
    tmp_path: Path, make_settings: SettingsFactory
) -> None:
    config = make_settings(tmp_path)
    assert rights.latest_session(config) is None


def test_cli_latest_resolves_the_most_active_session(
    tmp_path: Path, make_settings: SettingsFactory
) -> None:
    config = make_settings(tmp_path)
    make_session(config, "20260101-000000-zzzz", age=1000)
    make_session(config, "20250101-000000-aaaa", age=10)
    assert rights_cli._session_id(config, rights_cli.LATEST) == "20250101-000000-aaaa"


def test_prune_removes_only_sessions_idle_beyond_the_retention(
    tmp_path: Path, make_settings: SettingsFactory
) -> None:
    config = make_settings(tmp_path)
    make_session(config, "newest", age=60)
    make_session(config, "idle-40d", age=40 * DAY)
    make_session(config, "idle-10d", age=10 * DAY)
    make_session(config, "idle-60d", age=60 * DAY)
    assert run_prune(config, days=30) == 0
    assert remaining(config) == {"newest", "idle-10d"}


def test_prune_never_removes_the_newest_session(
    tmp_path: Path, make_settings: SettingsFactory
) -> None:
    config = make_settings(tmp_path)
    make_session(config, "only", age=400 * DAY)
    make_session(config, "older", age=500 * DAY)
    assert run_prune(config, days=0) == 0
    assert remaining(config) == {"only"}


def test_prune_keeps_sessions_touched_in_the_last_24_hours(
    tmp_path: Path, make_settings: SettingsFactory
) -> None:
    config = make_settings(tmp_path)
    make_session(config, "newest", age=60)
    make_session(config, "recent", age=2 * 3600)
    make_session(config, "older", age=3 * DAY)
    assert run_prune(config, days=0) == 0
    assert remaining(config) == {"newest", "recent"}


def test_prune_uses_the_journal_activity_not_only_the_marker(
    tmp_path: Path, make_settings: SettingsFactory
) -> None:
    config = make_settings(tmp_path)
    make_session(config, "newest", age=60)
    folder = make_session(config, "journal-recent", age=60 * DAY)
    journal = folder / rights.JOURNAL_FILE
    journal.write_text("{}\n", encoding="utf-8")
    set_age(journal, 3600)
    assert run_prune(config, days=30) == 0
    assert remaining(config) == {"newest", "journal-recent"}


def test_prune_ignores_the_folder_mtime(tmp_path: Path, make_settings: SettingsFactory) -> None:
    config = make_settings(tmp_path)
    make_session(config, "newest", age=60)
    folder = make_session(config, "stale-marker", age=60 * DAY)
    scratch = folder / "scratch.tmp"
    scratch.write_text("", encoding="utf-8")
    scratch.unlink()  # bumps the folder mtime to now without touching any activity file
    assert run_prune(config, days=30) == 0
    assert remaining(config) == {"newest"}


def test_prune_rejects_negative_days(
    tmp_path: Path, make_settings: SettingsFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    config = make_settings(tmp_path)
    make_session(config, "newest", age=60)
    assert run_prune(config, days=-1) == 1
    assert "--days must be 0 or more" in capsys.readouterr().err
    assert remaining(config) == {"newest"}


def test_prune_without_sessions_removes_nothing(
    tmp_path: Path, make_settings: SettingsFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    config = make_settings(tmp_path)
    assert run_prune(config, days=30) == 0
    assert "0 session folder(s) removed." in capsys.readouterr().out
