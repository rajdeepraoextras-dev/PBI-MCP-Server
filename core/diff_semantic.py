"""Semantic diff between two Power BI projects (report layer + model layer).

``semantic_diff(base, other)`` compares two ``PbipProject`` objects -- a
backup, another branch checkout, a sibling copy -- and returns a structured,
deterministic (everything sorted) description of what differs. Unlike
``core.diff.diff_projects`` (which only lists changed keys) this reports *how*
things changed: a visual that moved, a field that was rebound, a formatting
property that flipped, a measure whose DAX changed (as unified-diff lines).

Result shape (every list is sorted; empty categories are still present)::

    {
      "identical": bool,
      "projects": {"base": ..., "other": ...},
      "scope": {"page_id": ..., "include_model": ..., "max_format_changes": ...},
      "summary": {"pages_added": n, ..., "total_changes": n},
      "report": {
        "present": {"base": bool, "other": bool},
        "pages": {"added", "removed", "renamed", "resized", "hidden_changed",
                  "reordered", "active_page"},
        "page_details": [{"id", "name", "visuals": {"added", "removed",
                          "changed"}, "filters"?, "formatting"?}],
        "filters": {"added", "removed", "changed"},      # report scope
        "formatting": {...} | None,                      # report.json props
        "theme": {...} | None, "bookmarks": {...}, "report_measures": {...}
      },
      "model": {"present", "tables", "measures", "columns", "relationships",
                "calculation_groups", "partitions"}     # or {"included": false}
    }

A changed visual entry carries only the keys that changed: ``moved`` (x/y),
``resized`` (width/height), ``z_order``, ``type_changed``, ``title_changed``,
``bindings`` (per bucket ``{added, removed, reordered?}``), ``filters`` and
``formatting``. ``formatting`` is a list of flattened property paths relative
to the visual (``objects.title[0].properties.text``); a path present on one
side only has just ``from`` or just ``to``. Literal wrappers
(``{"expr": {"Literal": {"Value": v}}}``) collapse to ``v``. All formatting
entries across the whole diff share one budget (``max_format_changes``,
default 200): every section still reports its true ``count`` but only lists
entries while budget remains (``truncated`` says so).

A layer that exists in one project only is reported under ``present`` and
counted in ``summary.layers_missing`` instead of being enumerated.

The TMDL reading here is a small independent scanner (not core.tmdl's
parser) because the model layer needs properties the parser does not expose
(column formatString / sortByColumn, calculation items, correct
``isActive: false``).
"""

from __future__ import annotations

import difflib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from core.pbir import _HIDDEN_VISIBILITIES, _extract_title
from core.tmdl import _FENCE, _dedent, _leading_tabs, _split_col_ref, _unquote

DEFAULT_MAX_FORMAT_CHANGES = 200
_FILTER_DETAIL_CAP = 20
_MISSING = object()
_TITLE_PATH = "visualContainerObjects.title[0].properties.text"

_SUMMARY_KEYS = (
    "layers_missing",
    "pages_added", "pages_removed", "pages_renamed", "pages_resized",
    "pages_hidden_changed", "pages_reordered", "active_page_changed",
    "visuals_added", "visuals_removed", "visuals_changed",
    "filters_added", "filters_removed", "filters_changed",
    "formatting_changes", "theme_changed",
    "bookmarks_added", "bookmarks_removed", "bookmarks_changed",
    "report_measures_added", "report_measures_removed",
    "report_measures_changed",
    "tables_added", "tables_removed", "tables_changed",
    "measures_added", "measures_removed", "measures_changed",
    "columns_added", "columns_removed", "columns_changed",
    "relationships_added", "relationships_removed", "relationships_changed",
    "calculation_groups_added", "calculation_groups_removed",
    "calculation_groups_changed",
    "partitions_added", "partitions_removed", "partitions_changed",
)


class _Ctx:
    """Counters + the shared formatting-entry budget for one diff run."""

    def __init__(self, max_format: int):
        self.counts: Counter = Counter({k: 0 for k in _SUMMARY_KEYS})
        self.remaining = max_format
        self.shown = 0

    def hit(self, key: str, n: int = 1) -> None:
        if n:
            self.counts[key] += n


# --- generic helpers -----------------------------------------------------------

def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise ValueError(f"Cannot read JSON from {path}: {exc}") from exc


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    return a == b


def _literal_value(node: Any) -> Any:
    """``v`` for ``{"expr": {"Literal": {"Value": v}}}``, else ``_MISSING``."""
    if isinstance(node, dict) and len(node) == 1:
        expr = node.get("expr")
        if isinstance(expr, dict) and len(expr) == 1:
            lit = expr.get("Literal")
            if isinstance(lit, dict) and len(lit) == 1 and "Value" in lit:
                return lit["Value"]
    return _MISSING


def _flatten(node: Any, path: str, out: dict) -> None:
    lit = _literal_value(node)
    if lit is not _MISSING:
        out[path] = lit
    elif isinstance(node, dict):
        if not node:
            out[path] = {}
        for key in node:
            _flatten(node[key], f"{path}.{key}" if path else str(key), out)
    elif isinstance(node, list):
        if not node:
            out[path] = []
        for i, item in enumerate(node):
            _flatten(item, f"{path}[{i}]", out)
    else:
        out[path] = node


def _flat(node: Any) -> dict:
    out: dict = {}
    if node is not None:
        _flatten(node, "", out)
    return out


def _flat_diff(a: dict, b: dict) -> list[dict]:
    """Sorted property diffs between two flattened dicts."""
    out = []
    for path in sorted(set(a) | set(b)):
        if path in a and path in b:
            if not _same(a[path], b[path]):
                out.append({"path": path, "from": a[path], "to": b[path]})
        elif path in a:
            out.append({"path": path, "from": a[path]})
        else:
            out.append({"path": path, "to": b[path]})
    return out


