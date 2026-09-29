"""core/import_pages + pbi_import_pages / pbi_import_visuals."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
from pathlib import Path

import pytest

from core.import_pages import import_pages, import_visuals
from core.journal import snapshot
from core.pbip import PbipProject

SYNTH = Path(__file__).parent / "fixtures" / "synthetic"
PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
       b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xff"
       b"\xff?\x00\x05\xfe\x02\xfe\xa7\x9a\xa0\xa0\x00\x00\x00\x00IEND\xaeB`\x82")


# --- helpers --------------------------------------------------------------------------

@pytest.fixture
def pair(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    shutil.copytree(SYNTH, src)
    shutil.copytree(SYNTH, dst)
    return src, dst


def P(root: Path) -> PbipProject:
    return PbipProject(root / "Synthetic.pbip", backups=False)


def rdef(root: Path) -> Path:
    return root / "Synthetic.Report" / "definition"


def pages_dir(root: Path) -> Path:
    return rdef(root) / "pages"


def force_rmtree(path: Path) -> None:
    """rmtree that also clears read-only flags (Windows checkouts/copies)."""
    for p in [path, *path.rglob("*")]:
        try:
            os.chmod(p, stat.S_IRWXU)
        except OSError:
            pass
    shutil.rmtree(path)


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def edit_json(path: Path, fn) -> None:
    data = load(path)
    fn(data)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8", newline="\n")


def order(root: Path) -> list[str]:
    return load(pages_dir(root) / "pages.json")["pageOrder"]


def vjson(root: Path, page: str, vid: str) -> dict:
    return load(pages_dir(root) / page / "visuals" / vid / "visual.json")


def assert_valid(root: Path) -> None:
    res = P(root).validate_project()
    assert res["ok"], res


def png(tmp_path: Path, name: str = "logo.png", data: bytes = PNG) -> Path:
    p = tmp_path / name
    p.write_bytes(data)
    return p


def registered(root: Path, *items: dict) -> None:
    def add(j):
        j["resourcePackages"] = [{"name": "RegisteredResources",
                                  "type": "RegisteredResources",
                                  "items": list(items)}]
    edit_json(rdef(root) / "report.json", add)


# --- import_pages: ids, files, order -----------------------------------------------------

def test_import_pages_assigns_new_ids_when_taken(pair):
    src, dst = pair
    res = import_pages(P(dst), P(src), ["overview", "details"])
    assert res["ok"] and res["id_map"] == {"overview": "overview-2",
                                           "details": "details-2"}
    assert res["order"] == ["overview", "details", "overview-2", "details-2"]
    assert order(dst) == res["order"]
    for new, old, vids in (("overview-2", "overview", {"bar1", "card1", "table1"}),
                           ("details-2", "details", {"slicer1"})):
        pj = load(pages_dir(dst) / new / "page.json")
        assert pj["name"] == new and pj["displayName"] == load(
            pages_dir(src) / old / "page.json")["displayName"]
        got = {p.name for p in (pages_dir(dst) / new / "visuals").iterdir()}
        assert got == vids
        for vid in vids:
            assert vjson(dst, new, vid) == vjson(src, old, vid)
    assert res["pages"][0]["visual_ids"] == {"bar1": "bar1", "card1": "card1",
                                             "table1": "table1"}
    assert any("already exists" in w for w in res["warnings"])
    assert_valid(dst)
    # the originals are untouched
    assert vjson(dst, "overview", "card1") == vjson(src, "overview", "card1")


def test_free_ids_are_kept(pair):
    src, dst = pair
    P(dst).delete_page("details")
    res = import_pages(P(dst), P(src), ["details"])
    assert res["id_map"] == {"details": "details"}
    assert order(dst) == ["overview", "details"]
    assert (pages_dir(dst) / "details" / "visuals" / "slicer1" / "visual.json").exists()
    assert_valid(dst)


def test_rename_map_sets_display_name_and_derives_the_new_id(pair):
    src, dst = pair
    res = import_pages(P(dst), P(src), ["overview"],
                       rename_map={"overview": "Sales 2024"})
    assert res["id_map"] == {"overview": "sales-2024"}
    assert load(pages_dir(dst) / "sales-2024" / "page.json")["displayName"] == "Sales 2024"
    assert not any("already exists" in w for w in res["warnings"])
    # a free id survives a rename
    P(dst).delete_page("details")
    res = import_pages(P(dst), P(src), ["details"], rename_map={"details": "Renamed"})
    assert res["id_map"] == {"details": "details"}
    assert load(pages_dir(dst) / "details" / "page.json")["displayName"] == "Renamed"


@pytest.mark.parametrize("position, expected", [
    (None, ["overview", "details", "details-2"]),
    (0, ["details-2", "overview", "details"]),
    (1, ["overview", "details-2", "details"]),
    (2, ["overview", "details", "details-2"]),
    (99, ["overview", "details", "details-2"]),
])
def test_position(pair, position, expected):
    src, dst = pair
    res = import_pages(P(dst), P(src), ["details"], position=position)
    assert res["order"] == expected == order(dst)
    assert [p.id for p in P(dst).list_pages()] == expected


def test_bad_position_and_arguments(pair):
    src, dst = pair
    before = snapshot(dst)
    for kw in ({"position": -1}, {"position": True}, {"position": "1"}):
        with pytest.raises(ValueError, match="position"):
            import_pages(P(dst), P(src), ["details"], **kw)
    with pytest.raises(ValueError, match="at least one"):
        import_pages(P(dst), P(src), [])
    with pytest.raises(ValueError, match="duplicates"):
        import_pages(P(dst), P(src), ["details", "details"])
    with pytest.raises(ValueError, match="ghost"):
        import_pages(P(dst), P(src), ["ghost"])
    with pytest.raises(ValueError, match="rename_map"):
        import_pages(P(dst), P(src), ["details"], rename_map={"overview": "x"})
    with pytest.raises(ValueError, match="non-empty"):
        import_pages(P(dst), P(src), ["details"], rename_map={"details": " "})
    assert snapshot(dst) == before


def test_imported_pages_keep_source_order_not_argument_order(pair):
    src, dst = pair
    res = import_pages(P(dst), P(src), ["details", "overview"])
    assert res["order"][-2:] == ["overview-2", "details-2"]


def test_long_display_names_get_ids_within_the_schema_limit(pair):
    src, dst = pair
    long_name = "Quarterly Revenue Performance Review " * 3
    res = import_pages(P(dst), P(src), ["overview"], rename_map={"overview": long_name})
    new = res["id_map"]["overview"]
    assert 0 < len(new) <= 50
    res2 = import_pages(P(dst), P(src), ["overview"], rename_map={"overview": long_name})
    assert len(res2["id_map"]["overview"]) <= 50
    assert res2["id_map"]["overview"] != new
    assert_valid(dst)


def test_import_from_the_same_project(pair):
    _, dst = pair
    res = import_pages(P(dst), P(dst), ["overview"])
    assert res["id_map"] == {"overview": "overview-2"}
    assert_valid(dst)


def test_page_binding_follows_the_new_id(pair):
    src, dst = pair
    P(src).set_page_role("overview", "drillthrough")
    import_pages(P(dst), P(src), ["overview"])
    assert load(pages_dir(dst) / "overview-2" / "page.json")["pageBinding"]["name"] == "overview-2"
    assert_valid(dst)


def test_extra_files_are_copied_verbatim(pair):
    src, dst = pair
    (pages_dir(src) / "overview" / "visuals" / "card1" / "mobile.json").write_text(
        '{"mobile": true}', encoding="utf-8")
    (pages_dir(src) / "overview" / "notes.txt").write_text("hi", encoding="utf-8")
    import_pages(P(dst), P(src), ["overview"])
    new = pages_dir(dst) / "overview-2"
    assert (new / "visuals" / "card1" / "mobile.json").read_text(encoding="utf-8") == '{"mobile": true}'
    assert (new / "notes.txt").read_text(encoding="utf-8") == "hi"


# --- bookmarks ----------------------------------------------------------------------------------

def test_bookmarks_for_imported_pages_follow_with_remapped_ids(pair):
    src, dst = pair
    P(src).create_bookmark("Focus", page_id="overview")
    P(src).create_bookmark("Elsewhere", page_id="details")
    res = import_pages(P(dst), P(src), ["overview"])
    assert len(res["bookmarks"]["imported"]) == 1
    bdir = rdef(dst) / "bookmarks"
    assert sorted(p.name for p in bdir.glob("*.json")) == ["Focus.json", "bookmarks.json"]
    bm = load(bdir / "Focus.json")
    assert bm["explorationState"]["activeSection"] == "overview-2"
    assert set(bm["explorationState"]["sections"]) == {"overview-2"}   # details dropped
    assert load(bdir / "bookmarks.json")["items"] == [{"name": "Focus"}]
    assert_valid(dst)

    # importing again collides on name and file stem -> both get a suffix
    res2 = import_pages(P(dst), P(src), ["overview"])
    new_name = res2["bookmarks"]["imported"][0]["to"]
    assert new_name.endswith("-2") and new_name != res2["bookmarks"]["imported"][0]["from"]
    assert load(bdir / "Focus-2.json")["name"] == new_name
    assert load(bdir / "Focus-2.json")["explorationState"]["activeSection"] == "overview-3"
    assert load(bdir / "bookmarks.json")["items"] == [{"name": "Focus"}, {"name": "Focus-2"}]


def test_include_bookmarks_false(pair):
    src, dst = pair
    P(src).create_bookmark("Focus", page_id="overview")
    res = import_pages(P(dst), P(src), ["overview"], include_bookmarks=False)
    assert res["bookmarks"] == {"imported": [], "skipped": []}
    assert not (rdef(dst) / "bookmarks").exists()


def test_invalid_bookmarks_are_skipped_with_a_warning(pair):
    src, dst = pair
    P(src).create_bookmark("Broken", page_id="overview")
    edit_json(rdef(src) / "bookmarks" / "Broken.json",
              lambda j: j["explorationState"].pop("version"))     # required by schema
    res = import_pages(P(dst), P(src), ["overview"])
    assert res["bookmarks"]["imported"] == [] and len(res["bookmarks"]["skipped"]) == 1
    assert any("skipped" in w for w in res["warnings"])
    assert not (rdef(dst) / "bookmarks" / "Broken.json").exists()


# --- navigation buttons ---------------------------------------------------------------------------

def _nav_target(vis: dict) -> str:
    return vis["visual"]["visualContainerObjects"]["visualLink"][0]["properties"][
        "navigationSection"]["expr"]["Literal"]["Value"]


def test_nav_buttons_follow_imported_pages(pair):
    src, dst = pair
    nav = P(src).add_nav_button("overview", "Go", "details")
    res = import_pages(P(dst), P(src), ["overview", "details"])
    assert _nav_target(vjson(dst, "overview-2", nav)) == "'details-2'"
    assert not any("navigation" in w for w in res["warnings"])
    # only the source page: the link keeps pointing at the target's own 'details'
    res = import_pages(P(dst), P(src), ["overview"])
    assert _nav_target(vjson(dst, "overview-3", nav)) == "'details'"


def test_dangling_nav_target_is_reported(pair):
    src, dst = pair
    P(src).add_nav_button("overview", "Go", "details")
    P(dst).delete_page("details")
    res = import_pages(P(dst), P(src), ["overview"])
    assert any("navigation" in w and "details" in w for w in res["warnings"])


# --- resources ---------------------------------------------------------------------------------------

def test_registered_images_are_copied_and_reregistered(pair, tmp_path):
    src, dst = pair
    P(src).add_image("overview", str(png(tmp_path)), position={"x": 0, "y": 0})
    item = {"name": "logo.png", "path": "logo.png", "type": "Image"}
    registered(src, item)
    res = import_pages(P(dst), P(src), ["overview"])
    assert res["resources"] == {"copied": ["logo.png"], "reused": [], "renamed": {}}
    reg = dst / "Synthetic.Report" / "StaticResources" / "RegisteredResources"
    assert (reg / "logo.png").read_bytes() == PNG
    assert load(rdef(dst) / "report.json")["resourcePackages"] == [
        {"name": "RegisteredResources", "type": "RegisteredResources", "items": [item]}]
    assert_valid(dst)

    # same bytes again: reused, not registered twice
    res = import_pages(P(dst), P(src), ["overview"])
    assert res["resources"] == {"copied": [], "reused": ["logo.png"], "renamed": {}}
    assert len(load(rdef(dst) / "report.json")["resourcePackages"][0]["items"]) == 1

    # same name, different bytes: copied under a new name and re-pointed
    (src / "Synthetic.Report" / "StaticResources" / "RegisteredResources"
     / "logo.png").write_bytes(PNG + b"\x00")
    res = import_pages(P(dst), P(src), ["overview"])
    assert res["resources"]["renamed"] == {"logo.png": "logo-2.png"}
    assert (reg / "logo-2.png").read_bytes() == PNG + b"\x00"
    assert (reg / "logo.png").read_bytes() == PNG                 # original kept
    new_page = res["id_map"]["overview"]
    image_ref = vjson(dst, new_page, "image")["visual"]["objects"]["general"][0][
        "properties"]["imageUrl"]["expr"]["ResourcePackageItem"]
    assert image_ref["ItemName"] == "logo-2.png"
    names = [i["name"] for i in load(rdef(dst) / "report.json")["resourcePackages"][0]["items"]]
    assert names == ["logo.png", "logo-2.png"]
    assert_valid(dst)


def test_unregistered_images_are_copied_without_touching_report_json(pair, tmp_path):
    src, dst = pair
    P(src).add_image("overview", str(png(tmp_path)))
    before = (rdef(dst) / "report.json").read_bytes()
    res = import_pages(P(dst), P(src), ["overview"])
    assert res["resources"]["copied"] == ["logo.png"]
    assert (dst / "Synthetic.Report/StaticResources/RegisteredResources/logo.png").exists()
    assert (rdef(dst) / "report.json").read_bytes() == before


def test_missing_and_unsafe_resources(pair, tmp_path):
    src, dst = pair
    P(src).add_image("overview", str(png(tmp_path)))
    reg = src / "Synthetic.Report/StaticResources/RegisteredResources/logo.png"
    reg.unlink()
    res = import_pages(P(dst), P(src), ["overview"])
    assert res["resources"]["copied"] == []
    assert any("logo.png" in w and "does not exist" in w for w in res["warnings"])
    # a registered path that escapes the resource folder is refused
    reg.write_bytes(PNG)
    registered(src, {"name": "logo.png", "path": "../evil.png", "type": "Image"})
    before = snapshot(dst)
    with pytest.raises(ValueError, match="Unsafe resource path"):
        import_pages(P(dst), P(src), ["overview"])
    assert snapshot(dst) == before


def test_public_custom_visual_registration_follows(pair):
    src, dst = pair
    edit_json(pages_dir(src) / "overview" / "visuals" / "card1" / "visual.json",
              lambda j: j["visual"].update(visualType="myCustomVisual123"))
    edit_json(rdef(src) / "report.json",
              lambda j: j.update(publicCustomVisuals=["myCustomVisual123"]))
    import_pages(P(dst), P(src), ["overview"])
    assert load(rdef(dst) / "report.json")["publicCustomVisuals"] == ["myCustomVisual123"]
    assert_valid(dst)


# --- field check ----------------------------------------------------------------------------------------

def _drop_year_column(root: Path) -> None:
    f = root / "Synthetic.SemanticModel/definition/tables/Date.tmdl"
    text = f.read_text(encoding="utf-8")
    old = "\tcolumn Year\n\t\tdataType: int64\n\t\tsummarizeBy: none\n\t\tsourceColumn: Year\n\n"
    assert old in text
    f.write_text(text.replace(old, ""), encoding="utf-8", newline="\n")


def test_missing_fields_refuse_the_import_and_write_nothing(pair):
    src, dst = pair
    _drop_year_column(dst)
    before = snapshot(dst)
    with pytest.raises(ValueError) as err:
        import_pages(P(dst), P(src), ["overview", "details"])
    msg = str(err.value)
    assert "Date.Year" in msg and "allow_missing_fields" in msg
    assert snapshot(dst) == before

    res = import_pages(P(dst), P(src), ["overview", "details"], allow_missing_fields=True)
    assert res["ok"]
    assert res["missing_fields"] == [{
        "field": "Date.Year",
        "used_in": ["details/slicer1", "overview/bar1"]}]


def test_missing_measure_in_a_page_filter_is_caught(pair):
    src, dst = pair
    from core.formatting import build_filter

    P(src).add_filter("page", build_filter("Sales.Ghost", is_measure=True, values=[1],
                                           name="F"), page_id="overview")
    with pytest.raises(ValueError, match="Sales.Ghost"):
        import_pages(P(dst), P(src), ["overview"])
    res = import_pages(P(dst), P(src), ["overview"], allow_missing_fields=True)
    assert [m["field"] for m in res["missing_fields"]] == ["Sales.Ghost"]
    assert res["missing_fields"][0]["used_in"] == ["overview"]


def test_report_level_measures_in_the_target_satisfy_the_check(pair):
    src, dst = pair
    edit_json(pages_dir(src) / "overview" / "visuals" / "card1" / "visual.json",
              lambda j: j["visual"]["query"]["queryState"]["Values"]["projections"][0]["field"]["Measure"].update(Property="RptOnly"))
    ext = {"name": "extension", "entities": [{"name": "Sales", "measures": [
        {"name": "RptOnly", "expression": "1"}]}]}
    (rdef(src) / "reportExtensions.json").write_text(json.dumps(ext), encoding="utf-8")
    with pytest.raises(ValueError, match="Sales.RptOnly"):
        import_pages(P(dst), P(src), ["overview"])
    res = import_pages(P(dst), P(src), ["overview"], allow_missing_fields=True)
    assert "reportExtensions" in res["missing_fields"][0]["note"]
    (rdef(dst) / "reportExtensions.json").write_text(json.dumps(ext), encoding="utf-8")
    assert import_pages(P(dst), P(src), ["overview"])["missing_fields"] == []


def test_no_target_model_skips_the_check_with_a_warning(pair):
    src, dst = pair
    force_rmtree(dst / "Synthetic.SemanticModel")
    res = import_pages(P(dst), P(src), ["overview"])
    assert any("field check skipped" in w for w in res["warnings"])
    assert res["missing_fields"] == []


# --- validation + rollback ---------------------------------------------------------------------------------

def test_schema_invalid_source_is_refused_before_anything_is_written(pair):
    src, dst = pair
    edit_json(pages_dir(src) / "overview" / "visuals" / "bar1" / "visual.json",
              lambda j: j.pop("position"))
    before = snapshot(dst)
    with pytest.raises(ValueError, match="schema validation"):
        import_pages(P(dst), P(src), ["overview", "details"])
    assert snapshot(dst) == before


def test_unreadable_source_files_give_actionable_errors(pair):
    src, dst = pair
    before = snapshot(dst)
    bad = pages_dir(src) / "overview" / "visuals" / "card1" / "visual.json"
    bad.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ValueError, match="card1"):
        import_pages(P(dst), P(src), ["overview"])
    bad.unlink()
    (pages_dir(src) / "details" / "page.json").unlink()
    with pytest.raises(ValueError, match="no page.json"):
        import_pages(P(dst), P(src), ["details"])
    assert snapshot(dst) == before


def test_a_failing_write_is_rolled_back(pair, monkeypatch, tmp_path):
    src, dst = pair
    P(src).add_image("overview", str(png(tmp_path)))
    before = snapshot(dst)
    calls = {"n": 0}
    real = PbipProject._write_json

    def flaky(self, path, obj, *, validate=True):
        calls["n"] += 1
        if calls["n"] == 4:
            raise OSError("disk full")
        return real(self, path, obj, validate=validate)

    monkeypatch.setattr(PbipProject, "_write_json", flaky)
    with pytest.raises(OSError, match="disk full"):
        import_pages(P(dst), P(src), ["overview", "details"])
    monkeypatch.undo()
    assert snapshot(dst) == before
    assert not (pages_dir(dst) / "overview-2").exists()


# --- import_visuals -----------------------------------------------------------------------------------------

def test_import_visuals_keeps_free_ids(pair):
    src, dst = pair
    res = import_visuals(P(dst), P(src), "overview", ["card1", "bar1"], "details")
    assert res["ok"] and res["id_map"] == {"card1": "card1", "bar1": "bar1"}
    assert res["count"] == 2 and res["page_id"] == "details"
    assert vjson(dst, "details", "card1") == vjson(src, "overview", "card1")
    assert_valid(dst)
    assert {v.id for v in P(dst).list_visuals("details")} == {"slicer1", "card1", "bar1"}


def test_import_visuals_new_ids_on_collision(pair):
    src, dst = pair
    r1 = import_visuals(P(dst), P(src), "overview", ["card1"], "overview")
    r2 = import_visuals(P(dst), P(src), "overview", ["card1", "table1"], "overview")
    assert r1["id_map"] == {"card1": "card1-2"}
    assert r2["id_map"] == {"card1": "card1-3", "table1": "table1-2"}
    assert vjson(dst, "overview", "card1-3")["name"] == "card1-3"
    assert (pages_dir(dst) / "overview" / "visuals" / "card1-3" / "visual.json").exists()
    assert_valid(dst)


def test_import_visuals_offset(pair):
    src, dst = pair
    import_visuals(P(dst), P(src), "overview", ["card1"], "details",
                   offset={"x": 10, "y": -5})
    pos = vjson(dst, "details", "card1")["position"]
    assert (pos["x"], pos["y"], pos["width"]) == (26, 11, 220)
    import_visuals(P(dst), P(src), "overview", ["bar1"], "details", offset={"x": 4})
    pos = vjson(dst, "details", "bar1")["position"]
    assert (pos["x"], pos["y"]) == (20, 140)
    assert_valid(dst)


def test_import_visuals_argument_errors(pair):
    src, dst = pair
    before = snapshot(dst)
    with pytest.raises(ValueError, match="at least one"):
        import_visuals(P(dst), P(src), "overview", [], "details")
    with pytest.raises(ValueError, match="nope"):
        import_visuals(P(dst), P(src), "overview", ["nope"], "details")
    with pytest.raises(ValueError, match="Target page"):
        import_visuals(P(dst), P(src), "overview", ["card1"], "ghost")
    with pytest.raises(ValueError, match="source report"):
        import_visuals(P(dst), P(src), "ghost", ["card1"], "details")
    for bad in ({"z": 1}, {"x": "a"}, {"x": True}, [1, 2]):
        with pytest.raises(ValueError, match="offset"):
            import_visuals(P(dst), P(src), "overview", ["card1"], "details", offset=bad)
    assert snapshot(dst) == before


def test_import_visuals_missing_fields(pair):
    src, dst = pair
    _drop_year_column(dst)
    with pytest.raises(ValueError, match="Date.Year"):
        import_visuals(P(dst), P(src), "overview", ["bar1"], "details")
    assert not (pages_dir(dst) / "details" / "visuals" / "bar1").exists()
    res = import_visuals(P(dst), P(src), "overview", ["bar1"], "details",
                         allow_missing_fields=True)
    assert res["missing_fields"][0]["field"] == "Date.Year"


def test_import_visuals_groups(pair):
    src, dst = pair
    gid = P(src).group_visuals("overview", ["card1", "bar1"], "G")
    res = import_visuals(P(dst), P(src), "overview", [gid], "details")
    assert set(res["id_map"]) == {gid, "card1", "bar1"}
    assert any("group member" in w for w in res["warnings"])
    assert vjson(dst, "details", "card1")["parentGroupName"] == gid
    assert_valid(dst)

    # importing the same group into the page it came from renames group + members
    same = import_visuals(P(src), P(src), "overview", [gid], "overview")
    new_gid = same["id_map"][gid]
    assert new_gid != gid
    assert vjson(src, "overview", same["id_map"]["card1"])["parentGroupName"] == new_gid
    assert_valid(src)

    # a member imported without its group is ungrouped
    res = import_visuals(P(dst), P(src), "overview", ["card1"], "overview")
    assert "parentGroupName" not in vjson(dst, "overview", res["id_map"]["card1"])
    assert any("ungrouped" in w for w in res["warnings"])


def test_import_visuals_copies_images(pair, tmp_path):
    src, dst = pair
    vid = P(src).add_image("overview", str(png(tmp_path)))
    res = import_visuals(P(dst), P(src), "overview", [vid], "details")
    assert res["resources"]["copied"] == ["logo.png"]
    assert (dst / "Synthetic.Report/StaticResources/RegisteredResources/logo.png").exists()
    assert_valid(dst)


# --- tools ----------------------------------------------------------------------------------------------------------

def _payload(result):
    structured = None
    if isinstance(result, tuple):
        result, structured = result
    structured = (structured or getattr(result, "structured_content", None)
                  or getattr(result, "structuredContent", None))
    if structured is not None:
        return structured.get("result", structured) \
            if isinstance(structured, dict) else structured
    content = getattr(result, "content", result)
    items = [json.loads(c.text) for c in content]
    return items[0] if len(items) == 1 else items


def test_tools_dry_run_write_and_undo(pair):
    import report_server.server as srv

    src, dst = pair
    tools = {t.name: t for t in asyncio.run(srv.mcp.list_tools())}
    for name in ("pbi_import_pages", "pbi_import_visuals"):
        ann = tools[name].annotations
        ro = ann.readOnlyHint if hasattr(ann, "readOnlyHint") else ann.read_only_hint
        assert ro is False
        schema = getattr(tools[name], "inputSchema", None) or tools[name].input_schema
        assert schema["properties"]["dry_run"]["type"] == "boolean"
        assert "dry_run" not in schema.get("required", [])
    schema = getattr(tools["pbi_import_pages"], "inputSchema", None) \
        or tools["pbi_import_pages"].input_schema
    assert schema["required"] == ["from_path", "page_ids"]

    def call(name, **args):
        return _payload(asyncio.run(srv.mcp.call_tool(name, args)))

    call("pbi_set_project", path=str(dst / "Synthetic.pbip"))
    before = snapshot(dst)
    src_path = str(src / "Synthetic.pbip")

    preview = call("pbi_import_pages", from_path=src_path, page_ids=["details"],
                   dry_run=True)
    assert preview["dry_run"] is True
    assert "Synthetic.Report/definition/pages/details-2/page.json" in preview["changes"]["added"]
    assert "Synthetic.Report/definition/pages/pages.json" in preview["changes"]["modified"]
    assert snapshot(dst) == before

    real = call("pbi_import_pages", from_path=src_path, page_ids=["details"],
                rename_map={"details": "Details Copy"}, position=0)
    assert real["ok"] and real["order"][0] == "details-copy"
    assert snapshot(dst) != before

    vis = call("pbi_import_visuals", from_path=src_path, page_id="overview",
               visual_ids=["card1"], target_page_id="overview",
               offset={"x": 1, "y": 1})
    assert vis["id_map"] == {"card1": "card1-2"}

    history = call("pbi_undo_history")
    assert [h["tool"] for h in history[:2]] == ["pbi_import_visuals", "pbi_import_pages"]
    call("pbi_undo", steps=2)
    assert snapshot(dst) == before

    # bad input surfaces as an error, not a crash
    try:
        res = asyncio.run(srv.mcp.call_tool(
            "pbi_import_pages", {"from_path": str(src / "nope"), "page_ids": ["x"]}))
    except Exception as exc:  # noqa: BLE001 - raised by mcp 1.x and 2.x
        chain, cur = [], exc
        while cur is not None and cur not in chain:
            chain.append(cur)
            cur = cur.__cause__ or cur.__context__
        assert "does not exist" in " | ".join(str(e) for e in chain)
    else:
        first = res[0] if isinstance(res, tuple) else res
        assert getattr(first, "isError", None) or getattr(first, "is_error", None)
