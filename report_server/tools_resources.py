"""MCP resources and prompts for the report server.

Resources expose read-only views of the selected project as JSON text so a
host can attach them to a conversation without a tool call:

    pbip://pages                 pages in report order
    pbip://pages/{page_id}       one page with its visuals and bindings
    pbip://capabilities          visual types, buckets, filters, workflow
    pbip://theme                 the report's active theme

Every project-backed resource answers with ``{"error": ...}`` (never an
exception) when no project is selected; ``pbip://capabilities`` needs none.
Prompts (``build_dashboard``, ``theme_report``, ``review_page``) hand the
model a step-by-step recipe that names the exact tools to call.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from urllib.parse import unquote

from core.pbir import visual_bindings

if TYPE_CHECKING:  # never import report_server.server at import time
    from report_server.server import ReportState

NO_PROJECT = ("No project selected. Call pbi_set_project(path) with a .pbip "
              "file or project folder first, then read this resource again.")


# --- resource logic (plain functions; unit-tested directly) ------------------

def _json(obj) -> str:
    return json.dumps(obj, indent=2, default=str)


def _safe(state: ReportState, build) -> str:
    """JSON text of ``build(project)``, or a JSON error object."""
    if state.project is None:
        return _json({"error": NO_PROJECT})
    try:
        return _json(build(state.project))
    except Exception as exc:  # noqa: BLE001 - a resource read must not raise
        return _json({"error": f"{type(exc).__name__}: {exc}"})


def pages_overview(state: ReportState) -> str:
    def build(project):
        pages = [p.model_dump() for p in project.list_pages()]
        return {"count": len(pages), "pages": pages,
                "see_also": ["pbip://pages/{page_id}", "pbip://theme",
                             "pbip://capabilities"]}
    return _safe(state, build)


def page_detail(state: ReportState, page_id: str) -> str:
    def build(project):
        pages = {p.id: p for p in project.list_pages()}
        pid = page_id if page_id in pages else unquote(page_id)
        if pid not in pages:
            return {"error": f"Page {page_id!r} not found.",
                    "available": sorted(pages)}
        visuals = []
        for v in project.list_visuals(pid):
            d = v.model_dump(exclude={"raw"})
            d["bindings"] = visual_bindings(v)
            visuals.append(d)
        return {"page": pages[pid].model_dump(), "visual_count": len(visuals),
                "visuals": visuals}
    return _safe(state, build)


def capabilities_doc() -> str:
    """Static: needs no project."""
    from core.capabilities import capabilities

    return _json(capabilities())


def theme_info(state: ReportState) -> str:
    """Active theme: base theme name, custom theme JSON and its accent."""
    def build(project):
        report_dir = project._require_report()
        report_json = report_dir / "definition" / "report.json"
        collection: dict = {}
        if report_json.exists():
            data = json.loads(report_json.read_text(encoding="utf-8-sig"))
            collection = data.get("themeCollection") or {}
        base = collection.get("baseTheme")
        custom = collection.get("customTheme")
        out: dict = {
            "base_theme": base.get("name") if isinstance(base, dict) else base,
            "custom_theme": None,
            "accent": project.report_accent(),
        }
        if custom:
            resource = report_dir / "StaticResources" / "RegisteredResources" \
                / custom.get("name", "")
            entry: dict = {"resource": custom.get("name"), "theme": None}
            if resource.is_file():
                entry["theme"] = json.loads(
                    resource.read_text(encoding="utf-8-sig"))
            out["custom_theme"] = entry
        return out
    return _safe(state, build)


# --- prompt text ---------------------------------------------------------------

BUILD_DASHBOARD = """\
Build a Power BI report for this goal: {goal}

Steps
1. pbi_set_project(path), then pbi_doctor(). Fix any finding with severity \
'error' first.
2. pbi_capabilities() - the exact visual types, bucket names and filter \
kinds. Do not guess bucket names (the Legend role is 'Series'; combo charts \
use 'Y' and 'Y2'; donuts have no 'Category').
3. pbi_profile_model() - fact, dimension and date tables, measure roles and \
grouping columns. Choose KPIs and breakdowns from fields that really exist.
4. Fastest path: pbi_scaffold_report(dry_run=true) returns the proposed \
pages (KPI strip, trend, breakdown, table, per-dimension detail pages and a \
nav bar). Show me the proposal, adjust it to the goal, then run \
pbi_scaffold_report(accent="#RRGGBB") for real. It never overwrites an \
existing theme.
   Custom path: pbi_generate_theme(brand="#RRGGBB", install=false), then \
pbi_set_report_theme(theme=<returned theme>); then one \
pbi_build_designed_page(name, title, subtitle, kpis=[{{"measure": \
"Table.Measure", "title": "..."}}], charts=[{{"visual_type": ..., \
"bindings": {{"Category": ["Table.Field"], "Y": ["Table.Measure"]}}, \
"title": ...}}]) per page. Add pbi_add_nav_button(page_id, label, \
target_page_id) between pages.
5. For every page run pbi_lint_page(page_id) and fix overlaps or off-canvas \
visuals with pbi_move_visual(page_id, visual_id, x, y, width, height).
6. pbi_validate_project() must return ok: true.
7. Tell me to reopen the .pbip in Power BI Desktop, the one check that \
cannot be automated.

