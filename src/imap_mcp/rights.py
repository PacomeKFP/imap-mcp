"""Access rights: rules, rights files and decisions. Reads and writes local files only."""
from __future__ import annotations

import json
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

from .config import AGENT_DIR, Settings
from .errors import MailError

ALLOW = "allow"
ASK = "ask"
DENY = "deny"
DECISIONS = frozenset({ALLOW, ASK, DENY})
_STRICTNESS = {DENY: 0, ASK: 1, ALLOW: 2}

ACTIONS = frozenset({
    "account.read",
    "folder.read", "folder.create", "folder.update",
    "mail.read", "mail.create", "mail.update", "mail.move", "mail.delete", "mail.send",
    "file.read", "file.download",
})
WRITE_ACTIONS = frozenset({
    "folder.create", "folder.update",
    "mail.create", "mail.update", "mail.move", "mail.delete", "mail.send",
})
_ALLOWED_BY_DEFAULT = frozenset({
    "account.read", "folder.read", "folder.create", "mail.read", "mail.update",
})

# Pseudo-targets for special folders and locations. Rules match them by exact name.
DRAFTS = "@drafts"
ARCHIVE = "@archive"
JUNK = "@junk"
DOWNLOADS = "@downloads"

RIGHTS_FILE = "rights.toml"
JOURNAL_FILE = "journal.jsonl"
SESSIONS = "sessions"


@dataclass(frozen=True)
class Rule:
    action: str
    decision: str
    folder: str | None = None  # None applies to every target


@dataclass
class RightsFile:
    rules: list[Rule] = field(default_factory=list)
    recipients: list[str] = field(default_factory=list)  # addresses or @domains allowed for mail.send


Layer = tuple[str, RightsFile]


def default_decision(action: str, target: str | None) -> str:
    if action in _ALLOWED_BY_DEFAULT:
        return ALLOW
    if action == "mail.create" and target == DRAFTS:
        return ALLOW
    if action == "file.download" and target == DOWNLOADS:
        return ALLOW
    return ASK


def decide(layers: Sequence[Layer], action: str, target: str | None, separator: str = "/") -> tuple[str, str]:
    """Return (decision, source) for one action on one target. The first layer with a match wins."""
    for source, content in layers:
        found = [rule for rule in content.rules
                 if rule.action == action and _matches(rule.folder, target, separator)]
        if found:
            best = max(_specificity(rule) for rule in found)
            decision = min((rule.decision for rule in found if _specificity(rule) == best),
                           key=_STRICTNESS.__getitem__)
            return decision, source
    return default_decision(action, target), "default"


def recipient_allowed(entries: Iterable[str], address: str) -> bool:
    address = address.lower()
    return any(address == entry.lower() or (entry.startswith("@") and address.endswith(entry.lower()))
               for entry in entries)


def _specificity(rule: Rule) -> int:
    return -1 if rule.folder is None else len(rule.folder)


def _matches(rule_folder: str | None, target: str | None, separator: str) -> bool:
    if rule_folder is None:
        return True
    if target is None:
        return False
    if target == rule_folder:
        return True
    if target.startswith("@") or rule_folder.startswith("@"):
        return False
    return target.startswith(rule_folder + separator)


def local_file(config: Settings) -> Path:
    return config.project_dir / AGENT_DIR / RIGHTS_FILE


def global_file(config: Settings) -> Path:
    return config.config_dir / RIGHTS_FILE


def sessions_dir(config: Settings) -> Path:
    return config.project_dir / AGENT_DIR / SESSIONS


def session_dir(config: Settings, session_id: str) -> Path:
    return sessions_dir(config) / session_id


def session_file(config: Settings, session_id: str) -> Path:
    return session_dir(config, session_id) / RIGHTS_FILE


def latest_session(config: Settings) -> str | None:
    folder = sessions_dir(config)
    names = sorted(entry.name for entry in folder.iterdir() if entry.is_dir()) if folder.is_dir() else []
    return names[-1] if names else None


def load(path: Path) -> RightsFile:
    """Read a rights file. A missing file holds no rules; an invalid one stops the call."""
    if not path.is_file():
        return RightsFile()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MailError(f"Cannot read rights file {path}: {exc}", code="rights_invalid") from exc
    rules = [_parse_rule(item, path) for item in _tables(data, "rules", path)]
    recipients = [_parse_entry(item, path) for item in _tables(data, "recipients", path)]
    return RightsFile(rules, recipients)


def save(path: Path, content: RightsFile) -> None:
    lines = ["version = 1", ""]
    for rule in content.rules:
        lines += ["[[rules]]", f"action = {_text(rule.action)}"]
        if rule.folder is not None:
            lines.append(f"folder = {_text(rule.folder)}")
        lines += [f"decision = {_text(rule.decision)}", ""]
    for entry in content.recipients:
        lines += ["[[recipients]]", f"match = {_text(entry)}", ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text("\n".join(lines), encoding="utf-8")
    os.replace(temporary, path)


def upsert_rule(content: RightsFile, rule: Rule) -> None:
    """Replace the rule for the same action and folder, or add it."""
    content.rules = [item for item in content.rules
                     if (item.action, item.folder) != (rule.action, rule.folder)]
    content.rules.append(rule)


def remove_rule(content: RightsFile, action: str, folder: str | None) -> int:
    before = len(content.rules)
    content.rules = [item for item in content.rules if (item.action, item.folder) != (action, folder)]
    return before - len(content.rules)


def add_recipients(content: RightsFile, entries: Iterable[str]) -> None:
    for entry in entries:
        if entry not in content.recipients:
            content.recipients.append(entry)


def remove_recipients(content: RightsFile, entries: Iterable[str]) -> int:
    wanted = set(entries)
    before = len(content.recipients)
    content.recipients = [entry for entry in content.recipients if entry not in wanted]
    return before - len(content.recipients)


def _text(value: str) -> str:
    """TOML basic string. JSON escaping is valid TOML for these values."""
    return json.dumps(value, ensure_ascii=False)


def _tables(data: dict[str, Any], key: str, path: Path) -> list[dict[str, Any]]:
    tables = data.get(key, [])
    if not isinstance(tables, list) or not all(isinstance(item, dict) for item in tables):
        raise MailError(f"'{key}' must be a list of tables in {path}", code="rights_invalid")
    return tables


def _parse_rule(item: dict[str, Any], path: Path) -> Rule:
    action, decision, folder = item.get("action"), item.get("decision"), item.get("folder")
    valid = (isinstance(action, str) and action in ACTIONS
             and isinstance(decision, str) and decision in DECISIONS
             and (folder is None or (isinstance(folder, str) and folder != "")))
    if not valid:
        raise MailError(f"Invalid rule in {path}: {item}", code="rights_invalid")
    return Rule(action, decision, folder)


def _parse_entry(item: dict[str, Any], path: Path) -> str:
    match = item.get("match")
    if not isinstance(match, str) or not match:
        raise MailError(f"Invalid recipient entry in {path}: {item}", code="rights_invalid")
    return match
