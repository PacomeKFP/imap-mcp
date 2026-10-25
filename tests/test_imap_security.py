"""IMAP security: certificate verification by default, and the MAIL_TLS_INSECURE opt-out."""
from __future__ import annotations

import dataclasses
import ssl
from typing import Any

import pytest

from imap_mcp import imap
from imap_mcp.config import Settings, load_settings, settings as base_settings


class FakeConn:
    def __init__(self) -> None:
        self.capabilities = ("IMAP4rev1",)

    def login(self, user: str, password: str):
        return "OK", [b"logged in"]

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
    monkeypatch.setattr(imap, "settings",
                        dataclasses.replace(base_settings, imap_host="imap.example.com"))
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


def test_tls_insecure_is_off_by_default(monkeypatch):
    monkeypatch.delenv("MAIL_TLS_INSECURE", raising=False)
    assert Settings.__dataclass_fields__["tls_insecure"].default is False
    assert load_settings().tls_insecure is False


def test_tls_insecure_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("MAIL_TLS_INSECURE", "1")
    assert load_settings().tls_insecure is True
