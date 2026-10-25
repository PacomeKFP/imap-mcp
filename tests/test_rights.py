"""Rights resolution: defaults, layer precedence, folder matching, allowlists and rights files."""
from __future__ import annotations

import pytest

from imap_mcp import rights
from imap_mcp.errors import MailError
from imap_mcp.rights import ALLOW, ASK, DENY, Rule, RightsFile


def layers(session: RightsFile | None = None, local: RightsFile | None = None,
           global_: RightsFile | None = None) -> list[rights.Layer]:
    return [("session", session or RightsFile()), ("local", local or RightsFile()),
            ("global", global_ or RightsFile())]


def test_defaults_follow_the_table():
    assert rights.decide(layers(), "mail.read", "INBOX") == (ALLOW, "default")
    assert rights.decide(layers(), "mail.move", "INBOX") == (ASK, "default")
    assert rights.decide(layers(), "mail.create", rights.DRAFTS) == (ALLOW, "default")
    assert rights.decide(layers(), "mail.create", "INBOX") == (ASK, "default")
    assert rights.decide(layers(), "file.download", rights.DOWNLOADS) == (ALLOW, "default")
    assert rights.decide(layers(), "file.read", None) == (ASK, "default")


def test_session_beats_local_beats_global():
    deny = RightsFile([Rule("mail.move", DENY)])
    allow = RightsFile([Rule("mail.move", ALLOW)])
    ask = RightsFile([Rule("mail.move", ASK)])
    assert rights.decide(layers(global_=deny), "mail.move", "INBOX") == (DENY, "global")
    assert rights.decide(layers(local=allow, global_=deny), "mail.move", "INBOX") == (ALLOW, "local")
    assert rights.decide(layers(session=ask, local=allow), "mail.move", "INBOX") == (ASK, "session")


def test_folder_rules_cover_sub_folders_and_the_most_specific_rule_wins():
    local = RightsFile([Rule("mail.move", ALLOW, "Clients"), Rule("mail.move", DENY, "Clients/Archive")])
    assert rights.decide(layers(local=local), "mail.move", "Clients/2026") == (ALLOW, "local")
    assert rights.decide(layers(local=local), "mail.move", "Clients/Archive/2025") == (DENY, "local")
    assert rights.decide(layers(local=local), "mail.move", "Clientsbis") == (ASK, "default")


def test_same_level_most_restrictive_rule_wins():
    local = RightsFile([Rule("mail.move", ALLOW, "Clients"), Rule("mail.move", DENY, "Clients")])
    assert rights.decide(layers(local=local), "mail.move", "Clients") == (DENY, "local")


def test_folder_separator_is_respected():
    local = RightsFile([Rule("mail.move", ALLOW, "INBOX")])
    assert rights.decide(layers(local=local), "mail.move", "INBOX.Clients", ".") == (ALLOW, "local")
    assert rights.decide(layers(local=local), "mail.move", "INBOX.Clients", "/") == (ASK, "default")


def test_special_tokens_only_match_by_exact_name():
    local = RightsFile([Rule("mail.move", ALLOW, "@archive")])
    assert rights.decide(layers(local=local), "mail.move", rights.ARCHIVE) == (ALLOW, "local")
    assert rights.decide(layers(local=local), "mail.move", "@archive/x") == (ASK, "default")


def test_recipient_allowlist_matches_addresses_and_domains():
    assert rights.recipient_allowed(["@client.com"], "Anna@Client.com")
    assert not rights.recipient_allowed(["@client.com"], "x@client.com.evil.org")
    assert not rights.recipient_allowed(["@client.com"], "x@sub.client.com")
    assert rights.recipient_allowed(["bob@x.org"], "BOB@x.org")
    assert not rights.recipient_allowed([], "bob@x.org")


def test_rights_file_round_trip(tmp_path):
    path = tmp_path / "rights.toml"
    content = RightsFile([Rule("mail.move", ALLOW, 'Clients "é"'), Rule("file.read", ASK)],
                         ["@client.com", "bob@x.org"])
    rights.save(path, content)
    assert rights.load(path) == content


def test_missing_rights_file_means_no_rules(tmp_path):
    assert rights.load(tmp_path / "absent.toml") == RightsFile()


def test_invalid_rights_file_is_rejected(tmp_path):
    path = tmp_path / "rights.toml"
    path.write_text('[[rules]]\naction = "nope"\ndecision = "allow"\n', encoding="utf-8")
    with pytest.raises(MailError) as info:
        rights.load(path)
    assert info.value.code == "rights_invalid"
