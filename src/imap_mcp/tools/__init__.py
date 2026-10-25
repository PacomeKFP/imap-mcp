"""MCP tools, grouped by area. Importing this package registers every tool."""
from ..app import mcp
from . import account, composing, organizing, reading, replying

__all__ = ["account", "composing", "organizing", "reading", "replying", "run"]


def run() -> None:
    """Serve over stdio."""
    mcp.run()
