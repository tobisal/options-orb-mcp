"""Compatibility shim across MCP Python SDK versions.

The high-level decorator server was called ``FastMCP`` in mcp<2 and
``MCPServer`` in mcp>=2. Both expose ``.tool()`` and ``.run()`` with the same
ergonomics, so we normalise to a single ``create_server`` factory.
"""

from __future__ import annotations

from typing import Any


def create_server(name: str, instructions: str | None = None) -> Any:
    """Return a high-level MCP server instance for whatever SDK is installed."""
    try:
        from mcp.server.mcpserver import MCPServer  # mcp >= 2

        return MCPServer(name=name, instructions=instructions, version="0.1.0")
    except Exception:  # pragma: no cover - depends on installed version
        from mcp.server.fastmcp import FastMCP  # mcp < 2

        return FastMCP(name, instructions=instructions)
