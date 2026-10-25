"""Outils MCP exposés aux clients : comptes, dossiers, lecture, recherche, brouillons, envoi."""
from __future__ import annotations

import imaplib
import re
from dataclasses import asdict
from email.message import EmailMessage
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from . import imap, mime, smtp
from .config import ensure_writable, get_credentials, settings
from .errors import MailError

mcp = MCPServer("imap")

_IMAP_DATE = re.compile(r"\d{1,2}-[A-Za-z]{3}-\d{4}")
_UNSAFE_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def run() -> None:
    """Lance le serveur MCP sur stdio."""
    mcp.run()


# ------------------------------------------------------------------ helpers
def _sender() -> str:
    return get_credentials()[0]


def _load(uid: str, folder: str = "INBOX") -> EmailMessage:
    with imap.session(folder) as conn:
        raw, _ = imap.fetch_raw(conn, uid)
    return mime.parse(raw)


def _imap_date(value: str, name: str) -> str:
    if value and not _IMAP_DATE.fullmatch(value):
        raise MailError(
            f"{name} : format attendu JJ-Mon-AAAA (ex. 01-Oct-2026).")
    return value


def _move_to_role(uids: list[str], folder: str, role: str) -> str:
    """Déplace des messages vers le dossier spécial `role` et renvoie son nom."""
    ensure_writable()
    ids = imap.uid_set(uids)
    with imap.session(folder, readonly=False) as conn:
        destination = imap.special_folder(conn, role)
        imap.move(conn, ids, destination)
    return destination


def _save_draft(msg: EmailMessage) -> tuple[str, str | None]:
    with imap.session() as conn:
        drafts = imap.special_folder(conn, "drafts")
        imap.append(conn, drafts, msg, r"\Draft")
        uid = imap.uid_by_message_id(conn, drafts, str(msg["Message-ID"]))
    return drafts, uid


def _save_to_sent(msg: EmailMessage) -> None:
    with imap.session() as conn:
        sent = imap.special_folder(conn, "sent")
        imap.append(conn, sent, msg, r"\Seen")


def _unique_path(path: Path) -> Path:
    candidate, n = path, 1
    while candidate.exists():
        candidate = path.with_name(f"{path.stem}-{n}{path.suffix}")
        n += 1
    return candidate


# ------------------------------------------------------------------ compte et dossiers
@mcp.tool()
def whoami() -> dict:
    """Adresse connectée, serveurs utilisés, mode lecture seule, dossier de téléchargement."""
    user, _ = get_credentials()
    return {
        "user": user,
        "imap": f"{settings.imap_host}:{settings.imap_port}",
        "smtp": f"{settings.smtp_host}:{settings.smtp_port}",
        "read_only": settings.read_only,
        "download_dir": str(settings.download_dir),
    }


@mcp.tool()
def list_folders() -> list[dict]:
    """Liste les dossiers avec nombre de messages, non lus et rôle (drafts, sent, trash, junk, archive)."""
    with imap.session() as conn:
        result = []
        for folder in imap.list_folders(conn):
            counts = imap.folder_status(conn, folder.name)
            result.append({
                "folder": folder.name,
                "role": folder.role,
                "messages": counts[0] if counts else None,
                "unread": counts[1] if counts else None,
            })
    return result


@mcp.tool()
def get_quota() -> dict:
    """Quota de stockage de la boîte, si le serveur le fournit (réponse IMAP brute)."""
    try:
        with imap.session() as conn:
            status, data = conn.getquotaroot("INBOX")
    except imaplib.IMAP4.error:
        return {"quota": "non disponible"}
    if status != "OK":
        return {"quota": "non disponible"}
    return {"raw": [item.decode(errors="replace") for item in data if isinstance(item, bytes)]}


@mcp.tool()
def create_folder(name: str) -> str:
    """Crée un dossier (ex. 'Projets/Alpha')."""
    ensure_writable()
    with imap.session() as conn:
        status, data = conn.create(imap.quote(name))
        imap.expect_ok(status, data, "Création")
        conn.subscribe(imap.quote(name))
    return f"Dossier créé : {name}"


