"""core/tooling: annotations, injected dry_run, journal tools, end to end."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from core.journal import snapshot

SYNTH = Path(__file__).parent / "fixtures" / "synthetic"


def _ann(tool, hint: str):
    """Read an annotation hint across mcp 1.x (camelCase) and 2.x (snake)."""
    a = tool.annotations
    snake = {"readOnlyHint": "read_only_hint", "destructiveHint": "destructive_hint",
             "idempotentHint": "idempotent_hint"}[hint]
    return getattr(a, hint, None) if hasattr(a, hint) else getattr(a, snake, None)


def _schema(tool) -> dict:
    return getattr(tool, "inputSchema", None) or getattr(tool, "input_schema")


def _tools(server_module):
    return {t.name: t for t in asyncio.run(server_module.mcp.list_tools())}


def _payload(result):
    """Tool result -> python object, across mcp majors and result shapes.

    Prefers structured content ({"result": x} for non-object returns); falls
    back to parsing the text blocks (a list return becomes one block per item).
    """
    structured = None
    if isinstance(result, tuple):           # mcp 1.x: (content, structured)
        result, structured = result
    structured = (structured
                  or getattr(result, "structured_content", None)
                  or getattr(result, "structuredContent", None))
    if structured is not None:
        return structured.get("result", structured) if isinstance(structured, dict) else structured
    content = getattr(result, "content", result)
    items = [json.loads(c.text) for c in content]
    return items[0] if len(items) == 1 else items


@pytest.mark.parametrize("server", ["model", "report"])
def test_annotations_and_dry_run_parameter(server):
    if server == "model":
        import model_server.server as mod
        read, write, own_dry, destructive = (
            "pbi_list_measures", "pbi_create_measure", "pbi_delete_measure", "pbi_delete_measure")
    else:
        import report_server.server as mod
        read, write, own_dry, destructive = (
            "pbi_list_pages", "pbi_add_text", "pbi_scaffold_report", "pbi_delete_visual")
    tools = _tools(mod)

    assert _ann(tools[read], "readOnlyHint") is True
    assert "dry_run" not in _schema(tools[read]).get("properties", {})

    assert _ann(tools[write], "readOnlyHint") is False
    assert _ann(tools[write], "destructiveHint") is False
    props = _schema(tools[write])["properties"]
    assert props["dry_run"]["type"] == "boolean" and "dry_run" not in _schema(tools[write]).get("required", [])
    assert "dry_run=true" in (tools[write].description or "")

    assert _ann(tools[destructive], "destructiveHint") is True
    # a tool with its own dry_run keeps exactly one such parameter
    assert list(_schema(tools[own_dry])["properties"]).count("dry_run") == 1

    for name in ("pbi_undo", "pbi_undo_history", "pbi_begin_transaction",
                 "pbi_commit", "pbi_rollback"):
        assert name in tools
    assert _ann(tools["pbi_undo"], "destructiveHint") is True
    assert "dry_run" not in _schema(tools["pbi_undo"]).get("properties", {})

    # every tool carries annotations
    assert all(t.annotations is not None for t in tools.values())


def test_every_tool_is_classified():
    import model_server.server as m
    import report_server.server as r
    for mod in (m, r):
        for t in _tools(mod).values():
            assert _ann(t, "readOnlyHint") in (True, False), t.name


def test_write_tool_end_to_end_dry_run_then_write_then_undo(tmp_path):
    import model_server.server as mod
    proj = tmp_path / "p"
    shutil.copytree(SYNTH, proj)
    mcp = mod.mcp

    def call(tool_name, **args):
        return _payload(asyncio.run(mcp.call_tool(tool_name, args)))

    call("pbi_set_project", path=str(proj / "Synthetic.pbip"))
    before = snapshot(proj)

    preview = call("pbi_create_measure", table="Sales", name="E2E", dax="1", dry_run=True)
    assert preview["dry_run"] is True and "E2E" in preview["diff"]
    assert snapshot(proj) == before

    real = call("pbi_create_measure", table="Sales", name="E2E", dax="1")
    assert real["ok"] is True and snapshot(proj) != before
    history = call("pbi_undo_history")
    assert history[0]["tool"] == "pbi_create_measure"

    undone = call("pbi_undo")
    assert undone["undone"][0]["tool"] == "pbi_create_measure"
    assert snapshot(proj) == before
    assert call("pbi_undo_history") == []


def test_restore_tools_have_no_dry_run_and_generate_theme_is_a_write():
    import model_server.server as m
    import report_server.server as r
    rt, mt = _tools(r), _tools(m)
    for tools, name in ((rt, "pbi_restore_visual"), (mt, "pbi_restore_backup")):
        assert "dry_run" not in _schema(tools[name]).get("properties", {}), name
        assert _ann(tools[name], "readOnlyHint") is False
    theme = rt["pbi_generate_theme"]
    assert _ann(theme, "readOnlyHint") is False
    assert "dry_run" in _schema(theme)["properties"]
