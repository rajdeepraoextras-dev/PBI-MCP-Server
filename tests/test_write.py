"""Day 4: safe write layer tests.

Every test runs on a fresh copy of the synthetic fixture (never the committed
original). The bar: mutations are correct AND loss-free — no other content in
a touched file may change.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from core import io_safe
from core.pbip import PbipProject

SYNTH = Path(__file__).parent / "fixtures" / "synthetic"


@pytest.fixture
def project(tmp_path) -> PbipProject:
    dst = tmp_path / "synthetic"
    shutil.copytree(SYNTH, dst)
    return PbipProject(dst / "Synthetic.pbip")


def _sales_text(project: PbipProject) -> str:
    return project._table_file("Sales").read_text(encoding="utf-8-sig")


# --- io_safe units ----------------------------------------------------------

def test_atomic_write_creates_and_replaces(tmp_path):
    p = tmp_path / "sub" / "f.txt"
    io_safe.atomic_write(p, "hello\nworld")
    assert p.read_text(encoding="utf-8") == "hello\nworld"
    io_safe.atomic_write(p, "again")
    assert p.read_text(encoding="utf-8") == "again"
    # no stray temp files left behind
    assert list(p.parent.glob("*.tmp-*")) == []


def test_backup_snapshots_existing(tmp_path):
    p = tmp_path / "f.txt"
    assert io_safe.backup(p) is None      # nothing to back up yet
    p.write_text("v1", encoding="utf-8")
    bak = io_safe.backup(p)
    assert bak is not None and bak.read_text(encoding="utf-8") == "v1"


# --- upsert_measure: insert -------------------------------------------------

def test_insert_new_measure(project):
    project.upsert_measure("Sales", "Test M", "BLANK()", fmt="#,0")
    m = {m.name: m for m in project.list_measures(table="Sales")}["Test M"]
    assert m.dax == "BLANK()"
    assert m.format_string == "#,0"


def test_insert_preserves_everything_else(project):
    before = _sales_text(project)
    project.upsert_measure("Sales", "Test M", "BLANK()")
    after = _sales_text(project)
    # existing members + partition/source/lineageTag all still present
    for token in ["lineageTag:", "partition Sales = m", "Csv.Document",
                  "Net Revenue", "Margin %", "Complex Measure",
                  "Fenced Measure", "column Amount"]:
        assert token in after, token
    assert len(after) > len(before)


def test_multiline_measure_roundtrips(project):
    dax = "VAR _x = [Net Revenue]\nRETURN\n    _x * 2"
    project.upsert_measure("Sales", "Doubled", dax)
    m = {m.name: m for m in project.list_measures(table="Sales")}["Doubled"]
    assert "VAR _x = [Net Revenue]" in m.dax
    assert "_x * 2" in m.dax


# --- upsert_measure: replace ------------------------------------------------

def test_replace_existing_measure(project):
    project.upsert_measure("Sales", "Net Revenue", "SUM(Sales[Cost])", fmt="0")
    measures = {m.name: m for m in project.list_measures(table="Sales")}
    assert measures["Net Revenue"].dax == "SUM(Sales[Cost])"
    assert measures["Net Revenue"].format_string == "0"
    # no measure lost in the replace
    assert {"Net Revenue", "Margin %", "Complex Measure",
            "Fenced Measure", "Hidden Helper"} <= set(measures)


def test_replace_does_not_duplicate(project):
    project.upsert_measure("Sales", "Net Revenue", "1")
    names = [m.name for m in project.list_measures(table="Sales")]
    assert names.count("Net Revenue") == 1


# --- backups ----------------------------------------------------------------

def test_mutation_creates_backup(project):
    tfile = project._table_file("Sales")
    project.upsert_measure("Sales", "Test M", "BLANK()")
    assert list(tfile.parent.glob("Sales.tmdl.bak-*")), "expected a backup"


def test_backup_once_per_file(project):
    tfile = project._table_file("Sales")
    project.upsert_measure("Sales", "A", "1")
    project.upsert_measure("Sales", "B", "2")
    assert len(list(tfile.parent.glob("Sales.tmdl.bak-*"))) == 1


def test_backups_disabled(tmp_path):
    dst = tmp_path / "synthetic"
    shutil.copytree(SYNTH, dst)
    project = PbipProject(dst / "Synthetic.pbip", backups=False)
    project.upsert_measure("Sales", "A", "1")
    tfile = project._table_file("Sales")
    assert list(tfile.parent.glob("Sales.tmdl.bak-*")) == []


# --- create_page ------------------------------------------------------------

def test_create_page(project):
    pid = project.create_page("QA")
    assert pid == "qa"
    pages = {p.id: p for p in project.list_pages()}
    assert "qa" in pages
    assert pages["qa"].name == "QA"
    # registered in pages.json order, after the originals
    assert [p.id for p in project.list_pages()][-1] == "qa"


def test_create_page_unique_ids(project):
    a = project.create_page("QA")
    b = project.create_page("QA")
    assert a != b
    assert {a, b} <= {p.id for p in project.list_pages()}


# --- add_visual -------------------------------------------------------------

def test_add_card_bound_to_measure(project):
    pid = project.create_page("QA")
    vid = project.add_visual(pid, {
        "visual_type": "card",
        "bindings": {"Values": ["Sales.Net Revenue"]},
        "title": "Revenue",
    })
    v = project.get_visual(pid, vid)
    assert v.visual_type == "card"
    assert v.title == "Revenue"
    # bound as a Measure (not a Column)
    proj = v.raw["visual"]["query"]["queryState"]["Values"]["projections"][0]
    assert "Measure" in proj["field"]
    assert proj["queryRef"] == "Sales.Net Revenue"


def test_add_visual_column_binding_uses_column_kind(project):
    pid = project.create_page("QA")
    vid = project.add_visual(pid, {
        "visual_type": "slicer",
        "bindings": {"Values": ["Date.Year"]},
    })
    v = project.get_visual(pid, vid)
    proj = v.raw["visual"]["query"]["queryState"]["Values"]["projections"][0]
    assert "Column" in proj["field"]


# --- mini B.3 integration (dry run of the Day-5 gate, synthetic) ------------

def test_b3_style_integration(project):
    project.upsert_measure("Sales", "Test M", "BLANK()")
    pid = project.create_page("QA")
    vid = project.add_visual(pid, {
        "visual_type": "card",
        "bindings": {"Values": ["Sales.Test M"]},
    })
    project.save()

    reopened = PbipProject(project.path)
    assert "Test M" in {m.name for m in reopened.list_measures()}
    assert pid in {p.id for p in reopened.list_pages()}
    assert vid in {v.id for v in reopened.list_visuals(pid)}
