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


def test_committed_site_data_is_current(gen, capsys):
    assert gen.main(["--check"]) == 0, capsys.readouterr().out + " (run scripts/gen_site_assets.py)"


def test_numbers_are_stamped_into_the_html(gen):
    """The page shows the right numbers before, or without, JavaScript."""
    stats = _data(ASSETS / "site-data.js")["stats"]
    html = (REPO / "website" / "index.html").read_text(encoding="utf-8")
    found = dict(re.findall(r'data-stat="(\w+)"[^>]*>([^<]*)<', html))
    assert {"tools", "tests", "families", "bpa_rules", "schemas", "deneb_templates", "read_tools"} <= set(found)
    for key, text in re.findall(r'data-stat="(\w+)"[^>]*>([^<]*)<', html):
        assert text == f"{stats[key]:,}", (key, text)
    assert stats["read_tools"] + stats["write_tools"] == stats["tools"]
    assert gen.stamp_html('<b data-stat="tools">0</b> <i data-stat="python_min">x</i>', {"tools": 1234, "python_min": "3.11"}) \
        == '<b data-stat="tools">1,234</b> <i data-stat="python_min">x</i>'


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


# --- strictly Poppins ------------------------------------------------------------------

def _site_text() -> dict[str, str]:
    web = REPO / "website"
    return {rel: (web / rel).read_text(encoding="utf-8")
            for rel in ("index.html", "assets/site.css", "assets/site.js",
                        "assets/site-data.js", "assets/demo-data.js")}


def test_poppins_is_the_only_typeface():
    css = (ASSETS / "site.css").read_text(encoding="utf-8")
    faces = re.findall(r'@font-face\s*\{[^}]*font-family:\s*"([^"]+)"', css)
    assert faces and set(faces) == {"Poppins"}
    assert "font-display: swap" not in css          # block: no other face may flash
    values = {v.strip() for v in re.findall(r"font-family:\s*([^;}]+)", css)}
    assert values <= {'"Poppins"', "inherit", "var(--font)"}, values
    assert re.search(r'--font:\s*"Poppins",\s*sans-serif;', css)
    for name, text in _site_text().items():
        assert "font-family" not in text or name.endswith("site.css"), name
        assert not re.search(r"monospace|JetBrains|Grotesk|Consolas|Segoe|system-ui", text), name
    # and nothing but Poppins is shipped
    fonts = sorted(p.name for p in (ASSETS / "fonts").iterdir())
    assert fonts == ["OFL-Poppins.txt"] + [f"poppins-{w}-latin.woff2" for w in (400, 500, 600, 700, 800, 900)]


def test_every_character_on_the_site_exists_in_poppins(gen):
    """A character outside the font renders in a fallback face: icons are drawn
    as SVG or CSS shapes, never typed."""
    for name, text in _site_text().items():
        decoded = text
        decoded += "".join(chr(int(h, 16)) for h in re.findall(r'content:\s*"\\([0-9A-Fa-f]{2,6})', text))
        decoded += "".join(chr(int(h, 16)) for h in re.findall(r"\\u([0-9A-Fa-f]{4})", text))
        decoded += "".join(chr(int(d)) for d in re.findall(r"&#(\d+);", text))
        decoded += "".join(chr(int(h, 16)) for h in re.findall(r"&#x([0-9A-Fa-f]+);", text))
        bad = gen.outside_poppins(decoded)
        assert not bad, f"{name}: {[f'U+{ord(c):04X}' for c in bad]} cannot be drawn by Poppins"
    assert not re.search(r"&(?!amp;|lt;|gt;|quot;|middot;|nbsp;|#)\w+;", _site_text()["index.html"]), \
        "named HTML entity: check that Poppins has the glyph"


def test_generator_refuses_characters_outside_poppins(gen):
    assert gen.outside_poppins("plain text, 123 · … —") == []
    assert gen.outside_poppins("check ✓ arrow →") == ["→", "✓"]
    assert gen._summary("Moves A → B. More.") == "Moves A -> B."
    with pytest.raises(SystemExit, match="Poppins cannot draw"):
        gen.render_js("X", {"text": "◐"})


def test_page_references_only_files_that_exist():
    html = (REPO / "website" / "index.html").read_text(encoding="utf-8")
    for ref in re.findall(r'(?:href|src)="(assets/[^"]+)"', html):
        assert (REPO / "website" / ref).is_file(), ref
    css = (ASSETS / "site.css").read_text(encoding="utf-8")
    for ref in re.findall(r'url\("([^"]+)"\)', css):
        if not ref.startswith("data:"):               # inline SVG icons are not files
            assert (ASSETS / ref).is_file(), ref
    assert 'src="assets/site-data.js"' in html and 'src="assets/demo-data.js"' in html
