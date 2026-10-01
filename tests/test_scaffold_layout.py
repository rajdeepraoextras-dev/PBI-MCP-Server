"""Scaffold output must pass the plugin's own lint: no overlapping header text,
nav bar clear of the title, no half-empty chart rows, unique page names."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from core.layout import layout_page
from core.scaffold import propose_report
from core.templates import NAV_BTN_H, NAV_BTN_MIN_W, NAV_BTN_W, _header_band, nav_layout

ENGINE = Path(__file__).parent / "fixtures" / "engine"

BAR = {"visual_type": "clusteredBarChart", "bindings": {}}
TABLE = {"visual_type": "tableEx", "bindings": {}}


def _v(spec):
    return dict(spec)


# --- layout: no half-empty rows ----------------------------------------------

def test_lone_chart_before_a_table_spans_the_whole_row():
    laid, _ = layout_page([_v(BAR), _v(TABLE)], width=1280)
    bar, table = laid[0]["position"], laid[1]["position"]
    assert bar["x"] == 16 and bar["width"] == table["width"] > 1200
    assert bar["y"] + bar["height"] <= table["y"]


def test_odd_chart_count_last_chart_spans_the_row():
    laid, _ = layout_page([_v(BAR), _v(BAR), _v(BAR)], width=1280)
    first, second, third = (v["position"] for v in laid)
    assert first["width"] == second["width"] < 700          # pair stays side by side
    assert first["y"] == second["y"] and first["x"] < second["x"]
    assert third["width"] > 1200 and third["y"] > first["y"]


def test_pair_of_charts_is_unchanged():
    laid, _ = layout_page([_v(BAR), _v(BAR)], width=1280)
    a, b = laid[0]["position"], laid[1]["position"]
    assert a["width"] == b["width"] < 700 and a["y"] == b["y"]


def test_single_chart_alone_spans_the_row():
    laid, _ = layout_page([_v(BAR)], width=1280)
    assert laid[0]["position"]["width"] > 1200


# --- header band: text boxes never overlap --------------------------------------

def _boxes(steps):
    return [s["position"] for s in steps if s["op"] == "text"]


def _overlap(a, b):
    return not (a["x"] + a["width"] <= b["x"] or b["x"] + b["width"] <= a["x"]
                or a["y"] + a["height"] <= b["y"] or b["y"] + b["height"] <= a["y"])


@pytest.mark.parametrize("subtitle", ["3 measures", None])
def test_header_title_and_subtitle_do_not_overlap_and_fit_the_band(subtitle):
    steps, band_h = _header_band("Executive Overview", subtitle, 1280, "#1F3A5F", "#252423")
    boxes = _boxes(steps)
    assert len(boxes) == (2 if subtitle else 1)
    for b in boxes:
        assert 0 <= b["y"] and b["y"] + b["height"] <= band_h
    if subtitle:
        assert not _overlap(boxes[0], boxes[1])


def test_header_reserves_room_for_the_nav_bar():
    steps, _ = _header_band("T", "S", 1280, "#1F3A5F", "#252423", reserve_right=450)
    for b in _boxes(steps):
        assert b["x"] + b["width"] <= 1280 - 16 - 450 + 1


# --- scaffold naming ----------------------------------------------------------------

def _profile(dims: dict) -> dict:
    return {
        "measures": [{"table": "Sales", "name": "Total", "role": "currency"}],
        "tables": {t: {"kind": "dimension", "grouping_columns": cols, "date_columns": []}
                   for t, cols in dims.items()},
        "date_tables": [],
        "summary": {"measures": 1, "facts": 1},
    }


def test_duplicate_column_names_get_table_prefixed_page_names():
    proposal = propose_report(_profile({"Customer": ["Name"], "Product": ["Name"]}))
    names = [p["name"] for p in proposal["pages"]]
    assert names == ["Overview", "Customer Name Detail", "Product Name Detail"]
    assert len(set(names)) == len(names)
    titles = [p["title"] for p in proposal["pages"]]
    assert "Customer Name Breakdown" in titles and "Product Name Breakdown" in titles
    charts = [c["title"] for p in proposal["pages"] for c in p["charts"]]
    assert not any(t.endswith(" by Name") for t in charts)


def test_unique_column_names_are_left_alone():
    proposal = propose_report(_profile({"Customer": ["Region"], "Product": ["Category"]}))
    assert [p["name"] for p in proposal["pages"]] == [
        "Overview", "Region Detail", "Category Detail"]


# --- end to end: scaffolded pages pass the plugin's own lint ---------------------------

@pytest.fixture
def scaffolded(tmp_path):
    from report_server.server import ReportState, scaffold_report, set_project
    dst = tmp_path / "engine"
    shutil.copytree(ENGINE, dst)
    state = ReportState()
    set_project(state, str(dst / "Engine.pbip"))
    original = {p.id for p in state.project.list_pages()}
    result = scaffold_report(state)
    return state, result, original


def test_scaffolded_pages_have_unique_names_and_no_layout_warnings(scaffolded):
    from core.lint import lint_page
    state, result, original = scaffolded
    project = state.project
    pages = {p.id: p for p in project.list_pages()}
    built = [pages[pid] for pid in result["pages"]]
    names = [p.name for p in built]
    assert len(set(names)) == len(names), names
    for page in built:
        findings = lint_page(page, project.list_visuals(page.id),
                             page_width=page.width, page_height=page.height,
                             project=project)
        layout = [f for f in findings
                  if not f["code"].startswith("a11y_") and f["severity"] == "warning"]
        assert layout == [], (page.name, layout)


def test_scaffold_nav_bar_sits_right_aligned_clear_of_the_title(scaffolded):
    state, result, _ = scaffolded
    project = state.project
    n_other = len(result["pages"]) - 1
    for pid in result["pages"]:
        vis = project.list_visuals(pid)
        buttons = [v for v in vis if v.visual_type == "actionButton"]
        texts = [v for v in vis if v.visual_type == "textbox"]
        assert len(buttons) == n_other
        left = min(b.position.x for b in buttons)
        right = max(b.position.x + b.position.width for b in buttons)
        btn_w, bar_w = nav_layout(n_other, 1280)
        assert right <= 1280 - 16 + 1
        assert abs((right - left) - bar_w) <= n_other     # rounding of button widths
        assert {b.position.width for b in buttons} == {round(btn_w)}
        assert {b.position.height for b in buttons} == {NAV_BTN_H}
        for t in texts:
            assert t.position.x + t.position.width <= left, (pid, t.position, left)


def test_scaffold_overview_has_no_half_empty_chart_row(scaffolded):
    state, result, _ = scaffolded
    vis = state.project.list_visuals(result["pages"][0])
    charts = [v for v in vis if v.visual_type in ("clusteredBarChart", "lineChart")]
    assert charts
    # a chart alone on its row must not leave the right half empty
    rows: dict[float, list] = {}
    for c in charts:
        rows.setdefault(c.position.y, []).append(c)
    for row in rows.values():
        if len(row) == 1:
            assert row[0].position.width > 1200


# --- nav layout ---------------------------------------------------------------------

def test_nav_layout_uses_full_width_buttons_when_there_is_room():
    assert nav_layout(3, 1280)[0] == NAV_BTN_W
    assert nav_layout(0, 1280) == (NAV_BTN_W, 0)


def test_nav_layout_shrinks_buttons_and_stays_within_its_share():
    w, total = nav_layout(5, 1280)
    assert NAV_BTN_MIN_W <= w < NAV_BTN_W
    assert total <= 1280 * 0.55 + 1
    # never below the minimum, even for absurd page counts
    assert nav_layout(30, 1280)[0] == NAV_BTN_MIN_W


def test_lint_does_not_flag_single_line_text_and_buttons_as_tiny():
    from types import SimpleNamespace

    from core.lint import lint_page

    def vis(kind, w, h, i):
        return SimpleNamespace(id=i, visual_type=kind, raw={}, title=None,
                               position=SimpleNamespace(x=0, y=i * 100, z=0, width=w, height=h))
    page = SimpleNamespace(id="p", width=1280, height=720)
    findings = lint_page(page, [vis("textbox", 1248, 32, 0), vis("actionButton", 170, 32, 1),
                                vis("clusteredBarChart", 40, 30, 2), vis("textbox", 20, 10, 3)],
                         accessibility=False)
    tiny = {f["visuals"][0] for f in findings if f["code"] == "tiny"}
    assert tiny == {2, 3}   # the small chart and the minuscule text box, not the healthy ones
