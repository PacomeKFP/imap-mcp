"""Access rights: rules, rights files and decisions. Reads and writes local files only."""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, NamedTuple

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from .config import AGENT_DIR, Settings
from .errors import MailError


class Action(str, Enum):
    """Every action a tool can ask the guard to authorize. Compares equal to its value."""

    ACCOUNT_READ = "account.read"
    FOLDER_READ = "folder.read"
    FOLDER_CREATE = "folder.create"
    FOLDER_UPDATE = "folder.update"
    MAIL_READ = "mail.read"
    MAIL_CREATE = "mail.create"
    MAIL_UPDATE = "mail.update"
    MAIL_MOVE = "mail.move"
    MAIL_DELETE = "mail.delete"
    MAIL_SEND = "mail.send"
    FILE_READ = "file.read"
    FILE_DOWNLOAD = "file.download"

    def __str__(self) -> str:
        return self.value


class Decision(str, Enum):
    """The verdict for an action: allowed, asked for approval, or denied."""

    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"

    def __str__(self) -> str:
        return self.value


ALLOW = Decision.ALLOW
ASK = Decision.ASK
DENY = Decision.DENY
DECISIONS = frozenset(Decision)
_STRICTNESS: dict[Decision, int] = {Decision.DENY: 0, Decision.ASK: 1, Decision.ALLOW: 2}

ACTIONS = frozenset(Action)
_ALLOWED_BY_DEFAULT = frozenset(
    {
        Action.ACCOUNT_READ,
        Action.FOLDER_READ,
        Action.FOLDER_CREATE,
        Action.MAIL_READ,
        Action.MAIL_UPDATE,
    }
)

# Pseudo-targets for special folders and locations. Rules match them by exact name.
DRAFTS = "@drafts"
ARCHIVE = "@archive"
JUNK = "@junk"
DOWNLOADS = "@downloads"

RIGHTS_FILE = "rights.toml"
JOURNAL_FILE = "journal.jsonl"
SESSIONS = "sessions"
ACTIVE_FILE = "active"  # marker whose mtime is refreshed on every guarded call


@dataclass(frozen=True, init=False)
class Rule:
    """One rule: a decision for an action on a folder, or on every target when folder is None.
    Plain strings are converted to the enum members."""

    action: Action
    decision: Decision
    folder: str | None = None  # None applies to every target

    def __init__(
        self,
        action: Action | str,
        decision: Decision | str,
        folder: str | None = None,
    ) -> None:
        object.__setattr__(self, "action", Action(action))
        object.__setattr__(self, "decision", Decision(decision))
        object.__setattr__(self, "folder", folder)


@dataclass
class RightsFile:
    """The rules and mail.send recipients stored in one rights file."""

    rules: list[Rule] = field(default_factory=list)
    # Addresses or @domains allowed for mail.send.
    recipients: list[str] = field(default_factory=list)


class Layer(NamedTuple):
    """A named rights file in precedence order: session, local or global."""

    source: str
    content: RightsFile


class Verdict(NamedTuple):
    """The outcome of one check: the target, its decision and the layer that decided it."""

    target: str | None
    decision: Decision
    source: str


_ALLOWED_ON_TARGET = frozenset({(Action.MAIL_CREATE, DRAFTS), (Action.FILE_DOWNLOAD, DOWNLOADS)})


def default_decision(action: Action | str, target: str | None) -> Decision:
    """The decision when no rule matches: reads and drafts are allowed, everything else asks."""
    if action in _ALLOWED_BY_DEFAULT or (action, target) in _ALLOWED_ON_TARGET:
        return ALLOW
    return ASK


def decide(
    layers: Sequence[Layer], action: Action | str, target: str | None, separator: str = "/"
) -> tuple[Decision, str]:
    """Return (decision, source) for one action on one target.
    The first layer with a match wins."""
    for source, content in layers:
        found = [
            rule
            for rule in content.rules
            if rule.action == action and _matches(rule.folder, target, separator)
        ]
        if found:
            best = max(_specificity(rule) for rule in found)
            decision = min(
                (rule.decision for rule in found if _specificity(rule) == best),
                key=_STRICTNESS.__getitem__,
            )
            return decision, source
    return default_decision(action, target), "default"


def strictest_decision(results: Iterable[tuple[Decision, str]]) -> tuple[Decision, str]:
    """The (decision, source) pair with the strictest decision. The first one wins ties."""
    return min(results, key=lambda result: _STRICTNESS[result[0]])


