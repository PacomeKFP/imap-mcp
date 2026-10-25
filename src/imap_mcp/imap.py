"""Connexion IMAP et opérations bas niveau : dossiers, recherche, messages, drapeaux."""
from __future__ import annotations

import imaplib
import re
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any

from . import mime, utf7
from .config import TIMEOUT, get_credentials, require, settings
from .errors import MailError

Connection = imaplib.IMAP4_SSL

# Rôle -> noms de repli, utilisés si le serveur n'expose pas l'attribut SPECIAL-USE.
SPECIAL_ROLES: dict[str, tuple[str, ...]] = {
    "sent": ("Sent", "Éléments envoyés"),
    "drafts": ("Drafts", "Brouillons"),
    "trash": ("Trash", "Corbeille"),
    "junk": ("Junk", "Spam"),
    "archive": ("Archive", "Archives"),
}
_LIST_LINE = re.compile(
    r'\((?P<attrs>[^)]*)\)\s+(?:"[^"]*"|NIL)\s+(?P<name>.+)$')
_SUMMARY_FIELDS = "(UID FLAGS RFC822.SIZE BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE MESSAGE-ID)])"


@dataclass(frozen=True)
class Folder:
    name: str
    attributes: frozenset[str]

    @property
    def role(self) -> str | None:
        return next((attr[1:] for attr in self.attributes if attr[1:] in SPECIAL_ROLES), None)


def quote(name: str) -> str:
    """Nom de dossier au format IMAP (UTF-7 modifié, entre guillemets)."""
    return '"' + utf7.encode(name).replace("\\", "\\\\").replace('"', '\\"') + '"'


def search_literal(value: str) -> str:
    """Chaîne entre guillemets pour SEARCH."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def uid_set(uids: Iterable[str]) -> str:
    """Valide une liste d'UID et la renvoie au format IMAP (1,2,3)."""
    clean = [str(uid).strip() for uid in uids]
    if not clean or not all(re.fullmatch(r"\d+", uid) for uid in clean):
        raise MailError("uids doit être une liste d'entiers non vide.")
    return ",".join(clean)


def expect_ok(status: str, data: list[Any], action: str) -> None:
    if status != "OK":
        detail = data[0].decode(errors="replace") if data and isinstance(
            data[0], bytes) else data
        raise MailError(f"{action} refusé : {detail}")


@contextmanager
def session(folder: str | None = None, readonly: bool = True) -> Iterator[Connection]:
    """Ouvre une connexion authentifiée et, si demandé, sélectionne un dossier."""
    user, password = get_credentials()
    conn = Connection(require(settings.imap_host, "MAIL_HOST"),
                      settings.imap_port, timeout=TIMEOUT)
    try:
        try:
            conn.login(user, password)
        except imaplib.IMAP4.error as exc:
            raise MailError(f"Authentification refusée : {exc}") from exc
        if folder is not None:
            select(conn, folder, readonly)
        yield conn
    finally:
        with suppress(imaplib.IMAP4.error, OSError):
            conn.logout()


def select(conn: Connection, folder: str, readonly: bool = True) -> int:
    """Sélectionne un dossier et renvoie son nombre de messages."""
    status, data = conn.select(quote(folder), readonly=readonly)
    if status != "OK":
        raise MailError(f"Dossier introuvable ou inaccessible : {folder}")
    return int(data[0])


def has_capability(conn: Connection, name: str) -> bool:
    caps = {(cap.decode() if isinstance(cap, bytes) else cap).upper()
            for cap in conn.capabilities}
    return name.upper() in caps


def list_folders(conn: Connection) -> list[Folder]:
    _, rows = conn.list()
    folders: list[Folder] = []
    for row in rows or []:
        if not isinstance(row, bytes):
            continue
        match = _LIST_LINE.match(row.decode(errors="replace"))
        if match:
            name = utf7.decode(match["name"].strip().strip('"'))
            attributes = frozenset(attr.lower()
                                   for attr in match["attrs"].split())
            folders.append(Folder(name, attributes))
    return folders


def special_folder(conn: Connection, role: str) -> str:
    """Nom du dossier spécial (sent, drafts, trash, junk, archive) : attribut IMAP puis noms de repli."""
    folders = list_folders(conn)
    for folder in folders:
        if folder.role == role:
            return folder.name
    by_lower = {folder.name.lower(): folder.name for folder in folders}
    for fallback in SPECIAL_ROLES[role]:
        if fallback.lower() in by_lower:
            return by_lower[fallback.lower()]
    raise MailError(f"Dossier spécial introuvable : {role}")


