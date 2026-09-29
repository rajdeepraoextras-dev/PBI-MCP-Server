"""Semantic diff + page/visual import tools for the report server.

Plain logic functions take an explicit ``state`` (``state.require()`` is the
selected ``PbipProject``); ``register`` wires the thin ``pbi_*`` wrappers.
The heavy lifting lives in ``core.diff_semantic`` and ``core.import_pages``.

This module is loaded by ``report_server.server`` (``load_tool_modules``), so
it must not import the server module at import time.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from core.pbip import PbipProject

if TYPE_CHECKING:  # pragma: no cover - typing only
    from report_server.server import ReportState


def _open_project(path: str, *, need_report: bool = False) -> PbipProject:
    """Open another project read-only (no backups); fail with a clear error."""
    p = Path(str(path)).expanduser()
    if not p.exists():
        raise ValueError(f"Path does not exist: {path}")
    project = PbipProject(p, backups=False)
    if project.report_dir is None and project.semantic_model_dir is None:
        raise ValueError(
            f"No *.Report or *.SemanticModel folder found from {path}; pass "
            f"the project root, the .pbip file, or a layer folder.")
    if need_report and project.report_dir is None:
        raise ValueError(f"No *.Report folder found from {path}; nothing to "
                         f"import from.")
    return project


def semantic_diff(state: ReportState, other_path: str,
                  page_id: str | None = None, include_model: bool = True,
                  max_format_changes: int = 200) -> dict:
    """Structured diff from the selected project to `other_path`."""
    from core.diff_semantic import semantic_diff as _diff

    return _diff(state.require(), _open_project(other_path), page_id=page_id,
                 include_model=include_model,
                 max_format_changes=max_format_changes)


def import_pages(state: ReportState, from_path: str, page_ids: list[str],
                 rename_map: dict | None = None,
                 include_bookmarks: bool = True,
                 allow_missing_fields: bool = False,
                 position: int | None = None) -> dict:
    """Copy pages from another project into the selected one."""
    from core.import_pages import import_pages as _import

    return _import(state.require(), _open_project(from_path, need_report=True),
                   list(page_ids), rename_map, include_bookmarks,
                   allow_missing_fields, position)


def import_visuals(state: ReportState, from_path: str, page_id: str,
                   visual_ids: list[str], target_page_id: str,
                   offset: dict | None = None,
                   allow_missing_fields: bool = False) -> dict:
    """Copy visuals from a page of another project onto a target page."""
    from core.import_pages import import_visuals as _import

    return _import(state.require(), _open_project(from_path, need_report=True),
                   page_id, list(visual_ids), target_page_id, offset,
                   allow_missing_fields)


def register(mcp, state, tool) -> None:
    @tool(read=True)
    def pbi_semantic_diff(other_path: str, page_id: str | None = None,
                          include_model: bool = True,
                          max_format_changes: int = 200) -> dict:
        """Structured, deterministic diff between the selected project and
        another PBIP (a backup, another branch checkout or a sibling copy):
        other_path is a project root, .pbip file or *.Report / *.SemanticModel
        folder. Report layer: pages added/removed/renamed/reordered/resized/
        hidden; per page, visuals added/removed and, per surviving visual
        (matched by id), moved/resized/z-order/type/title, bindings added or
        removed per bucket, formatting property changes as flattened paths
        (old -> new, at most max_format_changes entries in total, with true
        counts), filters at report/page/visual scope, theme (name + which
        dataColors), bookmarks and report-level measures. Model layer (skip
        with include_model=false): tables, measures (DAX as unified diff
        lines, format string, folder, hidden), columns (type, format, hidden,
        sort-by), relationships (cardinality, direction, active), calculation
        groups, partitions and shared expressions/parameters. page_id limits
        the report part to one page.
        Returns {identical, summary (counts), report, model}; a layer missing
        on one side is flagged instead of enumerated."""
        return semantic_diff(state, other_path, page_id, include_model,
                             max_format_changes)

    @tool(write=True)
    def pbi_import_pages(from_path: str, page_ids: list[str],
                         rename_map: dict | None = None,
                         include_bookmarks: bool = True,
                         allow_missing_fields: bool = False,
                         position: int | None = None) -> dict:
        """Copy pages from another PBIP (from_path: project root, .pbip or
        *.Report folder) into the selected report: page.json, every visual
        folder, the registered images they use (copied and re-registered in
        report.json when needed) and, with include_bookmarks, the bookmarks
        that open those pages. Ids that are taken get new ones (page ids from
        the display name, "-2" suffixes); navigation buttons, bookmarks and
        pageBinding follow. rename_map is {source page id: new display name};
        position is the 0-based index in the page order (omit to append);
        imported pages keep their source order. Every field the imported
        visuals and filters bind is checked against the target model and the
        import is refused with the list of missing fields unless
        allow_missing_fields is true. Returns {id_map (old -> new page id),
        pages, bookmarks, resources, order, missing_fields, warnings}."""
        return import_pages(state, from_path, page_ids, rename_map,
                            include_bookmarks, allow_missing_fields, position)

    @tool(write=True)
    def pbi_import_visuals(from_path: str, page_id: str,
                           visual_ids: list[str], target_page_id: str,
                           offset: dict | None = None,
                           allow_missing_fields: bool = False) -> dict:
        """Copy individual visuals from page `page_id` of another PBIP
        (from_path) onto `target_page_id` of the selected report. Visual ids
        already used on the target page get a "-2"/"-3" suffix; offset
        {"x": 20, "y": 10} shifts every imported visual; selecting a group
        also imports its members. Bound fields are checked against the target
        model (refused with the list of missing fields unless
        allow_missing_fields is true) and referenced images are copied.
        Returns {id_map (old -> new visual id), count, missing_fields,
        resources, warnings}."""
        return import_visuals(state, from_path, page_id, visual_ids,
                              target_page_id, offset, allow_missing_fields)
