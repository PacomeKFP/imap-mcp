"""Send tools: a failure after SMTP delivery must become a warning, never an exception."""

from __future__ import annotations

import asyncio
import imaplib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from email.message import EmailMessage
from typing import Any

import pytest

from imap_mcp import imap, mime, smtp
from imap_mcp.errors import MailError
from imap_mcp.permissions import guard
from imap_mcp.tools import composing, replying

ME = "me@example.org"
ALICE = "alice@example.org"
DRAFT_UID = "7"
SENT_FOLDER = "Sent"
DRAFTS_FOLDER = "Drafts"


def _raw_mail(subject: str, to: str, sender: str, message_id: str) -> bytes:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    msg["Date"] = "Mon, 05 Jan 2026 10:00:00 +0000"
    msg.set_content("Hello there.")
    return msg.as_bytes()


class FakeMail:
    """Records SMTP and IMAP calls; `fail` maps a step name to the exception it raises."""

    def __init__(self) -> None:
        self.fail: dict[str, BaseException] = {}
        self.smtp_calls: list[list[str]] = []
        self.appended: list[str] = []
        self.moved: list[str] = []
        self.stored: list[str] = []
        self.raw = _raw_mail("Hi", ME, ALICE, "<orig@example.org>")

    def maybe_fail(self, step: str) -> None:
        if step in self.fail:
            raise self.fail[step]


