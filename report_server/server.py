"""Report-layer MCP server.

Tools (Part C.2) — prefix every tool `pbi_`.

Day 14 (this file):  pbi_set_project, pbi_list_pages, pbi_list_visuals,
                     pbi_get_visual
Day 16:              pbi_model_usage
Day 19+ (write):     pbi_create_page, pbi_add_visual, pbi_format_visual,
                     pbi_update_bindings, pbi_set_report_theme, pbi_add_filter,
                     pbi_delete_visual, pbi_move_visual

Same design as the model server: tool logic in plain functions over an
explicit ReportState; thin FastMCP wrappers at the bottom.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.mcp_compat import Server

from core.pbip import PbipProject
from core.tooling import load_tool_modules, make_tool, register_journal_tools
from core.pbir import visual_bindings


@dataclass
class ReportState:
    """Holds the project selected via pbi_set_project for the session."""
    project: PbipProject | None = field(default=None)
    greeted: bool = field(default=False)

    def require(self) -> PbipProject:
        if self.project is None:
            raise ValueError("No project set — call pbi_set_project(path) first.")
        return self.project


# --- tool logic --------------------------------------------------------------

def set_project(state: ReportState, path: str) -> dict:
    """Select the .pbip project all subsequent report tools operate on."""
    project = PbipProject(path)
    project._require_report()  # fail fast if no Report layer resolves
    state.project = project
    pages = project.list_pages()
    result = {
        "ok": True,
        "path": str(project.path),
        "pages": len(pages),
        "visuals": sum(p.visual_count for p in pages),
    }
    if not state.greeted:
        result["greeting"] = GREETING
        state.greeted = True
    return result


def list_pages(state: ReportState) -> list[dict]:
    """Pages in report order: id, name, size, visual count, hidden flag."""
    return [p.model_dump() for p in state.require().list_pages()]


def list_visuals(state: ReportState, page_id: str) -> list[dict]:
    """Visuals on a page: id, type, position, title, bindings summary."""
    out = []
    for v in state.require().list_visuals(page_id):
        d = v.model_dump(exclude={"raw"})
        d["bindings"] = visual_bindings(v)
        out.append(d)
    return out


def get_visual(state: ReportState, page_id: str, visual_id: str) -> dict:
    """One visual's full config, including the raw visual.json."""
    v = state.require().get_visual(page_id, visual_id)
    d = v.model_dump()
    d["bindings"] = visual_bindings(v)
    return d


def model_usage(state: ReportState) -> dict:
    """Classify every model field as direct / indirect / unused in the report."""
    from core.usage import classify_usage

    return classify_usage(state.require())


def _validate_refs_exist(project: PbipProject,
                         bindings: dict[str, list[str]]) -> None:
    """Every queryRef must resolve to a real model measure or column."""
    measures = {(m.table, m.name) for m in project.list_measures()}
    columns = {(t.name, c.name) for t in project.list_tables()
               for c in t.columns}
    import re as _re
    agg = _re.compile(r"^\w+\(([^)]+)\)$")
    bad = []
    for refs in bindings.values():
        for ref in refs:
            m = agg.match(ref.strip())
            target = m.group(1) if m else ref
            entity, _, prop = target.partition(".")
            if (entity, prop) not in measures and (entity, prop) not in columns:
                bad.append(ref)
    if bad:
        raise KeyError(f"Unknown model field(s): {bad} — use Table.Field "
                       f"names that exist in the semantic model.")


def create_page(state: ReportState, name: str,
                width: float = 1280, height: float = 720) -> dict:
    """Create a new page; returns its pageId."""
    page_id = state.require().create_page(name, width, height)
    return {"ok": True, "page_id": page_id, "name": name}