Safety
- Every write tool accepts dry_run=true: it returns the unified diff and \
writes nothing (pbi_scaffold_report's dry_run returns its proposal instead).
- pbi_undo() reverts the latest write; pbi_undo_history() lists what can be \
undone.
- For a multi-page build call pbi_begin_transaction() first, then \
pbi_commit() to keep it or pbi_rollback() to discard all of it.
"""

THEME_REPORT = """\
Re-theme the report to the brand colour {brand_color}.

Steps
1. pbi_set_project(path). pbi_list_pages(), then pbi_page_style(page_id) on \
one page to see the current header and KPI look.
2. pbi_generate_theme(brand="{brand_color}", mode="light", install=false) \
(use mode="dark" if I asked for a dark report). This only previews the \
palette, text classes and visual styles.
3. Install it through a write tool so it can be previewed and undone: \
pbi_set_report_theme(theme=<the theme object returned in step 2>, \
dry_run=true) shows the diff; run it again without dry_run to apply. \
(pbi_generate_theme with install=true also installs, but bypasses dry_run \
and the undo journal.)
4. Pages with an explicit canvas colour: pbi_style_page(page_id, \
background_color="#RRGGBB", wallpaper_color="#RRGGBB").
5. Visuals with hard-coded colours ignore the theme. Find them with \
pbi_list_visuals(page_id) and pbi_get_visual(page_id, visual_id), then \
recolour with pbi_format_visual(page_id, visual_id, target="visual", \
objects={{...}}).
6. pbi_lint_page(page_id) for every page, then pbi_validate_project() \
(must be ok: true).
7. Show me a before/after summary: theme name, accent colour and the pages \
or visuals you touched by hand.

Recovery: pbi_undo() reverts the theme install (pbi_undo_history() lists \
it), and pbi_begin_transaction() / pbi_rollback() reverts a whole restyle \
made of several writes.
"""

REVIEW_PAGE = """\
Review the report page {page_id} for design and correctness. Read-only: \
propose fixes and wait for my approval before changing anything.

Steps
1. pbi_set_project(path) if not already done. pbi_list_pages() confirms \
{page_id} exists (page ids are not display names).
2. pbi_list_visuals("{page_id}") - every visual's type, position, title and \
field bindings. pbi_get_visual("{page_id}", visual_id) for any that need a \
closer look.
3. pbi_lint_page("{page_id}") - overlaps, off-canvas visuals, too-small \
visuals and near-misaligned edges.
4. pbi_page_style("{page_id}") - the header band, KPI card and backplate \
composition, to compare with the report's other pages.
5. pbi_validate_project() - are all report files schema-valid?
6. pbi_model_usage() - are the fields this page binds actually in the \
model, and is anything on the page unused?
7. Check: one accent colour, KPI strip on top, charts on a grid, titles that \
say what is measured, sensible sort order (pbi_sort_visual), and no visual \
bound to a field that no longer exists.
8. Reply with a prioritised list: what is wrong, why it matters, and the \
exact tool call that fixes it (pbi_move_visual, pbi_format_visual, \
pbi_update_bindings, pbi_delete_visual, ...).

If I approve fixes
- Preview each with dry_run=true first, then apply it.
- pbi_undo() reverts the latest fix; pbi_delete_visual is recoverable from \
the trash (pbi_list_trash, pbi_restore_visual).
- Finish with pbi_lint_page("{page_id}") and pbi_validate_project() again.
"""


def build_dashboard_text(goal: str) -> str:
    return BUILD_DASHBOARD.format(goal=goal.strip() or "(none given - ask me)")


def theme_report_text(brand_color: str) -> str:
    return THEME_REPORT.format(brand_color=brand_color.strip() or "#1F3A5F")


def review_page_text(page_id: str) -> str:
    return REVIEW_PAGE.format(page_id=page_id.strip())


# --- registration ---------------------------------------------------------------

def register(mcp, state, tool) -> None:
    @mcp.resource("pbip://pages", name="report_pages",
                  description="Report pages in order: id, name, size, "
                              "visual count and hidden flag.",
                  mime_type="application/json")
    def pages_resource() -> str:
        return pages_overview(state)

    @mcp.resource("pbip://pages/{page_id}", name="report_page",
                  description="One page with every visual: type, position, "
                              "title and field bindings.",
                  mime_type="application/json")
    def page_resource(page_id: str) -> str:
        return page_detail(state, page_id)

    @mcp.resource("pbip://capabilities", name="report_capabilities",
                  description="What the report server can build: visual "
                              "types and buckets, filters, formatting, "
                              "design elements, workflow and an example.",
                  mime_type="application/json")
    def capabilities_resource() -> str:
        return capabilities_doc()

    @mcp.resource("pbip://theme", name="report_theme",
                  description="The report's active theme: base theme, custom "
                              "theme JSON and accent colour.",
                  mime_type="application/json")
    def theme_resource() -> str:
        return theme_info(state)

    @mcp.prompt()
    def build_dashboard(goal: str) -> str:
        """Build a themed, navigable, schema-valid report for a stated goal,
        starting with a previewable scaffold."""
        return build_dashboard_text(goal)

    @mcp.prompt()
    def theme_report(brand_color: str) -> str:
        """Re-theme the whole report to a brand colour, previewable and
        undoable."""
        return theme_report_text(brand_color)

    @mcp.prompt()
    def review_page(page_id: str) -> str:
        """Review one report page for design and correctness; proposes fixes
        and waits for approval."""
        return review_page_text(page_id)
