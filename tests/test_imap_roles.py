"""Folder role and separator caches, exercised against a fake LIST server (no network)."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from imap_mcp import imap
from imap_mcp.config import Settings
from imap_mcp.errors import ConfigError, MailError

ROWS: list[Any] = [
    b'(\\HasNoChildren \\Sent) "/" "Sent Items"',
    b'(\\HasNoChildren) "/" "Trash"',
    b'(\\HasNoChildren) "/" "Drafts"',
    b'(\\HasNoChildren) "/" "INBOX"',
]


class FakeListConn:
    """Answers LIST, CREATE and SUBSCRIBE and counts every LIST request."""

    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows
        self.list_calls = 0
        self.creates: list[str] = []
        self.capabilities: tuple[str, ...] = ()

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        return ("OK", [b"logged in"])

    def logout(self) -> tuple[str, list[bytes]]:
        return ("BYE", [b"bye"])

    def capability(self) -> tuple[str, list[bytes]]:
        return ("OK", [b"IMAP4REV1"])

    def list(self, *args: str) -> tuple[str, list[Any]]:
        self.list_calls += 1
        return ("OK", list(self.rows))

    def create(self, mailbox: str) -> tuple[str, list[bytes]]:
        self.creates.append(mailbox)
        return ("OK", [b"created"])

    def subscribe(self, mailbox: str) -> tuple[str, list[bytes]]:
        return ("OK", [b"subscribed"])


class FakeClock:
    """Injected clock; tests move time forward by assigning `now`."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def fresh_caches() -> Iterator[None]:
    imap.clear_caches()
    yield
    imap.clear_caches()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake_clock = FakeClock()
    monkeypatch.setattr(imap, "_clock", fake_clock)
    return fake_clock


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clock: FakeClock) -> Any:
    """Route imap.session() to one FakeListConn with fixed settings and credentials."""
    conn = FakeListConn(list(ROWS))
    settings = Settings(
        imap_host="imap.example.org",
        imap_port=993,
        smtp_host="imap.example.org",
        smtp_port=587,
        read_only=False,
        download_dir=tmp_path,
        max_body=20000,
        agent_trash="Agent Trash",
        project_dir=tmp_path,
        config_dir=tmp_path,
    )
    monkeypatch.setattr(imap, "settings", settings)
    monkeypatch.setattr(imap, "Connection", lambda *args, **kwargs: conn)
    monkeypatch.setattr(imap, "get_credentials", lambda: ("user@example.org", "pw-for-test"))
    return conn


def test_second_role_lookup_uses_cache_without_list(server: FakeListConn) -> None:
    assert imap.cached_special_folder("sent") == "Sent Items"
    assert imap.cached_special_folder("sent") == "Sent Items"
    assert server.list_calls == 1


def test_one_list_fills_every_role(server: FakeListConn) -> None:
    imap.cached_special_folder("sent")
    assert imap.cached_special_folder("trash") == "Trash"
    assert imap.cached_special_folder("drafts") == "Drafts"
    assert server.list_calls == 1


def test_absent_role_raises_from_cache_without_list(server: FakeListConn) -> None:
    imap.cached_special_folder("sent")
    with pytest.raises(MailError) as excinfo:
        imap.cached_special_folder("archive")
    assert excinfo.value.code == "special_folder_missing"
    assert server.list_calls == 1


def test_unknown_role_is_rejected(server: FakeListConn) -> None:
    with pytest.raises(MailError) as excinfo:
        imap.cached_special_folder("inbox")
    assert excinfo.value.code == "invalid_argument"


def test_cache_expires_after_ttl(server: FakeListConn, clock: FakeClock) -> None:
    imap.cached_special_folder("sent")
    clock.now += imap.CACHE_TTL_SECONDS - 1
    imap.cached_special_folder("sent")
    assert server.list_calls == 1

    clock.now += 1
    imap.cached_special_folder("sent")
    assert server.list_calls == 2


def test_clear_caches_forces_a_new_list(server: FakeListConn) -> None:
    imap.cached_special_folder("sent")
    imap.clear_caches()
    imap.cached_special_folder("sent")
    assert server.list_calls == 2


def test_special_folder_with_connection_fills_cache(server: FakeListConn) -> None:
    assert imap.special_folder(server, "trash") == "Trash"
    assert imap.cached_special_folder("sent") == "Sent Items"
    assert server.list_calls == 1


def test_ensure_folder_invalidates_role_cache(server: FakeListConn) -> None:
    imap.cached_special_folder("sent")
    imap.ensure_folder(server, "Archive")
    assert server.creates == ['"Archive"']
    imap.cached_special_folder("sent")
    assert server.list_calls == 3  # cached LIST, ensure_folder LIST, refreshed LIST


def test_ensure_folder_without_create_keeps_cache(server: FakeListConn) -> None:
    imap.cached_special_folder("sent")
    imap.ensure_folder(server, "INBOX")
    assert server.creates == []
    imap.cached_special_folder("sent")
    assert server.list_calls == 2  # the cached LIST and the existence check only


def test_cache_is_keyed_by_host_and_user(
    server: FakeListConn, monkeypatch: pytest.MonkeyPatch
) -> None:
    imap.cached_special_folder("sent")
    monkeypatch.setattr(imap, "get_credentials", lambda: ("other@example.org", "pw"))
    imap.cached_special_folder("sent")
    assert server.list_calls == 2

    monkeypatch.setattr(imap, "settings", replace(imap.settings, imap_host="imap.other.example"))
    imap.cached_special_folder("sent")
    assert server.list_calls == 3


def test_missing_credentials_scope_cache_to_host(
    server: FakeListConn, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_credentials() -> tuple[str, str]:
        raise ConfigError("Credentials missing.")

    monkeypatch.setattr(imap, "get_credentials", no_credentials)
    assert imap._cache_key() == ("imap.example.org", "", imap.Connection)


def test_folder_separator_is_cached(server: FakeListConn) -> None:
    server.rows = [b'(\\Noselect) "." ""']
    assert imap.folder_separator() == "."
    assert imap.folder_separator() == "."
    assert server.list_calls == 1


def test_folder_separator_cache_expires(server: FakeListConn, clock: FakeClock) -> None:
    server.rows = [b'(\\Noselect) "." ""']
    imap.folder_separator()
    clock.now += imap.CACHE_TTL_SECONDS
    imap.folder_separator()
    assert server.list_calls == 2


def test_clear_caches_also_drops_separator(server: FakeListConn) -> None:
    server.rows = [b'(\\Noselect) "." ""']
    imap.folder_separator()
    imap.clear_caches()
    imap.folder_separator()
    assert server.list_calls == 2


def test_folder_separator_literal_row_with_escaped_quote(server: FakeListConn) -> None:
    server.rows = [(b'(\\Noselect) "\\\\" {0}', b"")]
    assert imap.folder_separator() == "\\"


def test_folder_separator_literal_row_with_dot(server: FakeListConn) -> None:
    server.rows = [(b'(\\Noselect) "." {5}', b"Trash")]
    assert imap.folder_separator() == "."
