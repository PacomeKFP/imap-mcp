"""Account and folder tools."""

from __future__ import annotations

import imaplib
from typing import Any

from mcp.server.mcpserver import Context

from .. import imap
from ..app import in_thread, mcp
from ..config import get_credentials, settings
from ..permissions import guard


@mcp.tool()
async def whoami(ctx: Context) -> dict[str, Any]:
    """Connected address, servers, read-only mode, download folder and current session id."""
    await guard.authorize(ctx, "account.read")
    user, _ = await in_thread(get_credentials)
    return {
        "user": user,
        "imap": f"{settings.imap_host}:{settings.imap_port}",
        "smtp": f"{settings.smtp_host}:{settings.smtp_port}",
        "read_only": settings.read_only,
        "download_dir": str(settings.download_dir),
        "session": guard.session_id,
    }


@mcp.tool()
async def list_folders(ctx: Context) -> list[dict[str, Any]]:
    """Folders with message count, unread count and role (drafts, sent, trash, junk, archive)."""
    await guard.authorize(ctx, "folder.read")
    return await in_thread(_list_folders)


def _list_folders() -> list[dict[str, Any]]:
    with imap.session() as conn:
        result: list[dict[str, Any]] = []
        for folder in imap.list_folders(conn):
            counts = imap.folder_status(conn, folder.name)
            result.append(
                {
                    "folder": folder.name,
                    "role": folder.role,
                    "messages": counts[0] if counts else None,
                    "unread": counts[1] if counts else None,
                }
            )
    return result


@mcp.tool()
async def get_quota(ctx: Context) -> dict[str, Any]:
    """Storage quota, when the server provides it (raw IMAP response)."""
    await guard.authorize(ctx, "account.read")
    return await in_thread(_get_quota)


def _get_quota() -> dict[str, Any]:
    try:
        with imap.session() as conn:
            status, data = conn.getquotaroot("INBOX")
    except imaplib.IMAP4.error:
        return {"quota": "unavailable"}
    if status != "OK":
        return {"quota": "unavailable"}
    return {"raw": [item.decode(errors="replace") for item in data if isinstance(item, bytes)]}


@mcp.tool()
async def create_folder(ctx: Context, name: str) -> str:
    """Create a folder (e.g. 'Projects/Alpha')."""
    await guard.authorize(ctx, "folder.create", targets=[name])
    return await in_thread(_create_folder, name)


def _create_folder(name: str) -> str:
    with imap.session() as conn:
        status, data = conn.create(imap.quote(name))
        imap.expect_ok(status, data, "Folder creation")
        conn.subscribe(imap.quote(name))
    return f"Folder created: {name}"


@mcp.tool()
async def rename_folder(ctx: Context, old_name: str, new_name: str) -> str:
    """Rename or move a folder."""
    await guard.authorize(ctx, "folder.update", targets=[old_name, new_name])
    return await in_thread(_rename_folder, old_name, new_name)


def _rename_folder(old_name: str, new_name: str) -> str:
    with imap.session() as conn:
        status, data = conn.rename(imap.quote(old_name), imap.quote(new_name))
        imap.expect_ok(status, data, "Folder rename")
    return f"{old_name} -> {new_name}"
