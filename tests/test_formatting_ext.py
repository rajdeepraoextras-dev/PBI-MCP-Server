"""Advanced visual formatting (feature package: formatting).

Conditional formatting, analytics lines, tooltip pages, slicer settings, data
labels, visual calculations — every write is applied to a scratch copy of the
synthetic fixture, must validate against the vendored visualContainer schema,
must keep list_visuals/get_visual working, and is snapshotted as a golden
under tests/goldens/ext_*.json (regenerate deliberately with
PBI_MCP_REGEN_GOLDENS=1, same mechanism as tests/test_goldens.py).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path

import pytest

from core import formatting_ext as fx
from core import schema_validate as sv
from core.journal import snapshot
from core.pbip import PbipProject
from report_server import tools_formatting as tf
from report_server.server import ReportState, get_visual, list_visuals, set_project

SYNTH = Path(__file__).parent / "fixtures" / "synthetic"
GOLDEN_DIR = Path(__file__).parent / "goldens"
REGEN = os.environ.get("PBI_MCP_REGEN_GOLDENS") == "1"


@pytest.fixture
def state(tmp_path) -> ReportState:
    dst = tmp_path / "synthetic"
    shutil.copytree(SYNTH, dst)
    st = ReportState()
    set_project(st, str(dst / "Synthetic.pbip"))
    return st


def _raw(state, page, vid) -> dict:
    return PbipProject(state.project.path).get_visual(page, vid).raw


def _clean(state, page, vid) -> dict:
    """Reload the visual: schema-clean, still listable/readable; return raw."""
    raw = _raw(state, page, vid)
    if sv.is_available():
        assert sv.validate("visualContainer", raw) == []
    assert any(v["id"] == vid for v in list_visuals(state, page))
    assert get_visual(state, page, vid)["raw"] == raw
    return raw


def _golden(name: str, emitted: dict) -> None:
    golden_file = GOLDEN_DIR / f"ext_{name}.json"
    if REGEN or not golden_file.exists():
        golden_file.write_text(json.dumps(emitted, indent=2) + "\n",
                               encoding="utf-8", newline="\n")
    golden = json.loads(golden_file.read_text(encoding="utf-8"))
    assert emitted == golden, (
        f"ext_{name} output changed vs golden. If intentional, set "
        f"PBI_MCP_REGEN_GOLDENS=1 and rerun.")


def _lit(node):
    return node["expr"]["Literal"]["Value"]


# --- 1. conditional formatting ---------------------------------------------------

GRADIENT = {"kind": "gradient", "measure": "Sales.Net Revenue",
            "min": {"color": "#FFFFFF"}, "max": {"color": "#118DFF", "value": 1000}}


def test_cf_table_gradient_background(state):
    res = tf.set_conditional_format(state, "overview", "table1",
                                    "Sales.Net Revenue", "background", GRADIENT)
    assert res["object"] == "values" and res["property"] == "backColor"
    raw = _clean(state, "overview", "table1")
    entry = raw["visual"]["objects"]["values"][0]
    # selector: Desktop's values-only wildcard + the field's queryRef [real]
    assert entry["selector"]["metadata"] == "Sales.Net Revenue"
    assert entry["selector"]["data"] == [{"dataViewWildcard": {"matchingOption": 1}}]
    rule = entry["properties"]["backColor"]["solid"]["color"]["expr"]["FillRule"]
    assert rule["Input"]["Measure"]["Property"] == "Net Revenue"
    grad = rule["FillRule"]["linearGradient2"]
    assert grad["min"]["color"] == {"Literal": {"Value": "'#FFFFFF'"}}
    assert grad["max"]["value"] == {"Literal": {"Value": "1000.0D"}}
    assert grad["nullColoringStrategy"]["strategy"]["Literal"]["Value"] == "'asZero'"
    _golden("cf_table_gradient", raw)


def test_cf_table_rules_font(state):
    tf.set_conditional_format(state, "overview", "table1", "Sales.Margin %", "font", {
        "kind": "rules", "measure": "Sales.Margin %",
        "rules": [{"min": 0.2, "max": 1, "color": "#3BB44A"},
                  {"max": 0.2, "max_inclusive": True, "color": "#E81123"}],
        "default_color": "#888888"})
    raw = _clean(state, "overview", "table1")
    entry = raw["visual"]["objects"]["values"][0]
    cond = entry["properties"]["fontColor"]["solid"]["color"]["expr"]["Conditional"]
    assert len(cond["Cases"]) == 2
    both = cond["Cases"][0]["Condition"]["And"]
    assert both["Left"]["Comparison"]["ComparisonKind"] == 2     # >= min
    assert both["Right"]["Comparison"]["ComparisonKind"] == 3    # < max
    assert cond["Cases"][1]["Condition"]["Comparison"]["ComparisonKind"] == 4  # <= max
    assert cond["Cases"][0]["Value"] == {"Literal": {"Value": "'#3BB44A'"}}
    assert cond["DefaultValue"] == {"Literal": {"Value": "'#888888'"}}
    _golden("cf_table_rules", raw)


def test_cf_chart_gradient3_and_field_value(state):
    tf.set_conditional_format(state, "overview", "bar1", "Sales.Net Revenue", "background", {
        "kind": "gradient", "measure": "Sales.Margin %",
        "min": {"color": "#FFAC00"}, "mid": {"color": "#FFFFFF", "value": 0.5},
        "max": {"color": "#9B0065"}, "null_color": "#CCCCCC"})
    tf.set_conditional_format(state, "overview", "bar1", "Sales.Net Revenue", "font",
                              {"kind": "field", "measure": "Sales.Margin %"})
    raw = _clean(state, "overview", "bar1")
    objects = raw["visual"]["objects"]
    fill = objects["dataPoint"][0]["properties"]["fill"]["solid"]["color"]["expr"]["FillRule"]
    grad = fill["FillRule"]["linearGradient3"]
    assert set(grad) == {"min", "mid", "max", "nullColoringStrategy"}
    assert grad["nullColoringStrategy"] == {
        "strategy": {"Literal": {"Value": "'specificColor'"}},
        "color": {"Literal": {"Value": "'#CCCCCC'"}}}
    # the unbound input measure is resolved from the model as a Measure ref
    assert fill["Input"]["Measure"]["Property"] == "Margin %"
    color = objects["labels"][0]["properties"]["color"]["solid"]["color"]["expr"]
    assert color["Measure"]["Property"] == "Margin %"
    assert objects["labels"][0]["selector"] == {"metadata": "Sales.Net Revenue"}
    _golden("cf_chart_gradient3", raw)


def test_cf_data_bars_icons_web_url(state):
    tf.set_conditional_format(state, "overview", "table1", "Sales.Net Revenue", "data_bars",
                              {"positive_color": "#1F8A70", "negative_color": "#D13438",
                               "axis_color": "#000000", "show_bar_only": True, "min": 0})
    tf.set_conditional_format(state, "overview", "table1", "Sales.Margin %", "icons", {
        "style": "threeArrowsColored", "layout": "rightOfData",
        "rules": [{"min": 0.5, "icon": 0}, {"min": 0.2, "max": 0.5, "icon": 1},
                  {"max": 0.2, "icon": 2}]})
    tf.set_conditional_format(state, "overview", "table1", "Sales.Net Revenue", "web_url",
                              {"kind": "field", "measure": "Sales.Margin %"})
    raw = _clean(state, "overview", "table1")
    objects = raw["visual"]["objects"]
    bars = objects["dataBars"][0]
    assert bars["selector"] == {"metadata": "Sales.Net Revenue"}
    assert _lit(bars["properties"]["hideText"]) == "true"
    assert _lit(bars["properties"]["min"]) == "0.0D"
    assert bars["properties"]["axisColor"]["solid"]["color"]["expr"]["Literal"]["Value"] == "'#000000'"
    icon = objects["icon"][0]
    cases = icon["properties"]["iconSet"]["expr"]["Conditional"]["Cases"]
    assert [c["Value"]["Literal"]["Value"] for c in cases] == [
        "'threeArrowsColored_0'", "'threeArrowsColored_1'", "'threeArrowsColored_2'"]
    assert cases[0]["Condition"]["Comparison"]["Left"]["Measure"]["Property"] == "Margin %"
    assert _lit(icon["properties"]["layout"]) == "'rightOfData'"
    url = objects["webUrl"][0]
    assert url["properties"]["url"]["expr"]["Measure"]["Property"] == "Margin %"
    _golden("cf_table_bars_icons_url", raw)


def test_cf_rerun_replaces_and_clear_removes(state):
    for _ in range(2):
        tf.set_conditional_format(state, "overview", "table1",
                                  "Sales.Net Revenue", "background", GRADIENT)
        tf.set_conditional_format(state, "overview", "table1", "Sales.Net Revenue",
                                  "data_bars", {"positive_color": "#00FF00"})
    objects = _raw(state, "overview", "table1")["visual"]["objects"]
    assert len(objects["values"]) == 1 and len(objects["dataBars"]) == 1

    # font on the same field lands in the same entry as background
    tf.set_conditional_format(state, "overview", "table1", "Sales.Net Revenue", "font",
                              {"kind": "field", "measure": "Sales.Margin %"})
    objects = _raw(state, "overview", "table1")["visual"]["objects"]
    assert set(objects["values"][0]["properties"]) == {"backColor", "fontColor"}

    assert tf.clear_conditional_format(state, "overview", "table1",
                                       "Sales.Net Revenue", "background")["removed"] == 1
    objects = _raw(state, "overview", "table1")["visual"]["objects"]
    assert set(objects["values"][0]["properties"]) == {"fontColor"}
    assert tf.clear_conditional_format(state, "overview", "table1",
                                       "Sales.Net Revenue", "font")["removed"] == 1
    assert tf.clear_conditional_format(state, "overview", "table1",
                                       "Sales.Net Revenue", "data_bars")["removed"] == 1
    raw = _clean(state, "overview", "table1")
    assert "objects" not in raw["visual"]          # nothing left -> key dropped
    assert tf.clear_conditional_format(state, "overview", "table1",
                                       "Sales.Net Revenue", "font")["removed"] == 0


def test_cf_errors(state):
    with pytest.raises(ValueError, match="not bound"):
        tf.set_conditional_format(state, "overview", "table1", "Date.Year",
                                  "background", GRADIENT)
    with pytest.raises(KeyError, match="Unknown model field"):
        tf.set_conditional_format(state, "overview", "table1", "Sales.Net Revenue",
                                  "background", {**GRADIENT, "measure": "Sales.Nope"})
    with pytest.raises(ValueError, match="table/matrix"):
        tf.set_conditional_format(state, "overview", "bar1", "Sales.Net Revenue",
                                  "data_bars", {})
    with pytest.raises(ValueError, match="rule.kind"):
        tf.set_conditional_format(state, "overview", "table1", "Sales.Net Revenue",
                                  "background", {"kind": "rainbow"})
    with pytest.raises(ValueError, match="hex"):
        tf.set_conditional_format(state, "overview", "table1", "Sales.Net Revenue",
                                  "background", {**GRADIENT, "min": {"color": "red"}})
    with pytest.raises(ValueError, match="target must be one of"):
        tf.set_conditional_format(state, "overview", "table1", "Sales.Net Revenue",
                                  "glow", GRADIENT)
    with pytest.raises(ValueError, match="not supported"):
        tf.set_conditional_format(state, "details", "slicer1", "Date.Year",
                                  "background", GRADIENT)
    with pytest.raises(ValueError, match="min.*max"):
        tf.set_conditional_format(state, "overview", "table1", "Sales.Net Revenue",
                                  "font", {"kind": "rules", "measure": "Sales.Margin %",
                                           "rules": [{"color": "#000000"}]})


# --- 2. analytics lines ---------------------------------------------------------

def test_analytics_lines_add_list_remove(state):
    r = tf.add_analytics_line(state, "overview", "bar1", "constant", value=100,
                              color="#FF0000", label="Target")
    assert r["object"] == "y1AxisReferenceLine" and r["index"] == 0
    tf.add_analytics_line(state, "overview", "bar1", "average",
                          measure="Sales.Net Revenue", style="dotted", position="front")
    tf.add_analytics_line(state, "overview", "bar1", "percentile", percentile=90,
                          transparency=40)
    tf.add_analytics_line(state, "overview", "bar1", "trend")
    r2 = tf.add_analytics_line(state, "overview", "bar1", "constant", value=50)
    raw = _clean(state, "overview", "bar1")
    objects = raw["visual"]["objects"]
    assert set(objects) == {"y1AxisReferenceLine", "averageLine", "percentileLine", "trend"}
    consts = objects["y1AxisReferenceLine"]
    assert [e["selector"]["id"] for e in consts] == ["0", "1"]
    assert _lit(consts[0]["properties"]["value"]) == "100.0D"
    assert _lit(consts[0]["properties"]["displayName"]) == "'Target'"
    assert _lit(consts[0]["properties"]["position"]) == "'back'"
    assert consts[0]["properties"]["lineColor"]["solid"]["color"]["expr"]["Literal"]["Value"] == "'#FF0000'"
    avg = objects["averageLine"][0]["properties"]
    assert avg["measure"]["expr"]["Measure"]["Property"] == "Net Revenue"
    assert _lit(avg["style"]) == "'dotted'" and _lit(avg["position"]) == "'front'"
    assert _lit(objects["percentileLine"][0]["properties"]["percentile"]) == "90.0D"
    assert "position" not in objects["trend"][0]["properties"]

    lines = tf.list_analytics_lines(state, "overview", "bar1")
    assert [l["kind"] for l in lines] == ["constant", "constant", "average",
                                          "percentile", "trend"]
    assert lines[0]["value"] == 100.0 and lines[0]["label"] == "Target"
    assert lines[0]["color"] == "#FF0000"
    assert lines[2]["measure"] == "Sales.Net Revenue"
    assert lines[3]["transparency"] == 40.0
    assert lines[r2["index"]]["value"] == 50.0
    _golden("analytics_lines", raw)

    out = tf.remove_analytics_line(state, "overview", "bar1", 1)
    assert out["removed"]["value"] == 50.0 and out["remaining"] == 4
    lines = tf.list_analytics_lines(state, "overview", "bar1")
    assert [l["kind"] for l in lines] == ["constant", "average", "percentile", "trend"]
    for _ in range(4):
        tf.remove_analytics_line(state, "overview", "bar1", 0)
    raw = _clean(state, "overview", "bar1")
    assert "objects" not in raw["visual"]
    with pytest.raises(KeyError, match="no analytics lines"):
        tf.remove_analytics_line(state, "overview", "bar1", 0)


def test_analytics_line_errors(state):
    with pytest.raises(ValueError, match="cartesian"):
        tf.add_analytics_line(state, "overview", "card1", "constant", value=1)
    with pytest.raises(ValueError, match="needs value"):
        tf.add_analytics_line(state, "overview", "bar1", "constant")
    with pytest.raises(ValueError, match="percentile"):
        tf.add_analytics_line(state, "overview", "bar1", "percentile")
    with pytest.raises(ValueError, match="kind must be"):
        tf.add_analytics_line(state, "overview", "bar1", "forecast")
    with pytest.raises(ValueError, match="style"):
        tf.add_analytics_line(state, "overview", "bar1", "max", style="wavy")
    with pytest.raises(ValueError, match="position"):
        tf.add_analytics_line(state, "overview", "bar1", "max", position="under")
    with pytest.raises(KeyError, match="Unknown model field"):
        tf.add_analytics_line(state, "overview", "bar1", "min", measure="Sales.Nope")
    state.project.add_visual("overview", {
        "visual_type": "barChart", "id": "stacked",
        "bindings": {"Category": ["Date.Year"], "Y": ["Sales.Net Revenue"]}})
    with pytest.raises(ValueError, match="Trend lines"):
        tf.add_analytics_line(state, "overview", "stacked", "trend")
    tf.add_analytics_line(state, "overview", "bar1", "constant", value=1)
    with pytest.raises(KeyError, match="index must be"):
        tf.remove_analytics_line(state, "overview", "bar1", 5)


# --- 3. tooltip page ------------------------------------------------------------

def test_tooltip_page_bind_and_reset(state):
    project = state.project
    plain = project.create_page("Plain")
    with pytest.raises(ValueError, match="not a tooltip page"):
        tf.set_tooltip_page(state, "overview", "bar1", plain)
    with pytest.raises(KeyError):
        tf.set_tooltip_page(state, "overview", "bar1", "ghost")
    with pytest.raises(ValueError, match="own page"):
        tf.set_tooltip_page(state, "overview", "bar1", "overview")

    tip = project.create_page("Tip")
    project.set_page_role(tip, "tooltip", (320, 240))
    res = tf.set_tooltip_page(state, "overview", "bar1", tip)
    assert res["tooltip_page"] == tip == "tip"
    raw = _clean(state, "overview", "bar1")
    props = raw["visual"]["visualContainerObjects"]["visualTooltip"][0]["properties"]
    assert _lit(props["type"]) == "'ReportPage'"
    assert _lit(props["section"]) == "'tip'"
    assert _lit(props["show"]) == "true"
    assert raw["visual"]["visualContainerObjects"]["title"]      # chrome kept
    assert fx.current_tooltip_page(raw["visual"]["visualContainerObjects"]) == "tip"
    _golden("tooltip_page", raw)

    tf.set_tooltip_page(state, "overview", "bar1", None)
    raw = _clean(state, "overview", "bar1")
    props = raw["visual"]["visualContainerObjects"]["visualTooltip"][0]["properties"]
    assert _lit(props["type"]) == "'Default'" and "section" not in props
    assert fx.current_tooltip_page(raw["visual"]["visualContainerObjects"]) is None


# --- 4. slicer ------------------------------------------------------------------

def test_set_slicer(state):
    with pytest.raises(ValueError, match="needs a 'slicer'"):
        tf.set_slicer(state, "overview", "card1", style="list")
    with pytest.raises(ValueError, match="nothing to change"):
        tf.set_slicer(state, "details", "slicer1")
    with pytest.raises(ValueError, match="style must be"):
        tf.set_slicer(state, "details", "slicer1", style="chiclet")
    with pytest.raises(ValueError, match="need sync_group"):
        tf.set_slicer(state, "details", "slicer1", sync_field_changes=True)

    res = tf.set_slicer(state, "details", "slicer1", style="tile", single_select=True,
                        select_all=False, sync_group="Year", sync_filter_changes=False,
                        search=True, header="Pick a year")
    assert res["sync_group"] == {"groupName": "Year", "fieldChanges": True,
                                 "filterChanges": False}
    raw = _clean(state, "details", "slicer1")
    visual = raw["visual"]
    objects = visual["objects"]
    assert _lit(objects["data"][0]["properties"]["mode"]) == "'Basic'"
    assert _lit(objects["general"][0]["properties"]["orientation"]) == "1L"
    assert _lit(objects["general"][0]["properties"]["selfFilterEnabled"]) == "true"
    assert _lit(objects["selection"][0]["properties"]["singleSelect"]) == "true"
    assert _lit(objects["selection"][0]["properties"]["selectAllCheckboxEnabled"]) == "false"
    assert _lit(objects["header"][0]["properties"]["text"]) == "'Pick a year'"
    assert visual["syncGroup"] == res["sync_group"]
    assert visual["visualContainerObjects"]["title"]                  # untouched
    _golden("slicer", raw)

    # partial update: only passed settings change; '' leaves the sync group
    tf.set_slicer(state, "details", "slicer1", style="dropdown", orientation="vertical",
                  header=False, sync_group="")
    raw = _clean(state, "details", "slicer1")
    objects = raw["visual"]["objects"]
    assert _lit(objects["data"][0]["properties"]["mode"]) == "'Dropdown'"
    assert _lit(objects["general"][0]["properties"]["orientation"]) == "0L"
    assert _lit(objects["general"][0]["properties"]["selfFilterEnabled"]) == "true"
    assert _lit(objects["header"][0]["properties"]["show"]) == "false"
    assert "syncGroup" not in raw["visual"]


# --- 5. data labels -------------------------------------------------------------

def test_data_labels_chart_card_table(state):
    tf.set_data_labels(state, "overview", "bar1", show=True, position="OutsideEnd",
                       display_units="thousands", decimals=1, color="#333333",
                       font_size=10, font_family="Segoe UI", background="#FFFFFF")
    raw = _clean(state, "overview", "bar1")
    props = raw["visual"]["objects"]["labels"][0]["properties"]
    assert _lit(props["show"]) == "true"
    assert _lit(props["labelPosition"]) == "'OutsideEnd'"
    assert _lit(props["labelDisplayUnits"]) == "1000L"
    assert _lit(props["labelPrecision"]) == "1L"
    assert props["color"]["solid"]["color"]["expr"]["Literal"]["Value"] == "'#333333'"
    assert _lit(props["fontSize"]) == "10L"
    assert _lit(props["enableBackground"]) == "true"
    assert props["backgroundColor"]["solid"]["color"]["expr"]["Literal"]["Value"] == "'#FFFFFF'"
    _golden("labels_chart", raw)

    tf.set_data_labels(state, "overview", "card1", color="#111111", font_size=28,
                       display_units=1000000, decimals=0)
    raw = _clean(state, "overview", "card1")
    props = raw["visual"]["objects"]["labels"][0]["properties"]
    assert set(props) == {"color", "fontSize", "labelDisplayUnits", "labelPrecision"}
    assert _lit(props["labelDisplayUnits"]) == "1000000L"
    _golden("labels_card", raw)

    tf.set_data_labels(state, "overview", "table1", color="#222222", font_size=9,
                       font_family="Arial", background="#F5F5F5")
    raw = _clean(state, "overview", "table1")
    props = raw["visual"]["objects"]["values"][0]["properties"]
    assert set(props) == {"fontColor", "fontSize", "fontFamily", "backColor"}
    _golden("labels_table", raw)

    # a second call merges into the same entry (no duplicate objects)
    tf.set_data_labels(state, "overview", "bar1", show=False, background=False)
    props = _raw(state, "overview", "bar1")["visual"]["objects"]["labels"]
    assert len(props) == 1
    assert _lit(props[0]["properties"]["show"]) == "false"
    assert _lit(props[0]["properties"]["enableBackground"]) == "false"
    assert _lit(props[0]["properties"]["labelPosition"]) == "'OutsideEnd'"   # kept


def test_data_labels_rejections(state):
    with pytest.raises(ValueError, match="not supported"):
        tf.set_data_labels(state, "details", "slicer1")
    with pytest.raises(ValueError, match="per-column"):
        tf.set_data_labels(state, "overview", "table1", decimals=2)
    with pytest.raises(ValueError, match="nothing to set"):
        tf.set_data_labels(state, "overview", "table1")
    with pytest.raises(ValueError, match="do not apply to card"):
        tf.set_data_labels(state, "overview", "card1", position="Above")
    with pytest.raises(ValueError, match="display_units"):
        tf.set_data_labels(state, "overview", "bar1", display_units="bazillions")
    with pytest.raises(ValueError, match="hex"):
        tf.set_data_labels(state, "overview", "bar1", color="blue")


# --- 7. visual calculations -----------------------------------------------------

def test_visual_calculations(state):
    assert _raw(state, "overview", "table1")["$schema"].endswith("/1.0.0/schema.json")
    res = tf.add_visual_calculation(state, "overview", "table1", "Running total",
                                    "RUNNINGSUM([Net Revenue])", format_string="#,0")
    assert res["bucket"] == "Values" and res["schema_upgraded"] is True
    assert res["field_names"] == ["Margin %", "Net Revenue"]
    assert "warnings" not in res
    res2 = tf.add_visual_calculation(state, "overview", "table1", "Helper",
                                     "[Nope] * [Net Revenue]", hidden=True)
    assert res2["schema_upgraded"] is False
    assert res2["warnings"] and "[Nope]" in res2["warnings"][0]

    raw = _clean(state, "overview", "table1")
    assert raw["$schema"].endswith("/visualContainer/2.10.0/schema.json")
    projs = raw["visual"]["query"]["queryState"]["Values"]["projections"]
    assert [p["nativeQueryRef"] for p in projs] == [
        "Net Revenue", "Margin %", "Running total", "Helper"]
    calc = projs[2]
    assert calc["field"] == {"NativeVisualCalculation": {
        "Language": "dax", "Expression": "RUNNINGSUM([Net Revenue])",
        "Name": "Running total"}}
    assert calc["queryRef"] == "Running total" and calc["format"] == "#,0"
    assert "hidden" not in calc and projs[3]["hidden"] is True
    # the calculation shows up as a binding of the visual
    assert "Running total" in get_visual(state, "overview", "table1")["bindings"]["Values"]
    _golden("visual_calc", raw)

    calcs = tf.list_visual_calculations(state, "overview", "table1")
    assert [(c["name"], c["hidden"], c["format"]) for c in calcs] == [
        ("Running total", False, "#,0"), ("Helper", True, None)]

    with pytest.raises(ValueError, match="already a field"):
        tf.add_visual_calculation(state, "overview", "table1", "Net Revenue", "1")
    with pytest.raises(ValueError, match="already a field"):
        tf.add_visual_calculation(state, "overview", "table1", "Sales.Net Revenue", "1")
    with pytest.raises(ValueError, match="bucket"):
        tf.add_visual_calculation(state, "overview", "table1", "X", "1", bucket="Rows")
    with pytest.raises(ValueError, match="non-empty"):
        tf.add_visual_calculation(state, "overview", "table1", "X", "  ")

    out = tf.remove_visual_calculation(state, "overview", "table1", "Helper")
    assert out["remaining"] == ["Running total"]
    with pytest.raises(KeyError, match="No visual calculation"):
        tf.remove_visual_calculation(state, "overview", "table1", "Helper")
    _clean(state, "overview", "table1")
    assert state.project.validate_project()["ok"]


def test_visual_calc_default_bucket_for_chart(state):
    res = tf.add_visual_calculation(state, "overview", "bar1", "Share",
                                    "DIVIDE([Net Revenue], COLLAPSEALL([Net Revenue], ROWS))")
    assert res["bucket"] == "Y"
    raw = _clean(state, "overview", "bar1")
    assert raw["visual"]["query"]["queryState"]["Category"]["projections"][0][
        "nativeQueryRef"] == "Year"


def test_ensure_schema_at_least():
    from core.pbir import _VC_SCHEMA

    old = {"$schema": "https://developer.microsoft.com/json-schemas/fabric/item/"
                      "report/definition/visualContainer/1.0.0/schema.json"}
    assert fx.ensure_schema_at_least(old) is True and old["$schema"] == _VC_SCHEMA
    new = {"$schema": _VC_SCHEMA}
    assert fx.ensure_schema_at_least(new) is False
    newer = {"$schema": _VC_SCHEMA.replace("2.10.0", "2.12.0")}
    assert fx.ensure_schema_at_least(newer) is False
    assert fx.ensure_schema_at_least({}) is True


# --- builders -------------------------------------------------------------------

def test_decode_literal_roundtrip():
    from core.formatting import _expr_literal

    for value in ("text", "it's", 3, 2.5, True, False):
        assert fx.decode_literal(_expr_literal(value)) == value
    assert fx.decode_literal({"expr": {"Measure": {}}}) is None


def test_upsert_and_remove_entries():
    objects: dict = {}
    fx.upsert_entry(objects, "values", {"metadata": "A"}, {"x": 1})
    fx.upsert_entry(objects, "values", {"metadata": "A"}, {"y": 2})
    fx.upsert_entry(objects, "values", {"metadata": "B"}, {"x": 3})
    fx.upsert_entry(objects, "values", {}, {"z": 0})              # unscoped entry
    assert [e.get("selector", {}).get("metadata") for e in objects["values"]] == ["A", "B", None]
    assert objects["values"][0]["properties"] == {"x": 1, "y": 2}
    assert fx.remove_props(objects, "values", "A", ["x"]) == 1
    assert fx.remove_props(objects, "values", "A", ["y"]) == 1      # entry now dropped
    assert fx.remove_props(objects, "values", "B", None) == 1
    assert [e["properties"] for e in objects["values"]] == [{"z": 0}]
    assert fx.remove_props(objects, "values", None, ["z"]) == 1 and "values" not in objects


# --- MCP registration + journal integration ---------------------------------------

def _ann(tool, hint):
    a = tool.annotations
    snake = {"readOnlyHint": "read_only_hint", "destructiveHint": "destructive_hint"}[hint]
    return getattr(a, hint, None) if hasattr(a, hint) else getattr(a, snake, None)


def _schema(tool) -> dict:
    return getattr(tool, "inputSchema", None) or getattr(tool, "input_schema")


def _payload(result):
    structured = None
    if isinstance(result, tuple):
        result, structured = result
    structured = (structured or getattr(result, "structured_content", None)
                  or getattr(result, "structuredContent", None))
    if structured is not None:
        return structured.get("result", structured) if isinstance(structured, dict) else structured
    items = [json.loads(c.text) for c in getattr(result, "content", result)]
    return items[0] if len(items) == 1 else items


WRITE_TOOLS = {"pbi_set_conditional_format", "pbi_clear_conditional_format",
               "pbi_add_analytics_line", "pbi_remove_analytics_line",
               "pbi_set_tooltip_page", "pbi_set_slicer", "pbi_set_data_labels",
               "pbi_add_visual_calculation", "pbi_remove_visual_calculation"}
READ_TOOLS = {"pbi_list_analytics_lines", "pbi_list_visual_calculations"}


def test_tools_registered_with_annotations():
    import report_server.server as srv

    tools = {t.name: t for t in asyncio.run(srv.mcp.list_tools())}
    assert WRITE_TOOLS | READ_TOOLS <= set(tools)
    for name in WRITE_TOOLS:
        assert _ann(tools[name], "readOnlyHint") is False, name
        assert "dry_run" in _schema(tools[name])["properties"], name
        assert "dry_run=true" in (tools[name].description or ""), name
    for name in READ_TOOLS:
        assert _ann(tools[name], "readOnlyHint") is True, name
        assert "dry_run" not in _schema(tools[name]).get("properties", {}), name
    for name in ("pbi_clear_conditional_format", "pbi_remove_analytics_line",
                 "pbi_remove_visual_calculation"):
        assert _ann(tools[name], "destructiveHint") is True, name
    assert _ann(tools["pbi_set_data_labels"], "destructiveHint") is False


def test_end_to_end_dry_run_write_undo(tmp_path):
    import report_server.server as srv

    proj = tmp_path / "p"
    shutil.copytree(SYNTH, proj)
    original_state = srv.STATE.project
    try:
        def call(name, **args):
            return _payload(asyncio.run(srv.mcp.call_tool(name, args)))

        call("pbi_set_project", path=str(proj / "Synthetic.pbip"))
        before = snapshot(proj)
        preview = call("pbi_set_data_labels", page_id="overview", visual_id="bar1",
                       show=True, position="InsideEnd", dry_run=True)
        assert preview["dry_run"] is True and "InsideEnd" in preview["diff"]
        assert snapshot(proj) == before

        real = call("pbi_set_data_labels", page_id="overview", visual_id="bar1",
                    show=True, position="InsideEnd")
        assert real["ok"] is True and snapshot(proj) != before
        assert call("pbi_undo_history")[0]["tool"] == "pbi_set_data_labels"
        lines = call("pbi_list_analytics_lines", page_id="overview", visual_id="bar1")
        assert lines == []
        assert call("pbi_undo")["undone"][0]["tool"] == "pbi_set_data_labels"
        assert snapshot(proj) == before
    finally:
        srv.STATE.project = original_state
