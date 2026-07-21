"""E1 D3-D5: relativeDate filters, parse cache, capabilities."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from core import schema_validate as sv
from core.capabilities import capabilities
from core.formatting import build_filter
from core.pbip import PbipProject
from core.pbir import build_visual_json

SYNTH = Path(__file__).parent / "fixtures" / "synthetic"


# --- D3: relativeDate --------------------------------------------------------

@pytest.mark.parametrize("unit", ["day", "week", "month", "year"])
def test_relative_date_validates(unit):
    f = build_filter("Date.Date", filter_type="RelativeDate",
                     last_n=30, relative_unit=unit)
    v = build_visual_json("b", "clusteredBarChart",
                          {"Category": ["D.Y"], "Y": ["S.M"]}, lambda e, p: False)
    v["filterConfig"] = {"filters": [f]}
    if sv.is_available():
        assert sv.validate("visualContainer", v) == []
    cond = f["filter"]["Where"][0]["Condition"]["Comparison"]
    assert cond["ComparisonKind"] == 2  # >=
    assert cond["Right"]["DateAdd"]["Amount"] == -30


def test_relative_date_needs_args():
    with pytest.raises(ValueError, match="RelativeDate needs"):
        build_filter("Date.Date", filter_type="RelativeDate")


# --- D4: parse cache ---------------------------------------------------------

def test_cache_reuses_parse(tmp_path):
    dst = tmp_path / "s"
    shutil.copytree(SYNTH, dst)
    p = PbipProject(dst / "Synthetic.pbip")
    a = p.list_tables()
    b = p.list_tables()
    # same object instances returned from cache (not reparsed)
    assert a[0] is b[0]


def test_cache_invalidates_on_write(tmp_path):
    dst = tmp_path / "s"
    shutil.copytree(SYNTH, dst)
    p = PbipProject(dst / "Synthetic.pbip")
    assert "Cached M" not in {m.name for m in p.list_measures()}
    p.create_measure("Sales", "Cached M", "1")
    # cache must reflect the write immediately (mtime changed)
    assert "Cached M" in {m.name for m in p.list_measures()}


# --- D5: capabilities --------------------------------------------------------

def test_capabilities_shape():
    c = capabilities()
    assert "clusteredBarChart" in c["visual_types"]
    assert c["visual_types"]["clusteredBarChart"]["required_buckets"] == \
        ["Category", "Y"]
    assert "RelativeDate" in c["filters"]["types"]
    assert c["example"]["tool"] == "pbi_build_page"


def test_capabilities_matches_specs():
    from core.visual_specs import VISUAL_SPECS

    c = capabilities()
    assert set(c["visual_types"]) == set(VISUAL_SPECS)
