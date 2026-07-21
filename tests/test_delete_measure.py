"""Day 11: pbi_delete_measure with the lineage guard."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from core.pbip import PbipProject
from model_server.server import ModelState, delete_measure, set_project

SYNTH = Path(__file__).parent / "fixtures" / "synthetic"


@pytest.fixture
def state(tmp_path) -> ModelState:
    dst = tmp_path / "synthetic"
    shutil.copytree(SYNTH, dst)
    st = ModelState()
    set_project(st, str(dst / "Synthetic.pbip"))
    return st


def _names(state) -> set[str]:
    return {m.name for m in PbipProject(state.project.path).list_measures()}


def test_delete_unreferenced_measure(state):
    # 'Hidden Helper' (= 1) has no dependents
    res = delete_measure(state, "Sales", "Hidden Helper")
    assert res["action"] == "deleted"
    assert "Hidden Helper" not in _names(state)


def test_delete_referenced_measure_refused(state):
    # 'Net Revenue' is referenced by Margin % / Complex Measure / Fenced Measure
    with pytest.raises(ValueError, match="Refusing to delete"):
        delete_measure(state, "Sales", "Net Revenue")
    assert "Net Revenue" in _names(state)  # untouched


def test_refusal_names_the_dependents(state):
    with pytest.raises(ValueError, match="Margin %"):
        delete_measure(state, "Sales", "Net Revenue")


def test_force_overrides_guard(state):
    res = delete_measure(state, "Sales", "Net Revenue", force=True)
    assert "Margin %" in res["forced_past_dependents"]
    assert "Net Revenue" not in _names(state)


def test_delete_missing_measure(state):
    with pytest.raises(KeyError):
        delete_measure(state, "Sales", "Ghost")


def test_delete_wrong_table(state):
    with pytest.raises(ValueError, match="lives in table"):
        delete_measure(state, "Date", "Net Revenue")


def test_delete_preserves_rest_of_file(state):
    delete_measure(state, "Sales", "Hidden Helper")
    text = state.project._table_file("Sales").read_text(encoding="utf-8-sig")
    for token in ["Net Revenue", "Margin %", "Complex Measure",
                  "Fenced Measure", "column Amount", "partition Sales = m"]:
        assert token in text, token
    assert "Hidden Helper" not in text


def test_delete_then_recreate(state):
    delete_measure(state, "Sales", "Hidden Helper")
    state.project.create_measure("Sales", "Hidden Helper", "2")
    m = {m.name: m for m in PbipProject(state.project.path)
         .list_measures()}["Hidden Helper"]
    assert m.dax == "2"
