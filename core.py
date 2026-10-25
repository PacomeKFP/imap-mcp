"""Briques techniques du connecteur mail : identifiants, IMAP/SMTP, MIME."""
from __future__ import annotations

import base64
import binascii
import email
import imaplib
import mimetypes
import os
import re
import smtplib
import ssl
import time
from contextlib import contextmanager
from email import policy
from email.header import decode_header, make_header
from email.message import EmailMessage, Message
from email.utils import formatdate, getaddresses, make_msgid, parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path

SERVICE = "imap-mcp"
IMAP_HOST = os.environ.get("MAIL_HOST", "")
IMAP_PORT = int(os.environ.get("MAIL_PORT", "993"))
SMTP_HOST = os.environ.get("MAIL_SMTP_HOST", IMAP_HOST)
SMTP_PORT = int(os.environ.get("MAIL_SMTP_PORT", "587"))
READ_ONLY = os.environ.get("MAIL_READONLY", "").lower() in ("1", "true", "yes")
DOWNLOAD_DIR = Path(os.environ.get("MAIL_DOWNLOAD_DIR",
                    Path.home() / "Downloads" / "imap-mcp"))
MAX_BODY = int(os.environ.get("MAIL_MAX_BODY", "20000"))
TIMEOUT = 40


def _require_host(host: str) -> str:
    if not host:
        raise RuntimeError(
            "Serveur mail non defini : renseigner MAIL_HOST (ex. imap.example.com).")
    return host


# ---------------------------------------------------------------- identifiants
def credentials() -> tuple[str, str]:
    """Adresse + mot de passe : variables d'environnement, sinon trousseau systeme (keyring)."""
    user = os.environ.get("MAIL_USER")
    password = os.environ.get("MAIL_PASSWORD")
    if not (user and password):
        try:
            import keyring
        except ImportError:
            keyring = None
        if keyring is not None:
            user = user or keyring.get_password(SERVICE, "user")
            password = password or (keyring.get_password(
                SERVICE, user) if user else None)
    if not (user and password):
        raise RuntimeError(
            "Identifiants absents : definir MAIL_USER et MAIL_PASSWORD, ou lancer `python setup_credentials.py`.")
    return user, password


def ensure_writable() -> None:
    if READ_ONLY:
        raise PermissionError("Serveur en lecture seule (MAIL_READONLY=1).")


# ---------------------------------------------------------------- UTF-7 IMAP
def utf7_encode(s: str) -> str:
    out, buf = [], []

    def flush():
        if buf:
            b = "".join(buf).encode("utf-16-be")
            out.append(
                "&" + base64.b64encode(b).decode().rstrip("=").replace("/", ",") + "-")
            buf.clear()

    for ch in s:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buf.append(ch)
    flush()
    return "".join(out)


def utf7_decode(s: str) -> str:
    def repl(m: re.Match) -> str:
        if m.group(1) == "":
            return "&"
        b64 = m.group(1).replace(",", "/")
        b64 += "=" * (-len(b64) % 4)
        try:
            return base64.b64decode(b64).decode("utf-16-be")
        except (binascii.Error, UnicodeDecodeError):
            return m.group(0)

    return re.sub(r"&([^-]*)-", repl, s)