def _format_section(ctx: _Ctx, diffs: list[dict]) -> dict | None:
    """Wrap property diffs, spending the shared budget."""
    if not diffs:
        return None
    ctx.hit("formatting_changes", len(diffs))
    take = min(len(diffs), max(ctx.remaining, 0))
    ctx.remaining -= take
    ctx.shown += take
    return {"count": len(diffs), "shown": take, "truncated": take < len(diffs),
            "changes": diffs[:take]}


def _limited(diffs: list[dict], cap: int = _FILTER_DETAIL_CAP) -> dict:
    return {"count": len(diffs), "truncated": len(diffs) > cap,
            "changes": diffs[:cap]}


def _udiff(old: str | None, new: str | None) -> list[str]:
    return list(difflib.unified_diff(
        (old or "").splitlines(), (new or "").splitlines(),
        fromfile="base", tofile="other", lineterm="", n=1))


def _field_label(field: Any) -> str:
    """``Entity.Property`` for a PBIR field expression (best effort)."""
    if isinstance(field, dict):
        for kind, body in field.items():
            if not isinstance(body, dict):
                continue
            expr = body.get("Expression")
            if kind == "Aggregation" and isinstance(expr, dict):
                return f"Agg{body.get('Function', '')}({_field_label(expr)})"
            entity = (expr or {}).get("SourceRef", {}).get("Entity") \
                if isinstance(expr, dict) else None
            prop = body.get("Property") or body.get("Level") \
                or body.get("Hierarchy")
            if isinstance(entity, str) and isinstance(prop, str):
                return f"{entity}.{prop}"
    return json.dumps(field, sort_keys=True)


def _projection_ref(proj: Any) -> str:
    if isinstance(proj, dict):
        ref = proj.get("queryRef")
        if isinstance(ref, str) and ref:
            return ref
        return _field_label(proj.get("field"))
    return json.dumps(proj, sort_keys=True)


def _strip_filters(d: dict) -> dict:
    """Copy of a container dict without ``filterConfig.filters`` (handled
    separately); any other filterConfig keys stay."""
    fc = d.get("filterConfig")
    out = {k: v for k, v in d.items() if k != "filterConfig"}
    if isinstance(fc, dict):
        rest = {k: v for k, v in fc.items() if k != "filters"}
        if rest:
            out["filterConfig"] = rest
    return out


def _filters_of(container: dict) -> list[dict]:
    fc = container.get("filterConfig")
    if isinstance(fc, dict) and isinstance(fc.get("filters"), list):
        return [f for f in fc["filters"] if isinstance(f, dict)]
    return []


def _diff_filters(a: list[dict], b: list[dict], ctx: _Ctx) -> dict | None:
    """Filters matched by name -> {added, removed, changed} or None."""
    def key(f: dict) -> str:
        return f.get("name") or f"{_field_label(f.get('field'))}|{f.get('type')}"

    def brief(f: dict) -> dict:
        return {"name": key(f), "field": _field_label(f.get("field")),
                "type": f.get("type")}

    ma, mb = {key(f): f for f in a}, {key(f): f for f in b}
    added = [brief(mb[k]) for k in sorted(set(mb) - set(ma))]
    removed = [brief(ma[k]) for k in sorted(set(ma) - set(mb))]
    changed = []
    for k in sorted(set(ma) & set(mb)):
        if ma[k] != mb[k]:
            diffs = _flat_diff(_flat({x: y for x, y in ma[k].items() if x != "name"}),
                               _flat({x: y for x, y in mb[k].items() if x != "name"}))
            if diffs:
                changed.append({**brief(mb[k]), **_limited(diffs)})
    if not (added or removed or changed):
        return None
    ctx.hit("filters_added", len(added))
    ctx.hit("filters_removed", len(removed))
    ctx.hit("filters_changed", len(changed))
    return {"added": added, "removed": removed, "changed": changed}


# --- report layer ---------------------------------------------------------------

def _load_report(project) -> dict | None:
    try:
        report_dir = project._require_report()
    except FileNotFoundError:
        return None
    definition = report_dir / "definition"
    pages_dir = definition / "pages"
    order = [p.id for p in project.list_pages()]
    meta = _read_json(pages_dir / "pages.json") or {}
    pages: dict[str, dict] = {}
    for pid in order:
        pj = _read_json(pages_dir / pid / "page.json") or {}
        visuals: dict[str, dict] = {}
        vdir = pages_dir / pid / "visuals"
        if vdir.is_dir():
            for vf in sorted(vdir.glob("*/visual.json")):
                data = _read_json(vf) or {}
                visuals[data.get("name") or vf.parent.name] = data
        pages[pid] = {"json": pj, "visuals": visuals}
    bookmarks: dict[str, dict] = {}
    bdir = definition / "bookmarks"
    if bdir.is_dir():
        for bf in sorted(bdir.glob("*.json")):
            if bf.name == "bookmarks.json":
                continue
            data = _read_json(bf)
            if isinstance(data, dict):
                name = data.get("name") or bf.name.split(".")[0]
                bookmarks[str(name)] = data
    report_json = _read_json(definition / "report.json") or {}
    return {
        "dir": report_dir,
        "order": order,
        "active": meta.get("activePageName"),
        "report": report_json,
        "pages": pages,
        "bookmarks": bookmarks,
        "extensions": _read_json(definition / "reportExtensions.json"),
        "theme": _load_theme(report_dir, report_json),
    }


def _load_theme(report_dir: Path, report_json: dict) -> dict:
    tc = report_json.get("themeCollection")
    tc = tc if isinstance(tc, dict) else {}
    base = tc.get("baseTheme")
    custom = tc.get("customTheme")
    custom_name = custom.get("name") if isinstance(custom, dict) else None
    theme_json = None
    if custom_name:
        try:
            theme_json = _read_json(report_dir / "StaticResources"
                                    / "RegisteredResources" / custom_name)
        except ValueError:
            theme_json = None
    return {"base": base.get("name") if isinstance(base, dict) else None,
            "custom": custom_name, "json": theme_json}


