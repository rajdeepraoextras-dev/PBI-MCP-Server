#!/usr/bin/env python
"""Generate the website's data files from the live code, so the site never lies.

Writes into ``website/assets/``:

* ``site-data.js``  ``window.PBI_SITE``: version, the numbers the page shows
  (tools, families, tests, best-practice rules, Deneb templates, schemas,
  Python versions), and every tool grouped into families with a one-line
  summary and its read/write/destructive kind.
* ``demo-data.js``  ``window.PBI_DEMO``: a real preview / apply / undo
  transcript recorded against a scratch copy of the engine fixture.

Usage::

    python scripts/gen_site_assets.py           # regenerate both files
    python scripts/gen_site_assets.py --check   # exit 1 if the tool list in
                                                # site-data.js is stale

Neither file needs Power BI Desktop or the network. ``--check`` ignores the
test count, which changes with every new test.
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import importlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ASSETS = REPO / "website" / "assets"
ENGINE_FIXTURE = REPO / "tests" / "fixtures" / "engine"

if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

SERVERS = (
    ("model", "model_server.server"),
    ("report", "report_server.server"),
    ("service", "service_server.server"),
)

# Family id, title, blurb, tool names (without the pbi_ prefix). Every tool must
# appear in exactly one family; the generator refuses to run when a new tool is
# not classified, so the site never silently omits one.
FAMILIES: list[tuple[str, str, str, list[str]]] = [
    ("start", "Start here", "Open a project and see what it can do.",
     ["set_project", "doctor", "capabilities", "project_summary"]),
    ("build", "Build reports",
     "Scaffold a whole report in one call, or compose pages, visuals and navigation.",
     ["scaffold_report", "build_page", "build_designed_page", "profile_model",
      "create_page", "add_visual", "add_visual_raw", "update_bindings", "move_visual",
      "delete_visual", "duplicate_page", "rename_page", "hide_page", "reorder_pages",
      "delete_page", "group_visuals", "add_text", "add_image", "add_shape",
      "add_nav_button", "create_bookmark", "set_page_role", "set_visual_interactions",
      "sort_visual", "import_pages", "import_visuals", "list_pages", "list_visuals",
      "get_visual"]),
    ("style", "Style and format",
     "Themes, conditional formatting, analytics lines, slicers, labels, filters and Deneb templates.",
     ["generate_theme", "set_report_theme", "theme_from_image", "style_page", "page_style",
      "format_visual", "set_conditional_format", "clear_conditional_format",
      "add_analytics_line", "list_analytics_lines", "remove_analytics_line",
      "set_tooltip_page", "set_slicer", "set_data_labels", "add_visual_calculation",
      "list_visual_calculations", "remove_visual_calculation", "add_filter",
      "list_filters", "remove_filter", "list_deneb_templates", "add_deneb_visual",
      "set_deneb_spec"]),
    ("check", "Preview and check",
     "Draw a page without opening Desktop, lint it, validate the project, diff two versions.",
     ["render_page", "render_report", "lint_page", "validate_project", "semantic_diff",
      "project_diff", "model_usage"]),
    ("access", "Accessibility and mobile",
     "Alt text, tab order, contrast checks and phone layouts.",
     ["accessibility_report", "set_alt_text", "auto_alt_text", "set_tab_order",
      "auto_tab_order", "set_mobile_layout", "get_mobile_layout"]),
    ("dax", "Measures and DAX",
     "Create, rename and refactor measures with lineage that follows the DAX, plus a formatter.",
     ["create_measure", "update_measure", "delete_measure", "bulk_create_measures",
      "list_measures", "get_model", "model_lineage", "find_references", "dax_references",
      "format_dax", "format_measures", "rename_measure", "rename_column", "rename_table",
      "set_measure_properties", "remove_kpi", "create_report_measure",
      "list_report_measures", "update_report_measure", "delete_report_measure"]),
    ("tables", "Tables and queries",
     "Tables, partitions, shared queries, parameters and incremental refresh.",
     ["create_table", "create_calculated_table", "update_table", "list_partitions",
      "update_partition", "delete_table", "create_expression", "list_expressions",
      "update_expression", "set_refresh_policy", "remove_refresh_policy"]),
    ("columns", "Columns, hierarchies, relationships",
     "Column properties, hierarchies, relationships and calculation groups.",
     ["list_columns", "create_column", "update_column", "delete_column",
      "create_hierarchy", "list_hierarchies", "delete_hierarchy", "create_relationship",
      "create_calc_group"]),
    ("security", "Security and metadata",
     "Row and object security, perspectives, translations, field and what-if parameters.",
     ["create_role", "update_role", "list_roles", "delete_role", "set_column_permission",
      "set_table_permission_metadata", "create_perspective", "update_perspective",
      "list_perspectives", "delete_perspective", "add_culture", "set_translation",
      "list_translations", "delete_culture", "create_field_parameter",
      "list_field_parameters", "create_whatif_parameter", "list_whatif_parameters"]),
    ("quality", "Quality",
     "A best-practice analyzer with safe fixers, ported from the Tabular Editor rules.",
     ["bpa", "bpa_fix", "bpa_rules"]),
    ("engine", "Live engine",
     "Run and validate DAX against a Power BI Desktop that is open. Windows only.",
     ["engine_status", "engine_connect", "evaluate_dax", "validate_dax",
      "table_row_counts", "column_stats", "preview_table"]),
    ("safety", "Safety net",
     "Undo history, transactions, backups and a recoverable trash.",
     ["undo", "undo_history", "begin_transaction", "commit", "rollback", "list_backups",
      "restore_backup", "list_trash", "restore_visual"]),
    ("cloud", "Cloud, optional",
     "Publish, refresh, deploy and export through the Fabric and Power BI REST APIs.",
     ["service_status", "service_login", "list_workspaces", "list_items",
      "get_item_definition", "publish_project", "refresh_dataset", "refresh_status",
      "list_deployment_pipelines", "deploy_pipeline_stage", "export_report"]),
]


# --- strictly Poppins ---------------------------------------------------------------
# The site is set in Poppins and nothing else. These are the codepoints present in
# all six self-hosted weights (read from the font files' cmap tables). A character
# outside them would silently render in a fallback font, so the generator maps the
# common offenders to ASCII and refuses to emit anything else.
POPPINS_RANGES = [(0x0020, 0x007E), (0x00A0, 0x00FF), (0x0131, 0x0131), (0x0152, 0x0153),
                  (0x02BC, 0x02BC), (0x02C6, 0x02C6), (0x02DA, 0x02DA), (0x02DC, 0x02DC),
                  (0x2013, 0x2014), (0x2018, 0x201A), (0x201C, 0x201E), (0x2022, 0x2022),
                  (0x2026, 0x2026), (0x2039, 0x203A), (0x2044, 0x2044), (0x20AC, 0x20AC),
                  (0x2122, 0x2122), (0x2212, 0x2212), (0x2215, 0x2215)]
_PLAIN = str.maketrans({
    "→": "->", "←": "<-", "↔": "<->", "⇒": "=>",
    "≥": ">=", "≤": "<=", "≠": "!=", "≈": "~",
    "✓": "", "✔": "", "✗": "", "‑": "-", "‐": "-", " ": " ",
})


def outside_poppins(text: str) -> list[str]:
    """Characters in `text` that Poppins cannot draw (line breaks and tabs are fine)."""
    return sorted({ch for ch in text if ch not in "\n\r\t"
                   and not any(a <= ord(ch) <= b for a, b in POPPINS_RANGES)})


# --- tools ------------------------------------------------------------------------

def _hint(annotations, camel: str) -> bool | None:
    """Read a ToolAnnotations hint on either mcp major (camelCase or snake_case)."""
    if annotations is None:
        return None
    snake = re.sub(r"([A-Z])", lambda m: "_" + m.group(1).lower(), camel)
    for name in (camel, snake):
        if hasattr(annotations, name):
            return getattr(annotations, name)
    return None


def _summary(description: str | None, limit: int = 110) -> str:
    """First sentence of a tool description, cut at a word boundary if long."""
    text = " ".join((description or "").translate(_PLAIN).split())
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    first = m.group(1) if m else text
    if len(first) > limit:
        cut = first[:limit].rsplit(" ", 1)[0].rstrip(" ,;:(+-—")
        first = cut + "…"
    return first


def collect_tools() -> dict[str, dict]:
    """Every distinct tool: summary, kind and the servers that expose it."""
    tools: dict[str, dict] = {}
    for stem, module in SERVERS:
        mod = importlib.import_module(module)
        for t in asyncio.run(mod.mcp.list_tools()):
            name = t.name.removeprefix("pbi_")
            ann = t.annotations
            if _hint(ann, "readOnlyHint"):
                kind = "read"
            elif _hint(ann, "destructiveHint"):
                kind = "destructive"
            else:
                kind = "write"
            entry = tools.setdefault(name, {"name": t.name, "summary": _summary(t.description),
                                            "kind": kind, "servers": []})
            entry["servers"].append(stem)
    return tools


def build_families(tools: dict[str, dict]) -> list[dict]:
    assigned: dict[str, str] = {}
    for fid, _, _, names in FAMILIES:
        for n in names:
            if n in assigned:
                raise SystemExit(f"tool {n!r} is in two families: {assigned[n]} and {fid}")
            assigned[n] = fid
    unassigned = sorted(set(tools) - set(assigned))
    unknown = sorted(set(assigned) - set(tools))
    if unassigned or unknown:
        raise SystemExit("site families are out of date. Unclassified tools: "
                         f"{unassigned}; listed but missing: {unknown}. "
                         "Edit FAMILIES in scripts/gen_site_assets.py.")
    return [{"id": fid, "title": title, "blurb": blurb, "count": len(names),
             "tools": [tools[n] for n in names]}
            for fid, title, blurb, names in FAMILIES]


# --- numbers ------------------------------------------------------------------------

def count_tests() -> int:
    """Tests pytest collects, as the suite's own count of them."""
    proc = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q",
                           "-p", "no:cacheprovider"],
                          cwd=REPO, capture_output=True, text=True, timeout=600)
    m = re.search(r"^(\d+) tests? collected", proc.stdout, re.M)
    if not m:
        raise SystemExit("could not count tests:\n" + proc.stdout[-800:] + proc.stderr[-800:])
    return int(m.group(1))