def folder_status(conn: Connection, folder: str) -> tuple[int, int] | None:
    """(messages, non lus) d'un dossier, ou None si indisponible."""
    status, data = conn.status(quote(folder), "(MESSAGES UNSEEN)")
    if status != "OK" or not data or not data[0]:
        return None
    match = re.search(r"MESSAGES (\d+) UNSEEN (\d+)",
                      data[0].decode(errors="replace"))
    return (int(match[1]), int(match[2])) if match else None


def search_uids(conn: Connection, criteria: list[str]) -> list[str]:
    """UID correspondant aux critères SEARCH ; les valeurs non ASCII sont envoyées en UTF-8."""
    utf8 = any(not part.isascii() for part in criteria)
    args: list[str | bytes] = [part.encode(
        "utf-8") if not part.isascii() else part for part in criteria]
    charset = ["CHARSET", "UTF-8"] if utf8 else []
    _, data = conn.uid("SEARCH", *charset, *(args or ["ALL"]))
    return data[0].decode().split() if data and data[0] else []


def fetch_raw(conn: Connection, uid: str) -> tuple[bytes, str]:
    """Message brut et drapeaux, sans marquer le message comme lu."""
    _, parts = conn.uid("FETCH", uid_set([uid]), "(FLAGS BODY.PEEK[])")
    for part in parts or []:
        if isinstance(part, tuple):
            flags = re.search(rb"FLAGS \(([^)]*)\)", part[0])
            return part[1], flags[1].decode() if flags else ""
    raise MailError(f"Message introuvable : uid {uid}")


def fetch_summaries(conn: Connection, uids: list[str]) -> list[dict[str, Any]]:
    """En-têtes résumés pour une liste d'UID, dans l'ordre demandé."""
    if not uids:
        return []
    _, parts = conn.uid("FETCH", ",".join(uids), _SUMMARY_FIELDS)
    found: dict[str, dict[str, Any]] = {}
    for part in parts or []:
        if not isinstance(part, tuple):
            continue
        head = part[0].decode(errors="replace")
        uid = re.search(r"UID (\d+)", head)
        if not uid:
            continue
        flags = re.search(r"FLAGS \(([^)]*)\)", head)
        size = re.search(r"RFC822\.SIZE (\d+)", head)
        found[uid[1]] = mime.summary(
            uid[1],
            mime.parse(part[1]),
            flags[1] if flags else "",
            int(size[1]) if size else None,
        )
    return [found[uid] for uid in uids if uid in found]


def store(conn: Connection, uids: str, flags: str, value: bool) -> None:
    """Ajoute (value=True) ou retire des drapeaux IMAP sur des UID déjà validés."""
    op = "+FLAGS.SILENT" if value else "-FLAGS.SILENT"
    status, data = conn.uid("STORE", uids, op, f"({flags})")
    expect_ok(status, data, "Modification des drapeaux")


def copy(conn: Connection, uids: str, destination: str) -> None:
    status, data = conn.uid("COPY", uids, quote(destination))
    expect_ok(status, data, "Copie")


def delete_uids(conn: Connection, uids: str) -> None:
    """Marque \\Deleted et purge ces seuls UID si UIDPLUS est disponible (sinon rien n'est purgé)."""
    status, data = conn.uid("STORE", uids, "+FLAGS.SILENT", r"(\Deleted)")
    expect_ok(status, data, "Suppression")
    if has_capability(conn, "UIDPLUS"):
        conn.uid("EXPUNGE", uids)


def move(conn: Connection, uids: str, destination: str) -> None:
    if has_capability(conn, "MOVE"):
        status, data = conn.uid("MOVE", uids, quote(destination))
        expect_ok(status, data, "Déplacement")
        return
    copy(conn, uids, destination)
    delete_uids(conn, uids)


def append(conn: Connection, folder: str, message: EmailMessage, flags: str = "") -> None:
    status, data = conn.append(
        quote(folder),
        f"({flags})" if flags else None,
        imaplib.Time2Internaldate(time.time()),
        message.as_bytes(),
    )
    expect_ok(status, data, f"Ajout dans {folder}")


def uid_by_message_id(conn: Connection, folder: str, message_id: str) -> str | None:
    """UID du message portant ce Message-ID dans le dossier, ou None."""
    select(conn, folder, readonly=True)
    uids = search_uids(conn, ["HEADER", "Message-ID",
                       search_literal(message_id)])
    return uids[-1] if uids else None
