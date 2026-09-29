"""Report-level measures and the phone (mobile) layout.

Report-level measures
---------------------
``definition/reportExtensions.json`` (schema ``reportExtension/1.0.0``) holds
measures that live in the *report*, not the semantic model::

    {"$schema": ".../reportExtension/1.0.0/schema.json",
     "name": "extension",
     "entities": [{"name": "Sales",            # must be a model table
                   "measures": [{"name": "...", "dataType": "Double",
                                 "expression": "DAX", "formatString": "0.0%",
                                 "description": "..."}]}]}

Visuals bind them exactly like model measures (``Measure`` field with
``Entity``/``Property``), so a delete guard must look at the report files.
``core.usage.classify_usage`` only walks model fields: a bound report measure
is ignored there (it is not a model column or measure), which is harmless; the
guard for report measures is ``report_measure_bindings`` below.

Phone layout
------------
PBIR stores the phone layout as one optional sidecar per visual,
``pages/<page>/visuals/<visual>/mobile.json`` (schema
``visualContainerMobileState``: a required mobile ``position`` plus optional
mobile-specific ``objects`` / ``visualContainerObjects``). ``report.json``
carries ``layoutOptimization`` (``None`` | ``PhonePortrait``) which we keep in
step with whether any ``mobile.json`` exists. The phone canvas is 320 wide.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from core import schema_validate
from core.accessibility import (
    DECORATIVE_TYPES, is_hidden, reading_order,
)

_BASE = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/"
EXTENSION_SCHEMA = _BASE + "reportExtension/1.0.0/schema.json"
MOBILE_SCHEMA = _BASE + "visualContainerMobileState/1.0.0/schema.json"
EXTENSION_FILE = "reportExtensions.json"
EXTENSION_NAME = "extension"

DATA_TYPES = ["Binary", "Boolean", "Date", "DateTime", "DateTimeZone", "Decimal",
              "Double", "Duration", "Integer", "Json", "None", "Null", "Text",
              "Time", "Variant"]
_TYPE_ALIASES = {"string": "Text", "int": "Integer", "int64": "Integer",
                 "whole": "Integer", "float": "Double", "number": "Double",
                 "currency": "Decimal", "bool": "Boolean", "datetime": "DateTime"}

MOBILE_WIDTH = 320
MOBILE_MIN_HEIGHT = 80
MOBILE_GAP = 8


def _definition(project) -> Path:
    return project._require_report() / "definition"


# =============================================================================
# report-level measures
# =============================================================================

def extension_path(project) -> Path:
    return _definition(project) / EXTENSION_FILE


def _blank_extension() -> dict:
    return {"$schema": EXTENSION_SCHEMA, "name": EXTENSION_NAME, "entities": []}


def load_extension(project) -> dict | None:
    """The parsed reportExtensions.json, or None when the report has none."""
    path = extension_path(project)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _save_extension(project, obj: dict) -> None:
    """Validate against the vendored schema, then write with the file's own
    style preserved (new file: LF, no BOM)."""
    schema_validate.assert_valid("reportExtension", obj, context=EXTENSION_FILE)
    project._write_json(extension_path(project), obj, validate=False)


def _normalize_type(data_type: str | None) -> str:
    if data_type is None:
        return "Double"
    key = str(data_type).strip()
    for t in DATA_TYPES:
        if t.lower() == key.lower():
            return t
    if key.lower() in _TYPE_ALIASES:
        return _TYPE_ALIASES[key.lower()]
    raise ValueError(f"Unknown data_type {data_type!r}; use one of {DATA_TYPES}")


def _model_tables(project):
    try:
        return project.list_tables()
    except FileNotFoundError:
        return None  # report-only project: cannot cross-check the model


def _flat(ext: dict | None):
    for ent in (ext or {}).get("entities", []) or []:
        for m in ent.get("measures", []) or []:
            yield ent["name"], m


def list_report_measures(project) -> list[dict]:
    """Every report-level measure: table, name, dax, data type, format, note."""
    return [{"table": t, "name": m["name"], "dax": m.get("expression", ""),
             "data_type": m.get("dataType"),
             "format_string": m.get("formatString"),
             "description": m.get("description")}
            for t, m in _flat(load_extension(project))]


def _find(ext: dict | None, table: str, name: str):
    """(entity dict, measure dict) or (None, None)."""
    for ent in (ext or {}).get("entities", []) or []:
        if ent.get("name") != table:
            continue
        for m in ent.get("measures", []) or []:
            if m.get("name") == name:
                return ent, m
    return None, None


def _dax_warnings(project, dax: str, ext_names: set[str]) -> list[str]:
    try:
        return project._lint_dax(dax, ext_names)
    except FileNotFoundError:
        return []


def create_report_measure(project, table: str, name: str, dax: str,
                          format_string: str | None = None,
                          description: str | None = None,
                          data_type: str | None = None) -> dict:
    if not (isinstance(name, str) and name.strip()):
        raise ValueError("name must be a non-empty string")
    if not (isinstance(dax, str) and dax.strip()):
        raise ValueError("dax must be a non-empty DAX expression")
    if not (isinstance(table, str) and table.strip()):
        raise ValueError("table must name a semantic-model table")
    dtype = _normalize_type(data_type)

    tables = _model_tables(project)
    if tables is not None:
        by_name = {t.name: t for t in tables}
        if table not in by_name:
            raise ValueError(f"Table {table!r} is not in the semantic model "
                             f"(have: {sorted(by_name)}). A report measure "
                             "must hang off an existing table.")
        if any(c.name == name for c in by_name[table].columns):
            raise ValueError(f"{table!r} already has a column named {name!r}")
        for m in project.list_measures():
            if m.name == name:
                raise ValueError(
                    f"Measure {name!r} already exists in the model (table "
                    f"{m.table!r}); measure names are unique model-wide.")

    ext = load_extension(project) or _blank_extension()
    existing = {m["name"] for _, m in _flat(ext)}
    if name in existing:
        raise ValueError(f"Report measure {name!r} already exists")

    measure: dict = {"name": name, "dataType": dtype, "expression": dax}
    if format_string:
        measure["formatString"] = format_string
    if description:
        measure["description"] = description
    entity = next((e for e in ext.setdefault("entities", [])
                   if e.get("name") == table), None)
    if entity is None:
        entity = {"name": table, "measures": []}
        ext["entities"].append(entity)
    entity.setdefault("measures", []).append(measure)
    _save_extension(project, ext)

    result = {"ok": True, "action": "created", "table": table, "name": name,
              "data_type": dtype, "file": f"definition/{EXTENSION_FILE}"}
    warnings = _dax_warnings(project, dax, existing | {name})
    if warnings:
        result["warnings"] = warnings
    return result


def update_report_measure(project, table: str, name: str,
                          dax: str | None = None,
                          format_string: str | None = None,
                          description: str | None = None) -> dict:
    """Partial update; None keeps a field, an empty string clears the
    optional ones (format_string, description)."""
    ext = load_extension(project)
    _, measure = _find(ext, table, name)
    if measure is None:
        where = [t for t, m in _flat(ext) if m["name"] == name]
        hint = f" (it lives in table {where[0]!r})" if where else ""
        raise ValueError(f"Report measure {name!r} not found in table "
                         f"{table!r}{hint}; see pbi_list_report_measures.")
    if dax is None and format_string is None and description is None:
        raise ValueError("Nothing to update: pass dax, format_string or "
                         "description")
    if dax is not None:
        if not dax.strip():
            raise ValueError("dax must be a non-empty DAX expression")
        measure["expression"] = dax
    for key, val in (("formatString", format_string),
                     ("description", description)):
        if val is None:
            continue
        if val == "":
            measure.pop(key, None)
        else:
            measure[key] = val
    _save_extension(project, ext)
    result = {"ok": True, "action": "updated", "table": table, "name": name}
    warnings = _dax_warnings(project, measure["expression"],
                             {m["name"] for _, m in _flat(ext)})
    if warnings:
        result["warnings"] = warnings
    return result


def _has_queryref(node, qref: str) -> bool:
    if isinstance(node, dict):
        if node.get("queryRef") == qref:
            return True
        return any(_has_queryref(v, qref) for v in node.values())
    if isinstance(node, list):
        return any(_has_queryref(v, qref) for v in node)
    return False


def report_measure_bindings(project, table: str, name: str) -> list[str]:
    """Report files (relative to ``definition/``) that bind ``table.name``:
    visuals, pages, report/visual filters, bookmarks... Found by walking every
    definition JSON for the field pattern or a matching ``queryRef``."""
    from core.usage import _walk_field_refs

    definition = _definition(project)
    qref = f"{table}.{name}"
    hits: list[str] = []
    for jf in sorted(definition.rglob("*.json")):
        if jf.name == EXTENSION_FILE:
            continue
        try:
            data = json.loads(jf.read_text(encoding="utf-8-sig"))
        except ValueError:
            continue
        if (table, name) in _walk_field_refs(data) or _has_queryref(data, qref):
            hits.append(jf.relative_to(definition).as_posix())
    return hits


def _dependent_report_measures(ext: dict | None, name: str) -> list[str]:
    from core.dax_parser import parse_references

    out = []
    for t, m in _flat(ext):
        if m["name"] == name:
            continue
        refs = parse_references(m.get("expression", ""))
        if name in refs.measures or any(col == name for _, col in refs.columns):
            out.append(f"{t}.{m['name']}")
    return out


def delete_report_measure(project, table: str, name: str,
                          force: bool = False) -> dict:
    """Remove a report measure. Refuses while a visual/filter/bookmark binds
    it or another report measure's DAX references it, unless ``force``."""
    ext = load_extension(project)
    entity, measure = _find(ext, table, name)
    if measure is None:
        raise ValueError(f"Report measure {name!r} not found in table "
                         f"{table!r}; see pbi_list_report_measures.")
    bound = report_measure_bindings(project, table, name)
    dependents = _dependent_report_measures(ext, name)
    if (bound or dependents) and not force:
        parts = []
        if bound:
            parts.append(f"bound in {bound}")
        if dependents:
            parts.append(f"referenced by report measures {dependents}")
        raise ValueError(
            f"Refusing to delete report measure {name!r}: "
            + "; ".join(parts)
            + ". Remove those usages first, or pass force=true.")
    entity["measures"] = [m for m in entity["measures"] if m is not measure]
    if not entity["measures"]:
        ext["entities"] = [e for e in ext["entities"] if e is not entity]
    _save_extension(project, ext)
    return {"ok": True, "action": "deleted", "table": table, "name": name,
            "forced_past": {"bindings": bound, "dependents": dependents}
            if force else {}}


