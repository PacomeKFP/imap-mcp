"""Point d'entrée `imap-mcp` : serveur MCP (par défaut), `setup` et `check`."""
from __future__ import annotations

import argparse
import getpass
import imaplib
import re
import sys
from collections.abc import Sequence

import keyring
from keyring.errors import KeyringError

from . import imap
from .config import SERVICE, get_credentials, settings
from .errors import MailError
from .server import run as run_server


def setup_credentials() -> int:
    """Enregistre l'adresse et le mot de passe dans le trousseau système."""
    user = input("Adresse mail : ").strip()
    password = getpass.getpass("Mot de passe : ")
    if not user or not password:
        print("Adresse et mot de passe requis.", file=sys.stderr)
        return 1
    try:
        keyring.set_password(SERVICE, "user", user)
        keyring.set_password(SERVICE, user, password)
    except KeyringError as exc:
        print(f"Trousseau système indisponible : {exc}", file=sys.stderr)
        return 1
    print("Identifiants enregistrés dans le trousseau système.")
    return 0


def _folder_volume(conn: imap.Connection, folder: str) -> tuple[int, int]:
    """Nombre de messages et taille totale (octets) d'un dossier, sans lire le contenu."""
    count = imap.select(conn, folder)
    if not count:
        return 0, 0
    _, rows = conn.fetch(f"1:{count}", "(RFC822.SIZE)")
    total = 0
    for row in rows or []:
        match = re.search(rb"RFC822\.SIZE (\d+)",
                          row) if isinstance(row, bytes) else None
        if match:
            total += int(match[1])
    return count, total


def check_connection() -> int:
    """Teste la connexion et l'authentification en lecture seule."""
    try:
        user, _ = get_credentials()
        with imap.session() as conn:
            print(
                f"Connexion TLS et authentification OK : {user} ({settings.imap_host}:{settings.imap_port})")
            folders = imap.list_folders(conn)
            print(f"Dossiers ({len(folders)}) :")
            for folder in folders:
                print(f"   {folder.name}")
            print(
                f"INBOX : {imap.select(conn, 'INBOX')} messages (lecture seule)")
            drafts = imap.special_folder(conn, "drafts")
            count, size = _folder_volume(conn, drafts)
            print(f"{drafts} : {count} messages, {size / 1024:.1f} Ko au total")
    except (MailError, imaplib.IMAP4.error, OSError) as exc:
        print(f"Échec : {exc}", file=sys.stderr)
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="imap-mcp", description="Serveur MCP IMAP/SMTP.")
    commands = parser.add_subparsers(
        dest="command", metavar="{serve,setup,check}")
    commands.add_parser(
        "serve", help="lance le serveur MCP sur stdio (par défaut)")
    commands.add_parser(
        "setup", help="enregistre les identifiants dans le trousseau système")
    commands.add_parser(
        "check", help="teste la connexion IMAP en lecture seule")
    command = parser.parse_args(argv).command or "serve"
    if command == "setup":
        return setup_credentials()
    if command == "check":
        return check_connection()
    run_server()
    return 0
