"""Tables, partitions, shared expressions, incremental refresh, delete-table.

Every test works on a throwaway copy of the synthetic fixture and re-parses
from disk (core.tmdl.parse_table_file / PbipProject) to prove the emitted or
edited TMDL is what the rest of the stack reads back.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from core import tmdl_tables as tt
from core.pbip import PbipProject
from core.tmdl import parse_table_file
from model_server import tools_tables as tools
from model_server.server import ModelState, set_project

SYNTH = Path(__file__).parent / "fixtures" / "synthetic"

M_SOURCE = (
    "let\n"
    "    Source = Csv.Document(File.Contents(\"orders.csv\")),\n"
    "    Typed = Table.TransformColumnTypes(Source, {{\"Amount\", type number}})\n"
    "in\n"
    "    Typed"
)
M_INCREMENTAL = (
    "let\n"
    "    Source = Csv.Document(File.Contents(\"sales.csv\")),\n"
    "    Filtered = Table.SelectRows(Source, each [OrderDate] >= RangeStart "
    "and [OrderDate] < RangeEnd)\n"
    "in\n"
    "    Filtered"
)
COLUMNS = [
    {"name": "Order ID", "data_type": "int64"},
    {"name": "Amount", "data_type": "double", "summarize_by": "sum",
     "format_string": "#,0.00"},
    {"name": "Country", "data_type": "string", "data_category": "Country",
     "is_hidden": True, "source_column": "CountryName"},
]

# the fixture's Sales partition source, exactly as written on disk
SALES_SOURCE_ON_DISK = (
    "\t\t\t\tlet\n"
    "\t\t\t\t\tSource = Csv.Document(File.Contents(\"sales.csv\"))\n"
    "\t\t\t\tin\n"
    "\t\t\t\t\tSource"
)


@pytest.fixture
def state(tmp_path) -> ModelState:
    dst = tmp_path / "synthetic"
    shutil.copytree(SYNTH, dst)
    st = ModelState()
    set_project(st, str(dst / "Synthetic.pbip"))
    return st


def _defn(state: ModelState) -> Path:
    return state.project._require_model() / "definition"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def _reload(state: ModelState) -> PbipProject:
    return PbipProject(state.project.path)


# --- create table -------------------------------------------------------------

def test_create_table_parses_back_with_columns_and_partition(state):
    res = tools.create_table(state, "Orders", M_SOURCE, COLUMNS,
                             description="Order lines\nfrom the CSV extract")
    assert res["action"] == "created"
    assert res["columns"] == ["Order ID", "Amount", "Country"]
    assert res["partition"] == {"name": "Orders", "kind": "m", "mode": "import"}

    path = _defn(state) / "tables" / "Orders.tmdl"
    table = parse_table_file(path)
    assert table.name == "Orders" and not table.is_hidden
    cols = {c.name: c for c in table.columns}
    assert cols["Order ID"].data_type == "int64"
    assert cols["Order ID"].summarize_by == "none"
    assert cols["Amount"].summarize_by == "sum"
    assert cols["Country"].data_category == "Country"
    assert cols["Country"].is_hidden is True

    text = _read(path)
    assert text.startswith("/// Order lines\n/// from the CSV extract\ntable Orders\n\tlineageTag: ")
    assert "\t\tformatString: #,0.00\n" in text
    assert "\t\tsourceColumn: CountryName\n" in text
    assert "\tpartition Orders = m\n\t\tmode: import\n\t\tsource =\n\t\t\t\tlet\n" in text
    assert "\t\t\t\t    Typed = Table.TransformColumnTypes" in text   # M indent kept
    assert "\n\tannotation PBI_ResultType = Table\n" in text
    assert text.count("lineageTag: ") == 1 + len(COLUMNS)

    parts = tt.parse_partitions_text(text)
    assert parts == [{"name": "Orders", "kind": "m", "mode": "import",
                      "source": M_SOURCE, "source_form": "block",
                      "properties": {"mode": "import"}}]
    # visible to the model layer + registered
    assert "Orders" in {t.name for t in _reload(state).list_tables()}
    assert "\tref table Orders\n" in _read(_defn(state) / "model.tmdl")
    assert "\tref table Sales\n" in _read(_defn(state) / "model.tmdl")


def test_create_hidden_table_quoted_name_no_columns(state):
    tools.create_table(state, "Staging Rows", "Table.FromRows({})", hidden=True)
    path = _defn(state) / "tables" / "Staging Rows.tmdl"
    table = parse_table_file(path)
    assert table.is_hidden is True and table.columns == []
    text = _read(path)
    assert "table 'Staging Rows'\n\tisHidden\n" in text
    assert "\tpartition 'Staging Rows' = m\n\t\tmode: import\n\t\tsource = Table.FromRows({})\n" in text
    assert "ref table 'Staging Rows'" in _read(_defn(state) / "model.tmdl")


def test_create_table_duplicate_rejected_without_writing(state):
    before = _read(_defn(state) / "model.tmdl")
    with pytest.raises(ValueError, match="already exists"):
        tools.create_table(state, "Sales", M_SOURCE)
    with pytest.raises(ValueError, match="already exists"):
        tools.create_table(state, "sales", M_SOURCE)      # names are case-insensitive
    assert _read(_defn(state) / "model.tmdl") == before
    assert not list((_defn(state) / "tables").glob("*.bak-*"))


@pytest.mark.parametrize("columns, msg", [
    ([{"name": "A", "data_type": "text"}], "data_type"),
    ([{"data_type": "string"}], "name"),
    ([{"name": "A", "data_type": "string", "summarize_by": "total"}], "summarize_by"),
    ([{"name": "A", "data_type": "string"}, {"name": "a", "data_type": "int64"}],
     "Duplicate column"),
])
def test_create_table_bad_columns_rejected(state, columns, msg):
    with pytest.raises(ValueError, match=msg):
        tools.create_table(state, "Bad", M_SOURCE, columns)
    assert not (_defn(state) / "tables" / "Bad.tmdl").exists()


def test_create_table_empty_source_rejected(state):
    with pytest.raises(ValueError, match="m_source"):
        tools.create_table(state, "Bad", "   ")


# --- calculated table -----------------------------------------------------------

def test_create_calculated_table(state):
    res = tools.create_calculated_table(
        state, "Years", "CALENDAR(DATE(2020,1,1), DATE(2025,12,31))",
        description="One row per day")
    assert res["partition"] == {"name": "Years", "kind": "calculated", "mode": "import"}
    assert "infers" in res["note"]
    path = _defn(state) / "tables" / "Years.tmdl"
    text = _read(path)
    assert text.startswith("/// One row per day\ntable Years\n\tlineageTag: ")
    assert ("\tpartition Years = calculated\n\t\tmode: import\n"
            "\t\tsource = CALENDAR(DATE(2020,1,1), DATE(2025,12,31))\n") in text
    assert "annotation PBI_Id = " in text
    assert parse_table_file(path).name == "Years"
    assert "ref table Years" in _read(_defn(state) / "model.tmdl")
    listed = tools.list_partitions(state, "Years")
    assert listed[0]["partitions"][0]["kind"] == "calculated"
    assert listed[0]["partitions"][0]["source"] == "CALENDAR(DATE(2020,1,1), DATE(2025,12,31))"


def test_create_calculated_table_multiline_dax_block(state):
    dax = "VAR _d = CALENDAR(DATE(2020,1,1), DATE(2020,12,31))\nRETURN\n    ADDCOLUMNS(_d, \"Year\", YEAR([Date]))"
    tools.create_calculated_table(state, "Cal", dax)
    text = _read(_defn(state) / "tables" / "Cal.tmdl")
    assert ("\t\tsource =\n\t\t\t\tVAR _d = CALENDAR(DATE(2020,1,1), DATE(2020,12,31))\n"
            "\t\t\t\tRETURN\n\t\t\t\t    ADDCOLUMNS(_d, \"Year\", YEAR([Date]))\n") in text
    assert tools.list_partitions(state, "Cal")[0]["partitions"][0]["source"] == dax


def test_create_calculated_table_duplicate_rejected(state):
    with pytest.raises(ValueError, match="already exists"):
        tools.create_calculated_table(state, "Date", "CALENDAR(1,2)")


# --- partitions -----------------------------------------------------------------

def test_list_partitions_fixture(state):
    listing = {t["table"]: t["partitions"] for t in tools.list_partitions(state)}
    assert set(listing) == {"Date", "Sales"}
    sales, = listing["Sales"]
    assert (sales["name"], sales["kind"], sales["mode"]) == ("Sales", "m", "import")
    assert "Csv.Document" in sales["source"] and sales["source"].startswith("let")
    date, = listing["Date"]
    assert (date["kind"], date["source"]) == (
        "calculated", "CALENDAR(DATE(2020,1,1), DATE(2025,12,31))")
    with pytest.raises(KeyError):
        tools.list_partitions(state, "Nope")


def test_update_partition_preserves_every_other_byte(state):
    path = _defn(state) / "tables" / "Sales.tmdl"
    before = path.read_bytes()
    assert SALES_SOURCE_ON_DISK.encode() in before

    new_m = "let\n    Source = Sql.Database(\"srv\", \"db\")\nin\n    Source"
    res = tools.update_partition(state, "Sales", new_m)
    assert res == {"ok": True, "action": "updated", "table": "Sales",
                   "partition": "Sales", "kind": "m", "mode": "import"}

    rendered = ("\t\t\t\tlet\n\t\t\t\t    Source = Sql.Database(\"srv\", \"db\")\n"
                "\t\t\t\tin\n\t\t\t\t    Source")
    assert path.read_bytes() == before.replace(SALES_SOURCE_ON_DISK.encode(), rendered.encode())
    assert tools.list_partitions(state, "Sales")[0]["partitions"][0]["source"] == new_m
    # the rest of the model still reads identically
    t = parse_table_file(path)
    assert [m.name for m in t.measures] == ["Net Revenue", "Margin %", "Complex Measure",
                                            "Fenced Measure", "Hidden Helper"]
    assert len(t.columns) == 4
    # idempotent: same source again -> no change, no write
    stamp = path.stat().st_mtime_ns
    assert tools.update_partition(state, "Sales", new_m)["action"] == "unchanged"
    assert path.stat().st_mtime_ns == stamp


def test_update_partition_inline_and_calculated(state):
    tools.update_partition(state, "Sales", "Table.FromRows({})")
    text = _read(_defn(state) / "tables" / "Sales.tmdl")
    assert "\t\tmode: import\n\t\tsource = Table.FromRows({})\n" in text
    tools.update_partition(state, "Date", "CALENDARAUTO()")
    assert tools.list_partitions(state, "Date")[0]["partitions"][0]["source"] == "CALENDARAUTO()"
    assert parse_table_file(_defn(state) / "tables" / "Date.tmdl").columns[0].name == "Date"


def test_update_partition_keeps_crlf_and_bom(state):
    path = _defn(state) / "tables" / "Sales.tmdl"
    path.write_bytes(b"\xef\xbb\xbf" + _read(path).replace("\n", "\r\n").encode("utf-8"))
    tools.update_partition(state, "Sales", "Table.FromRows({})")
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" in raw and raw.count(b"\n") == raw.count(b"\r\n")
    assert b"source = Table.FromRows({})\r\n" in raw


def test_update_partition_multiple_partitions_need_a_name(state):
    path = _defn(state) / "tables" / "Sales.tmdl"
    text = _read(path).rstrip("\n") + (
        "\n\n\tpartition 'Sales 2024' = m\n\t\tmode: import\n\t\tsource =\n"
        "\t\t\t\tlet\n\t\t\t\t    Source = 2024\n\t\t\t\tin\n\t\t\t\t    Source\n")
    path.write_text(text, encoding="utf-8")
    names = [p["name"] for p in tools.list_partitions(state, "Sales")[0]["partitions"]]
    assert names == ["Sales", "Sales 2024"]
    with pytest.raises(ValueError, match="pass partition="):
        tools.update_partition(state, "Sales", "1")
    with pytest.raises(KeyError, match="Sales 2023"):
        tools.update_partition(state, "Sales", "1", partition="Sales 2023")
    tools.update_partition(state, "Sales", "let\n    Source = 2025\nin\n    Source",
                           partition="Sales 2024")
    parts = {p["name"]: p["source"] for p in tools.list_partitions(state, "Sales")[0]["partitions"]}
    assert parts["Sales 2024"] == "let\n    Source = 2025\nin\n    Source"
    assert parts["Sales"].startswith("let\n") and "sales.csv" in parts["Sales"]


def test_update_partition_unknown_table(state):
    with pytest.raises(KeyError):
        tools.update_partition(state, "Nope", "1")


# --- shared expressions ---------------------------------------------------------

def test_list_expressions_fixture(state):
    listed = tools.list_expressions(state)
    assert len(listed) == 1
    e = listed[0]
    assert (e["name"], e["kind"], e["m"]) == ("ServerParam", "parameter", '"prod-server"')
    assert e["meta"] == {"IsParameterQuery": True, "Type": "Text"}


def test_create_parameter_desktop_shape(state):
    res = tools.create_expression(state, "Environment", '"dev"', kind="parameter",
                                  description="Target environment")
    assert res["kind"] == "parameter" and res["result_type"] == "Text"
    assert res["meta"] == {"IsParameterQuery": True, "Type": "Text",
                           "IsParameterQueryRequired": True}
    text = _read(_defn(state) / "expressions.tmdl")
    assert text.startswith('expression ServerParam = "prod-server" meta [IsParameterQuery=true, Type="Text"]\n')
    block = text[text.index("/// Target environment"):]
    lines = block.split("\n")
    assert lines[0] == "/// Target environment"
    assert lines[1] == ('expression Environment = "dev" meta [IsParameterQuery=true, '
                        'Type="Text", IsParameterQueryRequired=true]')
    assert lines[2].startswith("\tlineageTag: ") and len(lines[2]) == len("\tlineageTag: ") + 36
    assert lines[3:8] == ["", "\tannotation PBI_NavigationStepName = Navigation", "",
                          "\tannotation PBI_ResultType = Text", ""]
    # no `ref expression` is ever written to model.tmdl
    assert "ref expression" not in _read(_defn(state) / "model.tmdl")


@pytest.mark.parametrize("value, ptype", [
    ("#datetime(2024, 1, 1, 0, 0, 0)", "DateTime"),
    ("#date(2024, 1, 1)", "Date"),
    ("42", "Number"),
    ("true", "Logical"),
])
def test_parameter_type_inferred_and_overridable(state, value, ptype):
    res = tools.create_expression(state, f"P{ptype}", value, kind="parameter")
    assert res["meta"]["Type"] == ptype and res["result_type"] == ptype
    res2 = tools.create_expression(state, f"Q{ptype}", value, kind="parameter",
                                   parameter_meta={"Type": "Any", "IsParameterQueryRequired": False})
    assert res2["meta"] == {"IsParameterQuery": True, "Type": "Any",
                            "IsParameterQueryRequired": False}
    text = _read(_defn(state) / "expressions.tmdl")
    assert f'meta [IsParameterQuery=true, Type="Any", IsParameterQueryRequired=false]' in text


def test_create_query_expression_multiline(state):
    m = "let\n    Source = Sql.Database(\"srv\", \"db\")\nin\n    Source"
    res = tools.create_expression(state, "Shared Source", m)
    assert res["kind"] == "query" and res["result_type"] == "Table" and res["meta"] is None
    text = _read(_defn(state) / "expressions.tmdl")
    assert ("\nexpression 'Shared Source' =\n\t\tlet\n\t\t    Source = Sql.Database(\"srv\", \"db\")\n"
            "\t\tin\n\t\t    Source\n\tlineageTag: ") in text
    assert "\tannotation PBI_ResultType = Table\n" in text
    listed = {e["name"]: e for e in tools.list_expressions(state)}
    assert listed["Shared Source"]["m"] == m
    assert listed["ServerParam"]["m"] == '"prod-server"'      # untouched
    fn = tools.create_expression(state, "AddOne", "(x as number) => x + 1")
    assert fn["result_type"] == "Function"


def test_create_expression_rejections(state):
    with pytest.raises(ValueError, match="already exists"):
        tools.create_expression(state, "serverparam", '"x"', kind="parameter")
    with pytest.raises(ValueError, match="unique across tables"):
        tools.create_expression(state, "Sales", "let a = 1 in a")
    with pytest.raises(ValueError, match="single-line"):
        tools.create_expression(state, "P", "1\n+ 2", kind="parameter")
    with pytest.raises(ValueError, match="kind"):
        tools.create_expression(state, "P", "1", kind="function")
    with pytest.raises(ValueError, match="parameter_meta"):
        tools.create_expression(state, "P", "1", parameter_meta={"Type": "Text"})


def test_update_expression_keeps_meta_and_annotations(state):
    tools.create_expression(state, "Shared Source", "let\n    a = 1\nin\n    a")
    before = _read(_defn(state) / "expressions.tmdl")

    res = tools.update_expression(state, "ServerParam", '"dev-server"')
    assert res["action"] == "updated" and res["meta"] == {"IsParameterQuery": True, "Type": "Text"}
    after = _read(_defn(state) / "expressions.tmdl")
    assert after == before.replace(
        'expression ServerParam = "prod-server" meta [IsParameterQuery=true, Type="Text"]',
        'expression ServerParam = "dev-server" meta [IsParameterQuery=true, Type="Text"]')

    tools.update_expression(state, "Shared Source", "let\n    a = 2\nin\n    a")
    listed = {e["name"]: e for e in tools.list_expressions(state)}
    assert listed["Shared Source"]["m"] == "let\n    a = 2\nin\n    a"
    assert listed["Shared Source"]["result_type"] == "Table"
    assert listed["Shared Source"]["lineage_tag"] is not None
    assert tools.update_expression(state, "Shared Source", "let\n    a = 2\nin\n    a")["action"] == "unchanged"
    with pytest.raises(KeyError):
        tools.update_expression(state, "Nope", "1")
    with pytest.raises(ValueError, match="single-line"):
        tools.update_expression(state, "ServerParam", "1\n+1")


# --- incremental refresh ------------------------------------------------------------

def test_set_refresh_policy_block_and_parameters(state):
    path = _defn(state) / "tables" / "Sales.tmdl"
    original = _read(path)
    res = tools.set_refresh_policy(state, "Sales", "year", 3, "month", 12,
                                   source_expression=M_INCREMENTAL)
    assert res["action"] == "created"
    assert res["parameters_added"] == ["RangeStart", "RangeEnd"]
    assert res["policy"] == {
        "policyType": "basic", "rollingWindowGranularity": "year",
        "rollingWindowPeriods": 3, "incrementalGranularity": "month",
        "incrementalPeriods": 12, "sourceExpression": M_INCREMENTAL}
    assert "warnings" in res        # the fixture's own query has no RangeStart filter

    text = _read(path)
    expected_block = (
        "\trefreshPolicy\n"
        "\t\tpolicyType: basic\n"
        "\t\trollingWindowGranularity: year\n"
        "\t\trollingWindowPeriods: 3\n"
        "\t\tincrementalGranularity: month\n"
        "\t\tincrementalPeriods: 12\n"
        "\t\tsourceExpression =\n"
        "\t\t\t\tlet\n"
        "\t\t\t\t    Source = Csv.Document(File.Contents(\"sales.csv\")),\n"
        "\t\t\t\t    Filtered = Table.SelectRows(Source, each [OrderDate] >= RangeStart "
        "and [OrderDate] < RangeEnd)\n"
        "\t\t\t\tin\n"
        "\t\t\t\t    Filtered\n")
    # sits after the table properties, before the first member, nothing else moved
    assert text == original.replace(
        "\tlineageTag: 11111111-1111-1111-1111-111111111111\n\n",
        "\tlineageTag: 11111111-1111-1111-1111-111111111111\n\n" + expected_block + "\n")
    t = parse_table_file(path)
    assert len(t.measures) == 5 and len(t.columns) == 4

    exprs = {e["name"]: e for e in tools.list_expressions(state)}
    for name in ("RangeStart", "RangeEnd"):
        assert exprs[name]["kind"] == "parameter"
        assert exprs[name]["meta"] == {"IsParameterQuery": True, "Type": "DateTime",
                                       "IsParameterQueryRequired": True}
        assert exprs[name]["m"].startswith("#datetime(")
        assert exprs[name]["result_type"] == "DateTime"
    assert exprs["ServerParam"]["m"] == '"prod-server"'


def test_set_refresh_policy_idempotent_replace_and_hybrid(state):
    path = _defn(state) / "tables" / "Sales.tmdl"
    tools.set_refresh_policy(state, "Sales", "year", 3, "month", 12,
                             source_expression=M_INCREMENTAL)
    once = path.read_bytes()
    exprs_once = _read(_defn(state) / "expressions.tmdl")
    res = tools.set_refresh_policy(state, "Sales", "year", 3, "month", 12,
                                   source_expression=M_INCREMENTAL)
    assert res["action"] == "unchanged" and res["parameters_added"] == []
    assert path.read_bytes() == once
    assert _read(_defn(state) / "expressions.tmdl") == exprs_once

    res = tools.set_refresh_policy(state, "Sales", "Quarter", 8, "day", 5, mode="hybrid",
                                   incremental_periods_offset=-1,
                                   source_expression=M_INCREMENTAL)
    assert res["action"] == "updated"
    text = _read(path)
    assert text.count("\trefreshPolicy\n") == 1
    assert ("\t\tincrementalPeriods: 5\n\t\tincrementalPeriodsOffset: -1\n"
            "\t\tmode: hybrid\n\t\tsourceExpression =\n") in text
    assert "rollingWindowGranularity: quarter" in text
    assert tt.parse_refresh_policy_text(text)["mode"] == "hybrid"


def test_set_refresh_policy_defaults_to_partition_source(state):
    tools.update_partition(state, "Sales", M_INCREMENTAL)
    res = tools.set_refresh_policy(state, "Sales", "year", 2, "day", 7)
    assert res["policy"]["sourceExpression"] == M_INCREMENTAL
    assert "warnings" not in res


@pytest.mark.parametrize("kwargs, msg", [
    ({}, "RangeStart"),                                        # fixture query lacks the filter
    ({"source_expression": "let a = 1 in a"}, "RangeStart"),
    ({"source_expression": M_INCREMENTAL, "rolling_window_granularity": "week"}, "rolling_window_granularity"),
    ({"source_expression": M_INCREMENTAL, "incremental_periods": 0}, "incremental_periods"),
    ({"source_expression": M_INCREMENTAL, "mode": "directQuery"}, "mode"),
])
def test_set_refresh_policy_rejections_leave_files_alone(state, kwargs, msg):
    path = _defn(state) / "tables" / "Sales.tmdl"
    before = path.read_bytes()
    exprs_before = _read(_defn(state) / "expressions.tmdl")
    args = dict(rolling_window_granularity="year", rolling_window_periods=3,
                incremental_granularity="month", incremental_periods=12)
    args.update(kwargs)
    with pytest.raises(ValueError, match=msg):
        tools.set_refresh_policy(state, "Sales", **args)
    assert path.read_bytes() == before
    assert _read(_defn(state) / "expressions.tmdl") == exprs_before


def test_set_refresh_policy_needs_m_partition(state):
    with pytest.raises(ValueError, match="no M"):
        tools.set_refresh_policy(state, "Date", "year", 3, "month", 12,
                                 source_expression=M_INCREMENTAL)


def test_remove_refresh_policy_round_trips_to_original(state):
    path = _defn(state) / "tables" / "Sales.tmdl"
    original = path.read_bytes()
    tools.set_refresh_policy(state, "Sales", "year", 3, "month", 12,
                             source_expression=M_INCREMENTAL)
    assert b"refreshPolicy" in path.read_bytes()
    res = tools.remove_refresh_policy(state, "Sales")
    assert res["action"] == "removed"
    assert path.read_bytes() == original
    with pytest.raises(ValueError, match="no refreshPolicy"):
        tools.remove_refresh_policy(state, "Sales")
    # parameters intentionally stay
    assert {"RangeStart", "RangeEnd"} <= {e["name"] for e in tools.list_expressions(state)}


def test_refresh_policy_on_table_with_described_member(state):
    path = _defn(state) / "tables" / "Sales.tmdl"
    text = _read(path).replace("\tmeasure 'Net Revenue'", "\t/// Revenue\n\tmeasure 'Net Revenue'", 1)
    path.write_text(text, encoding="utf-8")
    tools.set_refresh_policy(state, "Sales", "year", 3, "month", 12,
                             source_expression=M_INCREMENTAL)
    after = _read(path)
    # the description must still be glued to its measure
    assert "\n\n\t/// Revenue\n\tmeasure 'Net Revenue'" in after
    assert after.index("\trefreshPolicy") < after.index("\t/// Revenue")
    assert parse_table_file(path).measures[0].name == "Net Revenue"


# --- delete table ---------------------------------------------------------------------

def test_delete_table_refused_lists_dependents(state):
    before = sorted(p.name for p in (_defn(state) / "tables").iterdir())
    with pytest.raises(ValueError) as exc:
        tools.delete_table(state, "Date")
    msg = str(exc.value)
    # Sales measures reference Date via ALL(Date) / COUNTROWS is Date's own
    assert "measures: ['Sales.Fenced Measure']" in msg
    assert "relationships" in msg and "f1a2b3c4" in msg
    assert "report_fields: ['Date.Year']" in msg
    assert "force=true" in msg
    assert sorted(p.name for p in (_defn(state) / "tables").iterdir()) == before

    with pytest.raises(ValueError) as exc:
        tools.delete_table(state, "Sales")
    msg = str(exc.value)
    assert "Sales.Margin %" in msg and "Sales.Net Revenue" in msg   # report usage
    assert "relationships" in msg
    assert "measures:" not in msg     # Date's measures don't reference Sales


def test_table_dependents_detail(state):
    deps = tools.table_dependents(state.project, "Date")
    assert deps["measures"] == [{"table": "Sales", "name": "Fenced Measure", "via": ["table"]}]
    assert deps["relationships"][0]["from"] == "Sales.OrderDate"
    assert deps["report_fields"] == ["Date.Year"]
    assert deps["calculated_columns"] == [] and deps["calculated_tables"] == []

    # a calculated column + calculated table elsewhere that lean on Date
    tools.create_calculated_table(state, "DateCopy", "SUMMARIZE(Date, Date[Year])")
    state.project.create_column("Sales", "Yr", "int64", dax="RELATED('Date'[Year])")
    deps = tools.table_dependents(state.project, "Date")
    assert deps["calculated_tables"] == [{"table": "DateCopy", "partition": "DateCopy", "via": ["table"]}]
    assert deps["calculated_columns"] == [{"table": "Sales", "name": "Yr", "via": ["table"]}]
    # measure-level dependency: another table's measure using one of Date's measures
    state.project.create_measure("Sales", "Avg Per Day", "DIVIDE([Net Revenue], [Days In Period])")
    deps = tools.table_dependents(state.project, "Date")
    assert {"table": "Sales", "name": "Avg Per Day",
            "via": ["measures: Days In Period"]} in deps["measures"]


def test_delete_unreferenced_table_no_force(state):
    tools.create_table(state, "Scratch", "Table.FromRows({})")
    res = tools.delete_table(state, "Scratch")
    assert res["action"] == "deleted" and res["forced"] is False
    assert res["removed"]["ref_table"] is True and res["removed"]["relationships"] == []
    assert not (_defn(state) / "tables" / "Scratch.tmdl").exists()
    assert list((_defn(state) / "tables").glob("Scratch.tmdl.bak-*"))
    model = _read(_defn(state) / "model.tmdl")
    assert "Scratch" not in model and "\tref table Sales\n\tref table Date\n" in model
    assert {t.name for t in _reload(state).list_tables()} == {"Sales", "Date"}
    with pytest.raises(KeyError):
        tools.delete_table(state, "Scratch")


def test_delete_table_forced_drops_ref_and_relationships(state):
    res = tools.delete_table(state, "Date", force=True)
    assert res["forced"] is True
    assert res["removed"]["file"] == "definition/tables/Date.tmdl"
    assert res["removed"]["ref_table"] is True
    assert [r["name"] for r in res["removed"]["relationships"]] == [
        "f1a2b3c4-0000-0000-0000-000000000001"]
    assert res["removed"]["relationships_file_deleted"] is True
    assert res["dangling_references"]["measures"][0]["name"] == "Fenced Measure"
    assert res["dangling_references"]["report_fields"] == ["Date.Year"]

    assert not (_defn(state) / "tables" / "Date.tmdl").exists()
    assert not (_defn(state) / "relationships.tmdl").exists()
    assert list(_defn(state).glob("relationships.tmdl.bak-*"))
    assert "ref table Date" not in _read(_defn(state) / "model.tmdl")
    reloaded = _reload(state)
    assert [t.name for t in reloaded.list_tables()] == ["Sales"]
    assert reloaded.list_relationships() == []


def test_delete_table_keeps_other_relationships(state):
    state.project.create_column("Sales", "Key", "int64")
    tools.create_table(state, "Dim", "Table.FromRows({})",
                       [{"name": "Key", "data_type": "int64"}])
    state.project.create_relationship("Sales", "Key", "Dim", "Key")
    assert len(state.project.list_relationships()) == 2
    res = tools.delete_table(state, "Dim", force=True)
    assert len(res["removed"]["relationships"]) == 1
    assert res["removed"]["relationships_file_deleted"] is False
    rels = _reload(state).list_relationships()
    assert [(r.from_table, r.to_table) for r in rels] == [("Sales", "Date")]


# --- text-level helpers -----------------------------------------------------------------

def test_parse_and_render_meta_round_trip():
    raw = '[IsParameterQuery=true, Type="Text", List={"a", "b"}, DefaultValue="x""y", Nested=[a=1]]'
    meta = tt.parse_meta(raw)
    assert meta == {"IsParameterQuery": True, "Type": "Text", "List": ["a", "b"],
                    "DefaultValue": 'x"y', "Nested": {"m": "[a=1]"}}
    assert tt.render_meta(meta) == raw
    assert tt.split_meta('"srv" meta [IsParameterQuery=true]') == ('"srv"', "[IsParameterQuery=true]")
    assert tt.split_meta("Value.Metadata(x)") == ("Value.Metadata(x)", None)


def test_remove_relationships_text_handles_descriptions_and_layout():
    text = ("/// first\nrelationship a\n\tfromColumn: Sales.Key\n\ttoColumn: Dim.Key\n\n"
            "relationship b\n\tisActive: false\n\tfromColumn: Sales.'Other Key'\n"
            "\ttoColumn: 'Other Dim'.Key\n")
    kept, removed = tt.remove_relationships_for_table_text(text, "Other Dim")
    assert [r["name"] for r in removed] == ["b"]
    assert kept == "/// first\nrelationship a\n\tfromColumn: Sales.Key\n\ttoColumn: Dim.Key\n"
    kept, removed = tt.remove_relationships_for_table_text(text, "dim")
    assert [r["name"] for r in removed] == ["a"]
    assert kept.startswith("relationship b\n")
    assert tt.remove_relationships_for_table_text(text, "Nope") == (text, [])


def test_table_file_name_sanitised():
    assert tt.table_file_name("Sales") == "Sales.tmdl"
    assert tt.table_file_name("A/B:C?") == "A_B_C_.tmdl"
    with pytest.raises(ValueError):
        tt.table_file_name("...")


# --- MCP surface --------------------------------------------------------------------------

def _payload(result):
    structured = None
    if isinstance(result, tuple):
        result, structured = result
    structured = (structured
                  or getattr(result, "structured_content", None)
                  or getattr(result, "structuredContent", None))
    if structured is not None:
        return structured.get("result", structured) if isinstance(structured, dict) else structured
    content = getattr(result, "content", result)
    items = [json.loads(c.text) for c in content]
    return items[0] if len(items) == 1 else items


def _hint(tool, name):
    a = tool.annotations
    snake = {"readOnlyHint": "read_only_hint", "destructiveHint": "destructive_hint",
             "idempotentHint": "idempotent_hint"}[name]
    return getattr(a, name, None) if hasattr(a, name) else getattr(a, snake, None)


def test_tools_registered_with_annotations():
    import model_server.server as mod

    tools_by_name = {t.name: t for t in asyncio.run(mod.mcp.list_tools())}
    expected = {
        "pbi_create_table": (False, False), "pbi_create_calculated_table": (False, False),
        "pbi_update_partition": (False, False), "pbi_list_partitions": (True, False),
        "pbi_create_expression": (False, False), "pbi_list_expressions": (True, False),
        "pbi_update_expression": (False, False), "pbi_set_refresh_policy": (False, False),
        "pbi_remove_refresh_policy": (False, True), "pbi_delete_table": (False, True),
    }
    for name, (read_only, destructive) in expected.items():
        tool = tools_by_name[name]
        assert _hint(tool, "readOnlyHint") is read_only, name
        assert _hint(tool, "destructiveHint") is destructive, name
        schema = getattr(tool, "inputSchema", None) or getattr(tool, "input_schema")
        assert ("dry_run" in schema["properties"]) is (not read_only), name
        assert tool.description and "Returns" in tool.description or read_only
    assert _hint(tools_by_name["pbi_update_partition"], "idempotentHint") is True
    assert _hint(tools_by_name["pbi_set_refresh_policy"], "idempotentHint") is True


def test_delete_table_dry_run_via_mcp_touches_nothing(tmp_path):
    import model_server.server as mod
    from core.journal import snapshot

    proj = tmp_path / "p"
    shutil.copytree(SYNTH, proj)

    def call(tool_name, **args):
        return _payload(asyncio.run(mod.mcp.call_tool(tool_name, args)))

    call("pbi_set_project", path=str(proj / "Synthetic.pbip"))
    before = snapshot(proj)
    preview = call("pbi_delete_table", table="Date", force=True, dry_run=True)
    assert preview["dry_run"] is True
    assert preview["result"]["removed"]["ref_table"] is True
    assert "definition/tables/Date.tmdl" in " ".join(preview["changes"]["deleted"])
    assert "ref table Date" in preview["diff"]
    assert snapshot(proj) == before

    real = call("pbi_create_table", name="Orders", m_source=M_SOURCE, columns=COLUMNS)
    assert real["ok"] is True
    assert call("pbi_list_partitions", table="Orders")[0]["partitions"][0]["kind"] == "m"
    undone = call("pbi_undo")
    assert undone["undone"][0]["tool"] == "pbi_create_table"
    assert snapshot(proj) == before