@mcp.tool()
def rename_folder(old_name: str, new_name: str) -> str:
    """Renomme ou déplace un dossier."""
    ensure_writable()
    with imap.session() as conn:
        status, data = conn.rename(imap.quote(old_name), imap.quote(new_name))
        imap.expect_ok(status, data, "Renommage")
    return f"{old_name} -> {new_name}"


@mcp.tool()
def delete_folder(name: str) -> str:
    """Supprime un dossier VIDE uniquement (refus s'il contient des messages)."""
    ensure_writable()
    with imap.session() as conn:
        count = imap.select(conn, name)
        if count:
            raise MailError(
                f"Dossier non vide ({count} messages) : déplacer les messages d'abord.")
        conn.close()
        status, data = conn.delete(imap.quote(name))
        imap.expect_ok(status, data, "Suppression du dossier")
    return f"Dossier supprimé : {name}"


# ------------------------------------------------------------------ recherche et lecture
@mcp.tool()
def search_mails(folder: str = "INBOX", text: str = "", sender: str = "", to: str = "", cc: str = "",
                 subject: str = "", body: str = "", since: str = "", before: str = "",
                 unread: bool | None = None, flagged: bool | None = None,
                 larger_kb: int = 0, smaller_kb: int = 0, limit: int = 20, offset: int = 0) -> dict:
    """Recherche multicritère, résultats du plus récent au plus ancien.

    text : cherche dans les en-têtes et le corps. since/before : JJ-Mon-AAAA (ex. 01-Oct-2026).
    unread / flagged : True ou False pour filtrer, None pour ignorer.
    Pagination : limit (max 100) et offset. Renvoie total et liste (uid, date, from, to, subject, flags, size).
    """
    limit = max(1, min(limit, 100))
    criteria: list[str] = []
    for key, value in (("TEXT", text), ("FROM", sender), ("TO", to), ("CC", cc),
                       ("SUBJECT", subject), ("BODY", body)):
        if value:
            criteria += [key, imap.search_literal(value)]
    if since:
        criteria += ["SINCE", _imap_date(since, "since")]
    if before:
        criteria += ["BEFORE", _imap_date(before, "before")]
    if unread is not None:
        criteria.append("UNSEEN" if unread else "SEEN")
    if flagged is not None:
        criteria.append("FLAGGED" if flagged else "UNFLAGGED")
    if larger_kb:
        criteria += ["LARGER", str(larger_kb * 1024)]
    if smaller_kb:
        criteria += ["SMALLER", str(smaller_kb * 1024)]
    with imap.session(folder) as conn:
        uids = imap.search_uids(conn, criteria)[::-1]
        page = uids[offset:offset + limit]
        return {
            "folder": folder,
            "total": len(uids),
            "offset": offset,
            "results": imap.fetch_summaries(conn, page),
        }


@mcp.tool()
def read_mail(uid: str, folder: str = "INBOX", include_html: bool = False, mark_read: bool = False) -> dict:
    """Lit un mail : en-têtes complets, corps texte, pièces jointes (index à passer à download_attachment).

    Ne marque pas le message comme lu, sauf si mark_read=True.
    """
    if mark_read:
        ensure_writable()
    with imap.session(folder, readonly=not mark_read) as conn:
        raw, flags = imap.fetch_raw(conn, uid)
        if mark_read:
            imap.store(conn, imap.uid_set([uid]), r"\Seen", True)
    msg = mime.parse(raw)
    content = mime.read_content(msg)
    info = mime.summary(uid, msg, flags)
    info.update(
        from_list=mime.addresses(msg["From"]),
        to_list=mime.addresses(msg["To"]),
        cc_list=mime.addresses(msg["Cc"]),
        reply_to=mime.addresses(msg["Reply-To"]),
        message_id=mime.text_header(msg, "Message-ID"),
        in_reply_to=mime.text_header(msg, "In-Reply-To"),
        references=mime.text_header(msg, "References"),
        body=mime.excerpt(content.text, settings.max_body),
        attachments=[asdict(att) for att in content.attachments],
    )
    if include_html:
        info["html"] = mime.excerpt(content.html, settings.max_body)
    return info