def quote(s: str) -> str:
    return '"' + utf7_encode(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def search_quote(s: str) -> str:
    """Les critères SEARCH non ASCII passent en littéral UTF-8 : on retire les accents faute de mieux."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


# ---------------------------------------------------------------- connexion
@contextmanager
def imap_session(folder: str | None = None, readonly: bool = True):
    user, password = credentials()
    imap = imaplib.IMAP4_SSL(_require_host(IMAP_HOST),
                             IMAP_PORT, timeout=TIMEOUT)
    try:
        imap.login(user, password)
        if folder is not None:
            status, _ = imap.select(quote(folder), readonly=readonly)
            if status != "OK":
                raise RuntimeError(
                    f"Dossier introuvable ou inaccessible : {folder}")
        yield imap
    finally:
        try:
            imap.logout()
        except (imaplib.IMAP4.error, OSError):
            pass


def list_folder_names(imap: imaplib.IMAP4_SSL) -> list[tuple[str, str]]:
    """[(nom decode, attributs)]"""
    _, rows = imap.list()
    res = []
    for r in rows:
        line = r.decode(errors="replace")
        m = re.match(
            r'\((?P<attrs>[^)]*)\)\s+(?:"[^"]*"|NIL)\s+(?P<name>.+)$', line)
        if not m:
            continue
        name = m.group("name").strip().strip('"')
        res.append((utf7_decode(name), m.group("attrs")))
    return res


def special_folder(imap: imaplib.IMAP4_SSL, flag: str, fallbacks: list[str]) -> str:
    folders = list_folder_names(imap)
    for name, attrs in folders:
        if flag.lower() in attrs.lower():
            return name
    names = {n.lower(): n for n, _ in folders}
    for fb in fallbacks:
        if fb.lower() in names:
            return names[fb.lower()]
    raise RuntimeError(f"Dossier special introuvable ({flag})")


def smtp_send(msg: EmailMessage, recipients: list[str]) -> None:
    user, password = credentials()
    ctx = ssl.create_default_context()
    implicit_tls = SMTP_PORT == 465
    if implicit_tls:
        server = smtplib.SMTP_SSL(_require_host(
            SMTP_HOST), SMTP_PORT, context=ctx, timeout=TIMEOUT)
    else:
        server = smtplib.SMTP(_require_host(SMTP_HOST),
                              SMTP_PORT, timeout=TIMEOUT)
    with server as s:
        if not implicit_tls:
            s.starttls(context=ctx)
        s.login(user, password)
        s.send_message(msg, from_addr=user, to_addrs=recipients)


def append_to(imap: imaplib.IMAP4_SSL, folder: str, msg: EmailMessage, flags: str = "") -> None:
    status, data = imap.append(quote(folder), f"({flags})" if flags else None,
                               imaplib.Time2Internaldate(time.time()), msg.as_bytes())
    if status != "OK":
        raise RuntimeError(f"Ajout dans {folder} refuse : {data}")


# ---------------------------------------------------------------- lecture MIME
def dec(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(str(value))))
    except Exception:
        return str(value)


def addresses(value: str | None) -> list[dict]:
    return [{"name": dec(n), "email": a} for n, a in getaddresses([dec(value)]) if a]


class _Strip(HTMLParser):
    BLOCK = {"p", "div", "br", "tr", "li", "h1",
             "h2", "h3", "h4", "table", "blockquote"}

    def __init__(self):
        super().__init__()
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self.skip += 1
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head"):
            self.skip = max(0, self.skip - 1)

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def html_to_text(html: str) -> str:
    p = _Strip()
    p.feed(html)
    text = re.sub(r"[ \t\r\f\v]+", " ", "".join(p.out))
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()


def parse_message(raw: bytes) -> EmailMessage:
    # type: ignore[return-value]
    return email.message_from_bytes(raw, policy=policy.default)


def summary(uid: str, msg: Message, flags: str = "", size: int | None = None) -> dict:
    try:
        date = parsedate_to_datetime(str(msg["Date"])).isoformat()
    except Exception:
        date = str(msg["Date"] or "")
    d = {
        "uid": uid,
        "date": date,
        "from": dec(msg["From"]),
        "to": dec(msg["To"]),
        "subject": dec(msg["Subject"]),
        "flags": flags,
        "unread": "\\Seen" not in flags,
        "flagged": "\\Flagged" in flags,
    }
    if size is not None:
        d["size"] = size
    return d


def _text_of(part: Message) -> str:
    try:
        return part.get_content()
    except (LookupError, ValueError):
        return (part.get_payload(decode=True) or b"").decode("utf-8", errors="replace")


def body_and_attachments(msg: EmailMessage) -> tuple[str, str, list[dict]]:
    """(texte, html, pieces jointes) ; texte reconstruit depuis le html si besoin."""
    text = html = ""
    atts: list[dict] = []
    for i, part in enumerate(msg.walk()):
        if part.is_multipart():
            continue
        fn = part.get_filename()
        ctype = part.get_content_type()
        disp = part.get_content_disposition()
        if fn or disp == "attachment":
            payload = part.get_payload(decode=True) or b""
            atts.append({"index": i, "filename": dec(
                fn) or f"piece-{i}", "type": ctype, "size": len(payload)})
        elif ctype == "text/plain" and not text:
            text = _text_of(part)
        elif ctype == "text/html" and not html:
            html = _text_of(part)
    if not text and html:
        text = html_to_text(html)
    return text, html, atts


def fetch_raw(imap: imaplib.IMAP4_SSL, uid: str) -> tuple[bytes, str]:
    _, parts = imap.uid("FETCH", uid, "(FLAGS BODY.PEEK[])")
    for p in parts:
        if isinstance(p, tuple):
            flags = re.search(rb"FLAGS \(([^)]*)\)", p[0])
            return p[1], flags.group(1).decode() if flags else ""
    raise RuntimeError(f"Message introuvable : uid {uid}")


def fetch_headers(imap: imaplib.IMAP4_SSL, uids: list[str]) -> list[dict]:
    if not uids:
        return []
    _, parts = imap.uid(
        "FETCH", ",".join(uids),
        "(UID FLAGS RFC822.SIZE BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE MESSAGE-ID)])",
    )
    found = {}
    for p in parts:
        if isinstance(p, tuple):
            head = p[0].decode(errors="replace")
            uid = re.search(r"UID (\d+)", head).group(1)
            flags = re.search(r"FLAGS \(([^)]*)\)", head)
            size = re.search(r"RFC822\.SIZE (\d+)", head)
            found[uid] = summary(uid, email.message_from_bytes(p[1], policy=policy.default),
                                 flags.group(1) if flags else "", int(size.group(1)) if size else None)
    return [found[u] for u in uids if u in found]


# ---------------------------------------------------------------- composition
def attach_files(msg: EmailMessage, paths: list[str] | None) -> None:
    for p in paths or []:
        path = Path(p).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"Piece jointe introuvable : {p}")
        ctype = mimetypes.guess_type(
            path.name)[0] or "application/octet-stream"
        main, sub = ctype.split("/", 1)
        msg.add_attachment(path.read_bytes(), maintype=main,
                           subtype=sub, filename=path.name)


def build_message(to: list[str], subject: str, body: str, cc: list[str] | None = None,
                  bcc: list[str] | None = None, html: str | None = None,
                  attachments: list[str] | None = None, in_reply_to: str | None = None,
                  references: str | None = None) -> EmailMessage:
    user, _ = credentials()
    msg = EmailMessage()
    msg["From"] = user
    if to:
        msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if bcc:
        msg["Bcc"] = ", ".join(bcc)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=user.split("@")
                                   [-1] if "@" in user else "localhost")
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    attach_files(msg, attachments)
    return msg


def all_recipients(msg: EmailMessage) -> list[str]:
    return [a for _, a in getaddresses([msg.get("To", ""), msg.get("Cc", ""), msg.get("Bcc", "")]) if a]


def quote_text(original: EmailMessage, text: str) -> str:
    who = dec(original["From"])
    when = dec(original["Date"])
    return f"\n\nLe {when}, {who} a écrit :\n" + "\n".join("> " + l for l in text.splitlines())


def uid_set(uids: list[str]) -> str:
    clean = [str(u).strip() for u in uids]
    if not clean or not all(re.fullmatch(r"\d+", u) for u in clean):
        raise ValueError("uids doit etre une liste d'entiers non vide")
    return ",".join(clean)


def safe_filename(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip() or "piece"


def reply_target(original: EmailMessage, reply_all: bool, me: str) -> tuple[list[str], list[str]]:
    sender = parseaddr(original.get("Reply-To") or original.get("From", ""))[1]
    to = [sender]
    cc: list[str] = []
    if reply_all:
        others = getaddresses([original.get("To", ""), original.get("Cc", "")])
        cc = [a for _, a in others if a and a.lower() not in (
            me.lower(), sender.lower())]
    return to, cc
