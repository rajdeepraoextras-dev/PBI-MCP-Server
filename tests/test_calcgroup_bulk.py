"""Day 12: pbi_create_calc_group + pbi_bulk_create_measures."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from core.pbip import PbipProject

SYNTH = Path(__file__).parent / "fixtures" / "synthetic"

TI_ITEMS = [
    {"name": "CY", "dax": "SELECTEDMEASURE()"},
    {"name": "PY", "dax":
        "CALCULATE(SELECTEDMEASURE(), SAMEPERIODLASTYEAR('Date'[Date]))"},
    {"name": "YoY %", "dax":
        "VAR _cy = SELECTEDMEASURE()\n"
        "VAR _py = CALCULATE(SELECTEDMEASURE(), SAMEPERIODLASTYEAR('Date'[Date]))\n"
        "RETURN\n    DIVIDE(_cy - _py, _py)"},
]


@pytest.fixture
def project(tmp_path) -> PbipProject:
    dst = tmp_path / "synthetic"
    shutil.copytree(SYNTH, dst)
    return PbipProject(dst / "Synthetic.pbip")


# --- calc group ---------------------------------------------------------------

def test_calc_group_table_created(project):
    res = project.create_calc_group("Time Intelligence", 1, TI_ITEMS)
    assert res["items"] == ["CY", "PY", "YoY %"]
    reloaded = PbipProject(project.path)
    t = {t.name: t for t in reloaded.list_tables()}["Time Intelligence"]
    assert t.is_calc_group is True


def test_calc_group_registered_in_model(project):
    project.create_calc_group("Time Intelligence", 1, TI_ITEMS)
    model_text = (project._require_model() / "definition" / "model.tmdl") \
        .read_text(encoding="utf-8-sig")
    assert "ref table 'Time Intelligence'" in model_text
    # existing refs kept
    assert "ref table Sales" in model_text


def test_calc_group_file_shape(project):
    project.create_calc_group("Time Intelligence", 1, TI_ITEMS)
    text = (project._require_model() / "definition" / "tables"
            / "Time Intelligence.tmdl").read_text(encoding="utf-8-sig")
    assert "calculationGroup" in text
    assert "precedence: 1" in text
    assert "calculationItem CY = SELECTEDMEASURE()" in text
    assert "calculationItem 'YoY %' = ```" in text  # multiline -> fenced
    assert "sortByColumn: Ordinal" in text
    assert "partition 'Time Intelligence' = calculationGroup" in text


def test_calc_group_duplicate_table_rejected(project):
    with pytest.raises(ValueError, match="already exists"):
        project.create_calc_group("Sales", 1, TI_ITEMS)


def test_calc_group_empty_items_rejected(project):
    with pytest.raises(ValueError, match="at least one item"):
        project.create_calc_group("TI", 1, [])


# --- bulk create ----------------------------------------------------------------

def test_bulk_create_across_tables(project):
    batch = [
        {"table": "Sales", "name": f"KPI {i}", "dax": f"{i}", "format": "#,0"}
        for i in range(1, 6)
    ] + [
        {"table": "Date", "name": "Day Count", "dax": "COUNTROWS(Date)"},
    ]
    res = project.bulk_create_measures(batch)
    assert res["count"] == 6
    assert res["tables"] == ["Date", "Sales"]
    names = {m.name for m in PbipProject(project.path).list_measures()}
    assert {"KPI 1", "KPI 5", "Day Count"} <= names


def test_bulk_single_write_per_table(project):
    """One backup per touched file proves one write path per table."""
    batch = [{"table": "Sales", "name": f"B{i}", "dax": "1"} for i in range(10)]
    project.bulk_create_measures(batch)
    tfile = project._table_file("Sales")
    assert len(list(tfile.parent.glob("Sales.tmdl.bak-*"))) == 1


def test_bulk_rejects_existing_name_before_any_write(project):
    before = project._table_file("Date").read_bytes()
    batch = [
        {"table": "Date", "name": "New OK", "dax": "1"},
        {"table": "Sales", "name": "Net Revenue", "dax": "1"},  # exists!
    ]
    with pytest.raises(ValueError, match="already exists"):
        project.bulk_create_measures(batch)
    # nothing was written, not even the valid first entry
    assert project._table_file("Date").read_bytes() == before


def test_bulk_rejects_intra_batch_duplicate(project):
    batch = [
        {"table": "Sales", "name": "Dup", "dax": "1"},
        {"table": "Date", "name": "Dup", "dax": "2"},
    ]
    with pytest.raises(ValueError, match="within the batch"):
        project.bulk_create_measures(batch)


def test_bulk_rejects_unknown_table(project):
    with pytest.raises(KeyError):
        project.bulk_create_measures(
            [{"table": "Ghost", "name": "X", "dax": "1"}])


def test_bulk_measures_visible_to_lineage(project):
    project.bulk_create_measures([
        {"table": "Sales", "name": "Base X", "dax": "SUM(Sales[Amount])"},
        {"table": "Sales", "name": "Double X", "dax": "[Base X] * 2"},
    ])
    lin = PbipProject(project.path).model_lineage("Double X")
    assert lin["depends_on_measures"] == ["Base X"]
