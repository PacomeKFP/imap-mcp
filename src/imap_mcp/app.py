"""MCP server instance, and the helper that keeps blocking IMAP work off the event loop."""
from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any, TypeVar

from anyio import to_thread
from mcp.server.mcpserver import MCPServer

mcp = MCPServer("imap")

T = TypeVar("T")


async def in_thread(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    return await to_thread.run_sync(functools.partial(func, *args, **kwargs))
