"""The MCP server exposes the expected tool set, with keyword-only options intact."""

import asyncio
import inspect

from imap_mcp.app import mcp
from imap_mcp.tools import organizing, reading


def _tools() -> dict:
    return {tool.name: tool for tool in asyncio.run(mcp.list_tools())}


def test_server_registers_21_tools():
    assert len(_tools()) == 21


def test_trash_mails_is_registered_and_delete_mails_is_gone():
    names = _tools()
    assert "trash_mails" in names
    assert "delete_mails" not in names


def test_options_after_identifiers_are_keyword_only():
    for func, options in (
        (organizing.mark_mails, ["read", "flagged", "answered"]),
        (organizing.move_mails, ["folder"]),
        (reading.search_mails, ["text", "limit"]),
        (reading.read_mail, ["include_html", "mark_read"]),
        (reading.download_attachment, ["folder", "dest_dir"]),
    ):
        params = inspect.signature(func).parameters
        for name in options:
            assert params[name].kind is inspect.Parameter.KEYWORD_ONLY, (func.__name__, name)
