"""The website's generated data stays in step with the servers."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
ASSETS = REPO / "website" / "assets"


@pytest.fixture(scope="module")
def gen():
    spec = importlib.util.spec_from_file_location(
        "gen_site_assets", REPO / "scripts" / "gen_site_assets.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _data(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    return json.loads(text[text.index("{"): text.rindex("}") + 1])


def test_every_tool_is_in_exactly_one_family(gen):
    tools = gen.collect_tools()
    families = gen.build_families(tools)          # raises when a tool is unclassified
    seen = [t["name"] for f in families for t in f["tools"]]
    assert len(seen) == len(set(seen)) == len(tools)


def test_committed_site_data_is_current(gen):
    fresh = gen._without_tests(gen.render_js("PBI_SITE", gen.build_site_data(tests=0)))
    current = gen._without_tests((ASSETS / "site-data.js").read_text(encoding="utf-8"))
    assert current == fresh, "run scripts/gen_site_assets.py"


def test_site_data_numbers_are_sane():
    data = _data(ASSETS / "site-data.js")
    s = data["stats"]
    assert s["tools"] == sum(f["count"] for f in data["families"])
    assert s["families"] == len(data["families"]) >= 10
    assert s["tests"] > 1000 and s["bpa_rules"] > 30 and s["deneb_templates"] >= 10
    assert 0 < s["write_tools"] < s["tools"]
    assert re.fullmatch(r"\d+\.\d+", s["python_min"]) and re.fullmatch(r"\d+\.\d+", s["python_max"])
    for fam in data["families"]:
        for tool in fam["tools"]:
            assert tool["name"].startswith("pbi_") and tool["summary"] and tool["kind"] in ("read", "write", "destructive")


def test_demo_transcript_is_real_and_scrubbed():
    demo = _data(ASSETS / "demo-data.js")
    ids = [s["id"] for s in demo["steps"]]
    assert ids == ["preview", "apply", "undo"]
    preview, apply, undo = demo["steps"]
    assert preview["output"]["dry_run"] is True and "Revenue per Order" in preview["output"]["diff"]
    assert apply["output"]["ok"] is True
    assert undo["output"]["undone"][0]["tool"] == "pbi_create_measure"
    text = (ASSETS / "demo-data.js").read_text(encoding="utf-8")
    assert not re.search(r"[A-Za-z]:\\\\|/Users/|/home/|resod", text), "a local path leaked into the demo"


def test_page_references_only_files_that_exist():
    html = (REPO / "website" / "index.html").read_text(encoding="utf-8")
    for ref in re.findall(r'(?:href|src)="(assets/[^"]+)"', html):
        assert (REPO / "website" / ref).is_file(), ref
    css = (ASSETS / "site.css").read_text(encoding="utf-8")
    for ref in re.findall(r'url\("([^"]+)"\)', css):
        assert (ASSETS / ref).is_file(), ref
    assert 'src="assets/site-data.js"' in html and 'src="assets/demo-data.js"' in html
