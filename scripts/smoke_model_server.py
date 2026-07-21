"""Day 8 smoke test: drive the model server over stdio as a real MCP client.

Spawns `python -m model_server.server` as a subprocess and calls every M1
read tool end-to-end, exactly as an MCP client (Claude Desktop, Inspector)
would. Run it:

    pbi-mcp/.venv/Scripts/python.exe scripts/smoke_model_server.py [PATH_TO.pbip]
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO = Path(__file__).resolve().parent.parent
DEFAULT_PBIP = (
    REPO / "tests" / "fixtures" / "real" / "hr-sample"
    / "Human Resources Sample PBIX.pbip"
)


def _payload(result):
    """Pull the JSON payload out of a CallToolResult."""
    if getattr(result, "structuredContent", None):
        sc = result.structuredContent
        return sc.get("result", sc)
    return json.loads(result.content[0].text)


async def run(pbip: Path) -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "model_server.server"],
        cwd=str(REPO),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            print("tools:", [t.name for t in tools.tools])

            r = await session.call_tool("pbi_set_project", {"path": str(pbip)})
            print("set_project:", _payload(r))

            model = _payload(await session.call_tool("pbi_get_model", {}))
            print("get_model counts:", model["counts"])

            measures = _payload(await session.call_tool("pbi_list_measures", {}))
            print("list_measures:", len(measures), "measures;",
                  "e.g.", measures[0]["name"])

            lin = _payload(await session.call_tool(
                "pbi_model_lineage", {"measure": measures[0]["name"]}))
            print("lineage of", repr(lin["measure"]), "->",
                  "measures:", lin["depends_on_measures"],
                  "| referenced_by:", len(lin["referenced_by"]))

    print("\nOK: all M1 tools callable end-to-end over stdio.")


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PBIP
    asyncio.run(run(target))
