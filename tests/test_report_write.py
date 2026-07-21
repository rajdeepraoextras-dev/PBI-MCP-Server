"""Days 19-20: report write — emit a card and a bar chart via the server tools.

Runs on copies of the REAL HR project. The emitted visual.json is checked
structurally against visuals Desktop itself wrote in the same project
(same top-level keys, same projection field shape).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from core.pbip import PbipProject
from core.pbir import visual_bindings
from report_server.server import (
    ReportState, add_visual, create_page, set_project,
)

REAL_HR_DIR = Path(__file__).parent / "fixtures" / "real" / "hr-sample"
SYNTH_DIR = Path(__file__).parent / "fixtures" / "synthetic"
SRC = REAL_HR_DIR if REAL_HR_DIR.exists() else SYNTH_DIR


@pytest.fixture
def state(tmp_path) -> ReportState:
    dst = tmp_path / SRC.name
    shutil.copytree(SRC, dst)
    st = ReportState()
    set_project(st, str(next(dst.glob("*.pbip"))))
    return st


def _a_measure_ref(project: PbipProject) -> str:
    m = project.list_measures()[0]
    return f"{m.table}.{m.name}"


def _a_column_ref(project: PbipProject) -> str:
    for t in project.list_tables():
        for c in t.columns:
            return f"{t.name}.{c.name}"
    raise AssertionError("no columns")


# --- Day 19: card ---------------------------------------------------------

def test_emit_card(state):
    project = state.project
    ref = _a_measure_ref(project)
    page = create_page(state, "QA Card Page")
    res = add_visual(state, page["page_id"], [{
        "visual_type": "card",
        "bindings": {"Values": [ref]},
        "title": "QA Card",
        "position": {"x": 40, "y": 40, "width": 240, "height": 120},
    }])
    assert res["count"] == 1
    vid = res["visual_ids"][0]

    reopened = PbipProject(project.path)
    v = reopened.get_visual(page["page_id"], vid)
    assert v.visual_type == "card"
    assert v.title == "QA Card"
    assert visual_bindings(v)["Values"] == [ref]
    proj0 = v.raw["visual"]["query"]["queryState"]["Values"]["projections"][0]
    assert "Measure" in proj0["field"]  # measure bound with Measure kind


def test_emitted_card_shape_matches_desktops(state):
    """Same top-level keys + projection shape as a Desktop-written visual."""
    project = state.project
    # find a real Desktop visual with a queryState to compare against
    desktop_visual = None
    for pg in project.list_pages():
        for v in project.list_visuals(pg.id):
            qs = v.raw.get("visual", {}).get("query", {}).get("queryState", {})
            if qs:
                desktop_visual = v
                break
        if desktop_visual:
            break
    assert desktop_visual is not None

    page = create_page(state, "Shape QA")
    res = add_visual(state, page["page_id"], [{
        "visual_type": "card",
        "bindings": {"Values": [_a_measure_ref(project)]},
    }])
    mine = PbipProject(project.path).get_visual(
        page["page_id"], res["visual_ids"][0])

    assert set(mine.raw) >= {"$schema", "name", "position", "visual"}
    dt_bucket = next(iter(desktop_visual.raw["visual"]["query"]["queryState"].values()))
    my_bucket = mine.raw["visual"]["query"]["queryState"]["Values"]
    dt_proj = dt_bucket["projections"][0]
    my_proj = my_bucket["projections"][0]
    assert set(my_proj) >= {"field", "queryRef"}
    # field object nests Expression.SourceRef.Entity + Property, like Desktop's
    dt_kind = next(iter(dt_proj["field"].values()))
    my_kind = next(iter(my_proj["field"].values()))
    assert set(my_kind) == set(dt_kind) == {"Expression", "Property"}


# --- Day 20: bar chart -------------------------------------------------------

def test_emit_bar_chart(state):
    project = state.project
    mref = _a_measure_ref(project)
    cref = _a_column_ref(project)
    page = create_page(state, "QA Bar Page")
    res = add_visual(state, page["page_id"], [{
        "visual_type": "clusteredBarChart",
        "bindings": {"Category": [cref], "Y": [mref]},
        "title": "QA Bar",
        "position": {"x": 40, "y": 40, "width": 600, "height": 360},
    }])
    vid = res["visual_ids"][0]
    v = PbipProject(project.path).get_visual(page["page_id"], vid)
    binds = visual_bindings(v)
    assert binds["Category"] == [cref]
    assert binds["Y"] == [mref]
    qs = v.raw["visual"]["query"]["queryState"]
    assert "Column" in qs["Category"]["projections"][0]["field"]
    assert "Measure" in qs["Y"]["projections"][0]["field"]


def test_batch_two_visuals_one_call(state):
    project = state.project
    page = create_page(state, "QA Batch")
    res = add_visual(state, page["page_id"], [
        {"visual_type": "card",
         "bindings": {"Values": [_a_measure_ref(project)]}},
        {"visual_type": "clusteredBarChart",
         "bindings": {"Category": [_a_column_ref(project)],
                      "Y": [_a_measure_ref(project)]}},
    ])
    assert res["count"] == 2
    assert len(PbipProject(project.path)
               .list_visuals(page["page_id"])) == 2


# --- validation guards -----------------------------------------------------------

def test_wrong_bucket_rejected_before_write(state):
    page = create_page(state, "QA Guard")
    with pytest.raises(ValueError, match="unknown bucket"):
        add_visual(state, page["page_id"], [{
            "visual_type": "card",
            "bindings": {"Fields": [_a_measure_ref(state.project)]},
        }])
    assert PbipProject(state.project.path) \
        .list_visuals(page["page_id"]) == []


def test_missing_required_bucket_rejected(state):
    page = create_page(state, "QA Guard2")
    with pytest.raises(ValueError, match="missing required"):
        add_visual(state, page["page_id"], [{
            "visual_type": "clusteredBarChart",
            "bindings": {"Y": [_a_measure_ref(state.project)]},
        }])


def test_unknown_field_rejected(state):
    page = create_page(state, "QA Guard3")
    with pytest.raises(KeyError, match="Unknown model field"):
        add_visual(state, page["page_id"], [{
            "visual_type": "card",
            "bindings": {"Values": ["Ghost.Not Real"]},
        }])


def test_unknown_page_rejected(state):
    with pytest.raises(KeyError, match="not found"):
        add_visual(state, "no-such-page", [{
            "visual_type": "card",
            "bindings": {"Values": [_a_measure_ref(state.project)]},
        }])


def test_batch_atomic_validation(state):
    """A bad spec anywhere in the batch means nothing is written."""
    page = create_page(state, "QA Guard4")
    with pytest.raises(ValueError):
        add_visual(state, page["page_id"], [
            {"visual_type": "card",
             "bindings": {"Values": [_a_measure_ref(state.project)]}},
            {"visual_type": "card", "bindings": {}},  # invalid
        ])
    assert PbipProject(state.project.path) \
        .list_visuals(page["page_id"]) == []
