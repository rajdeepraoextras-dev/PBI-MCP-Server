"""core/diff_semantic + pbi_semantic_diff: exact entries, stable order."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import shutil
import stat
from pathlib import Path

import pytest

from core.diff_semantic import semantic_diff
from core.formatting import build_filter
from core.pbip import PbipProject

SYNTH = Path(__file__).parent / "fixtures" / "synthetic"


# --- helpers -------------------------------------------------------------------

@pytest.fixture
def pair(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    shutil.copytree(SYNTH, a)
    shutil.copytree(SYNTH, b)
    return a, b


def proj(root: Path) -> PbipProject:
    return PbipProject(root / "Synthetic.pbip", backups=False)


def diff(pair, **kw) -> dict:
    a, b = pair
    return semantic_diff(proj(a), proj(b), **kw)


def report_def(root: Path) -> Path:
    return root / "Synthetic.Report" / "definition"


def vfile(root: Path, page: str, vid: str) -> Path:
    return report_def(root) / "pages" / page / "visuals" / vid / "visual.json"


def write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")


def force_rmtree(path: Path) -> None:
    """rmtree that also clears read-only flags (Windows checkouts/copies)."""
    for p in [path, *path.rglob("*")]:
        try:
            os.chmod(p, stat.S_IRWXU)
        except OSError:
            pass
    shutil.rmtree(path)


def edit_json(path: Path, fn) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    fn(data)
    write(path, json.dumps(data, indent=2))


def tmdl(root: Path, name: str) -> Path:
    return root / "Synthetic.SemanticModel" / "definition" / "tables" / f"{name}.tmdl"


def sub(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, old
    write(path, text.replace(old, new, 1))


def changed_visuals(d: dict, page: str = "overview") -> list[dict]:
    detail = next(x for x in d["report"]["page_details"] if x["id"] == page)
    return detail["visuals"]["changed"]


# --- identical / shape -----------------------------------------------------------

def test_identical_copy(pair):
    d = diff(pair)
    assert d["identical"] is True
    assert d["summary"]["total_changes"] == 0
    assert d["report"]["page_details"] == []
    assert d["report"]["pages"] == {
        "added": [], "removed": [], "renamed": [], "resized": [],
        "hidden_changed": [], "reordered": None, "active_page": None}
    assert d["model"]["measures"] == {"added": [], "removed": [], "changed": []}
    json.dumps(d)  # JSON-serialisable


def test_semantically_equal_reformat_is_not_a_change(pair):
    """Rewriting a multi-line measure through the API (fenced form) changes
    the text but not the DAX: no measure/format diff."""
    _, b = pair
    p = proj(b)
    for name in ("Complex Measure", "Fenced Measure", "Net Revenue"):
        m = next(x for x in p.list_measures() if x.name == name)
        p.update_measure("Sales", name, dax=m.dax)
    d = diff(pair)
    assert d["model"]["measures"]["changed"] == []
    assert d["identical"] is True


# --- visuals -------------------------------------------------------------------------

def test_visual_moved_and_resized(pair):
    _, b = pair
    p = proj(b)
    p.move_visual("overview", "card1", x=100, y=50)
    p.move_visual("overview", "bar1", width=700, height=400)
    d = diff(pair)
    assert changed_visuals(d) == [
        {"id": "bar1", "type": "clusteredBarChart",
         "resized": {"from": {"width": 600, "height": 360},
                     "to": {"width": 700, "height": 400}}},
        {"id": "card1", "type": "card",
         "moved": {"from": {"x": 16, "y": 16}, "to": {"x": 100, "y": 50}}},
    ]
    assert d["summary"]["visuals_changed"] == 2
    assert d["summary"]["total_changes"] == 2


def test_z_order_and_type_change(pair):
    _, b = pair
    edit_json(vfile(b, "overview", "table1"),
              lambda j: j["position"].update(z=9))
    edit_json(vfile(b, "overview", "card1"),
              lambda j: j["visual"].update(visualType="multiRowCard"))
    d = diff(pair)
    assert changed_visuals(d) == [
        {"id": "card1", "type": "multiRowCard",
         "type_changed": {"from": "card", "to": "multiRowCard"}},
        {"id": "table1", "type": "tableEx", "z_order": {"from": 2, "to": 9}},
    ]


def test_rebind_reports_fields_added_and_removed_per_bucket(pair):
    _, b = pair
    proj(b).update_bindings("overview", "table1", {
        "Values": ["Sales.Net Revenue", "Date.Year"]})
    proj(b).update_bindings("overview", "bar1", {
        "Category": ["Date.Year"], "Y": ["Sales.Margin %"]})
    d = diff(pair)
    assert changed_visuals(d) == [
        {"id": "bar1", "type": "clusteredBarChart",
         "bindings": {"Y": {"added": ["Sales.Margin %"],
                            "removed": ["Sales.Net Revenue"]}}},
        {"id": "table1", "type": "tableEx",
         "bindings": {"Values": {"added": ["Date.Year"],
                                 "removed": ["Sales.Margin %"]}}},
    ]


def test_binding_reorder_is_flagged(pair):
    _, b = pair
    proj(b).update_bindings("overview", "table1", {
        "Values": ["Sales.Margin %", "Sales.Net Revenue"]})
    d = diff(pair)
    assert changed_visuals(d) == [
        {"id": "table1", "type": "tableEx",
         "bindings": {"Values": {"reordered": True}}}]


def test_title_change_is_not_double_reported_as_formatting(pair):
    _, b = pair

    def retitle(j):
        j["visual"]["visualContainerObjects"]["title"][0]["properties"][
            "text"]["expr"]["Literal"]["Value"] = "'Sales by Year'"
    edit_json(vfile(b, "overview", "bar1"), retitle)
    d = diff(pair)
    assert changed_visuals(d) == [
        {"id": "bar1", "type": "clusteredBarChart",
         "title_changed": {"from": "Revenue by Year", "to": "Sales by Year"}}]
    assert d["summary"]["formatting_changes"] == 0


def test_formatting_paths_added_changed_removed(pair):
    _, b = pair

    def restyle(j):
        vco = j["visual"]["visualContainerObjects"]
        vco["title"][0]["properties"]["fontSize"] = {
            "expr": {"Literal": {"Value": "14D"}}}
        vco["background"] = [{"properties": {"transparency": {
            "expr": {"Literal": {"Value": "0D"}}}}}]
        j["visual"]["objects"] = {"legend": [{"properties": {
            "show": {"expr": {"Literal": {"Value": "false"}}}}}]}
    edit_json(vfile(b, "overview", "bar1"), restyle)
    # base gets a property that other lacks
    edit_json(vfile(pair[0], "overview", "bar1"), lambda j: j["visual"].update(
        objects={"legend": [{"properties": {
            "show": {"expr": {"Literal": {"Value": "true"}}},
            "position": {"expr": {"Literal": {"Value": "'Top'"}}}}}]}))
    d = diff(pair)
    fmt = changed_visuals(d)[0]["formatting"]
    assert fmt["count"] == 4 and fmt["shown"] == 4 and not fmt["truncated"]
    assert fmt["changes"] == [
        {"path": "objects.legend[0].properties.position", "from": "'Top'"},
        {"path": "objects.legend[0].properties.show", "from": "true",
         "to": "false"},
        {"path": "visualContainerObjects.background[0].properties.transparency",
         "to": "0D"},
        {"path": "visualContainerObjects.title[0].properties.fontSize",
         "to": "14D"},
    ]
    assert d["summary"]["formatting_changes"] == 4


def test_first_top_level_property_does_not_create_phantom_paths(pair):
    """Regression: an empty container on one side must not show up as a
    change of the root path when the other side gains its first property."""
    _, b = pair
    edit_json(vfile(b, "overview", "card1"), lambda j: j.update(isHidden=True))
    edit_json(report_def(b) / "pages" / "overview" / "page.json",
              lambda j: j.update(objects={"background": [{"properties": {
                  "transparency": {"expr": {"Literal": {"Value": "50D"}}}}}]}))
    edit_json(report_def(b) / "report.json",
              lambda j: j.update(settings={"useStylableVisualContainerHeader": True}))
    d = diff(pair)
    assert changed_visuals(d)[0]["formatting"]["changes"] == [
        {"path": "isHidden", "to": True}]
    detail = d["report"]["page_details"][0]
    assert detail["formatting"]["changes"] == [
        {"path": "objects.background[0].properties.transparency", "to": "50D"}]
    assert d["report"]["formatting"]["changes"] == [
        {"path": "settings.useStylableVisualContainerHeader", "to": True}]
    assert d["summary"]["formatting_changes"] == 3


def test_desktop_shaped_visual_changes(pair):
    """Selectors, theme colours, sort definitions and projection extras."""
    a, b = pair

    def visual(color_id=1, legend="false", display=None, direction="Descending",
               year="2024L"):
        proj = {"field": {"Measure": {"Expression": {"SourceRef": {"Entity": "Sales"}},
                                      "Property": "Net Revenue"}},
                "queryRef": "Sales.Net Revenue", "nativeQueryRef": "Net Revenue"}
        if display:
            proj["displayName"] = display
        return {
            "$schema": "x", "name": "line1",
            "position": {"x": 1, "y": 2, "z": 3, "width": 4, "height": 5,
                         "tabOrder": 3},
            "visual": {
                "visualType": "lineChart",
                "query": {"queryState": {"Y": {"projections": [proj]}},
                          "sortDefinition": {"sort": [{"field": proj["field"],
                                                       "direction": direction}]}},
                "objects": {
                    "dataPoint": [{"properties": {"fill": {"solid": {"color": {
                        "expr": {"ThemeDataColor": {"ColorId": color_id,
                                                    "Percent": 0}}}}}},
                        "selector": {"metadata": "Sales.Net Revenue"}}],
                    "legend": [{"properties": {"show": {
                        "expr": {"Literal": {"Value": legend}}}}}]}},
            "filterConfig": {"filters": [{
                "name": "F", "type": "Categorical",
                "field": {"Column": {"Expression": {"SourceRef": {"Entity": "Date"}},
                                     "Property": "Year"}},
                "filter": {"Version": 2, "Where": [{"Condition": {"In": {"Values": [
                    [{"Literal": {"Value": year}}]]}}}]}}]},
        }
    for root, kw in ((a, {}), (b, {"color_id": 3, "legend": "true",
                                    "display": "Revenue", "direction": "Ascending",
                                    "year": "2025L"})):
        d = report_def(root) / "pages" / "overview" / "visuals" / "line1"
        d.mkdir(parents=True)
        write(d / "visual.json", json.dumps(visual(**kw), indent=2))
    d = diff(pair)
    assert d["report"]["page_details"][0]["visuals"]["added"] == []
    line = changed_visuals(d)[0]
    assert line["id"] == "line1" and set(line) == {"id", "type", "filters", "formatting"}
    assert [c["path"] for c in line["formatting"]["changes"]] == [
        "objects.dataPoint[0].properties.fill.solid.color.expr.ThemeDataColor.ColorId",
        "objects.legend[0].properties.show",
        "query.queryState.Y.projections.Sales.Net Revenue.displayName",
        "query.sortDefinition.sort[0].direction",
    ]
    assert line["filters"]["changed"][0]["changes"] == [
        {"path": "filter.Where[0].Condition.In.Values[0][0].Literal.Value",
         "from": "2024L", "to": "2025L"}]


def test_formatting_budget_is_shared_and_counts_stay_true(pair):
    _, b = pair

    def restyle(j):
        j["visual"]["objects"] = {"labels": [{"properties": {
            f"p{i}": {"expr": {"Literal": {"Value": f"{i}D"}}}
            for i in range(6)}}]}
    edit_json(vfile(b, "overview", "bar1"), restyle)
    edit_json(vfile(b, "overview", "card1"), restyle)
    d = diff(pair, max_format_changes=8)
    bar, card = changed_visuals(d)
    assert (bar["formatting"]["count"], bar["formatting"]["shown"]) == (6, 6)
    assert (card["formatting"]["count"], card["formatting"]["shown"]) == (6, 2)
    assert card["formatting"]["truncated"] is True
    assert d["summary"]["formatting_changes"] == 12
    assert d["summary"]["formatting_shown"] == 8
    zero = diff(pair, max_format_changes=0)
    assert changed_visuals(zero)[0]["formatting"]["changes"] == []
    with pytest.raises(ValueError):
        diff(pair, max_format_changes=-1)


def test_visuals_added_and_removed(pair):
    a, b = pair
    p = proj(b)
    p.delete_visual("overview", "table1")
    new_id = p.add_visual("details", {"visual_type": "card", "title": "Fresh",
                                      "bindings": {"Values": ["Sales.Net Revenue"]}})
    d = diff(pair)
    by_page = {x["id"]: x for x in d["report"]["page_details"]}
    assert by_page["overview"]["visuals"]["removed"] == [
        {"id": "table1", "type": "tableEx", "title": None}]
    assert by_page["details"]["visuals"]["added"] == [
        {"id": new_id, "type": "card", "title": "Fresh"}]
    assert d["summary"]["visuals_added"] == 1
    assert d["summary"]["visuals_removed"] == 1


# --- pages -------------------------------------------------------------------------------

def test_page_level_changes(pair):
    _, b = pair
    p = proj(b)
    p.rename_page("details", "Detail Page")
    p.hide_page("overview", True)
    p.hide_page("details", False)
    p.create_page("Extra")
    p.reorder_pages(["details", "overview"])
    edit_json(report_def(b) / "pages" / "overview" / "page.json",
              lambda j: j.update(width=1920, height=1080))
    edit_json(report_def(b) / "pages" / "pages.json",
              lambda j: j.update(activePageName="details"))
    d = diff(pair)
    assert d["report"]["pages"] == {
        "added": [{"id": "extra", "name": "Extra", "visual_count": 0}],
        "removed": [],
        "renamed": [{"id": "details", "from": "Details", "to": "Detail Page"}],
        "resized": [{"id": "overview",
                     "from": {"width": 1280, "height": 720},
                     "to": {"width": 1920, "height": 1080}}],
        "hidden_changed": [
            {"id": "details", "name": "Detail Page", "from": True, "to": False},
            {"id": "overview", "name": "Overview", "from": False, "to": True}],
        "reordered": {"from": ["overview", "details"],
                      "to": ["details", "overview"]},
        "active_page": {"from": "overview", "to": "details"},
    }
    assert d["summary"]["pages_added"] == 1
    assert d["summary"]["pages_reordered"] == 1


def test_removed_page(pair):
    _, b = pair
    proj(b).delete_page("details")
    d = diff(pair)
    assert d["report"]["pages"]["removed"] == [
        {"id": "details", "name": "Details", "visual_count": 1}]
    assert d["report"]["pages"]["reordered"] is None


def test_page_scope_and_unknown_page(pair):
    _, b = pair
    p = proj(b)
    p.move_visual("overview", "card1", x=1)
    p.rename_page("details", "Other")
    p.add_filter("report", build_filter("Date.Year", values=[2024], name="R1"))
    d = diff(pair, page_id="overview")
    assert [x["id"] for x in d["report"]["page_details"]] == ["overview"]
    assert d["report"]["pages"]["renamed"] == []          # details is out of scope
    assert "filters" not in d["report"] and "theme" not in d["report"]
    assert d["scope"]["page_id"] == "overview"
    with pytest.raises(ValueError, match="ghost"):
        diff(pair, page_id="ghost")


def test_page_level_formatting_and_filters(pair):
    _, b = pair
    proj(b).style_page("overview", background_color="#112233")
    proj(b).add_filter("page", build_filter("Date.Year", values=[2024], name="P1"),
                       page_id="overview")
    d = diff(pair)
    detail = d["report"]["page_details"][0]
    assert detail["id"] == "overview"
    assert detail["filters"]["added"] == [
        {"name": "P1", "field": "Date.Year", "type": "Categorical"}]
    paths = [c["path"] for c in detail["formatting"]["changes"]]
    assert any(p.startswith("objects.background") for p in paths)


# --- filters ---------------------------------------------------------------------------------

def test_filters_all_scopes(pair):
    a, b = pair
    pa, pb = proj(a), proj(b)
    for p in (pa, pb):
        p.add_filter("report", build_filter("Date.Year", values=[2024], name="Keep"))
        p.add_filter("report", build_filter("Date.Year", values=[2024], name="Chg"))
        p.add_filter("report", build_filter("Date.Year", values=[1999], name="Gone"))
    pb.remove_filter("report", "Gone")
    pb.remove_filter("report", "Chg")
    pb.add_filter("report", build_filter("Date.Year", values=[2025], name="Chg"))
    pb.add_filter("report", build_filter("Sales.Amount", filter_type="Advanced",
                                         comparison="gt", comparison_value=10,
                                         name="New"))
    pb.add_filter("visual", build_filter("Date.Year", values=[2024], name="V1"),
                  page_id="overview", visual_id="bar1")
    d = diff(pair)
    flt = d["report"]["filters"]
    assert flt["added"] == [{"name": "New", "field": "Sales.Amount",
                             "type": "Advanced"}]
    assert flt["removed"] == [{"name": "Gone", "field": "Date.Year",
                               "type": "Categorical"}]
    assert [c["name"] for c in flt["changed"]] == ["Chg"]
    change = flt["changed"][0]["changes"][0]
    assert (change["from"], change["to"]) == ("2024L", "2025L")
    bar = changed_visuals(d)[0]
    assert bar["id"] == "bar1"
    assert bar["filters"]["added"][0]["name"] == "V1"
    assert d["summary"]["filters_added"] == 2
    assert d["summary"]["filters_removed"] == 1
    assert d["summary"]["filters_changed"] == 1


# --- theme / bookmarks / report measures -----------------------------------------------------------

def _theme(name, colors):
    return {"name": name, "dataColors": colors, "background": "#FFFFFF"}


def test_theme_change_reports_name_and_data_colors(pair):
    a, b = pair
    proj(a).set_report_theme(_theme("Old", ["#111111", "#222222", "#333333"]))
    proj(b).set_report_theme(_theme("New", ["#111111", "#AAAAAA", "#333333", "#444444"]))
    d = diff(pair)
    theme = d["report"]["theme"]
    assert theme["custom_theme"] == {"from": "Old.json", "to": "New.json"}
    assert theme["name"] == {"from": "Old", "to": "New"}
    assert theme["data_colors"] == {
        "changed": [{"index": 1, "from": "#222222", "to": "#AAAAAA"},
                    {"index": 3, "to": "#444444"}],
        "count_from": 3, "count_to": 4}
    assert d["summary"]["theme_changed"] == 1


def test_same_theme_name_different_file_content(pair):
    a, b = pair
    proj(a).set_report_theme(_theme("T", ["#111111"]))
    proj(b).set_report_theme(_theme("T", ["#999999"]))
    theme = diff(pair)["report"]["theme"]
    assert "custom_theme" not in theme and "name" not in theme
    assert theme["data_colors"]["changed"] == [
        {"index": 0, "from": "#111111", "to": "#999999"}]


def test_bookmarks_added_and_removed(pair):
    a, b = pair
    proj(a).create_bookmark("Old One")
    proj(b).create_bookmark("New One")
    bm = diff(pair)["report"]["bookmarks"]
    assert [x["display_name"] for x in bm["added"]] == ["New One"]
    assert [x["display_name"] for x in bm["removed"]] == ["Old One"]
    assert bm["changed"] == []


def test_report_level_measures(pair):
    a, b = pair

    def ext(measures):
        return {"$schema": "x", "name": "extension", "entities": [
            {"name": "Sales", "measures": measures}]}
    write(report_def(a) / "reportExtensions.json", json.dumps(ext([
        {"name": "Keep", "expression": "1", "dataType": "Integer"},
        {"name": "Edit", "expression": "SUM(Sales[Amount])"},
        {"name": "Gone", "expression": "2"}])))
    write(report_def(b) / "reportExtensions.json", json.dumps(ext([
        {"name": "Keep", "expression": "1", "dataType": "Integer"},
        {"name": "Edit", "expression": "SUM(Sales[Amount]) * 2",
         "formatString": "0.0"},
        {"name": "Fresh", "expression": "3"}])))
    rm = diff(pair)["report"]["report_measures"]
    assert rm["added"] == [{"measure": "Sales.Fresh", "expression": "3"}]
    assert rm["removed"] == [{"measure": "Sales.Gone", "expression": "2"}]
    assert rm["changed"] == [{
        "measure": "Sales.Edit",
        "dax": {"diff": ["--- base", "+++ other", "@@ -1 +1 @@",
                         "-SUM(Sales[Amount])", "+SUM(Sales[Amount]) * 2"]},
        "formatString": {"from": None, "to": "0.0"}}]


# --- model: measures ------------------------------------------------------------------------------

def test_measure_changes(pair):
    a, b = pair
    p = proj(b)
    p.update_measure("Sales", "Net Revenue", dax="SUM(Sales[Amount]) * 2",
                     fmt="0.0")
    p.update_measure("Sales", "Margin %", display_folder="Ratios")
    p.create_measure("Sales", "New M", "1", fmt="0", display_folder="Misc")
    sub(tmdl(b, "Sales"), "\tmeasure 'Hidden Helper' = 1\n\t\tisHidden\n", "")
    d = diff(pair)
    m = d["model"]["measures"]
    assert m["added"] == [{"table": "Sales", "name": "New M", "dax": "1",
                           "format_string": "0", "display_folder": "Misc",
                           "hidden": False}]
    assert [x["name"] for x in m["removed"]] == ["Hidden Helper"]
    assert m["removed"][0]["hidden"] is True
    assert m["changed"] == [
        {"table": "Sales", "name": "Margin %",
         "display_folder": {"from": "KPIs", "to": "Ratios"}},
        {"table": "Sales", "name": "Net Revenue",
         "dax": {"diff": ["--- base", "+++ other", "@@ -1 +1 @@",
                          "-SUM(Sales[Amount])", "+SUM(Sales[Amount]) * 2"]},
         "format_string": {"from": "#,0", "to": "0.0"}},
    ]
    assert d["summary"]["measures_added"] == 1
    assert d["summary"]["measures_removed"] == 1
    assert d["summary"]["measures_changed"] == 2


def test_multiline_dax_diff_and_hidden_flag(pair):
    _, b = pair
    sub(tmdl(b, "Sales"), "DIVIDE(_rev, [Order Count])",
        "DIVIDE(_rev, [Order Count], 0)")
    sub(tmdl(b, "Sales"), "\tmeasure 'Hidden Helper' = 1\n\t\tisHidden\n",
        "\tmeasure 'Hidden Helper' = 1\n")
    m = diff(pair)["model"]["measures"]["changed"]
    by_name = {x["name"]: x for x in m}
    lines = by_name["Complex Measure"]["dax"]["diff"]
    assert "-\tDIVIDE(_rev, [Order Count])" in lines
    assert "+\tDIVIDE(_rev, [Order Count], 0)" in lines
    assert by_name["Hidden Helper"] == {
        "table": "Sales", "name": "Hidden Helper",
        "hidden": {"from": True, "to": False}}


# --- model: columns / tables ----------------------------------------------------------------------

def test_column_changes(pair):
    _, b = pair
    proj(b).create_column("Date", "Month", "int64")
    proj(b).create_column("Sales", "Margin", "double", dax="[Amount] - [Cost]")
    sales = tmdl(b, "Sales")
    sub(sales, "column OrderDate\n\t\tdataType: dateTime",
        "column OrderDate\n\t\tdataType: string\n\t\tformatString: dd/MM/yyyy"
        "\n\t\tsortByColumn: Amount")
    sub(sales, "column Amount\n", "column Amount\n\t\tisHidden\n")
    sub(sales, "\tcolumn Cost\n\t\tdataType: double\n\t\tsummarizeBy: sum\n"
               "\t\tsourceColumn: Cost\n\n", "")
    c = diff(pair)["model"]["columns"]
    assert [(x["table"], x["name"]) for x in c["added"]] == [
        ("Date", "Month"), ("Sales", "Margin")]
    assert c["added"][1]["calculated"] is True
    assert [(x["table"], x["name"]) for x in c["removed"]] == [("Sales", "Cost")]
    assert c["changed"] == [
        {"table": "Sales", "name": "Amount",
         "hidden": {"from": False, "to": True}},
        {"table": "Sales", "name": "OrderDate",
         "data_type": {"from": "dateTime", "to": "string"},
         "format_string": {"from": None, "to": "dd/MM/yyyy"},
         "sort_by": {"from": None, "to": "Amount"}},
    ]


def test_calculated_column_expression_diff(pair):
    a, b = pair
    proj(a).create_column("Sales", "Margin", "double", dax="[Amount] - [Cost]")
    proj(b).create_column("Sales", "Margin", "double", dax="[Amount] - 2 * [Cost]")
    c = diff(pair)["model"]["columns"]["changed"]
    assert c[0]["expression"]["diff"][-2:] == ["-[Amount] - [Cost]",
                                               "+[Amount] - 2 * [Cost]"]


def test_tables_and_calc_groups(pair):
    a, b = pair
    proj(a).create_calc_group("Time", 1, [{"name": "Current", "dax": "SELECTEDMEASURE()"},
                                          {"name": "YTD", "dax": "CALCULATE(SELECTEDMEASURE(), DATESYTD('Date'[Date]))"},
                                          {"name": "Old", "dax": "1"}])
    proj(b).create_calc_group("Time", 2, [{"name": "Current", "dax": "SELECTEDMEASURE()"},
                                          {"name": "YTD", "dax": "CALCULATE(SELECTEDMEASURE(), DATESMTD('Date'[Date]))"},
                                          {"name": "Fresh", "dax": "2"}])
    proj(b).create_calc_group("Scenario", 5, [{"name": "Base", "dax": "SELECTEDMEASURE()"}])
    sub(tmdl(b, "Date"), "dataCategory: Time", "dataCategory: Time\n\tisHidden")
    d = diff(pair)
    assert d["model"]["tables"]["added"] == [
        {"name": "Scenario", "columns": 2, "measures": 0, "calculation_group": True}]
    assert d["model"]["tables"]["changed"] == [
        {"name": "Date", "hidden": {"from": False, "to": True}}]
    cg = d["model"]["calculation_groups"]
    assert cg["added"] == [{"name": "Scenario", "precedence": 5, "items": ["Base"]}]
    assert len(cg["changed"]) == 1
    ch = cg["changed"][0]
    assert ch["name"] == "Time"
    assert ch["precedence"] == {"from": 1, "to": 2}
    assert ch["items_added"] == ["Fresh"] and ch["items_removed"] == ["Old"]
    assert ch["items_changed"][0]["name"] == "YTD"
    assert "+CALCULATE(SELECTEDMEASURE(), DATESMTD('Date'[Date]))" in \
        ch["items_changed"][0]["dax"]["diff"]


def test_partition_source_change(pair):
    _, b = pair
    sub(tmdl(b, "Sales"), "sales.csv", "sales2.csv")
    parts = diff(pair)["model"]["partitions"]
    assert [x["name"] for x in parts["changed"]] == ["Sales"]
    assert any("sales2.csv" in ln for ln in parts["changed"][0]["source"]["diff"])


EXPR = "Synthetic.SemanticModel/definition/expressions.tmdl"


def test_shared_expressions_and_parameters(pair):
    a, b = pair
    sub(b / EXPR, '"prod-server"', '"dev-server"')
    with (b / EXPR).open("a", encoding="utf-8", newline="\n") as fh:
        fh.write("\nexpression Region =\n\t\tlet\n\t\t\tR = \"EU\"\n\t\tin\n\t\t\tR\n"
                 "\tqueryGroup: Parameters\n")
    ex = diff(pair)["model"]["expressions"]
    assert ex["added"] == [{"name": "Region", "expression": 'let\n\tR = "EU"\nin\n\tR'}]
    assert ex["removed"] == []
    assert [e["name"] for e in ex["changed"]] == ["ServerParam"]
    assert '+"dev-server" meta [IsParameterQuery=true, Type="Text"]' in \
        ex["changed"][0]["expression"]["diff"]
    d = diff(pair)
    assert d["summary"]["expressions_added"] == 1
    assert d["summary"]["expressions_changed"] == 1
    reverse = semantic_diff(proj(pair[1]), proj(pair[0]))["model"]["expressions"]
    assert [e["name"] for e in reverse["removed"]] == ["Region"]


# --- model: relationships -----------------------------------------------------------------------------

REL = "Synthetic.SemanticModel/definition/relationships.tmdl"


def test_relationship_added_removed_changed(pair):
    a, b = pair
    proj(b).create_relationship("Sales", "Cost", "Date", "Year")
    sub(b / REL, "\tfromColumn: Sales.OrderDate\n",
        "\tisActive: false\n\tcrossFilteringBehavior: bothDirections\n"
        "\tfromCardinality: many\n\ttoCardinality: many\n"
        "\tfromColumn: Sales.OrderDate\n")
    r = diff(pair)["model"]["relationships"]
    assert r["added"] == [{"from": "Sales.Cost", "to": "Date.Year",
                           "cardinality": "many-to-one",
                           "direction": "oneDirection", "active": True}]
    assert r["removed"] == []
    assert r["changed"] == [{
        "from": "Sales.OrderDate", "to": "Date.Date",
        "cardinality": {"from": "many-to-one", "to": "many-to-many"},
        "direction": {"from": "oneDirection", "to": "bothDirections"},
        "active": {"from": True, "to": False}}]
    # and the reverse direction reports the removal
    removed = semantic_diff(proj(b), proj(a))["model"]["relationships"]["removed"]
    assert [x["to"] for x in removed] == ["Date.Year"]


# --- layers / options ---------------------------------------------------------------------------------------

def test_other_without_semantic_model(pair):
    _, b = pair
    force_rmtree(b / "Synthetic.SemanticModel")
    d = diff(pair)
    assert d["model"]["present"] == {"base": True, "other": False}
    assert "measures" not in d["model"]
    assert d["summary"]["layers_missing"] == 1
    assert d["identical"] is False
    assert d["report"]["pages"]["added"] == []      # report still compared


def test_other_without_report(pair):
    _, b = pair
    force_rmtree(b / "Synthetic.Report")
    d = diff(pair)
    assert d["report"]["present"] == {"base": True, "other": False}
    assert "pages" not in d["report"]
    assert d["summary"]["layers_missing"] == 1
    assert d["model"]["measures"] == {"added": [], "removed": [], "changed": []}


def test_include_model_false(pair):
    _, b = pair
    proj(b).create_measure("Sales", "New M", "1")
    d = diff(pair, include_model=False)
    assert d["model"] == {"included": False}
    assert d["identical"] is True


# --- determinism -------------------------------------------------------------------------------------------------

def _mutate_a(p: PbipProject) -> None:
    p.move_visual("overview", "card1", x=99)
    p.rename_page("details", "Zed")
    p.update_measure("Sales", "Net Revenue", dax="2")
    p.create_measure("Sales", "Zeta", "1")
    p.create_measure("Sales", "Alpha", "2")


def _mutate_b(p: PbipProject) -> None:
    p.create_measure("Sales", "Alpha", "2")
    p.create_measure("Sales", "Zeta", "1")
    p.update_measure("Sales", "Net Revenue", dax="2")
    p.rename_page("details", "Zed")
    p.move_visual("overview", "card1", x=99)


def test_diff_is_deterministic_and_independent_of_edit_order(tmp_path):
    base = tmp_path / "base"
    shutil.copytree(SYNTH, base)
    outs = []
    for i, mutate in enumerate((_mutate_a, _mutate_b)):
        other = tmp_path / f"o{i}"
        shutil.copytree(SYNTH, other)
        mutate(proj(other))
        d = semantic_diff(proj(base), proj(other))
        d.pop("projects")
        outs.append(d)
    assert json.dumps(outs[0]) == json.dumps(outs[1])
    names = [m["name"] for m in outs[0]["model"]["measures"]["added"]]
    assert names == sorted(names) == ["Alpha", "Zeta"]
    again = semantic_diff(proj(base), proj(tmp_path / "o0"))
    again.pop("projects")
    assert json.dumps(again) == json.dumps(outs[0])


# --- tool ---------------------------------------------------------------------------------------------------------

def _payload(result):
    structured = None
    if isinstance(result, tuple):
        result, structured = result
    structured = (structured or getattr(result, "structured_content", None)
                  or getattr(result, "structuredContent", None))
    if structured is not None:
        return structured.get("result", structured) \
            if isinstance(structured, dict) else structured
    content = getattr(result, "content", result)
    items = [json.loads(c.text) for c in content]
    return items[0] if len(items) == 1 else items


def test_tool_registered_and_callable(pair):
    import report_server.server as srv

    tools = {t.name: t for t in asyncio.run(srv.mcp.list_tools())}
    assert "pbi_semantic_diff" in tools and "pbi_project_diff" in tools
    ann = tools["pbi_semantic_diff"].annotations
    assert (getattr(ann, "readOnlyHint", None) if hasattr(ann, "readOnlyHint")
            else ann.read_only_hint) is True
    schema = getattr(tools["pbi_semantic_diff"], "inputSchema", None) \
        or tools["pbi_semantic_diff"].input_schema
    assert "dry_run" not in schema["properties"]
    assert schema["required"] == ["other_path"]

    a, b = pair
    proj(b).move_visual("overview", "card1", x=5)

    def call(name, **args):
        return _payload(asyncio.run(srv.mcp.call_tool(name, args)))

    call("pbi_set_project", path=str(a / "Synthetic.pbip"))
    out = call("pbi_semantic_diff", other_path=str(b / "Synthetic.pbip"))
    assert out["identical"] is False
    assert changed_visuals(out)[0]["moved"]["to"]["x"] == 5
    # a bad path gives an actionable error (raised, or an isError result)
    try:
        res = asyncio.run(srv.mcp.call_tool(
            "pbi_semantic_diff", {"other_path": str(a / "nope")}))
    except Exception as exc:  # noqa: BLE001 - raised by mcp 1.x and 2.x
        chain, cur = [], exc
        while cur is not None and cur not in chain:
            chain.append(cur)
            cur = cur.__cause__ or cur.__context__
        assert "does not exist" in " | ".join(str(e) for e in chain)
    else:
        first = res[0] if isinstance(res, tuple) else res
        assert getattr(first, "isError", None) or getattr(first, "is_error", None)
        assert "does not exist" in str(first)


def test_diff_does_not_touch_either_project(pair):
    from core.journal import snapshot

    a, b = pair
    proj(b).move_visual("overview", "card1", x=5)
    before = (snapshot(a), snapshot(b))
    semantic_diff(proj(a), proj(b))
    assert (snapshot(a), snapshot(b)) == before


def test_result_is_not_mutating_inputs_between_calls(pair):
    d1 = diff(pair)
    d2 = copy.deepcopy(d1)
    diff(pair)
    assert d1 == d2
