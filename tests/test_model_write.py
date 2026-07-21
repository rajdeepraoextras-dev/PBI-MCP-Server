"""Day 9: model write tools — pbi_create_measure / pbi_update_measure.

Each test runs on a throwaway copy of the synthetic fixture and reloads from
disk to prove the mutation persisted (the automatable half of "survives a
Desktop reopen").
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from core.pbip import PbipProject
from model_server.server import (
    ModelState, create_measure, set_project, update_measure,
)

SYNTH = Path(__file__).parent / "fixtures" / "synthetic"


@pytest.fixture
def state(tmp_path) -> ModelState:
    dst = tmp_path / "synthetic"
    shutil.copytree(SYNTH, dst)
    st = ModelState()
    set_project(st, str(dst / "Synthetic.pbip"))
    return st


def _reload(state: ModelState) -> PbipProject:
    return PbipProject(state.project.path)


def _measures(project: PbipProject, table: str | None = None) -> dict:
    return {m.name: m for m in project.list_measures(table)}


# --- create -----------------------------------------------------------------

def test_create_measure_persists(state):
    res = create_measure(state, "Sales", "Test M", "BLANK()",
                         format="#,0", display_folder="_QA")
    assert res["action"] == "created"
    m = _measures(_reload(state))["Test M"]
    assert m.dax == "BLANK()"
    assert m.format_string == "#,0"
    assert m.display_folder == "_QA"


def test_create_duplicate_name_rejected(state):
    with pytest.raises(ValueError, match="already exists"):
        create_measure(state, "Sales", "Net Revenue", "1")


def test_create_duplicate_does_not_mutate(state):
    before = state.project._table_file("Sales").read_bytes()
    with pytest.raises(ValueError):
        create_measure(state, "Sales", "Margin %", "1")
    assert state.project._table_file("Sales").read_bytes() == before


def test_create_in_unknown_table_rejected(state):
    with pytest.raises(KeyError):
        create_measure(state, "NoSuchTable", "X", "1")


def test_create_backup_written(state):
    tfile = state.project._table_file("Sales")
    create_measure(state, "Sales", "Test M", "1")
    assert list(tfile.parent.glob("Sales.tmdl.bak-*"))


# --- update -----------------------------------------------------------------

def test_update_dax_only_preserves_format_and_folder(state):
    # 'Net Revenue' starts as fmt '#,0', folder 'KPIs'
    update_measure(state, "Sales", "Net Revenue", dax="SUM(Sales[Cost])")
    m = _measures(_reload(state))["Net Revenue"]
    assert m.dax == "SUM(Sales[Cost])"
    assert m.format_string == "#,0"
    assert m.display_folder == "KPIs"


def test_update_format_only_preserves_dax(state):
    before = _measures(state.project)["Net Revenue"].dax
    update_measure(state, "Sales", "Net Revenue", format="0.00")
    m = _measures(_reload(state))["Net Revenue"]
    assert m.dax == before
    assert m.format_string == "0.00"


def test_update_missing_measure_rejected(state):
    with pytest.raises(KeyError):
        update_measure(state, "Sales", "Ghost", dax="1")


def test_update_wrong_table_rejected(state):
    # 'Net Revenue' lives in Sales, not Date
    with pytest.raises(ValueError, match="lives in table"):
        update_measure(state, "Date", "Net Revenue", dax="1")


def test_update_does_not_duplicate(state):
    update_measure(state, "Sales", "Net Revenue", dax="42")
    names = [m.name for m in _reload(state).list_measures(table="Sales")]
    assert names.count("Net Revenue") == 1


def test_other_measures_untouched_by_update(state):
    update_measure(state, "Sales", "Net Revenue", dax="1")
    reloaded = _measures(_reload(state), "Sales")
    assert {"Net Revenue", "Margin %", "Complex Measure",
            "Fenced Measure", "Hidden Helper"} <= set(reloaded)
