"""Flag, move, copy and delete tools. Nothing is purged: deletion moves mail to the Agent Trash."""
from __future__ import annotations

from mcp.server.mcpserver import Context

from .. import imap, rights
from ..app import in_thread, mcp
from ..config import settings
from ..permissions import guard


@mcp.tool()
async def mark_mails(ctx: Context, uids: list[str], folder: str = "INBOX", read: bool | None = None,
                     flagged: bool | None = None, answered: bool | None = None) -> str:
    """Set message state: read (seen), flagged, answered. None leaves a state unchanged."""
    await guard.authorize(ctx, "mail.update", targets=[folder])
    return await in_thread(_mark_mails, uids, folder, read, flagged, answered)


def _mark_mails(uids: list[str], folder: str, read: bool | None, flagged: bool | None,
                answered: bool | None) -> str:
    ids = imap.uid_set(uids)
    changes = ((r"\Seen", read), (r"\Flagged", flagged), (r"\Answered", answered))
    with imap.session(folder, readonly=False) as conn:
        for flag, value in changes:
            if value is not None:
                imap.store(conn, ids, flag, value)
    return f"{len(uids)} message(s) updated."


@mcp.tool()
async def move_mails(ctx: Context, uids: list[str], destination: str, folder: str = "INBOX") -> str:
    """Move messages to another folder."""
    await guard.authorize(ctx, "mail.read", targets=[folder])
    await guard.authorize(ctx, "mail.move", targets=[folder, destination])
    return await in_thread(_move_mails, uids, destination, folder)


def _move_mails(uids: list[str], destination: str, folder: str) -> str:
    ids = imap.uid_set(uids)
    with imap.session(folder, readonly=False) as conn:
        imap.move(conn, ids, destination)
    return f"{len(uids)} message(s) moved to {destination}."


@mcp.tool()
async def copy_mails(ctx: Context, uids: list[str], destination: str, folder: str = "INBOX") -> str:
    """Copy messages to another folder."""
    await guard.authorize(ctx, "mail.read", targets=[folder])
    await guard.authorize(ctx, "mail.create", targets=[destination])
    return await in_thread(_copy_mails, uids, destination, folder)


def _copy_mails(uids: list[str], destination: str, folder: str) -> str:
    ids = imap.uid_set(uids)
    with imap.session(folder) as conn:
        imap.copy(conn, ids, destination)
    return f"{len(uids)} message(s) copied to {destination}."


@mcp.tool()
async def archive_mails(ctx: Context, uids: list[str], folder: str = "INBOX") -> str:
    """Move messages to the Archive folder."""
    await guard.authorize(ctx, "mail.read", targets=[folder])
    await guard.authorize(ctx, "mail.move", targets=[folder, rights.ARCHIVE])
    destination = await in_thread(_move_to_role, uids, folder, "archive")
    return f"{len(uids)} message(s) archived in {destination}."


@mcp.tool()
async def delete_mails(ctx: Context, uids: list[str], folder: str = "INBOX") -> str:
    """Move messages to the Agent Trash. Nothing is purged; only the user can empty that folder."""
    await guard.authorize(ctx, "mail.delete", targets=[folder])
    return await in_thread(_delete_mails, uids, folder)


def _delete_mails(uids: list[str], folder: str) -> str:
    ids = imap.uid_set(uids)
    with imap.session(folder, readonly=False) as conn:
        imap.ensure_folder(conn, settings.agent_trash)
        imap.move(conn, ids, settings.agent_trash)
    return f"{len(uids)} message(s) moved to {settings.agent_trash}."


@mcp.tool()
async def mark_spam(ctx: Context, uids: list[str], folder: str = "INBOX") -> str:
    """Move messages to the Junk folder."""
    await guard.authorize(ctx, "mail.read", targets=[folder])
    await guard.authorize(ctx, "mail.move", targets=[folder, rights.JUNK])
    destination = await in_thread(_move_to_role, uids, folder, "junk")
    return f"{len(uids)} message(s) moved to {destination}."


def _move_to_role(uids: list[str], folder: str, role: str) -> str:
    ids = imap.uid_set(uids)
    with imap.session(folder, readonly=False) as conn:
        destination = imap.special_folder(conn, role)
        imap.move(conn, ids, destination)
    return destination
