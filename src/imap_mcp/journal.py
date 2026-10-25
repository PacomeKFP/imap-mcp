"""Audit journal: one JSON line per guard decision, stored in the session folder."""
from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime

from . import rights
from .config import Settings

READ_ACTIONS = frozenset({"account.read", "folder.read", "mail.read"})


def record(config: Settings, session_id: str, action: str, verdicts: Sequence[rights.Verdict],
           recipients: Sequence[str], outcome: str) -> None:
    """Append one decision. Reads that were allowed are not journaled."""
    if action in READ_ACTIONS and outcome == "allowed":
        return
    entry: dict[str, object] = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "session": session_id,
        "action": action,
        "verdicts": [{"target": target, "decision": decision, "source": source}
                     for target, decision, source in verdicts],
        "outcome": outcome,
    }
    if recipients:
        entry["recipients"] = list(recipients)
    folder = rights.session_dir(config, session_id)
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / rights.JOURNAL_FILE).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
