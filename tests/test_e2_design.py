"""E2 D6-11: design layer — text, image, shape, page chrome, groups, raw."""

from __future__ import annotations

import base64
import shutil
from pathlib import Path

import pytest

from core import schema_validate as sv
from core.pbip import PbipProject

SYNTH = Path(__file__).parent / "fixtures" / "synthetic"

# a 1x1 transparent PNG
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


@pytest.fixture
def project(tmp_path) -> PbipProject:
    dst = tmp_path / "s"
    shutil.copytree(SYNTH, dst)
    return PbipProject(dst / "Synthetic.pbip")


def _reload_raw(p, page, vid):
    return PbipProject(p.path).get_visual(page, vid).raw


# --- D6 textbox --------------------------------------------------------------

def test_add_text_simple(project):
    vid = project.add_text("overview", "Hello", z=5000)
    raw = _reload_raw(project, "overview", vid)
    assert raw["visual"]["visualType"] == "textbox"
    runs = raw["visual"]["objects"]["general"][0]["properties"]["paragraphs"][0]["textRuns"]
    assert runs[0]["value"] == "Hello"
    assert raw["position"]["z"] == 5000


def test_add_text_rich_runs(project):
    vid = project.add_text("overview", [
        {"text": "Big ", "bold": True, "size": 20, "color": "#1F3A5F"},
        {"text": "small", "size": 10, "align": "center"},
    ])
    raw = _reload_raw(project, "overview", vid)
    runs = raw["visual"]["objects"]["general"][0]["properties"]["paragraphs"][0]["textRuns"]
    assert len(runs) == 2
    assert runs[0]["textStyle"]["fontSize"] == "20pt"
    assert runs[0]["textStyle"]["color"] == "#1F3A5F"
    if sv.is_available():
        assert sv.validate("visualContainer", raw) == []


# --- D7 image ----------------------------------------------------------------

def test_add_image_uploads_and_places(project, tmp_path):
    png = tmp_path / "logo.png"
    png.write_bytes(_PNG)
    vid = project.add_image("overview", str(png), scaling="Fit")
    # resource copied
    res = (project._require_report() / "StaticResources"
           / "RegisteredResources" / "logo.png")
    assert res.exists()
    raw = _reload_raw(project, "overview", vid)
    item = raw["visual"]["objects"]["general"][0]["properties"]["imageUrl"] \
        ["expr"]["ResourcePackageItem"]
    assert item["ItemName"] == "logo.png"
    if sv.is_available():
        assert sv.validate("visualContainer", raw) == []


def test_add_image_missing_file(project):
    with pytest.raises(FileNotFoundError):
        project.add_image("overview", "nope.png")


# --- D8 shape ----------------------------------------------------------------

def test_add_shape_backplate(project):
    vid = project.add_shape("overview", "rectangle", fill="#F5F7FA",
                            round_corners=True, z=0)
    raw = _reload_raw(project, "overview", vid)
    objs = raw["visual"]["objects"]
    assert objs["shape"][0]["properties"]["tileShape"]["expr"]["Literal"]["Value"] \
        == "'rectangleRounded'"
    assert objs["fill"][0]["properties"]["fillColor"]["solid"]["color"]["expr"] \
        ["Literal"]["Value"] == "'#F5F7FA'"


def test_add_shape_divider_line(project):
    vid = project.add_shape("overview", "line", outline="#CCCCCC",
                            outline_weight=2)
    raw = _reload_raw(project, "overview", vid)
    assert raw["visual"]["objects"]["shape"][0]["properties"]["tileShape"] \
        ["expr"]["Literal"]["Value"] == "'line'"
    if sv.is_available():
        assert sv.validate("visualContainer", raw) == []


def test_bad_shape_rejected(project):
    with pytest.raises(ValueError, match="Unknown shape"):
        project.add_shape("overview", "dodecahedron")


# --- D9 page chrome ----------------------------------------------------------

def test_style_page_background_and_wallpaper(project):
    project.style_page("overview", background_color="#FFFFFF",
                       background_transparency=0.0, wallpaper_color="#EEEEEE")
    import json
    pj = json.loads((project._require_report() / "definition" / "pages"
                     / "overview" / "page.json").read_text(encoding="utf-8-sig"))
    bg = pj["objects"]["background"][0]["properties"]
    assert bg["color"]["solid"]["color"]["expr"]["Literal"]["Value"] == "'#FFFFFF'"
    assert pj["objects"]["outspace"][0]["properties"]["color"]["solid"]["color"] \
        ["expr"]["Literal"]["Value"] == "'#EEEEEE'"
    # page.json still schema-valid
    if sv.is_available():
        assert sv.validate("page", pj) == []


def test_style_page_nothing_rejected(project):
    with pytest.raises(ValueError, match="Nothing to style"):
        project.style_page("overview")


# --- D10 groups --------------------------------------------------------------

def test_group_visuals(project):
    a = project.add_shape("overview", "rectangle", fill="#EEE",
                          position={"x": 10, "y": 10, "width": 100, "height": 50})
    b = project.add_text("overview", "KPI",
                         position={"x": 20, "y": 20, "width": 80, "height": 30})
    gid = project.group_visuals("overview", [a, b], name="KPI Block")
    import json
    reloaded = PbipProject(project.path)
    # members reference the group
    for vid in (a, b):
        raw = reloaded.get_visual("overview", vid).raw
        assert raw["parentGroupName"] == gid
    # group container exists with a bounding box covering both
    gf = json.loads((project._require_report() / "definition" / "pages"
                     / "overview" / "visuals" / gid / "visual.json")
                    .read_text(encoding="utf-8-sig"))
    assert gf["visualGroup"]["displayName"] == "KPI Block"
    assert gf["position"]["width"] >= 100


def test_group_needs_two(project):
    a = project.add_shape("overview", "rectangle")
    with pytest.raises(ValueError, match="at least two"):
        project.group_visuals("overview", [a])


# --- D11 raw escape hatch ----------------------------------------------------

def test_add_visual_raw_validates(project):
    # replicate a third-party-ish visual from a known-good card shape
    good = PbipProject(project.path).get_visual("overview", "card1").raw
    vid = project.add_visual_raw("overview", good, base="cloned")
    assert vid.startswith("cloned")
    assert PbipProject(project.path).get_visual("overview", vid).visual_type \
        == "card"


def test_add_visual_raw_rejects_bad(project):
    with pytest.raises(ValueError, match="Schema validation failed"):
        project.add_visual_raw("overview", {"name": "x", "position": "bad",
                                            "visual": {"visualType": "card"}})
