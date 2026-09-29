"""Deneb (Vega / Vega-Lite) templates, spec editing and their tools.

No network: the visual GUID and property names are the constants hard-coded in
core/deneb.py (resolved once from the Deneb repository, see its docstring).
"""

from __future__ import annotations

import asyncio
import copy
import itertools
import json
import re
import shutil
from pathlib import Path

import pytest

from core import deneb, schema_validate
from core.journal import snapshot
from core.pbip import PbipProject
from core.pbir import visual_bindings
from core.theme import generate_theme
from report_server import tools_deneb
from report_server.server import ReportState, set_project

SYNTH = Path(__file__).parent / "fixtures" / "synthetic"
NO_DENEB = "Deneb custom visual must be present"

#: template -> (bindings on the synthetic model, options variants)
CASES: dict[str, tuple[dict, list[dict]]] = {
    "bar": ({"category": "Date.Year", "value": "Sales.Net Revenue"},
            [{}, {"labels": True}, {"orientation": "vertical", "sort": "ascending"}]),
    "stacked_bar": ({"category": "Date.Year", "value": "Sales.Net Revenue",
                     "series": "Sales.OrderDate"},
                    [{}, {"labels": True, "normalize": True}]),
    "line": ({"category": "Date.Date", "value": "Sales.Net Revenue"},
             [{}, {"points": True, "labels": True, "interpolate": "monotone"}]),
    "area": ({"category": "Date.Date", "value": "Sales.Net Revenue"},
             [{}, {"normalize": True}]),
    "scatter": ({"detail": "Date.Year", "x": "Sales.Net Revenue", "y": "Sales.Margin %"},
                [{}, {"labels": True}]),
    "heatmap": ({"x": "Date.Year", "y": "Sales.OrderDate", "value": "Sales.Net Revenue"},
                [{}, {"labels": True}]),
    "histogram": ({"detail": "Date.Year", "value": "Sales.Net Revenue"},
                  [{}, {"bins": 5, "orientation": "horizontal"}]),
    "box_plot": ({"category": "Date.Year", "detail": "Date.Date", "value": "Sales.Net Revenue"},
                 [{}, {"orientation": "horizontal"}]),
    "bullet": ({"category": "Date.Year", "value": "Sales.Net Revenue",
                "target": "Sales.Complex Measure"}, [{}, {"labels": True}]),
    "sparkline": ({"category": "Date.Date", "value": "Sales.Net Revenue"},
                  [{}, {"end_point": False}]),
    "waffle": ({"category": "Date.Year", "value": "Sales.Net Revenue"}, [{}, {"columns": 20}]),
    "dumbbell": ({"category": "Date.Year", "start": "Sales.Net Revenue",
                  "end": "Sales.Margin %"}, [{}, {"labels": True}]),
}
CASE_PARAMS = [(name, bindings, opts) for name, (bindings, variants) in CASES.items()
               for opts in variants]

#: names created by the templates' own transforms (not dataset fields)
DERIVED = {"wfTotal", "wfCount", "wfEnd", "wfStart", "wfSeq", "wfCell", "wfCol", "wfRow",
           "hmMax", "snTotal", "snShare", "spRow", "spLast", "dbMeasure", "dbValue"}


@pytest.fixture
def root(tmp_path) -> Path:
    dst = tmp_path / "s"
    shutil.copytree(SYNTH, dst)
    return dst


@pytest.fixture
def state(root) -> ReportState:
    st = ReportState()
    set_project(st, str(root / "Synthetic.pbip"))
    return st


def _visual(state, page: str, vid: str) -> dict:
    return json.loads(state.require()._visual_file(page, vid).read_text(encoding="utf-8"))


def _prop(vjson: dict, name: str) -> str:
    """A vega property, decoded from its Literal expression."""
    node = vjson["visual"]["objects"]["vega"][0]["properties"][name]
    value = node["expr"]["Literal"]["Value"]
    assert isinstance(value, str)
    return value


def _unquote(literal: str) -> str:
    assert literal.startswith("'") and literal.endswith("'")
    return literal[1:-1].replace("''", "'")


def _spec(vjson: dict) -> dict:
    return json.loads(_unquote(_prop(vjson, "jsonSpec")))


def _references(node, found: set | None = None) -> set:
    """Every dataset/derived field name a spec refers to."""
    found = set() if found is None else found
    if isinstance(node, dict):
        if isinstance(node.get("field"), str):
            found.add(node["field"])
        for key in ("groupby", "fold"):
            if isinstance(node.get(key), list):
                found.update(x for x in node[key] if isinstance(x, str))
        for value in node.values():
            _references(value, found)
    elif isinstance(node, list):
        for value in node:
            _references(value, found)
    elif isinstance(node, str):
        for m in re.finditer(r'datum\[("(?:[^"\\]|\\.)*")\]', node):
            found.add(json.loads(m.group(1)))
    return found