def build_stats(tools: dict[str, dict], families: list[dict], *, tests: int | None) -> dict:
    with open(REPO / "pyproject.toml", "rb") as fh:
        project = tomllib.load(fh)["project"]
    pythons = sorted({c.rsplit("::", 1)[-1].strip() for c in project.get("classifiers", [])
                      if re.match(r"Programming Language :: Python :: 3\.\d+$", c)},
                     key=lambda v: [int(x) for x in v.split(".")])
    rules = json.loads((REPO / "resources" / "bpa_rules.json").read_text(encoding="utf-8"))
    rules = rules.get("rules", rules) if isinstance(rules, dict) else rules
    from core import deneb
    return {
        "tools": len(tools),
        "registrations": sum(len(t["servers"]) for t in tools.values()),
        "per_server": {stem: sum(1 for t in tools.values() if stem in t["servers"])
                       for stem, _ in SERVERS},
        "servers": len(SERVERS),
        "families": len(families),
        "write_tools": sum(1 for t in tools.values() if t["kind"] != "read"),
        "read_tools": sum(1 for t in tools.values() if t["kind"] == "read"),
        "tests": tests,
        "bpa_rules": len(rules),
        "deneb_templates": len(deneb.TEMPLATES),
        "schemas": len(glob.glob(str(REPO / "resources" / "schemas" / "*_schema_json.json"))),
        "python_min": pythons[0] if pythons else project.get("requires-python", ""),
        "python_max": pythons[-1] if pythons else "",
        "python_versions": len(pythons),
        "mcp_majors": 2,
    }


