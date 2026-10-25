"""Approval prompt: asks the user through the MCP client and describes what is requested."""
from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from mcp.server.mcpserver import Context
from pydantic import BaseModel

LISTED_RECIPIENTS = 10


class _Choice(BaseModel):
    choice: Literal["once", "session", "refuse"]


def describe_targets(targets: Sequence[str | None]) -> str:
    named = [target for target in targets if target]
    return ", ".join(named) if named else "any target"


def describe_request(action: str, asks: Sequence[str | None], recipients: Sequence[str],
                     detail: str) -> str:
    if action == "mail.send" and recipients:
        shown = ", ".join(recipients[:LISTED_RECIPIENTS])
        if len(recipients) > LISTED_RECIPIENTS:
            shown += f" and {len(recipients) - LISTED_RECIPIENTS} more"
        subject = f"sending mail to {shown}"
    else:
        subject = f"{action} on {describe_targets(asks)}"
    suffix = f" ({detail})" if detail else ""
    return (f"Approval needed: {subject}{suffix}. "
            "Choose once, session (this session only), or refuse.")


async def ask_user(ctx: Context, action: str, asks: Sequence[str | None],
                   recipients: Sequence[str], detail: str) -> str | None:
    """Return 'once', 'session' or 'refuse'; None when the client cannot ask."""
    message = describe_request(action, asks, recipients, detail)
    try:
        result = await ctx.elicit(message, _Choice)
    except Exception:  # the client does not support elicitation
        return None
    return result.data.choice if result.action == "accept" else "refuse"
