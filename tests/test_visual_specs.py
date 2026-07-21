"""Day 18: visual_specs — bucket lookup + binding validation.

The killer test: every visual in every real project on disk must pass
validate_bindings for its own (type, buckets) — i.e. the spec table agrees
with what Desktop actually emits.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.pbip import PbipProject
from core.pbir import visual_bindings
from core.visual_specs import VISUAL_SPECS, buckets_for, validate_bindings

_CANDIDATES = list(
    (Path(__file__).parent / "fixtures" / "real").glob("*/*.pbip")
) + [
    Path(r"C:\Users\resod\OneDrive\Desktop\pbiiiiiiiiii\Corporate Spend.pbip"),
    Path(r"C:\Users\resod\OneDrive\Desktop\finaltest\Zomato Dashboard - Rajdeep Rao.pbip"),
]
PROJECTS = [p for p in _CANDIDATES if p.exists()]


# --- lookup ---------------------------------------------------------------

def test_known_type():
    spec = buckets_for("clusteredBarChart")
    assert spec["required"] == ["Category", "Y"]
    assert "Series" in spec["optional"]


def test_unknown_type_lists_known():
    with pytest.raises(KeyError, match="clusteredBarChart"):
        buckets_for("hologramChart")


def test_combo_uses_y2_not_liney():
    spec = buckets_for("lineClusteredColumnComboChart")
    assert "Y2" in spec["optional"]
    assert "LineY" not in spec["optional"] + spec["required"]


def test_donut_has_no_category():
    spec = buckets_for("donutChart")
    assert "Category" not in spec["required"] + spec["optional"]


# --- validation --------------------------------------------------------------

def test_valid_bindings_pass():
    validate_bindings("card", {"Values": ["Sales.Net Revenue"]})
    validate_bindings("clusteredBarChart",
                      {"Category": ["Date.Year"], "Y": ["Sales.Net Revenue"],
                       "Series": ["Store.Region"]})


def test_missing_required_rejected():
    with pytest.raises(ValueError, match="missing required"):
        validate_bindings("clusteredBarChart", {"Y": ["Sales.Net Revenue"]})


def test_empty_required_rejected():
    with pytest.raises(ValueError, match="missing required"):
        validate_bindings("card", {"Values": []})


def test_unknown_bucket_rejected():
    with pytest.raises(ValueError, match="unknown bucket"):
        validate_bindings("card", {"Values": ["X.Y"], "Fields": ["X.Z"]})


# --- spec agrees with real Desktop output ------------------------------------

@pytest.mark.skipif(not PROJECTS, reason="no real projects on disk")
@pytest.mark.parametrize("pbip", PROJECTS, ids=[p.stem[:20] for p in PROJECTS])
def test_spec_matches_every_real_visual(pbip):
    project = PbipProject(pbip)
    checked = 0
    for page in project.list_pages():
        for v in project.list_visuals(page.id):
            if v.visual_type not in VISUAL_SPECS:
                continue  # third-party/unmodeled types are out of scope
            binds = visual_bindings(v)
            if not binds:
                continue  # some visuals carry no queryState (e.g. static)
            validate_bindings(v.visual_type, binds)  # must not raise
            checked += 1
    assert checked > 0
