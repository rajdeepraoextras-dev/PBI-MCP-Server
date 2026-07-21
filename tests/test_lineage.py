"""Day 7: model lineage (DAX dependency graph)."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.lineage import extract_references
from core.pbip import PbipProject
from model_server.server import ModelState, model_lineage, set_project

SYNTH = Path(__file__).parent / "fixtures" / "synthetic" / "Synthetic.pbip"
REAL_HR = (
    Path(__file__).parent / "fixtures" / "real" / "hr-sample"
    / "Human Resources Sample PBIX.pbip"
)


# --- reference extraction ---------------------------------------------------

def test_extract_bare_measure_ref():
    assert (None, "Net Revenue") in extract_references("[Net Revenue] * 2")


def test_extract_qualified_column():
    assert ("Sales", "Amount") in extract_references("SUM(Sales[Amount])")


def test_extract_quoted_table():
    assert ("Date Table", "Date") in extract_references("ALL('Date Table'[Date])")


def test_ignores_strings_and_comments():
    dax = 'IF([X] > 0, "not a [ref]", [Y]) -- [comment]'
    refs = {name for _, name in extract_references(dax)}
    assert refs == {"X", "Y"}


# --- synthetic graph --------------------------------------------------------

def test_synthetic_margin_deps():
    lin = PbipProject(SYNTH).model_lineage("Margin %")
    assert lin["depends_on_measures"] == ["Net Revenue"]
    assert lin["depends_on_columns"] == ["Sales.Cost"]


def test_synthetic_bare_column_resolves_to_home_table():
    # 'Complex Measure' references [Order Count], a Sales column, with no qualifier
    lin = PbipProject(SYNTH).model_lineage("Complex Measure")
    assert "Sales.Order Count" in lin["depends_on_columns"]
    assert "Net Revenue" in lin["depends_on_measures"]


def test_reverse_edges():
    lin = PbipProject(SYNTH).model_lineage("Net Revenue")
    # Margin % and Complex Measure both reference Net Revenue
    assert {"Margin %", "Complex Measure"} <= set(lin["referenced_by"])


def test_full_graph_shape():
    full = PbipProject(SYNTH).model_lineage()
    assert "measures" in full and "referenced_by" in full
    assert "Net Revenue" in full["measures"]


# --- real HR graph ----------------------------------------------------------

@pytest.mark.skipif(not REAL_HR.exists(), reason="real HR fixture not present")
def test_real_measure_to_measure():
    lin = PbipProject(REAL_HR).model_lineage("AVG Tenure Months")
    assert "AVG Tenure Days" in lin["depends_on_measures"]


@pytest.mark.skipif(not REAL_HR.exists(), reason="real HR fixture not present")
def test_real_transitive_dependents():
    lin = PbipProject(REAL_HR).model_lineage("EmpCount")
    # EmpCount feeds Actives directly, and a long chain transitively
    assert "Actives" in lin["referenced_by"]
    assert len(lin["referenced_by_transitive"]) > len(lin["referenced_by"])


@pytest.mark.skipif(not REAL_HR.exists(), reason="real HR fixture not present")
def test_no_unresolved_refs_in_real_model():
    """A clean model should leave no bracketed ref unclassified."""
    full = PbipProject(REAL_HR).model_lineage()
    offenders = {
        name: node["unresolved"]
        for name, node in full["measures"].items()
        if node.get("unresolved")
    }
    assert not offenders, f"unresolved references: {offenders}"


# --- via server tool --------------------------------------------------------

def test_tool_lineage_requires_project():
    with pytest.raises(ValueError):
        model_lineage(ModelState())


def test_tool_lineage_unknown_measure():
    st = ModelState()
    set_project(st, str(SYNTH))
    with pytest.raises(KeyError):
        model_lineage(st, "Does Not Exist")