# --- resolved identifiers ---------------------------------------------------------------

def test_visual_guid_and_property_names_are_the_resolved_constants():
    assert deneb.DENEB_VISUAL_GUID == "deneb7E15AEF80B9E4D4F8E12924291ECE89A"
    assert re.fullmatch(r"deneb[0-9A-F]{32}", deneb.DENEB_VISUAL_GUID)
    assert deneb.DATA_ROLE == "dataset" and deneb.SPEC_OBJECT == "vega"
    assert {"jsonSpec", "jsonConfig", "provider", "version", "renderMode"} <= set(deneb.VEGA_PROPERTIES)
    assert deneb.PROVIDERS == ("vegaLite", "vega")
    assert deneb.is_deneb_type(deneb.DENEB_VISUAL_GUID) and not deneb.is_deneb_type("card")
    assert not deneb.is_deneb_type(None)


def test_dataset_field_names_follow_deneb_encoding():
    assert deneb.dataset_field_name("Net Revenue") == "Net Revenue"
    assert deneb.dataset_field_name("Margin %") == "Margin %"
    assert deneb.dataset_field_name('Avg. [Price] "x"\\') == "Avg_ _Price_ _x__"


# --- catalogue -------------------------------------------------------------------------------

def test_template_catalogue(state):
    catalogue = tools_deneb.list_deneb_templates(state)
    assert [t["name"] for t in catalogue] == list(CASES)
    json.dumps(catalogue)                                            # JSON-serialisable
    for tpl in catalogue:
        assert tpl["description"] and tpl["title"]
        assert all({"role", "required", "kind", "description"} <= set(b) for b in tpl["bindings"])
        assert any(b["required"] for b in tpl["bindings"])
        assert tpl["example"]["template"] == tpl["name"] and tpl["example"]["bindings"]
        for name, opt in tpl["options"].items():
            assert {"type", "default", "description"} <= set(opt), name
    bar = next(t for t in catalogue if t["name"] == "bar")
    assert {b["role"] for b in bar["bindings"]} == {"category", "value", "series"}
    assert {"orientation", "labels", "color", "format"} <= set(bar["options"])
    assert tools_deneb.list_deneb_templates(ReportState())           # needs no project


# --- every template -----------------------------------------------------------------------------

@pytest.mark.parametrize("template, bindings, options", CASE_PARAMS,
                         ids=[f"{t}-{'-'.join(o) or 'default'}" for t, _, o in CASE_PARAMS])
def test_template_produces_a_schema_valid_bound_visual(state, template, bindings, options):
    res = tools_deneb.add_deneb_visual(state, "details", template, bindings, options=options)
    assert res["ok"] and res["template"] == template
    vjson = _visual(state, "details", res["visual_id"])
    assert schema_validate.validate("visualContainer", vjson) == []
    assert state.require().validate_project()["ok"]

    visual = vjson["visual"]
    assert visual["visualType"] == deneb.DENEB_VISUAL_GUID
    # the bound fields are projected into the dataset role, in role order, no duplicates
    projections = visual["query"]["queryState"]["dataset"]["projections"]
    tpl = deneb.TEMPLATES[template]
    expected = [bindings[r.name] for r in tpl.roles if r.name in bindings]
    assert [p["queryRef"] for p in projections] == list(dict.fromkeys(expected))
    project = state.require()
    for p in projections:
        entity, _, prop = p["queryRef"].partition(".")
        kind = "Measure" if project._is_measure(entity, prop) else "Column"
        assert set(p["field"]) == {kind}
        assert p["field"][kind]["Property"] == prop
        assert p["field"][kind]["Expression"]["SourceRef"]["Entity"] == entity

    # objects.vega: every property is a string Literal expression
    props = visual["objects"]["vega"][0]["properties"]
    assert {"provider", "version", "jsonSpec", "jsonConfig", "renderMode"} <= set(props)
    for name, node in props.items():
        assert set(node) == {"expr"} and set(node["expr"]) == {"Literal"}, name
    assert _unquote(_prop(vjson, "provider")) == "vegaLite"
    assert _unquote(_prop(vjson, "version")) == deneb.PROVIDER_VERSIONS["vegaLite"]
    assert _unquote(_prop(vjson, "renderMode")) == "svg"
    assert _prop(vjson, "enableTooltips") == "true"

    # the spec is JSON, reads the `dataset`, and only touches bound fields
    spec = _spec(vjson)
    assert spec["$schema"] == deneb.SCHEMA_URLS["vegaLite"]
    assert spec["data"] == {"name": "dataset"}
    assert "width" not in spec and "height" not in spec              # Deneb sizes to the container
    dataset_fields = set(res["dataset_fields"].values())
    assert _references(spec) - {"Count"} <= dataset_fields | DERIVED
    # ... and uses all of them, except a `detail` that only defines the dataset's rows
    row_only = {res["dataset_fields"]["detail"]} if template in ("histogram", "box_plot") else set()
    assert dataset_fields - _references(spec) == row_only
    config = json.loads(_unquote(_prop(vjson, "jsonConfig")))
    assert config["range"]["category"] and config["view"]["stroke"] == "transparent"

    # readable back through the public reader
    read = deneb.read_deneb_visual(vjson)
    assert read["spec"] == spec and read["config"] == config and read["provider"] == "vegaLite"
    assert visual_bindings(project.get_visual("details", res["visual_id"])) == {
        "dataset": list(dict.fromkeys(expected))}