def build_page(state: ReportState, name: str, visuals: list[dict],
               width: float | None = None, height: float | None = None) -> dict:
    """One-call flow: create a page and add visuals with grid auto-layout.

    Uses the 12-column layout engine (KPI band + body grid, page auto-extends)
    for visuals without an explicit position. Width/height default to the
    existing pages' size so a new page matches the report's orientation.
    """
    from core.layout import layout_page

    dw, dh = state.require().default_page_size()
    width = width or dw
    height = height or dh
    _, page_height = layout_page(visuals, width=width, height=height)
    page = create_page(state, name, width, page_height)
    res = add_visual(state, page["page_id"], visuals)
    return {"ok": True, "page_id": page["page_id"],
            "visual_ids": res["visual_ids"], "page_height": page_height}


def build_designed_page(state: ReportState, name: str, title: str,
                        subtitle: str | None = None,
                        kpis: list[dict] | None = None,
                        charts: list[dict] | None = None,
                        template: str = "exec-summary",
                        accent: str | None = None,
                        width: float | None = None,
                        match_page: str | None = None) -> dict:
    """Compose a designed page from a template: header band, KPI strip on
    backplates, chart grid. Executes the plan through validated primitives.

    When `accent`/`width` are omitted they are inherited from the report's
    existing theme + page size. Pass `match_page` (an existing page id) to also
    copy its header/KPI composition (band height + color, title font, card
    height, backplate) so the new page matches it closely.
    """
    from core.templates import TEMPLATES

    if template not in TEMPLATES:
        raise ValueError(f"Unknown template {template!r}; have {sorted(TEMPLATES)}")
    project = state.require()
    kpis = kpis or []
    charts = charts or []

    page_style = None
    if match_page is not None:
        from core.page_style import extract_page_style
        page_style = extract_page_style(project, match_page)
        if accent is None and page_style.get("header", {}).get("fill"):
            accent = page_style["header"]["fill"]

    if accent is None:
        accent = project.report_accent() or "#1F3A5F"
    if width is None:
        width = project.default_page_size()[0]

    # validate chart specs + refs up front (fail before creating anything)
    for c in charts:
        from core.visual_specs import validate_bindings
        validate_bindings(c["visual_type"], c.get("bindings", {}))
        _validate_refs_exist(project, c.get("bindings", {}))
    for k in kpis:
        _validate_refs_exist(project, {"_": [k["measure"]]})

    steps, page_height = TEMPLATES[template](
        title, subtitle, kpis, charts, width=width, accent=accent,
        style=page_style)
    page_id = project.create_page(name, width, page_height)

    made = {"shapes": 0, "texts": 0, "visuals": []}
    for step in steps:
        op = step["op"]
        if op == "style_page":
            project.style_page(page_id,
                               background_color=step.get("background_color"),
                               wallpaper_color=step.get("wallpaper_color"))
        elif op == "shape":
            project.add_shape(page_id, step.get("shape", "rectangle"),
                              fill=step.get("fill"),
                              round_corners=step.get("round_corners", False),
                              position=step.get("position"), z=step.get("z"))
            made["shapes"] += 1
        elif op == "text":
            project.add_text(page_id, step["runs"],
                             position=step.get("position"), z=step.get("z"))
            made["texts"] += 1
        elif op == "visual":
            vid = project.add_visual(page_id, step["spec"])
            made["visuals"].append(vid)
    return {"ok": True, "page_id": page_id, "page_height": page_height,
            "elements": made}


def add_visual(state: ReportState, page_id: str, visuals: list[dict]) -> dict:
    """Add one or more visuals to a page (batch, Part C.2).

    Each spec: {"visual_type", "bindings": {bucket: ["Table.Field", ...]},
    "position"?, "title"?, "id"?}. Bindings are validated against the visual
    type's bucket spec AND against the model before anything is written.
    """
    from core.visual_specs import validate_bindings

    project = state.require()
    page_ids = {p.id for p in project.list_pages()}
    if page_id not in page_ids:
        raise KeyError(f"Page {page_id!r} not found (have: {sorted(page_ids)})")

    # validate the whole batch before writing anything
    for spec in visuals:
        validate_bindings(spec["visual_type"], spec.get("bindings", {}))
        _validate_refs_exist(project, spec.get("bindings", {}))

    ids = [project.add_visual(page_id, spec) for spec in visuals]
    return {"ok": True, "page_id": page_id, "visual_ids": ids,
            "count": len(ids)}


