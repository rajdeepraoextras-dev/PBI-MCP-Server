"""Accessibility tools: alt text, tab order and the accessibility report.

Logic is in core/accessibility.py; JSON is written through
``PbipProject._write_json`` (schema pre-flight, style preserving). This module
never imports report_server.server (the server loads it).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from core import accessibility as a11y

if TYPE_CHECKING:  # pragma: no cover
    from report_server.server import ReportState


def _visuals(project, page_id: str) -> list:
    if page_id not in {p.id for p in project.list_pages()}:
        raise ValueError(f"Page {page_id!r} not found "
                         f"(have: {[p.id for p in project.list_pages()]})")
    return project.list_visuals(page_id)


def _write_visual(project, page_id: str, data: dict, visual_id: str) -> None:
    vfile = project._visual_file(page_id, visual_id)
    # group containers have no 'visual' block, which the schema pre-flight
    # rejects (same reason PbipProject.group_visuals skips it)
    project._write_json(vfile, data, validate="visualGroup" not in data)


def _load(project, page_id: str, visual_id: str) -> dict:
    return json.loads(project._visual_file(page_id, visual_id)
                      .read_text(encoding="utf-8-sig"))


# --- alt text -------------------------------------------------------------------

def set_alt_text(state: "ReportState", page_id: str, visual_id: str,
                 text: str) -> dict:
    project = state.require()
    visuals = {v.id: v for v in _visuals(project, page_id)}
    if visual_id not in visuals:
        raise ValueError(f"Visual {visual_id!r} not on page {page_id!r} "
                         f"(have: {sorted(visuals)})")
    if "visualGroup" in (visuals[visual_id].raw or {}):
        raise ValueError(f"{visual_id!r} is a visual group; set alt text on "
                         "its member visuals")
    data = _load(project, page_id, visual_id)
    a11y.set_alt_text_raw(data, text.strip() if text else "")
    _write_visual(project, page_id, data, visual_id)
    return {"ok": True, "page_id": page_id, "visual_id": visual_id,
            "alt_text": a11y.get_alt_text(data) or ""}


def auto_alt_text(state: "ReportState", page_id: str,
                  overwrite: bool = False) -> dict:
    project = state.require()
    set_, skipped = [], []
    for v in _visuals(project, page_id):
        if not a11y.needs_alt_text(v):
            continue
        if a11y.get_alt_text(v.raw or {}) and not overwrite:
            skipped.append(v.id)
            continue
        text = a11y.describe_visual(v)
        data = json.loads(json.dumps(v.raw))
        a11y.set_alt_text_raw(data, text)
        _write_visual(project, page_id, data, v.id)
        set_.append({"visual_id": v.id, "alt_text": text})
    return {"ok": True, "page_id": page_id, "set": set_,
            "skipped_existing": skipped}


# --- tab order --------------------------------------------------------------------

def _apply_tab_order(project, page_id: str, sequence: list) -> list[dict]:
    changed = []
    for i, v in enumerate(sequence):
        data = json.loads(json.dumps(v.raw))
        pos = data.setdefault("position", {})
        if pos.get("tabOrder") == i:
            continue
        pos["tabOrder"] = i
        _write_visual(project, page_id, data, v.id)
        changed.append({"visual_id": v.id, "tab_order": i})
    return changed


def set_tab_order(state: "ReportState", page_id: str, order: list[str]) -> dict:
    project = state.require()
    visuals = _visuals(project, page_id)
    by_id = {v.id: v for v in visuals}
    if not isinstance(order, list) or not order:
        raise ValueError("order must be a non-empty list of visual ids")
    dupes = sorted({x for x in order if order.count(x) > 1})
    if dupes:
        raise ValueError(f"order lists {dupes} more than once")
    unknown = [x for x in order if x not in by_id]
    if unknown:
        raise ValueError(f"Unknown visual id(s) {unknown} on page {page_id!r} "
                         f"(have: {sorted(by_id)})")
    rest = [v for v in a11y.tab_order_sequence(visuals) if v.id not in order]
    sequence = [by_id[x] for x in order] + rest
    changed = _apply_tab_order(project, page_id, sequence)
    return {"ok": True, "page_id": page_id,
            "order": [v.id for v in sequence], "changed": changed}


def auto_tab_order(state: "ReportState", page_id: str) -> dict:
    project = state.require()
    sequence = a11y.tab_order_sequence(_visuals(project, page_id))
    changed = _apply_tab_order(project, page_id, sequence)
    return {"ok": True, "page_id": page_id,
            "order": [v.id for v in sequence], "changed": changed}


# --- report -----------------------------------------------------------------------

def accessibility_report(state: "ReportState", page_id: str) -> dict:
    project = state.require()
    visuals = _visuals(project, page_id)
    page_json = json.loads((project._require_report() / "definition" / "pages"
                            / page_id / "page.json").read_text(encoding="utf-8-sig"))
    theme = a11y.theme_colors(project)
    findings = a11y.analyze_page(visuals, page_json=page_json, theme=theme)
    counts: dict[str, int] = {}
    for f in findings:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
    return {"page_id": page_id,
            "ok": not any(f["severity"] in ("warning", "error") for f in findings),
            "counts": counts, "theme": theme, "findings": findings}


# --- MCP wrappers -------------------------------------------------------------------

def register(mcp, state, tool) -> None:
    @tool(write=True, idempotent=True)
    def pbi_set_alt_text(page_id: str, visual_id: str, text: str) -> dict:
        """Set a visual's alt text (visualContainerObjects.general.altText,
        read aloud by screen readers). An empty string clears it. Groups and
        decorative shapes are not valid targets. Returns the stored text."""
        return set_alt_text(state, page_id, visual_id, text)

    @tool(write=True, idempotent=True)
    def pbi_auto_alt_text(page_id: str, overwrite: bool = False) -> dict:
        """Generate alt text for a page's content visuals that lack it, as
        '<visual type> of <measures> by <category>' (prefixed by the title when
        it adds information). Existing alt text is kept unless overwrite=true.
        Text boxes and decorative shapes are skipped. Returns what was set."""
        return auto_alt_text(state, page_id, overwrite)

    @tool(write=True, idempotent=True)
    def pbi_set_tab_order(page_id: str, order: list[str]) -> dict:
        """Set keyboard tab order (position.tabOrder) from a list of visual
        ids, first to last, numbered 0..n-1. Visuals not listed follow in
        reading order. Returns the full resulting order and what changed."""
        return set_tab_order(state, page_id, order)

    @tool(write=True, idempotent=True)
    def pbi_auto_tab_order(page_id: str) -> dict:
        """Set tab order to reading order (top-to-bottom, left-to-right), with
        hidden and decorative visuals last. Returns the order and what
        changed."""
        return auto_tab_order(state, page_id)

    @tool(read=True, idempotent=True)
    def pbi_accessibility_report(page_id: str) -> dict:
        """Audit a page: missing alt text, tab-order duplicates/gaps, text
        below 9 pt, and text/background contrast under WCAG 2.1 AA (4.5:1),
        using the active theme's colours (defaults #252423 on #FFFFFF).
        Returns {ok, counts, theme, findings[]}; each finding has severity,
        code, visual_id, message and a fix hint."""
        return accessibility_report(state, page_id)
