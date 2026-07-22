"""New pages inherit the report's page size + theme accent (match existing)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from core.pbip import PbipProject
from core.theme import generate_theme
from report_server.server import (
    ReportState, build_designed_page, set_project,
)

SYNTH = Path(__file__).parent / "fixtures" / "synthetic"


@pytest.fixture
def project(tmp_path) -> PbipProject:
    dst = tmp_path / "s"
    shutil.copytree(SYNTH, dst)
    return PbipProject(dst / "Synthetic.pbip")


# --- page size inheritance ---------------------------------------------------

def test_default_page_size_from_existing(project):
    # synthetic pages are 1280x720
    assert project.default_page_size() == (1280.0, 720.0)


def test_new_page_matches_widescreen(project):
    # make the existing pages a custom size, then a new page should match width
    for pid in ("overview", "details"):
        import json
        pj = (project._require_report() / "definition" / "pages" / pid
              / "page.json")
        d = json.loads(pj.read_text(encoding="utf-8-sig"))
        d["width"], d["height"] = 1920, 1080
        project._write_json(pj, d)
    new_id = project.create_page("Added")
    import json
    nd = json.loads((project._require_report() / "definition" / "pages"
                     / new_id / "page.json").read_text(encoding="utf-8-sig"))
    assert nd["width"] == 1920 and nd["height"] == 1080


def test_explicit_size_still_wins(project):
    new_id = project.create_page("Custom", width=800, height=600)
    import json
    nd = json.loads((project._require_report() / "definition" / "pages"
                     / new_id / "page.json").read_text(encoding="utf-8-sig"))
    assert nd["width"] == 800


# --- theme accent inheritance ------------------------------------------------

def test_report_accent_from_installed_theme(project):
    project.set_report_theme(generate_theme("#0B7A75", name="Teal"))
    assert project.report_accent() == "#0B7A75"


def test_report_accent_none_without_theme(project):
    assert project.report_accent() is None


def test_designed_page_inherits_theme_accent(project):
    project.set_report_theme(generate_theme("#8E44AD", name="Purple"))
    st = ReportState()
    set_project(st, str(project.path))
    res = build_designed_page(st, "Inherited", "Title",
                              kpis=[{"measure": "Sales.Net Revenue"}],
                              charts=[])  # no accent passed
    # the header band shape should use the inherited accent, not the default
    reloaded = PbipProject(project.path)
    shapes = [v for v in reloaded.list_visuals(res["page_id"])
              if v.visual_type == "shape"]
    fills = [s.raw["visual"]["objects"]["fill"][0]["properties"].get("fillColor")
             for s in shapes if "fill" in s.raw["visual"]["objects"]]
    hexes = [f["solid"]["color"]["expr"]["Literal"]["Value"]
             for f in fills if f and "solid" in f]
    assert any("8E44AD" in h for h in hexes), hexes