# --- MCP server registration ------------------------------------------------------

from core.capabilities import GREETING

STATE = ReportState()
mcp = Server(
    "pbi-report",
    instructions=(
        GREETING + "\n\n"
        "Builds and edits the REPORT layer of a Power BI Project (.pbip) on "
        "disk — pages, visuals, formatting, themes, filters, design elements. "
        "Call pbi_set_project(path) first, then pbi_capabilities() to learn "
        "visual types/buckets/filters. Fastest path: pbi_scaffold_report(). "
        "Every write is schema-validated; run pbi_validate_project and "
        "pbi_lint_page before finishing."
    ),
)
tool = make_tool(mcp, STATE)


@tool(read=True, idempotent=True)
def pbi_capabilities() -> dict:
    """What this server can build: visual types + buckets, filters, formatting,
    design elements, workflow, and a worked example. Call after set_project."""
    from core.capabilities import capabilities

    return capabilities()


@tool(read=True, idempotent=True)
def pbi_set_project(path: str) -> dict:
    """Point the report server at a Power BI Project (.pbip). Call this first."""
    return set_project(STATE, path)


@tool(read=True)
def pbi_list_pages() -> list[dict]:
    """List report pages: id, display name, size, visual count, hidden flag."""
    return list_pages(STATE)


@tool(read=True)
def pbi_list_visuals(page_id: str) -> list[dict]:
    """List visuals on a page with type, position, title, and field bindings."""
    return list_visuals(STATE, page_id)


@tool(read=True)
def pbi_get_visual(page_id: str, visual_id: str) -> dict:
    """Full configuration of one visual (including raw visual.json)."""
    return get_visual(STATE, page_id, visual_id)


@tool(write=True, idempotent=True)
def pbi_rename_page(page_id: str, new_name: str) -> dict:
    """Rename a page's display name."""
    return STATE.require().rename_page(page_id, new_name)


@tool(write=True, idempotent=True)
def pbi_hide_page(page_id: str, hidden: bool = True) -> dict:
    """Hide or show a page in view mode."""
    return STATE.require().hide_page(page_id, hidden)


@tool(write=True, idempotent=True)
def pbi_reorder_pages(order: list) -> dict:
    """Set page order (list of page ids). Omitted pages keep their order after."""
    return STATE.require().reorder_pages(order)


@tool(write=True, destructive=True)
def pbi_delete_page(page_id: str) -> dict:
    """Delete a page (recoverable via .pbi/mcp-trash)."""
    return STATE.require().delete_page(page_id)


@tool(write=True)
def pbi_duplicate_page(page_id: str, new_name: str | None = None) -> dict:
    """Duplicate a page and its visuals; returns the new page id."""
    nid = STATE.require().duplicate_page(page_id, new_name)
    return {"ok": True, "page_id": nid}


@tool(read=True)
def pbi_list_filters(scope: str, page_id: str | None = None,
                     visual_id: str | None = None) -> list:
    """List filters at report/page/visual scope."""
    return STATE.require().list_filters(scope, page_id, visual_id)


@tool(write=True, destructive=True)
def pbi_remove_filter(scope: str, filter_name: str,
                      page_id: str | None = None,
                      visual_id: str | None = None) -> dict:
    """Remove a filter by name (from pbi_list_filters)."""
    return STATE.require().remove_filter(scope, filter_name, page_id, visual_id)


@tool(read=True)
def pbi_list_trash() -> list:
    """List recoverable deleted pages/visuals in .pbi/mcp-trash."""
    return STATE.require().list_trash()


@tool(write=True, preview=False)
def pbi_restore_visual(trash_path: str) -> dict:
    """Restore a deleted visual (path from pbi_list_trash) back onto its page."""
    return STATE.require().restore_visual(trash_path)