@mcp.tool()
def get_thread(uid: str, folder: str = "INBOX") -> list[dict]:
    """Fil de discussion d'un message (Message-ID/References) dans le dossier courant et les envoyés, trié par date."""
    with imap.session(folder) as conn:
        raw, _ = imap.fetch_raw(conn, uid)
        sent = imap.special_folder(conn, "sent")
    msg = mime.parse(raw)
    header_values = " ".join(str(msg.get(h) or "")
                             for h in ("Message-ID", "References", "In-Reply-To"))
    ids = set(re.findall(r"<[^>]+>", header_values))
    thread: list[dict] = []
    for name in dict.fromkeys([folder, sent]):
        with imap.session(name) as conn:
            uids: set[str] = set()
            for message_id in ids:
                for field in ("Message-ID", "References", "In-Reply-To"):
                    uids.update(imap.search_uids(
                        conn, ["HEADER", field, imap.search_literal(message_id)]))
            thread += [{"folder": name, **item}
                       for item in imap.fetch_summaries(conn, sorted(uids))]
    return sorted(thread, key=lambda item: item["date"])


@mcp.tool()
def download_attachment(uid: str, index: int, folder: str = "INBOX", dest_dir: str = "") -> dict:
    """Enregistre une pièce jointe sur le disque (index fourni par read_mail). Par défaut : Downloads/imap-mcp."""
    part = mime.get_part(_load(uid, folder), index)
    if part is None:
        raise MailError(f"Aucune pièce jointe à l'index {index}.")
    payload = part.get_payload(decode=True) or b""
    target_dir = Path(dest_dir).expanduser(
    ) if dest_dir else settings.download_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    name = _UNSAFE_FILENAME_CHARS.sub("_", mime.decode_text(
        part.get_filename()) or f"piece-{index}").strip()
    target = _unique_path(target_dir / (name or "piece"))
    target.write_bytes(payload)
    return {"path": str(target), "size": len(payload), "type": part.get_content_type()}


# ------------------------------------------------------------------ état et déplacement
@mcp.tool()
def mark_mails(uids: list[str], folder: str = "INBOX", read: bool | None = None,
               flagged: bool | None = None, answered: bool | None = None) -> str:
    """Modifie l'état de messages : read (lu/non lu), flagged (suivi), answered (répondu). None = inchangé."""
    ensure_writable()
    ids = imap.uid_set(uids)
    changes = ((r"\Seen", read), (r"\Flagged", flagged),
               (r"\Answered", answered))
    with imap.session(folder, readonly=False) as conn:
        for flag, value in changes:
            if value is not None:
                imap.store(conn, ids, flag, value)
    return f"{len(uids)} message(s) mis à jour."


@mcp.tool()
def move_mails(uids: list[str], destination: str, folder: str = "INBOX") -> str:
    """Déplace des messages vers un autre dossier."""
    ensure_writable()
    ids = imap.uid_set(uids)
    with imap.session(folder, readonly=False) as conn:
        imap.move(conn, ids, destination)
    return f"{len(uids)} message(s) déplacé(s) vers {destination}."


@mcp.tool()
def copy_mails(uids: list[str], destination: str, folder: str = "INBOX") -> str:
    """Copie des messages vers un autre dossier."""
    ensure_writable()
    ids = imap.uid_set(uids)
    with imap.session(folder) as conn:
        imap.copy(conn, ids, destination)
    return f"{len(uids)} message(s) copié(s) vers {destination}."