def _diff_theme(a: dict, b: dict, ctx: _Ctx) -> dict | None:
    out: dict = {}
    if a["base"] != b["base"]:
        out["base_theme"] = {"from": a["base"], "to": b["base"]}
    if a["custom"] != b["custom"]:
        out["custom_theme"] = {"from": a["custom"], "to": b["custom"]}
    ta, tb = a["json"], b["json"]
    if ta != tb:
        na = ta.get("name") if isinstance(ta, dict) else None
        nb = tb.get("name") if isinstance(tb, dict) else None
        if na != nb:
            out["name"] = {"from": na, "to": nb}
        ca = (ta or {}).get("dataColors") if isinstance(ta, dict) else None
        cb = (tb or {}).get("dataColors") if isinstance(tb, dict) else None
        ca, cb = ca or [], cb or []
        if ca != cb:
            changed = []
            for i in range(max(len(ca), len(cb))):
                va = ca[i] if i < len(ca) else None
                vb = cb[i] if i < len(cb) else None
                if va != vb:
                    entry: dict = {"index": i}
                    if va is not None:
                        entry["from"] = va
                    if vb is not None:
                        entry["to"] = vb
                    changed.append(entry)
            out["data_colors"] = {"changed": changed,
                                  "count_from": len(ca), "count_to": len(cb)}
        if isinstance(ta, dict) and isinstance(tb, dict):
            other_keys = sorted(k for k in set(ta) | set(tb)
                                if k not in ("name", "dataColors")
                                and not _same(ta.get(k), tb.get(k)))
            if other_keys:
                out["other_keys_changed"] = other_keys
    if out:
        ctx.hit("theme_changed")
    return out or None


def _vtype(v: dict) -> str | None:
    vis = v.get("visual")
    if isinstance(vis, dict) and vis.get("visualType"):
        return vis["visualType"]
    return "visualGroup" if "visualGroup" in v else None


def _bindings(v: dict) -> dict[str, list[str]]:
    vis = v.get("visual")
    qs = (vis.get("query") or {}).get("queryState") if isinstance(vis, dict) \
        and isinstance(vis.get("query"), dict) else None
    out: dict[str, list[str]] = {}
    if isinstance(qs, dict):
        for bucket, spec in qs.items():
            if isinstance(spec, dict):
                out[bucket] = [_projection_ref(p)
                               for p in spec.get("projections") or []]
    return out


def _diff_bindings(a: dict, b: dict) -> dict:
    out = {}
    for bucket in sorted(set(a) | set(b)):
        la, lb = a.get(bucket, []), b.get(bucket, [])
        entry: dict = {}
        added = sorted(set(lb) - set(la))
        removed = sorted(set(la) - set(lb))
        if added:
            entry["added"] = added
        if removed:
            entry["removed"] = removed
        if [r for r in la if r in lb] != [r for r in lb if r in la]:
            entry["reordered"] = True
        if entry:
            out[bucket] = entry
    return out


def _query_extras(v: dict) -> dict:
    """queryState content that is not the bare field/queryRef membership:
    bucket-level extras and per-projection extras keyed by queryRef."""
    vis = v.get("visual")
    qs = (vis.get("query") or {}).get("queryState") if isinstance(vis, dict) \
        and isinstance(vis.get("query"), dict) else None
    out: dict = {}
    if isinstance(qs, dict):
        for bucket, spec in qs.items():
            if not isinstance(spec, dict):
                out[bucket] = spec
                continue
            entry = {k: x for k, x in spec.items() if k != "projections"}
            for proj in spec.get("projections") or []:
                if isinstance(proj, dict):
                    extra = {k: x for k, x in proj.items()
                             if k not in ("field", "queryRef")}
                    if extra:
                        entry.setdefault("projections", {})[
                            _projection_ref(proj)] = extra
            if entry:
                out[bucket] = entry
    return out


def _visual_flat(v: dict) -> dict:
    """Flattened visual.json minus identity/position core/type/bindings/
    filters: what the ``formatting`` section compares."""
    top = {k: x for k, x in v.items()
           if k not in ("$schema", "name", "position", "visual", "filterConfig")}
    flat = _flat(top)
    pos = v.get("position")
    if isinstance(pos, dict):
        extra = {k: x for k, x in pos.items()
                 if k not in ("x", "y", "z", "width", "height")}
        for path, val in _flat(extra).items():
            flat[f"position.{path}" if path else "position"] = val
    vis = v.get("visual")
    if isinstance(vis, dict):
        inner = {k: x for k, x in vis.items() if k not in ("visualType", "query")}
        flat.update(_flat(inner))
        query = vis.get("query")
        if isinstance(query, dict):
            rest = {k: x for k, x in query.items() if k != "queryState"}
            for path, val in _flat(rest).items():
                flat[f"query.{path}" if path else "query"] = val
    extras = _query_extras(v)
    for path, val in _flat(extras).items():
        flat[f"query.queryState.{path}" if path else "query.queryState"] = val
    return flat


def _pos_pair(pos: dict, keys: tuple[str, ...]) -> dict:
    return {k: pos.get(k) for k in keys}


