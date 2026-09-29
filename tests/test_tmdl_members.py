"""Column / table / measure lifecycle, descriptions, hierarchies, KPIs.

core/tmdl_members.py (span-tracking TMDL parser + splice editors) and
model_server/tools_members.py (the pbi_* tools). Every write runs on a
throwaway copy of the synthetic fixture; round-trip tests reload with the
core parser and compare untouched member blocks byte for byte.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from core import tmdl_members as tm
from core.journal import snapshot
from core.tmdl import parse_table_file
from model_server.server import ModelState, set_project
from model_server.tools_members import (
    column_dependents, create_hierarchy, delete_column, delete_hierarchy,
    list_columns, list_hierarchies, remove_kpi, report_hierarchy_usage,
    set_measure_properties, update_column, update_table,
)

SYNTH = Path(__file__).parent / "fixtures" / "synthetic"
TABLES = SYNTH / "Synthetic.SemanticModel" / "definition" / "tables"
NEW_TOOLS = (
    "pbi_list_columns", "pbi_update_column", "pbi_update_table",
    "pbi_set_measure_properties", "pbi_remove_kpi", "pbi_delete_column",
    "pbi_create_hierarchy", "pbi_list_hierarchies", "pbi_delete_hierarchy",
)


@pytest.fixture
def state(tmp_path) -> ModelState:
    dst = tmp_path / "synthetic"
    shutil.copytree(SYNTH, dst)
    st = ModelState()
    set_project(st, str(dst / "Synthetic.pbip"))
    return st


def _path(state, table: str) -> Path:
    return state.project._table_file(table)


def _text(state, table: str) -> str:
    return _path(state, table).read_text(encoding="utf-8-sig")


def _original(table: str) -> str:
    return (TABLES / f"{table}.tmdl").read_text(encoding="utf-8-sig")


def _members(text: str) -> dict[tuple[str, str], str]:
    """{(kind, name): exact block text} for every member of the table."""
    lines = text.split("\n")
    tbl = tm.parse_table(lines)
    return {(c.kind, c.name): "\n".join(lines[c.start:c.end]) for c in tbl.children}


def _assert_others_untouched(before: dict, after: dict, *targets):
    for key, block in before.items():
        if key not in targets:
            assert after[key] == block, key


def _columns(state, table: str) -> dict:
    return {c["name"]: c for c in list_columns(state, table)}


# --- parser -------------------------------------------------------------------

def test_parse_table_members_expressions_and_spans():
    lines = _original("Sales").split("\n")
    tbl = tm.parse_table(lines)
    assert tbl.name == "Sales"
    assert tbl.value("lineageTag") == "11111111-1111-1111-1111-111111111111"
    kinds = [(c.kind, c.name) for c in tbl.children]
    assert kinds == [
        ("measure", "Net Revenue"), ("measure", "Margin %"),
        ("measure", "Complex Measure"), ("measure", "Fenced Measure"),
        ("measure", "Hidden Helper"), ("column", "Amount"), ("column", "Cost"),
        ("column", "OrderDate"), ("column", "Order Count"), ("partition", "Sales"),
    ]
    complex_ = tbl.child("measure", "Complex Measure")
    assert complex_.expression == ("VAR _rev = [Net Revenue]\nRETURN\n"
                                   "\tDIVIDE(_rev, [Order Count])")
    assert complex_.value("formatString") == "#,0"
    fenced = tbl.child("measure", "Fenced Measure")
    assert fenced.expression.startswith("CALCULATE(") and fenced.expression.endswith(")")
    assert fenced.value("formatString") == "#,0"
    hidden = tbl.child("measure", "Hidden Helper")
    assert hidden.expression == "1" and hidden.flag("isHidden")
    part = tbl.child("partition", "Sales")
    assert part.expression == "m" and part.value("mode") == "import"
    assert part.prop("source").is_expr and "Csv.Document" in part.prop("source").value
    # spans are ordered, disjoint and exclude the blank separators
    ends = [c.end for c in tbl.children]
    starts = [c.start for c in tbl.children]
    assert all(e <= s for e, s in zip(ends, starts[1:]))
    assert all(lines[c.end - 1].strip() for c in tbl.children)
    assert tbl.end == len(lines) - 1  # file ends with a newline


def test_parse_descriptions_kpi_and_nameless_children():
    text = (
        "/// Fact table\n"
        "table T\n"
        "\tlineageTag: x\n\n"
        "\t/// Sales in USD\n"
        "\t/// second line\n"
        "\tmeasure Sales = SUM(T[A])\n"
        "\t\tformatString: #,0\n\n"
        "\t\tkpi\n"
        "\t\t\ttargetExpression = [Goal]\n"
        "\t\t\tstatusGraphic: Shapes\n"
        "\t\t\tstatusExpression =\n"
        "\t\t\t\t\tVAR x = 1\n"
        "\t\t\t\t\tRETURN x\n\n"
        "\t\t\tannotation GoalType = Measure\n\n"
        "\t\tannotation PBI_FormatHint = {\"isGeneralNumber\":true}\n\n"
        "\tcolumn A\n"
        "\t\tdataType: int64\n"
        "\t\trelatedColumnDetails\n"
        "\t\t\tgroupByColumn: B\n"
        "\t\tisHidden\n"
    )
    lines = text.split("\n")
    tbl = tm.parse_table(lines)
    assert tbl.description == ["Fact table"] and tbl.start == 0 and tbl.header == 1
    m = tbl.child("measure", "Sales")
    assert m.description == ["Sales in USD", "second line"] and m.start == 4
    kpi = m.child("kpi")
    assert kpi is not None and kpi.name == ""
    assert kpi.value("targetExpression") == "[Goal]"
    assert kpi.value("statusGraphic") == "Shapes"
    assert kpi.value("statusExpression") == "VAR x = 1\nRETURN x"
    assert [a.name for a in kpi.children_of("annotation")] == ["GoalType"]
    assert [a.name for a in m.children_of("annotation")] == ["PBI_FormatHint"]
    col = tbl.child("column", "A")
    assert col.child("relatedColumnDetails") is not None   # not a boolean prop
    assert col.flag("isHidden") and col.value("dataType") == "int64"


def test_property_insert_lands_after_multiline_expression():
    text = ("table T\n\tcolumn Calc =\n\t\t\tVAR x = 1\n\t\t\tRETURN x\n\n"
            "\tcolumn B\n\t\tdataType: string\n")
    lines = text.split("\n")
    calc = tm.parse_table(lines).child("column", "Calc")
    assert calc.expression == "VAR x = 1\nRETURN x" and calc.body_end == 4
    new = tm.set_property(lines, calc, "dataType", "int64")
    assert "\n".join(new) == ("table T\n\tcolumn Calc =\n\t\t\tVAR x = 1\n\t\t\tRETURN x\n"
                              "\t\tdataType: int64\n\n\tcolumn B\n\t\tdataType: string\n")


def test_delete_span_tidies_blank_lines_and_final_newline():
    body = "table T\n\n\tcolumn A\n\t\tdataType: string\n\n\tcolumn B\n\t\tdataType: string"
    for text, expected in [(body, "table T\n\n\tcolumn A\n\t\tdataType: string"),
                           (body + "\n", "table T\n\n\tcolumn A\n\t\tdataType: string\n")]:
        lines = text.split("\n")
        b = tm.parse_table(lines).child("column", "B")
        assert "\n".join(tm.delete_span(lines, b.start, b.end)) == expected
    lines = body.split("\n")
    a = tm.parse_table(lines).child("column", "A")
    assert "\n".join(tm.delete_span(lines, a.start, a.end)) == \
        "table T\n\n\tcolumn B\n\t\tdataType: string"


# --- pbi_list_columns -----------------------------------------------------------

def test_list_columns_fields(state):
    cols = _columns(state, "Sales")
    assert set(cols) == {"Amount", "Cost", "OrderDate", "Order Count"}
    amount = cols["Amount"]
    assert amount == {
        "table": "Sales", "name": "Amount", "data_type": "double",
        "is_hidden": False, "is_key": False, "summarize_by": "sum",
        "format_string": None, "data_category": None, "sort_by_column": None,
        "display_folder": None, "description": None, "is_calculated": False,
        "dax": None, "source_column": "Amount",
    }
    assert cols["Order Count"]["is_hidden"] is True
    assert cols["Order Count"]["source_column"] is None
    assert _columns(state, "Date")["Date"]["is_key"] is True
    assert {c["table"] for c in list_columns(state)} == {"Sales", "Date"}
    with pytest.raises(KeyError):
        list_columns(state, "Ghost")


def test_list_columns_detects_calculated(state):
    state.project.create_column("Sales", "Margin Amt", "double",
                                dax="Sales[Amount] - Sales[Cost]")
    col = _columns(state, "Sales")["Margin Amt"]
    assert col["is_calculated"] is True
    assert col["dax"] == "Sales[Amount] - Sales[Cost]"
    assert col["source_column"] is None


# --- pbi_update_column ----------------------------------------------------------

def test_update_column_sets_every_property_surgically(state):
    before = _members(_original("Sales"))
    res = update_column(state, "Sales", "Amount", description="Gross amount\nin USD",
                        format_string="#,0.00", data_category="Latitude",
                        sort_by_column="Cost", is_hidden=True,
                        display_folder="Money", summarize_by="average")
    assert res["ok"] and res["changed"] is True
    text = _text(state, "Sales")
    after = _members(text)
    _assert_others_untouched(before, after, ("column", "Amount"))
    assert after[("column", "Amount")] == (
        "\t/// Gross amount\n\t/// in USD\n"
        "\tcolumn Amount\n"
        "\t\tdataType: double\n"
        "\t\tsummarizeBy: average\n"
        "\t\tsourceColumn: Amount\n"
        "\t\tformatString: #,0.00\n"
        "\t\tdataCategory: Latitude\n"
        "\t\tsortByColumn: Cost\n"
        "\t\tisHidden\n"
        "\t\tdisplayFolder: Money")
    # the core parser still reads the file, and sees the new values
    core_col = {c.name: c for c in parse_table_file(_path(state, "Sales")).columns}["Amount"]
    assert core_col.is_hidden and core_col.summarize_by == "average"
    assert core_col.data_category == "Latitude" and core_col.data_type == "double"
    info = _columns(state, "Sales")["Amount"]
    assert info["description"] == "Gross amount\nin USD"
    assert info["format_string"] == "#,0.00" and info["sort_by_column"] == "Cost"
    assert info["display_folder"] == "Money" and info["is_hidden"] is True
    assert res["column_after"] == info


def test_update_column_idempotent_replace_and_remove(state):
    original = _original("Sales")
    kwargs = dict(format_string="0.0%", description="Desc", is_hidden=True,
                  data_type="decimal")
    update_column(state, "Sales", "Cost", **kwargs)
    once = _text(state, "Sales")
    res = update_column(state, "Sales", "Cost", **kwargs)
    assert res["changed"] is False and _text(state, "Sales") == once

    update_column(state, "Sales", "Cost", description="New", format_string="0")
    text = _text(state, "Sales")
    assert "\t/// New\n\tcolumn Cost\n" in text and "/// Desc" not in text
    cost = _members(text)[("column", "Cost")]
    assert cost.count("formatString") == 1 and "\t\tformatString: 0\n" in cost + "\n"
    assert "0.0%" not in cost

    update_column(state, "Sales", "Cost", description="", format_string="",
                  is_hidden=False, data_type="double")
    assert _text(state, "Sales") == original            # byte-for-byte round trip


def test_update_column_normalises_enum_case(state):
    update_column(state, "Sales", "Amount", data_category="weburl",
                  data_type="DATETIME", summarize_by="distinctcount")
    text = _text(state, "Sales")
    for line in ("\t\tdataCategory: WebUrl", "\t\tdataType: dateTime",
                 "\t\tsummarizeBy: distinctCount"):
        assert line in text


def test_update_column_quotes_values_that_need_it(state):
    update_column(state, "Sales", "Amount", display_folder=" Padded ",
                  sort_by_column="Order Count")
    text = _text(state, "Sales")
    assert '\t\tdisplayFolder: " Padded "' in text
    assert "\t\tsortByColumn: 'Order Count'" in text
    info = _columns(state, "Sales")["Amount"]
    assert info["display_folder"] == " Padded " and info["sort_by_column"] == "Order Count"


def test_update_column_validation_writes_nothing(state):
    original = _original("Sales")
    cases = [
        (dict(data_category="Money"), ValueError, "data_category"),
        (dict(sort_by_column="Ghost"), ValueError, "sort_by_column"),
        (dict(sort_by_column="Amount"), ValueError, "itself"),
        (dict(summarize_by="total"), ValueError, "summarize_by"),
        (dict(data_type="text"), ValueError, "data_type"),
        ({}, ValueError, "Nothing to update"),
    ]
    for kwargs, exc, match in cases:
        with pytest.raises(exc, match=match):
            update_column(state, "Sales", "Amount", **kwargs)
    with pytest.raises(KeyError):
        update_column(state, "Sales", "Ghost", is_hidden=True)
    with pytest.raises(KeyError):
        update_column(state, "Ghost", "Amount", is_hidden=True)
    assert _text(state, "Sales") == original


def test_update_column_preserves_crlf_and_bom(state):
    p = _path(state, "Sales")
    p.write_bytes(b"\xef\xbb\xbf" + p.read_bytes().replace(b"\n", b"\r\n"))
    update_column(state, "Sales", "Amount", is_hidden=True)
    raw = p.read_bytes()
    assert raw[:3] == b"\xef\xbb\xbf"
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")
    assert "\t\tsourceColumn: Amount\n\t\tisHidden\n" in _text(state, "Sales")


# --- pbi_update_table -----------------------------------------------------------

def test_update_table_description_and_hidden_roundtrip(state):
    original = _original("Sales")
    before = _members(original)
    res = update_table(state, "Sales", description="Fact table", is_hidden=True)
    assert res["changed"] and res["is_hidden"] and res["description"] == "Fact table"
    text = _text(state, "Sales")
    assert text.startswith("/// Fact table\ntable Sales\n"
                           "\tlineageTag: 11111111-1111-1111-1111-111111111111\n"
                           "\tisHidden\n\n\tmeasure 'Net Revenue'")
    _assert_others_untouched(before, _members(text))
    t = parse_table_file(_path(state, "Sales"))
    assert t.is_hidden and len(t.measures) == 5 and len(t.columns) == 4

    assert update_table(state, "Sales", description="Fact table", is_hidden=True)["changed"] is False
    update_table(state, "Sales", description="", is_hidden=False)
    assert _text(state, "Sales") == original
    with pytest.raises(ValueError, match="Nothing to update"):
        update_table(state, "Sales")
    with pytest.raises(KeyError):
        update_table(state, "Ghost", is_hidden=True)


# --- pbi_set_measure_properties / pbi_remove_kpi -------------------------------

def test_measure_description_and_hidden_roundtrip(state):
    original = _original("Sales")
    before = _members(original)
    res = set_measure_properties(state, "Sales", "Net Revenue",
                                 description="Sum of Amount", is_hidden=True)
    assert res["changed"] and res["is_hidden"] and res["description"] == "Sum of Amount"
    text = _text(state, "Sales")
    _assert_others_untouched(before, _members(text), ("measure", "Net Revenue"))
    assert ("\t/// Sum of Amount\n\tmeasure 'Net Revenue' = SUM(Sales[Amount])\n"
            "\t\tformatString: #,0\n\t\tdisplayFolder: KPIs\n\t\tisHidden\n\n"
            "\tmeasure 'Margin %'") in text
    m = {m.name: m for m in parse_table_file(_path(state, "Sales")).measures}["Net Revenue"]
    assert m.is_hidden and m.dax == "SUM(Sales[Amount])" and m.format_string == "#,0"

    set_measure_properties(state, "Sales", "Net Revenue", description="", is_hidden=False)
    assert _text(state, "Sales") == original
    with pytest.raises(ValueError, match="lives in table"):
        set_measure_properties(state, "Date", "Net Revenue", is_hidden=True)
    with pytest.raises(KeyError):
        set_measure_properties(state, "Sales", "Ghost", is_hidden=True)
    with pytest.raises(ValueError, match="Nothing to update"):
        set_measure_properties(state, "Sales", "Net Revenue")


def test_kpi_block_shape_replace_and_remove(state):
    original = _original("Sales")
    before = _members(original)
    status = "VAR x = [Net Revenue]\nRETURN\n\tIF(x > 100, 1, IF(x > 50, 0, -1))"
    res = set_measure_properties(state, "Sales", "Net Revenue", kpi={
        "target_measure": "Sales.Margin %", "status_dax": status,
        "trend_dax": "1", "description": "Revenue vs margin"})
    text = _text(state, "Sales")
    _assert_others_untouched(before, _members(text), ("measure", "Net Revenue"))
    assert _members(text)[("measure", "Net Revenue")] == (
        "\tmeasure 'Net Revenue' = SUM(Sales[Amount])\n"
        "\t\tformatString: #,0\n"
        "\t\tdisplayFolder: KPIs\n"
        "\n"
        "\t\t/// Revenue vs margin\n"
        "\t\tkpi\n"
        "\t\t\ttargetExpression = [Margin %]\n"
        "\t\t\tstatusGraphic: Traffic Light - Single\n"
        "\t\t\tstatusExpression = ```\n"
        "\t\t\t\tVAR x = [Net Revenue]\n"
        "\t\t\t\tRETURN\n"
        "\t\t\t\t\tIF(x > 100, 1, IF(x > 50, 0, -1))\n"
        "\t\t\t\t```\n"
        "\t\t\ttrendGraphic: Standard Arrow\n"
        "\t\t\ttrendExpression = 1\n"
        "\n"
        "\t\t\tannotation GoalType = Measure\n"
        "\n"
        "\t\t\tannotation KpiStatusType = Linear")
    assert res["kpi"] == {
        "description": "Revenue vs margin", "target_expression": "[Margin %]",
        "target_format_string": None, "status_expression": status,
        "status_graphic": "Traffic Light - Single", "trend_expression": "1",
        "trend_graphic": "Standard Arrow",
        "annotations": {"GoalType": "Measure", "KpiStatusType": "Linear"},
    }
    m = {m.name: m for m in parse_table_file(_path(state, "Sales")).measures}["Net Revenue"]
    assert m.dax == "SUM(Sales[Amount])" and m.format_string == "#,0"

    # same kpi again -> no change; a different one replaces the block
    assert set_measure_properties(state, "Sales", "Net Revenue", kpi={
        "target_measure": "Margin %", "status_dax": status, "trend_dax": "1",
        "description": "Revenue vs margin"})["changed"] is False
    set_measure_properties(state, "Sales", "Net Revenue", kpi={
        "target_dax": "1000", "status_dax": "IF([Net Revenue] > 1000, 1, -1)",
        "status_graphic": "Shapes", "target_format_string": "#,0",
        "annotations": {"KpiStatusType": None, "Custom": "x"}})
    text = _text(state, "Sales")
    assert text.count("\t\tkpi\n") == 1 and "Margin %]" not in text.split("kpi", 1)[1].split("annotation", 1)[0]
    block = _members(text)[("measure", "Net Revenue")]
    assert "\t\t\ttargetExpression = 1000\n\t\t\ttargetFormatString: #,0\n\t\t\tstatusGraphic: Shapes\n" in block
    assert "annotation GoalType = Absolute" in block and "annotation Custom = x" in block
    assert "KpiStatusType" not in block and "trendGraphic" not in block

    res = remove_kpi(state, "Sales", "Net Revenue")
    assert res["changed"] is True and _text(state, "Sales") == original
    assert remove_kpi(state, "Sales", "Net Revenue")["changed"] is False


def test_kpi_validation_writes_nothing(state):
    original = _original("Sales")
    bad = [
        {"status_dax": "1"},                                          # no target
        {"target_measure": "Sales.Margin %", "target_dax": "1", "status_dax": "1"},
        {"target_dax": "1"},                                          # no status
        {"target_dax": "1", "status_dax": "1", "bogus": 1},
        {"target_dax": "1", "status_dax": "1", "annotations": ["x"]},
        "not a dict",
    ]
    for kpi in bad:
        with pytest.raises(ValueError):
            set_measure_properties(state, "Sales", "Net Revenue", kpi=kpi)
    with pytest.raises(KeyError):
        set_measure_properties(state, "Sales", "Net Revenue",
                               kpi={"target_measure": "Sales.Ghost", "status_dax": "1"})
    with pytest.raises(ValueError, match="lives in table"):
        set_measure_properties(state, "Sales", "Net Revenue",
                               kpi={"target_measure": "Date.Margin %", "status_dax": "1"})
    assert _text(state, "Sales") == original


# --- pbi_delete_column ----------------------------------------------------------

def test_delete_column_guards_list_dependents(state):
    original = _original("Sales")
    with pytest.raises(ValueError, match="Net Revenue"):          # SUM(Sales[Amount])
        delete_column(state, "Sales", "Amount")
    with pytest.raises(ValueError, match="Complex Measure"):      # bare [Order Count]
        delete_column(state, "Sales", "Order Count")
    with pytest.raises(ValueError, match="relationships"):
        delete_column(state, "Sales", "OrderDate")
    with pytest.raises(ValueError, match="report"):               # bar1 + slicer1
        delete_column(state, "Date", "Year")
    assert _text(state, "Sales") == original
    deps = column_dependents(state.project, "Date", "Year")
    assert deps["report"] == ["pages/details/visuals/slicer1/visual.json",
                              "pages/overview/visuals/bar1/visual.json"]
    deps = column_dependents(state.project, "Sales", "OrderDate")
    assert deps["relationships"] == [
        "f1a2b3c4-0000-0000-0000-000000000001 (Sales.OrderDate -> Date.Date)"]
    assert deps["measures"] == [] and deps["report"] == []


def test_delete_column_guards_sort_by_hierarchy_and_calculated_column(state):
    state.project.create_column("Sales", "Region", "string")
    state.project.create_column("Sales", "RegionKey", "int64", summarize_by="none")
    update_column(state, "Sales", "Region", sort_by_column="RegionKey")
    with pytest.raises(ValueError, match="sort_by"):
        delete_column(state, "Sales", "RegionKey")
    update_column(state, "Sales", "Region", sort_by_column="")

    create_hierarchy(state, "Sales", "Geo", [{"column": "Region"}, {"column": "RegionKey"}])
    with pytest.raises(ValueError, match="hierarchy_levels"):
        delete_column(state, "Sales", "RegionKey")
    delete_hierarchy(state, "Sales", "Geo")

    state.project.create_column("Sales", "Key2", "int64", dax="Sales[RegionKey] * 2")
    with pytest.raises(ValueError, match="calculated_columns"):
        delete_column(state, "Sales", "RegionKey")
    deps = column_dependents(state.project, "Sales", "RegionKey")
    assert deps["calculated_columns"] == ["Sales.Key2"]
    # a bare [Col] inside a calculated column of the same table counts too
    state.project.create_column("Sales", "Key3", "int64", dax="[RegionKey] + 1")
    assert column_dependents(state.project, "Sales", "RegionKey")["calculated_columns"] == [
        "Sales.Key2", "Sales.Key3"]


def test_delete_unreferenced_column_is_clean(state):
    original = _original("Sales")
    state.project.create_column("Sales", "Region", "string")
    before = _members(_text(state, "Sales"))
    res = delete_column(state, "Sales", "Region")
    assert res["action"] == "deleted" and res["forced"] is False
    assert res["removed"] == {"relationships": [], "hierarchy_levels": [],
                              "hierarchies": [], "sort_by_cleared": []}
    after = _members(_text(state, "Sales"))
    assert ("column", "Region") not in after
    _assert_others_untouched(before, after, ("column", "Region"))
    assert _text(state, "Sales") == original
    parse_table_file(_path(state, "Sales"))
    with pytest.raises(KeyError):
        delete_column(state, "Sales", "Region")


def test_delete_column_force_removes_relationship_levels_and_sort_by(state):
    create_hierarchy(state, "Sales", "Time", [{"column": "OrderDate"}])
    create_hierarchy(state, "Sales", "Mixed", [{"column": "Amount"},
                                               {"name": "Day", "column": "OrderDate"}])
    state.project.create_column("Sales", "Region", "string")
    update_column(state, "Sales", "Region", sort_by_column="OrderDate")

    res = delete_column(state, "Sales", "OrderDate", force=True)
    assert res["forced"] is True
    assert res["removed"]["relationships"] == ["f1a2b3c4-0000-0000-0000-000000000001"]
    assert res["removed"]["hierarchies"] == ["Time"]
    assert sorted(res["removed"]["hierarchy_levels"]) == ["Mixed/Day", "Time/OrderDate"]
    assert res["removed"]["sort_by_cleared"] == ["Region"]
    assert res["still_referenced_by"] == {"measures": [], "calculated_columns": [],
                                          "report": []}

    text = _text(state, "Sales")
    assert "OrderDate" not in text and "sortByColumn" not in text
    assert state.project.list_relationships() == []
    hiers = {h["name"]: h for h in list_hierarchies(state, "Sales")}
    assert set(hiers) == {"Mixed"}
    assert hiers["Mixed"]["levels"] == [{"name": "Amount", "column": "Amount"}]
    t = parse_table_file(_path(state, "Sales"))
    assert {c.name for c in t.columns} == {"Amount", "Cost", "Order Count", "Region"}
    assert len(t.measures) == 5


def test_delete_column_force_reports_what_still_breaks(state):
    res = delete_column(state, "Sales", "Amount", force=True)
    assert res["still_referenced_by"]["measures"] == ["Net Revenue"]
    assert "column Amount" not in _text(state, "Sales")
    assert "measure 'Net Revenue' = SUM(Sales[Amount])" in _text(state, "Sales")


# --- hierarchies ------------------------------------------------------------------

def test_create_list_delete_hierarchy_roundtrip(state):
    original = _original("Date")
    before = _members(original)
    res = create_hierarchy(state, "Date", "Calendar",
                           [{"name": "Year", "column": "Year"}, {"column": "Date"}],
                           description="Year > Date", hidden=True)
    expected_levels = [{"name": "Year", "column": "Year"}, {"name": "Date", "column": "Date"}]
    assert res["action"] == "created" and res["hierarchy"]["levels"] == expected_levels

    text = _text(state, "Date")
    assert text.index("\tcolumn Year") < text.index("\thierarchy Calendar") < text.index("\tpartition Date")
    after = _members(text)
    _assert_others_untouched(before, after)
    lines = after[("hierarchy", "Calendar")].split("\n")
    assert lines[:3] == ["\t/// Year > Date", "\thierarchy Calendar", "\t\tisHidden"]
    assert lines[3].startswith("\t\tlineageTag: ") and len(lines[3]) == len("\t\tlineageTag: ") + 36
    assert lines[4] == "" and lines[5] == "\t\tlevel Year"
    assert lines[6].startswith("\t\t\tlineageTag: ") and lines[7] == "\t\t\tcolumn: Year"
    assert lines[8] == "" and lines[9] == "\t\tlevel Date" and lines[11] == "\t\t\tcolumn: Date"
    assert len(lines) == 12
    parse_table_file(_path(state, "Date"))

    assert list_hierarchies(state) == [{
        "table": "Date", "name": "Calendar", "description": "Year > Date",
        "is_hidden": True, "display_folder": None, "levels": expected_levels}]
    assert list_hierarchies(state, "Sales") == []
    with pytest.raises(KeyError):
        list_hierarchies(state, "Ghost")

    res = delete_hierarchy(state, "Date", "Calendar")
    assert res["action"] == "deleted" and res["forced_past_report_usage"] == []
    assert _text(state, "Date") == original
    assert list_hierarchies(state) == []


def test_create_hierarchy_validation_writes_nothing(state):
    original = _original("Date")
    cases = [
        ("H", [{"column": "Ghost"}], "not in"),
        ("H", [], "non-empty"),
        ("H", [{"name": "Y"}], "column"),
        ("H", [{"column": "Year"}, {"column": "Year"}], "Duplicate level"),
        ("Year", [{"column": "Year"}], "column named"),
        ("", [{"column": "Year"}], "empty"),
    ]
    for name, levels, match in cases:
        with pytest.raises(ValueError, match=match):
            create_hierarchy(state, "Date", name, levels)
    with pytest.raises(KeyError):
        create_hierarchy(state, "Ghost", "H", [{"column": "Year"}])
    assert _text(state, "Date") == original
    create_hierarchy(state, "Date", "H", [{"column": "Year"}])
    with pytest.raises(ValueError, match="already exists"):
        create_hierarchy(state, "Date", "H", [{"column": "Year"}])
    with pytest.raises(KeyError):
        delete_hierarchy(state, "Date", "Ghost")


def test_delete_hierarchy_guarded_by_report_usage(state):
    create_hierarchy(state, "Date", "Calendar", [{"column": "Year"}])
    vf = (state.project.report_dir / "definition" / "pages" / "details"
          / "visuals" / "slicer1" / "visual.json")
    data = json.loads(vf.read_text(encoding="utf-8-sig"))
    data["visual"]["query"]["queryState"]["Values"]["projections"] = [{
        "field": {"HierarchyLevel": {
            "Expression": {"Hierarchy": {
                "Expression": {"SourceRef": {"Entity": "Date"}},
                "Hierarchy": "Calendar"}},
            "Level": "Year"}},
        "queryRef": "Date.Calendar.Year"}]
    vf.write_text(json.dumps(data), encoding="utf-8")

    used = ["pages/details/visuals/slicer1/visual.json"]
    assert report_hierarchy_usage(state.project, "Date", "Calendar") == used
    assert report_hierarchy_usage(state.project, "Date", "Other") == []
    with pytest.raises(ValueError, match="Refusing to delete hierarchy"):
        delete_hierarchy(state, "Date", "Calendar")
    assert list_hierarchies(state, "Date")
    res = delete_hierarchy(state, "Date", "Calendar", force=True)
    assert res["forced_past_report_usage"] == used
    assert list_hierarchies(state, "Date") == []


# --- MCP registration -------------------------------------------------------------

def _ann(tool, hint: str):
    a = tool.annotations
    snake = {"readOnlyHint": "read_only_hint", "destructiveHint": "destructive_hint",
             "idempotentHint": "idempotent_hint"}[hint]
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
    content = getattr(result, "content", result)
    items = [json.loads(c.text) for c in content]
    return items[0] if len(items) == 1 else items


def test_tools_registered_with_annotations_and_dry_run():
    import model_server.server as mod
    tools = {t.name: t for t in asyncio.run(mod.mcp.list_tools())}
    for name in NEW_TOOLS:
        assert name in tools, name
        assert (tools[name].description or "").strip()
    reads = ("pbi_list_columns", "pbi_list_hierarchies")
    for name in reads:
        assert _ann(tools[name], "readOnlyHint") is True
        assert "dry_run" not in _schema(tools[name]).get("properties", {})
    for name in set(NEW_TOOLS) - set(reads):
        assert _ann(tools[name], "readOnlyHint") is False
        assert _schema(tools[name])["properties"]["dry_run"]["type"] == "boolean", name
    for name in ("pbi_delete_column", "pbi_delete_hierarchy", "pbi_remove_kpi"):
        assert _ann(tools[name], "destructiveHint") is True
    for name in ("pbi_update_column", "pbi_update_table", "pbi_set_measure_properties"):
        assert _ann(tools[name], "destructiveHint") is False
        assert _ann(tools[name], "idempotentHint") is True
    assert "kpi" in _schema(tools["pbi_set_measure_properties"])["properties"]
    assert "levels" in _schema(tools["pbi_create_hierarchy"])["properties"]


def test_dry_run_then_write_then_undo_via_mcp(tmp_path):
    import model_server.server as mod
    proj = tmp_path / "p"
    shutil.copytree(SYNTH, proj)
    sales = proj / "Synthetic.SemanticModel" / "definition" / "tables" / "Sales.tmdl"

    def call(tool_name, **args):
        return _payload(asyncio.run(mod.mcp.call_tool(tool_name, args)))

    call("pbi_set_project", path=str(proj / "Synthetic.pbip"))
    before = snapshot(proj)
    preview = call("pbi_update_column", table="Sales", name="Amount",
                   description="Preview", dry_run=True)
    assert preview["dry_run"] is True and preview["result"]["changed"] is True
    assert "+\t/// Preview" in preview["diff"]
    assert snapshot(proj) == before

    real = call("pbi_update_column", table="Sales", name="Amount", description="Real")
    assert real["ok"] is True
    assert "\t/// Real\n\tcolumn Amount" in sales.read_text(encoding="utf-8-sig")
    assert call("pbi_undo_history")[0]["tool"] == "pbi_update_column"
    call("pbi_undo")
    assert snapshot(proj) == before

    cols = call("pbi_list_columns", table="Date")
    assert [c["name"] for c in cols] == ["Date", "Year"]
