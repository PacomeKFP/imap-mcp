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

from . import imap, rights_cli
from .config import SERVICE, get_credentials, settings
from .errors import MailError
from .tools import run as run_server


def setup_credentials() -> int:
    """Enregistre l'adresse et le mot de passe dans le trousseau système."""
    user = input("Email address: ").strip()
    password = getpass.getpass("Password: ")
    if not user or not password:
        print("Email address and password are required.", file=sys.stderr)
        return 1
    try:
        keyring.set_password(SERVICE, "user", user)
        keyring.set_password(SERVICE, user, password)
    except KeyringError as exc:
        print(f"System keyring unavailable: {exc}", file=sys.stderr)
        return 1
    print("Credentials saved in the system keyring.")
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
                f"TLS connection and authentication OK: {user} ({settings.imap_host}:{settings.imap_port})")
            folders = imap.list_folders(conn)
            print(f"Folders ({len(folders)}):")
            for folder in folders:
                print(f"   {folder.name}")
            print(
                f"INBOX: {imap.select(conn, 'INBOX')} messages (read-only)")
            drafts = imap.special_folder(conn, "drafts")
            count, size = _folder_volume(conn, drafts)
            print(f"{drafts}: {count} messages, {size / 1024:.1f} KB total")
            if not settings.read_only:
                imap.ensure_folder(conn, settings.agent_trash)
                print(f"Agent Trash ready: {settings.agent_trash}")
    except (MailError, imaplib.IMAP4.error, OSError) as exc:
        print(f"Failed: {exc}", file=sys.stderr)
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="imap-mcp", description="IMAP/SMTP MCP server.")
    commands = parser.add_subparsers(
        dest="command", metavar="{serve,setup,check,rights}")
    commands.add_parser(
        "serve", help="run the MCP server on stdio (default)")
    commands.add_parser(
        "setup", help="store credentials in the system keyring")
    commands.add_parser(
        "check", help="test the IMAP connection")
    rights_cli.add_parser(commands)
    args = parser.parse_args(argv)
    command = args.command or "serve"
    if command == "setup":
        return setup_credentials()
    if command == "check":
        return check_connection()
    if command == "rights":
        return rights_cli.run(args)
    run_server()
    return 0