def _diff_visual(vid: str, a: dict, b: dict, ctx: _Ctx) -> dict | None:
    entry: dict = {"id": vid, "type": _vtype(b)}
    changed = False
    pa = a.get("position") if isinstance(a.get("position"), dict) else {}
    pb = b.get("position") if isinstance(b.get("position"), dict) else {}
    if not (_same(pa.get("x"), pb.get("x")) and _same(pa.get("y"), pb.get("y"))):
        entry["moved"] = {"from": _pos_pair(pa, ("x", "y")),
                          "to": _pos_pair(pb, ("x", "y"))}
    if not (_same(pa.get("width"), pb.get("width"))
            and _same(pa.get("height"), pb.get("height"))):
        entry["resized"] = {"from": _pos_pair(pa, ("width", "height")),
                            "to": _pos_pair(pb, ("width", "height"))}
    if not _same(pa.get("z"), pb.get("z")):
        entry["z_order"] = {"from": pa.get("z"), "to": pb.get("z")}
    ta, tb = _vtype(a), _vtype(b)
    if ta != tb:
        entry["type_changed"] = {"from": ta, "to": tb}
    bind = _diff_bindings(_bindings(a), _bindings(b))
    if bind:
        entry["bindings"] = bind
    title_a = _extract_title(a.get("visual") or {}) \
        if isinstance(a.get("visual"), dict) else None
    title_b = _extract_title(b.get("visual") or {}) \
        if isinstance(b.get("visual"), dict) else None
    title_reported = title_a != title_b
    if title_reported:
        entry["title_changed"] = {"from": title_a, "to": title_b}
    filters = _diff_filters(_filters_of(a), _filters_of(b), ctx)
    if filters:
        entry["filters"] = filters
    fa, fb = _visual_flat(a), _visual_flat(b)
    if title_reported:
        fa.pop(_TITLE_PATH, None)
        fb.pop(_TITLE_PATH, None)
    fmt = _format_section(ctx, _flat_diff(fa, fb))
    if fmt:
        entry["formatting"] = fmt
    changed = len(entry) > 2
    if changed:
        ctx.hit("visuals_changed")
        return entry
    return None


def _brief_visual(vid: str, v: dict) -> dict:
    vis = v.get("visual")
    return {"id": vid, "type": _vtype(v),
            "title": _extract_title(vis) if isinstance(vis, dict) else None}


def _page_name(pid: str, pj: dict) -> str:
    return pj.get("displayName") or pj.get("name") or pid


def _is_hidden(pj: dict) -> bool:
    return pj.get("visibility") in _HIDDEN_VISIBILITIES


def _page_flat(pj: dict) -> dict:
    rest = {k: v for k, v in pj.items()
            if k not in ("$schema", "name", "displayName", "width", "height",
                         "visibility")}
    return _flat(_strip_filters(rest))


def _diff_report(a: dict, b: dict, ctx: _Ctx, only_page: str | None) -> dict:
    pa, pb = a["pages"], b["pages"]
    ids_a = set(pa) if only_page is None else ({only_page} & set(pa))
    ids_b = set(pb) if only_page is None else ({only_page} & set(pb))

    pages: dict = {"added": [], "removed": [], "renamed": [], "resized": [],
                   "hidden_changed": [], "reordered": None,
                   "active_page": None}
    for pid in sorted(ids_b - ids_a):
        pages["added"].append({"id": pid, "name": _page_name(pid, pb[pid]["json"]),
                               "visual_count": len(pb[pid]["visuals"])})
    for pid in sorted(ids_a - ids_b):
        pages["removed"].append({"id": pid,
                                 "name": _page_name(pid, pa[pid]["json"]),
                                 "visual_count": len(pa[pid]["visuals"])})
    details = []
    for pid in sorted(ids_a & ids_b):
        ja, jb = pa[pid]["json"], pb[pid]["json"]
        na, nb = _page_name(pid, ja), _page_name(pid, jb)
        if na != nb:
            pages["renamed"].append({"id": pid, "from": na, "to": nb})
        if not (_same(ja.get("width"), jb.get("width"))
                and _same(ja.get("height"), jb.get("height"))):
            pages["resized"].append({
                "id": pid,
                "from": {"width": ja.get("width"), "height": ja.get("height")},
                "to": {"width": jb.get("width"), "height": jb.get("height")}})
        if _is_hidden(ja) != _is_hidden(jb):
            pages["hidden_changed"].append({"id": pid, "name": nb,
                                            "from": _is_hidden(ja),
                                            "to": _is_hidden(jb)})
        detail: dict = {"id": pid, "name": nb}
        va, vb = pa[pid]["visuals"], pb[pid]["visuals"]
        v_added = [_brief_visual(v, vb[v]) for v in sorted(set(vb) - set(va))]
        v_removed = [_brief_visual(v, va[v]) for v in sorted(set(va) - set(vb))]
        v_changed = []
        for vid in sorted(set(va) & set(vb)):
            if va[vid] != vb[vid]:
                entry = _diff_visual(vid, va[vid], vb[vid], ctx)
                if entry:
                    v_changed.append(entry)
        ctx.hit("visuals_added", len(v_added))
        ctx.hit("visuals_removed", len(v_removed))
        if v_added or v_removed or v_changed:
            detail["visuals"] = {"added": v_added, "removed": v_removed,
                                 "changed": v_changed}
        pfilters = _diff_filters(_filters_of(ja), _filters_of(jb), ctx)
        if pfilters:
            detail["filters"] = pfilters
        pfmt = _format_section(ctx, _flat_diff(_page_flat(ja), _page_flat(jb)))
        if pfmt:
            detail["formatting"] = pfmt
        if len(detail) > 2:
            details.append(detail)

    ctx.hit("pages_added", len(pages["added"]))
    ctx.hit("pages_removed", len(pages["removed"]))
    ctx.hit("pages_renamed", len(pages["renamed"]))
    ctx.hit("pages_resized", len(pages["resized"]))
    ctx.hit("pages_hidden_changed", len(pages["hidden_changed"]))

    out: dict = {"present": {"base": True, "other": True}, "pages": pages,
                 "page_details": details}
    if only_page is None:
        common_a = [i for i in a["order"] if i in pb]
        common_b = [i for i in b["order"] if i in pa]
        if common_a != common_b:
            pages["reordered"] = {"from": common_a, "to": common_b}
            ctx.hit("pages_reordered")
        if a["active"] != b["active"]:
            pages["active_page"] = {"from": a["active"], "to": b["active"]}
            ctx.hit("active_page_changed")
        ra, rb = a["report"], b["report"]
        out["filters"] = _diff_filters(_filters_of(ra), _filters_of(rb), ctx) \
            or {"added": [], "removed": [], "changed": []}
        out["formatting"] = _format_section(ctx, _flat_diff(
            _flat(_strip_filters({k: v for k, v in ra.items()
                                  if k not in ("$schema", "themeCollection")})),
            _flat(_strip_filters({k: v for k, v in rb.items()
                                  if k not in ("$schema", "themeCollection")}))))
        out["theme"] = _diff_theme(a["theme"], b["theme"], ctx)
        out["bookmarks"] = _diff_bookmarks(a["bookmarks"], b["bookmarks"], ctx)
        out["report_measures"] = _diff_report_measures(
            a["extensions"], b["extensions"], ctx)
    return out