@tool(write=True)
def pbi_create_page(name: str, width: float | None = None,
                    height: float | None = None) -> dict:
    """Create a report page; returns page_id. Size defaults to the existing
    pages' dimensions so orientation matches the rest of the report."""
    return create_page(STATE, name, width, height)


@tool(write=True)
def pbi_add_visual(page_id: str, visuals: list[dict]) -> dict:
    """Add visuals to a page (batch). Each: {"visual_type", "bindings":
    {bucket: ["Table.Field",...]}, "position"?: {x,y,width,height},
    "title"?, "id"?}. Bindings are validated against the visual type's
    buckets and the model before writing."""
    return add_visual(STATE, page_id, visuals)


@tool(write=True)
def pbi_build_page(name: str, visuals: list[dict],
                   width: float | None = None,
                   height: float | None = None) -> dict:
    """Create a page AND add visuals in one call. Visuals without a
    "position" get an automatic layout (KPI cards top row, charts on a
    2-column grid). Size defaults to the existing pages' dimensions.
    Same visual spec shape as pbi_add_visual."""
    return build_page(STATE, name, visuals, width, height)


@tool(write=True, idempotent=True)
def pbi_style_page(page_id: str, background_color: str | None = None,
                   background_transparency: float | None = None,
                   wallpaper_color: str | None = None) -> dict:
    """Style a page's canvas background and wallpaper (outspace).
    Colors are '#hex'; transparency 0.0-1.0."""
    return STATE.require().style_page(page_id, background_color,
                                      background_transparency, wallpaper_color)


@tool(write=True)
def pbi_group_visuals(page_id: str, visual_ids: list, name: str = "Group") -> dict:
    """Group visuals so they move and style as one block. Needs >= 2 ids."""
    gid = STATE.require().group_visuals(page_id, visual_ids, name)
    return {"ok": True, "page_id": page_id, "group_id": gid}


@tool(write=True)
def pbi_add_visual_raw(page_id: str, visual_json: dict,
                       base: str = "visual") -> dict:
    """Escape hatch: add a prebuilt visual.json (validated against the schema)
    for third-party visuals this server doesn't model. Get a real one via
    pbi_get_visual, tweak it, and pass it here."""
    vid = STATE.require().add_visual_raw(page_id, visual_json, base)
    return {"ok": True, "page_id": page_id, "visual_id": vid}


@tool(write=True)
def pbi_add_text(page_id: str, runs, position: dict | None = None,
                 z: int | None = None) -> dict:
    """Add a textbox (titles, headers, commentary). `runs` is a string or a
    list of run dicts {text, bold, italic, size, color, font, align, url}.
    Use a high z to keep text above backplates."""
    vid = STATE.require().add_text(page_id, runs, position, z)
    return {"ok": True, "page_id": page_id, "visual_id": vid}


@tool(write=True)
def pbi_add_image(page_id: str, image_path: str,
                  position: dict | None = None, scaling: str | None = None,
                  z: int | None = None) -> dict:
    """Upload a local image into the report's resources and place it (logos,
    icons, backgrounds). scaling in Fit|Fill|Normal."""
    vid = STATE.require().add_image(page_id, image_path, position, scaling, z)
    return {"ok": True, "page_id": page_id, "visual_id": vid}


@tool(write=True)
def pbi_add_shape(page_id: str, shape: str = "rectangle",
                  fill: str | None = None, outline: str | None = None,
                  outline_weight: float | None = None,
                  position: dict | None = None, round_corners: bool = False,
                  z: int | None = None) -> dict:
    """Add a shape (rectangle/rectangleRounded/oval/line/arrow…): KPI
    backplates, dividers, accent bars. fill/outline are '#hex'. Use a LOW z
    to sit behind data visuals."""
    vid = STATE.require().add_shape(page_id, shape, fill, outline,
                                    outline_weight, position, round_corners, z)
    return {"ok": True, "page_id": page_id, "visual_id": vid}