@mcp.tool()
def archive_mails(uids: list[str], folder: str = "INBOX") -> str:
    """Déplace vers le dossier Archive."""
    destination = _move_to_role(uids, folder, "archive")
    return f"{len(uids)} message(s) archivé(s) dans {destination}."


@mcp.tool()
def trash_mails(uids: list[str], folder: str = "INBOX") -> str:
    """Envoie à la corbeille (récupérable). Aucune suppression définitive n'est proposée."""
    destination = _move_to_role(uids, folder, "trash")
    return f"{len(uids)} message(s) envoyé(s) à la corbeille ({destination})."


@mcp.tool()
def mark_spam(uids: list[str], folder: str = "INBOX") -> str:
    """Déplace vers le dossier Junk (indésirables)."""
    destination = _move_to_role(uids, folder, "junk")
    return f"{len(uids)} message(s) déplacé(s) vers {destination}."


# ------------------------------------------------------------------ composition
@mcp.tool()
def create_draft(to: list[str], subject: str, body: str, cc: list[str] | None = None,
                 bcc: list[str] | None = None, html: str | None = None,
                 attachments: list[str] | None = None) -> dict:
    """Crée un brouillon dans Drafts (pièces jointes = chemins de fichiers locaux). Renvoie l'uid du brouillon."""
    ensure_writable()
    msg = mime.build_message(_sender(), to, subject, body,
                             cc=cc, bcc=bcc, html=html, attachments=attachments)
    drafts, uid = _save_draft(msg)
    return {"folder": drafts, "uid": uid, "message_id": str(msg["Message-ID"])}


@mcp.tool()
def update_draft(uid: str, to: list[str] | None = None, subject: str | None = None, body: str | None = None,
                 cc: list[str] | None = None, bcc: list[str] | None = None,
                 attachments: list[str] | None = None) -> dict:
    """Modifie un brouillon : les champs fournis remplacent les anciens, les autres sont conservés.

    Les pièces jointes existantes sont conservées ; `attachments` en ajoute de nouvelles. Renvoie le nouvel uid.
    """
    ensure_writable()
    sender = _sender()
    with imap.session() as conn:
        drafts = imap.special_folder(conn, "drafts")
    with imap.session(drafts, readonly=False) as conn:
        raw, _ = imap.fetch_raw(conn, uid)
        old = mime.parse(raw)
        content = mime.read_content(old)
        msg = mime.build_message(
            sender,
            to if to is not None else mime.emails(old["To"]),
            subject if subject is not None else mime.decode_text(
                old["Subject"]),
            body if body is not None else content.text,
            cc=cc if cc is not None else mime.emails(old["Cc"]),
            bcc=bcc if bcc is not None else mime.emails(old["Bcc"]),
            html=None if body is not None else (content.html or None),
            in_reply_to=mime.text_header(old, "In-Reply-To"),
            references=mime.text_header(old, "References"),
        )
        mime.copy_attachments(old, msg, content.attachments)
        mime.attach_files(msg, attachments)
        imap.append(conn, drafts, msg, r"\Draft")
        imap.delete_uids(conn, imap.uid_set([uid]))
        new_uid = imap.uid_by_message_id(conn, drafts, str(msg["Message-ID"]))
    return {"folder": drafts, "uid": new_uid}


@mcp.tool()
def send_mail(to: list[str], subject: str, body: str, cc: list[str] | None = None, bcc: list[str] | None = None,
              html: str | None = None, attachments: list[str] | None = None, save_copy: bool = True) -> dict:
    """Envoie un mail via SMTP et en garde une copie dans Sent. Action irréversible : vérifier destinataires et contenu."""
    ensure_writable()
    if not (to or cc or bcc):
        raise MailError("Au moins un destinataire est requis.")
    msg = mime.build_message(_sender(), to, subject, body,
                             cc=cc, bcc=bcc, html=html, attachments=attachments)
    recipients = mime.all_recipients(msg)
    smtp.send(msg, recipients)
    if save_copy:
        _save_to_sent(msg)
    return {"sent": True, "message_id": str(msg["Message-ID"]), "recipients": recipients}


