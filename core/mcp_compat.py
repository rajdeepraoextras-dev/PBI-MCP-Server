"""Import shim: run on either mcp 1.x (FastMCP) or mcp 2.x (MCPServer).

mcp 2.0 renamed ``mcp.server.fastmcp.FastMCP`` to
``mcp.server.mcpserver.MCPServer`` and reordered the constructor's positional
parameters (``title``/``description`` now sit before ``instructions``). Both
servers use only the surface that is identical across majors --
``Server(name, instructions=...)``, ``@server.tool()``, ``server.run()`` --
so one alias covers both. Always pass ``instructions`` as a keyword.
"""

from __future__ import annotations

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as Server
    MCP_MAJOR = 2
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as Server
    MCP_MAJOR = 1

__all__ = ["Server", "MCP_MAJOR"]
