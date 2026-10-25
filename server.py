"""Serveur MCP complet pour une boite mail via IMAP/SMTP.

Lecture, recherche, fils de discussion, pieces jointes, drapeaux, dossiers,
brouillons, envoi, reponse, transfert. Ecriture desactivable avec MAIL_READONLY=1.
Suppression = deplacement vers la corbeille ; aucune suppression definitive de message.
"""
from __future__ import annotations

import re
from pathlib import Path

from mcp.server.mcpserver import MCPServer

import core
from core import (imap_session, quote, search_quote, uid_set)

mcp = MCPServer("imap")


# ------------------------------------------------------------------ utilitaires
def _remove_uids(imap, uids: str) -> None:
    """Marque \\Deleted et purge uniquement ces UID si le serveur supporte UIDPLUS."""
    imap.uid("STORE", uids, "+FLAGS.SILENT", r"(\Deleted)")
    caps = " ".join(c.decode() if isinstance(c, bytes)
                    else c for c in imap.capabilities)
    if "UIDPLUS" in caps:
        imap.uid("EXPUNGE", uids)


def _move(imap, uids: str, destination: str) -> None:
    caps = " ".join(c.decode() if isinstance(c, bytes)
                    else c for c in imap.capabilities)
    if "MOVE" in caps.split():
        status, data = imap.uid("MOVE", uids, quote(destination))
        if status != "OK":
            raise RuntimeError(f"Deplacement refuse : {data}")
        return
    status, data = imap.uid("COPY", uids, quote(destination))
    if status != "OK":
        raise RuntimeError(f"Copie refusee : {data}")
    _remove_uids(imap, uids)


def _original(imap, uid: str):
    raw, flags = core.fetch_raw(imap, uid)
    return core.parse_message(raw), flags


# ------------------------------------------------------------------ compte et dossiers
@mcp.tool()
def whoami() -> dict:
    """Adresse connectee, serveurs utilises, mode lecture seule, dossier de telechargement."""
    user, _ = core.credentials()
    return {"user": user, "imap": f"{core.IMAP_HOST}:{core.IMAP_PORT}", "smtp": f"{core.SMTP_HOST}:{core.SMTP_PORT}",
            "read_only": core.READ_ONLY, "download_dir": str(core.DOWNLOAD_DIR)}


@mcp.tool()
def list_folders() -> list[dict]:
    """Liste les dossiers avec nombre de messages, non lus et role (Drafts, Sent, Trash, Junk...)."""
    with imap_session() as imap:
        out = []
        for name, attrs in core.list_folder_names(imap):
            status, data = imap.status(quote(name), "(MESSAGES UNSEEN)")
            m = re.search(r"MESSAGES (\d+) UNSEEN (\d+)",
                          data[0].decode(errors="replace")) if status == "OK" else None
            role = next((a.strip("\\") for a in attrs.split() if a.lower() in
                         ("\\drafts", "\\sent", "\\trash", "\\junk", "\\archive")), None)
            out.append({"folder": name, "role": role,
                        "messages": int(m.group(1)) if m else None, "unread": int(m.group(2)) if m else None})
        return out


@mcp.tool()
def get_quota() -> dict:
    """Quota de stockage de la boite (ko utilises / limite), si le serveur le fournit."""
    with imap_session() as imap:
        status, data = imap.getquotaroot("INBOX")
        if status != "OK":
            return {"quota": "non disponible"}
        return {"raw": [d.decode(errors="replace") if isinstance(d, bytes) else str(d)
                        for d in data[1] if d] if len(data) > 1 else []}


@mcp.tool()
def create_folder(name: str) -> str:
    """Cree un dossier (ex. 'Projets/Alpha')."""
    core.ensure_writable()
    with imap_session() as imap:
        status, data = imap.create(quote(name))
        if status != "OK":
            raise RuntimeError(f"Creation refusee : {data}")
        imap.subscribe(quote(name))
    return f"Dossier cree : {name}"


@mcp.tool()
def rename_folder(old_name: str, new_name: str) -> str:
    """Renomme ou deplace un dossier."""
    core.ensure_writable()
    with imap_session() as imap:
        status, data = imap.rename(quote(old_name), quote(new_name))
        if status != "OK":
            raise RuntimeError(f"Renommage refuse : {data}")
    return f"{old_name} -> {new_name}"