@mcp.tool()
def send_draft(uid: str, save_copy: bool = True) -> dict:
    """Envoie un brouillon existant puis le retire de Drafts. Action irréversible."""
    ensure_writable()
    with imap.session() as conn:
        drafts = imap.special_folder(conn, "drafts")
    with imap.session(drafts, readonly=False) as conn:
        raw, _ = imap.fetch_raw(conn, uid)
        msg = mime.parse(raw)
        recipients = mime.all_recipients(msg)
        if not recipients:
            raise MailError("Le brouillon n'a aucun destinataire.")
        smtp.send(msg, recipients)
        imap.delete_uids(conn, imap.uid_set([uid]))
    if save_copy:
        _save_to_sent(msg)
    return {"sent": True, "message_id": str(msg["Message-ID"]), "recipients": recipients}


@mcp.tool()
def reply_mail(uid: str, body: str, folder: str = "INBOX", reply_all: bool = False, send: bool = False,
               html: str | None = None, attachments: list[str] | None = None,
               include_quote: bool = True) -> dict:
    """Répond à un mail (In-Reply-To/References et 'Re:' gérés). Par défaut crée un BROUILLON ;
    send=True envoie immédiatement. reply_all=True ajoute les autres destinataires en copie."""
    ensure_writable()
    sender = _sender()
    original = _load(uid, folder)
    to, cc = mime.reply_target(original, reply_all, sender)
    subject = mime.prefixed(mime.decode_text(
        original["Subject"]), "Re:", r"(?i)^(re|réf)\s*:")
    message_id = mime.text_header(original, "Message-ID")
    references = " ".join(x for x in (mime.text_header(
        original, "References"), message_id) if x)
    quoted = mime.quote_reply(original, mime.read_content(
        original).text) if include_quote else ""
    msg = mime.build_message(
        sender, to, subject, body + quoted,
        cc=cc, html=html, attachments=attachments,
        in_reply_to=message_id, references=references,
    )
    if send:
        smtp.send(msg, mime.all_recipients(msg))
        _save_to_sent(msg)
        with imap.session(folder, readonly=False) as conn:
            imap.store(conn, imap.uid_set([uid]), r"\Answered \Seen", True)
        return {"sent": True, "to": to, "cc": cc, "subject": subject}
    drafts, draft_uid = _save_draft(msg)
    return {"sent": False, "draft_folder": drafts, "draft_uid": draft_uid,
            "to": to, "cc": cc, "subject": subject}


@mcp.tool()
def forward_mail(uid: str, to: list[str], body: str = "", folder: str = "INBOX", cc: list[str] | None = None,
                 send: bool = False, include_attachments: bool = True) -> dict:
    """Transfère un mail (corps cité + pièces jointes d'origine). Par défaut crée un BROUILLON ; send=True envoie."""
    ensure_writable()
    original = _load(uid, folder)
    content = mime.read_content(original)
    subject = mime.prefixed(mime.decode_text(
        original["Subject"]), "Fwd:", r"(?i)^(tr|fwd?)\s*:")
    header = (
        "\n\n---------- Message transféré ----------\n"
        f"De : {mime.decode_text(original['From'])}\n"
        f"Date : {mime.decode_text(original['Date'])}\n"
        f"Objet : {mime.decode_text(original['Subject'])}\n"
        f"À : {mime.decode_text(original['To'])}\n\n"
    )
    msg = mime.build_message(_sender(), to, subject,
                             body + header + content.text, cc=cc)
    if include_attachments:
        mime.copy_attachments(original, msg, content.attachments)
    if send:
        smtp.send(msg, mime.all_recipients(msg))
        _save_to_sent(msg)
        return {"sent": True, "to": to, "subject": subject}
    drafts, draft_uid = _save_draft(msg)
    return {"sent": False, "draft_folder": drafts, "draft_uid": draft_uid,
            "to": to, "subject": subject}