def scaffold_report(state: ReportState, accent: str | None = None,
                    max_detail_pages: int = 3, dry_run: bool = False,
                    theme: bool = True) -> dict:
    """Profile the model and build (or propose) a full designed report.

    If the report already has a custom theme, its color is inherited and the
    theme is NOT overwritten, so new pages match the existing look.
    """
    from core.profile import profile_model
    from core.scaffold import propose_report

    project = state.require()
    existing_accent = project.report_accent()
    if accent is None:
        accent = existing_accent or "#1F3A5F"
    profile = profile_model(project)
    proposal = propose_report(profile, accent=accent,
                              max_detail_pages=max_detail_pages)
    if dry_run:
        return {"ok": True, "dry_run": True, "proposal": proposal,
                "profile_summary": profile["summary"]}

    # Only install a theme when the report doesn't already have one — never
    # clobber the user's existing theme.
    if theme and existing_accent is None:
        from core.theme import generate_theme
        project.set_report_theme(generate_theme(accent, name="Scaffold Theme",
                                                 mode=proposal["theme_mode"]))

    built_pages = []
    for spec in proposal["pages"]:
        res = build_designed_page(
            state, spec["name"], spec["title"], spec.get("subtitle"),
            kpis=spec["kpis"], charts=spec["charts"], accent=accent)
        built_pages.append(res["page_id"])

    # a simple nav bar of buttons across the top of each page
    if proposal["add_nav_bar"] and len(built_pages) > 1:
        for pid in built_pages:
            x = 16
            for target, spec in zip(built_pages, proposal["pages"]):
                if target == pid:
                    x += 150
                    continue
                project.add_nav_button(pid, spec["name"], target,
                                       position={"x": x, "y": 8, "width": 140,
                                                 "height": 32}, fill=accent)
                x += 150

    return {"ok": True, "pages": built_pages,
            "profile_summary": profile["summary"]}


@tool(write=True)
def pbi_scaffold_report(accent: str | None = None, max_detail_pages: int = 3,
                        dry_run: bool = False, theme: bool = True) -> dict:
    """AUTOPILOT: profile the model and build a full designed report — themed
    overview page (KPI strip + trend + breakdown + table) plus per-dimension
    detail pages with a nav bar. dry_run=true returns the proposal to edit
    first. Omit accent to inherit the report's existing theme (won't overwrite
    it). Fastest path from a raw model to an epic report."""
    return scaffold_report(STATE, accent, max_detail_pages, dry_run, theme)


@tool(read=True)
def pbi_profile_model() -> dict:
    """Classify the model (fact/dimension/date tables, measure roles, grouping
    columns) — the analysis that drives pbi_scaffold_report."""
    from core.profile import profile_model

    return profile_model(STATE.require())


@tool(write=True)
def pbi_build_designed_page(name: str, title: str, subtitle: str | None = None,
                            kpis: list | None = None, charts: list | None = None,
                            template: str = "exec-summary",
                            accent: str | None = None,
                            match_page: str | None = None) -> dict:
    """Build a fully DESIGNED page in one call: header band, KPI strip on
    rounded backplates, and a chart grid — themed and laid out.
    kpis: [{"measure": "Table.M", "title": "..."}]. charts: normal visual
    specs (positions auto-assigned). accent is the header/brand '#hex' —
    omit it to inherit the report's existing theme color; page size is
    inherited too. Pass match_page=<existing page id> to copy that page's
    header/KPI composition so the new page matches it closely."""
    return build_designed_page(STATE, name, title, subtitle, kpis, charts,
                               template, accent, match_page=match_page)


@tool(read=True)
def pbi_page_style(page_id: str) -> dict:
    """Inspect a page's header/KPI composition (band height+color, title font,
    card height, backplate) — what pbi_build_designed_page(match_page=...)
    copies."""
    from core.page_style import extract_page_style

    return extract_page_style(STATE.require(), page_id)


@tool(write=True, idempotent=True)
def pbi_update_bindings(page_id: str, visual_id: str,
                        bindings: dict) -> dict:
    """Replace a visual's field bindings ({bucket: ["Table.Field",...]});
    formatting and position are preserved. Validated before writing."""
    from core.visual_specs import validate_bindings

    project = STATE.require()
    v = project.get_visual(page_id, visual_id)
    if v.visual_type:
        validate_bindings(v.visual_type, bindings)
    _validate_refs_exist(project, bindings)
    return project.update_bindings(page_id, visual_id, bindings)


