"""Day 6: model-server read tools + relationships parsing.

Exercises the tool logic directly against the real HR fixture (falls back to
the synthetic project if the real one isn't present).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.pbip import PbipProject
from model_server.server import (
    ModelState, get_model, list_measures, set_project,
)

REAL_HR = (
    Path(__file__).parent / "fixtures" / "real" / "hr-sample"
    / "Human Resources Sample PBIX.pbip"
)
SYNTH = (
    Path(__file__).parent / "fixtures" / "synthetic" / "Synthetic.pbip"
)
PROJECT_PATH = REAL_HR if REAL_HR.exists() else SYNTH


@pytest.fixture
def state() -> ModelState:
    st = ModelState()
    set_project(st, str(PROJECT_PATH))
    return st


# --- pbi_set_project --------------------------------------------------------

def test_set_project_summary():
    st = ModelState()
    summary = set_project(st, str(PROJECT_PATH))
    assert summary["ok"] is True
    assert summary["tables"] > 0
    assert summary["measures"] > 0
    assert st.project is not None


def test_tools_require_project():
    st = ModelState()
    with pytest.raises(ValueError):
        get_model(st)
    with pytest.raises(ValueError):
        list_measures(st)


def test_set_project_bad_path(tmp_path):
    st = ModelState()
    with pytest.raises(FileNotFoundError):
        set_project(st, str(tmp_path / "nope.pbip"))


# --- pbi_get_model ----------------------------------------------------------

def test_get_model_shape(state):
    model = get_model(state)
    assert set(model) >= {"path", "tables", "relationships", "counts"}
    assert model["counts"]["tables"] == len(model["tables"])
    a_table = model["tables"][0]
    assert set(a_table) >= {"name", "columns", "measures"}


def test_get_model_is_json_serializable(state):
    import json
    json.dumps(get_model(state))  # must not raise


# --- pbi_list_measures ------------------------------------------------------

def test_list_measures(state):
    measures = list_measures(state)
    assert measures and all("dax" in m and "name" in m for m in measures)


def test_list_measures_filter(state):
    all_m = list_measures(state)
    some_table = all_m[0]["table"]
    filtered = list_measures(state, table=some_table)
    assert filtered and all(m["table"] == some_table for m in filtered)


# --- relationships parsing --------------------------------------------------

def test_relationships_parsed():
    project = PbipProject(str(PROJECT_PATH))
    rels = project.list_relationships()
    assert rels, "expected at least one relationship"
    r = rels[0]
    assert r.from_table and r.from_column
    assert r.to_table and r.to_column


def test_relationship_quoted_column():
    """The HR model has a relationship to Ethnicity.'Ethnic Group' (quoted)."""
    if not REAL_HR.exists():
        pytest.skip("real HR fixture not present")
    rels = PbipProject(str(REAL_HR)).list_relationships()
    quoted = [r for r in rels if r.to_column == "Ethnic Group"]
    assert quoted, "expected the quoted-column relationship to unquote cleanly"
