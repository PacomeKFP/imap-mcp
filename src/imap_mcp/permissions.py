"""Runtime guard: resolves rights before each tool, asks when needed, and journals decisions."""
from __future__ import annotations

import secrets
from collections.abc import Sequence
from datetime import datetime

from mcp.server.mcpserver import Context

from . import imap, journal, rights
from .app import in_thread
from .approval import ask_user, describe_targets
from .config import Settings, settings
from .errors import MailError


def _allowlisted(action: str, recipients: Sequence[str], layers: list[rights.Layer]) -> bool:
    """True when every recipient of a mail.send is already allowlisted in some layer."""
    if action != "mail.send" or not recipients:
        return False
    allowlist = [entry for _, content in layers for entry in content.recipients]
    return all(rights.recipient_allowed(allowlist, address) for address in recipients)


def new_session_id() -> str:
    return f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


class Guard:
    """Checks rights before each tool call. One instance per server process."""

    def __init__(self, config: Settings, session_id: str) -> None:
        self.config = config
        self.session_id = session_id
        self.separator: str | None = None

    def layers(self) -> list[rights.Layer]:
        return rights.load_layers(self.config, self.session_id)

    async def authorize(self, ctx: Context, action: str, *, targets: Sequence[str | None] = (),
                        recipients: Sequence[str] = (), detail: str = "") -> None:
        """Return if the action may run; else raise MailError. Asks the user if needed."""
        wanted: list[str | None] = list(targets) or [None]
        if self.config.read_only and action in rights.WRITE_ACTIONS:
            targets = [(t, rights.DENY, "config") for t in wanted]
            self._record(action, targets, recipients, "read_only")
            raise MailError(f"{action} is blocked: the server is read-only (MAIL_READONLY=1).",
                            code="read_only")
        verdicts, layers = await self._verdicts(action, wanted)
        if any(decision == rights.DENY for _, decision, _ in verdicts):
            self._record(action, verdicts, recipients, "denied")
            denied = describe_targets([target for target, _, _ in verdicts])
            raise MailError(f"{action} is denied for {denied}.", code="denied")
        asks = [target for target, decision, _ in verdicts if decision == rights.ASK]
        if not asks:
            self._record(action, verdicts, recipients, "allowed")
            return
        if _allowlisted(action, recipients, layers):
            self._record(action, verdicts, recipients, "allowed_by_allowlist")
            return
        choice = await ask_user(ctx, action, asks, recipients, detail)
        if choice is None:
            self._record(action, verdicts, recipients, "permission_required")
            raise MailError(f"{action} needs approval and this client cannot ask. "
                            f"Ask the user, then run: {_hint(action)}", code="permission_required")
        if choice == "refuse":
            self._record(action, verdicts, recipients, "refused")
            raise MailError(f"{action} was refused.", code="refused")
        if choice == "session":
            self._remember(action, asks, recipients)
        self._record(action, verdicts, recipients, choice)

    async def _verdicts(
            self, action: str, wanted: list[str | None]
    ) -> tuple[list[rights.Verdict], list[rights.Layer]]:
        """Resolve the folder separator once, then the decision for each target."""
        if self.separator is None and any(t is not None and not t.startswith("@") for t in wanted):
            self.separator = await in_thread(imap.folder_separator)
        layers = self.layers()
        verdicts = [(target, *rights.decide(layers, action, target, self.separator or "/"))
                    for target in wanted]
        return verdicts, layers

    def _remember(self, action: str, asks: list[str | None], recipients: Sequence[str]) -> None:
        path = rights.session_file(self.config, self.session_id)
        content = rights.load(path)
        if action == "mail.send":
            rights.add_recipients(content, recipients)
        else:
            for target in asks:
                rights.upsert_rule(content, rights.Rule(action, rights.ALLOW, target))
        rights.save(path, content)

    def _record(self, action: str, verdicts: list[rights.Verdict], recipients: Sequence[str],
                outcome: str) -> None:
        journal.record(self.config, self.session_id, action,
                       verdicts, recipients, outcome)


def _hint(action: str) -> str:
    if action == "mail.send":
        return "imap-mcp rights allow mail.send --recipient ADDRESS --session"
    return f"imap-mcp rights allow {action} [--folder FOLDER] --session"


SESSION_ID = new_session_id()
guard = Guard(settings, SESSION_ID)
