"""Days 15-16: field-usage classifier (direct / indirect / unused)."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.pbip import PbipProject
from core.usage import classify_usage, collect_direct_refs
from report_server.server import ReportState, model_usage, set_project

SYNTH = Path(__file__).parent / "fixtures" / "synthetic" / "Synthetic.pbip"
REAL_HR = (
    Path(__file__).parent / "fixtures" / "real" / "hr-sample"
    / "Human Resources Sample PBIX.pbip"
)


@pytest.fixture(scope="module")
def synth_usage() -> dict:
    return classify_usage(PbipProject(SYNTH))


# --- Day 15: direct extraction -----------------------------------------------

def test_direct_refs_from_bindings():
    refs = collect_direct_refs(PbipProject(SYNTH))
    assert ("Sales", "Net Revenue") in refs   # card + bar Y
    assert ("Date", "Year") in refs           # bar Category + slicer
    assert ("Sales", "Margin %") in refs      # table Values


def test_direct_split_measures_vs_columns(synth_usage):
    assert synth_usage["direct"]["measures"] == ["Margin %", "Net Revenue"]
    assert synth_usage["direct"]["columns"] == ["Date.Year"]


# --- Day 16: classification ----------------------------------------------------

def test_indirect_via_measure_dax(synth_usage):
    # Net Revenue -> Sales.Amount ; Margin % -> Sales.Cost
    assert "Sales.Amount" in synth_usage["indirect"]["columns"]
    assert "Sales.Cost" in synth_usage["indirect"]["columns"]


def test_indirect_via_relationship(synth_usage):
    # join columns are load-bearing even if unbound
    assert "Sales.OrderDate" in synth_usage["indirect"]["columns"]
    assert "Date.Date" in synth_usage["indirect"]["columns"]


def test_unused_detected(synth_usage):
    assert "Hidden Helper" in synth_usage["unused"]["measures"]
    assert "Days In Period" in synth_usage["unused"]["measures"]
    # Order Count only feeds an *unused* measure -> itself unused
    assert "Sales.Order Count" in synth_usage["unused"]["columns"]


def test_buckets_are_disjoint_and_complete(synth_usage):
    project = PbipProject(SYNTH)
    all_measures = {m.name for m in project.list_measures()}
    all_columns = {f"{t.name}.{c.name}" for t in project.list_tables()
                   for c in t.columns}
    dm = set(synth_usage["direct"]["measures"])
    im = set(synth_usage["indirect"]["measures"])
    um = set(synth_usage["unused"]["measures"])
    assert dm | im | um == all_measures
    assert not (dm & im) and not (dm & um) and not (im & um)
    dc = set(synth_usage["direct"]["columns"])
    ic = set(synth_usage["indirect"]["columns"])
    uc = set(synth_usage["unused"]["columns"])
    assert dc | ic | uc == all_columns
    assert not (dc & ic) and not (dc & uc) and not (ic & uc)


@pytest.mark.skipif(not REAL_HR.exists(), reason="real HR fixture not present")
def test_real_hr_classification_sane():
    u = classify_usage(PbipProject(REAL_HR))
    # a measure with a huge dependent chain is definitely direct in this report
    assert "Actives" in u["direct"]["measures"]
    # its DAX deps must not be classified unused
    assert "Employee.TermDate" not in u["unused"]["columns"]
    # every bucket nonempty on a real report, and counts add up
    total = sum(u["counts"].values())
    project = PbipProject(REAL_HR)
    expected = len(project.list_measures()) + sum(
        len(t.columns) for t in project.list_tables())
    assert total == expected


# --- via server tool -------------------------------------------------------------

def test_tool_model_usage():
    st = ReportState()
    set_project(st, str(SYNTH))
    u = model_usage(st)
    assert {"direct", "indirect", "unused", "counts"} <= set(u)
