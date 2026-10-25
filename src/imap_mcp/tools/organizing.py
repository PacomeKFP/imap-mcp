"""Flag, move, copy and delete tools. Nothing is purged: deletion moves mail to the Agent Trash."""

from __future__ import annotations

from mcp.server.mcpserver import Context
from typing_extensions import TypedDict

from .. import imap, rights
from ..app import in_thread, mcp
from ..config import settings
from ..permissions import guard

_FLAG_NAMES = {"read": r"\Seen", "flagged": r"\Flagged", "answered": r"\Answered"}


class MarkResult(TypedDict):
    count: int
    folder: str
    changes: dict[str, bool]


class TransferResult(TypedDict):
    count: int
    folder: str
    destination: str


@mcp.tool()
async def mark_mails(
    ctx: Context,
    uids: list[str],
    folder: str = "INBOX",
    *,
    read: bool | None = None,
    flagged: bool | None = None,
    answered: bool | None = None,
) -> MarkResult:
    """Set message state: read (seen), flagged, answered. None leaves a state unchanged.

    changes lists the states that were set, by name (read, flagged, answered).
    """
    await guard.authorize(ctx, "mail.update", targets=[folder])
    return await in_thread(_mark_mails, uids, folder, read, flagged, answered)


def _mark_mails(
    uids: list[str], folder: str, read: bool | None, flagged: bool | None, answered: bool | None
) -> MarkResult:
    ids = imap.uid_set(uids)
    requested = {"read": read, "flagged": flagged, "answered": answered}
    changes: dict[str, bool] = {
        name: value for name, value in requested.items() if value is not None
    }
    with imap.session(folder, readonly=False) as conn:
        for name, value in changes.items():
            imap.store(conn, ids, _FLAG_NAMES[name], value)
    return {"count": len(uids), "folder": folder, "changes": changes}


@mcp.tool()
async def move_mails(
    ctx: Context, uids: list[str], destination: str, *, folder: str = "INBOX"
) -> TransferResult:
    """Move messages to another folder."""
    await guard.authorize(ctx, "mail.read", targets=[folder])
    await guard.authorize(ctx, "mail.move", targets=[folder, destination])
    return await in_thread(_move_mails, uids, destination, folder)


def _move_mails(uids: list[str], destination: str, folder: str) -> TransferResult:
    ids = imap.uid_set(uids)
    with imap.session(folder, readonly=False) as conn:
        imap.move(conn, ids, destination)
    return {"count": len(uids), "folder": folder, "destination": destination}


@mcp.tool()
async def copy_mails(
    ctx: Context, uids: list[str], destination: str, *, folder: str = "INBOX"
) -> TransferResult:
    """Copy messages to another folder. The originals stay in place."""
    await guard.authorize(ctx, "mail.read", targets=[folder])
    await guard.authorize(ctx, "mail.create", targets=[destination])
    return await in_thread(_copy_mails, uids, destination, folder)


def _copy_mails(uids: list[str], destination: str, folder: str) -> TransferResult:
    ids = imap.uid_set(uids)
    with imap.session(folder) as conn:
        imap.copy(conn, ids, destination)
    return {"count": len(uids), "folder": folder, "destination": destination}


@mcp.tool()
async def archive_mails(ctx: Context, uids: list[str], *, folder: str = "INBOX") -> TransferResult:
    """Move messages to the Archive folder."""
    await guard.authorize(ctx, "mail.read", targets=[folder])
    await guard.authorize(ctx, "mail.move", targets=[folder, rights.ARCHIVE])
    return await in_thread(_move_to_role, uids, folder, "archive")


@mcp.tool()
async def trash_mails(ctx: Context, uids: list[str], *, folder: str = "INBOX") -> TransferResult:
    """Move messages to the Agent Trash. Nothing is purged; only the user can empty that folder."""
    await guard.authorize(ctx, "mail.delete", targets=[folder])
    return await in_thread(_trash_mails, uids, folder)


def _trash_mails(uids: list[str], folder: str) -> TransferResult:
    ids = imap.uid_set(uids)
    with imap.session(folder, readonly=False) as conn:
        imap.ensure_folder(conn, settings.agent_trash)
        imap.move(conn, ids, settings.agent_trash)
    return {"count": len(uids), "folder": folder, "destination": settings.agent_trash}


@mcp.tool()
async def mark_spam(ctx: Context, uids: list[str], *, folder: str = "INBOX") -> TransferResult:
    """Move messages to the Junk folder."""
    await guard.authorize(ctx, "mail.read", targets=[folder])
    await guard.authorize(ctx, "mail.move", targets=[folder, rights.JUNK])
    return await in_thread(_move_to_role, uids, folder, "junk")


def _move_to_role(uids: list[str], folder: str, role: str) -> TransferResult:
    ids = imap.uid_set(uids)
    destination = imap.cached_special_folder(role)
    with imap.session(folder, readonly=False) as conn:
        imap.move(conn, ids, destination)
    return {"count": len(uids), "folder": folder, "destination": destination}
