"""Days 21-24: table emit, update/move/delete visual, build_page, gotcha types."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from core.pbip import PbipProject
from core.pbir import visual_bindings
from report_server.server import (
    ReportState, add_visual, build_page, create_page, set_project,
)

SYNTH = Path(__file__).parent / "fixtures" / "synthetic"


@pytest.fixture
def state(tmp_path) -> ReportState:
    dst = tmp_path / "synthetic"
    shutil.copytree(SYNTH, dst)
    st = ReportState()
    set_project(st, str(dst / "Synthetic.pbip"))
    return st


def _reload_visual(state, page_id, visual_id):
    return PbipProject(state.project.path).get_visual(page_id, visual_id)


# --- Day 21: table visual ------------------------------------------------------

def test_emit_table_multi_values(state):
    page = create_page(state, "T21")
    res = add_visual(state, page["page_id"], [{
        "visual_type": "tableEx",
        "bindings": {"Values": ["Date.Year", "Sales.Net Revenue",
                                "Sales.Margin %"]},
    }])
    v = _reload_visual(state, page["page_id"], res["visual_ids"][0])
    binds = visual_bindings(v)
    assert binds["Values"] == ["Date.Year", "Sales.Net Revenue",
                               "Sales.Margin %"]
    projections = v.raw["visual"]["query"]["queryState"]["Values"]["projections"]
    kinds = [next(iter(p["field"])) for p in projections]
    assert kinds == ["Column", "Measure", "Measure"]  # order preserved


# --- Day 22: update / move / delete -----------------------------------------------

def test_update_bindings_preserves_position_and_title(state):
    page = create_page(state, "T22")
    res = add_visual(state, page["page_id"], [{
        "visual_type": "card",
        "bindings": {"Values": ["Sales.Net Revenue"]},
        "title": "Keep Me",
        "position": {"x": 99, "y": 88, "width": 240, "height": 120},
    }])
    vid = res["visual_ids"][0]
    state.project.update_bindings(page["page_id"], vid,
                                  {"Values": ["Sales.Margin %"]})
    v = _reload_visual(state, page["page_id"], vid)
    assert visual_bindings(v)["Values"] == ["Sales.Margin %"]
    assert v.title == "Keep Me"
    assert v.position.x == 99 and v.position.y == 88


def test_move_visual_partial(state):
    page = create_page(state, "T22b")
    res = add_visual(state, page["page_id"], [{
        "visual_type": "card",
        "bindings": {"Values": ["Sales.Net Revenue"]},
        "position": {"x": 10, "y": 20, "width": 200, "height": 100},
    }])
    vid = res["visual_ids"][0]
    state.project.move_visual(page["page_id"], vid, x=500, width=300)
    v = _reload_visual(state, page["page_id"], vid)
    assert v.position.x == 500 and v.position.width == 300
    assert v.position.y == 20 and v.position.height == 100  # untouched


def test_delete_visual_recoverable(state):
    page = create_page(state, "T22c")
    res = add_visual(state, page["page_id"], [{
        "visual_type": "card",
        "bindings": {"Values": ["Sales.Net Revenue"]},
    }])
    vid = res["visual_ids"][0]
    out = state.project.delete_visual(page["page_id"], vid)
    assert PbipProject(state.project.path).list_visuals(page["page_id"]) == []
    trash = Path(out["recoverable_at"])
    assert (trash / "visual.json").exists()          # recoverable
    assert ".pbi" in trash.parts                      # invisible to Desktop


def test_delete_missing_visual(state):
    page = create_page(state, "T22d")
    with pytest.raises(FileNotFoundError):
        state.project.delete_visual(page["page_id"], "ghost")


# --- Day 23: build_page one-call flow ------------------------------------------------

def test_build_page_card_bar_table(state):
    res = build_page(state, "Overview QA", [
        {"visual_type": "card",
         "bindings": {"Values": ["Sales.Net Revenue"]}, "title": "Revenue"},
        {"visual_type": "clusteredBarChart",
         "bindings": {"Category": ["Date.Year"], "Y": ["Sales.Net Revenue"]},
         "title": "By Year"},
        {"visual_type": "tableEx",
         "bindings": {"Values": ["Date.Year", "Sales.Margin %"]}},
    ])
    assert len(res["visual_ids"]) == 3
    project = PbipProject(state.project.path)
    visuals = project.list_visuals(res["page_id"])
    assert {v.visual_type for v in visuals} == \
        {"card", "clusteredBarChart", "tableEx"}
    # auto-layout: no overlaps between the two non-card visuals
    charts = [v for v in visuals if v.visual_type != "card"]
    a, b = charts[0].position, charts[1].position
    assert (a.x + a.width <= b.x or b.x + b.width <= a.x
            or a.y + a.height <= b.y or b.y + b.height <= a.y)
    # card sits above the charts
    card = next(v for v in visuals if v.visual_type == "card")
    assert card.position.y <= min(c.position.y for c in charts)


def test_build_page_respects_explicit_position(state):
    res = build_page(state, "Explicit", [
        {"visual_type": "card",
         "bindings": {"Values": ["Sales.Net Revenue"]},
         "position": {"x": 777, "y": 5, "width": 100, "height": 100}},
    ])
    v = _reload_visual(state, res["page_id"], res["visual_ids"][0])
    assert v.position.x == 777


# --- Day 24: gotcha types --------------------------------------------------------------

def test_emit_stacked_vs_clustered(state):
    page = create_page(state, "T24")
    res = add_visual(state, page["page_id"], [
        {"visual_type": "barChart",           # STACKED
         "bindings": {"Category": ["Date.Year"], "Y": ["Sales.Net Revenue"]}},
        {"visual_type": "clusteredBarChart",
         "bindings": {"Category": ["Date.Year"], "Y": ["Sales.Net Revenue"]}},
    ])
    types = [_reload_visual(state, page["page_id"], vid).visual_type
             for vid in res["visual_ids"]]
    assert types == ["barChart", "clusteredBarChart"]


def test_emit_combo_with_y2(state):
    page = create_page(state, "T24b")
    res = add_visual(state, page["page_id"], [{
        "visual_type": "lineClusteredColumnComboChart",
        "bindings": {"Category": ["Date.Year"],
                     "Y": ["Sales.Net Revenue"],
                     "Y2": ["Sales.Margin %"]},
    }])
    v = _reload_visual(state, page["page_id"], res["visual_ids"][0])
    qs = v.raw["visual"]["query"]["queryState"]
    assert set(qs) == {"Category", "Y", "Y2"}


def test_emit_scatter_details(state):
    page = create_page(state, "T24c")
    res = add_visual(state, page["page_id"], [{
        "visual_type": "scatterChart",
        "bindings": {"Details": ["Date.Year"],
                     "X": ["Sales.Net Revenue"], "Y": ["Sales.Margin %"]},
    }])
    v = _reload_visual(state, page["page_id"], res["visual_ids"][0])
    assert "Details" in v.raw["visual"]["query"]["queryState"]


def test_combo_rejects_liney(state):
    page = create_page(state, "T24d")
    with pytest.raises(ValueError, match="unknown bucket"):
        add_visual(state, page["page_id"], [{
            "visual_type": "lineClusteredColumnComboChart",
            "bindings": {"Category": ["Date.Year"],
                         "ColumnY": ["Sales.Net Revenue"],
                         "LineY": ["Sales.Margin %"]},
        }])