def test_all_templates_can_share_a_page_and_stay_valid(state):
    ids = [tools_deneb.add_deneb_visual(state, "details", t, b)["visual_id"]
           for t, (b, _) in CASES.items()]
    assert len(set(ids)) == len(CASES)
    assert state.require().validate_project()["ok"]
    report = json.loads((state.require().report_dir / "definition" / "report.json").read_text())
    assert report["publicCustomVisuals"] == [deneb.DENEB_VISUAL_GUID]   # registered once


# --- template behaviour ---------------------------------------------------------------------------

def _build(state, template, extra=None, options=None):
    bindings = {**CASES[template][0], **(extra or {})}
    project = state.require()
    fields = deneb.resolve_bindings(project, deneb.TEMPLATES[template], bindings)
    spec, config = deneb.build_spec(template, fields, options, deneb.project_theme(project))
    return spec, config


def test_bar_orientation_labels_series_and_sort(state):
    spec, _ = _build(state, "bar")
    assert spec["encoding"]["y"]["field"] == "Year" and spec["encoding"]["x"]["field"] == "Net Revenue"
    assert spec["mark"]["type"] == "bar" and "layer" not in spec
    vertical, _ = _build(state, "bar", options={"orientation": "vertical"})
    assert vertical["encoding"]["x"]["field"] == "Year"
    assert deneb.TEMPLATES["bar"].options["orientation"]["default"] == "horizontal"
    labelled, _ = _build(state, "bar", options={"labels": True})
    assert [layer["mark"]["type"] for layer in labelled["layer"]] == ["bar", "text"]
    clustered, _ = _build(state, "bar", {"series": "Sales.OrderDate"})
    assert clustered["encoding"]["color"]["field"] == "OrderDate"
    assert clustered["encoding"]["yOffset"] == {"field": "OrderDate"}
    natural, _ = _build(state, "bar", options={"sort": "natural"})
    assert "sort" not in natural["encoding"]["y"]
    descending, _ = _build(state, "bar", options={"sort": "descending"})
    assert descending["encoding"]["y"]["sort"] == "-x"


def test_stacked_bar_normalize_and_labels_show_the_share(state):
    spec, _ = _build(state, "stacked_bar", options={"normalize": True, "labels": True})
    assert spec["encoding"]["x"]["stack"] == "normalize"
    text = spec["layer"][1]
    assert text["encoding"]["text"] == {"field": "snShare", "type": "quantitative", "format": ".0%"}
    assert text["transform"][0]["groupby"] == ["Year"]
    assert text["encoding"]["detail"]["field"] == "OrderDate"        # same stack groups as the bars


def test_options_reach_the_spec_and_config(state):
    spec, config = _build(state, "bar", options={"color": "#0B7A75", "palette": ["#111111", "#222222"]})
    assert spec["mark"]["color"] == "#0B7A75"
    assert config["range"]["category"] == ["#111111", "#222222"]
    scatter, _ = _build(state, "scatter", {"size": "Sales.Margin %", "series": "Sales.OrderDate"},
                        {"labels": True})
    point_layer, label_layer = scatter["layer"]
    assert {"size", "color"} <= set(point_layer["encoding"])         # not inherited by the labels
    assert set(label_layer["encoding"]) == {"text"}
    waffle, _ = _build(state, "waffle", options={"columns": 5})
    assert any("% 5" in t.get("calculate", "") for t in waffle["transform"])
    hist, _ = _build(state, "histogram", options={"bins": 7})
    assert hist["encoding"]["x"]["bin"] == {"maxbins": 7}


