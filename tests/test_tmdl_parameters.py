"""Field parameters and what-if parameters: TMDL text + model tools."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from pathlib import Path

import pytest

from core import tmdl_parameters as tp
from core.journal import delta, snapshot
from core.pbip import PbipProject
from core.tmdl import parse_table_file
from model_server.server import ModelState, set_project
import model_server.tools_parameters as par

SYNTH = Path(__file__).parent / "fixtures" / "synthetic"

# Trimmed copies of files Power BI Desktop wrote (Microsoft "pbidevmode" sample
# and Microsoft/BCApps); the parsers must understand what Desktop emits.
DESKTOP_FIELD_PARAMETER = """\
table 'Parameter - Measure'
\tlineageTag: eee26640-bfec-44ed-b1e7-d56562bc25ed

\tcolumn 'Parameter - Measure'
\t\tdataType: string
\t\tlineageTag: f2f4b00e-fa13-46c5-9726-16717f325e26
\t\tsummarizeBy: none
\t\tisDataTypeInferred
\t\tsourceColumn: [Value1]
\t\tsortByColumn: 'Parameter - Measure Order'

\t\trelatedColumnDetails
\t\t\tgroupByColumn: 'Parameter - Measure Fields'

\t\tannotation SummarizationSetBy = Automatic

\tcolumn 'Parameter - Measure Fields'
\t\tdataType: string
\t\tisHidden
\t\tlineageTag: 4787b049-3037-4007-9719-bfeed93a7cff
\t\tsummarizeBy: none
\t\tisDataTypeInferred
\t\tsourceColumn: [Value2]
\t\tsortByColumn: 'Parameter - Measure Order'

\t\textendedProperty ParameterMetadata =
\t\t\t\t{
\t\t\t\t  "version": 3,
\t\t\t\t  "kind": 2
\t\t\t\t}

\t\tannotation SummarizationSetBy = Automatic

\tcolumn 'Parameter - Measure Order'
\t\tdataType: int64
\t\tisHidden
\t\tformatString: 0
\t\tlineageTag: 5898d688-d0af-4285-9d33-3a41d2401e4b
\t\tsummarizeBy: sum
\t\tisDataTypeInferred
\t\tsourceColumn: [Value3]

\t\tannotation SummarizationSetBy = Automatic

