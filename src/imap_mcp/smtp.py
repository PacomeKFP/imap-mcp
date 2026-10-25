"""SMTP delivery: STARTTLS (port 587, default) or implicit SSL (port 465)."""
from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage

from .config import TIMEOUT, get_credentials, require, settings


def send(message: EmailMessage, recipients: list[str]) -> None:
    user, password = get_credentials()
    host = require(settings.smtp_host, "MAIL_SMTP_HOST or MAIL_HOST")
    context = ssl.create_default_context()
    implicit_tls = settings.smtp_port == 465
    if implicit_tls:
        server = smtplib.SMTP_SSL(
            host, settings.smtp_port, context=context, timeout=TIMEOUT)
    else:
        server = smtplib.SMTP(host, settings.smtp_port, timeout=TIMEOUT)
    with server as smtp:
        if not implicit_tls:
            smtp.starttls(context=context)
        smtp.login(user, password)
        smtp.send_message(message, from_addr=user, to_addrs=recipients)