def _diff_bookmarks(a: dict, b: dict, ctx: _Ctx) -> dict:
    def brief(k: str, d: dict) -> dict:
        return {"id": k, "display_name": d.get("displayName")}

    added = [brief(k, b[k]) for k in sorted(set(b) - set(a))]
    removed = [brief(k, a[k]) for k in sorted(set(a) - set(b))]
    changed = []
    for k in sorted(set(a) & set(b)):
        if a[k] != b[k]:
            entry = brief(k, b[k])
            fmt = _format_section(ctx, _flat_diff(_flat(a[k]), _flat(b[k])))
            if fmt:
                entry["formatting"] = fmt
            changed.append(entry)
    ctx.hit("bookmarks_added", len(added))
    ctx.hit("bookmarks_removed", len(removed))
    ctx.hit("bookmarks_changed", len(changed))
    return {"added": added, "removed": removed, "changed": changed}


def _extension_measures(ext: Any) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if isinstance(ext, dict):
        for entity in ext.get("entities") or []:
            if not isinstance(entity, dict):
                continue
            for m in entity.get("measures") or []:
                if isinstance(m, dict) and m.get("name") is not None:
                    out[f"{entity.get('name')}.{m['name']}"] = m
    return out


def _diff_report_measures(a: Any, b: Any, ctx: _Ctx) -> dict:
    ma, mb = _extension_measures(a), _extension_measures(b)
    added = [{"measure": k, "expression": mb[k].get("expression")}
             for k in sorted(set(mb) - set(ma))]
    removed = [{"measure": k, "expression": ma[k].get("expression")}
               for k in sorted(set(ma) - set(mb))]
    changed = []
    for k in sorted(set(ma) & set(mb)):
        if ma[k] == mb[k]:
            continue
        entry: dict = {"measure": k}
        if ma[k].get("expression") != mb[k].get("expression"):
            entry["dax"] = {"diff": _udiff(ma[k].get("expression"),
                                           mb[k].get("expression"))}
        for prop in sorted((set(ma[k]) | set(mb[k])) - {"name", "expression"}):
            if not _same(ma[k].get(prop), mb[k].get(prop)):
                entry[prop] = {"from": ma[k].get(prop), "to": mb[k].get(prop)}
        changed.append(entry)
    ctx.hit("report_measures_added", len(added))
    ctx.hit("report_measures_removed", len(removed))
    ctx.hit("report_measures_changed", len(changed))
    return {"added": added, "removed": removed, "changed": changed}


# --- TMDL scanning ---------------------------------------------------------------

_PROP_COLON = re.compile(r"^([A-Za-z_][\w.]*)\s*:\s*(.*)$")
_PROP_EQ = re.compile(r"^([A-Za-z_][\w.]*)\s*=\s*(.*)$")
_PROP_FLAG = re.compile(r"^[A-Za-z_][\w.]*$")
_SKIP_PREFIXES = ("annotation ", "//", "changedProperty ", "extendedProperty ")


def _parse_prop_line(s: str) -> tuple[str, Any, bool] | None:
    """(key, value, is_expression_property) for a TMDL property line."""
    m = _PROP_COLON.match(s)
    if m:
        return m.group(1), m.group(2).strip(), False
    m = _PROP_EQ.match(s)
    if m:
        return m.group(1), m.group(2).strip(), True
    if _PROP_FLAG.match(s):
        return s, True, False
    return None


def _split_name_expr(rest: str) -> tuple[str, str, bool]:
    """Split ``Name = expr`` (Name may be 'quoted'); -> (name, expr, has_eq)."""
    rest = rest.strip()
    if rest.startswith("'"):
        j = 1
        while j < len(rest):
            if rest[j] == "'":
                if j + 1 < len(rest) and rest[j + 1] == "'":
                    j += 2
                    continue
                break
            j += 1
        raw, tail = rest[: j + 1], rest[j + 1:].strip()
    else:
        m = re.match(r"^([^\s=]+)\s*(.*)$", rest, re.S)
        raw, tail = (m.group(1), m.group(2)) if m else (rest, "")
    if tail.startswith("="):
        return _unquote(raw), tail[1:].strip(), True
    return _unquote(raw), "", False


