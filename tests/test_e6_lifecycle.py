"""E6 D28-29: page CRUD, filter list/remove, trash restore, project diff."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from core.diff import diff_projects
from core.formatting import build_filter
from core.pbip import PbipProject

SYNTH = Path(__file__).parent / "fixtures" / "synthetic"


@pytest.fixture
def project(tmp_path) -> PbipProject:
    dst = tmp_path / "s"
    shutil.copytree(SYNTH, dst)
    return PbipProject(dst / "Synthetic.pbip")


# --- page CRUD ---------------------------------------------------------------

def test_rename_page(project):
    project.rename_page("overview", "Home")
    names = {p.id: p.name for p in PbipProject(project.path).list_pages()}
    assert names["overview"] == "Home"


def test_hide_page(project):
    project.hide_page("overview", True)
    pages = {p.id: p for p in PbipProject(project.path).list_pages()}
    assert pages["overview"].is_hidden


def test_reorder_pages(project):
    project.reorder_pages(["details", "overview"])
    order = [p.id for p in PbipProject(project.path).list_pages()]
    assert order[:2] == ["details", "overview"]


def test_delete_page_recoverable(project):
    out = project.delete_page("details")
    assert "details" not in {p.id for p in PbipProject(project.path).list_pages()}
    assert (Path(out["recoverable_at"]) / "page.json").exists()


def test_duplicate_page(project):
    nid = project.duplicate_page("overview", "Overview Copy")
    reloaded = PbipProject(project.path)
    pages = {p.id: p for p in reloaded.list_pages()}
    assert nid in pages and pages[nid].name == "Overview Copy"
    # visuals copied too
    assert reloaded.list_visuals(nid)


def test_reorder_unknown_page(project):
    with pytest.raises(KeyError):
        project.reorder_pages(["ghost"])


# --- filters -----------------------------------------------------------------

def test_list_and_remove_filter(project):
    project.add_filter("report", build_filter("Date.Year", values=[2024],
                                              name="F1"))
    project.add_filter("report", build_filter("Date.Year", values=[2025],
                                              name="F2"))
    listed = project.list_filters("report")
    assert {f["name"] for f in listed} == {"F1", "F2"}
    project.remove_filter("report", "F1")
    assert {f["name"] for f in project.list_filters("report")} == {"F2"}


def test_remove_missing_filter(project):
    with pytest.raises(KeyError):
        project.remove_filter("report", "nope")


# --- trash / restore ---------------------------------------------------------

def test_delete_then_restore_visual(project):
    out = project.delete_visual("overview", "card1")
    assert "card1" not in {v.id for v in
                           PbipProject(project.path).list_visuals("overview")}
    trash = project.list_trash()
    assert any(t["kind"] == "visual" for t in trash)
    project.restore_visual(out["recoverable_at"])
    assert "card1" in {v.id for v in
                       PbipProject(project.path).list_visuals("overview")}


# --- diff --------------------------------------------------------------------

def test_diff_identical(project, tmp_path):
    other = tmp_path / "s2"
    shutil.copytree(SYNTH, other)
    d = diff_projects(project, PbipProject(other / "Synthetic.pbip"))
    assert d["identical"] and d["total_changes"] == 0


def test_diff_detects_changes(project, tmp_path):
    other = tmp_path / "s2"
    shutil.copytree(SYNTH, other)
    p2 = PbipProject(other / "Synthetic.pbip")
    p2.create_measure("Sales", "New M", "1")
    p2.update_measure("Sales", "Net Revenue", dax="999")
    p2.create_page("Extra")
    d = diff_projects(project, p2)
    assert "Sales.New M" in d["measures"]["added"]
    assert "Sales.Net Revenue" in d["measures"]["changed"]
    assert "extra" in d["pages"]["added"]
    assert not d["identical"]
