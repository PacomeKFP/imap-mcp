"""Entry point `imap-mcp`: MCP server (default), `setup`, `check` and `rights`."""

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
from .config import ACCOUNT_KEY, HOST_SUFFIX, SERVICE, SMTP_SUFFIX, get_credentials, get_settings
from .errors import ConfigError, MailError
from .tools import run as run_server


def setup_account() -> int:
    """Ask for the account and servers, store them in the keyring, then verify the connection."""
    user = input("Email address: ").strip()
    if "@" not in user:
        print("A valid email address is required.", file=sys.stderr)
        return 1
    domain = user.rsplit("@", 1)[1]
    imap_host = input(f"IMAP server [imap.{domain}]: ").strip() or f"imap.{domain}"
    smtp_host = input(f"SMTP server [smtp.{domain}]: ").strip() or f"smtp.{domain}"
    password = getpass.getpass("Password: ")
    if not password:
        print("A password is required.", file=sys.stderr)
        return 1
    try:
        keyring.set_password(SERVICE, ACCOUNT_KEY, user)
        keyring.set_password(SERVICE, user, password)
        keyring.set_password(SERVICE, f"{user}{HOST_SUFFIX}", imap_host)
        keyring.set_password(SERVICE, f"{user}{SMTP_SUFFIX}", smtp_host)
    except KeyringError as exc:
        print(f"System keyring unavailable: {exc}", file=sys.stderr)
        return 1
    print("Credentials saved in the system keyring. Checking the connection...")
    get_settings.cache_clear()
    get_credentials.cache_clear()
    return check_connection()


def _folder_volume(conn: imap.Connection, folder: str) -> tuple[int, int]:
    """Message count and total size in bytes of a folder, without reading the content."""
    count = imap.select(conn, folder)
    if not count:
        return 0, 0
    _, rows = conn.fetch(f"1:{count}", "(RFC822.SIZE)")
    total = 0
    for row in rows or []:
        match = re.search(rb"RFC822\.SIZE (\d+)", row) if isinstance(row, bytes) else None
        if match:
            total += int(match[1])
    return count, total


def _print_folders(conn: imap.Connection) -> None:
    """Print the folder list, the INBOX count and the volume of the Drafts folder."""
    folders = imap.list_folders(conn)
    print(f"Folders ({len(folders)}):")
    for folder in folders:
        print(f"   {folder.name}")
    print(f"INBOX: {imap.select(conn, 'INBOX')} messages (read-only)")
    drafts = imap.special_folder(conn, "drafts")
    count, size = _folder_volume(conn, drafts)
    print(f"{drafts}: {count} messages, {size / 1024:.1f} KB total")


def check_connection() -> int:
    """Check connection and authentication, then create the Agent Trash folder if missing."""
    try:
        settings = get_settings()
        user, _ = get_credentials()
        with imap.session() as conn:
            print(
                f"TLS connection and authentication OK: {user} "
                f"({settings.imap_host}:{settings.imap_port})"
            )
            _print_folders(conn)
            imap.ensure_folder(conn, settings.agent_trash)
            print(f"Agent Trash ready: {settings.agent_trash}")
    except (MailError, imaplib.IMAP4.error, OSError) as exc:
        print(f"Failed: {exc}", file=sys.stderr)
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="imap-mcp", description="IMAP/SMTP MCP server.")
    commands = parser.add_subparsers(dest="command", metavar="{serve,setup,check,rights}")
    commands.add_parser("serve", help="run the MCP server on stdio (default)")
    commands.add_parser("setup", help="configure the account, then verify the connection")
    commands.add_parser("check", help="test the IMAP connection")
    rights_cli.add_parser(commands)
    args = parser.parse_args(argv)
    command = args.command or "serve"
    try:
        if command == "setup":
            return setup_account()
        if command == "check":
            return check_connection()
        if command == "rights":
            return rights_cli.run(args)
        get_settings()  # fail fast on an invalid environment, before serving
    except ConfigError as exc:
        print(f"Failed: {exc}", file=sys.stderr)
        return 1
    run_server()
    return 0
