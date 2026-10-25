"""`imap-mcp rights` CLI: scopes, recipient allowlists and error handling, without IMAP access."""
from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path

from imap_mcp import rights, rights_cli
from imap_mcp.config import Settings

SESSION = "20260101-000000-aaaa"


def make_settings(root: Path) -> Settings:
    return Settings(
        imap_host="imap.test", imap_port=993, smtp_host="smtp.test", smtp_port=587,
        read_only=False, download_dir=root / "downloads", max_body=1000,
        agent_trash="Agent Trash", project_dir=root / "project", config_dir=root / "config",
    )


def parse(*argv: str):
    parser = ArgumentParser(prog="imap-mcp")
    rights_cli.add_parser(parser.add_subparsers(dest="command"))
    return parser.parse_args(["rights", *argv])


def test_allow_local_rule_then_revoke(tmp_path):
    config = make_settings(tmp_path)
    assert rights_cli.run(parse("allow", "mail.move", "--folder", "Clients", "--local"), config) == 0
    assert rights.load(rights.local_file(config)).rules == [rights.Rule("mail.move", "allow", "Clients")]
    assert rights_cli.run(parse("revoke", "mail.move", "--folder", "Clients", "--local"), config) == 0
    assert rights.load(rights.local_file(config)).rules == []


def test_session_scope_needs_an_existing_session(tmp_path):
    config = make_settings(tmp_path)
    args = parse("allow", "mail.send", "--recipient", "@client.com", "--session")
    assert rights_cli.run(args, config) == 1  # no session folder yet
    (rights.sessions_dir(config) / SESSION).mkdir(parents=True)
    assert rights_cli.run(args, config) == 0
    assert rights.load(rights.session_file(config, SESSION)).recipients == ["@client.com"]


def test_recipients_only_apply_to_allow_mail_send(tmp_path):
    config = make_settings(tmp_path)
    args = parse("ask", "mail.move", "--recipient", "x@y.org", "--local")
    assert rights_cli.run(args, config) == 1
    assert not rights.local_file(config).exists()
