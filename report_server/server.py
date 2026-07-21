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

from mcp.server.fastmcp import FastMCP

from core.pbip import PbipProject
from core.pbir import visual_bindings


@dataclass
class ReportState:
    """Holds the project selected via pbi_set_project for the session."""
    project: PbipProject | None = field(default=None)

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
    return {
        "ok": True,
        "path": str(project.path),
        "pages": len(pages),
        "visuals": sum(p.visual_count for p in pages),
    }


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
    bad = []
    for refs in bindings.values():
        for ref in refs:
            entity, _, prop = ref.partition(".")
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


_CARD_TYPES = {"card", "cardVisual", "multiRowCard", "gauge"}


def auto_layout(visuals: list[dict], width: float = 1280,
                height: float = 720, margin: float = 16) -> list[dict]:
    """Assign positions to specs that lack one: KPI cards in a top row,
    everything else on a 2-column grid below."""
    cards = [v for v in visuals if v["visual_type"] in _CARD_TYPES
             and "position" not in v]
    charts = [v for v in visuals if v["visual_type"] not in _CARD_TYPES
              and "position" not in v]

    y = margin
    if cards:
        card_w = (width - margin * (len(cards) + 1)) / max(len(cards), 1)
        x = margin
        for v in cards:
            v["position"] = {"x": round(x), "y": y,
                             "width": round(card_w), "height": 120}
            x += card_w + margin
        y += 120 + margin

    if charts:
        cols = 2 if len(charts) > 1 else 1
        rows = -(-len(charts) // cols)
        chart_w = (width - margin * (cols + 1)) / cols
        chart_h = max((height - y - margin * rows) / rows, 200)
        for i, v in enumerate(charts):
            r, c = divmod(i, cols)
            v["position"] = {
                "x": round(margin + c * (chart_w + margin)),
                "y": round(y + r * (chart_h + margin)),
                "width": round(chart_w), "height": round(chart_h),
            }
    return visuals


def build_page(state: ReportState, name: str, visuals: list[dict],
               width: float = 1280, height: float = 720) -> dict:
    """One-call flow: create a page and add visuals with auto-layout."""
    page = create_page(state, name, width, height)
    auto_layout(visuals, width, height)
    res = add_visual(state, page["page_id"], visuals)
    return {"ok": True, "page_id": page["page_id"],
            "visual_ids": res["visual_ids"]}


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


# --- FastMCP registration ------------------------------------------------------

STATE = ReportState()
mcp = FastMCP(
    "pbi-report",
    instructions=(
        "Builds and edits the REPORT layer of a Power BI Project (.pbip) on "
        "disk — pages, visuals, formatting, themes, filters, design elements. "
        "Call pbi_set_project(path) first, then pbi_capabilities() to learn "
        "visual types/buckets/filters. Prefer pbi_build_page for a full page "
        "in one call. Every write is schema-validated; run pbi_validate_project "
        "and pbi_lint_page before finishing."
    ),
)


@mcp.tool()
def pbi_capabilities() -> dict:
    """What this server can build: visual types + buckets, filters, formatting,
    design elements, workflow, and a worked example. Call after set_project."""
    from core.capabilities import capabilities

    return capabilities()


@mcp.tool()
def pbi_set_project(path: str) -> dict:
    """Point the report server at a Power BI Project (.pbip). Call this first."""
    return set_project(STATE, path)


@mcp.tool()
def pbi_list_pages() -> list[dict]:
    """List report pages: id, display name, size, visual count, hidden flag."""
    return list_pages(STATE)


@mcp.tool()
def pbi_list_visuals(page_id: str) -> list[dict]:
    """List visuals on a page with type, position, title, and field bindings."""
    return list_visuals(STATE, page_id)


@mcp.tool()
def pbi_get_visual(page_id: str, visual_id: str) -> dict:
    """Full configuration of one visual (including raw visual.json)."""
    return get_visual(STATE, page_id, visual_id)


@mcp.tool()
def pbi_create_page(name: str, width: float = 1280,
                    height: float = 720) -> dict:
    """Create a report page; returns page_id."""
    return create_page(STATE, name, width, height)


@mcp.tool()
def pbi_add_visual(page_id: str, visuals: list[dict]) -> dict:
    """Add visuals to a page (batch). Each: {"visual_type", "bindings":
    {bucket: ["Table.Field",...]}, "position"?: {x,y,width,height},
    "title"?, "id"?}. Bindings are validated against the visual type's
    buckets and the model before writing."""
    return add_visual(STATE, page_id, visuals)


@mcp.tool()
def pbi_build_page(name: str, visuals: list[dict],
                   width: float = 1280, height: float = 720) -> dict:
    """Create a page AND add visuals in one call. Visuals without a
    "position" get an automatic layout (KPI cards top row, charts on a
    2-column grid). Same visual spec shape as pbi_add_visual."""
    return build_page(STATE, name, visuals, width, height)


@mcp.tool()
def pbi_style_page(page_id: str, background_color: str | None = None,
                   background_transparency: float | None = None,
                   wallpaper_color: str | None = None) -> dict:
    """Style a page's canvas background and wallpaper (outspace).
    Colors are '#hex'; transparency 0.0-1.0."""
    return STATE.require().style_page(page_id, background_color,
                                      background_transparency, wallpaper_color)


@mcp.tool()
def pbi_group_visuals(page_id: str, visual_ids: list, name: str = "Group") -> dict:
    """Group visuals so they move and style as one block. Needs >= 2 ids."""
    gid = STATE.require().group_visuals(page_id, visual_ids, name)
    return {"ok": True, "page_id": page_id, "group_id": gid}


@mcp.tool()
def pbi_add_visual_raw(page_id: str, visual_json: dict,
                       base: str = "visual") -> dict:
    """Escape hatch: add a prebuilt visual.json (validated against the schema)
    for third-party visuals this server doesn't model. Get a real one via
    pbi_get_visual, tweak it, and pass it here."""
    vid = STATE.require().add_visual_raw(page_id, visual_json, base)
    return {"ok": True, "page_id": page_id, "visual_id": vid}


@mcp.tool()
def pbi_add_text(page_id: str, runs, position: dict | None = None,
                 z: int | None = None) -> dict:
    """Add a textbox (titles, headers, commentary). `runs` is a string or a
    list of run dicts {text, bold, italic, size, color, font, align, url}.
    Use a high z to keep text above backplates."""
    vid = STATE.require().add_text(page_id, runs, position, z)
    return {"ok": True, "page_id": page_id, "visual_id": vid}


@mcp.tool()
def pbi_add_image(page_id: str, image_path: str,
                  position: dict | None = None, scaling: str | None = None,
                  z: int | None = None) -> dict:
    """Upload a local image into the report's resources and place it (logos,
    icons, backgrounds). scaling in Fit|Fill|Normal."""
    vid = STATE.require().add_image(page_id, image_path, position, scaling, z)
    return {"ok": True, "page_id": page_id, "visual_id": vid}


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
def pbi_move_visual(page_id: str, visual_id: str,
                    x: float | None = None, y: float | None = None,
                    width: float | None = None,
                    height: float | None = None) -> dict:
    """Move/resize a visual; only passed fields change."""
    return STATE.require().move_visual(page_id, visual_id, x, y, width, height)


@mcp.tool()
def pbi_delete_visual(page_id: str, visual_id: str) -> dict:
    """Delete a visual (recoverable: moved into the report's .pbi/mcp-trash)."""
    return STATE.require().delete_visual(page_id, visual_id)


@mcp.tool()
def pbi_format_visual(page_id: str, visual_id: str, target: str,
                      objects: dict) -> dict:
    """Format a visual. target='container' (title, background, border) or
    'visual' (axes, legend, labels). objects={objectName: {prop: value}} —
    plain strings/numbers/bools/'#hex' colors are auto-encoded."""
    return STATE.require().format_visual(page_id, visual_id, target, objects)


@mcp.tool()
def pbi_set_report_theme(theme: dict) -> dict:
    """Install a custom report theme (standard Power BI theme JSON with a
    'name' field) and activate it in report.json."""
    return STATE.require().set_report_theme(theme)


@mcp.tool()
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


@mcp.tool()
def pbi_validate_project() -> dict:
    """Validate every report JSON against the official Fabric schemas.
    Returns {ok, checked, errors[]}. Catches shapes Desktop would reject."""
    return STATE.require().validate_project()


@mcp.tool()
def pbi_model_usage() -> dict:
    """Classify model fields as direct (bound in report), indirect (needed by
    a bound measure or a relationship), or unused. The deletion fail-safe."""
    return model_usage(STATE)


def main() -> None:
    """Entry point (pbi-report-server): run over stdio for an MCP client."""
    mcp.run()


if __name__ == "__main__":
    main()
