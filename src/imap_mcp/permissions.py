"""Runtime guard: resolves rights before each tool, asks when needed, and journals decisions."""

from __future__ import annotations

import imaplib
import secrets
import sys
from collections.abc import Mapping, Sequence
from contextlib import suppress
from datetime import datetime, timezone

from mcp.server.mcpserver import Context

from . import imap, journal, rights
from .app import in_thread
from .approval import ask_user, describe_targets
from .config import Settings, settings
from .errors import MailError

# Pseudo-target aliases of the special folders, keyed to their IMAP role.
ROLE_ALIASES: dict[str, str] = {
    rights.DRAFTS: "drafts",
    rights.ARCHIVE: "archive",
    rights.JUNK: "junk",
}


def _allowlisted(
    action: rights.Action | str, recipients: Sequence[str], layers: list[rights.Layer]
) -> bool:
    """True when every recipient of a mail.send is already allowlisted in some layer."""
    if action != rights.Action.MAIL_SEND or not recipients:
        return False
    allowlist = [entry for layer in layers for entry in layer.content.recipients]
    return all(rights.recipient_allowed(allowlist, address) for address in recipients)


def new_session_id() -> str:
    """A new session id: a UTC timestamp plus a random suffix, so ids sort by start time."""
    return f"{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


def counterparts(target: str | None, roles: Mapping[str, str]) -> list[str | None]:
    """The target and its equivalent: a real folder name maps to its alias, and an alias to its folder."""
    if target is None:
        return [None]
    names: list[str | None] = [target]
    if target in roles:
        names.append(roles[target])
    names.extend(alias for alias, name in roles.items() if name == target)
    return list(dict.fromkeys(names))


def effective_decision(
    layers: Sequence[rights.Layer],
    action: rights.Action | str,
    target: str | None,
    roles: Mapping[str, str],
    separator: str,
) -> tuple[rights.Decision, str]:
    """Strictest explicit decision over the target and its counterparts, else the target's default."""
    results = [
        rights.decide(layers, action, name, separator) for name in counterparts(target, roles)
    ]
    explicit = [result for result in results if result[1] != "default"]
    if not explicit:
        return rights.decide(layers, action, target, separator)
    return rights.strictest_decision(explicit)


def _names_folder(wanted: Sequence[str | None]) -> bool:
    """True when a target is a real folder or a special-folder alias (not None or @downloads)."""
    return any(
        target is not None and (target in ROLE_ALIASES or not target.startswith("@"))
        for target in wanted
    )


def _special_folders() -> dict[str, str]:
    """Real names of the special folders keyed by alias. Uses one IMAP session."""
    names: dict[str, str] = {}
    with imap.session() as conn:
        for alias, role in ROLE_ALIASES.items():
            with suppress(MailError):
                names[alias] = imap.special_folder(conn, role)
    return names


