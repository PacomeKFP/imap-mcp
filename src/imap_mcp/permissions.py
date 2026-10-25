"""Runtime guard: resolves rights before each tool runs, asks the user when needed, and journals decisions."""
from __future__ import annotations

import json
import secrets
from collections.abc import Sequence
from datetime import datetime
from typing import Literal

from mcp.server.mcpserver import Context
from pydantic import BaseModel

from . import imap, rights
from .app import in_thread
from .config import Settings, settings
from .errors import MailError

READ_ACTIONS = frozenset({"account.read", "folder.read", "mail.read"})
_LISTED_RECIPIENTS = 10

Verdict = tuple[str | None, str, str]  # target, decision, source


class _Choice(BaseModel):
    choice: Literal["once", "session", "refuse"]


def new_session_id() -> str:
    return f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


class Guard:
    """Checks rights before each tool call. One instance per server process."""

    def __init__(self, config: Settings, session_id: str) -> None:
        self.config = config
        self.session_id = session_id
        self.separator: str | None = None

    def layers(self) -> list[rights.Layer]:
        return [
            ("session", rights.load(rights.session_file(self.config, self.session_id))),
            ("local", rights.load(rights.local_file(self.config))),
            ("global", rights.load(rights.global_file(self.config))),
        ]

    async def authorize(self, ctx: Context, action: str, *, targets: Sequence[str | None] = (),
                        recipients: Sequence[str] = (), detail: str = "") -> None:
        """Return if the action may run; raise MailError otherwise. Asks the user when the rights require it."""
        wanted: list[str | None] = list(targets) or [None]
        if self.config.read_only and action in rights.WRITE_ACTIONS:
            self._journal(action, [(t, rights.DENY, "config") for t in wanted], recipients, "read_only")
            raise MailError(f"{action} is blocked: the server is read-only (MAIL_READONLY=1).",
                            code="read_only")
        if self.separator is None and any(t is not None and not t.startswith("@") for t in wanted):
            self.separator = await in_thread(imap.folder_separator)
        layers = self.layers()
        verdicts: list[Verdict] = [
            (target, *rights.decide(layers, action, target, self.separator or "/")) for target in wanted
        ]
        if any(decision == rights.DENY for _, decision, _ in verdicts):
            self._journal(action, verdicts, recipients, "denied")
            raise MailError(f"{action} is denied for {_label_targets([t for t, _, _ in verdicts])}.",
                            code="denied")
        asks = [target for target, decision, _ in verdicts if decision == rights.ASK]
        if not asks:
            self._journal(action, verdicts, recipients, "allowed")
            return
        allowlist = [entry for _, content in layers for entry in content.recipients]
        if action == "mail.send" and recipients and all(
                rights.recipient_allowed(allowlist, address) for address in recipients):
            self._journal(action, verdicts, recipients, "allowed_by_allowlist")
            return
        choice = await self._ask(ctx, action, asks, recipients, detail)
        if choice is None:
            self._journal(action, verdicts, recipients, "permission_required")
            raise MailError(f"{action} needs approval and this client cannot ask. "
                            f"Ask the user, then run: {_hint(action)}", code="permission_required")
        if choice == "refuse":
            self._journal(action, verdicts, recipients, "refused")
            raise MailError(f"{action} was refused.", code="refused")
        if choice == "session":
            self._remember(action, asks, recipients)
        self._journal(action, verdicts, recipients, choice)

    async def _ask(self, ctx: Context, action: str, asks: list[str | None],
                   recipients: Sequence[str], detail: str) -> str | None:
        """Return 'once', 'session' or 'refuse'; None when the client cannot ask."""
        if action == "mail.send" and recipients:
            shown = ", ".join(recipients[:_LISTED_RECIPIENTS])
            if len(recipients) > _LISTED_RECIPIENTS:
                shown += f" and {len(recipients) - _LISTED_RECIPIENTS} more"
            subject = f"sending mail to {shown}"
        else:
            subject = f"{action} on {_label_targets(asks)}"
        suffix = f" ({detail})" if detail else ""
        message = f"Approval needed: {subject}{suffix}. Choose once, session (this session only), or refuse."
        try:
            result = await ctx.elicit(message, _Choice)
        except Exception:  # the client does not support elicitation
            return None
        return result.data.choice if result.action == "accept" else "refuse"

    def _remember(self, action: str, asks: list[str | None], recipients: Sequence[str]) -> None:
        path = rights.session_file(self.config, self.session_id)
        content = rights.load(path)
        if action == "mail.send":
            rights.add_recipients(content, recipients)
        else:
            for target in asks:
                rights.upsert_rule(content, rights.Rule(action, rights.ALLOW, target))
        rights.save(path, content)

    def _journal(self, action: str, verdicts: list[Verdict], recipients: Sequence[str], outcome: str) -> None:
        if action in READ_ACTIONS and outcome == "allowed":
            return
        entry: dict[str, object] = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "session": self.session_id,
            "action": action,
            "verdicts": [{"target": t, "decision": d, "source": s} for t, d, s in verdicts],
            "outcome": outcome,
        }
        if recipients:
            entry["recipients"] = list(recipients)
        folder = rights.session_dir(self.config, self.session_id)
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / rights.JOURNAL_FILE).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _label_targets(targets: Sequence[str | None]) -> str:
    named = [target for target in targets if target]
    return ", ".join(named) if named else "any target"


def _hint(action: str) -> str:
    if action == "mail.send":
        return "imap-mcp rights allow mail.send --recipient ADDRESS --session"
    return f"imap-mcp rights allow {action} [--folder FOLDER] --session"


SESSION_ID = new_session_id()
guard = Guard(settings, SESSION_ID)