def test_report_theme_colors_are_used(state):
    state.require().set_report_theme(generate_theme("#0B7A75", name="Teal"))
    spec, config = _build(state, "bar")
    assert spec["mark"]["color"] == "#0B7A75"
    assert config["range"]["category"][0] == "#0B7A75"
    assert len(config["range"]["category"]) == 8


def test_temporal_columns_get_temporal_or_discrete_encodings(state):
    line, _ = _build(state, "line")
    assert line["encoding"]["x"]["type"] == "temporal"               # Date.Date is a dateTime
    bar, _ = _build(state, "bar", {"category": "Date.Date"})
    assert bar["encoding"]["y"]["type"] == "ordinal" and bar["encoding"]["y"]["timeUnit"] == "yearmonthdate"
    year, _ = _build(state, "bar")
    assert year["encoding"]["y"]["type"] == "ordinal"                # numeric key: discrete


def test_fields_with_reserved_characters_use_deneb_names(state):
    project = state.require()
    project.create_measure("Sales", "Avg. Price [net]", "1")
    res = tools_deneb.add_deneb_visual(
        state, "details", "bar", {"category": "Date.Year", "value": "Sales.Avg. Price [net]"})
    assert res["dataset_fields"]["value"] == "Avg_ Price _net_"
    vjson = _visual(state, "details", res["visual_id"])
    assert "Avg_ Price _net_" in _references(_spec(vjson))
    assert vjson["visual"]["query"]["queryState"]["dataset"]["projections"][1]["queryRef"] \
        == "Sales.Avg. Price [net]"                                  # the model name is untouched


def test_column_aggregation_binding(state):
    res = tools_deneb.add_deneb_visual(
        state, "details", "bar", {"category": "Date.Year", "value": "Sum(Sales.Amount)"})
    proj = _visual(state, "details", res["visual_id"])["visual"]["query"]["queryState"]["dataset"]["projections"][1]
    assert proj["queryRef"] == "Sum(Sales.Amount)" and "Aggregation" in proj["field"]
    assert proj["displayName"] == "Amount"                           # keeps the dataset field name
    assert res["dataset_fields"]["value"] == "Amount"
    assert state.require().validate_project()["ok"]


def test_role_names_are_case_insensitive_and_accept_single_item_lists(state):
    res = tools_deneb.add_deneb_visual(
        state, "details", "bar", {"Category": ["Date.Year"], "VALUE": "Sales.Net Revenue"})
    assert res["ok"]


# --- validation ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("bindings, message", [
    ({"category": "Date.Year"}, "needs binding"),
    ({"category": "Date.Year", "value": "Sales.Net Revenue", "bogus": "Date.Year"}, "Unknown binding role"),
    ({"category": "Sales.Net Revenue", "value": "Sales.Net Revenue"}, "needs a column"),
    ({"category": "Date.Year", "value": "Date.Date"}, "needs a measure or a numeric column"),
    ({"category": "Nope.Year", "value": "Sales.Net Revenue"}, "Unknown table"),
    ({"category": "Date.Nope", "value": "Sales.Net Revenue"}, "neither a column nor a measure"),
    ({"category": "Year", "value": "Sales.Net Revenue"}, "Table.Column"),
    ({"category": ["Date.Year", "Date.Date"], "value": "Sales.Net Revenue"}, "exactly one field"),
    ({"category": "Date.Year", "value": "Sum(Sales.Net Revenue)"}, "cannot be aggregated"),
    ({}, "bindings must be"),
])
def test_binding_errors_are_actionable(state, bindings, message):
    with pytest.raises(ValueError, match=message):
        tools_deneb.add_deneb_visual(state, "details", "bar", bindings)
    assert not (state.require().report_dir / "definition" / "pages" / "details" / "visuals"
                / "deneb").exists()                                   # nothing was written


def test_fields_that_collide_in_the_dataset_are_rejected(state):
    state.require().create_column("Sales", "Year", "int64")
    with pytest.raises(ValueError, match="distinct names"):
        tools_deneb.add_deneb_visual(state, "details", "bar",
                                     {"category": "Date.Year", "value": "Sales.Year"})


