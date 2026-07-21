"""E4 D18-23: sort, aggregations, nav buttons, page roles, interactions,
bookmarks."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from core import schema_validate as sv
from core.pbip import PbipProject
from core.pbir import visual_bindings

SYNTH = Path(__file__).parent / "fixtures" / "synthetic"


@pytest.fixture
def project(tmp_path) -> PbipProject:
    dst = tmp_path / "s"
    shutil.copytree(SYNTH, dst)
    return PbipProject(dst / "Synthetic.pbip")


def _bar(project, page):
    return project.add_visual(page, {
        "visual_type": "clusteredBarChart",
        "bindings": {"Category": ["Date.Year"], "Y": ["Sales.Net Revenue"]}})


# --- D18 sort ----------------------------------------------------------------

def test_sort_visual(project):
    page = project.create_page("Sort")
    vid = _bar(project, page)
    project.sort_visual(page, vid, "Sales.Net Revenue", "Descending")
    raw = PbipProject(project.path).get_visual(page, vid).raw
    sd = raw["visual"]["query"]["sortDefinition"]
    assert sd["sort"][0]["direction"] == "Descending"
    assert sd["sort"][0]["field"]["Measure"]["Property"] == "Net Revenue"
    if sv.is_available():
        assert sv.validate("visualContainer", raw) == []


def test_bad_sort_direction(project):
    page = project.create_page("S2")
    vid = _bar(project, page)
    with pytest.raises(ValueError, match="Ascending or Descending"):
        project.sort_visual(page, vid, "Sales.Net Revenue", "sideways")


# --- D19 aggregation ---------------------------------------------------------

def test_aggregation_binding(project):
    page = project.create_page("Agg")
    vid = project.add_visual(page, {
        "visual_type": "tableEx",
        "bindings": {"Values": ["Date.Year", "Sum(Sales.Amount)"]}})
    raw = PbipProject(project.path).get_visual(page, vid).raw
    projs = raw["visual"]["query"]["queryState"]["Values"]["projections"]
    agg = [p for p in projs if "Aggregation" in p["field"]]
    assert agg and agg[0]["field"]["Aggregation"]["Function"] == 0  # Sum
    if sv.is_available():
        assert sv.validate("visualContainer", raw) == []


# --- D21 nav button ----------------------------------------------------------

def test_nav_button(project):
    p1 = project.create_page("Home")
    p2 = project.create_page("Detail")
    vid = project.add_nav_button(p1, "Go to Detail", p2)
    raw = PbipProject(project.path).get_visual(p1, vid).raw
    link = raw["visual"]["visualContainerObjects"]["visualLink"][0]["properties"]
    assert link["type"]["expr"]["Literal"]["Value"] == "'PageNavigation'"
    assert p2 in link["navigationSection"]["expr"]["Literal"]["Value"]
    if sv.is_available():
        assert sv.validate("visualContainer", raw) == []


def test_nav_button_unknown_target(project):
    p1 = project.create_page("Home2")
    with pytest.raises(KeyError):
        project.add_nav_button(p1, "X", "no-such-page")


# --- D22 page role -----------------------------------------------------------

def test_drillthrough_role(project):
    page = project.create_page("Drill")
    project.set_page_role(page, "drillthrough")
    pj = json.loads((project._require_report() / "definition" / "pages"
                     / page / "page.json").read_text(encoding="utf-8-sig"))
    assert pj["pageBinding"]["type"] == "Drillthrough"
    if sv.is_available():
        assert sv.validate("page", pj) == []


def test_tooltip_role_sets_size(project):
    page = project.create_page("Tip")
    project.set_page_role(page, "tooltip", (400, 300))
    pj = json.loads((project._require_report() / "definition" / "pages"
                     / page / "page.json").read_text(encoding="utf-8-sig"))
    assert pj["pageBinding"]["type"] == "Tooltip"
    assert pj["width"] == 400 and pj["height"] == 300


# --- D23 interactions --------------------------------------------------------

def test_visual_interactions(project):
    page = project.create_page("Inter")
    a = _bar(project, page)
    b = project.add_visual(page, {"visual_type": "card",
        "bindings": {"Values": ["Sales.Net Revenue"]}})
    project.set_visual_interactions(page, a, {b: "NoFilter"})
    pj = json.loads((project._require_report() / "definition" / "pages"
                     / page / "page.json").read_text(encoding="utf-8-sig"))
    vi = pj["visualInteractions"]
    assert any(i["source"] == a and i["target"] == b for i in vi)
    if sv.is_available():
        assert sv.validate("page", pj) == []


# --- D20 bookmark ------------------------------------------------------------

def test_create_bookmark(project):
    from core.formatting import build_filter

    project.add_filter("report", build_filter("Date.Year", values=[2024]))
    res = project.create_bookmark("Focus 2024")
    bm = json.loads((project._require_report() / "definition" / "bookmarks"
                     / res["file"]).read_text(encoding="utf-8-sig"))
    assert bm["displayName"] == "Focus 2024"
    assert "explorationState" in bm
    # registered in metadata
    meta = json.loads((project._require_report() / "definition" / "bookmarks"
                       / "bookmarks.json").read_text(encoding="utf-8-sig"))
    assert any(i["name"] == res["file"][:-5] for i in meta["items"])
