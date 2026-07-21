"""Days 31-33: M6 safety layer.

Day 31 — report-aware deletion fail-safes.
Day 32 — backup listing / restore / dry-run.
Day 33 — fuzz: a seeded random op storm must never leave the project
unparseable (the automatable proxy for "Desktop never refuses to open").
"""

from __future__ import annotations

import random
import shutil
import string
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


# --- Day 31: report-aware delete guards ---------------------------------------

def test_delete_directly_bound_measure_refused(project):
    # 'Net Revenue' has measure dependents AND is bound in visuals; the
    # measure-lineage guard fires first. 'Margin %' has NO measure
    # dependents but IS bound in table1 -> the report guard must catch it.
    with pytest.raises(ValueError, match="bound directly"):
        project.delete_measure("Sales", "Margin %")


def test_delete_indirectly_needed_measure_refused(project):
    """A helper only a displayed measure depends on is still protected.

    The measure-lineage guard fires first here (Helper has a dependent);
    the report guard is the second net behind it. Either way: refused.
    """
    project.upsert_measure("Sales", "Helper", "SUM(Sales[Amount])")
    project.upsert_measure("Sales", "Shown", "[Helper] * 2")
    project.update_bindings("overview", "card1", {"Values": ["Sales.Shown"]})
    with pytest.raises(ValueError, match="Refusing to delete"):
        project.delete_measure("Sales", "Helper")


def test_force_still_overrides_report_guard(project):
    res = project.delete_measure("Sales", "Margin %", force=True)
    assert res["action"] == "deleted"


def test_unbound_measure_still_deletable(project):
    res = project.delete_measure("Sales", "Hidden Helper")
    assert res["action"] == "deleted"


# --- Day 32: backups & dry-run ---------------------------------------------------

def test_list_backups_after_mutations(project):
    assert project.list_backups() == []
    project.create_measure("Sales", "B1", "1")
    project.create_measure("Date", "B2", "2")
    baks = project.list_backups()
    assert len(baks) == 2
    names = {Path(b["original"]).name for b in baks}
    assert names == {"Sales.tmdl", "Date.tmdl"}


def test_restore_backup_roundtrip(project):
    before = project._table_file("Sales").read_text(encoding="utf-8-sig")
    project.create_measure("Sales", "Junk", "1")
    bak = project.list_backups()[0]
    res = project.restore_backup(bak["backup"])
    assert res["ok"]
    after = project._table_file("Sales").read_text(encoding="utf-8-sig")
    assert after == before
    assert "Junk" not in {m.name for m in
                          PbipProject(project.path).list_measures()}


def test_restore_foreign_file_rejected(project, tmp_path):
    stray = tmp_path / "x.tmdl.bak-20260101-000000"
    stray.write_text("nope", encoding="utf-8")
    with pytest.raises(ValueError, match="not a backup of this project"):
        project.restore_backup(str(stray))


def test_delete_dry_run_writes_nothing(project):
    before = project._table_file("Sales").read_bytes()
    res = project.delete_measure("Sales", "Hidden Helper", dry_run=True)
    assert res["action"] == "dry_run"
    assert res["would_delete"] == "Hidden Helper"
    assert project._table_file("Sales").read_bytes() == before
    assert project.list_backups() == []  # not even a backup


# --- Day 33: fuzz storm ------------------------------------------------------------

def _rand_name(rng) -> str:
    alphabet = string.ascii_letters + " %()'-/#" + "0123456789"
    return ("F " + "".join(rng.choice(alphabet) for _ in range(rng.randint(3, 18)))).strip()


@pytest.mark.parametrize("seed", [1, 7, 42])
def test_fuzz_storm_never_corrupts(project, seed):
    rng = random.Random(seed)
    tfile = project._table_file("Sales")
    style0 = io_safe.detect_style(tfile)
    created: list[str] = []

    for _ in range(60):
        op = rng.randrange(6)
        try:
            if op == 0:
                name = _rand_name(rng)
                dax = rng.choice([
                    "1", "SUM(Sales[Amount])",
                    'IF([Net Revenue] > 0, "hi [trap]", "lo")',
                    "VAR _x = 1\nRETURN\n    _x + " + str(rng.randint(0, 99)),
                ])
                project.create_measure("Sales", name, dax,
                                       fmt=rng.choice([None, "#,0", "0.0%"]))
                created.append(name)
            elif op == 1 and created:
                project.update_measure("Sales", rng.choice(created),
                                       dax=str(rng.randint(0, 999)))
            elif op == 2 and created:
                victim = rng.choice(created)
                project.delete_measure("Sales", victim, force=True)
                created.remove(victim)
            elif op == 3:
                project.create_column("Sales", _rand_name(rng), "string")
            elif op == 4:
                project.upsert_measure("Sales", _rand_name(rng), "BLANK()")
            elif op == 5 and created:
                project.create_measure(
                    "Sales", _rand_name(rng),
                    f"[{rng.choice(created)}] + 1")
        except (ValueError, KeyError):
            pass  # duplicate names etc. — guards working is fine

    # THE BAR: everything still parses, style intact, model coherent
    reloaded = PbipProject(project.path)
    tables = reloaded.list_tables()
    assert {t.name for t in tables} >= {"Sales", "Date"}
    measures = reloaded.list_measures()
    assert len({m.name for m in measures}) == len(measures)  # no dupes
    reloaded.model_lineage()          # graph builds
    for pg in reloaded.list_pages():  # report untouched and readable
        reloaded.list_visuals(pg.id)
    assert io_safe.detect_style(tfile) == style0
    text = tfile.read_text(encoding="utf-8-sig")
    assert text.startswith("table Sales")
    assert "partition Sales = m" in text  # partition survived the storm