def _scan_member(lines: list[str], i: int) -> tuple[dict, int]:
    """Scan one member block (measure/column/partition/calculationItem)."""
    member_indent = _leading_tabs(lines[i])
    kind, _, rest = lines[i].strip().partition(" ")
    name, inline, has_eq = _split_name_expr(rest)
    i += 1
    expr: str | None = None
    if has_eq:
        if inline.startswith(_FENCE):
            block: list[str] = []
            while i < len(lines):
                if lines[i].strip() == _FENCE:
                    i += 1
                    break
                block.append(lines[i])
                i += 1
            expr = _dedent(block)
        else:
            expr = inline
    props: dict = {}
    body: list[str] = []
    prop_body: dict[str, list[str]] = {}
    last_expr_key: str | None = None
    body_done = False
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        ind = _leading_tabs(line)
        if ind <= member_indent:
            break
        s = line.strip()
        if ind == member_indent + 1:
            if s.startswith(_SKIP_PREFIXES):
                body_done = True
                last_expr_key = None
                i += 1
                continue
            parsed = _parse_prop_line(s)
            if parsed:
                key, val, is_expr = parsed
                props[key] = val
                last_expr_key = key if (is_expr and val == "") else None
                body_done = True
            elif has_eq and not body_done:
                body.append(line)      # unrecognised +1 line: expression body
            i += 1
            continue
        if last_expr_key is not None:
            prop_body.setdefault(last_expr_key, []).append(line)
        elif has_eq and not body_done:
            body.append(line)
        i += 1
    for key, blines in prop_body.items():
        props[key] = _dedent(blines)
    if has_eq and not expr and body:
        expr = _dedent(body)
    return {"kind": kind, "name": name, "expr": expr, "props": props}, i


def _scan_calc_group(lines: list[str], i: int) -> tuple[dict, int]:
    base_indent = _leading_tabs(lines[i])
    i += 1
    group: dict = {"props": {}, "items": {}, "order": []}
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        ind = _leading_tabs(line)
        if ind <= base_indent:
            break
        s = line.strip()
        if ind == base_indent + 1 and s.startswith("calculationItem "):
            item, i = _scan_member(lines, i)
            group["items"][item["name"]] = item
            group["order"].append(item["name"])
            continue
        if ind == base_indent + 1 and not s.startswith(_SKIP_PREFIXES):
            parsed = _parse_prop_line(s)
            if parsed:
                group["props"][parsed[0]] = parsed[1]
        i += 1
    return group, i


def _scan_table(text: str) -> dict:
    lines = [ln.rstrip("\r") for ln in text.split("\n")]
    n = len(lines)
    i = 0
    name = None
    while i < n:
        s = lines[i].strip()
        if s.startswith("table "):
            name = _split_name_expr(s[len("table "):])[0]
            i += 1
            break
        i += 1
    if name is None:
        raise ValueError("no `table` declaration found")
    table: dict = {"name": name, "props": {}, "columns": {}, "measures": {},
                   "partitions": {}, "calc_group": None}
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        ind = _leading_tabs(line)
        if ind == 0:
            break
        s = line.strip()
        if ind != 1 or s.startswith(_SKIP_PREFIXES):
            i += 1
            continue
        if s.startswith("measure "):
            m, i = _scan_member(lines, i)
            table["measures"][m["name"]] = m
        elif s.startswith("column "):
            m, i = _scan_member(lines, i)
            table["columns"][m["name"]] = m
        elif s.startswith("partition "):
            m, i = _scan_member(lines, i)
            table["partitions"][m["name"]] = m
        elif s.startswith("calculationGroup"):
            table["calc_group"], i = _scan_calc_group(lines, i)
        else:
            parsed = _parse_prop_line(s)
            if parsed:
                table["props"][parsed[0]] = parsed[1]
            i += 1
    return table


