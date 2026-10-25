"""IMAP security: certificate verification by default, and the MAIL_TLS_INSECURE opt-out."""

from __future__ import annotations

import dataclasses
import ssl
from typing import Any

import pytest

from imap_mcp import imap
from imap_mcp.config import Settings, load_settings
from imap_mcp.config import settings as base_settings
from imap_mcp.errors import MailError


class FakeConn:
    def __init__(self) -> None:
        self.capabilities = ("IMAP4rev1",)

    def login(self, user: str, password: str):
        return "OK", [b"logged in"]

    def capability(self):
        return "OK", [b"IMAP4rev1"]

    def logout(self):
        return "BYE", [b""]


@pytest.fixture
def captured(monkeypatch):
    """Records the arguments of every Connection that session() opens."""
    holder: dict[str, Any] = {}

    def fake_connection(host, port, **kwargs):
        holder["kwargs"] = kwargs
        return FakeConn()

    monkeypatch.setattr(imap, "Connection", fake_connection)
    monkeypatch.setattr(imap, "get_credentials", lambda: ("user@example.com", "secret"))
    monkeypatch.setattr(
        imap, "settings", dataclasses.replace(base_settings, imap_host="imap.example.com")
    )
    return holder


def test_session_verifies_certificates_by_default(captured):
    with imap.session():
        pass
    context = captured["kwargs"]["ssl_context"]
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_session_skips_verification_only_when_tls_insecure(captured, monkeypatch):
    monkeypatch.setattr(imap, "settings", dataclasses.replace(imap.settings, tls_insecure=True))
    with imap.session():
        pass
    context = captured["kwargs"]["ssl_context"]
    assert context.verify_mode == ssl.CERT_NONE
    assert context.check_hostname is False


class PostLoginCapConn(FakeConn):
    """Pre-auth capabilities omit MOVE/UIDPLUS, as many servers do."""

    def __init__(self, post_login: list[bytes] | None) -> None:
        super().__init__()
        self.capabilities = (b"IMAP4rev1", b"STARTTLS")
        self.post_login = post_login
        self.capability_calls = 0

    def capability(self):
        self.capability_calls += 1
        if self.post_login is None:
            raise imap.imaplib.IMAP4.error("CAPABILITY failed")
        return "OK", [b" ".join(self.post_login)]


def test_session_refreshes_capabilities_after_login(monkeypatch):
    conn = PostLoginCapConn([b"IMAP4rev1", b"MOVE", b"UIDPLUS"])
    monkeypatch.setattr(imap, "Connection", lambda *a, **k: conn)
    monkeypatch.setattr(imap, "get_credentials", lambda: ("user@example.com", "secret"))
    monkeypatch.setattr(
        imap, "settings", dataclasses.replace(base_settings, imap_host="imap.example.com")
    )
    with imap.session():
        assert conn.capability_calls == 1
        assert imap.has_capability(conn, "MOVE")
        assert imap.has_capability(conn, "uidplus")


def test_session_keeps_known_capabilities_when_refresh_fails(monkeypatch):
    conn = PostLoginCapConn(None)
    monkeypatch.setattr(imap, "Connection", lambda *a, **k: conn)
    monkeypatch.setattr(imap, "get_credentials", lambda: ("user@example.com", "secret"))
    monkeypatch.setattr(
        imap, "settings", dataclasses.replace(base_settings, imap_host="imap.example.com")
    )
    with imap.session():
        assert not imap.has_capability(conn, "MOVE")
        assert imap.has_capability(conn, "IMAP4rev1")


def test_tls_insecure_is_off_by_default(monkeypatch):
    monkeypatch.delenv("MAIL_TLS_INSECURE", raising=False)
    assert Settings.__dataclass_fields__["tls_insecure"].default is False
    assert load_settings().tls_insecure is False


def test_tls_insecure_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("MAIL_TLS_INSECURE", "1")
    assert load_settings().tls_insecure is True


class SearchConn:
    """Records UID commands and the literal that was pending when each one ran."""

    def __init__(self, search: list[bytes] | None = None) -> None:
        self.search_result = search or [b""]
        self.literal: bytes | None = None
        self.calls: list[tuple[str, tuple, bytes | None]] = []

    def uid(self, command: str, *args):
        self.calls.append((command, args, self.literal))
        self.literal = None
        return "OK", self.search_result

    def select(self, mailbox: str, readonly: bool = True):
        return "OK", [b"3"]


CONTROL_VALUES = ["x\r\nA1 LOGOUT", "x\nA1 NOOP", "x\r", "x\x00y", "x\x7fy", "x\x1by"]


