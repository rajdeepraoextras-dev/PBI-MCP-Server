"""Page rendering (SVG / PNG), theme-from-image, and their tools."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from core import render, theme_image
from core.journal import snapshot
from core.pbip import PbipProject
from core.pbir import build_visual_json
from core.theme import generate_theme
from report_server import tools_render
from report_server.server import ReportState, set_project

SYNTH = Path(__file__).parent / "fixtures" / "synthetic"
SVG_NS = "{http://www.w3.org/2000/svg}"


@pytest.fixture
def root(tmp_path) -> Path:
    dst = tmp_path / "s"
    shutil.copytree(SYNTH, dst)
    return dst


@pytest.fixture
def state(root) -> ReportState:
    st = ReportState()
    set_project(st, str(root / "Synthetic.pbip"))
    return st


def _texts(svg: str) -> str:
    return " ".join(t.strip() for t in ET.fromstring(svg).itertext() if t.strip())


def _add(project: PbipProject, page: str, vtype: str, bindings: dict, pos: dict,
         title: str | None = None, base: str = "v") -> str:
    obj = build_visual_json(base, vtype, bindings, project._is_measure, pos, title)
    return project.add_visual_raw(page, obj, base=base)


# --- SVG: structure ------------------------------------------------------------------

def test_every_synthetic_page_renders_well_formed_svg_with_every_visual(state):
    project = state.require()
    for page in project.list_pages():
        res = tools_render.render_page(state, page.id)
        doc = ET.fromstring(res["svg"])                       # well-formed XML
        assert doc.tag == f"{SVG_NS}svg"
        assert doc.get("viewBox") == f"0 0 {int(page.width)} {int(page.height)}"
        text = _texts(res["svg"])
        groups = [g for g in doc.iter(f"{SVG_NS}g") if g.get("data-visual-id")]
        visuals = project.list_visuals(page.id)
        assert {g.get("data-visual-id") for g in groups} == {v.id for v in visuals}
        for v in visuals:
            if v.title:                                       # the visual's own title
                assert v.title in text
            assert render.type_label(v.visual_type) in text   # friendly type name
            assert f'data-visual-type="{v.visual_type}"' in res["svg"]   # raw type too
        assert res["visual_count"] == len(visuals)


def test_titles_types_bindings_and_glyphs(state):
    svg = tools_render.render_page(state, "overview")["svg"]
    text = _texts(svg)
    assert "Revenue by Year" in text and "Clustered bar chart" in text
    assert "Category: Date.Year | Y: Sales.Net Revenue" in text      # compact bindings
    assert "Values: Sales.Net Revenue" in text                       # card
    assert "123" in text                                             # big-number glyph
    # untitled table: falls back to its type name (Power BI has no auto title for it)
    assert "Table" in text


def test_auto_title_when_none_is_set(root):
    project = PbipProject(root / "Synthetic.pbip")
    vid = _add(project, "overview", "lineChart",
               {"Category": ["Date.Year"], "Y": ["Sales.Net Revenue"]},
               dict(x=20, y=520, width=400, height=180))
    res = render.render_page(project, "overview")
    info = {v["id"]: v for v in res["visuals"]}
    assert info[vid]["title"] == "Net Revenue by Year"               # derived from bindings
    assert "Net Revenue by Year" in _texts(res["svg"])


def test_titles_with_markup_characters_are_escaped(root):
    project = PbipProject(root / "Synthetic.pbip")
    weird = "A & B <C> \"q\" 'x' \x07"
    _add(project, "overview", "card", {"Values": ["Sales.Net Revenue"]},
         dict(x=300, y=16, width=300, height=110), weird)
    res = render.render_page(project, "overview")
    ET.fromstring(res["svg"])                                        # still well-formed
    assert "A & B <C> \"q\" 'x'" in _texts(res["svg"])


def test_hidden_visual_is_dashed(root):
    project = PbipProject(root / "Synthetic.pbip")
    vfile = project._visual_file("overview", "card1")
    data = json.loads(vfile.read_text())
    data["isHidden"] = True
    project._write_json(vfile, data)
    doc = ET.fromstring(render.render_page(project, "overview")["svg"])
    groups = {g.get("data-visual-id"): g for g in doc.iter(f"{SVG_NS}g")
              if g.get("data-visual-id")}
    assert groups["card1"].get("data-hidden") == "true"
    assert any(r.get("stroke-dasharray") for r in groups["card1"].iter(f"{SVG_NS}rect"))
    assert groups["bar1"].get("data-hidden") is None
    assert not any(r.get("stroke-dasharray") for r in groups["bar1"].iter(f"{SVG_NS}rect"))


def test_group_is_outlined_and_named(root):
    project = PbipProject(root / "Synthetic.pbip")
    gid = project.group_visuals("overview", ["bar1", "table1"], "Charts")
    res = render.render_page(project, "overview")
    doc = ET.fromstring(res["svg"])
    assert "Group: Charts" in _texts(res["svg"])
    grp = [g for g in doc.iter(f"{SVG_NS}g") if g.get("data-group-name") == "Charts"]
    assert len(grp) == 1
    assert any(r.get("stroke-dasharray") for r in grp[0].iter(f"{SVG_NS}rect"))
    assert any(v["id"] == gid and v["type"] == "visualGroup" for v in res["visuals"])
    # members are still drawn
    drawn = {g.get("data-visual-id") for g in doc.iter(f"{SVG_NS}g")}
    assert {"bar1", "table1"} <= drawn


def test_z_order_is_respected(root):
    project = PbipProject(root / "Synthetic.pbip")
    project.add_shape("overview", fill="#123456", position=dict(x=0, y=0, width=300, height=300), z=-5)
    project.add_text("overview", "Front", position=dict(x=0, y=0, width=100, height=30), z=999)
    doc = ET.fromstring(render.render_page(project, "overview")["svg"])
    order = [(g.get("data-visual-id"), float(g.get("data-z")))
             for g in doc.iter(f"{SVG_NS}g") if g.get("data-visual-id")]
    zs = [z for _, z in order]
    assert zs == sorted(zs)
    assert order[0][0].startswith("shape") and order[-1][0].startswith("textbox")


def test_design_elements_draw_their_content(root):
    project = PbipProject(root / "Synthetic.pbip")
    project.add_shape("overview", fill="#0B7A75", position=dict(x=0, y=600, width=1280, height=60), z=0)
    project.add_text("overview", [{"text": "Executive Overview", "bold": True, "size": 20,
                                    "color": "#FFFFFF"}],
                     position=dict(x=16, y=610, width=600, height=40), z=100)
    project.add_nav_button("overview", "Go to Details", "details",
                           position=dict(x=1000, y=610, width=200, height=40))
    svg = render.render_page(project, "overview")["svg"]
    text = _texts(svg)
    assert "Executive Overview" in text and "Go to Details" in text
    assert 'fill="#0B7A75"' in svg                                   # the shape draws as itself
    assert 'fill="#FFFFFF"' in svg                                   # the text keeps its color


def test_every_shape_kind_draws_as_itself(root):
    project = PbipProject(root / "Synthetic.pbip")
    kinds = ["rectangle", "rectangleRounded", "oval", "line", "arrow", "triangle",
             "hexagon", "pentagon"]
    for i, kind in enumerate(kinds):
        project.add_shape("details", kind, fill="#00AA55" if kind != "line" else None,
                          outline="#112233", outline_weight=2,
                          position=dict(x=20 + i * 150, y=400, width=120, height=90), z=i)
    scene = render.build_scene(project, "details")
    svg = render.scene_to_svg(scene)
    doc = ET.fromstring(svg)
    shapes = [g for g in doc.iter(f"{SVG_NS}g") if g.get("data-visual-type") == "shape"]
    assert len(shapes) == len(kinds)
    for g in shapes:                                                 # shapes carry no labels
        assert not list(g.iter(f"{SVG_NS}text"))
        assert 'fill="#00AA55"' in ET.tostring(g, encoding="unicode") \
            or 'stroke="#112233"' in ET.tostring(g, encoding="unicode")
    kinds_drawn = {child.tag.replace(SVG_NS, "") for g in shapes for child in g}
    assert {"rect", "polygon", "line"} <= kinds_drawn
    if importlib.util.find_spec("PIL") is not None:
        assert render.scene_to_png(scene)[:4] == b"\x89PNG"


def test_empty_page_still_renders(root):
    project = PbipProject(root / "Synthetic.pbip")
    pid = project.create_page("Blank")
    res = render.render_page(project, pid)
    ET.fromstring(res["svg"])
    assert "No visuals on this page" in _texts(res["svg"])
    assert res["visual_count"] == 0


def test_deneb_visual_uses_the_spec_mark_glyph(root):
    from core import deneb

    project = PbipProject(root / "Synthetic.pbip")
    res = deneb.add_deneb_visual(project, "details", "line",
                                 {"category": "Date.Date", "value": "Sales.Net Revenue"})
    out = render.render_page(project, "details")
    doc = ET.fromstring(out["svg"])
    grp = next(g for g in doc.iter(f"{SVG_NS}g") if g.get("data-visual-id") == res["visual_id"])
    assert grp.get("data-visual-type") == deneb.DENEB_VISUAL_GUID
    assert "Deneb (Vega-Lite)" in _texts(out["svg"])
    assert list(grp.iter(f"{SVG_NS}polyline"))                       # line glyph, not the generic box


# --- SVG: colors ---------------------------------------------------------------------------

def test_default_palette_and_theme_colors(root):
    project = PbipProject(root / "Synthetic.pbip")
    plain = render.render_page(project, "overview")["svg"]
    assert "#118DFF" in plain                                        # Power BI default accent
    project.set_report_theme(generate_theme("#0B7A75", name="Teal"))
    themed = render.render_page(project, "overview")["svg"]
    assert "#0B7A75" in themed and "#118DFF" not in themed           # theme accent + palette


def test_stored_base_theme_is_used_and_custom_theme_wins(root):
    project = PbipProject(root / "Synthetic.pbip")
    base_dir = project.report_dir / "StaticResources" / "SharedResources" / "BaseThemes"
    base_dir.mkdir(parents=True)
    (base_dir / "CY24SU10.json").write_text(json.dumps({
        "name": "CY24SU10", "dataColors": ["#AA3300", "#003399", "#00AA33", "#AA00AA"],
        "foreground": "#111111", "background": "#FEFEFE", "tableAccent": "#AA3300"}))
    report = project.report_dir / "definition" / "report.json"
    doc = json.loads(report.read_text())
    doc["themeCollection"] = {"baseTheme": {"name": "CY24SU10", "type": "SharedResources",
                                            "reportVersionAtImport": "5.59"}}
    report.write_text(json.dumps(doc))
    theme = render.load_theme(project)
    assert theme["source"] == "base" and theme["dataColors"][0] == "#AA3300"
    assert theme["accent"] == "#AA3300" and theme["background"] == "#FEFEFE"
    assert "#AA3300" in render.render_page(project, "overview")["svg"]
    # a custom theme, when present, overrides the base theme
    project.set_report_theme(generate_theme("#0B7A75", name="Teal"))
    doc = json.loads(report.read_text())
    assert doc["themeCollection"]["baseTheme"]["name"] == "CY24SU10"      # kept alongside
    theme = render.load_theme(project)
    assert theme["source"] == "custom" and theme["accent"] == "#0B7A75"
    assert theme["foreground"] != "#111111"
    # a missing base file falls back to the Power BI defaults
    (base_dir / "CY24SU10.json").unlink()
    project.report_dir.joinpath("StaticResources", "RegisteredResources", "Teal.json").unlink()
    doc["themeCollection"].pop("customTheme")
    report.write_text(json.dumps(doc))
    assert render.load_theme(project)["source"] == "default"


def test_page_background_and_wallpaper_come_from_page_objects(root):
    project = PbipProject(root / "Synthetic.pbip")
    project.style_page("overview", background_color="#F4F7F7", wallpaper_color="#DDE5E5",
                       background_transparency=0)
    svg = render.render_page(project, "overview")["svg"]
    first_rects = svg.split("<g ")[0]
    assert 'fill="#DDE5E5"' in first_rects and 'fill="#F4F7F7"' in first_rects


def test_dark_theme_uses_light_text(root):
    project = PbipProject(root / "Synthetic.pbip")
    project.set_report_theme(generate_theme("#1F3A5F", name="Night", mode="dark"))
    scene = render.build_scene(project, "overview")
    texts = [i for i in scene.items if isinstance(i, render.Text)]
    assert texts and all(render.luminance(t.fill) > 0.5 for t in texts[:3])


# --- files + arguments -----------------------------------------------------------------------

def test_default_output_path_and_result_shape(state):
    res = tools_render.render_page(state, "overview")
    project = state.require()
    expected = project.report_dir / ".pbi" / "mcp-renders" / "overview.svg"
    assert Path(res["path"]) == expected and expected.exists()
    assert expected.read_text(encoding="utf-8") == res["svg"]
    assert res["bytes"] == expected.stat().st_size
    assert (res["width"], res["height"], res["scale"]) == (1280, 720, 1.0)
    assert res["format"] == "svg" and res["page_name"] == "Overview"
    assert {v["id"] for v in res["visuals"]} == {"card1", "bar1", "table1"}
    bar = next(v for v in res["visuals"] if v["id"] == "bar1")
    assert (bar["x"], bar["y"], bar["width"], bar["height"]) == (16, 140, 600, 360)
    assert not expected.read_bytes().startswith(b"\xef\xbb\xbf")     # UTF-8, no BOM
    assert b"\r\n" not in expected.read_bytes()                      # LF


def test_render_is_deterministic_and_leaves_the_project_untouched(state, root):
    before = snapshot(root)                                          # skips .pbi/
    a = tools_render.render_page(state, "overview")
    b = tools_render.render_page(state, "overview")
    assert a["svg"] == b["svg"]
    assert snapshot(root) == before


def test_inline_flag_and_size_limit(state, monkeypatch):
    assert "svg" not in tools_render.render_page(state, "overview", inline=False)
    monkeypatch.setattr(render, "INLINE_SVG_LIMIT", 100)
    res = tools_render.render_page(state, "overview")
    assert "svg" not in res and "inline_skipped" in res
    assert Path(res["path"]).exists()


def test_scale_changes_size_not_coordinates(state):
    res = tools_render.render_page(state, "overview", scale=2.0)
    doc = ET.fromstring(res["svg"])
    assert (doc.get("width"), doc.get("height")) == ("2560", "1440")
    assert doc.get("viewBox") == "0 0 1280 720"
    assert (res["width"], res["height"]) == (2560, 1440)


def test_out_path_file_directory_and_wrong_extension(state, tmp_path):
    f = tmp_path / "shots" / "p.svg"
    assert Path(tools_render.render_page(state, "overview", out_path=str(f))["path"]) == f
    assert f.exists()
    d = tmp_path / "dir"
    d.mkdir()
    assert Path(tools_render.render_page(state, "details", out_path=str(d))["path"]) == d / "details.svg"
    with pytest.raises(ValueError, match=r"\.svg"):
        tools_render.render_page(state, "overview", out_path=str(tmp_path / "x.png"))


@pytest.mark.parametrize("kwargs, message", [
    ({"format": "gif"}, "format must be"),
    ({"scale": 0}, "scale must be"),
    ({"scale": 99}, "scale must be"),
    ({"scale": True}, "scale must be"),
])
def test_bad_arguments(state, kwargs, message):
    with pytest.raises(ValueError, match=message):
        tools_render.render_page(state, "overview", **kwargs)


def test_unknown_page_lists_the_real_ones(state):
    with pytest.raises(ValueError, match="overview"):
        tools_render.render_page(state, "nope")


def test_render_report_renders_every_page(state, tmp_path):
    results = tools_render.render_report(state)
    assert [r["page_id"] for r in results] == ["overview", "details"]
    assert all(Path(r["path"]).exists() and "svg" not in r for r in results)
    out = tmp_path / "all"
    results = tools_render.render_report(state, out_dir=str(out))
    assert sorted(p.name for p in out.iterdir()) == ["details.svg", "overview.svg"]
    assert all(Path(r["path"]).parent == out for r in results)


def test_requires_a_project():
    with pytest.raises(ValueError, match="No project set"):
        tools_render.render_page(ReportState(), "overview")


# --- PNG (Pillow) -------------------------------------------------------------------------------

def test_png_render(state):
    Image = pytest.importorskip("PIL.Image")
    res = tools_render.render_page(state, "overview", format="png")
    path = Path(res["path"])
    assert path.suffix == ".png" and "svg" not in res
    with Image.open(path) as im:
        assert im.size == (1280, 720) and path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
        colors = im.convert("RGB").getcolors(maxcolors=1_000_000)
        assert len(colors) > 10                                      # actually drawn on
        assert im.convert("RGB").getpixel((5, 700)) == (255, 255, 255)   # page background
    half = tools_render.render_page(state, "overview", format="png", scale=0.5)
    with Image.open(half["path"]) as im:
        assert im.size == (640, 360)


def test_png_and_svg_are_drawn_from_the_same_scene(root):
    pytest.importorskip("PIL.Image")
    project = PbipProject(root / "Synthetic.pbip")
    scene = render.build_scene(project, "overview")
    assert render.scene_to_png(scene)[:4] == b"\x89PNG"
    assert render.scene_to_svg(scene).startswith('<?xml version="1.0"')


def test_png_covers_every_glyph_family(root):
    Image = pytest.importorskip("PIL.Image")
    project = PbipProject(root / "Synthetic.pbip")
    pid = project.create_page("Glyphs", 1280, 1000)
    y = 10
    for vtype, b in [
        ("clusteredBarChart", {"Category": ["Date.Year"], "Y": ["Sales.Net Revenue"]}),
        ("clusteredColumnChart", {"Category": ["Date.Year"], "Y": ["Sales.Net Revenue"], "Series": ["Sales.OrderDate"]}),
        ("lineChart", {"Category": ["Date.Year"], "Y": ["Sales.Net Revenue"]}),
        ("areaChart", {"Category": ["Date.Year"], "Y": ["Sales.Net Revenue"]}),
        ("lineClusteredColumnComboChart", {"Category": ["Date.Year"], "Y": ["Sales.Net Revenue"]}),
        ("waterfallChart", {"Category": ["Date.Year"], "Y": ["Sales.Net Revenue"]}),
        ("pieChart", {"Y": ["Sales.Net Revenue"], "Series": ["Date.Year"]}),
        ("donutChart", {"Y": ["Sales.Net Revenue"], "Series": ["Date.Year"]}),
        ("scatterChart", {"Details": ["Date.Year"], "X": ["Sales.Net Revenue"], "Y": ["Sales.Margin %"]}),
        ("gauge", {"Y": ["Sales.Net Revenue"]}),
        ("card", {"Values": ["Sales.Net Revenue"]}),
        ("kpi", {"Values": ["Sales.Net Revenue"]}),
        ("tableEx", {"Values": ["Date.Year", "Sales.Net Revenue"]}),
        ("pivotTable", {"Rows": ["Date.Year"], "Values": ["Sales.Net Revenue"]}),
        ("slicer", {"Values": ["Date.Year"]}),
        ("map", {"Category": ["Date.Year"]}),
        ("funnel", {"Category": ["Date.Year"], "Y": ["Sales.Net Revenue"]}),
        ("treemap", {"Category": ["Date.Year"], "Y": ["Sales.Net Revenue"]}),
        ("ribbonChart", {"Category": ["Date.Year"], "Y": ["Sales.Net Revenue"]}),
        ("decompositionTreeVisual", {"Values": ["Sales.Net Revenue"]}),
        ("someBrandNewVisual", {"Values": ["Date.Year"]}),
    ]:
        col = (len([v for v in project.list_visuals(pid)]) % 4)
        _add(project, pid, vtype, b, dict(x=10 + col * 315, y=10 + (len(project.list_visuals(pid)) // 4) * 190,
                                           width=300, height=180))
        y += 0
    res = render.render_page(project, pid, "png")
    with Image.open(res["path"]) as im:
        assert im.size == (1280, 1000)
    svg = render.render_page(project, pid)["svg"]
    ET.fromstring(svg)
    assert "Some brand new visual" in _texts(svg)                    # unknown types get a readable name


def test_png_without_pillow_explains_the_extra(state, monkeypatch):
    monkeypatch.setitem(sys.modules, "PIL", None)                    # simulate "not installed"
    with pytest.raises(ImportError, match=r"pbi-mcp\[render\]"):
        tools_render.render_page(state, "overview", format="png")
    with pytest.raises(ImportError, match=r"pbi-mcp\[render\]"):
        tools_render.render_report(state, format="png")
    assert not (state.require().report_dir / ".pbi" / "mcp-renders").exists()
    # SVG needs no Pillow at all
    assert tools_render.render_page(state, "overview")["svg"].startswith("<?xml")


def test_png_refuses_absurd_sizes_before_allocating(root):
    pytest.importorskip("PIL.Image")
    scene = render.build_scene(PbipProject(root / "Synthetic.pbip"), "overview")
    with pytest.raises(ValueError, match="megapixels"):
        render.scene_to_png(scene, 200.0)                            # 256000 x 144000 px


# --- hardening ---------------------------------------------------------------------------------------

def test_fit_text_is_bounded_and_truncates():
    long = "W" * 200_000
    out = render.fit_text(long, 12, 100)
    assert out.endswith("...") and render.text_width(out, 12) <= 100
    assert render.fit_text("short", 12, 100) == "short"
    assert render.fit_text("anything", 12, 0) == ""
    assert render.fit_text("a\nb\r\x00c", 12, 200) == "a b c"        # newlines flattened, NUL dropped
    bad = "x" + chr(0xD800) + "y" + chr(0xFFFF) + "z" + chr(0xFFFE)
    assert render.fit_text(bad, 12, 200) == "xyz"                     # XML-illegal code points


def test_deneb_label_follows_the_provider(root):
    from core import deneb

    project = PbipProject(root / "Synthetic.pbip")
    added = deneb.add_deneb_visual(project, "details", "line",
                                   {"category": "Date.Date", "value": "Sales.Net Revenue"})
    assert "Deneb (Vega-Lite)" in _texts(render.render_page(project, "details")["svg"])
    deneb.set_deneb_spec(project, "details", added["visual_id"],
                         {"$schema": "https://vega.github.io/schema/vega/v6.json",
                          "data": [{"name": "dataset"}], "marks": []})
    text = _texts(render.render_page(project, "details")["svg"])
    assert "Deneb (Vega)" in text and "Deneb (Vega-Lite)" not in text


def _odd_visuals() -> list[dict]:
    """Malformed / extreme visual.json bodies a hand-edited report can contain."""
    lit = lambda v: {"expr": {"Literal": {"Value": v}}}                  # noqa: E731
    pos = lambda **k: {**dict(x=10, y=10, width=200, height=120), **k}  # noqa: E731
    return [
        {"name": "zero", "position": pos(width=0, height=0), "visual": {"visualType": "card"}},
        {"name": "negative", "position": pos(x=-500, y=-500, width=-20, height=-20),
         "visual": {"visualType": "lineChart"}},
        {"name": "huge", "position": pos(x=1e7, y=1e7, width=1e7, height=1e7, z=1e9),
         "visual": {"visualType": "clusteredBarChart", "query": {"queryState": {"Y": {
             "projections": [{"queryRef": "Sales.Net Revenue"}]}}}}},
        {"name": "tiny", "position": pos(width=5, height=5), "visual": {"visualType": "tableEx"}},
        {"name": "no-type", "position": {"x": 10, "y": 10}, "visual": {}},
        {"name": "bare", "position": pos()},                                # neither visual nor group
        {"name": "junk-text", "position": pos(width=300, height=200), "visual": {
            "visualType": "textbox", "objects": {"general": [{"properties": {"paragraphs": [
                {"textRuns": "oops"}, {"textRuns": [{"value": 5}]}, "x", None,
                {"textRuns": [{"value": "ok", "textStyle": {"fontSize": "abcpt", "color": 7}}]}]}}]}}},
        {"name": "odd-shape", "isHidden": "yes", "position": pos(), "visual": {
            "visualType": "shape", "objects": {"shape": [{"properties": {"tileShape": lit("'star'")}}],
                                               "fill": [{"properties": {"fillColor": "not-a-color"}}]}}},
        {"name": "self-parent", "parentGroupName": "self-parent", "position": pos(),
         "visual": {"visualType": "card"}},
        {"name": "g1", "parentGroupName": "g2", "position": pos(width=400, height=300),
         "visualGroup": {"displayName": "G1 <&> \"q\"", "groupMode": "ScaleMode"}},
        {"name": "g2", "parentGroupName": "g1", "position": pos(width=400, height=300),
         "visualGroup": {"displayName": "G2", "groupMode": "ScaleMode"}},
        {"name": "orphan", "parentGroupName": "missing-group", "position": pos(),
         "visual": {"visualType": "slicer"}},
        {"name": "bad-deneb", "position": pos(), "visual": {
            "visualType": "deneb7E15AEF80B9E4D4F8E12924291ECE89A",
            "objects": {"vega": [{"properties": {"jsonSpec": lit("'not json'")}}]}}},
        {"name": "dynamic-title", "position": pos(), "visual": {
            "visualType": "clusteredBarChart", "visualContainerObjects": {"title": [{"properties": {
                "text": {"expr": {"Measure": {"Expression": {"SourceRef": {"Entity": "Sales"}},
                                              "Property": "Net Revenue"}}},
                "show": lit("true")}}]}}},
        {"name": "button", "position": pos(), "visual": {
            "visualType": "actionButton", "objects": {"text": [{"properties": {"text": lit("5L")}}],
                                                      "fill": [{"properties": {}}]}}},
    ]


def test_renderer_survives_malformed_and_extreme_visuals(root):
    project = PbipProject(root / "Synthetic.pbip")
    vdir = project.report_dir / "definition" / "pages" / "details" / "visuals"
    for body in _odd_visuals():                                      # raw writes: no schema in the way
        (vdir / body["name"]).mkdir()
        (vdir / body["name"] / "visual.json").write_text(json.dumps(body), encoding="utf-8")
    res = render.render_page(project, "details")
    doc = ET.fromstring(res["svg"])                                  # still well-formed XML
    assert len(res["visuals"]) == 1 + len(_odd_visuals())            # slicer1 + every odd one
    assert "G1 <&> \"q\"" in _texts(res["svg"])
    assert {"zero", "huge", "orphan"} <= {g.get("data-visual-id") for g in doc.iter(f"{SVG_NS}g")}
    if importlib.util.find_spec("PIL") is not None:                  # the raster back-end copes too
        scene = render.build_scene(project, "details")
        assert render.scene_to_png(scene, 0.25)[:4] == b"\x89PNG"


# --- theme from image ------------------------------------------------------------------------------

def _logo(path: Path, blocks) -> Path:
    Image = pytest.importorskip("PIL.Image")
    from PIL import ImageDraw

    im = Image.new("RGB", (200, 200), (255, 255, 255))
    d = ImageDraw.Draw(im)
    for box, color in blocks:
        d.rectangle(box, fill=color)
    im.save(path)
    return path


TEAL, ORANGE, PURPLE, MUTED = (11, 122, 117), (230, 126, 34), (125, 60, 152), (140, 109, 70)


def _brand(tmp_path) -> Path:
    return _logo(tmp_path / "brand.png", [
        ((0, 0, 199, 12), (20, 20, 20)),             # near-black band: dropped
        ((10, 20, 120, 110), TEAL),                  # the vivid brand color, largest
        ((130, 20, 190, 80), ORANGE),
        ((10, 120, 70, 190), PURPLE),
        ((90, 130, 190, 190), MUTED),                # low saturation: never the accent
        ((80, 120, 88, 128), (128, 128, 128)),       # gray: dropped
    ])


def _close(hex_color: str, rgb, tol: int = 6) -> bool:
    r, g, b, _ = render.parse_color(hex_color)
    return max(abs(r - rgb[0]), abs(g - rgb[1]), abs(b - rgb[2])) <= tol


def test_theme_extraction_picks_the_expected_accent(tmp_path):
    out = theme_image.theme_from_image(_brand(tmp_path), name="Brand", mode="light")
    assert _close(out["accent"], TEAL)                               # most saturated mid-tone
    colors = out["data_colors"]
    assert 6 <= len(colors) <= 8 and len(set(colors)) == len(colors)
    assert colors[0] == out["accent"]
    assert any(_close(c, ORANGE) for c in colors) and any(_close(c, PURPLE) for c in colors)
    for c in colors:                                                 # no near-white / near-black
        assert theme_image.MIN_LUMINANCE <= render.luminance(c) <= theme_image.MAX_LUMINANCE
    theme = out["theme"]
    assert theme["name"] == "Brand" and theme["dataColors"] == colors   # palette overridden
    assert theme["tableAccent"] == out["accent"]                     # from generate_theme(accent)
    assert theme["background"] == "#FFFFFF" and "textClasses" in theme
    hexes = [s["hex"] for s in out["swatches"]]
    assert out["accent"] in hexes and "#FFFFFF" not in hexes
    dark = theme_image.theme_from_image(_brand(tmp_path), mode="dark")
    assert dark["theme"]["background"] != "#FFFFFF" and dark["theme"]["name"] == "brand Theme"


def test_theme_generation_reuses_core_theme(tmp_path, monkeypatch):
    calls = []
    real = theme_image.generate_theme
    monkeypatch.setattr(theme_image, "generate_theme",
                        lambda *a, **k: calls.append(a) or real(*a, **k))
    out = theme_image.theme_from_image(_brand(tmp_path), name="X")
    assert calls and calls[0][0] == out["accent"] and calls[0][2] == "light"


def test_theme_extraction_edge_cases(tmp_path):
    # monochrome artwork still yields a usable palette
    gray = _logo(tmp_path / "gray.png", [((20, 20, 180, 180), (0, 0, 0))])
    out = theme_image.theme_from_image(gray)
    assert len(out["data_colors"]) >= 6
    # a single vivid color is padded to a full palette derived from it
    one = _logo(tmp_path / "one.png", [((0, 0, 199, 199), (200, 30, 30))])
    out = theme_image.theme_from_image(one)
    assert _close(out["accent"], (200, 30, 30)) and len(out["data_colors"]) >= 6
    # nothing but white has no usable colors
    white = _logo(tmp_path / "white.png", [])
    with pytest.raises(ValueError, match="no usable colors"):
        theme_image.theme_from_image(white)
    with pytest.raises(ValueError, match="mode"):
        theme_image.theme_from_image(_brand(tmp_path), mode="sepia")
    with pytest.raises(FileNotFoundError):
        theme_image.theme_from_image(tmp_path / "missing.png")


def test_transparent_logo_is_flattened_onto_white(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    im = Image.new("RGBA", (100, 100), (0, 0, 0, 0))                 # fully transparent
    for x in range(20, 80):
        for y in range(20, 80):
            im.putpixel((x, y), (*TEAL, 255))
    path = tmp_path / "logo.png"
    im.save(path)
    out = theme_image.theme_from_image(path)
    assert _close(out["accent"], TEAL)                               # not the black under alpha 0


def test_theme_from_image_tool_previews_then_installs(state, root, tmp_path):
    image = _brand(tmp_path)
    before = snapshot(root)
    preview = tools_render.theme_from_image(state, str(image), "Brand Look")
    assert preview["installed"] is False and snapshot(root) == before   # nothing written
    installed = tools_render.theme_from_image(state, str(image), "Brand Look", install=True)
    assert installed["installed"] is True and installed["resource"] == "Brand Look.json"
    assert isinstance(installed["theme"], dict) and installed["theme"]["name"] == "Brand Look"
    assert installed["theme"] == preview["theme"]                    # same theme JSON either way
    project = state.require()
    theme_file = project.report_dir / "StaticResources" / "RegisteredResources" / "Brand Look.json"
    assert json.loads(theme_file.read_text())["dataColors"] == installed["data_colors"]
    report = json.loads((project.report_dir / "definition" / "report.json").read_text())
    assert report["themeCollection"]["customTheme"]["name"] == "Brand Look.json"
    assert project.report_accent() == installed["data_colors"][0] == installed["accent"]
    assert project.validate_project()["ok"]
    # the render now picks that theme up
    assert installed["accent"].upper() in render.render_page(project, "overview")["svg"]


def test_theme_names_become_safe_file_names(state, tmp_path):
    res = tools_render.theme_from_image(state, str(_brand(tmp_path)), "../evil/name", install=True)
    assert "/" not in res["resource"] and ".." not in res["resource"]
    resources = state.require().report_dir / "StaticResources" / "RegisteredResources"
    assert (resources / res["resource"]).exists()


def test_theme_from_image_without_pillow(state, tmp_path, monkeypatch):
    image = _brand(tmp_path)
    monkeypatch.setitem(sys.modules, "PIL", None)
    with pytest.raises(ImportError, match=r"pbi-mcp\[render\]"):
        tools_render.theme_from_image(state, str(image))


# --- MCP wiring -------------------------------------------------------------------------------------

def _mcp_tools():
    from report_server.server import mcp

    return {t.name: t for t in asyncio.run(mcp.list_tools())}


def _hint(tool, name: str):
    snake = {"readOnlyHint": "read_only_hint", "destructiveHint": "destructive_hint",
             "idempotentHint": "idempotent_hint"}[name]
    a = tool.annotations
    return getattr(a, name) if hasattr(a, name) else getattr(a, snake)


def _schema(tool) -> dict:
    return getattr(tool, "inputSchema", None) or getattr(tool, "input_schema")


def test_render_tools_are_registered_with_honest_annotations():
    tools = _mcp_tools()
    for name in ("pbi_render_page", "pbi_render_report", "pbi_theme_from_image"):
        assert name in tools
    page, report, theme = (tools[n] for n in
                           ("pbi_render_page", "pbi_render_report", "pbi_theme_from_image"))
    # they write files, so they are not read-only; the render tools are not journaled
    assert _hint(page, "readOnlyHint") is False and _hint(page, "idempotentHint") is True
    assert "dry_run" not in _schema(page)["properties"]
    assert _schema(page)["properties"]["format"]["enum"] == ["svg", "png"]
    assert _hint(report, "readOnlyHint") is False
    assert _hint(theme, "readOnlyHint") is False and _hint(theme, "destructiveHint") is True
    assert "dry_run" in _schema(theme)["properties"]                 # install goes through the journal
    assert _schema(theme)["properties"]["mode"]["enum"] == ["light", "dark"]
    assert "install" in _schema(theme)["properties"]


def test_render_and_theme_over_the_mcp_layer(root, tmp_path):
    from report_server.server import STATE, mcp

    def call(tool_name, /, **args):
        result = asyncio.run(mcp.call_tool(tool_name, args))
        if isinstance(result, tuple):
            result = result[0]
        structured = getattr(result, "structured_content", None) \
            or getattr(result, "structuredContent", None)
        if structured is not None:
            return structured.get("result", structured) if isinstance(structured, dict) else structured
        content = getattr(result, "content", result)
        items = [json.loads(c.text) for c in content]
        return items[0] if len(items) == 1 else items

    set_project(STATE, str(root / "Synthetic.pbip"))
    res = call("pbi_render_page", page_id="overview")
    assert res["ok"] and res["svg"].startswith("<?xml") and Path(res["path"]).exists()
    pages = call("pbi_render_report")
    assert [p["page_id"] for p in pages] == ["overview", "details"]

    before = snapshot(root)
    preview = call("pbi_theme_from_image", image_path=str(_brand(tmp_path)), name="Wired",
                   install=True, dry_run=True)
    assert preview["dry_run"] is True and snapshot(root) == before
    assert any("Wired.json" in p for p in preview["changes"]["added"])
    call("pbi_theme_from_image", image_path=str(_brand(tmp_path)), name="Wired", install=True)
    assert snapshot(root) != before
    call("pbi_undo")
    assert snapshot(root) == before