def _scan_relationships(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    rels: dict[str, dict] = {}
    cur: dict | None = None

    def flush() -> None:
        if cur is None:
            return
        props = cur["props"]
        if "fromColumn" not in props or "toColumn" not in props:
            return
        ft, fc = _split_col_ref(str(props["fromColumn"]))
        tt, tc = _split_col_ref(str(props["toColumn"]))
        active = props.get("isActive", True)
        extra = {k: v for k, v in props.items()
                 if k not in ("fromColumn", "toColumn", "isActive",
                              "crossFilteringBehavior", "fromCardinality",
                              "toCardinality")}
        rels[f"{ft}.{fc}->{tt}.{tc}"] = {
            "from": f"{ft}.{fc}", "to": f"{tt}.{tc}",
            "cardinality": f"{props.get('fromCardinality', 'many')}-to-"
                           f"{props.get('toCardinality', 'one')}",
            "direction": props.get("crossFilteringBehavior", "oneDirection"),
            "active": not (active is False or str(active).lower() == "false"),
            "extra": extra,
        }

    text = path.read_text(encoding="utf-8-sig")
    for line in text.split("\n"):
        line = line.rstrip("\r")
        s = line.strip()
        if not s or s.startswith("//"):
            continue
        if _leading_tabs(line) == 0 and s.startswith("relationship "):
            flush()
            cur = {"name": s[len("relationship "):].strip(), "props": {}}
        elif cur is not None and _leading_tabs(line) >= 1:
            parsed = _parse_prop_line(s)
            if parsed:
                cur["props"][parsed[0]] = parsed[1]
    flush()
    return rels


def _has_model(project) -> bool:
    try:
        project._require_model()
        return True
    except FileNotFoundError:
        return False


def _load_model(project) -> dict:
    model_dir = project._require_model()
    tables_dir = model_dir / "definition" / "tables"
    tables: dict[str, dict] = {}
    if tables_dir.is_dir():
        for f in sorted(tables_dir.glob("*.tmdl")):
            try:
                t = _scan_table(f.read_text(encoding="utf-8-sig"))
            except ValueError as exc:
                raise ValueError(f"Cannot read table file {f}: {exc}") from exc
            tables[t["name"]] = t
    return {"tables": tables,
            "relationships": _scan_relationships(
                model_dir / "definition" / "relationships.tmdl")}


# --- model diff --------------------------------------------------------------------

def _truthy(v: Any) -> bool:
    return v is True or (isinstance(v, str) and v.strip().lower() == "true")


def _unq(v: Any) -> Any:
    return _unquote(v) if isinstance(v, str) and v.startswith("'") else v


def _measure_view(m: dict) -> dict:
    p = m["props"]
    return {"dax": (m["expr"] or "").strip(),
            "format_string": p.get("formatString"),
            "display_folder": _unq(p.get("displayFolder")),
            "hidden": _truthy(p.get("isHidden"))}


def _column_view(m: dict) -> dict:
    p = m["props"]
    return {"data_type": p.get("dataType"),
            "format_string": p.get("formatString"),
            "hidden": _truthy(p.get("isHidden")),
            "sort_by": _unq(p.get("sortByColumn")),
            "summarize_by": p.get("summarizeBy"),
            "data_category": p.get("dataCategory"),
            "key": _truthy(p.get("isKey")),
            "display_folder": _unq(p.get("displayFolder")),
            "expression": (m["expr"].strip() if m["expr"] is not None else None)}


def _attr_changes(va: dict, vb: dict, text_keys: tuple[str, ...]) -> dict:
    out: dict = {}
    for k in vb:
        if _same(va.get(k), vb.get(k)):
            continue
        out[k] = ({"diff": _udiff(va.get(k), vb.get(k))} if k in text_keys
                  else {"from": va.get(k), "to": vb.get(k)})
    return out


def _flat_members(tables: dict, section: str, view) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for tname, t in tables.items():
        for mname, m in t[section].items():
            out[f"{tname}.{mname}"] = {"table": tname, "name": mname,
                                       **view(m)}
    return out


def _table_brief(t: dict) -> dict:
    return {"name": t["name"], "columns": len(t["columns"]),
            "measures": len(t["measures"]),
            "calculation_group": t["calc_group"] is not None}


def _diff_model(a: dict, b: dict, ctx: _Ctx) -> dict:
    ta, tb = a["tables"], b["tables"]
    tables = {"added": [_table_brief(tb[n]) for n in sorted(set(tb) - set(ta))],
              "removed": [_table_brief(ta[n]) for n in sorted(set(ta) - set(tb))],
              "changed": []}
    for n in sorted(set(ta) & set(tb)):
        entry: dict = {"name": n}
        ha, hb = _truthy(ta[n]["props"].get("isHidden")), \
            _truthy(tb[n]["props"].get("isHidden"))
        if ha != hb:
            entry["hidden"] = {"from": ha, "to": hb}
        ca, cb = ta[n]["props"].get("dataCategory"), tb[n]["props"].get("dataCategory")
        if ca != cb:
            entry["data_category"] = {"from": ca, "to": cb}
        if len(entry) > 1:
            tables["changed"].append(entry)

    out: dict = {"present": {"base": True, "other": True}, "tables": tables}

    ma = _flat_members(ta, "measures", _measure_view)
    mb = _flat_members(tb, "measures", _measure_view)
    measures: dict = {
        "added": [mb[k] for k in sorted(set(mb) - set(ma))],
        "removed": [ma[k] for k in sorted(set(ma) - set(mb))],
        "changed": []}
    for k in sorted(set(ma) & set(mb)):
        ch = _attr_changes(ma[k], mb[k], ("dax",))
        ch.pop("table", None)
        ch.pop("name", None)
        if ch:
            measures["changed"].append(
                {"table": mb[k]["table"], "name": mb[k]["name"], **ch})
    out["measures"] = measures

    ca_ = _flat_members(ta, "columns", _column_view)
    cb_ = _flat_members(tb, "columns", _column_view)

    def col_brief(c: dict) -> dict:
        d = {"table": c["table"], "name": c["name"], "data_type": c["data_type"]}
        if c["hidden"]:
            d["hidden"] = True
        if c["expression"] is not None:
            d["calculated"] = True
        return d

    columns: dict = {
        "added": [col_brief(cb_[k]) for k in sorted(set(cb_) - set(ca_))],
        "removed": [col_brief(ca_[k]) for k in sorted(set(ca_) - set(cb_))],
        "changed": []}
    for k in sorted(set(ca_) & set(cb_)):
        ch = _attr_changes(ca_[k], cb_[k], ("expression",))
        ch.pop("table", None)
        ch.pop("name", None)
        if ch:
            columns["changed"].append(
                {"table": cb_[k]["table"], "name": cb_[k]["name"], **ch})
    out["columns"] = columns

    ra, rb = a["relationships"], b["relationships"]

    def rel_brief(r: dict) -> dict:
        return {"from": r["from"], "to": r["to"],
                "cardinality": r["cardinality"], "direction": r["direction"],
                "active": r["active"]}

    rels: dict = {"added": [rel_brief(rb[k]) for k in sorted(set(rb) - set(ra))],
                  "removed": [rel_brief(ra[k]) for k in sorted(set(ra) - set(rb))],
                  "changed": []}
    for k in sorted(set(ra) & set(rb)):
        entry = {"from": rb[k]["from"], "to": rb[k]["to"]}
        for attr in ("cardinality", "direction", "active"):
            if ra[k][attr] != rb[k][attr]:
                entry[attr] = {"from": ra[k][attr], "to": rb[k][attr]}
        extra = _flat_diff(_flat(ra[k]["extra"]), _flat(rb[k]["extra"]))
        if extra:
            entry["properties"] = extra
        if len(entry) > 2:
            rels["changed"].append(entry)
    out["relationships"] = rels

    out["calculation_groups"] = _diff_calc_groups(ta, tb)

    def parts(tables_: dict) -> dict[str, dict]:
        res = {}
        for tname, t in tables_.items():
            for pname, p in t["partitions"].items():
                res[f"{tname}.{pname}"] = {
                    "table": tname, "name": pname, "type": p["expr"],
                    "mode": p["props"].get("mode"),
                    "source": (str(p["props"]["source"]).strip()
                               if "source" in p["props"] else None)}
        return res

    pa, pb = parts(ta), parts(tb)
    partitions: dict = {
        "added": [{"table": pb[k]["table"], "name": pb[k]["name"],
                   "type": pb[k]["type"]} for k in sorted(set(pb) - set(pa))],
        "removed": [{"table": pa[k]["table"], "name": pa[k]["name"],
                     "type": pa[k]["type"]} for k in sorted(set(pa) - set(pb))],
        "changed": []}
    for k in sorted(set(pa) & set(pb)):
        ch = _attr_changes(pa[k], pb[k], ("source",))
        ch.pop("table", None)
        ch.pop("name", None)
        if ch:
            partitions["changed"].append(
                {"table": pb[k]["table"], "name": pb[k]["name"], **ch})
    out["partitions"] = partitions

    for section in ("tables", "measures", "columns", "relationships",
                    "calculation_groups", "partitions"):
        for kind in ("added", "removed", "changed"):
            ctx.hit(f"{section}_{kind}", len(out[section][kind]))
    return out


def _num(v: Any) -> Any:
    """'5' -> 5 for numeric TMDL values; anything else unchanged."""
    if isinstance(v, str):
        try:
            return int(v.strip())
        except ValueError:
            return v
    return v


def _diff_calc_groups(ta: dict, tb: dict) -> dict:
    ga = {n: t["calc_group"] for n, t in ta.items() if t["calc_group"]}
    gb = {n: t["calc_group"] for n, t in tb.items() if t["calc_group"]}

    def brief(name: str, g: dict) -> dict:
        return {"name": name, "precedence": _num(g["props"].get("precedence")),
                "items": list(g["order"])}

    out: dict = {"added": [brief(n, gb[n]) for n in sorted(set(gb) - set(ga))],
                 "removed": [brief(n, ga[n]) for n in sorted(set(ga) - set(gb))],
                 "changed": []}
    for n in sorted(set(ga) & set(gb)):
        x, y = ga[n], gb[n]
        entry: dict = {"name": n}
        px, py = _num(x["props"].get("precedence")), _num(y["props"].get("precedence"))
        if px != py:
            entry["precedence"] = {"from": px, "to": py}
        added = sorted(set(y["items"]) - set(x["items"]))
        removed = sorted(set(x["items"]) - set(y["items"]))
        changed = []
        for item in sorted(set(x["items"]) & set(y["items"])):
            ia, ib = x["items"][item], y["items"][item]
            ch: dict = {}
            if (ia["expr"] or "").strip() != (ib["expr"] or "").strip():
                ch["dax"] = {"diff": _udiff((ia["expr"] or "").strip(),
                                            (ib["expr"] or "").strip())}
            for prop in sorted((set(ia["props"]) | set(ib["props"]))
                               - {"lineageTag"}):
                if not _same(ia["props"].get(prop), ib["props"].get(prop)):
                    ch[prop] = {"from": ia["props"].get(prop),
                                "to": ib["props"].get(prop)}
            if ch:
                changed.append({"name": item, **ch})
        if added:
            entry["items_added"] = added
        if removed:
            entry["items_removed"] = removed
        if changed:
            entry["items_changed"] = changed
        if not (added or removed) and \
                [i for i in x["order"] if i in y["items"]] != \
                [i for i in y["order"] if i in x["items"]]:
            entry["items_reordered"] = True
        if len(entry) > 1:
            out["changed"].append(entry)
    return out


# --- entry point ----------------------------------------------------------------------

def semantic_diff(base, other, *, page_id: str | None = None,
                  include_model: bool = True,
                  max_format_changes: int = DEFAULT_MAX_FORMAT_CHANGES) -> dict:
    """Structured diff ``base`` -> ``other`` (both ``PbipProject``).

    ``page_id`` restricts the report layer to one page (report-level sections
    -- filters, theme, bookmarks, report measures -- are then omitted);
    ``include_model=False`` skips the model layer.
    """
    if not isinstance(max_format_changes, int) or max_format_changes < 0:
        raise ValueError("max_format_changes must be an integer >= 0")
    ctx = _Ctx(max_format_changes)

    rep_a, rep_b = _load_report(base), _load_report(other)
    if page_id is not None:
        known = sorted(set((rep_a or {}).get("pages", {}))
                       | set((rep_b or {}).get("pages", {})))
        if page_id not in known:
            raise ValueError(f"Page {page_id!r} exists in neither project "
                             f"(known page ids: {known})")

    report: dict
    if rep_a is not None and rep_b is not None:
        report = _diff_report(rep_a, rep_b, ctx, page_id)
    else:
        report = {"present": {"base": rep_a is not None,
                              "other": rep_b is not None}}
        if rep_a is not None or rep_b is not None:
            ctx.hit("layers_missing")
            report["skipped"] = ("report layer exists in only one project; "
                                 "nothing to compare")

    model: dict
    if not include_model:
        model = {"included": False}
    else:
        has_a, has_b = _has_model(base), _has_model(other)
        if has_a and has_b:
            model = _diff_model(_load_model(base), _load_model(other), ctx)
        else:
            model = {"present": {"base": has_a, "other": has_b}}
            if has_a != has_b:
                ctx.hit("layers_missing")
                model["skipped"] = ("semantic model exists in only one "
                                    "project; nothing to compare")

    counts = dict(sorted(ctx.counts.items()))
    total = sum(counts.values())
    counts["formatting_shown"] = ctx.shown
    counts["total_changes"] = total
    return {
        "identical": total == 0,
        "projects": {"base": str(base.path), "other": str(other.path)},
        "scope": {"page_id": page_id, "include_model": include_model,
                  "max_format_changes": max_format_changes},
        "summary": counts,
        "report": report,
        "model": model,
    }
