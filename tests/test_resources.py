"""MCP resources and prompts registered by tools_resources.py in both servers."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from pathlib import Path

import pytest

from core.pbip import PbipProject

SYNTH = Path(__file__).parent / "fixtures" / "synthetic"
SYNTH_PBIP = SYNTH / "Synthetic.pbip"

MODEL_RESOURCES = {"pbip://model", "pbip://model/measures", "pbip://model/lineage"}
MODEL_TEMPLATES = {"pbip://model/tables/{table}"}
MODEL_PROMPTS = {"audit_model": [], "bulk_measures": ["spec"]}
REPORT_RESOURCES = {"pbip://pages", "pbip://capabilities", "pbip://theme"}
REPORT_TEMPLATES = {"pbip://pages/{page_id}"}
REPORT_PROMPTS = {"build_dashboard": ["goal"], "theme_report": ["brand_color"],
                  "review_page": ["page_id"]}


def _servers():
    import model_server.server as model
    import report_server.server as report
    return model, report


def _template_uri(t) -> str:
    return getattr(t, "uriTemplate", None) or getattr(t, "uri_template")


def _read(mod, uri: str) -> tuple[str, str | None]:
    """(text, mime type) of a resource, across mcp majors."""
    contents = list(asyncio.run(mod.mcp.read_resource(uri)))
    assert len(contents) == 1
    return contents[0].content, contents[0].mime_type


def _read_json(mod, uri: str) -> dict:
    text, mime = _read(mod, uri)
    assert mime == "application/json"
    return json.loads(text)


def _prompt_text(mod, name: str, arguments: dict | None = None) -> str:
    result = asyncio.run(mod.mcp.get_prompt(name, arguments or {}))
    assert result.messages and result.messages[0].role == "user"
    return result.messages[0].content.text


@pytest.fixture
def model(monkeypatch):
    import model_server.server as mod
    monkeypatch.setattr(mod.STATE, "project", PbipProject(SYNTH_PBIP))
    return mod


@pytest.fixture
def report(monkeypatch):
    import report_server.server as mod
    monkeypatch.setattr(mod.STATE, "project", PbipProject(SYNTH_PBIP))
    return mod


# --- listing --------------------------------------------------------------------

def test_model_server_lists_its_resources_templates_and_prompts():
    mod, _ = _servers()
    resources = {str(r.uri) for r in asyncio.run(mod.mcp.list_resources())}
    assert MODEL_RESOURCES <= resources
    templates = {_template_uri(t)
                 for t in asyncio.run(mod.mcp.list_resource_templates())}
    assert MODEL_TEMPLATES <= templates
    prompts = {p.name: [a.name for a in (p.arguments or [])]
               for p in asyncio.run(mod.mcp.list_prompts())}
    for name, args in MODEL_PROMPTS.items():
        assert prompts[name] == args


def test_report_server_lists_its_resources_templates_and_prompts():
    _, mod = _servers()
    resources = {str(r.uri) for r in asyncio.run(mod.mcp.list_resources())}
    assert REPORT_RESOURCES <= resources
    templates = {_template_uri(t)
                 for t in asyncio.run(mod.mcp.list_resource_templates())}
    assert REPORT_TEMPLATES <= templates
    prompts = {p.name: [a.name for a in (p.arguments or [])]
               for p in asyncio.run(mod.mcp.list_prompts())}
    for name, args in REPORT_PROMPTS.items():
        assert prompts[name] == args


def test_resources_have_json_mime_type_and_descriptions():
    for mod in _servers():
        for r in asyncio.run(mod.mcp.list_resources()):
            mime = getattr(r, "mimeType", None) or getattr(r, "mime_type", None)
            if str(r.uri).startswith("pbip://"):
                assert mime == "application/json", r.uri
                assert r.description, r.uri
        for t in asyncio.run(mod.mcp.list_resource_templates()):
            assert t.description, _template_uri(t)


def test_resources_are_not_tools():
    """Adding resources must not disturb the tool surface."""
    for mod in _servers():
        names = {t.name for t in asyncio.run(mod.mcp.list_tools())}
        assert "pbi_doctor" in names
        assert not any(n.startswith("pbip:") for n in names)


# --- model resources ------------------------------------------------------------------

def test_model_overview_has_the_expected_keys(model):
    doc = _read_json(model, "pbip://model")
    assert set(doc) >= {"path", "counts", "tables", "relationships", "see_also"}
    project = model.STATE.project
    tables = project.list_tables()
    assert doc["counts"] == {
        "tables": len(tables),
        "columns": sum(len(t.columns) for t in tables),
        "measures": sum(len(t.measures) for t in tables),
        "relationships": len(project.list_relationships()),
    }
    assert [t["name"] for t in doc["tables"]] == [t.name for t in tables]
    first = doc["tables"][0]
    assert set(first) == {"name", "is_hidden", "is_calc_group", "columns",
                          "measures"}
    assert all(isinstance(n, str) for n in first["columns"] + first["measures"])


def test_model_measures_resource_matches_the_tool(model):
    from model_server.server import list_measures

    doc = _read_json(model, "pbip://model/measures")
    expected = list_measures(model.STATE)
    assert doc["count"] == len(expected) > 0
    assert doc["measures"] == expected
    assert all("dax" in m for m in doc["measures"])


def test_model_lineage_resource_matches_the_tool(model):
    from model_server.server import model_lineage

    doc = _read_json(model, "pbip://model/lineage")
    assert doc == json.loads(json.dumps(model_lineage(model.STATE)))
    assert set(doc) == {"measures", "referenced_by"}


def test_model_table_template(model):
    doc = _read_json(model, "pbip://model/tables/Sales")
    assert doc["name"] == "Sales"
    assert doc["columns"] and {"name", "data_type"} <= set(doc["columns"][0])
    assert doc["measures"] and "dax" in doc["measures"][0]
    assert isinstance(doc["relationships"], list)
    assert all("Sales" in (r["from_table"], r["to_table"])
               for r in doc["relationships"])


def test_model_table_template_unknown_table_lists_the_available_ones(model):
    doc = _read_json(model, "pbip://model/tables/Nope")
    assert "not found" in doc["error"]
    assert "Sales" in doc["available"]


def test_model_table_template_handles_names_with_spaces(monkeypatch, tmp_path):
    """Real models are full of 'Sales Detail'-style names; hosts send %20."""
    import model_server.server as mod

    dst = tmp_path / "proj"
    shutil.copytree(SYNTH, dst)
    project = PbipProject(dst / "Synthetic.pbip")
    project.create_calc_group("Time Intelligence", 10,
                              [{"name": "Current", "dax": "SELECTEDMEASURE()"}])
    monkeypatch.setattr(mod.STATE, "project", project)
    for uri in ("pbip://model/tables/Time%20Intelligence",
                "pbip://model/tables/Time Intelligence"):
        doc = _read_json(mod, uri)
        assert doc["name"] == "Time Intelligence", uri
        assert doc["is_calc_group"] is True


def test_model_table_name_is_url_decoded(model):
    from model_server.tools_resources import model_table

    doc = json.loads(model_table(model.STATE, "Sa%6Ces"))
    assert doc["name"] == "Sales"


def test_model_resources_without_a_project_return_a_clear_error(monkeypatch):
    import model_server.server as mod
    monkeypatch.setattr(mod.STATE, "project", None)
    for uri in ("pbip://model", "pbip://model/measures",
                "pbip://model/lineage", "pbip://model/tables/Sales"):
        doc = _read_json(mod, uri)
        assert set(doc) == {"error"}
        assert "pbi_set_project" in doc["error"], uri


# --- report resources ------------------------------------------------------------------

def test_pages_resource(report):
    doc = _read_json(report, "pbip://pages")
    pages = report.STATE.project.list_pages()
    assert doc["count"] == len(pages) > 0
    assert [p["id"] for p in doc["pages"]] == [p.id for p in pages]
    assert {"id", "name", "visual_count", "is_hidden"} <= set(doc["pages"][0])


def test_page_template_lists_visuals_with_bindings(report):
    doc = _read_json(report, "pbip://pages/overview")
    assert doc["page"]["id"] == "overview"
    assert doc["visual_count"] == len(doc["visuals"]) > 0
    v = doc["visuals"][0]
    assert {"id", "visual_type", "position", "bindings"} <= set(v)
    assert "raw" not in v            # raw visual.json stays out of the listing


def test_page_template_unknown_page_lists_the_available_ones(report):
    doc = _read_json(report, "pbip://pages/nope")
    assert "not found" in doc["error"] and "overview" in doc["available"]


def test_capabilities_resource_needs_no_project(monkeypatch):
    import report_server.server as mod
    monkeypatch.setattr(mod.STATE, "project", None)
    doc = _read_json(mod, "pbip://capabilities")
    assert "visual_types" in doc and "workflow" in doc


def test_theme_resource_without_a_custom_theme(report):
    doc = _read_json(report, "pbip://theme")
    assert set(doc) == {"base_theme", "custom_theme", "accent"}
    assert doc["custom_theme"] is None and doc["accent"] is None


def test_theme_resource_returns_the_installed_theme(monkeypatch, tmp_path):
    import report_server.server as mod
    from core.theme import generate_theme

    dst = tmp_path / "proj"
    shutil.copytree(SYNTH, dst)
    project = PbipProject(dst / "Synthetic.pbip")
    theme = generate_theme("#1F3A5F", "Doc Theme", "light")
    project.set_report_theme(theme)
    monkeypatch.setattr(mod.STATE, "project", project)

    doc = _read_json(mod, "pbip://theme")
    assert doc["custom_theme"]["resource"] == "Doc Theme.json"
    assert doc["custom_theme"]["theme"]["name"] == "Doc Theme"
    assert doc["accent"] == theme["dataColors"][0]


def test_report_resources_without_a_project_return_a_clear_error(monkeypatch):
    import report_server.server as mod
    monkeypatch.setattr(mod.STATE, "project", None)
    for uri in ("pbip://pages", "pbip://pages/overview", "pbip://theme"):
        doc = _read_json(mod, uri)
        assert set(doc) == {"error"}
        assert "pbi_set_project" in doc["error"], uri


# --- prompts ------------------------------------------------------------------------------

def test_model_prompts_render_with_their_arguments():
    mod, _ = _servers()
    audit = _prompt_text(mod, "audit_model")
    for tool in ("pbi_set_project", "pbi_doctor", "pbi_get_model",
                 "pbi_list_measures", "pbi_model_lineage", "pbi_model_usage",
                 "pbi_delete_measure", "pbi_begin_transaction", "pbi_undo"):
        assert tool in audit
    assert "dry_run=true" in audit

    spec = "Total Sales = SUM(Sales[Amount]) in folder KPIs"
    bulk = _prompt_text(mod, "bulk_measures", {"spec": spec})
    assert spec in bulk
    for tool in ("pbi_bulk_create_measures", "pbi_update_measure",
                 "pbi_model_lineage", "pbi_undo"):
        assert tool in bulk
    assert "dry_run=true" in bulk
    assert '"table", "name", "dax"' in bulk        # literal braces survive format()


def test_report_prompts_render_with_their_arguments():
    _, mod = _servers()
    dash = _prompt_text(mod, "build_dashboard", {"goal": "regional sales review"})
    assert "regional sales review" in dash
    for tool in ("pbi_capabilities", "pbi_profile_model", "pbi_scaffold_report",
                 "pbi_build_designed_page", "pbi_lint_page",
                 "pbi_validate_project", "pbi_undo", "pbi_begin_transaction"):
        assert tool in dash
    assert '{"measure": "Table.Measure"' in dash   # braces rendered, not eaten

    theme = _prompt_text(mod, "theme_report", {"brand_color": "#0B6E4F"})
    assert theme.count("#0B6E4F") >= 2
    for tool in ("pbi_generate_theme", "pbi_set_report_theme", "pbi_style_page",
                 "pbi_format_visual", "pbi_lint_page", "pbi_undo"):
        assert tool in theme

    review = _prompt_text(mod, "review_page", {"page_id": "overview"})
    assert 'pbi_lint_page("overview")' in review
    for tool in ("pbi_list_visuals", "pbi_page_style", "pbi_validate_project",
                 "pbi_model_usage", "pbi_move_visual"):
        assert tool in review


def test_prompts_are_numbered_step_lists_with_recovery_advice():
    model, report = _servers()
    texts = [
        _prompt_text(model, "audit_model"),
        _prompt_text(model, "bulk_measures", {"spec": "x"}),
        _prompt_text(report, "build_dashboard", {"goal": "x"}),
        _prompt_text(report, "theme_report", {"brand_color": "#123456"}),
        _prompt_text(report, "review_page", {"page_id": "p"}),
    ]
    for text in texts:
        assert re.search(r"^1\. ", text, re.M) and re.search(r"^3\. ", text, re.M)
        assert "pbi_undo" in text or "pbi_rollback" in text
        assert "{{" not in text and "}}" not in text     # format() escapes resolved


def test_prompts_only_name_tools_that_exist():
    """A renamed or removed tool must not leave a prompt pointing at it."""
    model, report = _servers()
    known = set()
    for mod in (model, report):
        known |= {t.name for t in asyncio.run(mod.mcp.list_tools())}
    texts = [
        _prompt_text(model, "audit_model"),
        _prompt_text(model, "bulk_measures", {"spec": "x"}),
        _prompt_text(report, "build_dashboard", {"goal": "x"}),
        _prompt_text(report, "theme_report", {"brand_color": "#123456"}),
        _prompt_text(report, "review_page", {"page_id": "p"}),
    ]
    mentioned = set()
    for text in texts:
        mentioned |= set(re.findall(r"\bpbi_[a-z0-9_]+\b", text))
    assert mentioned, "prompts should name tools"
    assert mentioned <= known, sorted(mentioned - known)


def test_prompt_arguments_are_stripped_and_defaulted():
    from model_server.tools_resources import bulk_measures_text
    from report_server.tools_resources import (
        build_dashboard_text, theme_report_text)

    assert "(none given" in bulk_measures_text("   ")
    assert "(none given" in build_dashboard_text("")
    assert "#1F3A5F" in theme_report_text("  ")
    assert "Spec text" in bulk_measures_text("  Spec text \n")
