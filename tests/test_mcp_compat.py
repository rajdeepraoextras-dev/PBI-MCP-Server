"""core/mcp_compat: both servers build on whichever mcp major is installed."""

from __future__ import annotations

import importlib.metadata


def test_shim_matches_installed_mcp():
    from core.mcp_compat import MCP_MAJOR, Server
    installed = int(importlib.metadata.version("mcp").split(".")[0])
    assert MCP_MAJOR == installed
    assert Server.__name__ == ("MCPServer" if installed >= 2 else "FastMCP")


def test_servers_are_shim_instances():
    from core.mcp_compat import Server
    from model_server.server import mcp as model
    from report_server.server import mcp as report
    assert isinstance(model, Server) and isinstance(report, Server)