@pytest.fixture
def mail(monkeypatch: pytest.MonkeyPatch) -> FakeMail:
    fake = FakeMail()

    @contextmanager
    def session(folder: str | None = None, readonly: bool = True) -> Iterator[None]:
        yield None

    def special_folder(conn: object, role: str) -> str:
        return {"sent": SENT_FOLDER, "drafts": DRAFTS_FOLDER}[role]

    def send(message: EmailMessage, recipients: list[str]) -> None:
        fake.maybe_fail("smtp")
        fake.smtp_calls.append(list(recipients))

    def append(conn: object, folder: str, msg: EmailMessage, flags: str) -> None:
        fake.maybe_fail("append")
        fake.appended.append(folder)

    def move(conn: object, uids: str, dest: str) -> None:
        fake.maybe_fail("move")
        fake.moved.append(dest)

    def store(conn: object, uids: str, flags: str, value: bool) -> None:
        fake.maybe_fail("store")
        fake.stored.append(flags)

    def fetch_raw(conn: object, uid: str) -> tuple[bytes, set[str]]:
        return fake.raw, set()

    async def allow(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(imap, "session", session)
    monkeypatch.setattr(imap, "special_folder", special_folder)
    monkeypatch.setattr(imap, "ensure_folder", lambda conn, name: None)
    monkeypatch.setattr(imap, "append", append)
    monkeypatch.setattr(imap, "move", move)
    monkeypatch.setattr(imap, "store", store)
    monkeypatch.setattr(imap, "fetch_raw", fetch_raw)
    monkeypatch.setattr(smtp, "send", send)
    monkeypatch.setattr(guard, "authorize", allow)
    monkeypatch.setattr(composing, "sender", lambda: ME)
    monkeypatch.setattr(replying, "sender", lambda: ME)
    return fake


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


CTX: Any = None
SEND_ARGS: dict[str, Any] = {"to": [ALICE], "subject": "Hi", "body": "Hello"}


def _send_mail(**overrides: Any) -> dict[str, Any]:
    return _run(composing.send_mail(CTX, **{**SEND_ARGS, **overrides}))


def _send_draft(**overrides: Any) -> dict[str, Any]:
    return _run(composing.send_draft(CTX, DRAFT_UID, **overrides))


def _reply_mail(**overrides: Any) -> dict[str, Any]:
    return _run(replying.reply_mail(CTX, DRAFT_UID, "Thanks", send=True, **overrides))


def _forward_mail(**overrides: Any) -> dict[str, Any]:
    return _run(replying.forward_mail(CTX, DRAFT_UID, [ALICE], send=True, **overrides))


IMAP_FAILURES: list[Callable[[], BaseException]] = [
    lambda: MailError("IMAP is down", code="imap_error"),
    lambda: imaplib.IMAP4.error("connection lost"),
    lambda: OSError("socket closed"),
]


# send_mail


def test_send_mail_success_has_no_warnings(mail: FakeMail) -> None:
    result = _send_mail()
    assert result["sent"] is True
    assert result["recipients"] == [ALICE]
    assert "warnings" not in result
    assert mail.appended == [SENT_FOLDER]


def test_send_mail_without_copy_skips_sent(mail: FakeMail) -> None:
    result = _send_mail(save_copy=False)
    assert "warnings" not in result
    assert mail.appended == []


@pytest.mark.parametrize("make_error", IMAP_FAILURES)
def test_send_mail_copy_failure_returns_warning(
    mail: FakeMail, make_error: Callable[[], BaseException]
) -> None:
    mail.fail["append"] = make_error()
    result = _send_mail()
    assert result["sent"] is True
    assert len(mail.smtp_calls) == 1
    assert len(result["warnings"]) == 1
    assert all(isinstance(item, str) for item in result["warnings"])
    assert "Do not send it again" in result["warnings"][0]


def test_send_mail_smtp_failure_propagates(mail: FakeMail) -> None:
    mail.fail["smtp"] = MailError("SMTP refused", code="smtp_error")
    with pytest.raises(MailError):
        _send_mail()
    assert mail.appended == []


def test_send_mail_rejects_missing_recipients(mail: FakeMail) -> None:
    with pytest.raises(MailError) as info:
        _send_mail(to=[])
    assert info.value.code == "no_recipients"
    assert mail.smtp_calls == []


# send_draft


def test_send_draft_success_trashes_and_copies(mail: FakeMail) -> None:
    result = _send_draft()
    assert result["sent"] is True
    assert "warnings" not in result
    assert mail.moved == [composing.settings.agent_trash]
    assert mail.appended == [SENT_FOLDER]


@pytest.mark.parametrize("make_error", IMAP_FAILURES)
def test_send_draft_trash_failure_still_copies_to_sent(
    mail: FakeMail, make_error: Callable[[], BaseException]
) -> None:
    mail.fail["move"] = make_error()
    result = _send_draft()
    assert result["sent"] is True
    assert len(result["warnings"]) == 1
    assert "Agent Trash" in result["warnings"][0]
    assert mail.appended == [SENT_FOLDER]


def test_send_draft_copy_failure_returns_warning(mail: FakeMail) -> None:
    mail.fail["append"] = imaplib.IMAP4.error("append refused")
    result = _send_draft()
    assert result["sent"] is True
    assert mail.moved == [composing.settings.agent_trash]
    assert len(result["warnings"]) == 1


def test_send_draft_smtp_failure_propagates_and_moves_nothing(mail: FakeMail) -> None:
    mail.fail["smtp"] = MailError("SMTP refused", code="smtp_error")
    with pytest.raises(MailError):
        _send_draft()
    assert mail.moved == []
    assert mail.appended == []


# reply_mail


def test_reply_mail_send_success_marks_answered(mail: FakeMail) -> None:
    result = _reply_mail()
    assert result["sent"] is True
    assert "warnings" not in result
    assert mail.stored == [r"\Answered \Seen"]
    assert mail.appended == [SENT_FOLDER]


@pytest.mark.parametrize("make_error", IMAP_FAILURES)
def test_reply_mail_mark_answered_failure_returns_warning(
    mail: FakeMail, make_error: Callable[[], BaseException]
) -> None:
    mail.fail["store"] = make_error()
    result = _reply_mail()
    assert result["sent"] is True
    assert len(result["warnings"]) == 1
    assert "answered" in result["warnings"][0]


def test_reply_mail_copy_failure_still_marks_answered(mail: FakeMail) -> None:
    mail.fail["append"] = OSError("disk full")
    result = _reply_mail()
    assert len(result["warnings"]) == 1
    assert mail.stored == [r"\Answered \Seen"]


def test_reply_mail_smtp_failure_propagates(mail: FakeMail) -> None:
    mail.fail["smtp"] = MailError("SMTP refused", code="smtp_error")
    with pytest.raises(MailError):
        _reply_mail()
    assert mail.stored == []
    assert mail.appended == []


# forward_mail


def test_forward_mail_send_success_has_no_warnings(mail: FakeMail) -> None:
    result = _forward_mail()
    assert result["sent"] is True
    assert "warnings" not in result
    assert mail.appended == [SENT_FOLDER]


@pytest.mark.parametrize("make_error", IMAP_FAILURES)
def test_forward_mail_copy_failure_returns_warning(
    mail: FakeMail, make_error: Callable[[], BaseException]
) -> None:
    mail.fail["append"] = make_error()
    result = _forward_mail()
    assert result["sent"] is True
    assert len(mail.smtp_calls) == 1
    assert len(result["warnings"]) == 1


def test_forward_mail_smtp_failure_propagates(mail: FakeMail) -> None:
    mail.fail["smtp"] = MailError("SMTP refused", code="smtp_error")
    with pytest.raises(MailError):
        _forward_mail()
    assert mail.appended == []


def test_forward_header_uses_mail_locale_templates() -> None:
    header = replying._forward_header(mime.parse(_raw_mail("Hi", ME, ALICE, "<o@x>")))
    assert header == (
        "\n\n---------- Message transféré ----------\n"
        f"De : {ALICE}\n"
        "Date : Mon, 05 Jan 2026 10:00:00 +0000\n"
        "Objet : Hi\n"
        f"À : {ME}\n\n"
    )


@pytest.mark.parametrize(
    ("subject", "expected"),
    [("Agenda", "Fwd: Agenda"), ("Fw: Agenda", "Fw: Agenda"), ("TR : Agenda", "TR : Agenda")],
)
def test_forward_subject_keeps_existing_prefix(mail: FakeMail, subject: str, expected: str) -> None:
    mail.raw = _raw_mail(subject, ME, ALICE, "<o@x>")
    assert replying._build_forward(DRAFT_UID, "INBOX", [ALICE], "", None, True)[1] == expected