def build_site_data(*, tests: int | None) -> dict:
    tools = collect_tools()
    families = build_families(tools)
    with open(REPO / "pyproject.toml", "rb") as fh:
        version = tomllib.load(fh)["project"]["version"]
    return {"version": version, "stats": build_stats(tools, families, tests=tests),
            "families": families}


def render_js(var: str, data: dict) -> str:
    js = (f"// Generated by scripts/gen_site_assets.py. Do not edit by hand.\n"
          f"window.{var} = {json.dumps(data, indent=1, ensure_ascii=False)};\n")
    bad = outside_poppins(js)
    if bad:
        raise SystemExit(f"{var}: characters Poppins cannot draw: "
                         f"{[f'U+{ord(c):04X}' for c in bad]}. Replace them in the tool "
                         "docstring, or map them in _PLAIN in scripts/gen_site_assets.py.")
    return js


def stamp_html(html: str, stats: dict) -> str:
    """Write each number into its ``data-stat`` element, so the page is right
    even before (or without) JavaScript: ``<b data-stat="tools">158</b>``."""
    def repl(m: re.Match) -> str:
        value = stats.get(m.group(2))
        if not isinstance(value, int):
            return m.group(0)
        return f"{m.group(1)}{value:,}{m.group(3)}"
    return re.sub(r'(<[^>]*\bdata-stat="(\w+)"[^>]*>)[^<]*(<)', repl, html)


