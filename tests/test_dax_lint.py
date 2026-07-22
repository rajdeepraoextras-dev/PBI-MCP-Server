"""DAX linter — catches the common Power-BI-rejected patterns, non-blocking."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from core.dax_lint import lint_dax
from core.pbip import PbipProject

SYNTH = Path(__file__).parent / "fixtures" / "synthetic"


# --- unit --------------------------------------------------------------------

def test_measure_in_calculate_boolean_filter_flagged():
    w = lint_dax("CALCULATE([X], orders[Year]=[PrevYear])", {"PrevYear", "X"})
    assert w and "PLACEHOLDER" in w[0]


def test_filter_form_not_flagged():
    # the correct fix must NOT warn
    w = lint_dax("CALCULATE([X], FILTER(ALL(orders[Year]), orders[Year]=[PrevYear]))",
                 {"PrevYear", "X"})
    assert w == []


def test_empty_argument_flagged():
    w = lint_dax("TOPN(1, ALL(orders[city]), [Sales], , DESC)")
    assert w and "Empty argument" in w[0]


def test_topn_correct_not_flagged():
    assert lint_dax("TOPN(1, ALL(orders[city]), [Sales], DESC)") == []


def test_column_equals_constant_not_flagged():
    # column = string/number constant is a valid CALCULATE filter
    assert lint_dax('CALCULATE([Rev], Sales[Region]="West")', {"Rev"}) == []
    assert lint_dax("CALCULATE([Rev], Sales[Year]=2024)", {"Rev"}) == []


def test_plain_measures_clean():
    for dax in ["SUM(Sales[Amount])", "DIVIDE([A]-[B], [B])",
                "AVERAGEX(Sales, Sales[Qty]*Sales[Price])"]:
        assert lint_dax(dax, {"A", "B"}) == []


# --- wired into measure creation (non-blocking) ------------------------------

@pytest.fixture
def project(tmp_path) -> PbipProject:
    dst = tmp_path / "s"
    shutil.copytree(SYNTH, dst)
    return PbipProject(dst / "Synthetic.pbip")


def test_create_measure_returns_warning_but_still_creates(project):
    project.create_measure("Sales", "PrevYear", "2023")
    res = project.create_measure(
        "Sales", "Bad One", "CALCULATE([Net Revenue], Sales[Cost]=[PrevYear])")
    assert res["ok"] is True                      # still created (non-blocking)
    assert "warnings" in res
    # measure is actually on disk
    assert "Bad One" in {m.name for m in
                         PbipProject(project.path).list_measures()}


def test_bulk_flags_bad_measures(project):
    res = project.bulk_create_measures([
        {"table": "Sales", "name": "Good", "dax": "SUM(Sales[Amount])"},
        {"table": "Sales", "name": "BadTopN",
         "dax": "TOPN(1, ALL(Sales[Amount]), [Net Revenue], , DESC)"},
    ])
    assert res["count"] == 2
    assert "BadTopN" in res["warnings"]
    assert "Good" not in res.get("warnings", {})


def test_clean_bulk_has_no_warnings(project):
    res = project.bulk_create_measures([
        {"table": "Sales", "name": "C1", "dax": "1"},
        {"table": "Sales", "name": "C2", "dax": "[C1] + 1"},
    ])
    assert "warnings" not in res