@pytest.mark.parametrize("value", CONTROL_VALUES)
def test_quote_rejects_control_characters(value):
    with pytest.raises(MailError) as info:
        imap.quote(value)
    assert info.value.code == "invalid_argument"


@pytest.mark.parametrize("value", CONTROL_VALUES)
def test_search_literal_rejects_control_characters(value):
    with pytest.raises(MailError) as info:
        imap.search_literal(value)
    assert info.value.code == "invalid_argument"


@pytest.mark.parametrize("value", CONTROL_VALUES)
def test_search_uids_rejects_control_characters_and_sends_nothing(value):
    conn = SearchConn()
    with pytest.raises(MailError) as info:
        imap.search_uids(conn, ["SUBJECT", f'"{value}"'])
    assert info.value.code == "invalid_argument"
    assert conn.calls == []


def test_search_literal_keeps_quotes_and_backslashes_escaped():
    assert imap.search_literal('say "hi" \\ there') == '"say \\"hi\\" \\\\ there"'


def test_quote_keeps_plain_and_accented_folder_names():
    assert imap.quote('a"b') == '"a\\"b"'
    assert imap.quote("Été") == '"' + imap.utf7.encode("Été") + '"'


@pytest.mark.parametrize(
    "criteria",
    [
        ["FROM", '"a\\"b"'],
        ["SUBJECT", imap.search_literal('back\\slash "quoted"')],
        ["HEADER", "Message-ID", '"<id@example.com>"'],
        ["SINCE", "01-Jan-2024", "UNSEEN"],
    ],
)
def test_ascii_criteria_are_sent_unchanged(criteria):
    conn = SearchConn()
    imap.search_uids(conn, criteria)
    assert conn.calls == [("SEARCH", tuple(criteria), None)]


def test_accented_value_is_sent_as_a_utf8_literal():
    conn = SearchConn(search=[b"3 7"])
    assert imap.search_uids(conn, ["SUBJECT", '"Été"', "UNSEEN"]) == ["3", "7"]
    command, args, literal = conn.calls[0]
    assert command == "SEARCH"
    assert args == ("CHARSET", "UTF-8", "UNSEEN", "SUBJECT")
    assert literal == "Été".encode()


def test_accented_header_value_keeps_its_field_name_before_the_literal():
    conn = SearchConn()
    imap.search_uids(conn, ["HEADER", "Subject", '"café"', "UNSEEN"])
    _, args, literal = conn.calls[0]
    assert args[-2:] == ("HEADER", "Subject")
    assert literal == "café".encode()


def test_accented_value_with_escaped_quote_is_unescaped_in_the_literal():
    conn = SearchConn()
    imap.search_uids(conn, ["SUBJECT", '"é \\"x\\""'])
    assert conn.calls[0][2] == 'é "x"'.encode()


def test_two_non_ascii_values_are_refused():
    conn = SearchConn()
    with pytest.raises(MailError) as info:
        imap.search_uids(conn, ["SUBJECT", '"é"', "FROM", '"ü"'])
    assert info.value.code == "invalid_argument"
    assert conn.calls == []


def test_non_ascii_value_without_keyword_is_refused():
    with pytest.raises(MailError) as info:
        imap.search_uids(SearchConn(), ['"é"'])
    assert info.value.code == "invalid_argument"


@pytest.mark.parametrize(
    "criteria",
    [
        ["HEADER", "X Bad Field", '"v"'],
        ["SUBJECT", "unquoted value"],
        ["SUBJECT", "{5}"],
        ["SUBJECT", '"unterminated'],
    ],
)
def test_malformed_criteria_are_refused(criteria):
    conn = SearchConn()
    with pytest.raises(MailError) as info:
        imap.search_uids(conn, criteria)
    assert info.value.code == "invalid_argument"
    assert conn.calls == []


def test_no_line_break_reaches_the_server_for_legitimate_values():
    conn = SearchConn()
    criteria = ["FROM", imap.search_literal('a"b\\c'), "SUBJECT", imap.search_literal("Été")]
    imap.search_uids(conn, criteria)
    for _command, args, _ in conn.calls:
        for arg in args:
            assert "\r" not in arg and "\n" not in arg


def test_message_id_folding_is_unfolded_and_never_breaks_the_line():
    conn = SearchConn(search=[b"4 9"])
    uid = imap.uid_by_message_id(conn, "Drafts", "<abc@example.com>\r\n\t")
    assert uid == "9"
    _, args, _ = conn.calls[0]
    assert args == ("HEADER", "Message-ID", '"<abc@example.com>"')
