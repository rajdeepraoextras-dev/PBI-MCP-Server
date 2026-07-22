"""Page-style extraction + match_page (copy header/KPI composition)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from core.page_style import extract_page_style
from core.pbip import PbipProject
from report_server.server import (
    ReportState, build_designed_page, set_project,
)

SYNTH = Path(__file__).parent / "fixtures" / "synthetic"


@pytest.fixture
def state(tmp_path) -> ReportState:
    dst = tmp_path / "s"
    shutil.copytree(SYNTH, dst)
    st = ReportState()
    set_project(st, str(dst / "Synthetic.pbip"))
    return st


def _src(state, accent="#0B7A75"):
    return build_designed_page(
        state, "Src", "Sales Overview",
        kpis=[{"measure": "Sales.Net Revenue", "title": "Rev"},
              {"measure": "Sales.Margin %", "title": "Margin"}],
        charts=[], accent=accent)["page_id"]


# --- extraction --------------------------------------------------------------

def test_extract_header_and_kpi(state):
    pid = _src(state)
    style = extract_page_style(PbipProject(state.project.path), pid)
    assert style["header"]["fill"] == "#0B7A75"
    assert style["header"]["height"] > 0
    assert style["title"]["color"] == "#FFFFFF"
    assert style["kpi"]["backplate"] is True
    assert style["kpi"]["count"] == 2


def test_extract_unknown_page(state):
    with pytest.raises(KeyError):
        extract_page_style(state.project, "nope")


def test_extract_bare_page_has_no_header(state):
    # a plain page (no shapes/cards) yields an empty-ish style
    pid = state.project.create_page("Bare")
    style = extract_page_style(PbipProject(state.project.path), pid)
    assert "header" not in style and "kpi" not in style


# --- z-order regression (backplate must sit BEHIND the card) -----------------

def test_backplate_is_behind_card(state):
    pid = _src(state)
    project = PbipProject(state.project.path)
    visuals = project.list_visuals(pid)
    cards = [v for v in visuals if v.visual_type == "card"]
    shapes = [v for v in visuals if v.visual_type == "shape"]
    for c in cards:
        # a shape that contains this card must have a LOWER z (be behind it)
        behind = [s for s in shapes
                  if s.position.x <= c.position.x
                  and s.position.y <= c.position.y
                  and s.position.z < c.position.z]
        assert behind, f"card {c.id} has no backplate behind it"


# --- match_page --------------------------------------------------------------

def test_match_page_copies_header_color(state):
    src = _src(state, accent="#8E44AD")
    res = build_designed_page(state, "Matched", "Detail",
                              kpis=[{"measure": "Sales.Net Revenue"}],
                              charts=[], match_page=src)
    style = extract_page_style(PbipProject(state.project.path), res["page_id"])
    assert style["header"]["fill"] == "#8E44AD"
    assert style["kpi"]["backplate"] is True


def test_match_page_height_is_stable(state):
    src = _src(state)
    prev, heights = src, []
    for i in range(3):
        r = build_designed_page(state, f"M{i}", "B",
                                kpis=[{"measure": "Sales.Net Revenue"}],
                                charts=[], match_page=prev)
        heights.append(extract_page_style(
            PbipProject(state.project.path), r["page_id"])["kpi"]["height"])
        prev = r["page_id"]
    assert len(set(heights)) == 1, f"height drifts: {heights}"


def test_matched_page_schema_valid(state):
    src = _src(state)
    res = build_designed_page(state, "Matched", "Detail",
                              kpis=[{"measure": "Sales.Net Revenue"}],
                              charts=[{"visual_type": "clusteredBarChart",
                                       "bindings": {"Category": ["Date.Year"],
                                                    "Y": ["Sales.Net Revenue"]}}],
                              match_page=src)
    assert PbipProject(state.project.path).validate_project()["ok"]