@tool(write=True, idempotent=True)
def pbi_sort_visual(page_id: str, visual_id: str, field: str,
                    direction: str = "Descending",
                    is_measure: bool = True) -> dict:
    """Sort a visual by a field (Ascending|Descending). Pairs with a TopN
    filter to make a real top-N chart."""
    return STATE.require().sort_visual(page_id, visual_id, field,
                                       direction, is_measure)


@tool(write=True)
def pbi_add_nav_button(page_id: str, label: str, target_page_id: str,
                       position: dict | None = None, fill: str = "#1F3A5F",
                       text_color: str = "#FFFFFF") -> dict:
    """Add a page-navigation button (click -> go to target_page_id). Build a
    nav bar by adding one per page."""
    vid = STATE.require().add_nav_button(page_id, label, target_page_id,
                                         position, fill, text_color)
    return {"ok": True, "page_id": page_id, "visual_id": vid}


@tool(write=True, idempotent=True)
def pbi_set_page_role(page_id: str, role: str,
                      tooltip_width: int = 320,
                      tooltip_height: int = 240) -> dict:
    """Make a page a drillthrough or tooltip page. role in
    drillthrough|tooltip|default."""
    size = (tooltip_width, tooltip_height) if role == "tooltip" else None
    return STATE.require().set_page_role(page_id, role, size)


@tool(write=True, idempotent=True)
def pbi_set_visual_interactions(page_id: str, source_visual: str,
                                interactions: dict) -> dict:
    """Control cross-filtering: interactions = {target_visual_id:
    'Filter'|'Highlight'|'NoFilter'} for clicks on source_visual."""
    return STATE.require().set_visual_interactions(page_id, source_visual,
                                                   interactions)


@tool(write=True)
def pbi_create_bookmark(name: str, display_name: str | None = None,
                        page_id: str | None = None) -> dict:
    """Capture the current report state (active page + filters) as a bookmark."""
    return STATE.require().create_bookmark(name, display_name, page_id)


@tool(write=True, idempotent=True)
def pbi_move_visual(page_id: str, visual_id: str,
                    x: float | None = None, y: float | None = None,
                    width: float | None = None,
                    height: float | None = None) -> dict:
    """Move/resize a visual; only passed fields change."""
    return STATE.require().move_visual(page_id, visual_id, x, y, width, height)


@tool(write=True, destructive=True)
def pbi_delete_visual(page_id: str, visual_id: str) -> dict:
    """Delete a visual (recoverable: moved into the report's .pbi/mcp-trash)."""
    return STATE.require().delete_visual(page_id, visual_id)


@tool(write=True, idempotent=True)
def pbi_format_visual(page_id: str, visual_id: str, target: str,
                      objects: dict) -> dict:
    """Format a visual. target='container' (title, background, border) or
    'visual' (axes, legend, labels). objects={objectName: {prop: value}} —
    plain strings/numbers/bools/'#hex' colors are auto-encoded."""
    return STATE.require().format_visual(page_id, visual_id, target, objects)


@tool(write=True, destructive=True)
def pbi_set_report_theme(theme: dict) -> dict:
    """Install a custom report theme (standard Power BI theme JSON with a
    'name' field) and activate it in report.json."""
    return STATE.require().set_report_theme(theme)


@tool(write=True, idempotent=True)
def pbi_generate_theme(brand: str = "#1F3A5F", name: str = "MCP Brand Theme",
                       mode: str = "light", install: bool = True) -> dict:
    """Generate a coherent Power BI theme from a brand color (palette + text
    classes + visual styles, light|dark). install=True (the default) applies
    it to the report now; install=False only returns the theme JSON."""
    from core.theme import generate_theme

    theme = generate_theme(brand, name, mode)
    if install:
        STATE.require().set_report_theme(theme)
    return {"ok": True, "installed": install, "theme": theme}


