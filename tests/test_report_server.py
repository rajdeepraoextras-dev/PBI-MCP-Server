"""Day 14: report-server read tools (against real HR fixture, else synthetic)."""

from __future__ import annotations

from pathlib import Path

import pytest

from report_server.server import (
    ReportState, get_visual, list_pages, list_visuals, set_project,
)

REAL_HR = (
    Path(__file__).parent / "fixtures" / "real" / "hr-sample"
    / "Human Resources Sample PBIX.pbip"
)
SYNTH = Path(__file__).parent / "fixtures" / "synthetic" / "Synthetic.pbip"
PBIP = REAL_HR if REAL_HR.exists() else SYNTH


@pytest.fixture
def state() -> ReportState:
    st = ReportState()
    set_project(st, str(PBIP))
    return st


def test_set_project_summary():
    st = ReportState()
    s = set_project(st, str(PBIP))
    assert s["ok"] and s["pages"] > 0 and s["visuals"] > 0


def test_tools_require_project():
    st = ReportState()
    with pytest.raises(ValueError):
        list_pages(st)


def test_list_pages_shape(state):
    pages = list_pages(state)
    assert pages and {"id", "name", "visual_count"} <= set(pages[0])


def test_list_visuals_have_bindings(state):
    pages = list_pages(state)
    page = next(p for p in pages if p["visual_count"] > 0)
    visuals = list_visuals(state, page["id"])
    assert visuals
    v = visuals[0]
    assert "bindings" in v and "raw" not in v  # raw excluded from listing


def test_get_visual_includes_raw(state):
    pages = list_pages(state)
    page = next(p for p in pages if p["visual_count"] > 0)
    vid = list_visuals(state, page["id"])[0]["id"]
    v = get_visual(state, page["id"], vid)
    assert v["raw"] and "bindings" in v


def test_server_tool_registration():
    import asyncio

    from report_server.server import mcp

    tools = {t.name for t in asyncio.run(mcp.list_tools())}
    assert {"pbi_set_project", "pbi_list_pages",
            "pbi_list_visuals", "pbi_get_visual"} <= tools