@mcp.tool()
def delete_folder(name: str) -> str:
    """Supprime un dossier VIDE uniquement (refus s'il contient des messages)."""
    core.ensure_writable()
    with imap_session() as imap:
        status, data = imap.select(quote(name), readonly=True)
        if status != "OK":
            raise RuntimeError(f"Dossier introuvable : {name}")
        if int(data[0]) > 0:
            raise RuntimeError(
                f"Dossier non vide ({int(data[0])} messages) : deplacer les messages d'abord.")
        imap.close()
        status, data = imap.delete(quote(name))
        if status != "OK":
            raise RuntimeError(f"Suppression refusee : {data}")
    return f"Dossier supprime : {name}"


# ------------------------------------------------------------------ recherche et lecture
@mcp.tool()
def search_mails(folder: str = "INBOX", text: str = "", sender: str = "", to: str = "", cc: str = "",
                 subject: str = "", body: str = "", since: str = "", before: str = "",
                 unread: bool | None = None, flagged: bool | None = None,
                 larger_kb: int = 0, smaller_kb: int = 0, limit: int = 20, offset: int = 0) -> dict:
    """Recherche multicritere, resultats du plus recent au plus ancien.

    text : cherche partout (en-tetes + corps). since/before : JJ-Mon-AAAA (ex. 01-Oct-2026).
    unread / flagged : True ou False pour filtrer, None pour ignorer.
    Pagination : limit (max 100) et offset. Renvoie total + liste (uid, date, from, to, subject, flags, size).
    """
    limit = max(1, min(limit, 100))
    c: list[str] = []
    for key, val in (("TEXT", text), ("FROM", sender), ("TO", to), ("CC", cc), ("SUBJECT", subject), ("BODY", body)):
        if val:
            c += [key, search_quote(val)]
    if since:
        c += ["SINCE", since]
    if before:
        c += ["BEFORE", before]
    if unread is True:
        c.append("UNSEEN")
    elif unread is False:
        c.append("SEEN")
    if flagged is True:
        c.append("FLAGGED")
    elif flagged is False:
        c.append("UNFLAGGED")
    if larger_kb:
        c += ["LARGER", str(larger_kb * 1024)]
    if smaller_kb:
        c += ["SMALLER", str(smaller_kb * 1024)]
    with imap_session(folder) as imap:
        _, data = imap.uid("SEARCH", None, *(c or ["ALL"]))
        uids = [u.decode() for u in data[0].split()][::-1]
        page = uids[offset:offset + limit]
        return {"folder": folder, "total": len(uids), "offset": offset, "results": core.fetch_headers(imap, page)}


@mcp.tool()
def read_mail(uid: str, folder: str = "INBOX", include_html: bool = False, mark_read: bool = False) -> dict:
    """Lit un mail : en-tetes complets, corps texte, pieces jointes (index a passer a download_attachment).

    Ne marque pas le message comme lu sauf mark_read=True.
    """
    with imap_session(folder, readonly=not mark_read) as imap:
        msg, flags = _original(imap, uid)
        text, html, atts = core.body_and_attachments(msg)
        if mark_read:
            imap.uid("STORE", uid, "+FLAGS.SILENT", r"(\Seen)")
        info = core.summary(uid, msg, flags)
        info.update({
            "from_list": core.addresses(msg["From"]), "to_list": core.addresses(msg["To"]),
            "cc_list": core.addresses(msg["Cc"]), "reply_to": core.addresses(msg["Reply-To"]),
            "message_id": msg["Message-ID"], "in_reply_to": msg["In-Reply-To"], "references": msg["References"],
            "body": text[:core.MAX_BODY] + ("\n[tronque]" if len(text) > core.MAX_BODY else ""),
            "attachments": atts,
        })
        if include_html:
            info["html"] = html[:core.MAX_BODY]
        return info


