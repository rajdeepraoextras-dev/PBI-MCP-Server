"""Alt text, tab order, accessibility report and its lint integration."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from core import accessibility as a11y
from core import schema_validate as sv
from core.journal import snapshot
from core.lint import lint_page
from core.pbip import PbipProject
from core.theme import generate_theme
from report_server import tools_accessibility as tacc
from report_server.server import ReportState, set_project

SYNTH = Path(__file__).parent / "fixtures" / "synthetic"

pytestmark = pytest.mark.skipif(not sv.is_available(),
                                reason="jsonschema/schemas unavailable")


@pytest.fixture
def state(tmp_path) -> ReportState:
    dst = tmp_path / "s"
    shutil.copytree(SYNTH, dst)
    st = ReportState()
    set_project(st, str(dst / "Synthetic.pbip"))
    return st


def _vfile(state, page, vid) -> Path:
    return (state.project._require_report() / "definition" / "pages" / page
            / "visuals" / vid / "visual.json")


def _visual(state, page, vid) -> dict:
    return json.loads(_vfile(state, page, vid).read_text("utf-8"))


def _codes(findings, code):
    return [f for f in findings if f["code"] == code]


# --- colour maths ----------------------------------------------------------------

def test_contrast_ratio_known_values():
    assert a11y.contrast_ratio("#000000", "#FFFFFF") == pytest.approx(21.0)
    assert a11y.contrast_ratio("#FFFFFF", "#FFFFFF") == pytest.approx(1.0)
    # the classic AA borderline: #767676 on white is 4.54:1
    assert a11y.contrast_ratio("#767676", "#FFFFFF") == pytest.approx(4.54, abs=0.01)
    assert a11y.contrast_ratio("#777777", "#FFFFFF") < 4.5
    assert a11y.contrast_ratio("#252423", "#FFFFFF") > 15
    assert a11y.best_text_color("#000000") == "#FFFFFF"
    assert a11y.best_text_color("#FFFFFF") == "#000000"
    assert a11y.parse_hex("#abc") == (170, 187, 204)
    assert a11y.parse_hex("red") is None


# --- alt text --------------------------------------------------------------------------

def test_set_alt_text_uses_general_alt_text_path_and_validates(state):
    res = tacc.set_alt_text(state, "overview", "bar1", "Revenue by year bars")
    assert res["alt_text"] == "Revenue by year bars"
    data = _visual(state, "overview", "bar1")
    prop = data["visual"]["visualContainerObjects"]["general"][0]["properties"]
    assert prop["altText"] == {"expr": {"Literal": {"Value": "'Revenue by year bars'"}}}
    # title formatting sharing the container objects is untouched
    assert data["visual"]["visualContainerObjects"]["title"]
    assert sv.validate("visualContainer", data) == []
    assert state.project.validate_project()["ok"]


def test_alt_text_quotes_round_trip_and_clear(state):
    tacc.set_alt_text(state, "overview", "card1", "Owner's revenue")
    data = _visual(state, "overview", "card1")
    assert a11y.get_alt_text(data) == "Owner's revenue"
    tacc.set_alt_text(state, "overview", "card1", "")
    data = _visual(state, "overview", "card1")
    assert a11y.get_alt_text(data) is None
    assert "general" not in data["visual"]["visualContainerObjects"]
    assert sv.validate("visualContainer", data) == []


def test_alt_text_errors(state):
    with pytest.raises(ValueError, match="not found"):
        tacc.set_alt_text(state, "nope", "bar1", "x")
    with pytest.raises(ValueError, match="not on page"):
        tacc.set_alt_text(state, "overview", "ghost", "x")
    gid = state.project.group_visuals("overview", ["card1", "bar1"], "G")
    with pytest.raises(ValueError, match="visual group"):
        tacc.set_alt_text(state, "overview", gid, "x")


def test_auto_alt_text_generates_from_bindings_and_titles(state):
    res = tacc.auto_alt_text(state, "overview")
    got = {r["visual_id"]: r["alt_text"] for r in res["set"]}
    assert got == {
        "bar1": "Clustered bar chart of Net Revenue by Year",
        "card1": "Total Revenue: Card of Net Revenue",
        "table1": "Table of Net Revenue and Margin %",
    }
    for vid, text in got.items():
        assert a11y.get_alt_text(_visual(state, "overview", vid)) == text
    assert state.project.validate_project()["ok"]


def test_auto_alt_text_keeps_existing_unless_overwrite(state):
    tacc.set_alt_text(state, "overview", "bar1", "Hand written")
    res = tacc.auto_alt_text(state, "overview")
    assert res["skipped_existing"] == ["bar1"]
    assert a11y.get_alt_text(_visual(state, "overview", "bar1")) == "Hand written"
    # idempotent: a second run has nothing left to do
    assert tacc.auto_alt_text(state, "overview")["set"] == []
    res = tacc.auto_alt_text(state, "overview", overwrite=True)
    assert {r["visual_id"] for r in res["set"]} == {"bar1", "card1", "table1"}
    assert a11y.get_alt_text(_visual(state, "overview", "bar1")) \
        == "Clustered bar chart of Net Revenue by Year"


def test_auto_alt_text_skips_decor_and_text_boxes(state):
    p = state.project
    p.add_shape("overview", fill="#EEEEEE",
                position={"x": 0, "y": 0, "width": 100, "height": 50})
    p.add_text("overview", "Hello", position={"x": 0, "y": 600, "width": 100,
                                              "height": 40})
    res = tacc.auto_alt_text(state, "overview")
    assert {r["visual_id"] for r in res["set"]} == {"bar1", "card1", "table1"}


# --- tab order --------------------------------------------------------------------------

def _tab(state, page):
    return {v.id: v.raw["position"].get("tabOrder")
            for v in state.project.list_visuals(page)}


def test_set_tab_order_numbers_listed_then_rest(state):
    res = tacc.set_tab_order(state, "overview", ["table1", "card1"])
    assert res["order"] == ["table1", "card1", "bar1"]
    assert _tab(state, "overview") == {"table1": 0, "card1": 1, "bar1": 2}
    assert state.project.validate_project()["ok"]
    # idempotent: nothing changes the second time
    assert tacc.set_tab_order(state, "overview", ["table1", "card1"])["changed"] == []


@pytest.mark.parametrize("order, match", [
    (["bar1", "bar1"], "more than once"),
    (["ghost"], "Unknown visual id"),
    ([], "non-empty"),
])
def test_set_tab_order_errors(state, order, match):
    with pytest.raises(ValueError, match=match):
        tacc.set_tab_order(state, "overview", order)


def test_auto_tab_order_is_reading_order_decor_last(state):
    p = state.project
    p.add_shape("overview", fill="#EEEEEE",
                position={"x": 0, "y": 0, "width": 1280, "height": 100})
    res = tacc.auto_tab_order(state, "overview")
    order = res["order"]
    # card (y16) -> bar (y140,x16) -> table (y140,x640) -> the decorative shape
    assert order[:3] == ["card1", "bar1", "table1"]
    assert order[3].startswith("shape")
    tabs = _tab(state, "overview")
    assert [tabs[v] for v in order] == [0, 1, 2, 3]
    assert tacc.auto_tab_order(state, "overview")["changed"] == []


def test_reading_order_groups_rows_with_jitter(state):
    p = state.project
    p.add_visual("details", {"visual_type": "card", "id": "right",
                             "bindings": {"Values": ["Sales.Net Revenue"]},
                             "position": {"x": 400, "y": 20, "width": 100,
                                          "height": 50}})
    p.add_visual("details", {"visual_type": "card", "id": "left",
                             "bindings": {"Values": ["Sales.Net Revenue"]},
                             "position": {"x": 300, "y": 26, "width": 100,
                                          "height": 50}})
    order = [v.id for v in a11y.reading_order(p.list_visuals("details"))]
    # slicer1 (x16,y16) first; 'left' (y26) is in the same row band as 'right'
    assert order == ["slicer1", "left", "right"]


# --- accessibility report ---------------------------------------------------------------

def test_report_flags_missing_alt_and_duplicate_tab_order(state):
    for vid in ("card1", "bar1", "table1"):
        state.project.move_visual("overview", vid)  # no-op write keeps files valid
    data = _visual(state, "overview", "table1")
    data["position"]["tabOrder"] = 1            # duplicate of bar1
    state.project._write_json(_vfile(state, "overview", "table1"), data)

    rep = tacc.accessibility_report(state, "overview")
    assert rep["ok"] is False
    alt = _codes(rep["findings"], "missing_alt_text")
    assert {f["visual_id"] for f in alt} == {"card1", "bar1", "table1"}
    dup = _codes(rep["findings"], "duplicate_tab_order")
    assert len(dup) == 1 and "bar1" in dup[0]["message"] and "table1" in dup[0]["message"]
    for f in rep["findings"]:
        assert {"severity", "visual_id", "message", "fix"} <= set(f)
    assert rep["theme"]["source"] == "default"


def test_report_clean_after_fixes(state):
    tacc.auto_alt_text(state, "overview")
    tacc.auto_tab_order(state, "overview")
    rep = tacc.accessibility_report(state, "overview")
    assert rep["ok"] and rep["findings"] == []


def test_report_tab_order_gaps_are_info(state):
    for vid, n in (("card1", 0), ("bar1", 5), ("table1", 9)):
        d = _visual(state, "overview", vid)
        d["position"]["tabOrder"] = n
        state.project._write_json(_vfile(state, "overview", vid), d)
    gaps = _codes(tacc.accessibility_report(state, "overview")["findings"],
                  "tab_order_gaps")
    assert len(gaps) == 1 and gaps[0]["severity"] == "info"


def test_report_small_text(state):
    state.project.format_visual("overview", "card1", "container",
                                {"title": {"fontSize": 8}})
    state.project.format_visual("overview", "bar1", "visual",
                                {"labels": {"fontSize": 9}})
    small = _codes(tacc.accessibility_report(state, "overview")["findings"],
                   "small_text")
    assert [f["visual_id"] for f in small] == ["card1"]
    assert "8 pt" in small[0]["message"] and "title.fontSize" in small[0]["message"]


def test_report_small_text_in_textbox_runs(state):
    state.project.add_text("overview", [{"text": "fine print", "size": 7}],
                           position={"x": 0, "y": 600, "width": 200, "height": 30})
    small = _codes(tacc.accessibility_report(state, "overview")["findings"],
                   "small_text")
    assert len(small) == 1 and "7 pt" in small[0]["message"]


def test_report_low_contrast_against_default_white(state):
    state.project.format_visual("overview", "card1", "container",
                                {"title": {"fontColor": "#CCCCCC"}})
    state.project.format_visual("overview", "bar1", "container",
                                {"title": {"fontColor": "#252423"}})
    low = _codes(tacc.accessibility_report(state, "overview")["findings"],
                 "low_contrast")
    assert [f["visual_id"] for f in low] == ["card1"]
    assert "#CCCCCC on #FFFFFF" in low[0]["message"]
    assert "#000000" in low[0]["fix"]


def test_report_contrast_uses_container_background_and_transparency(state):
    p = state.project
    p.format_visual("overview", "card1", "container",
                    {"title": {"fontColor": "#FFFFFF"},
                     "background": {"color": "#1F3A5F", "transparency": 0}})
    p.format_visual("overview", "bar1", "container",
                    {"title": {"fontColor": "#FFFFFF"},
                     "background": {"color": "#1F3A5F", "transparency": 100}})
    low = _codes(tacc.accessibility_report(state, "overview")["findings"],
                 "low_contrast")
    # card: white on navy is fine; bar: fully transparent -> white on white
    assert [f["visual_id"] for f in low] == ["bar1"]


def test_report_uses_theme_and_page_background(state):
    p = state.project
    p.set_report_theme(generate_theme("#1F3A5F", name="Dark", mode="dark"))
    theme = a11y.theme_colors(p)
    assert theme["source"] == "theme" and theme["foreground"] == "#F3F2F1"

    # dark theme, no page override: default light text on dark bg -> fine
    assert not _codes(tacc.accessibility_report(state, "overview")["findings"],
                      "low_contrast")
    # a white page background under the dark theme's light default text
    p.style_page("overview", background_color="#FFFFFF")
    low = _codes(tacc.accessibility_report(state, "overview")["findings"],
                 "low_contrast")
    assert {f["visual_id"] for f in low} == {"card1", "bar1", "table1"}
    assert "default text #F3F2F1 on #FFFFFF" in low[0]["message"]


def test_report_ignores_hidden_title_and_unresolvable_colours(state):
    p = state.project
    p.format_visual("overview", "card1", "container",
                    {"title": {"show": False, "fontColor": "#FFFFFF"}})
    p.format_visual("overview", "bar1", "container", {"title": {
        "fontColor": {"solid": {"color": {"expr": {"ThemeDataColor": {
            "ColorId": 0, "Percent": 0}}}}}}})
    assert not _codes(tacc.accessibility_report(state, "overview")["findings"],
                      "low_contrast")


def test_button_text_is_judged_against_its_fill(state):
    state.project.add_nav_button("overview", "Go", "details",
                                 fill="#FFFF00", text_color="#FFFFFF",
                                 position={"x": 0, "y": 600, "width": 100,
                                           "height": 30})
    low = _codes(tacc.accessibility_report(state, "overview")["findings"],
                 "low_contrast")
    assert len(low) == 1 and "#FFFFFF on #FFFF00" in low[0]["message"]


def test_emitted_json_stays_schema_valid(state):
    tacc.auto_alt_text(state, "overview")
    tacc.auto_alt_text(state, "details")
    tacc.set_tab_order(state, "overview", ["table1", "bar1", "card1"])
    tacc.auto_tab_order(state, "details")
    for page, vid in (("overview", "bar1"), ("overview", "card1"),
                      ("overview", "table1"), ("details", "slicer1")):
        assert sv.validate("visualContainer", _visual(state, page, vid)) == []
    assert state.project.validate_project()["ok"]


def test_tab_order_on_page_with_a_group(state):
    p = state.project
    gid = p.group_visuals("overview", ["card1", "bar1"], "G")
    res = tacc.auto_tab_order(state, "overview")
    assert gid in res["order"]
    assert state.project.validate_project()["ok"]


# --- lint integration ---------------------------------------------------------------------

def _lint(state, page, **kw):
    project = PbipProject(state.project.path)
    pages = {p.id: p for p in project.list_pages()}
    return lint_page(pages[page], project.list_visuals(page), **kw)


def test_lint_appends_accessibility_warnings_without_changing_design_findings(state):
    with_a11y = _lint(state, "overview")
    design_only = _lint(state, "overview", accessibility=False)
    assert with_a11y[:len(design_only)] == design_only     # untouched, first
    extra = with_a11y[len(design_only):]
    assert extra and all(f["code"].startswith("a11y_") for f in extra)
    assert all(f["severity"] == "warning" for f in extra)
    assert {f["code"] for f in extra} >= {"a11y_missing_alt_text"}
    assert all("fix" in f and f["visuals"] is not None for f in extra)


def test_lint_a11y_clears_after_fixes(state):
    tacc.auto_alt_text(state, "overview")
    tacc.auto_tab_order(state, "overview")
    assert [f for f in _lint(state, "overview")
            if f["code"].startswith("a11y_")] == []


def test_lint_context_free_never_guesses_a_background(state):
    state.project.format_visual("overview", "card1", "container",
                                {"title": {"fontColor": "#CCCCCC"}})
    # unknown page background: no contrast verdict in context-free mode ...
    assert not [f for f in _lint(state, "overview")
                if f["code"] == "a11y_low_contrast"]
    # ... but with the project (page + theme known) it is reported
    project = PbipProject(state.project.path)
    pages = {p.id: p for p in project.list_pages()}
    found = lint_page(pages["overview"], project.list_visuals("overview"),
                      project=project)
    assert [f for f in found if f["code"] == "a11y_low_contrast"]
    # an explicit opaque container background is judged even without context
    state.project.format_visual("overview", "card1", "container",
                                {"background": {"color": "#FFFFFF",
                                                "transparency": 0}})
    assert [f for f in _lint(state, "overview") if f["code"] == "a11y_low_contrast"]


def test_pbi_lint_page_tool_includes_accessibility(tmp_path):
    from tests.test_tooling import _payload

    import report_server.server as mod

    proj = tmp_path / "p"
    shutil.copytree(SYNTH, proj)

    def call(tool_name, **args):
        return _payload(asyncio.run(mod.mcp.call_tool(tool_name, args)))

    call("pbi_set_project", path=str(proj / "Synthetic.pbip"))
    res = call("pbi_lint_page", page_id="overview")
    codes = {f["code"] for f in res["findings"]}
    assert "a11y_missing_alt_text" in codes and res["ok"] is False


# --- through the MCP server ------------------------------------------------------------------

def test_accessibility_tools_registered_and_undoable(tmp_path):
    from tests.test_tooling import _ann, _payload, _tools

    import report_server.server as mod

    tools = _tools(mod)
    for name in ("pbi_set_alt_text", "pbi_auto_alt_text", "pbi_set_tab_order",
                 "pbi_auto_tab_order", "pbi_accessibility_report"):
        assert name in tools, name
    assert _ann(tools["pbi_accessibility_report"], "readOnlyHint") is True
    assert _ann(tools["pbi_set_alt_text"], "readOnlyHint") is False

    proj = tmp_path / "p"
    shutil.copytree(SYNTH, proj)

    def call(tool_name, **args):
        return _payload(asyncio.run(mod.mcp.call_tool(tool_name, args)))

    call("pbi_set_project", path=str(proj / "Synthetic.pbip"))
    before = snapshot(proj)
    prev = call("pbi_auto_alt_text", page_id="overview", dry_run=True)
    assert prev["dry_run"] and "altText" in prev["diff"]
    assert snapshot(proj) == before
    call("pbi_auto_alt_text", page_id="overview")
    call("pbi_auto_tab_order", page_id="overview")
    assert call("pbi_accessibility_report", page_id="overview")["ok"] is True
    call("pbi_undo", steps=2)
    assert snapshot(proj) == before
