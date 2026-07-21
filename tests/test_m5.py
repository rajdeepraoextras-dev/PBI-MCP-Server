"""Days 26-29: batch scale, formatting (container+visual), theme, filters."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from core.pbip import PbipProject
from core.usage import classify_usage
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


def _raw(state, page_id, visual_id) -> dict:
    return PbipProject(state.project.path) \
        .get_visual(page_id, visual_id).raw


# --- Day 26: 10 visuals, one call ------------------------------------------------

def test_ten_visual_page_one_call(state):
    mref, m2 = "Sales.Net Revenue", "Sales.Margin %"
    cref = "Date.Year"
    specs = (
        [{"visual_type": "card", "bindings": {"Values": [mref]}}] * 2 +
        [{"visual_type": "clusteredBarChart",
          "bindings": {"Category": [cref], "Y": [mref]}}] * 3 +
        [{"visual_type": "lineChart",
          "bindings": {"Category": [cref], "Y": [m2]}}] * 2 +
        [{"visual_type": "tableEx", "bindings": {"Values": [cref, mref]}}] * 2 +
        [{"visual_type": "slicer", "bindings": {"Values": [cref]}}]
    )
    specs = [dict(s) for s in specs]  # de-alias the * copies
    res = build_page(state, "Big Page", specs)
    assert len(res["visual_ids"]) == 10
    assert len(set(res["visual_ids"])) == 10  # unique ids
    visuals = PbipProject(state.project.path).list_visuals(res["page_id"])
    assert len(visuals) == 10
    # every visual got a position from auto-layout
    for v in visuals:
        assert v.position.width > 0 and v.position.height > 0


# --- Day 27: container formatting ---------------------------------------------------

def test_container_format_title_background_border(state):
    page = create_page(state, "F27")
    res = add_visual(state, page["page_id"], [{
        "visual_type": "card",
        "bindings": {"Values": ["Sales.Net Revenue"]},
        "title": "Original",
    }])
    vid = res["visual_ids"][0]
    state.project.format_visual(page["page_id"], vid, "container", {
        "title": {"text": "Styled", "fontColor": "#FFFFFF",
                  "background": "#1F3A5F", "fontSize": 14},
        "background": {"color": "#F5F7FA", "transparency": 0.0},
        "border": {"show": True, "color": "#CCCCCC"},
    })
    raw = _raw(state, page["page_id"], vid)
    vco = raw["visual"]["visualContainerObjects"]
    title_props = vco["title"][0]["properties"]
    assert title_props["text"]["expr"]["Literal"]["Value"] == "'Styled'"
    assert title_props["fontColor"]["solid"]["color"]["expr"]["Literal"] \
        ["Value"] == "'#FFFFFF'"
    assert title_props["fontSize"]["expr"]["Literal"]["Value"] == "14L"
    assert vco["border"][0]["properties"]["show"]["expr"]["Literal"] \
        ["Value"] == "true"


def test_container_format_merges_not_replaces(state):
    page = create_page(state, "F27b")
    res = add_visual(state, page["page_id"], [{
        "visual_type": "card",
        "bindings": {"Values": ["Sales.Net Revenue"]},
        "title": "Keep",
    }])
    vid = res["visual_ids"][0]
    state.project.format_visual(page["page_id"], vid, "container",
                                {"title": {"fontSize": 16}})
    props = _raw(state, page["page_id"], vid)["visual"] \
        ["visualContainerObjects"]["title"][0]["properties"]
    assert props["text"]["expr"]["Literal"]["Value"] == "'Keep'"  # survived
    assert props["fontSize"]["expr"]["Literal"]["Value"] == "16L"


# --- Day 28: visual-content formatting ---------------------------------------------

def test_visual_format_objects(state):
    page = create_page(state, "F28")
    res = add_visual(state, page["page_id"], [{
        "visual_type": "clusteredBarChart",
        "bindings": {"Category": ["Date.Year"], "Y": ["Sales.Net Revenue"]},
    }])
    vid = res["visual_ids"][0]
    state.project.format_visual(page["page_id"], vid, "visual", {
        "labels": {"show": True, "color": "#252423", "fontSize": 10},
        "legend": {"show": False},
        "categoryAxis": {"show": True},
    })
    objs = _raw(state, page["page_id"], vid)["visual"]["objects"]
    assert objs["labels"][0]["properties"]["show"]["expr"]["Literal"] \
        ["Value"] == "true"
    assert objs["legend"][0]["properties"]["show"]["expr"]["Literal"] \
        ["Value"] == "false"
    # queryState untouched by formatting
    assert "queryState" in _raw(state, page["page_id"], vid)["visual"]["query"]


def test_bad_target_rejected(state):
    page = create_page(state, "F28b")
    res = add_visual(state, page["page_id"], [{
        "visual_type": "card", "bindings": {"Values": ["Sales.Net Revenue"]},
    }])
    with pytest.raises(ValueError, match="container.*visual|visual.*container"):
        state.project.format_visual(page["page_id"], res["visual_ids"][0],
                                    "chrome", {"title": {}})


# --- Day 29: theme ---------------------------------------------------------------

def test_set_report_theme(state):
    theme = {"name": "MCP Corporate", "dataColors": ["#1F3A5F", "#5B8DB8"],
             "background": "#FFFFFF", "foreground": "#252423"}
    res = state.project.set_report_theme(theme)
    report_dir = state.project._require_report()
    theme_file = (report_dir / "StaticResources" / "RegisteredResources"
                  / "MCP Corporate.json")
    assert theme_file.exists()
    assert json.loads(theme_file.read_text(encoding="utf-8"))["dataColors"] \
        == ["#1F3A5F", "#5B8DB8"]
    report = json.loads((report_dir / "definition" / "report.json")
                        .read_text(encoding="utf-8-sig"))
    assert report["themeCollection"]["customTheme"] == {
        "name": "MCP Corporate.json", "type": "RegisteredResources"}


def test_theme_requires_name(state):
    with pytest.raises(ValueError, match="name"):
        state.project.set_report_theme({"dataColors": []})


# --- Day 29: filters ----------------------------------------------------------------

def test_categorical_filter_report_scope(state):
    from core.formatting import build_filter

    entry = build_filter("Date.Year", filter_type="Categorical",
                         values=[2023, 2024])
    state.project.add_filter("report", entry)
    report = json.loads(
        (state.project._require_report() / "definition" / "report.json")
        .read_text(encoding="utf-8-sig"))
    flt = report["filterConfig"]["filters"][-1]
    assert flt["type"] == "Categorical"
    cond = flt["filter"]["Where"][0]["Condition"]["In"]
    assert cond["Values"] == [[{"Literal": {"Value": "2023L"}}],
                              [{"Literal": {"Value": "2024L"}}]]
    assert flt["filter"]["From"][0]["Entity"] == "Date"


def test_advanced_filter_page_scope(state):
    from core.formatting import build_filter

    page = create_page(state, "F29")
    entry = build_filter("Sales.Amount", filter_type="Advanced",
                         comparison="gt", comparison_value=100.0)
    state.project.add_filter("page", entry, page_id=page["page_id"])
    page_json = json.loads(
        (state.project._require_report() / "definition" / "pages"
         / page["page_id"] / "page.json").read_text(encoding="utf-8-sig"))
    cond = page_json["filterConfig"]["filters"][0]["filter"]["Where"][0] \
        ["Condition"]["Comparison"]
    assert cond["ComparisonKind"] == 1
    assert cond["Right"]["Literal"]["Value"] == "100.0D"


def test_topn_filter_visual_scope(state):
    from core.formatting import build_filter

    page = create_page(state, "F29b")
    res = add_visual(state, page["page_id"], [{
        "visual_type": "clusteredBarChart",
        "bindings": {"Category": ["Date.Year"], "Y": ["Sales.Net Revenue"]},
    }])
    entry = build_filter("Date.Year", filter_type="TopN", top_n=5,
                         order_by="Sales.Net Revenue")
    state.project.add_filter("visual", entry, page_id=page["page_id"],
                             visual_id=res["visual_ids"][0])
    raw = _raw(state, page["page_id"], res["visual_ids"][0])
    top = raw["filterConfig"]["filters"][0]["filter"]["Where"][0] \
        ["Condition"]["Top"]
    assert top["Count"] == 5
    assert top["OrderBy"][0]["Expression"]["Measure"]["Property"] \
        == "Net Revenue"


def test_filter_fields_count_as_direct_usage(state):
    """A field used only in a filter must classify as direct, not unused."""
    from core.formatting import build_filter

    # Sales.Order Count is 'unused' in the base fixture
    entry = build_filter("Sales.Order Count", filter_type="Advanced",
                         comparison="gt", comparison_value=0)
    state.project.add_filter("report", entry)
    usage = classify_usage(PbipProject(state.project.path))
    assert "Sales.Order Count" in usage["direct"]["columns"]


def test_filter_unknown_scope_rejected(state):
    from core.formatting import build_filter

    entry = build_filter("Date.Year", values=[2024])
    with pytest.raises(ValueError, match="scope"):
        state.project.add_filter("universe", entry)