\tpartition 'Parameter - Measure' = calculated
\t\tmode: import
\t\tsource =
\t\t\t\t{
\t\t\t\t    ("# Sales", NAMEOF('Sales'[# Sales]), 0),
\t\t\t\t    ("Sales Amount", NAMEOF('Sales'[Sales Amount]), 1)
\t\t\t\t}

\tannotation PBI_Id = 1a6e47ba0137472192b990cc0ff130aa
"""

DESKTOP_WHATIF = """\
/// Pareto cutoff
table 'Pareto Scale'
\tlineageTag: 28e4c956-a3c4-4f11-b6c1-696539df1e2b

\tmeasure 'Pareto Value' = SELECTEDVALUE('Pareto Scale'[Value], 0.8)
\t\tformatString: 0.00%;-0.00%;0.00%
\t\tisHidden
\t\tlineageTag: f592a786-8435-46c1-94be-78a29ceec45b

\tcolumn Value
\t\tformatString: 0%;-0%;0%
\t\tlineageTag: 845c36bd-262d-48a6-820a-c939ff0d96aa
\t\tsummarizeBy: none
\t\tsourceColumn: [Value]

\t\textendedProperty ParameterMetadata =
\t\t\t\t{
\t\t\t\t  "version": 0
\t\t\t\t}

\t\tannotation SummarizationSetBy = User

\tpartition 'Pareto Scale' = calculated
\t\tmode: import
\t\tsource = GENERATESERIES(0.1, 1, 0.1)

\tannotation PBI_Id = 3916c229c3a64b31abc9a7e47195c1f7
"""

GOLDEN_FIELD_PARAMETER = """\
/// Pick a view
table 'Slice by'
\tlineageTag: <GUID>

\tcolumn 'Slice by'
\t\tdataType: string
\t\tlineageTag: <GUID>
\t\tsummarizeBy: none
\t\tisDataTypeInferred
\t\tsourceColumn: [Value1]
\t\tsortByColumn: 'Slice by Order'

\t\trelatedColumnDetails
\t\t\tgroupByColumn: 'Slice by Fields'

\t\tannotation SummarizationSetBy = Automatic

\tcolumn 'Slice by Fields'
\t\tdataType: string
\t\tisHidden
\t\tlineageTag: <GUID>
\t\tsummarizeBy: none
\t\tisDataTypeInferred
\t\tsourceColumn: [Value2]
\t\tsortByColumn: 'Slice by Order'

\t\textendedProperty ParameterMetadata =
\t\t\t\t{
\t\t\t\t  "version": 3,
\t\t\t\t  "kind": 2
\t\t\t\t}

\t\tannotation SummarizationSetBy = Automatic

\tcolumn 'Slice by Order'
\t\tdataType: int64
\t\tisHidden
\t\tformatString: 0
\t\tlineageTag: <GUID>
\t\tsummarizeBy: sum
\t\tisDataTypeInferred
\t\tsourceColumn: [Value3]

\t\tannotation SummarizationSetBy = Automatic

\tpartition 'Slice by' = calculated
\t\tmode: import
\t\tsource =
\t\t\t\t{
\t\t\t\t    ("Amount", NAMEOF('Sales'[Amount]), 0),
\t\t\t\t    ("Net Revenue", NAMEOF('Sales'[Net Revenue]), 1),
\t\t\t\t    ("Year", NAMEOF('Date'[Year]), 2)
\t\t\t\t}

\tannotation PBI_Id = <ID>
"""

GOLDEN_WHATIF = """\
table Discount
\tlineageTag: <GUID>

\tmeasure 'Discount Value' = SELECTEDVALUE('Discount'[Discount], 0.1)
\t\tformatString: 0.00
\t\tlineageTag: <GUID>

\tcolumn Discount
\t\tdataType: double
\t\tformatString: 0.00
\t\tlineageTag: <GUID>
\t\tsummarizeBy: none
\t\tisDataTypeInferred
\t\tsourceColumn: [Value]

\t\textendedProperty ParameterMetadata =
\t\t\t\t{
\t\t\t\t  "version": 0
\t\t\t\t}

\t\tannotation SummarizationSetBy = User

\tpartition Discount = calculated
\t\tmode: import
\t\tsource = GENERATESERIES(0, 0.5, 0.05)

\tannotation PBI_Id = <ID>
"""


def normalise(text: str) -> str:
    text = re.sub(r"lineageTag: [0-9a-f-]{36}", "lineageTag: <GUID>", text)
    return re.sub(r"PBI_Id = [0-9a-f]{32}", "PBI_Id = <ID>", text)


@pytest.fixture
def proj(tmp_path) -> Path:
    dst = tmp_path / "synthetic"
    shutil.copytree(SYNTH, dst)
    return dst


@pytest.fixture
def state(proj) -> ModelState:
    st = ModelState()
    set_project(st, str(proj / "Synthetic.pbip"))
    return st


def defn(proj: Path) -> Path:
    return proj / "Synthetic.SemanticModel" / "definition"


def only_table_and_model(before: dict, proj: Path, table_file: str) -> None:
    d = delta(before, snapshot(proj))
    assert d["deleted"] == []
    assert d["added"] == [f"Synthetic.SemanticModel/definition/tables/{table_file}"]
    assert d["modified"] == ["Synthetic.SemanticModel/definition/model.tmdl"]


# --- literal helpers ----------------------------------------------------------

def test_dax_builders_escape_quotes_and_brackets():
    assert tp.dax_string('say "hi"') == '"say ""hi"""'
    assert tp.dax_column_ref("O'Brien", "a]b") == "'O''Brien'[a]]b]"
    assert tp.format_number(1.0) == "1" and tp.format_number(0.05) == "0.05"
    assert tp.format_number(1e-05) == "0.00001" and tp.format_number(3) == "3"
    for bad in (True, "1", float("nan"), float("inf"), None):
        with pytest.raises(ValueError):
            tp.format_number(bad)
    assert tp.default_format_string(0, 10, 1) == "0"
    assert tp.default_format_string(0, 1, 0.25) == "0.00"
    assert tp.default_format_string(0, 1, 0.001) == "0.000"
    assert tp.series_length(0, 0.5, 0.05) == 11 and tp.series_length(1, 10, 1) == 10


def test_field_tuples_round_trip_special_names():
    fields = [{"label": 'He said "x"', "table": "O'Brien", "name": "a]b"},
              {"label": "Plain", "table": "Sales", "name": "Net Revenue"}]
    text = tp.emit_field_parameter_table("P", fields)
    got = tp.parse_field_tuples(tp.partition_source(text))
    assert [(g["label"], g["table"], g["name"], g["order"]) for g in got] == [
        ('He said "x"', "O'Brien", "a]b", 0), ("Plain", "Sales", "Net Revenue", 1)]


# --- field parameters ---------------------------------------------------------

def test_create_field_parameter_matches_desktop_shape(state, proj):
    before = snapshot(proj)
    orig_model = (defn(proj) / "model.tmdl").read_text(encoding="utf-8")
    res = par.create_field_parameter(
        state, "Slice by", ["Sales.Amount", "Sales.[Net Revenue]", "Date.Year"],
        description="Pick a view")
    assert res["ok"] and res["file"] == "definition/tables/Slice by.tmdl"
    assert res["fields"] == [
        {"label": "Amount", "field": "Sales.Amount", "kind": "column", "order": 0},
        {"label": "Net Revenue", "field": "Sales.Net Revenue", "kind": "measure", "order": 1},
        {"label": "Year", "field": "Date.Year", "kind": "column", "order": 2}]
    text = (defn(proj) / "tables" / "Slice by.tmdl").read_text(encoding="utf-8")
    assert normalise(text) == GOLDEN_FIELD_PARAMETER
    model = (defn(proj) / "model.tmdl").read_text(encoding="utf-8")
    assert model == orig_model.replace("\tref table Date\n",
                                       "\tref table Date\n\tref table 'Slice by'\n")
    only_table_and_model(before, proj, "Slice by.tmdl")


def test_field_parameter_is_a_normal_table_for_the_reader(state, proj):
    par.create_field_parameter(state, "Slice by", ["Sales.Amount", "Sales.[Net Revenue]"])
    table = parse_table_file(defn(proj) / "tables" / "Slice by.tmdl")
    assert table.name == "Slice by" and not table.is_calc_group and table.measures == []
    cols = {c.name: c for c in table.columns}
    assert list(cols) == ["Slice by", "Slice by Fields", "Slice by Order"]
    assert not cols["Slice by"].is_hidden
    assert cols["Slice by Fields"].is_hidden and cols["Slice by Order"].is_hidden
    assert cols["Slice by Order"].data_type == "int64"
    fresh = PbipProject(proj / "Synthetic.pbip")
    assert "Slice by" in [t.name for t in fresh.list_tables()]
    fresh.model_lineage()          # other readers still cope with the new table
    fresh.get_model()


def test_field_parameter_order_label_and_reference_forms(state, proj):
    res = par.create_field_parameter(
        state, "View",
        ["'Date'.Year", {"field": "Sales[Cost]", "label": "Cost of sales"},
         "sales.[margin %]", {"field": "Sales.Order Count"}],
        default_index=2)
    assert [(f["label"], f["field"], f["kind"]) for f in res["fields"]] == [
        ("Margin %", "Sales.Margin %", "measure"),
        ("Year", "Date.Year", "column"),
        ("Cost of sales", "Sales.Cost", "column"),
        ("Order Count", "Sales.Order Count", "column")]
    info = par.list_field_parameters(state)[0]
    assert [f["order"] for f in info["fields"]] == [0, 1, 2, 3]
    assert info["fields"][0]["name"] == "Margin %"


def test_field_parameter_with_unicode_and_quotes_in_labels(state, proj):
    par.create_field_parameter(state, "Größe", [{"field": "Sales.Amount", "label": 'Betrag "netto"'}])
    raw = (defn(proj) / "tables" / "Größe.tmdl").read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8")
    assert '("Betrag ""netto""", NAMEOF(\'Sales\'[Amount]), 0)' in text
    (info,) = par.list_field_parameters(state)
    assert info["table"] == "Größe" and info["fields"][0]["label"] == 'Betrag "netto"'
    assert parse_table_file(defn(proj) / "tables" / "Größe.tmdl").name == "Größe"


def test_field_parameter_rejections_leave_project_untouched(state, proj):
    before = snapshot(proj)
    bad = [
        (dict(name="P", fields=[]), "non-empty list"),
        (dict(name="P", fields="Sales.Amount"), "non-empty list"),
        (dict(name="P", fields=["Ghost.Amount"]), "no table in the model matches"),
        (dict(name="P", fields=["Amount"]), "no table in the model matches"),
        (dict(name="P", fields=["Sales.Ghost"]), "neither a column nor a measure"),
        (dict(name="P", fields=["Sales.[Amount]"]), "is a column; write 'Sales.Amount'"),
        (dict(name="P", fields=["Sales.[Ghost]"]), "measure 'Ghost' not found"),
        (dict(name="P", fields=["Sales.Amount", "sales.amount"]), "more than once"),
        (dict(name="P", fields=["Sales.Amount", {"field": "Sales.Cost", "label": "amount"}]),
         "used twice"),
        (dict(name="P", fields=[{"field": "Sales.Amount", "lable": "x"}]), "field object"),
        (dict(name="P", fields=[{"field": "Sales.Amount", "label": " "}]), "Label"),
        (dict(name="P", fields=[5]), "non-empty string"),
        (dict(name="P", fields=["Sales.Amount"], default_index=1), "default_index"),
        (dict(name="P", fields=["Sales.Amount"], default_index=-1), "default_index"),
        (dict(name="sales", fields=["Sales.Amount"]), "already exists"),
        (dict(name="", fields=["Sales.Amount"]), "non-empty"),
    ]
    for kwargs, msg in bad:
        with pytest.raises(ValueError, match=msg):
            par.create_field_parameter(state, **kwargs)
    assert snapshot(proj) == before


def test_list_field_parameters_flags_missing_fields(state, proj):
    assert par.list_field_parameters(state) == []
    par.create_field_parameter(state, "P", ["Sales.[Hidden Helper]", "Sales.Amount"],
                               description="d")
    (info,) = par.list_field_parameters(state)
    assert info["table"] == "P" and info["description"] == "d"
    assert (info["label_column"], info["fields_column"], info["order_column"]) == \
        ("P", "P Fields", "P Order")
    assert [f["exists"] for f in info["fields"]] == [True, True]
    state.require().delete_measure("Sales", "Hidden Helper")
    (info,) = par.list_field_parameters(state)
    assert [(f["name"], f["exists"], f["kind"]) for f in info["fields"]] == [
        ("Hidden Helper", False, None), ("Amount", True, "column")]


def test_parse_desktop_field_parameter_and_table(tmp_path, proj):
    info = tp.parse_field_parameter(DESKTOP_FIELD_PARAMETER)
    assert info["table"] == "Parameter - Measure"
    assert info["label_column"] == "Parameter - Measure"
    assert info["fields_column"] == "Parameter - Measure Fields"
    assert info["order_column"] == "Parameter - Measure Order"
    assert [(f["label"], f["table"], f["name"], f["order"]) for f in info["fields"]] == [
        ("# Sales", "Sales", "# Sales", 0), ("Sales Amount", "Sales", "Sales Amount", 1)]
    assert tp.parse_whatif_parameter(DESKTOP_FIELD_PARAMETER, []) is None
    f = tmp_path / "Parameter - Measure.tmdl"
    f.write_text(DESKTOP_FIELD_PARAMETER, encoding="utf-8")
    assert [c.name for c in parse_table_file(f).columns] == [
        "Parameter - Measure", "Parameter - Measure Fields", "Parameter - Measure Order"]


def test_ordinary_tables_are_not_parameters(state):
    assert par.list_field_parameters(state) == []
    assert par.list_whatif_parameters(state) == []
    assert tp.parse_field_parameter("table T\n\tcolumn A\n\t\tdataType: string\n") is None


# --- what-if parameters ---------------------------------------------------------

def test_create_whatif_parameter_matches_desktop_shape(state, proj):
    before = snapshot(proj)
    res = par.create_whatif_parameter(state, "Discount", 0, 0.5, 0.05, default=0.1)
    assert res["ok"] and res["measure"] == "Discount Value" and res["rows"] == 11
    assert res["format_string"] == "0.00" and res["default"] == 0.1
    text = (defn(proj) / "tables" / "Discount.tmdl").read_text(encoding="utf-8")
    assert normalise(text) == GOLDEN_WHATIF
    only_table_and_model(before, proj, "Discount.tmdl")
    assert "\tref table Discount\n" in (defn(proj) / "model.tmdl").read_text(encoding="utf-8")


def test_whatif_table_is_readable_and_measure_is_a_real_measure(state, proj):
    par.create_whatif_parameter(state, "Discount", 0, 50, 5, description="Scenario")
    fresh = PbipProject(proj / "Synthetic.pbip")
    table = {t.name: t for t in fresh.list_tables()}["Discount"]
    assert [c.name for c in table.columns] == ["Discount"]
    assert table.columns[0].data_type == "int64"          # whole-number series
    (m,) = table.measures
    assert (m.name, m.dax, m.format_string) == \
        ("Discount Value", "SELECTEDVALUE('Discount'[Discount], 0)", "0")
    assert "Discount Value" in [x.name for x in fresh.list_measures()]
    fresh.create_measure("Sales", "Discounted", "[Net Revenue] * (1 - [Discount Value] / 100)")
    assert "Discount Value" in fresh.model_lineage("Discounted")["depends_on_measures"]


def test_whatif_defaults_and_custom_format(state, proj):
    res = par.create_whatif_parameter(state, "Rate", 0.001, 0.005, 0.001, format_string="0.0%")
    assert res["default"] == 0.001 and res["format_string"] == "0.0%"
    text = (defn(proj) / "tables" / "Rate.tmdl").read_text(encoding="utf-8")
    assert "SELECTEDVALUE('Rate'[Rate], 0.001)" in text
    assert "dataType: double" in text and "GENERATESERIES(0.001, 0.005, 0.001)" in text
    res = par.create_whatif_parameter(state, "Growth", 0.1, 1, 0.1)
    assert res["format_string"] == "0.0"


def test_whatif_rejections_leave_project_untouched(state, proj):
    state.require().create_measure("Sales", "Taken Value", "1")
    before = snapshot(proj)
    bad = [
        (dict(name="W", minimum=0, maximum=10, increment=0), "greater than 0"),
        (dict(name="W", minimum=0, maximum=10, increment=-1), "greater than 0"),
        (dict(name="W", minimum=10, maximum=10, increment=1), "less than maximum"),
        (dict(name="W", minimum=10, maximum=0, increment=1), "less than maximum"),
        (dict(name="W", minimum=0, maximum=10, increment=1, default=11), "outside the range"),
        (dict(name="W", minimum=0, maximum=10, increment=1, default=-1), "outside the range"),
        (dict(name="W", minimum=0, maximum=10 ** 9, increment=1), "rows"),
        (dict(name="W", minimum=True, maximum=10, increment=1), "minimum"),
        (dict(name="W", minimum="0", maximum=10, increment=1), "minimum"),
        (dict(name="W", minimum=0, maximum=float("inf"), increment=1), "maximum"),
        (dict(name="Sales", minimum=0, maximum=1, increment=1), "already exists"),
        (dict(name="Taken", minimum=0, maximum=1, increment=1), "Taken Value"),
        (dict(name="W", minimum=0, maximum=1, increment=1, format_string="0\n0"), "single line"),
    ]
    for kwargs, msg in bad:
        with pytest.raises(ValueError, match=msg):
            par.create_whatif_parameter(state, **kwargs)
    assert snapshot(proj) == before


def test_list_whatif_parameters(state):
    par.create_whatif_parameter(state, "Discount", 0, 0.5, 0.05, 0.1, description="d")
    par.create_field_parameter(state, "P", ["Sales.Amount"])       # not a what-if
    (info,) = par.list_whatif_parameters(state)
    assert info["table"] == "Discount" and info["column"] == "Discount"
    assert (info["minimum"], info["maximum"], info["increment"]) == (0, 0.5, 0.05)
    assert info["measure"] == "Discount Value" and info["default"] == 0.1
    assert info["format_string"] == "0.00" and info["description"] == "d"
    assert info["source"] == "GENERATESERIES(0, 0.5, 0.05)"


def test_parse_desktop_whatif():
    from core.schemas import Measure
    ms = [Measure(table="Pareto Scale", name="Pareto Value",
                  dax="SELECTEDVALUE('Pareto Scale'[Value], 0.8)")]
    info = tp.parse_whatif_parameter(DESKTOP_WHATIF, ms)
    assert info["table"] == "Pareto Scale" and info["description"] == "Pareto cutoff"
    assert (info["column"], info["minimum"], info["maximum"], info["increment"]) == \
        ("Value", 0.1, 1, 0.1)
    assert (info["measure"], info["default"], info["format_string"]) == \
        ("Pareto Value", 0.8, "0%;-0%;0%")
    assert tp.parse_field_parameter(DESKTOP_WHATIF) is None


def test_two_parameters_register_in_order(state, proj):
    par.create_whatif_parameter(state, "A", 0, 1, 1)
    par.create_field_parameter(state, "B", ["Sales.Amount"])
    refs = [ln.strip() for ln in (defn(proj) / "model.tmdl").read_text(
        encoding="utf-8").split("\n") if "ref " in ln]
    assert refs == ["ref table Sales", "ref table Date", "ref table A", "ref table B"]


def test_parameters_coexist_with_security_objects(state, proj):
    import model_server.tools_security as sec
    sec.create_role(state, "R", table_filters={"Sales": "[Amount] > 0"})
    par.create_whatif_parameter(state, "A", 0, 1, 1)
    refs = [ln.strip() for ln in (defn(proj) / "model.tmdl").read_text(
        encoding="utf-8").split("\n") if "ref " in ln]
    assert refs == ["ref table Sales", "ref table Date", "ref table A", "ref role R"]


def test_tools_need_a_project():
    with pytest.raises(ValueError, match="pbi_set_project"):
        par.list_field_parameters(ModelState())


# --- MCP layer ----------------------------------------------------------------

def _payload(result):
    structured = None
    if isinstance(result, tuple):
        result, structured = result
    structured = (structured or getattr(result, "structured_content", None)
                  or getattr(result, "structuredContent", None))
    if structured is not None:
        return structured.get("result", structured) if isinstance(structured, dict) else structured
    content = getattr(result, "content", result)
    items = [json.loads(c.text) for c in content]
    return items[0] if len(items) == 1 else items


def _hint(tool, name: str):
    a = tool.annotations
    snake = {"readOnlyHint": "read_only_hint", "destructiveHint": "destructive_hint"}[name]
    return getattr(a, name) if hasattr(a, name) else getattr(a, snake)


def test_tools_registered_with_annotations():
    import model_server.server as srv
    import report_server.server as rsrv
    tools = {t.name: t for t in asyncio.run(srv.mcp.list_tools())}
    report_names = {t.name for t in asyncio.run(rsrv.mcp.list_tools())}
    for name, is_read in {"pbi_create_field_parameter": False, "pbi_list_field_parameters": True,
                          "pbi_create_whatif_parameter": False,
                          "pbi_list_whatif_parameters": True}.items():
        t = tools[name]
        assert name not in report_names and len(t.description) > 60
        props = (getattr(t, "inputSchema", None) or t.input_schema).get("properties", {})
        assert _hint(t, "readOnlyHint") is is_read
        assert ("dry_run" in props) is (not is_read)
        assert _hint(t, "destructiveHint") is False


def test_end_to_end_dry_run_write_and_undo(proj):
    import model_server.server as srv

    def call(tool_name, **args):
        return _payload(asyncio.run(srv.mcp.call_tool(tool_name, args)))

    call("pbi_set_project", path=str(proj / "Synthetic.pbip"))
    before = snapshot(proj)

    preview = call("pbi_create_whatif_parameter", name="Discount", minimum=0,
                   maximum=0.5, increment=0.05, dry_run=True)
    assert preview["dry_run"] is True and "GENERATESERIES(0, 0.5, 0.05)" in preview["diff"]
    assert snapshot(proj) == before

    real = call("pbi_create_whatif_parameter", name="Discount", minimum=0,
                maximum=0.5, increment=0.05, default=0.1)
    assert real["measure"] == "Discount Value"
    assert call("pbi_list_whatif_parameters")[0]["default"] == 0.1

    fp = call("pbi_create_field_parameter", name="Slice by",
              fields=["Sales.Amount", {"field": "Sales.[Net Revenue]", "label": "Revenue"}])
    assert [f["label"] for f in fp["fields"]] == ["Amount", "Revenue"]
    assert [p["table"] for p in call("pbi_list_field_parameters")] == ["Slice by"]

    call("pbi_undo", steps=2)
    assert snapshot(proj) == before