@pytest.mark.parametrize("options, message", [
    ({"nope": 1}, "Unknown option"),
    ({"labels": "yes"}, "true or false"),
    ({"orientation": "diagonal"}, "one of"),
    ({"color": "red"}, "#RRGGBB"),
    ({"palette": []}, "palette"),
    ({"palette": ["red"]}, "palette"),
    ({"format": 3}, "must be a string"),
])
def test_option_errors(state, options, message):
    with pytest.raises(ValueError, match=message):
        tools_deneb.add_deneb_visual(state, "details", "bar", CASES["bar"][0], options=options)
    with pytest.raises(ValueError, match="options must be"):
        tools_deneb.add_deneb_visual(state, "details", "bar", CASES["bar"][0], options="x")


def test_numeric_option_ranges(state):
    with pytest.raises(ValueError, match="bins"):
        tools_deneb.add_deneb_visual(state, "details", "histogram", CASES["histogram"][0],
                                     options={"bins": 1})
    with pytest.raises(ValueError, match="integer"):
        tools_deneb.add_deneb_visual(state, "details", "waffle", CASES["waffle"][0],
                                     options={"columns": 2.5})
    with pytest.raises(ValueError, match="columns"):
        tools_deneb.add_deneb_visual(state, "details", "waffle", CASES["waffle"][0],
                                     options={"columns": 99})


def test_unknown_template_and_page(state):
    with pytest.raises(ValueError, match="Unknown Deneb template"):
        tools_deneb.add_deneb_visual(state, "details", "pie", CASES["bar"][0])
    with pytest.raises(ValueError, match="Page 'nope' not found"):
        tools_deneb.add_deneb_visual(state, "nope", "bar", CASES["bar"][0])
    res = tools_deneb.add_deneb_visual(state, "details", "Stacked-Bar", CASES["stacked_bar"][0])
    assert res["template"] == "stacked_bar"                          # tolerant name matching


def test_requires_a_project():
    with pytest.raises(ValueError, match="No project set"):
        tools_deneb.add_deneb_visual(ReportState(), "details", "bar", CASES["bar"][0])


# --- placement, title, registration -------------------------------------------------------------------------

def test_default_position_is_a_free_slot(state):
    res = tools_deneb.add_deneb_visual(state, "details", "bar", CASES["bar"][0])
    pos = res["position"]
    assert (pos["width"], pos["height"]) == (480, 320) and "warnings" not in res
    slicer = (16, 16, 216, 316)                                      # details/slicer1
    assert pos["x"] + pos["width"] <= slicer[0] or slicer[2] <= pos["x"]
    assert pos["x"] >= 16 and pos["y"] >= 16
    assert pos["x"] + pos["width"] <= 1280 and pos["y"] + pos["height"] <= 720
    second = tools_deneb.add_deneb_visual(state, "details", "line", CASES["line"][0])["position"]
    a, b = pos, second
    assert (a["x"] + a["width"] <= b["x"] or b["x"] + b["width"] <= a["x"]
            or a["y"] + a["height"] <= b["y"] or b["y"] + b["height"] <= a["y"])   # no overlap
    vjson = _visual(state, "details", res["visual_id"])
    assert vjson["position"]["z"] == 1                               # on top of slicer1 (z 0)


def test_crowded_page_uses_the_space_left_and_warns(state):
    res = tools_deneb.add_deneb_visual(state, "overview", "bar", CASES["bar"][0])
    pos = res["position"]
    assert pos["y"] >= 500 and pos["y"] + pos["height"] <= 720       # below the content, on the page
    assert res["warnings"] and "below the existing content" in res["warnings"][0]


def test_explicit_position_and_title(state):
    res = tools_deneb.add_deneb_visual(
        state, "details", "bar", CASES["bar"][0],
        position={"x": 500, "y": 40, "width": 400, "height": 250}, title="Owner's view")
    vjson = _visual(state, "details", res["visual_id"])
    assert (vjson["position"]["x"], vjson["position"]["y"], vjson["position"]["width"],
            vjson["position"]["height"]) == (500, 40, 400, 250)
    title = vjson["visual"]["visualContainerObjects"]["title"][0]["properties"]
    assert title["text"]["expr"]["Literal"]["Value"] == "'Owner''s view'"   # quote-escaped
    assert title["show"]["expr"]["Literal"]["Value"] == "true"
    listed = state.require().list_visuals("details")
    assert next(v for v in listed if v.id == res["visual_id"]).title == "Owner's view"
    with pytest.raises(ValueError, match="position.width must be a number"):
        tools_deneb.add_deneb_visual(state, "details", "bar", CASES["bar"][0],
                                     position={"width": "wide"})
    with pytest.raises(ValueError, match="position must be an object"):
        tools_deneb.add_deneb_visual(state, "details", "bar", CASES["bar"][0], position=[1, 2])
    assert schema_validate.validate("visualContainer", vjson) == []


