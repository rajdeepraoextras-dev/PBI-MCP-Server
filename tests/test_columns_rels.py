"""Day 10: pbi_create_column + pbi_create_relationship.

Throwaway copies of the synthetic fixture; reload from disk to prove
persistence; byte-checks to prove loss-free surgery.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from core.pbip import PbipProject
from model_server.server import (
    ModelState, create_column, create_relationship, set_project,
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


def _cols(project: PbipProject, table: str) -> dict:
    t = {t.name: t for t in project.list_tables()}[table]
    return {c.name: c for c in t.columns}


# --- create_column ----------------------------------------------------------

def test_create_data_column_persists(state):
    res = create_column(state, "Sales", "Region", "string",
                        summarize_by="none")
    assert res["action"] == "created"
    col = _cols(_reload(state), "Sales")["Region"]
    assert col.data_type == "string"
    assert col.summarize_by == "none"


def test_create_column_source_column_defaults_to_name(state):
    create_column(state, "Sales", "Region", "string")
    text = state.project._table_file("Sales").read_text(encoding="utf-8-sig")
    assert "sourceColumn: Region" in text


def test_create_calculated_column(state):
    create_column(state, "Sales", "Margin Amount", "double",
                  summarize_by="sum", dax="Sales[Amount] - Sales[Cost]")
    text = state.project._table_file("Sales").read_text(encoding="utf-8-sig")
    assert "column 'Margin Amount' = Sales[Amount] - Sales[Cost]" in text
    assert _cols(_reload(state), "Sales")["Margin Amount"].data_type == "double"
    # calculated columns must not get a sourceColumn
    block = text[text.index("'Margin Amount'"):]
    assert "sourceColumn" not in block.split("column ")[0]


def test_create_column_duplicate_rejected(state):
    with pytest.raises(ValueError, match="already exists"):
        create_column(state, "Sales", "Amount", "double")


def test_create_column_unknown_table_rejected(state):
    with pytest.raises(KeyError):
        create_column(state, "Ghost", "X", "string")


def test_create_column_preserves_rest_of_file(state):
    before = state.project._table_file("Sales").read_text(encoding="utf-8-sig")
    create_column(state, "Sales", "Region", "string")
    after = state.project._table_file("Sales").read_text(encoding="utf-8-sig")
    for token in ["partition Sales = m", "Csv.Document", "Net Revenue",
                  "Fenced Measure", "column Amount", "lineageTag:"]:
        assert token in after, token
    assert len(after) > len(before)


def test_new_column_visible_to_measures_upsert(state):
    """A created column is immediately usable as a measure dependency."""
    create_column(state, "Sales", "Region", "string")
    state.project.upsert_measure("Sales", "Region Count",
                                 "DISTINCTCOUNT(Sales[Region])")
    lin = _reload(state).model_lineage("Region Count")
    assert "Sales.Region" in lin["depends_on_columns"]


# --- create_relationship ------------------------------------------------------

def test_create_relationship_persists(state):
    create_column(state, "Sales", "ShipDate", "dateTime")
    res = create_relationship(state, "Sales", "ShipDate", "Date", "Date")
    assert res["action"] == "created"
    rels = _reload(state).list_relationships()
    match = [r for r in rels if r.from_column == "ShipDate"]
    assert match and match[0].to_table == "Date" and match[0].to_column == "Date"


def test_existing_relationships_preserved(state):
    create_column(state, "Sales", "ShipDate", "dateTime")
    before = len(state.project.list_relationships())
    create_relationship(state, "Sales", "ShipDate", "Date", "Date")
    rels = _reload(state).list_relationships()
    assert len(rels) == before + 1
    # the original OrderDate rel is still there
    assert any(r.from_column == "OrderDate" for r in rels)


def test_relationship_unknown_endpoint_rejected(state):
    with pytest.raises(KeyError, match="not found"):
        create_relationship(state, "Sales", "Ghost", "Date", "Date")
    with pytest.raises(KeyError, match="not found"):
        create_relationship(state, "Sales", "Amount", "Ghost", "Date")


def test_duplicate_relationship_rejected(state):
    # OrderDate -> Date.Date already exists in the fixture
    with pytest.raises(ValueError, match="already exists"):
        create_relationship(state, "Sales", "OrderDate", "Date", "Date")


def test_relationship_quoted_column_written(state):
    create_column(state, "Sales", "Ship Date", "dateTime")
    create_relationship(state, "Sales", "Ship Date", "Date", "Date")
    text = (state.project._require_model() / "definition"
            / "relationships.tmdl").read_text(encoding="utf-8-sig")
    assert "Sales.'Ship Date'" in text
    # and it parses back cleanly
    rels = _reload(state).list_relationships()
    assert any(r.from_column == "Ship Date" for r in rels)


def test_inactive_relationship_flagged(state):
    create_column(state, "Sales", "ShipDate", "dateTime")
    create_relationship(state, "Sales", "ShipDate", "Date", "Date",
                        is_active=False)
    text = (state.project._require_model() / "definition"
            / "relationships.tmdl").read_text(encoding="utf-8-sig")
    assert "isActive: false" in text