@mcp.tool()
def get_thread(uid: str, folder: str = "INBOX") -> list[dict]:
    """Fil de discussion d'un message (par Message-ID/References), dossier courant + envoyes, trie par date."""
    with imap_session(folder) as imap:
        msg, _ = _original(imap, uid)
        ids = set(re.findall(r"<[^>]+>", " ".join(str(msg.get(h) or "") for h in
                                                  ("Message-ID", "References", "In-Reply-To"))))
        sent = core.special_folder(
            imap, "\\Sent", ["Sent", "Éléments envoyés"])
    results: dict[tuple[str, str], dict] = {}
    for fld in {folder, sent}:
        with imap_session(fld) as imap:
            for mid in ids:
                for field in ("Message-ID", "References", "In-Reply-To"):
                    _, data = imap.uid(
                        "SEARCH", None, "HEADER", field, search_quote(mid))
                    for u in data[0].split():
                        key = (fld, u.decode())
                        if key not in results:
                            results[key] = {
                                "folder": fld, **core.fetch_headers(imap, [u.decode()])[0]}
    return sorted(results.values(), key=lambda m: m["date"])


@mcp.tool()
def download_attachment(uid: str, index: int, folder: str = "INBOX", dest_dir: str = "") -> dict:
    """Enregistre une piece jointe sur le disque (index fourni par read_mail). Defaut : Downloads/imap-mcp."""
    with imap_session(folder) as imap:
        msg, _ = _original(imap, uid)
    for i, part in enumerate(msg.walk()):
        if i == index and not part.is_multipart():
            payload = part.get_payload(decode=True) or b""
            target_dir = Path(dest_dir).expanduser(
            ) if dest_dir else core.DOWNLOAD_DIR
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / \
                core.safe_filename(
                    core.dec(part.get_filename()) or f"piece-{index}")
            n = 1
            while target.exists():
                target = target.with_name(f"{target.stem}-{n}{target.suffix}")
                n += 1
            target.write_bytes(payload)
            return {"path": str(target), "size": len(payload), "type": part.get_content_type()}
    raise RuntimeError(f"Aucune piece jointe a l'index {index}")


# ------------------------------------------------------------------ etat des messages
@mcp.tool()
def mark_mails(uids: list[str], folder: str = "INBOX", read: bool | None = None, flagged: bool | None = None,
               answered: bool | None = None) -> str:
    """Change l'etat de messages : read (lu/non lu), flagged (suivi/etoile), answered (repondu). None = inchange."""
    core.ensure_writable()
    ids = uid_set(uids)
    changes = [(r"\Seen", read), (r"\Flagged", flagged),
               (r"\Answered", answered)]
    with imap_session(folder, readonly=False) as imap:
        for flag, value in changes:
            if value is not None:
                imap.uid(
                    "STORE", ids, "+FLAGS.SILENT" if value else "-FLAGS.SILENT", f"({flag})")
    return f"{len(uids)} message(s) mis a jour"


@mcp.tool()
def move_mails(uids: list[str], destination: str, folder: str = "INBOX") -> str:
    """Deplace des messages vers un autre dossier."""
    core.ensure_writable()
    with imap_session(folder, readonly=False) as imap:
        _move(imap, uid_set(uids), destination)
    return f"{len(uids)} message(s) deplace(s) vers {destination}"


@mcp.tool()
def copy_mails(uids: list[str], destination: str, folder: str = "INBOX") -> str:
    """Copie des messages vers un autre dossier."""
    core.ensure_writable()
    with imap_session(folder) as imap:
        status, data = imap.uid("COPY", uid_set(uids), quote(destination))
        if status != "OK":
            raise RuntimeError(f"Copie refusee : {data}")
    return f"{len(uids)} message(s) copie(s) vers {destination}"


@mcp.tool()
def archive_mails(uids: list[str], folder: str = "INBOX") -> str:
    """Deplace vers le dossier Archive."""
    core.ensure_writable()
    with imap_session(folder, readonly=False) as imap:
        dest = core.special_folder(imap, "\\Archive", ["Archive", "Archives"])
        _move(imap, uid_set(uids), dest)
    return f"{len(uids)} message(s) archive(s)"


@mcp.tool()
def trash_mails(uids: list[str], folder: str = "INBOX") -> str:
    """Envoie a la corbeille (recuperable). Aucune suppression definitive n'est proposee."""
    core.ensure_writable()
    with imap_session(folder, readonly=False) as imap:
        dest = core.special_folder(imap, "\\Trash", ["Trash", "Corbeille"])
        _move(imap, uid_set(uids), dest)
    return f"{len(uids)} message(s) mis a la corbeille"


@mcp.tool()
def mark_spam(uids: list[str], folder: str = "INBOX") -> str:
    """Deplace vers le dossier Junk (indesirables)."""
    core.ensure_writable()
    with imap_session(folder, readonly=False) as imap:
        dest = core.special_folder(imap, "\\Junk", ["Junk", "Spam"])
        _move(imap, uid_set(uids), dest)
    return f"{len(uids)} message(s) deplace(s) vers {dest}"