def test_registration_in_report_json(state):
    project = state.require()
    report = project.report_dir / "definition" / "report.json"
    assert "publicCustomVisuals" not in json.loads(report.read_text())
    first = tools_deneb.add_deneb_visual(state, "details", "bar", CASES["bar"][0])
    assert first["custom_visual_registered"] is True and NO_DENEB in first["note"]
    assert json.loads(report.read_text())["publicCustomVisuals"] == [deneb.DENEB_VISUAL_GUID]
    second = tools_deneb.add_deneb_visual(state, "details", "line", CASES["line"][0])
    assert second["custom_visual_registered"] is False
    assert json.loads(report.read_text())["publicCustomVisuals"] == [deneb.DENEB_VISUAL_GUID]
    assert project.validate_project()["ok"]


@pytest.mark.parametrize("extra", [
    {"publicCustomVisuals": [deneb.DENEB_VISUAL_GUID]},
    {"organizationCustomVisuals": [{"name": "Deneb", "path": "orgstore/deneb.pbiviz"}]},
    {"resourcePackages": [{"name": "deneb7E15", "type": "CustomVisual", "items": []}]},
])
def test_existing_availability_is_left_alone(state, extra):
    project = state.require()
    report = project.report_dir / "definition" / "report.json"
    data = json.loads(report.read_text())
    data.update(extra)
    project._write_json(report, data)
    before = report.read_text()
    assert deneb.is_registered(data) and deneb.ensure_registered(project) is False
    res = tools_deneb.add_deneb_visual(state, "details", "bar", CASES["bar"][0])
    assert res["custom_visual_registered"] is False and report.read_text() == before


# --- set spec ---------------------------------------------------------------------------------------------------

CUSTOM_SPEC = {
    "$schema": "https://vega.github.io/schema/vega-lite/v6.json",
    "data": {"name": "dataset"},
    "title": "Owner's \"quoted\" view — ünï",
    "mark": {"type": "arc", "innerRadius": 40},
    "encoding": {"theta": {"field": "Net Revenue", "type": "quantitative"},
                 "color": {"field": "Year", "type": "nominal"}},
}
CUSTOM_CONFIG = {"view": {"stroke": None}, "font": "Consolas"}


def test_set_spec_round_trip(state):
    added = tools_deneb.add_deneb_visual(state, "details", "bar", CASES["bar"][0], title="T")
    vid = added["visual_id"]
    before = _visual(state, "details", vid)
    res = tools_deneb.set_deneb_spec(state, "details", vid, copy.deepcopy(CUSTOM_SPEC), CUSTOM_CONFIG)
    assert res["ok"] and res["provider"] == "vegaLite" and res["config_updated"] is True
    assert "warnings" not in res and NO_DENEB in res["note"]
    after = _visual(state, "details", vid)
    assert schema_validate.validate("visualContainer", after) == []
    read = deneb.read_deneb_visual(after)
    assert read["spec"] == CUSTOM_SPEC and read["config"] == CUSTOM_CONFIG    # exact round trip
    # the raw literal is a quoted string with doubled apostrophes
    raw = _prop(after, "jsonSpec")
    assert raw.startswith("'") and "Owner''s" in raw
    # only the spec/config changed
    assert after["visual"]["query"] == before["visual"]["query"]
    assert after["position"] == before["position"]
    assert after["visual"]["visualContainerObjects"] == before["visual"]["visualContainerObjects"]
    assert _prop(after, "enableTooltips") == "true" and _prop(after, "renderMode") == "'svg'"
    # a second identical call is a no-op on disk
    snap = snapshot(state.require().path.parent)
    tools_deneb.set_deneb_spec(state, "details", vid, copy.deepcopy(CUSTOM_SPEC), CUSTOM_CONFIG)
    assert snapshot(state.require().path.parent) == snap


def test_set_spec_accepts_json_text_and_keeps_config_when_omitted(state):
    vid = tools_deneb.add_deneb_visual(state, "details", "bar", CASES["bar"][0])["visual_id"]
    config_before = deneb.read_deneb_visual(_visual(state, "details", vid))["config"]
    res = tools_deneb.set_deneb_spec(state, "details", vid, json.dumps(CUSTOM_SPEC))
    assert res["config_updated"] is False
    read = deneb.read_deneb_visual(_visual(state, "details", vid))
    assert read["spec"] == CUSTOM_SPEC and read["config"] == config_before


