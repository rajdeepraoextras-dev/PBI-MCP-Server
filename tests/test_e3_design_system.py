"""E3 D12-17: theme generator, layout engine, templates, KPI presets, lint."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from core import schema_validate as sv
from core.layout import Grid, layout_page
from core.lint import lint_page
from core.pbip import PbipProject
from core.theme import categorical_palette, generate_theme
from report_server.server import (
    ReportState, build_designed_page, set_project,
)

SYNTH = Path(__file__).parent / "fixtures" / "synthetic"
HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")


@pytest.fixture
def state(tmp_path) -> ReportState:
    dst = tmp_path / "s"
    shutil.copytree(SYNTH, dst)
    st = ReportState()
    set_project(st, str(dst / "Synthetic.pbip"))
    return st


# --- D12 theme ---------------------------------------------------------------

def test_palette_unique_valid():
    pal = categorical_palette("#1F3A5F", 8)
    assert len(pal) == 8 and len(set(pal)) == 8
    assert all(HEX.match(c) for c in pal)
    assert pal[0] == "#1F3A5F"


def test_theme_light_and_dark():
    light = generate_theme("#1F3A5F", mode="light")
    dark = generate_theme("#1F3A5F", mode="dark")
    assert light["background"] == "#FFFFFF"
    assert dark["background"] != "#FFFFFF"
    assert "textClasses" in light and "dataColors" in light


def test_generate_and_install_theme(state):
    import json
    from core.theme import generate_theme
    state.project.set_report_theme(generate_theme("#0B7A75", name="Teal"))
    rep = json.loads((state.project._require_report() / "definition"
                      / "report.json").read_text(encoding="utf-8-sig"))
    assert rep["themeCollection"]["customTheme"]["name"] == "Teal.json"


# --- D13-14 layout -----------------------------------------------------------

def test_grid_spans_no_overlap():
    g = Grid(width=1280)
    left = g.span(0, 6, 0, 100)
    right = g.span(6, 6, 0, 100)
    assert left["x"] + left["width"] <= right["x"] + 1


def test_layout_cards_then_charts():
    visuals = [
        {"visual_type": "card", "bindings": {}},
        {"visual_type": "card", "bindings": {}},
        {"visual_type": "clusteredBarChart", "bindings": {}},
        {"visual_type": "lineChart", "bindings": {}},
    ]
    laid, h = layout_page(visuals, width=1280, height=720)
    cards = [v for v in laid if v["visual_type"] == "card"]
    charts = [v for v in laid if v["visual_type"] != "card"]
    # cards above charts
    assert max(c["position"]["y"] for c in cards) <= \
        min(c["position"]["y"] for c in charts)
    # two charts side by side, no overlap
    a, b = charts[0]["position"], charts[1]["position"]
    assert a["x"] + a["width"] <= b["x"] + 1 or b["x"] + b["width"] <= a["x"] + 1


def test_layout_extends_page_height():
    many = [{"visual_type": "clusteredBarChart", "bindings": {}}
            for _ in range(12)]
    _, h = layout_page(many, width=1280, height=720)
    assert h > 720  # page grew to fit 6 rows of charts


def test_full_width_table_spans_all():
    v = [{"visual_type": "tableEx", "bindings": {}}]
    laid, _ = layout_page(v, width=1280)
    assert laid[0]["position"]["width"] > 1200


# --- D15-16 designed page ----------------------------------------------------

def test_build_designed_page(state):
    res = build_designed_page(
        state, "Exec", "Executive Overview", subtitle="FY26",
        kpis=[{"measure": "Sales.Net Revenue", "title": "Revenue"},
              {"measure": "Sales.Margin %", "title": "Margin"}],
        charts=[{"visual_type": "clusteredBarChart",
                 "bindings": {"Category": ["Date.Year"],
                              "Y": ["Sales.Net Revenue"]}, "title": "By Year"}])
    assert res["ok"]
    project = PbipProject(state.project.path)
    visuals = project.list_visuals(res["page_id"])
    types = [v.visual_type for v in visuals]
    # header shape + KPI backplates + textbox title + cards + chart
    assert types.count("shape") >= 3          # header band + 2 KPI backplates
    assert "textbox" in types                  # title
    assert types.count("card") == 2
    assert "clusteredBarChart" in types
    # whole page schema-valid
    if sv.is_available():
        assert project.validate_project()["ok"]


def test_designed_page_validates_charts_first(state):
    with pytest.raises(ValueError):
        build_designed_page(state, "Bad", "T",
                            charts=[{"visual_type": "clusteredBarChart",
                                     "bindings": {"Y": ["Sales.Net Revenue"]}}])
    # nothing created on failure
    assert "bad" not in {p.id for p in PbipProject(state.project.path).list_pages()}


# --- D17 lint ----------------------------------------------------------------

def test_lint_clean_designed_page(state):
    res = build_designed_page(
        state, "Clean", "Clean",
        kpis=[{"measure": "Sales.Net Revenue"}],
        charts=[{"visual_type": "clusteredBarChart",
                 "bindings": {"Category": ["Date.Year"],
                              "Y": ["Sales.Net Revenue"]}}])
    project = PbipProject(state.project.path)
    pages = {p.id: p for p in project.list_pages()}
    findings = lint_page(pages[res["page_id"]],
                         project.list_visuals(res["page_id"]))
    overlaps = [f for f in findings if f["code"] == "overlap"]
    assert not overlaps, overlaps


def test_lint_catches_overlap(state):
    page = state.project.create_page("Overlap")
    state.project.add_visual(page, {"visual_type": "card",
        "bindings": {"Values": ["Sales.Net Revenue"]},
        "position": {"x": 0, "y": 0, "width": 300, "height": 200}})
    state.project.add_visual(page, {"visual_type": "card",
        "bindings": {"Values": ["Sales.Margin %"]},
        "position": {"x": 50, "y": 50, "width": 300, "height": 200}})
    project = PbipProject(state.project.path)
    pages = {p.id: p for p in project.list_pages()}
    findings = lint_page(pages[page], project.list_visuals(page))
    assert any(f["code"] == "overlap" for f in findings)


def test_lint_ignores_backplate_overlap(state):
    """A shape backplate behind a card is intentional, not an overlap."""
    page = state.project.create_page("Backplate")
    state.project.add_shape(page, "rectangle", fill="#EEE",
        position={"x": 0, "y": 0, "width": 300, "height": 200}, z=0)
    state.project.add_visual(page, {"visual_type": "card",
        "bindings": {"Values": ["Sales.Net Revenue"]},
        "position": {"x": 10, "y": 10, "width": 280, "height": 180}})
    project = PbipProject(state.project.path)
    pages = {p.id: p for p in project.list_pages()}
    findings = lint_page(pages[page], project.list_visuals(page))
    assert not any(f["code"] == "overlap" for f in findings)