@tool(write=True)
def pbi_add_filter(scope: str, field: str, filter_type: str = "Categorical",
                   values: list | None = None,
                   comparison: str | None = None, comparison_value=None,
                   top_n: int | None = None, order_by: str | None = None,
                   last_n: int | None = None, relative_unit: str | None = None,
                   page_id: str | None = None,
                   visual_id: str | None = None,
                   is_measure: bool = False) -> dict:
    """Add a filter at report/page/visual scope. field='Table.Field'.
    Categorical: values=[...]. Advanced: comparison in eq|gt|ge|lt|le +
    comparison_value. TopN: top_n (rank by the visual's value field).
    RelativeDate: last_n + relative_unit in day|week|month|year."""
    from core.formatting import build_filter

    project = STATE.require()
    _validate_refs_exist(project, {"_": [field]})
    entry = build_filter(field, is_measure=is_measure,
                         filter_type=filter_type, values=values,
                         comparison=comparison,
                         comparison_value=comparison_value,
                         top_n=top_n, order_by=order_by,
                         last_n=last_n, relative_unit=relative_unit)
    return project.add_filter(scope, entry, page_id, visual_id)


@tool(read=True)
def pbi_project_diff(other_path: str) -> dict:
    """Diff the current project against another .pbip (or a backup dir):
    measures/pages/visuals/relationships added/removed/changed."""
    from core.diff import diff_projects

    return diff_projects(STATE.require(), PbipProject(other_path))


@tool(read=True)
def pbi_project_summary() -> dict:
    """A compact overview: tables, measures, relationships, pages, visuals,
    and field-usage counts."""
    project = STATE.require()
    tables = project.list_tables()
    pages = project.list_pages()
    from core.usage import classify_usage
    usage = classify_usage(project)
    return {
        "path": str(project.path),
        "model": {"tables": len(tables),
                  "measures": sum(len(t.measures) for t in tables),
                  "columns": sum(len(t.columns) for t in tables),
                  "relationships": len(project.list_relationships())},
        "report": {"pages": len(pages),
                   "visuals": sum(p.visual_count for p in pages)},
        "usage": usage["counts"],
    }


@tool(read=True)
def pbi_lint_page(page_id: str) -> dict:
    """Design lint a page: overlaps, off-canvas visuals, too-small visuals,
    near-misaligned edges, plus accessibility findings (a11y_*: alt text, tab
    order, small text, WCAG contrast, judged against the active theme).
    Returns {ok, accessibility_ok, findings[]}: `ok` is the layout verdict
    (no layout warnings); `accessibility_ok` is true when there are no a11y_*
    findings."""
    from core.lint import lint_page

    project = STATE.require()
    pages = {p.id: p for p in project.list_pages()}
    if page_id not in pages:
        raise KeyError(f"Page {page_id!r} not found")
    findings = lint_page(pages[page_id], project.list_visuals(page_id),
                         project=project)
    layout = [f for f in findings if not f["code"].startswith("a11y_")]
    return {"ok": not any(f["severity"] == "warning" for f in layout),
            "accessibility_ok": not any(f["code"].startswith("a11y_")
                                        for f in findings),
            "findings": findings}


@tool(read=True)
def pbi_validate_project() -> dict:
    """Validate every report JSON against the official Fabric schemas.
    Returns {ok, checked, errors[]}. Catches shapes Desktop would reject."""
    return STATE.require().validate_project()


@tool(read=True)
def pbi_model_usage() -> dict:
    """Classify model fields as direct (bound in report), indirect (needed by
    a bound measure or a relationship), or unused. The deletion fail-safe."""
    return model_usage(STATE)

register_journal_tools(mcp, STATE, tool)
load_tool_modules(__package__ or "report_server", mcp, STATE, tool)


def main() -> None:
    """Entry point (pbi-report-server): run over stdio for an MCP client."""
    mcp.run()


if __name__ == "__main__":
    main()