def test_set_spec_detects_vega_and_warns_without_dataset(state):
    vid = tools_deneb.add_deneb_visual(state, "details", "bar", CASES["bar"][0])["visual_id"]
    vega = {"$schema": "https://vega.github.io/schema/vega/v6.json", "width": 300,
            "data": [{"name": "dataset"}], "marks": []}
    res = tools_deneb.set_deneb_spec(state, "details", vid, vega)
    assert res["provider"] == "vega" and res["version"] == deneb.PROVIDER_VERSIONS["vega"]
    assert "warnings" not in res
    read = deneb.read_deneb_visual(_visual(state, "details", vid))
    assert read["provider"] == "vega" and read["version"] == deneb.PROVIDER_VERSIONS["vega"]
    res = tools_deneb.set_deneb_spec(state, "details", vid, {"mark": "bar", "data": {"values": []}})
    assert res["provider"] == "vegaLite" and "dataset" in res["warnings"][0]


def test_set_spec_errors(state):
    vid = tools_deneb.add_deneb_visual(state, "details", "bar", CASES["bar"][0])["visual_id"]
    with pytest.raises(ValueError, match="not a Deneb visual"):
        tools_deneb.set_deneb_spec(state, "overview", "bar1", CUSTOM_SPEC)
    with pytest.raises(ValueError, match="not valid JSON"):
        tools_deneb.set_deneb_spec(state, "details", vid, "{oops")
    with pytest.raises(ValueError, match="spec must be a JSON object"):
        tools_deneb.set_deneb_spec(state, "details", vid, [1, 2])
    with pytest.raises(ValueError, match="config must be a JSON object"):
        tools_deneb.set_deneb_spec(state, "details", vid, CUSTOM_SPEC, config="[1]")
    with pytest.raises(FileNotFoundError):
        tools_deneb.set_deneb_spec(state, "details", "missing", CUSTOM_SPEC)
    with pytest.raises(ValueError, match="Not a Deneb visual"):
        deneb.read_deneb_visual(_visual(state, "overview", "bar1"))


def test_set_spec_on_a_bare_deneb_visual_from_elsewhere(state):
    """A Deneb visual made by hand (or by Desktop) without objects.vega yet."""
    project = state.require()
    schema = ("https://developer.microsoft.com/json-schemas/fabric/item/report/definition/"
              "visualContainer/2.10.0/schema.json")
    raw = {"$schema": schema, "name": "hand", "position": {"x": 300, "y": 20, "width": 300, "height": 200},
           "visual": {"visualType": deneb.DENEB_VISUAL_GUID,
                      "query": {"queryState": {"dataset": {"projections": [
                          {"field": {"Column": {"Expression": {"SourceRef": {"Entity": "Date"}},
                                                "Property": "Year"}}, "queryRef": "Date.Year"}]}}}}}
    vid = project.add_visual_raw("details", raw, base="hand")
    tools_deneb.set_deneb_spec(state, "details", vid, CUSTOM_SPEC)
    after = _visual(state, "details", vid)
    assert schema_validate.validate("visualContainer", after) == []
    read = deneb.read_deneb_visual(after)
    assert read["spec"] == CUSTOM_SPEC and read["config"] == {}


def test_detect_provider():
    assert deneb.detect_provider({"$schema": "https://vega.github.io/schema/vega-lite/v5.json"}) == "vegaLite"
    assert deneb.detect_provider({"$schema": "https://vega.github.io/schema/vega/v5.json"}) == "vega"
    assert deneb.detect_provider({"layer": []}) == "vegaLite"
    assert deneb.detect_provider({"signals": []}) == "vega"
    assert deneb.detect_provider({}, default="vega") == "vega"


# --- MCP wiring -----------------------------------------------------------------------------------------------------

def _tools():
    from report_server.server import mcp

    return {t.name: t for t in asyncio.run(mcp.list_tools())}


def _hint(tool, name: str):
    snake = {"readOnlyHint": "read_only_hint", "destructiveHint": "destructive_hint",
             "idempotentHint": "idempotent_hint"}[name]
    a = tool.annotations
    return getattr(a, name) if hasattr(a, name) else getattr(a, snake)


def _schema(tool) -> dict:
    return getattr(tool, "inputSchema", None) or getattr(tool, "input_schema")