# ------------------------------------------------------------------ composition
def _save_sent(imap_msg, save_copy: bool) -> None:
    if not save_copy:
        return
    with imap_session() as imap:
        sent = core.special_folder(
            imap, "\\Sent", ["Sent", "Éléments envoyés"])
        core.append_to(imap, sent, imap_msg, r"\Seen")


@mcp.tool()
def create_draft(to: list[str], subject: str, body: str, cc: list[str] | None = None,
                 bcc: list[str] | None = None, html: str | None = None,
                 attachments: list[str] | None = None) -> dict:
    """Cree un brouillon dans Drafts (pieces jointes = chemins de fichiers locaux). Renvoie l'uid du brouillon."""
    core.ensure_writable()
    msg = core.build_message(to, subject, body, cc, bcc, html, attachments)
    with imap_session() as imap:
        drafts = core.special_folder(
            imap, "\\Drafts", ["Drafts", "Brouillons"])
        core.append_to(imap, drafts, msg, r"\Draft")
        imap.select(quote(drafts), readonly=True)
        _, data = imap.uid("SEARCH", None, "HEADER",
                           "Message-ID", search_quote(msg["Message-ID"]))
    return {"folder": drafts, "uid": data[0].split()[-1].decode() if data[0] else None, "message_id": msg["Message-ID"]}


@mcp.tool()
def update_draft(uid: str, to: list[str] | None = None, subject: str | None = None, body: str | None = None,
                 cc: list[str] | None = None, bcc: list[str] | None = None,
                 attachments: list[str] | None = None) -> dict:
    """Modifie un brouillon : les champs fournis remplacent les anciens, les autres sont conserves.

    Les pieces jointes existantes sont conservees ; `attachments` en ajoute de nouvelles. Renvoie le nouvel uid.
    """
    core.ensure_writable()
    with imap_session() as imap:
        drafts = core.special_folder(
            imap, "\\Drafts", ["Drafts", "Brouillons"])
    with imap_session(drafts, readonly=False) as imap:
        old, _ = _original(imap, uid)
        text, html, atts = core.body_and_attachments(old)
        new = core.build_message(
            to if to is not None else [a["email"]
                                       for a in core.addresses(old["To"])],
            subject if subject is not None else core.dec(old["Subject"]),
            body if body is not None else text,
            cc if cc is not None else [a["email"]
                                       for a in core.addresses(old["Cc"])],
            bcc if bcc is not None else [a["email"]
                                         for a in core.addresses(old["Bcc"])],
            None if body is not None else (html or None),
            None, old["In-Reply-To"], old["References"])
        for i, part in enumerate(old.walk()):
            if any(a["index"] == i for a in atts):
                maintype, subtype = part.get_content_type().split("/", 1)
                new.add_attachment(part.get_payload(decode=True) or b"", maintype=maintype, subtype=subtype,
                                   filename=core.dec(part.get_filename()))
        core.attach_files(new, attachments)
        core.append_to(imap, drafts, new, r"\Draft")
        _remove_uids(imap, uid_set([uid]))
        _, data = imap.uid("SEARCH", None, "HEADER",
                           "Message-ID", search_quote(new["Message-ID"]))
    return {"folder": drafts, "uid": data[0].split()[-1].decode() if data[0] else None}


@mcp.tool()
def send_mail(to: list[str], subject: str, body: str, cc: list[str] | None = None, bcc: list[str] | None = None,
              html: str | None = None, attachments: list[str] | None = None, save_copy: bool = True) -> dict:
    """Envoie un mail via SMTP et en garde une copie dans Sent. Action irreversible : verifier destinataires et contenu."""
    core.ensure_writable()
    if not (to or cc or bcc):
        raise ValueError("Au moins un destinataire est requis")
    msg = core.build_message(to, subject, body, cc, bcc, html, attachments)
    core.smtp_send(msg, core.all_recipients(msg))
    _save_sent(msg, save_copy)
    return {"sent": True, "message_id": msg["Message-ID"], "recipients": core.all_recipients(msg)}


