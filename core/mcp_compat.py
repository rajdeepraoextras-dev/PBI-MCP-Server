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


def tool_annotations(*, read_only: bool, destructive: bool = False,
                     idempotent: bool = False, open_world: bool = False):
    """Build ``ToolAnnotations`` for either major.

    mcp 1.x names the fields ``readOnlyHint`` etc.; 2.x renamed them to
    snake_case (``read_only_hint``). Everything here is local-file work, so
    ``open_world`` defaults to False.
    """
    import mcp.types as types

    fields = types.ToolAnnotations.model_fields
    if "readOnlyHint" in fields:
        return types.ToolAnnotations(
            readOnlyHint=read_only, destructiveHint=destructive,
            idempotentHint=idempotent, openWorldHint=open_world)
    return types.ToolAnnotations(
        read_only_hint=read_only, destructive_hint=destructive,
        idempotent_hint=idempotent, open_world_hint=open_world)


__all__ = ["Server", "MCP_MAJOR", "tool_annotations"]