# --- demo -------------------------------------------------------------------------

def _payload(result):
    """Tool result to python, across mcp majors."""
    structured = None
    if isinstance(result, tuple):
        result, structured = result
    structured = (structured or getattr(result, "structured_content", None)
                  or getattr(result, "structuredContent", None))
    if structured is not None:
        return structured.get("result", structured) if isinstance(structured, dict) else structured
    content = getattr(result, "content", result)
    items = [json.loads(c.text) for c in content]
    return items[0] if len(items) == 1 else items


def _call(mod, tool_name: str, **args):
    return _payload(asyncio.run(mod.mcp.call_tool(tool_name, args)))


def build_demo(work: Path) -> dict:
    import model_server.server as model

    proj = work / "engine"
    shutil.copytree(ENGINE_FIXTURE, proj)
    pbip = str(proj / "Engine.pbip")
    scrub = {str(proj): "<project>", str(proj).replace("\\", "/"): "<project>"}

    def clean(obj):
        text = json.dumps(obj, ensure_ascii=False)
        for k, v in scrub.items():
            text = text.replace(json.dumps(k)[1:-1], v)
        return json.loads(text)

    _call(model, "pbi_set_project", path=pbip)
    args = dict(table="Sales", name="Revenue per Order",
                dax="DIVIDE([Total Amount], [Order Count])", format="#,0.00")
    call_text = ('pbi_create_measure(table="Sales", name="Revenue per Order",\n'
                 '    dax="DIVIDE([Total Amount], [Order Count])", format="#,0.00"{})')
    preview = _call(model, "pbi_create_measure", dry_run=True, **args)
    applied = _call(model, "pbi_create_measure", **args)
    history = _call(model, "pbi_undo_history", limit=5)
    undone = _call(model, "pbi_undo")
    return {"steps": [
        {"id": "preview", "title": "Preview",
         "call": call_text.format(", dry_run=true"), "output": clean(preview)},
        {"id": "apply", "title": "Apply",
         "call": call_text.format(""), "output": clean(applied)},
        {"id": "undo", "title": "Undo",
         "call": "pbi_undo()", "output": clean(undone), "history": clean(history)},
    ]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true",
                    help="only verify the tool list in site-data.js is current (exit 1 if stale)")
    args = ap.parse_args(argv)
    target = ASSETS / "site-data.js"
    index = REPO / "website" / "index.html"
    committed = target.read_text(encoding="utf-8") if target.exists() else ""
    html = index.read_text(encoding="utf-8")

    if args.check:
        # The test count moves with every new test, so keep the committed one
        # and compare everything else exactly.
        m = re.search(r'"tests": (\d+)', committed)
        data = build_site_data(tests=int(m.group(1)) if m else 0)
        stale = []
        if render_js("PBI_SITE", data) != committed:
            stale.append("website/assets/site-data.js")
        if stamp_html(html, data["stats"]) != html:
            stale.append("website/index.html")
        if stale:
            print(f"stale: {', '.join(stale)}; run scripts/gen_site_assets.py")
            return 1
        print("site data and the numbers in index.html are up to date")
        return 0

    ASSETS.mkdir(parents=True, exist_ok=True)
    data = build_site_data(tests=count_tests())
    target.write_text(render_js("PBI_SITE", data), encoding="utf-8", newline="\n")
    print("wrote", target.relative_to(REPO), "|", json.dumps(data["stats"]))
    index.write_text(stamp_html(html, data["stats"]), encoding="utf-8", newline="\n")
    print("stamped the numbers into website/index.html")

    work = Path(tempfile.mkdtemp(prefix="pbisite-"))
    try:
        demo = build_demo(work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    (ASSETS / "demo-data.js").write_text(render_js("PBI_DEMO", demo), encoding="utf-8", newline="\n")
    print("wrote website/assets/demo-data.js")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
