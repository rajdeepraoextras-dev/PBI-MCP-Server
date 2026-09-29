"""Report-level measures (reportExtensions.json) and the phone layout."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from core import schema_validate as sv
from core.journal import snapshot
from core.pbir import build_visual_json
from report_server import tools_report_ext as tre
from report_server.server import ReportState, set_project

SYNTH = Path(__file__).parent / "fixtures" / "synthetic"

pytestmark = pytest.mark.skipif(not sv.is_available(),
                                reason="jsonschema/schemas unavailable")

EXT_SCHEMA = ("https://developer.microsoft.com/json-schemas/fabric/item/"
              "report/definition/reportExtension/1.0.0/schema.json")
MOBILE_SCHEMA = ("https://developer.microsoft.com/json-schemas/fabric/item/"
                 "report/definition/visualContainerMobileState/1.0.0/schema.json")


@pytest.fixture
def state(tmp_path) -> ReportState:
    dst = tmp_path / "s"
    shutil.copytree(SYNTH, dst)
    st = ReportState()
    set_project(st, str(dst / "Synthetic.pbip"))
    return st


def _def(state) -> Path:
    return state.project._require_report() / "definition"


def _ext(state) -> dict:
    return json.loads((_def(state) / "reportExtensions.json")
                      .read_text(encoding="utf-8"))


# --- schema vendoring ----------------------------------------------------------

def test_report_extension_schema_vendored_and_validates():
    sample = {"$schema": EXT_SCHEMA, "name": "extension",
              "entities": [{"name": "Sales", "measures": [
                  {"name": "M", "dataType": "Double", "expression": "1",
                   "formatString": "0.0", "description": "d"}]}]}
    assert sv.validate("reportExtension", sample) == []
    bad = {"$schema": EXT_SCHEMA, "name": "extension",
           "entities": [{"name": "Sales", "measures": [{"name": "M"}]}]}
    errs = sv.validate("reportExtension", bad)
    assert errs and any("dataType" in e or "expression" in e for e in errs)
    # the schema is registered in the vendored index under its canonical $id
    index = json.loads((Path(sv._SCHEMA_DIR) / "index.json").read_text("utf-8"))
    assert EXT_SCHEMA in index


def test_mobile_state_schema_validates_position():
    ok = {"$schema": MOBILE_SCHEMA,
          "position": {"x": 0, "y": 0, "width": 320, "height": 100}}
    assert sv.validate("visualContainerMobileState", ok) == []
    assert sv.validate("visualContainerMobileState",
                       {"$schema": MOBILE_SCHEMA})


# --- measures CRUD ---------------------------------------------------------------

def test_create_list_update_delete_round_trip(state):
    assert tre.list_report_measures(state) == []
    res = tre.create_report_measure(
        state, "Sales", "Report Margin", "DIVIDE([Net Revenue], 2)",
        format_string="0.0%", description="Half revenue", data_type="double")
    assert res["ok"] and res["data_type"] == "Double"

    obj = _ext(state)
    assert obj["$schema"] == EXT_SCHEMA and obj["name"] == "extension"
    assert sv.validate("reportExtension", obj) == []
    m = obj["entities"][0]["measures"][0]
    assert m == {"name": "Report Margin", "dataType": "Double",
                 "expression": "DIVIDE([Net Revenue], 2)",
                 "formatString": "0.0%", "description": "Half revenue"}

    listed = tre.list_report_measures(state)
    assert listed == [{"table": "Sales", "name": "Report Margin",
                       "dax": "DIVIDE([Net Revenue], 2)", "data_type": "Double",
                       "format_string": "0.0%", "description": "Half revenue"}]

    tre.update_report_measure(state, "Sales", "Report Margin", dax="1 + 1")
    m = _ext(state)["entities"][0]["measures"][0]
    assert m["expression"] == "1 + 1" and m["formatString"] == "0.0%"
    tre.update_report_measure(state, "Sales", "Report Margin",
                              format_string="", description="")
    m = _ext(state)["entities"][0]["measures"][0]
    assert "formatString" not in m and "description" not in m
    assert sv.validate("reportExtension", _ext(state)) == []

    res = tre.delete_report_measure(state, "Sales", "Report Margin")
    assert res["action"] == "deleted"
    assert tre.list_report_measures(state) == []
    assert _ext(state)["entities"] == []


def test_two_measures_share_an_entity(state):
    tre.create_report_measure(state, "Sales", "A1", "1")
    tre.create_report_measure(state, "Sales", "A2", "2", data_type="Integer")
    tre.create_report_measure(state, "Date", "D1", "3", data_type="Text")
    obj = _ext(state)
    assert [e["name"] for e in obj["entities"]] == ["Sales", "Date"]
    assert len(obj["entities"][0]["measures"]) == 2
    assert sv.validate("reportExtension", obj) == []


@pytest.mark.parametrize("kwargs, match", [
    (dict(table="Nope", name="X", dax="1"), "not in the semantic model"),
    (dict(table="Sales", name="Net Revenue", dax="1"), "already exists in the model"),
    (dict(table="Sales", name="Amount", dax="1"), "already has a column"),
    (dict(table="Sales", name="X", dax="  "), "non-empty DAX"),
    (dict(table="Sales", name="", dax="1"), "non-empty string"),
    (dict(table="Sales", name="X", dax="1", data_type="blob"), "Unknown data_type"),
])
def test_create_validation(state, kwargs, match):
    with pytest.raises(ValueError, match=match):
        tre.create_report_measure(state, **kwargs)
    assert not (_def(state) / "reportExtensions.json").exists()


def test_duplicate_report_measure_rejected(state):
    tre.create_report_measure(state, "Sales", "Dup", "1")
    with pytest.raises(ValueError, match="already exists"):
        tre.create_report_measure(state, "Date", "Dup", "1")


def test_update_unknown_and_empty(state):
    tre.create_report_measure(state, "Sales", "U", "1")
    with pytest.raises(ValueError, match="not found"):
        tre.update_report_measure(state, "Sales", "Missing", dax="1")
    with pytest.raises(ValueError, match="lives in table 'Sales'"):
        tre.update_report_measure(state, "Date", "U", dax="1")
    with pytest.raises(ValueError, match="Nothing to update"):
        tre.update_report_measure(state, "Sales", "U")


# --- delete guard ------------------------------------------------------------------

def _bind_report_measure(state, page, name):
    obj = build_visual_json(
        "rm-card", "card", {"Values": [f"Sales.{name}"]},
        lambda entity, prop: True, position={"x": 0, "y": 500, "width": 200,
                                             "height": 100})
    return state.project.add_visual_raw(page, obj, base="rm-card")


def test_delete_guard_refuses_bound_measure_unless_forced(state):
    tre.create_report_measure(state, "Sales", "Bound", "1")
    vid = _bind_report_measure(state, "overview", "Bound")
    with pytest.raises(ValueError, match="Refusing to delete") as exc:
        tre.delete_report_measure(state, "Sales", "Bound")
    assert "overview/visuals" in str(exc.value) and vid in str(exc.value)
    assert len(tre.list_report_measures(state)) == 1

    res = tre.delete_report_measure(state, "Sales", "Bound", force=True)
    assert res["forced_past"]["bindings"]
    assert tre.list_report_measures(state) == []


def test_delete_guard_sees_filters_and_queryref_only_refs(state):
    tre.create_report_measure(state, "Sales", "Filtered", "1")
    page_json = _def(state) / "pages" / "details" / "page.json"
    data = json.loads(page_json.read_text("utf-8"))
    data["filterConfig"] = {"filters": [{
        "name": "f1", "type": "Advanced",
        "field": {"Measure": {"Expression": {"SourceRef": {"Entity": "Sales"}},
                              "Property": "Filtered"}}}]}
    state.project._write_json(page_json, data)
    with pytest.raises(ValueError, match="details/page.json"):
        tre.delete_report_measure(state, "Sales", "Filtered")


def test_delete_guard_sees_dependent_report_measures(state):
    tre.create_report_measure(state, "Sales", "Base", "1")
    tre.create_report_measure(state, "Sales", "Derived", "[Base] * 2")
    with pytest.raises(ValueError, match="referenced by report measures"):
        tre.delete_report_measure(state, "Sales", "Base")
    tre.delete_report_measure(state, "Sales", "Derived")
    tre.delete_report_measure(state, "Sales", "Base")


def test_unbound_report_measure_does_not_disturb_usage_classifier(state):
    """core.usage only classifies model fields; a bound report measure must
    neither crash it nor be counted as a model column."""
    from core.usage import classify_usage

    tre.create_report_measure(state, "Sales", "Bound2", "1")
    _bind_report_measure(state, "overview", "Bound2")
    usage = classify_usage(state.project)
    assert "Sales.Bound2" not in usage["direct"]["columns"]
    assert "Bound2" not in usage["direct"]["measures"]


# --- through the MCP server ----------------------------------------------------------

def test_tools_registered_with_annotations():
    from tests.test_tooling import _ann, _tools

    import report_server.server as mod

    tools = _tools(mod)
    for name in ("pbi_create_report_measure", "pbi_list_report_measures",
                 "pbi_update_report_measure", "pbi_delete_report_measure",
                 "pbi_set_mobile_layout", "pbi_get_mobile_layout"):
        assert name in tools, name
    assert _ann(tools["pbi_list_report_measures"], "readOnlyHint") is True
    assert _ann(tools["pbi_get_mobile_layout"], "readOnlyHint") is True
    assert _ann(tools["pbi_delete_report_measure"], "destructiveHint") is True
    assert _ann(tools["pbi_create_report_measure"], "readOnlyHint") is False
    assert "dry_run" in tools["pbi_create_report_measure"].inputSchema["properties"] \
        if hasattr(tools["pbi_create_report_measure"], "inputSchema") else True


def test_dry_run_write_then_undo(tmp_path):
    from tests.test_tooling import _payload

    import report_server.server as mod

    proj = tmp_path / "p"
    shutil.copytree(SYNTH, proj)

    def call(tool_name, **args):
        return _payload(asyncio.run(mod.mcp.call_tool(tool_name, args)))

    call("pbi_set_project", path=str(proj / "Synthetic.pbip"))
    before = snapshot(proj)
    preview = call("pbi_create_report_measure", table="Sales", name="PM",
                   dax="1", dry_run=True)
    assert preview["dry_run"] and "reportExtensions.json" in preview["diff"]
    assert snapshot(proj) == before

    real = call("pbi_create_report_measure", table="Sales", name="PM", dax="1")
    assert real["ok"] and snapshot(proj) != before
    assert call("pbi_list_report_measures")[0]["name"] == "PM"
    call("pbi_undo")
    assert snapshot(proj) == before


# --- mobile layout ------------------------------------------------------------------

def _mobile(state, page, vid) -> dict:
    return json.loads((_def(state) / "pages" / page / "visuals" / vid
                       / "mobile.json").read_text("utf-8"))


def test_mobile_auto_stacks_in_reading_order_keeping_aspect(state):
    res = tre.set_mobile_layout(state, "overview", "auto")
    assert res["ok"] and res["mode"] == "auto"
    # card 220x110 -> 320x160 ; bar/table 600x360 -> 320x192 ; gap 8
    assert [(v["visual_id"], v["y"], v["height"]) for v in res["visuals"]] == [
        ("card1", 0, 160), ("bar1", 168, 192), ("table1", 368, 192)]
    assert all(v["x"] == 0 and v["width"] == 320 for v in res["visuals"])
    assert res["canvas_height"] == 560
    assert res["layoutOptimization"] == "PhonePortrait"

    for vid in ("card1", "bar1", "table1"):
        m = _mobile(state, "overview", vid)
        assert m["$schema"] == MOBILE_SCHEMA
        assert sv.validate("visualContainerMobileState", m) == []
    assert _mobile(state, "overview", "bar1")["position"]["tabOrder"] == 1

    report = json.loads((_def(state) / "report.json").read_text("utf-8"))
    assert report["layoutOptimization"] == "PhonePortrait"
    assert state.project.validate_project()["ok"]
    # auto is idempotent
    assert tre.set_mobile_layout(state, "overview", "auto")["visuals"] == res["visuals"]


def test_mobile_auto_min_height_hidden_and_decorative_skipped(state):
    p = state.project
    p.add_visual("overview", {"visual_type": "card", "id": "wide",
                              "bindings": {"Values": ["Sales.Net Revenue"]},
                              "position": {"x": 0, "y": 600, "width": 1000,
                                           "height": 50}})
    p.add_shape("overview", fill="#EEEEEE",
                position={"x": 0, "y": 0, "width": 1280, "height": 100, "z": 0})
    hidden = _def(state) / "pages" / "overview" / "visuals" / "bar1" / "visual.json"
    data = json.loads(hidden.read_text("utf-8"))
    data["isHidden"] = True
    p._write_json(hidden, data)

    res = tre.set_mobile_layout(state, "overview", "auto")
    ids = [v["visual_id"] for v in res["visuals"]]
    assert ids == ["card1", "table1", "wide"]          # no bar1, no shape
    wide = next(v for v in res["visuals"] if v["visual_id"] == "wide")
    assert wide["height"] == 80                          # 50*0.32=16 -> min 80
    assert state.project.validate_project()["ok"]


def test_mobile_manual_round_trip_and_get(state):
    res = tre.set_mobile_layout(state, "overview", "manual", [
        {"visual_id": "card1", "x": 10, "y": 0, "width": 300, "height": 120},
        {"visual_id": "table1", "x": 0, "y": 130, "width": 320, "height": 240}])
    assert res["ok"] and res["mode"] == "manual"
    got = tre.get_mobile_layout(state, "overview")
    assert got["enabled"] and got["canvas"] == {"width": 320, "height": 370}
    assert [v["visual_id"] for v in got["visuals"]] == ["card1", "table1"]
    assert got["unplaced"] == ["bar1"]
    assert got["visuals"][0]["x"] == 10
    for vid in ("card1", "table1"):
        assert sv.validate("visualContainerMobileState",
                           _mobile(state, "overview", vid)) == []

    # re-placing keeps the sidecar's other keys and updates position only
    path = _def(state) / "pages" / "overview" / "visuals" / "card1" / "mobile.json"
    data = json.loads(path.read_text("utf-8"))
    data["visualContainerObjects"] = {"title": [{"properties": {
        "show": {"expr": {"Literal": {"Value": "false"}}}}}]}
    state.project._write_json(path, data, validate=False)
    tre.set_mobile_layout(state, "overview", "manual", [
        {"visual_id": "card1", "x": 0, "y": 5, "width": 320, "height": 100}])
    data = json.loads(path.read_text("utf-8"))
    assert data["position"]["y"] == 5 and "visualContainerObjects" in data


@pytest.mark.parametrize("specs, match", [
    ([{"visual_id": "ghost", "x": 0, "y": 0, "width": 100, "height": 100}],
     "not on page"),
    ([{"visual_id": "card1", "x": 0, "y": 0, "width": 400, "height": 100}],
     "exceeds"),
    ([{"visual_id": "card1", "x": 0, "y": 0, "width": 0, "height": 100}],
     "must be > 0"),
    ([{"visual_id": "card1", "x": -1, "y": 0, "width": 10, "height": 10}],
     "must be >= 0"),
    ([{"visual_id": "card1", "x": 0, "y": 0, "width": 10, "height": 10},
      {"visual_id": "card1", "x": 0, "y": 20, "width": 10, "height": 10}],
     "twice"),
    ([{"visual_id": "card1", "x": 0, "y": 0, "width": "wide", "height": 10}],
     "finite number"),
    ([], "needs visuals"),
])
def test_mobile_manual_validation(state, specs, match):
    with pytest.raises(ValueError, match=match):
        tre.set_mobile_layout(state, "overview", "manual", specs)
    assert not list(_def(state).glob("pages/*/visuals/*/mobile.json"))


def test_mobile_off_removes_layout_and_resets_flag(state):
    tre.set_mobile_layout(state, "overview", "auto")
    assert list(_def(state).glob("pages/overview/visuals/*/mobile.json"))
    res = tre.set_mobile_layout(state, "overview", "off")
    assert sorted(res["removed"]) == ["bar1", "card1", "table1"]
    assert res["layoutOptimization"] == "None"
    assert not list(_def(state).glob("pages/*/visuals/*/mobile.json"))
    assert not tre.get_mobile_layout(state, "overview")["enabled"]
    # off on an already-off page is a no-op
    assert tre.set_mobile_layout(state, "overview", "off")["removed"] == []


def test_mobile_flag_stays_on_while_another_page_has_layout(state):
    tre.set_mobile_layout(state, "overview", "auto")
    tre.set_mobile_layout(state, "details", "auto")
    assert tre.set_mobile_layout(state, "overview", "off")["layoutOptimization"] \
        == "PhonePortrait"
    assert tre.set_mobile_layout(state, "details", "off")["layoutOptimization"] \
        == "None"


def test_mobile_bad_mode_and_page(state):
    with pytest.raises(ValueError, match="mode must be"):
        tre.set_mobile_layout(state, "overview", "tablet")
    with pytest.raises(ValueError, match="Page 'nope' not found"):
        tre.get_mobile_layout(state, "nope")
    with pytest.raises(ValueError, match="takes no visuals"):
        tre.set_mobile_layout(state, "overview", "auto", [{"visual_id": "card1"}])


def test_mobile_via_mcp_dry_run_and_undo(tmp_path):
    from tests.test_tooling import _payload

    import report_server.server as mod

    proj = tmp_path / "p"
    shutil.copytree(SYNTH, proj)

    def call(tool_name, **args):
        return _payload(asyncio.run(mod.mcp.call_tool(tool_name, args)))

    call("pbi_set_project", path=str(proj / "Synthetic.pbip"))
    before = snapshot(proj)
    preview = call("pbi_set_mobile_layout", page_id="overview", mode="auto",
                   dry_run=True)
    assert preview["dry_run"] and "mobile.json" in preview["diff"]
    assert snapshot(proj) == before
    call("pbi_set_mobile_layout", page_id="overview", mode="auto")
    assert call("pbi_get_mobile_layout", page_id="overview")["enabled"]
    call("pbi_undo")
    assert snapshot(proj) == before