class Guard:
    """Checks rights before each tool call. One instance per server process."""

    def __init__(self, config: Settings, session_id: str) -> None:
        self.config = config
        self.session_id = session_id
        self.separator: str | None = None
        self.roles: dict[str, str] | None = None  # alias -> real folder name, resolved once

    def layers(self) -> list[rights.Layer]:
        """Rights of every layer in precedence order, read fresh from disk."""
        return rights.load_layers(self.config, self.session_id)

    def start(self) -> None:
        """Register this session: create its folder and refresh the active marker. Idempotent."""
        rights.touch_session(self.config, self.session_id)

    async def authorize(
        self,
        ctx: Context,
        action: rights.Action | str,
        *,
        targets: Sequence[str | None] = (),
        recipients: Sequence[str] = (),
        detail: str = "",
    ) -> None:
        """Return if the action may run; else raise MailError. Asks the user if needed."""
        self._heartbeat()
        wanted: list[str | None] = list(targets) or [None]
        verdicts, layers = await self._verdicts(action, wanted)
        if any(verdict.decision == rights.DENY for verdict in verdicts):
            self._record(action, verdicts, recipients, "denied")
            denied = describe_targets([verdict.target for verdict in verdicts])
            raise MailError(f"{action} is denied for {denied}.", code="denied")
        asks = [verdict.target for verdict in verdicts if verdict.decision == rights.ASK]
        if not asks:
            self._record(action, verdicts, recipients, "allowed")
            return
        if _allowlisted(action, recipients, layers):
            self._record(action, verdicts, recipients, "allowed_by_allowlist")
            return
        choice = await ask_user(ctx, action, asks, recipients, detail)
        if choice is None:
            self._record(action, verdicts, recipients, "permission_required")
            raise MailError(
                f"{action} needs approval and this client cannot ask. "
                f"Ask the user, then run: {_hint(action, self.session_id)}",
                code="permission_required",
            )
        if choice == "refuse":
            self._record(action, verdicts, recipients, "refused")
            raise MailError(f"{action} was refused.", code="refused")
        if choice == "session":
            self._remember(action, asks, recipients)
        self._record(action, verdicts, recipients, choice)

    def _heartbeat(self) -> None:
        """Keep the session marked active. A failure is reported but never blocks the call."""
        try:
            self.start()
        except OSError as exc:
            print(f"imap-mcp: cannot mark session {self.session_id} active: {exc}", file=sys.stderr)

    async def _verdicts(
        self, action: rights.Action | str, wanted: list[str | None]
    ) -> tuple[list[rights.Verdict], list[rights.Layer]]:
        """Resolve the folder separator once, then the decision for each target."""
        if self.separator is None and any(t is not None and not t.startswith("@") for t in wanted):
            self.separator = await self._resolve_separator()
        layers = self.layers()
        roles = await self._roles(wanted)
        separator = self.separator or "/"
        verdicts = [
            rights.Verdict(target, *effective_decision(layers, action, target, roles, separator))
            for target in wanted
        ]
        return verdicts, layers

    async def _resolve_separator(self) -> str | None:
        """The server's folder separator. On failure, None: '/' is used and the lookup is retried."""
        try:
            return await in_thread(imap.folder_separator)
        except (MailError, OSError, imaplib.IMAP4.error) as exc:
            print(
                f"imap-mcp: cannot resolve the folder separator, assuming '/': {exc}",
                file=sys.stderr,
            )
            return None

    async def _roles(self, wanted: Sequence[str | None]) -> dict[str, str]:
        """Alias to real folder name. Failures fall back to no aliases and are retried later."""
        if self.roles is None and _names_folder(wanted):
            try:
                self.roles = await in_thread(_special_folders)
            except (MailError, OSError, imaplib.IMAP4.error):
                return {}
        return self.roles or {}

    def _remember(
        self, action: rights.Action | str, asks: list[str | None], recipients: Sequence[str]
    ) -> None:
        path = rights.session_file(self.config, self.session_id)
        content = rights.load(path)
        if action == rights.Action.MAIL_SEND:
            rights.add_recipients(content, recipients)
        else:
            for target in asks:
                rights.upsert_rule(content, rights.Rule(action, rights.ALLOW, target))
        rights.save(path, content)

    def _record(
        self,
        action: rights.Action | str,
        verdicts: list[rights.Verdict],
        recipients: Sequence[str],
        outcome: str,
    ) -> None:
        journal.record(self.config, self.session_id, action, verdicts, recipients, outcome)


def _hint(action: rights.Action | str, session_id: str) -> str:
    """The rights command that grants the action for this session, quoting the session id."""
    if action == rights.Action.MAIL_SEND:
        return f"imap-mcp rights allow mail.send --recipient ADDRESS --session {session_id}"
    return f"imap-mcp rights allow {action} [--folder FOLDER] --session {session_id}"


SESSION_ID = new_session_id()
guard = Guard(settings, SESSION_ID)