def test_tools_are_registered_annotated_and_say_deneb_must_be_present():
    tools = _tools()
    names = ("pbi_list_deneb_templates", "pbi_add_deneb_visual", "pbi_set_deneb_spec")
    for name in names:
        assert name in tools
        assert NO_DENEB in " ".join((tools[name].description or "").split()), name
    listing, add, setter = (tools[n] for n in names)
    assert _hint(listing, "readOnlyHint") is True and "dry_run" not in _schema(listing)["properties"]
    assert _hint(add, "readOnlyHint") is False and _hint(add, "destructiveHint") is False
    assert _hint(setter, "destructiveHint") is True and _hint(setter, "idempotentHint") is True
    for tool in (add, setter):                                       # writes get dry_run injected
        assert _schema(tool)["properties"]["dry_run"]["type"] == "boolean"
    assert set(_schema(add)["required"]) == {"page_id", "template", "bindings"}
    assert all(n.startswith("pbi_") for n in names)
    every = [t.name for t in tools.values()]
    assert len(every) == len(set(every))


def _call(tool_name: str, /, **args):
    from report_server.server import mcp

    result = asyncio.run(mcp.call_tool(tool_name, args))
    if isinstance(result, tuple):
        result = result[0]
    structured = getattr(result, "structured_content", None) \
        or getattr(result, "structuredContent", None)
    if structured is not None:
        return structured.get("result", structured) if isinstance(structured, dict) else structured
    items = [json.loads(c.text) for c in getattr(result, "content", result)]
    return items[0] if len(items) == 1 else items


def test_dry_run_then_write_then_undo_over_the_mcp_layer(root):
    from report_server.server import STATE

    set_project(STATE, str(root / "Synthetic.pbip"))
    before = snapshot(root)
    templates = _call("pbi_list_deneb_templates")
    assert len(templates) == 12

    preview = _call("pbi_add_deneb_visual", page_id="details", template="line",
                    bindings=CASES["line"][0], dry_run=True)
    assert preview["dry_run"] is True and snapshot(root) == before
    changes = preview["changes"]
    assert any(p.endswith("/visual.json") for p in changes["added"])
    assert any(p.endswith("report.json") for p in changes["modified"])   # registration is previewed too
    assert "deneb7E15AEF80B9E4D4F8E12924291ECE89A" in preview["diff"]

    real = _call("pbi_add_deneb_visual", page_id="details", template="line", bindings=CASES["line"][0])
    assert real["ok"] and snapshot(root) != before
    changed = _call("pbi_set_deneb_spec", page_id="details", visual_id=real["visual_id"],
                    spec=CUSTOM_SPEC)
    assert changed["ok"]
    assert _call("pbi_undo_history")[0]["tool"] == "pbi_set_deneb_spec"
    _call("pbi_undo", steps=2)
    assert snapshot(root) == before                                  # visual + registration reverted


# --- optional: compile against the real Vega-Lite ------------------------------------------------------------------------

def _rows(fields: dict, template: str) -> list[dict]:
    dims = list({f.name: f for f in fields.values() if f.vl_type != "quantitative"}.values())
    measures = [f for f in fields.values() if f.vl_type == "quantitative"]
    counts = {"histogram": [30], "box_plot": [4, 10]}.get(template, [6, 4, 3])

    def members(f, n):
        if f.vl_type == "temporal":
            return [f"2024-{m:02d}-{d:02d}" for m, d in itertools.islice(
                ((m, d) for m in range(1, 13) for d in (1, 15)), n)]
        if f.vl_type == "ordinal":
            return list(range(2020, 2020 + n))
        return [f"{f.name} {i}" for i in range(n)]

    lists = [members(f, counts[i] if i < len(counts) else 2) for i, f in enumerate(dims)]
    rows = []
    for k, combo in enumerate(itertools.product(*lists)):
        row = dict(zip((f.name for f in dims), combo))
        for j, m in enumerate(measures):
            row[m.name] = 100 + 37 * ((k * 7 + j * 3) % 11)
        rows.append(row)
    return rows


def test_specs_compile_with_the_real_vega_lite(state):
    """Compile every template + option variant with Vega-Lite 6.4.

    Runs only where ``vl-convert-python`` is installed (optional, not required).
    """
    vlc = pytest.importorskip("vl_convert")
    project = state.require()
    failures = []
    for template, bindings, options in CASE_PARAMS:
        fields = deneb.resolve_bindings(project, deneb.TEMPLATES[template], bindings)
        spec, config = deneb.build_spec(template, fields, options, deneb.project_theme(project))
        spec = copy.deepcopy(spec)
        spec["data"] = {"values": _rows(fields, template)}
        spec["width"], spec["height"] = 400, 260                     # Deneb supplies "container"
        try:
            assert vlc.vegalite_to_vega(spec, vl_version="6.4", config=config)["marks"]
        except Exception as e:  # noqa: BLE001 - report every failing variant together
            failures.append(f"{template} {options}: {str(e)[:200]}")
    assert not failures, " | ".join(failures)
