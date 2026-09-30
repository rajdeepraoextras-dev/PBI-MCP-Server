"""core/journal: dry-run previews, byte-exact undo, transactions."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from core import journal
from core.journal import Journal, delta, snapshot
from model_server.server import ModelState, create_measure, set_project
from report_server.server import ReportState, create_page
from report_server.server import set_project as set_report_project

SYNTH = Path(__file__).parent / "fixtures" / "synthetic"


@pytest.fixture
def project(tmp_path):
    dst = tmp_path / "proj"
    shutil.copytree(SYNTH, dst)
    return dst


def _model_state(root: Path) -> ModelState:
    state = ModelState()
    set_project(state, str(root / "Synthetic.pbip"))
    return state


def test_snapshot_skips_caches_and_backups(project):
    (project / "Synthetic.Report" / ".pbi").mkdir(exist_ok=True)
    (project / "Synthetic.Report" / ".pbi" / "cache.bin").write_bytes(b"x")
    (project / "Synthetic.pbip.bak-20260101-000000").write_bytes(b"y")
    snap = snapshot(project)
    assert "Synthetic.pbip" in snap
    assert not any(".pbi/" in k or ".bak-" in k for k in snap)


def test_dry_run_reports_diff_and_touches_nothing(project):
    state = _model_state(project)
    before = snapshot(project)
    out = journal.dry_run(
        state, lambda: create_measure(state, "Sales", "Dry Measure", "1 + 1"))
    assert out["dry_run"] is True
    assert out["result"]["ok"] is True
    assert "Synthetic.SemanticModel/definition/tables/Sales.tmdl" in out["changes"]["modified"]
    assert "+" in out["diff"] and "Dry Measure" in out["diff"]
    assert snapshot(project) == before                # disk untouched
    assert state.project.path == project / "Synthetic.pbip"  # state restored
    assert not (project / journal.STORE_DIR).exists()  # not journaled


def test_record_then_undo_restores_bytes_exactly(project):
    sales = project / "Synthetic.SemanticModel" / "definition" / "tables" / "Sales.tmdl"
    # give the file a CRLF + BOM style so restoration must be byte-exact
    sales.write_bytes(b"\xef\xbb\xbf" + sales.read_bytes().replace(b"\n", b"\r\n"))
    original = sales.read_bytes()
    state = _model_state(project)
    j = journal.for_state(state)
    j.record("pbi_create_measure",
             lambda: create_measure(state, "Sales", "Undo Me", "2"))
    assert sales.read_bytes() != original
    assert len(j.history()) == 1 and j.history()[0]["tool"] == "pbi_create_measure"
    out = j.undo()
    assert [u["tool"] for u in out["undone"]] == ["pbi_create_measure"]
    assert sales.read_bytes() == original
    assert j.history() == [] and out["remaining"] == 0


def test_undo_removes_added_files_and_empty_dirs(project):
    state = ReportState()
    set_report_project(state, str(project / "Synthetic.pbip"))
    j = journal.for_state(state)
    before = snapshot(project)
    j.record("pbi_create_page", lambda: create_page(state, "Undo Page"))
    assert delta(before, snapshot(project))["added"]
    j.undo()
    assert snapshot(project) == before
    assert not any(p.name.startswith("undo") and p.is_dir()
                   for p in (project / "Synthetic.Report" / "definition" / "pages").iterdir())


def test_undo_on_empty_history_is_a_noop(project):
    state = _model_state(project)
    assert journal.for_state(state).undo() == {"undone": [], "remaining": 0}


def test_transaction_rollback_and_commit(project):
    state = _model_state(project)
    j = journal.for_state(state)
    before = snapshot(project)
    j.begin()
    with pytest.raises(ValueError):
        j.begin()
    j.record("a", lambda: create_measure(state, "Sales", "Tx A", "1"))
    j.record("b", lambda: create_measure(state, "Sales", "Tx B", "2"))
    out = j.rollback()
    assert [u["tool"] for u in out["undone"]] == ["b", "a"]
    assert snapshot(project) == before and j.in_transaction() is None

    j.begin()
    j.record("c", lambda: create_measure(state, "Sales", "Tx C", "3"))
    assert j.commit() == {"ok": True, "writes_kept": 1}
    assert len(j.history()) == 1
    with pytest.raises(ValueError):
        j.commit()


def test_history_is_capped(project, monkeypatch):
    monkeypatch.setattr(journal, "MAX_ENTRIES", 3)
    state = _model_state(project)
    j = journal.for_state(state)
    for i in range(5):
        j.record(f"t{i}", lambda i=i: create_measure(state, "Sales", f"Cap {i}", "1"))
    assert [h["tool"] for h in j.history()] == ["t4", "t3", "t2"]


def test_record_without_changes_adds_no_entry(project):
    state = _model_state(project)
    j = journal.for_state(state)
    assert j.record("noop", lambda: 42) == 42
    assert j.history() == []


def test_project_root_for_all_input_forms(project):
    for p in (project, project / "Synthetic.pbip", project / "Synthetic.Report",
              project / "Synthetic.SemanticModel"):
        state = ModelState()
        set_project(state, str(p))
        assert journal.project_root(state.project) == project


# --- regressions found while documenting the foundation ----------------------

def test_rollback_survives_history_cap(project, monkeypatch):
    """The transaction mark is an entry id, not a count, and pruning is
    suspended while a transaction is open."""
    monkeypatch.setattr(journal, "MAX_ENTRIES", 3)
    state = _model_state(project)
    j = journal.for_state(state)
    before = snapshot(project)
    j.begin()
    for i in range(5):
        j.record(f"t{i}", lambda i=i: create_measure(state, "Sales", f"Tx {i}", "1"))
    out = j.rollback()
    assert len(out["undone"]) == 5
    assert snapshot(project) == before
    # pruning resumes after the transaction closes
    for i in range(5):
        j.record(f"u{i}", lambda i=i: create_measure(state, "Sales", f"After {i}", "1"))
    assert len(j.history(50)) == 3


def test_commit_after_history_cap_reports_all_kept_writes(project, monkeypatch):
    monkeypatch.setattr(journal, "MAX_ENTRIES", 2)
    state = _model_state(project)
    j = journal.for_state(state)
    j.begin()
    for i in range(4):
        j.record(f"t{i}", lambda i=i: create_measure(state, "Sales", f"Keep {i}", "1"))
    assert j.commit() == {"ok": True, "writes_kept": 4}
    assert len(j.history(50)) == 2


def test_undo_removes_backups_and_dirs_left_by_the_operation(project):
    state = ReportState()
    set_report_project(state, str(project / "Synthetic.pbip"))
    j = journal.for_state(state)
    before = snapshot(project)
    baks_before = journal.backup_files(project)

    def op():
        create_page(state, "Phantom")
        # a second write to the new page makes the project take a safety copy
        pages = project / "Synthetic.Report" / "definition" / "pages"
        newest = next(p for p in pages.iterdir()
                      if p.is_dir() and not (SYNTH / "Synthetic.Report" / "definition" / "pages" / p.name).exists())
        page_json = newest / "page.json"
        state.project._backed_up.discard(page_json)
        state.project._write_text(page_json, page_json.read_text(encoding="utf-8"))
        return newest.name

    name = j.record("pbi_create_page", op)
    assert any(name in b for b in journal.backup_files(project) - baks_before)
    j.undo()
    assert snapshot(project) == before
    assert journal.backup_files(project) == baks_before
    assert not (project / "Synthetic.Report" / "definition" / "pages" / name).exists()


def test_trash_is_journaled_so_delete_and_restore_undo_cleanly(project):
    from report_server.server import STATE, mcp  # noqa: F401 (server import ok in tests)
    state = ReportState()
    set_report_project(state, str(project / "Synthetic.pbip"))
    j = journal.for_state(state)
    proj = state.project
    page = next(p for p in proj.list_pages() if p.visual_count)
    vid = proj.list_visuals(page.id)[0].id
    before = snapshot(project)

    j.record("pbi_delete_visual", lambda: proj.delete_visual(page.id, vid))
    trashed = [k for k in snapshot(project) if ".pbi/mcp-trash" in k]
    assert trashed, "trash contents must be part of the snapshot"
    j.undo()
    assert snapshot(project) == before                     # visual back, trash entry gone

    j.record("pbi_delete_visual", lambda: proj.delete_visual(page.id, vid))
    after_delete = snapshot(project)
    trash_path = proj.list_trash()[0]["path"]
    j.record("pbi_restore_visual", lambda: proj.restore_visual(trash_path))
    assert snapshot(project) != after_delete
    j.undo()
    assert snapshot(project) == after_delete               # trash entry is back, not lost
