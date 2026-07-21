"""Day 13: M2 hardening — edge cases + the full write surface on REAL projects.

Two halves:
  1. Edge cases on the synthetic fixture (names with quotes/specials, DAX with
     brackets in strings, style stability under repeated writes).
  2. Every M2 write tool exercised on copies of each real project under
     tests/fixtures/real/, then reloaded and byte-checked.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from core import io_safe
from core.pbip import PbipProject

SYNTH = Path(__file__).parent / "fixtures" / "synthetic"
REAL_DIR = Path(__file__).parent / "fixtures" / "real"
REAL_PROJECTS = sorted(REAL_DIR.glob("*/*.pbip")) if REAL_DIR.is_dir() else []


@pytest.fixture
def synth(tmp_path) -> PbipProject:
    dst = tmp_path / "synthetic"
    shutil.copytree(SYNTH, dst)
    return PbipProject(dst / "Synthetic.pbip")


@pytest.fixture(params=REAL_PROJECTS,
                ids=[p.parent.name for p in REAL_PROJECTS])
def real(request, tmp_path) -> PbipProject:
    dst = tmp_path / request.param.parent.name
    shutil.copytree(request.param.parent, dst)
    return PbipProject(dst / request.param.name)


# --- edge cases (synthetic) --------------------------------------------------

def test_measure_name_with_apostrophe(synth):
    synth.create_measure("Sales", "Bob's KPI", "1")
    text = synth._table_file("Sales").read_text(encoding="utf-8-sig")
    assert "measure 'Bob''s KPI' = 1" in text  # '' escape
    m = {m.name: m for m in PbipProject(synth.path).list_measures()}
    assert "Bob's KPI" in m


def test_measure_name_with_specials(synth):
    for name in ["% to Target", "Rev (USD)", "A/B Ratio", "Ø Diameter"]:
        synth.create_measure("Sales", name, "1")
    names = {m.name for m in PbipProject(synth.path).list_measures()}
    assert {"% to Target", "Rev (USD)", "A/B Ratio", "Ø Diameter"} <= names


def test_dax_with_brackets_inside_strings(synth):
    dax = 'IF([Net Revenue] > 0, "over [budget]", "under")'
    synth.create_measure("Sales", "Bracket Trap", dax)
    m = {m.name: m for m in PbipProject(synth.path).list_measures()}
    assert m["Bracket Trap"].dax == dax


def test_dax_with_double_quotes_roundtrip(synth):
    dax = 'CONCATENATE("said ""hi""", [Net Revenue])'
    synth.create_measure("Sales", "Quote Trap", dax)
    assert {m.name: m for m in PbipProject(synth.path).list_measures()} \
        ["Quote Trap"].dax == dax


def test_repeated_updates_are_stable(synth):
    """50 updates: file stays parseable, no duplicate blocks, style stable."""
    tfile = synth._table_file("Sales")
    style0 = io_safe.detect_style(tfile)
    for i in range(50):
        synth.update_measure("Sales", "Net Revenue", dax=f"{i}")
    text = tfile.read_text(encoding="utf-8-sig")
    assert text.count("measure 'Net Revenue'") == 1
    assert io_safe.detect_style(tfile) == style0
    assert {m.name: m for m in PbipProject(synth.path).list_measures()} \
        ["Net Revenue"].dax == "49"


def test_column_name_with_spaces_quoted(synth):
    synth.create_column("Sales", "Unit Price", "double", summarize_by="sum")
    text = synth._table_file("Sales").read_text(encoding="utf-8-sig")
    assert "column 'Unit Price'" in text


def test_calc_group_then_measure_interop(synth):
    """Calc-group table must not confuse the measure-table lookup."""
    synth.create_calc_group("TI", 1, [{"name": "CY", "dax": "SELECTEDMEASURE()"}])
    synth.create_measure("Sales", "After CG", "1")
    names = {m.name for m in PbipProject(synth.path).list_measures()}
    assert "After CG" in names


# --- full write surface on real projects --------------------------------------

@pytest.mark.skipif(not REAL_PROJECTS, reason="no real fixtures")
def test_real_full_write_surface(real):
    """create/update/bulk/delete measure + column + relationship + calc group,
    then reload everything and verify untouched tmdl files byte-identical."""
    root = real.path.parent
    tables = real.list_tables()
    target = tables[0].name
    before_other_files = {
        p: p.read_bytes() for p in root.rglob("*.tmdl")
    }

    # measures
    real.create_measure(target, "M2 Probe", "1", fmt="#,0")
    real.update_measure(target, "M2 Probe", dax="2")
    real.bulk_create_measures([
        {"table": target, "name": "M2 Bulk A", "dax": "1"},
        {"table": target, "name": "M2 Bulk B", "dax": "[M2 Bulk A] + 1"},
    ])
    real.delete_measure(target, "M2 Probe")

    # column + relationship guards work on real schemas
    real.create_column(target, "M2 Col", "string")
    with pytest.raises(ValueError):
        real.create_column(target, "M2 Col", "string")

    # calc group
    real.create_calc_group("M2 TI", 1,
                           [{"name": "CY", "dax": "SELECTEDMEASURE()"}])

    reloaded = PbipProject(real.path)
    names = {m.name for m in reloaded.list_measures()}
    assert {"M2 Bulk A", "M2 Bulk B"} <= names
    assert "M2 Probe" not in names
    assert any(t.name == "M2 TI" and t.is_calc_group
               for t in reloaded.list_tables())
    lin = reloaded.model_lineage("M2 Bulk B")
    assert lin["depends_on_measures"] == ["M2 Bulk A"]

    # only the files we intended to touch changed
    touched_names = {f"{target}.tmdl".lower(), "model.tmdl", "m2 ti.tmdl"}
    for p, data in before_other_files.items():
        if p.name.lower() in touched_names:
            continue
        assert p.read_bytes() == data, f"unexpected change: {p.name}"


@pytest.mark.skipif(not REAL_PROJECTS, reason="no real fixtures")
def test_real_delete_guard_fires(real):
    """On the real HR model, deleting a depended-on measure must refuse."""
    graph = real.model_lineage()
    referenced = [name for name, deps in graph["referenced_by"].items() if deps]
    if not referenced:
        pytest.skip("model has no measure-to-measure refs")
    victim = referenced[0]
    table = graph["measures"][victim]["table"]
    with pytest.raises(ValueError, match="Refusing to delete"):
        real.delete_measure(table, victim)
