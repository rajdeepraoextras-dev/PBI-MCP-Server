"""E5 D24-27: model profiling + scaffold_report."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from core.pbip import PbipProject
from core.profile import classify_column, classify_measure, profile_model
from core.scaffold import propose_report
from report_server.server import ReportState, scaffold_report, set_project

SYNTH = Path(__file__).parent / "fixtures" / "synthetic"
REAL_HR = (Path(__file__).parent / "fixtures" / "real" / "hr-sample"
           / "Human Resources Sample PBIX.pbip")


@pytest.fixture
def state(tmp_path) -> ReportState:
    dst = tmp_path / "s"
    shutil.copytree(SYNTH, dst)
    st = ReportState()
    set_project(st, str(dst / "Synthetic.pbip"))
    return st


# --- D24 profiling -----------------------------------------------------------

def test_profile_synthetic():
    p = PbipProject(SYNTH / "Synthetic.pbip")
    prof = profile_model(p)
    assert "Sales" in prof["fact_tables"]
    assert "Date" in prof["date_tables"]
    assert prof["summary"]["measures"] > 0


def test_measure_classification():
    class M:
        def __init__(self, name, fmt, dax=""):
            self.name, self.format_string, self.dax = name, fmt, dax
    assert classify_measure(M("Margin %", "0.0%")) == "ratio"
    assert classify_measure(M("Revenue", "$#,0")) == "currency"
    assert classify_measure(M("Sales YoY", "#,0")) == "time_intelligence"
    assert classify_measure(M("Count", "#,0")) == "base"


def test_column_classification():
    class C:
        def __init__(self, dt, cat=None, key=False):
            self.data_type, self.data_category, self.is_key = dt, cat, key
    assert classify_column(C("dateTime")) == "date"
    assert classify_column(C("string", "City")) == "geography"
    assert classify_column(C("string")) == "category"
    assert classify_column(C("double")) == "numeric"


@pytest.mark.skipif(not REAL_HR.exists(), reason="no real HR fixture")
def test_profile_real_hr():
    prof = profile_model(PbipProject(REAL_HR))
    assert prof["fact_tables"] == ["Employee"]
    assert "Date" in prof["date_tables"]
    assert prof["summary"]["time_intel_measures"] > 5
    # auto date tables excluded
    assert not any(t.startswith("LocalDateTable") for t in prof["tables"])


# --- D25-26 proposal ---------------------------------------------------------

def test_propose_report_structure():
    prof = profile_model(PbipProject(SYNTH / "Synthetic.pbip"))
    proposal = propose_report(prof)
    assert proposal["pages"][0]["name"] == "Overview"
    assert proposal["pages"][0]["kpis"]
    # overview has a chart bound to real fields
    charts = proposal["pages"][0]["charts"]
    assert charts and "bindings" in charts[0]


def test_scaffold_dry_run_builds_nothing(state):
    res = scaffold_report(state, dry_run=True)
    assert res["dry_run"] and "proposal" in res
    # no pages created beyond the originals
    assert {p.id for p in PbipProject(state.project.path).list_pages()} == \
        {"overview", "details"}


def test_scaffold_builds_designed_report(state):
    res = scaffold_report(state, accent="#0B7A75")
    assert len(res["pages"]) >= 1
    project = PbipProject(state.project.path)
    overview = res["pages"][0]
    types = {v.visual_type for v in project.list_visuals(overview)}
    assert "card" in types           # KPI strip
    assert "shape" in types          # header band / backplates
    assert "textbox" in types        # title
    # whole report is schema-valid
    assert project.validate_project()["ok"]


@pytest.mark.skipif(not REAL_HR.exists(), reason="no real HR fixture")
def test_scaffold_real_hr_multipage(tmp_path):
    dst = tmp_path / "hr"
    shutil.copytree(REAL_HR.parent, dst)
    st = ReportState()
    set_project(st, str(next(dst.glob("*.pbip"))))
    res = scaffold_report(st, accent="#1F3A5F")
    assert len(res["pages"]) >= 2    # overview + detail pages
    project = PbipProject(st.project.path)
    # nav bar present (actionButtons on the overview)
    types = {v.visual_type for v in project.list_visuals(res["pages"][0])}
    assert "actionButton" in types
    assert project.validate_project()["ok"]
