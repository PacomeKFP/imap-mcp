"""Audit journal: one JSON line per guard decision, stored in the session folder."""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from datetime import datetime, timezone

from . import rights
from .config import Settings
from .rights import Action

READ_ACTIONS = frozenset({Action.ACCOUNT_READ, Action.FOLDER_READ, Action.MAIL_READ})


def record(
    config: Settings,
    session_id: str,
    action: rights.Action | str,
    verdicts: Sequence[rights.Verdict],
    recipients: Sequence[str],
    outcome: str,
) -> None:
    """Append one decision. Reads that were allowed are not journaled."""
    if action in READ_ACTIONS and outcome == "allowed":
        return
    entry: dict[str, object] = {
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "session": session_id,
        "action": str(action),
        "verdicts": [
            {"target": target, "decision": str(decision), "source": source}
            for target, decision, source in verdicts
        ],
        "outcome": outcome,
    }
    if recipients:
        entry["recipients"] = list(recipients)
    folder = rights.session_dir(config, session_id)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / rights.JOURNAL_FILE).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        print(f"imap-mcp: cannot write the audit journal in {folder}: {exc}", file=sys.stderr)