def load_layers(config: Settings, session_id: str | None) -> list[Layer]:
    """Rights of each layer in precedence order: session (when given), local, global."""
    layers: list[Layer] = []
    if session_id is not None:
        layers.append(Layer("session", load(session_file(config, session_id))))
    layers.append(Layer("local", load(local_file(config))))
    layers.append(Layer("global", load(global_file(config))))
    return layers


def recipient_allowed(entries: Iterable[str], address: str) -> bool:
    """True when the address matches an entry exactly or through an @domain entry."""
    address = address.lower()
    return any(
        address == entry.lower() or (entry.startswith("@") and address.endswith(entry.lower()))
        for entry in entries
    )


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
    """The project's rights file."""
    return config.project_dir / AGENT_DIR / RIGHTS_FILE


def global_file(config: Settings) -> Path:
    """The user-wide rights file."""
    return config.config_dir / RIGHTS_FILE


def sessions_dir(config: Settings) -> Path:
    """Folder holding one directory per session."""
    return config.project_dir / AGENT_DIR / SESSIONS


def session_dir(config: Settings, session_id: str) -> Path:
    """Directory of one session."""
    return sessions_dir(config) / session_id


def session_file(config: Settings, session_id: str) -> Path:
    """Rights file of one session."""
    return session_dir(config, session_id) / RIGHTS_FILE


def touch_session(config: Settings, session_id: str) -> None:
    """Create the session folder and refresh its active marker. Safe to call repeatedly."""
    folder = session_dir(config, session_id)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / ACTIVE_FILE).touch(exist_ok=True)


def session_activity(folder: Path) -> float:
    """Latest modification time of a session's marker, journal or rights file.

    The folder's own mtime is only used when none of those files exist.
    """
    times = [
        mtime
        for mtime in (_mtime(folder / name) for name in (ACTIVE_FILE, JOURNAL_FILE, RIGHTS_FILE))
        if mtime is not None
    ]
    return max(times) if times else folder.stat().st_mtime


def _mtime(path: Path) -> float | None:
    """Modification time of a file, or None when it does not exist."""
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def session_ids(config: Settings) -> list[str]:
    """Session ids from least to most recently active; the id breaks ties."""
    folder = sessions_dir(config)
    if not folder.is_dir():
        return []
    entries = sorted(
        (session_activity(entry), entry.name) for entry in folder.iterdir() if entry.is_dir()
    )
    return [name for _, name in entries]


def latest_session(config: Settings) -> str | None:
    """Id of the session with the most recent activity, or None when there is none."""
    ids = session_ids(config)
    return ids[-1] if ids else None


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
    """Write a rights file atomically: a temporary file is written, then renamed over the target."""
    lines = ["version = 1", ""]
    for rule in content.rules:
        lines += ["[[rules]]", f"action = {_text(rule.action.value)}"]
        if rule.folder is not None:
            lines.append(f"folder = {_text(rule.folder)}")
        lines += [f"decision = {_text(rule.decision.value)}", ""]
    for entry in content.recipients:
        lines += ["[[recipients]]", f"match = {_text(entry)}", ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    try:
        temporary.write_text("\n".join(lines), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def upsert_rule(content: RightsFile, rule: Rule) -> None:
    """Replace the rule for the same action and folder, or add it."""
    content.rules = [
        item for item in content.rules if (item.action, item.folder) != (rule.action, rule.folder)
    ]
    content.rules.append(rule)


def remove_rule(content: RightsFile, action: Action | str, folder: str | None) -> int:
    """Remove the rule for an action and folder. Returns how many were removed."""
    before = len(content.rules)
    content.rules = [
        item for item in content.rules if (item.action, item.folder) != (action, folder)
    ]
    return before - len(content.rules)


def add_recipients(content: RightsFile, entries: Iterable[str]) -> None:
    """Add mail.send recipients, skipping the ones already present."""
    for entry in entries:
        if entry not in content.recipients:
            content.recipients.append(entry)


def remove_recipients(content: RightsFile, entries: Iterable[str]) -> int:
    """Remove mail.send recipients. Returns how many were removed."""
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
    if (
        isinstance(action, str)
        and action in ACTIONS
        and isinstance(decision, str)
        and decision in DECISIONS
        and (folder is None or (isinstance(folder, str) and folder != ""))
    ):
        return Rule(action, decision, folder)
    raise MailError(f"Invalid rule in {path}: {item}", code="rights_invalid")


def _parse_entry(item: dict[str, Any], path: Path) -> str:
    match = item.get("match")
    if not isinstance(match, str) or not match:
        raise MailError(f"Invalid recipient entry in {path}: {item}", code="rights_invalid")
    return match
