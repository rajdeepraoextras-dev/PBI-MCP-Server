"""E1: pre-flight schema validation against vendored Fabric schemas."""

from __future__ import annotations

import glob
import json
import shutil
from pathlib import Path

import pytest

from core import schema_validate as sv
from core.pbip import PbipProject
from core.pbir import build_page_json, build_visual_json

SYNTH = Path(__file__).parent / "fixtures" / "synthetic"
REAL = Path(__file__).parent / "fixtures" / "real"

pytestmark = pytest.mark.skipif(not sv.is_available(),
                                reason="jsonschema/schemas unavailable")


def _ism(e, p):
    return p in ("Net Revenue", "Margin %")


# --- emitted output validates -----------------------------------------------

def test_emitted_card_valid():
    v = build_visual_json("c", "card", {"Values": ["S.M"]}, _ism, title="X")
    assert sv.validate("visualContainer", v) == []


def test_emitted_bar_valid():
    v = build_visual_json("b", "clusteredBarChart",
                          {"Category": ["D.Year"], "Y": ["S.M"]}, _ism)
    assert sv.validate("visualContainer", v) == []


def test_emitted_page_valid():
    assert sv.validate("page", build_page_json("p", "P", 1280, 720)) == []


# --- catches the historical bugs --------------------------------------------

def test_catches_bad_topn_top_condition():
    bad = build_visual_json("b", "clusteredBarChart",
                            {"Category": ["D.Y"], "Y": ["S.M"]}, _ism)
    bad["filterConfig"] = {"filters": [{
        "name": "f", "type": "TopN",
        "filter": {"Version": 2, "From": [{"Name": "s", "Entity": "S", "Type": 0}],
                   "Where": [{"Condition": {"Top": {"Count": 5}}}]}}]}
    assert sv.validate("visualContainer", bad), "should reject the old Top shape"


def test_visualtopn_accepted_as_drift():
    from core.formatting import build_filter

    ok = build_visual_json("b", "clusteredBarChart",
                           {"Category": ["D.Y"], "Y": ["S.M"]}, _ism)
    ok["filterConfig"] = {"filters": [
        build_filter("D.Y", filter_type="TopN", top_n=5)]}
    # the VisualTopN condition is tolerated as documented version drift
    assert sv.validate("visualContainer", ok) == []


# --- real Desktop files pass -------------------------------------------------

@pytest.mark.skipif(not REAL.is_dir(), reason="no real fixtures")
def test_real_files_validate():
    kinds = {"report.json": "report", "page.json": "page",
             "pages.json": "pagesMetadata", "visual.json": "visualContainer"}
    failures = []
    for f in glob.glob(str(REAL / "**" / "*.json"), recursive=True):
        name = Path(f).name
        if name not in kinds:
            continue
        errs = sv.validate(kinds[name], json.loads(
            Path(f).read_text(encoding="utf-8-sig")))
        if errs:
            failures.append((f, errs[0]))
    assert not failures, f"{len(failures)} real files failed: {failures[:3]}"


# --- pre-flight blocks bad writes -------------------------------------------

def test_preflight_blocks_bad_write(tmp_path):
    dst = tmp_path / "s"
    shutil.copytree(SYNTH, dst)
    p = PbipProject(dst / "Synthetic.pbip")
    page = p.create_page("QA")
    vfile = (p._require_report() / "definition" / "pages" / page
             / "visuals" / "bad" / "visual.json")
    # a structurally invalid visual (position wrong type) must be refused
    with pytest.raises(ValueError, match="Schema validation failed"):
        p._write_json(vfile, {"$schema": "x", "name": "bad",
                              "position": "not-an-object",
                              "visual": {"visualType": "card"}})


def test_validate_project_clean(tmp_path):
    dst = tmp_path / "s"
    shutil.copytree(SYNTH, dst)
    p = PbipProject(dst / "Synthetic.pbip")
    p.create_page("QA")
    report = p.validate_project()
    assert report["ok"], report["errors"]
    assert report["checked"] > 0
