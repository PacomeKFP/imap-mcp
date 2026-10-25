"""`imap-mcp rights` commands: allow, ask, deny, revoke, list, explain and prune rules."""

from __future__ import annotations

import argparse
import imaplib
import shutil
import sys
import time
from collections.abc import Callable
from pathlib import Path

from . import imap, rights
from .config import Settings
from .config import settings as default_settings
from .errors import MailError

LATEST = "latest"
SECONDS_PER_DAY = 86_400
# Plain strings: argparse shows choices in error messages, and enum reprs would be noisy.
ACTION_CHOICES = sorted(action.value for action in rights.Action)


def add_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the `rights` command and its subcommands on the main parser."""
    parser = commands.add_parser("rights", help="manage access rules and recipient allowlists")
    sub = parser.add_subparsers(dest="rights_command", metavar="COMMAND", required=True)
    for name, decision, text in (
        ("allow", rights.ALLOW, "allow an action without asking"),
        ("ask", rights.ASK, "ask the user before an action"),
        ("deny", rights.DENY, "deny an action"),
    ):
        item = sub.add_parser(name, help=text)
        item.set_defaults(func=_grant, decision=decision)
        _rule_arguments(item)
        _scope_arguments(item)
    item = sub.add_parser("revoke", help="remove a rule or recipient entry")
    item.set_defaults(func=_revoke)
    _rule_arguments(item)
    _scope_arguments(item)
    item = sub.add_parser("list", help="show rules and recipient allowlists")
    item.set_defaults(func=_list)
    _session_argument(item)
    item = sub.add_parser("explain", help="show the decision for an action on a folder")
    item.set_defaults(func=_explain)
    item.add_argument("action", choices=ACTION_CHOICES)
    item.add_argument("--folder")
    _session_argument(item)
    item = sub.add_parser("prune", help="delete old session folders; the newest one is kept")
    item.set_defaults(func=_prune)
    item.add_argument("--days", type=int, default=30, help="minimum age in days (default: 30)")


def run(args: argparse.Namespace, config: Settings | None = None) -> int:
    """Run the chosen rights subcommand and return its exit code."""
    handler: Callable[[argparse.Namespace, Settings], int] = args.func
    try:
        return handler(args, config if config is not None else default_settings)
    except (MailError, imaplib.IMAP4.error, OSError) as exc:
        print(f"Failed: {exc}", file=sys.stderr)
        return 1


def _rule_arguments(item: argparse.ArgumentParser) -> None:
    item.add_argument("action", choices=ACTION_CHOICES)
    item.add_argument("--folder", help="folder path; its sub-folders are included")
    item.add_argument(
        "--recipient",
        action="append",
        default=[],
        metavar="ADDRESS",
        help="address or @domain (mail.send only, repeatable)",
    )
    item.add_argument(
        "--file",
        metavar="PATH",
        help="text file with one address or @domain per line (mail.send only)",
    )


def _scope_arguments(item: argparse.ArgumentParser) -> None:
    group = item.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--global", dest="global_scope", action="store_true", help="user-wide, permanent"
    )
    group.add_argument(
        "--local", dest="local_scope", action="store_true", help="this project, permanent"
    )
    group.add_argument(
        "--session",
        nargs="?",
        const=LATEST,
        metavar="ID",
        help="this session only (ID defaults to the latest)",
    )


def _session_argument(item: argparse.ArgumentParser) -> None:
    item.add_argument(
        "--session",
        nargs="?",
        const=LATEST,
        metavar="ID",
        help="include a session (ID defaults to the latest)",
    )


def _target(args: argparse.Namespace, config: Settings) -> tuple[Path, str]:
    if args.global_scope:
        return rights.global_file(config), "global"
    if args.local_scope:
        return rights.local_file(config), "local"
    session_id = _session_id(config, args.session)
    return rights.session_file(config, session_id), f"session {session_id}"


def _session_id(config: Settings, value: str) -> str:
    if value == LATEST:
        session_id = rights.latest_session(config)
        if session_id is None:
            raise MailError(
                "No session folder yet: start the MCP server first, or pass --session ID.",
                code="no_session",
            )
        return session_id
    if not rights.session_dir(config, value).is_dir():
        raise MailError(f"Unknown session: {value}", code="no_session")
    return value


def _entries(args: argparse.Namespace) -> list[str]:
    return [_recipient_entry(item) for item in [*args.recipient, *_read_entries(args.file)]]


def _recipient_entry(entry: str) -> str:
    value = entry.strip().lower()
    if any(char.isspace() for char in value):
        valid = False
    elif value.startswith("@"):
        domain = value[1:]
        valid = bool(domain) and "@" not in domain
    else:
        local, separator, domain = value.partition("@")
        valid = bool(separator and local and domain) and "@" not in domain
    if not valid:
        raise MailError(
            f"Invalid recipient entry: {entry!r} (expected address or @domain)",
            code="invalid_argument",
        )
    return value


def _read_entries(path: str | None) -> list[str]:
    if not path:
        return []
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise MailError(f"Cannot read {path}: {exc}", code="invalid_argument") from exc
    return [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]


def _grant(args: argparse.Namespace, config: Settings) -> int:
    path, label = _target(args, config)
    content = rights.load(path)
    if args.recipient or args.file:
        if args.action != "mail.send" or args.decision != rights.ALLOW:
            raise MailError(
                "Recipient entries apply to 'rights allow mail.send' only.", code="invalid_argument"
            )
        entries = _entries(args)
        rights.add_recipients(content, entries)
        rights.save(path, content)
        print(f"{len(entries)} recipient entr(ies) allowed ({label}).")
        return 0
    rights.upsert_rule(content, rights.Rule(args.action, args.decision, args.folder))
    rights.save(path, content)
    print(f"{args.action} on {args.folder or 'all folders'}: {args.decision} ({label}).")
    return 0


def _revoke(args: argparse.Namespace, config: Settings) -> int:
    path, label = _target(args, config)
    content = rights.load(path)
    if args.recipient or args.file:
        removed = rights.remove_recipients(content, _entries(args))
        noun = "recipient entries"
    else:
        removed = rights.remove_rule(content, args.action, args.folder)
        noun = "rules"
    if removed:
        rights.save(path, content)
    print(f"{removed} {noun} removed ({label}).")
    return 0


def _list(args: argparse.Namespace, config: Settings) -> int:
    sections = [("global", rights.global_file(config)), ("local", rights.local_file(config))]
    if args.session is not None:
        session_id = _session_id(config, args.session)
        sections.append((f"session {session_id}", rights.session_file(config, session_id)))
    for label, path in sections:
        print(f"[{label}] {path}")
        _print_rights(rights.load(path))
    return 0


def _print_rights(content: rights.RightsFile) -> None:
    if not content.rules and not content.recipients:
        print("  (empty)")
    for rule in content.rules:
        print(f"  {rule.decision:<5} {rule.action}  folder={rule.folder or '*'}")
    for entry in content.recipients:
        print(f"  allow recipient {entry}")


def _explain(args: argparse.Namespace, config: Settings) -> int:
    session_id = _session_id(config, args.session) if args.session is not None else None
    layers = rights.load_layers(config, session_id)
    separator = imap.folder_separator() if args.folder else "/"
    decision, source = rights.decide(layers, args.action, args.folder, separator)
    print(f"{args.action} on {args.folder or 'all folders'}: {decision} (from {source}).")
    return 0


def _prune(args: argparse.Namespace, config: Settings) -> int:
    folder = rights.sessions_dir(config)
    if not folder.is_dir():
        print("0 session folder(s) removed.")
        return 0
    sessions = sorted(entry for entry in folder.iterdir() if entry.is_dir())
    cutoff = time.time() - args.days * SECONDS_PER_DAY
    removed = 0
    for entry in sessions[:-1]:  # the newest folder may belong to a running server
        if entry.stat().st_mtime < cutoff:
            shutil.rmtree(entry)
            removed += 1
    print(f"{removed} session folder(s) removed.")
    return 0
