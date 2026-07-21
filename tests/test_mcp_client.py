"""Day 8: end-to-end MCP client <-> model server over stdio.

Unlike the other model-server tests (which call the tool logic directly),
this spawns `python -m model_server.server` as a real subprocess and drives
it through an MCP ClientSession — the same path Claude Desktop / the MCP
Inspector use. This is the M1 exit gate: all read tools callable end-to-end.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO = Path(__file__).resolve().parent.parent
REAL_HR = (
    REPO / "tests" / "fixtures" / "real" / "hr-sample"
    / "Human Resources Sample PBIX.pbip"
)
SYNTH = REPO / "tests" / "fixtures" / "synthetic" / "Synthetic.pbip"
PBIP = REAL_HR if REAL_HR.exists() else SYNTH


def _payload(result):
    assert not result.isError, getattr(result, "content", result)
    if getattr(result, "structuredContent", None):
        sc = result.structuredContent
        return sc.get("result", sc)
    return json.loads(result.content[0].text)


async def _drive() -> dict:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "model_server.server"],
        cwd=str(REPO),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tool_names = [t.name for t in (await session.list_tools()).tools]

            setp = _payload(await session.call_tool(
                "pbi_set_project", {"path": str(PBIP)}))
            model = _payload(await session.call_tool("pbi_get_model", {}))
            measures = _payload(await session.call_tool("pbi_list_measures", {}))
            lineage = _payload(await session.call_tool(
                "pbi_model_lineage", {"measure": measures[0]["name"]}))
            return {
                "tools": tool_names,
                "set_project": setp,
                "model": model,
                "measures": measures,
                "lineage": lineage,
            }


@pytest.fixture(scope="module")
def session_result() -> dict:
    return asyncio.run(asyncio.wait_for(_drive(), timeout=60))


def test_all_tools_advertised(session_result):
    # subset check: the M1 read tools must always be present (more get added later)
    assert {
        "pbi_set_project", "pbi_get_model",
        "pbi_list_measures", "pbi_model_lineage",
    } <= set(session_result["tools"])


def test_set_project_over_stdio(session_result):
    assert session_result["set_project"]["ok"] is True
    assert session_result["set_project"]["tables"] > 0


def test_get_model_over_stdio(session_result):
    counts = session_result["model"]["counts"]
    assert counts["tables"] > 0 and counts["measures"] > 0


def test_list_measures_over_stdio(session_result):
    measures = session_result["measures"]
    assert measures and "dax" in measures[0]


def test_lineage_over_stdio(session_result):
    lin = session_result["lineage"]
    assert "depends_on_measures" in lin and "referenced_by" in lin


def test_state_persists_across_calls(session_result):
    """get_model succeeding proves set_project's state survived to later calls."""
    assert session_result["model"]["counts"]["measures"] == \
        len(session_result["measures"])