@mcp.tool()
def send_draft(uid: str, save_copy: bool = True) -> dict:
    """Envoie un brouillon existant puis le retire de Drafts. Action irreversible."""
    core.ensure_writable()
    with imap_session() as imap:
        drafts = core.special_folder(
            imap, "\\Drafts", ["Drafts", "Brouillons"])
    with imap_session(drafts, readonly=False) as imap:
        msg, _ = _original(imap, uid)
        recipients = core.all_recipients(msg)
        if not recipients:
            raise ValueError("Le brouillon n'a aucun destinataire")
        core.smtp_send(msg, recipients)
        _remove_uids(imap, uid_set([uid]))
    _save_sent(msg, save_copy)
    return {"sent": True, "message_id": msg["Message-ID"], "recipients": recipients}


@mcp.tool()
def reply_mail(uid: str, body: str, folder: str = "INBOX", reply_all: bool = False, send: bool = False,
               html: str | None = None, attachments: list[str] | None = None,
               include_quote: bool = True) -> dict:
    """Repond a un mail (en-tetes In-Reply-To/References et 'Re:' geres). Par defaut cree un BROUILLON ;
    send=True envoie immediatement. reply_all=True ajoute les autres destinataires en copie."""
    core.ensure_writable()
    user, _ = core.credentials()
    with imap_session(folder, readonly=False) as imap:
        orig, _ = _original(imap, uid)
        orig_text, _, _ = core.body_and_attachments(orig)
    to, cc = core.reply_target(orig, reply_all, user)
    subject = core.dec(orig["Subject"])
    subject = subject if re.match(
        r"(?i)^(re|réf)\s*:", subject) else f"Re: {subject}"
    mid = orig["Message-ID"]
    refs = " ".join(x for x in (orig["References"], mid) if x)
    full_body = body + (core.quote_text(orig, orig_text)
                        if include_quote else "")
    msg = core.build_message(to, subject, full_body,
                             cc, None, html, attachments, mid, refs)
    if send:
        core.smtp_send(msg, core.all_recipients(msg))
        _save_sent(msg, True)
        with imap_session(folder, readonly=False) as imap:
            imap.uid("STORE", uid, "+FLAGS.SILENT", r"(\Answered \Seen)")
        return {"sent": True, "to": to, "cc": cc, "subject": subject}
    with imap_session() as imap:
        drafts = core.special_folder(
            imap, "\\Drafts", ["Drafts", "Brouillons"])
        core.append_to(imap, drafts, msg, r"\Draft")
    return {"sent": False, "draft_saved_in": drafts, "to": to, "cc": cc, "subject": subject}


@mcp.tool()
def forward_mail(uid: str, to: list[str], body: str = "", folder: str = "INBOX", cc: list[str] | None = None,
                 send: bool = False, include_attachments: bool = True) -> dict:
    """Transfere un mail (corps cite + pieces jointes d'origine). Par defaut cree un BROUILLON ; send=True envoie."""
    core.ensure_writable()
    with imap_session(folder, readonly=False) as imap:
        orig, _ = _original(imap, uid)
    text, _, atts = core.body_and_attachments(orig)
    subject = core.dec(orig["Subject"])
    subject = subject if re.match(
        r"(?i)^(tr|fwd?)\s*:", subject) else f"Fwd: {subject}"
    header = (f"\n\n---------- Message transféré ----------\nDe : {core.dec(orig['From'])}\n"
              f"Date : {core.dec(orig['Date'])}\nObjet : {core.dec(orig['Subject'])}\nÀ : {core.dec(orig['To'])}\n\n")
    msg = core.build_message(to, subject, body + header + text, cc)
    if include_attachments:
        for i, part in enumerate(orig.walk()):
            if any(a["index"] == i for a in atts):
                maintype, subtype = part.get_content_type().split("/", 1)
                msg.add_attachment(part.get_payload(decode=True) or b"", maintype=maintype, subtype=subtype,
                                   filename=core.dec(part.get_filename()))
    if send:
        core.smtp_send(msg, core.all_recipients(msg))
        _save_sent(msg, True)
        return {"sent": True, "to": to, "subject": subject}
    with imap_session() as imap:
        drafts = core.special_folder(
            imap, "\\Drafts", ["Drafts", "Brouillons"])
        core.append_to(imap, drafts, msg, r"\Draft")
    return {"sent": False, "draft_saved_in": drafts, "to": to, "subject": subject}


if __name__ == "__main__":
    mcp.run()
