"""Day 2: TMDL reader tests against the synthetic fixture.

The synthetic fixture mirrors real PBIP TMDL (tab-indented, one table/file).
When a real export lands at fixtures/Sample.pbip, add a parametrization to
run these same assertions against it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.pbip import PbipProject
from core.tmdl import parse_table_file

FIXTURE = Path(__file__).parent / "fixtures" / "synthetic" / "Synthetic.pbip"
SALES = (
    Path(__file__).parent
    / "fixtures" / "synthetic" / "Synthetic.SemanticModel"
    / "definition" / "tables" / "Sales.tmdl"
)


def test_fixture_exists():
    assert FIXTURE.exists(), "synthetic fixture missing — run gen_fixture.py"


# --- table-file parsing -----------------------------------------------------

def test_parse_table_name():
    table = parse_table_file(SALES)
    assert table.name == "Sales"


def test_measure_names_and_folders():
    table = parse_table_file(SALES)
    by_name = {m.name: m for m in table.measures}
    assert set(by_name) == {
        "Net Revenue", "Margin %", "Complex Measure",
        "Fenced Measure", "Hidden Helper",
    }
    assert by_name["Net Revenue"].display_folder == "KPIs"


def test_inline_measure_dax_and_format():
    m = {m.name: m for m in parse_table_file(SALES).measures}["Net Revenue"]
    assert m.dax == "SUM(Sales[Amount])"
    assert m.format_string == "#,0"


def test_multiline_measure_body_captured():
    m = {m.name: m for m in parse_table_file(SALES).measures}["Complex Measure"]
    assert "VAR _rev = [Net Revenue]" in m.dax
    assert "RETURN" in m.dax
    assert "DIVIDE(_rev, [Order Count])" in m.dax
    # properties must NOT leak into the expression body
    assert "formatString" not in m.dax
    assert m.format_string == "#,0"


def test_fenced_measure_body_captured():
    m = {m.name: m for m in parse_table_file(SALES).measures}["Fenced Measure"]
    assert m.dax.startswith("CALCULATE(")
    assert "ALL(Date)" in m.dax
    assert "```" not in m.dax
    assert m.format_string == "#,0"


def test_hidden_measure_flag():
    m = {m.name: m for m in parse_table_file(SALES).measures}["Hidden Helper"]
    assert m.is_hidden is True
    assert m.dax == "1"


def test_columns_parsed():
    table = parse_table_file(SALES)
    cols = {c.name: c for c in table.columns}
    assert {"Amount", "Cost", "OrderDate", "Order Count"} <= set(cols)
    assert cols["Amount"].data_type == "double"
    assert cols["Amount"].summarize_by == "sum"
    assert cols["Order Count"].is_hidden is True


# --- via PbipProject --------------------------------------------------------

def test_project_lists_tables():
    project = PbipProject(FIXTURE)
    names = {t.name for t in project.list_tables()}
    assert names == {"Sales", "Date"}


def test_project_list_measures_all():
    project = PbipProject(FIXTURE)
    names = {m.name for m in project.list_measures()}
    assert "Net Revenue" in names
    assert "Days In Period" in names  # from Date.tmdl


def test_project_list_measures_filtered():
    project = PbipProject(FIXTURE)
    sales_only = project.list_measures(table="Sales")
    assert all(m.table == "Sales" for m in sales_only)
    assert "Days In Period" not in {m.name for m in sales_only}


def test_resolves_from_semantic_model_folder():
    sm = FIXTURE.parent / "Synthetic.SemanticModel"
    project = PbipProject(sm)
    assert {t.name for t in project.list_tables()} == {"Sales", "Date"}