# =============================================================================
# phone (mobile) layout
# =============================================================================

def _require_page(project, page_id: str) -> list:
    if page_id not in {p.id for p in project.list_pages()}:
        raise ValueError(f"Page {page_id!r} not found "
                         f"(have: {[p.id for p in project.list_pages()]})")
    return project.list_visuals(page_id)


def _mobile_file(project, page_id: str, visual_id: str) -> Path:
    return (_definition(project) / "pages" / page_id / "visuals" / visual_id
            / "mobile.json")


def _num(value, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number, got {value!r}")
    return float(value)


def _tidy(n: float):
    return int(n) if float(n).is_integer() else round(n, 2)


def _write_mobile(project, page_id: str, visual_id: str, position: dict) -> None:
    path = _mobile_file(project, page_id, visual_id)
    obj = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() \
        else {"$schema": MOBILE_SCHEMA}
    obj["position"] = position
    schema_validate.assert_valid("visualContainerMobileState", obj,
                                 context=f"{visual_id}/mobile.json")
    project._write_json(path, obj, validate=False)


def sync_layout_optimization(project) -> str | None:
    """Keep report.json ``layoutOptimization`` matching the mobile sidecars.
    Returns its value after syncing (None when report.json is absent)."""
    report_json = _definition(project) / "report.json"
    if not report_json.exists():
        return None
    data = json.loads(report_json.read_text(encoding="utf-8-sig"))
    has_mobile = any(_definition(project).glob("pages/*/visuals/*/mobile.json"))
    current = data.get("layoutOptimization")
    if has_mobile and current != "PhonePortrait":
        data["layoutOptimization"] = "PhonePortrait"
        project._write_json(report_json, data)
    elif not has_mobile and current == "PhonePortrait":
        data["layoutOptimization"] = "None"
        project._write_json(report_json, data)
    return data.get("layoutOptimization")


def get_mobile_layout(project, page_id: str) -> dict:
    """The page's phone layout: placed visuals (top to bottom) + unplaced ids."""
    visuals = _require_page(project, page_id)
    placed, unplaced = [], []
    for v in visuals:
        path = _mobile_file(project, page_id, v.id)
        if not path.exists():
            unplaced.append(v.id)
            continue
        pos = json.loads(path.read_text(encoding="utf-8-sig")).get("position", {})
        placed.append({"visual_id": v.id, "x": pos.get("x"), "y": pos.get("y"),
                       "width": pos.get("width"), "height": pos.get("height")})
    placed.sort(key=lambda p: (p["y"] or 0, p["x"] or 0))
    bottom = max(((p["y"] or 0) + (p["height"] or 0) for p in placed), default=0)
    return {"page_id": page_id, "enabled": bool(placed),
            "canvas": {"width": MOBILE_WIDTH, "height": _tidy(bottom)},
            "visuals": placed, "unplaced": unplaced}


def _auto_candidates(visuals: list) -> list:
    return reading_order(
        v for v in visuals
        if not is_hidden(v) and "visualGroup" not in (v.raw or {})
        and v.visual_type not in DECORATIVE_TYPES)


def set_mobile_layout(project, page_id: str, mode: str = "auto",
                      visuals: list[dict] | None = None) -> dict:
    """auto: stack visible visuals; manual: explicit boxes; off: remove."""
    mode = (mode or "").lower()
    if mode not in ("auto", "manual", "off"):
        raise ValueError("mode must be 'auto', 'manual' or 'off'")
    page_visuals = _require_page(project, page_id)
    by_id = {v.id: v for v in page_visuals}

    if mode == "off":
        removed = []
        for v in page_visuals:
            path = _mobile_file(project, page_id, v.id)
            if path.exists():
                path.unlink()
                removed.append(v.id)
        opt = sync_layout_optimization(project)
        return {"ok": True, "page_id": page_id, "mode": "off",
                "removed": removed, "layoutOptimization": opt}

    if mode == "auto":
        if visuals:
            raise ValueError("mode='auto' takes no visuals; use mode='manual' "
                             "to position them yourself")
        chosen = _auto_candidates(page_visuals)
        if not chosen:
            raise ValueError(f"Page {page_id!r} has no visible visuals to lay out")
        y = 0.0
        placed = []
        for i, v in enumerate(chosen):
            w, h = v.position.width, v.position.height
            height = MOBILE_MIN_HEIGHT if w <= 0 else max(
                MOBILE_MIN_HEIGHT, round(h * MOBILE_WIDTH / w))
            pos = {"x": 0, "y": _tidy(y), "z": i, "width": MOBILE_WIDTH,
                   "height": height, "tabOrder": i}
            _write_mobile(project, page_id, v.id, pos)
            placed.append({"visual_id": v.id, "x": 0, "y": _tidy(y),
                           "width": MOBILE_WIDTH, "height": height})
            y += height + MOBILE_GAP
        keep = {p["visual_id"] for p in placed}
        for v in page_visuals:  # a full re-layout: drop stale placements
            path = _mobile_file(project, page_id, v.id)
            if v.id not in keep and path.exists():
                path.unlink()
        opt = sync_layout_optimization(project)
        return {"ok": True, "page_id": page_id, "mode": "auto",
                "visuals": placed, "canvas_height": _tidy(y - MOBILE_GAP),
                "layoutOptimization": opt}

    # manual
    if not visuals:
        raise ValueError("mode='manual' needs visuals=[{'visual_id','x','y',"
                         "'width','height'}, ...]")
    seen: set[str] = set()
    specs = []
    for i, spec in enumerate(visuals):
        if not isinstance(spec, dict) or "visual_id" not in spec:
            raise ValueError(f"visuals[{i}] must be an object with a visual_id")
        vid = spec["visual_id"]
        if vid not in by_id:
            raise ValueError(f"visuals[{i}]: visual {vid!r} is not on page "
                             f"{page_id!r} (have: {sorted(by_id)})")
        if vid in seen:
            raise ValueError(f"visual {vid!r} listed twice")
        seen.add(vid)
        x, y = _num(spec.get("x", 0), f"{vid}.x"), _num(spec.get("y", 0), f"{vid}.y")
        w = _num(spec.get("width"), f"{vid}.width")
        h = _num(spec.get("height"), f"{vid}.height")
        if x < 0 or y < 0:
            raise ValueError(f"{vid}: x and y must be >= 0")
        if w <= 0 or h <= 0:
            raise ValueError(f"{vid}: width and height must be > 0")
        if x + w > MOBILE_WIDTH + 0.01:
            raise ValueError(f"{vid}: x + width = {x + w:g} exceeds the "
                             f"{MOBILE_WIDTH}-wide phone canvas")
        specs.append((vid, x, y, w, h))
    for i, (vid, x, y, w, h) in enumerate(specs):
        old = _mobile_file(project, page_id, vid)
        prev = json.loads(old.read_text(encoding="utf-8-sig")).get(
            "position", {}) if old.exists() else {}
        pos = {"x": _tidy(x), "y": _tidy(y), "z": prev.get("z", i),
               "width": _tidy(w), "height": _tidy(h),
               "tabOrder": prev.get("tabOrder", i)}
        _write_mobile(project, page_id, vid, pos)
    opt = sync_layout_optimization(project)
    out = get_mobile_layout(project, page_id)
    out.update(ok=True, mode="manual", layoutOptimization=opt)
    return out
