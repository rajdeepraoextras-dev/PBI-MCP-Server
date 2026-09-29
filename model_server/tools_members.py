"""Column / table / measure lifecycle tools for the pbi-model server.

    pbi_list_columns            rich per-column read-out (parsed from TMDL text)
    pbi_update_column           partial update of column properties + description
    pbi_update_table            table description / isHidden
    pbi_set_measure_properties  measure description / isHidden / kpi block
    pbi_remove_kpi              drop a measure's kpi block
    pbi_delete_column           guarded delete (lineage, relationships, sortBy,
                                hierarchy levels, report usage); force removes
                                the dependent relationship/level lines too
    pbi_create_hierarchy / pbi_list_hierarchies / pbi_delete_hierarchy

Plain functions take the ModelState first and are unit-tested directly;
`register()` wraps them as MCP tools. Every write is a surgical splice of the
target block's lines (core/tmdl_members.py) written through
PbipProject._write_text, so unrelated text, line endings and BOMs survive.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from core import tmdl_members as tm
from core.dax_parser import parse_references
from core.tmdl import _split_col_ref, _unquote, quote_ident
from core.usage import _walk_field_refs

if TYPE_CHECKING:
    from core.pbip import PbipProject
    from model_server.server import ModelState

_KPI_KEYS = frozenset({
    "target_measure", "target_dax", "status_dax", "trend_dax",
    "status_graphic", "trend_graphic", "description", "target_format_string",
    "target_description", "status_description", "trend_description",
    "annotations",
})


# --- file access ------------------------------------------------------------

def _iter_tables(project: PbipProject):
    """(path, lines, table block) for every tables/*.tmdl file."""
    tables_dir = project._require_model() / "definition" / "tables"
    if not tables_dir.is_dir():
        return
    for path in sorted(tables_dir.glob("*.tmdl")):
        lines = path.read_text(encoding="utf-8-sig").split("\n")
        try:
            block = tm.parse_table(lines)
        except ValueError:
            continue
        yield path, lines, block


def _read_table(project: PbipProject, table: str) -> tuple[Path, list[str], tm.Block]:
    for path, lines, block in _iter_tables(project):
        if block.name == table:
            return path, lines, block
    names = [b.name for _, _, b in _iter_tables(project)]
    raise KeyError(f"Table {table!r} not found; tables: {names}")


def _write_lines(project: PbipProject, path: Path, old: list[str],
                 new: list[str]) -> bool:
    if new == old:
        return False
    project._write_text(path, "\n".join(new))
    return True


def _column(table: tm.Block, name: str) -> tm.Block:
    col = table.child("column", name)
    if col is None:
        raise KeyError(f"Column {name!r} not found in table {table.name!r}; "
                       f"columns: {[c.name for c in table.children_of('column')]}")
    return col


def _measure(project: PbipProject, table: tm.Block, name: str) -> tm.Block:
    existing = project._find_measure(name)
    if existing is None:
        raise KeyError(f"Measure {name!r} not found")
    if existing.table != table.name:
        raise ValueError(f"Measure {name!r} lives in table {existing.table!r}, "
                         f"not {table.name!r}")
    block = table.child("measure", name)
    if block is None:  # pragma: no cover - parsers disagree
        raise KeyError(f"Measure {name!r} not found in {table.name}.tmdl")
    return block


def _hierarchy(table: tm.Block, name: str) -> tm.Block:
    hier = table.child("hierarchy", name)
    if hier is None:
        raise KeyError(f"Hierarchy {name!r} not found in table {table.name!r}; "
                       f"hierarchies: {[h.name for h in table.children_of('hierarchy')]}")
    return hier


def _text_or_remove(value: str | None) -> str | None:
    """Tool argument -> set_property value: "" removes, else quoted text."""
    if value is None:
        return None
    return "" if value == "" else tm.prop_value(value)


# --- report-side references -------------------------------------------------

def _walk_hierarchy_refs(node) -> set[tuple[str, str]]:
    """(Entity, Hierarchy) pairs from PBIR JSON: {"Expression": {"SourceRef":
    {"Entity": T}} | {"PropertyVariationSource": {...}}, "Hierarchy": H}."""
    refs: set[tuple[str, str]] = set()
    if isinstance(node, dict):
        expr = node.get("Expression")
        hier = node.get("Hierarchy")
        if isinstance(expr, dict) and isinstance(hier, str):
            src = expr.get("SourceRef")
            if not isinstance(src, dict):
                pvs = expr.get("PropertyVariationSource") or {}
                src = (pvs.get("Expression") or {}).get("SourceRef")
            entity = (src or {}).get("Entity") if isinstance(src, dict) else None
            if isinstance(entity, str):
                refs.add((entity, hier))
        for v in node.values():
            refs |= _walk_hierarchy_refs(v)
    elif isinstance(node, list):
        for v in node:
            refs |= _walk_hierarchy_refs(v)
    return refs


def _report_files(project: PbipProject) -> list[tuple[str, dict]]:
    """(relative path, parsed JSON) for report.json and every pages/** JSON;
    [] when the project has no report layer."""
    try:
        report_def = project._require_report() / "definition"
    except FileNotFoundError:
        return []
    files = [report_def / "report.json"]
    pages_dir = report_def / "pages"
    if pages_dir.is_dir():
        files += sorted(pages_dir.rglob("*.json"))
    out = []
    for f in files:
        if f.exists():
            out.append((f.relative_to(report_def).as_posix(),
                        json.loads(f.read_text(encoding="utf-8-sig"))))
    return out


def report_column_usage(project: PbipProject, table: str, column: str) -> list[str]:
    """Report files (visuals, page/report filters) that bind Table[Column]."""
    return [rel for rel, data in _report_files(project)
            if (table, column) in _walk_field_refs(data)]


def report_hierarchy_usage(project: PbipProject, table: str, hierarchy: str) -> list[str]:
    """Report files that bind a level of Table.Hierarchy."""
    return [rel for rel, data in _report_files(project)
            if (table, hierarchy) in _walk_hierarchy_refs(data)]


# --- columns ----------------------------------------------------------------

def list_columns(state: ModelState, table: str | None = None) -> list[dict]:
    """Every column (optionally one table) with its TMDL properties."""
    project = state.require()
    out: list[dict] = []
    seen = False
    for _, _, block in _iter_tables(project):
        if table is not None and block.name != table:
            continue
        seen = True
        out += [tm.column_info(block.name, c) for c in block.children_of("column")]
    if table is not None and not seen:
        raise KeyError(f"Table {table!r} not found")
    return out


def update_column(state: ModelState, table: str, name: str,
                  description: str | None = None,
                  format_string: str | None = None,
                  data_category: str | None = None,
                  sort_by_column: str | None = None,
                  is_hidden: bool | None = None,
                  display_folder: str | None = None,
                  summarize_by: str | None = None,
                  data_type: str | None = None) -> dict:
    """Partial update of a column; None keeps, "" removes a text property."""
    project = state.require()
    path, lines, tbl = _read_table(project, table)
    _column(tbl, name)

    edits: list[tuple[str, str | bool | None]] = []
    if format_string is not None:
        edits.append(("formatString", _text_or_remove(format_string)))
    if data_category is not None:
        edits.append(("dataCategory",
                      "" if data_category == "" else
                      tm.canonical(data_category, tm.DATA_CATEGORIES, "data_category")))
    if sort_by_column is not None:
        if sort_by_column == "":
            edits.append(("sortByColumn", ""))
        else:
            if tbl.child("column", sort_by_column) is None:
                raise ValueError(
                    f"sort_by_column {sort_by_column!r} is not a column of "
                    f"{table!r}; columns: {[c.name for c in tbl.children_of('column')]}")
            if sort_by_column == name:
                raise ValueError("A column cannot be sorted by itself")
            edits.append(("sortByColumn", quote_ident(sort_by_column)))
    if is_hidden is not None:
        edits.append(("isHidden", bool(is_hidden)))
    if display_folder is not None:
        edits.append(("displayFolder", _text_or_remove(display_folder)))
    if summarize_by is not None:
        edits.append(("summarizeBy",
                      tm.canonical(summarize_by, tm.SUMMARIZE_BY, "summarize_by")))
    if data_type is not None:
        edits.append(("dataType", tm.canonical(data_type, tm.DATA_TYPES, "data_type")))
    if not edits and description is None:
        raise ValueError("Nothing to update: pass at least one property")

    new = lines
    for key, value in edits:
        new = tm.set_property(new, _column(tm.parse_table(new), name), key, value)
    if description is not None:
        new = tm.set_description(new, _column(tm.parse_table(new), name),
                                 description or None)
    changed = _write_lines(project, path, lines, new)
    return {"ok": True, "table": table, "column": name, "changed": changed,
            "column_after": tm.column_info(table, _column(tm.parse_table(new), name))}


# --- tables -----------------------------------------------------------------

def update_table(state: ModelState, table: str, description: str | None = None,
                 is_hidden: bool | None = None) -> dict:
    """Table-level `///` description and `isHidden`."""
    project = state.require()
    path, lines, tbl = _read_table(project, table)
    if description is None and is_hidden is None:
        raise ValueError("Nothing to update: pass description and/or is_hidden")
    new = lines
    if is_hidden is not None:
        new = tm.set_property(new, tbl, "isHidden", bool(is_hidden))
    if description is not None:
        new = tm.set_description(new, tm.parse_table(new), description or None)
    changed = _write_lines(project, path, lines, new)
    after = tm.parse_table(new)
    return {"ok": True, "table": table, "changed": changed,
            "is_hidden": after.flag("isHidden"),
            "description": "\n".join(after.description) or None}


# --- measures ---------------------------------------------------------------

def _resolve_target_measure(project: PbipProject, ref: str) -> str:
    """'Table.Measure' or 'Measure' -> the measure's name (validated)."""
    m = project._find_measure(ref)
    if m is None and "." in ref:
        tbl, _, name = ref.partition(".")
        m = project._find_measure(name)
        if m is not None and m.table != tbl:
            raise ValueError(f"Measure {name!r} lives in table {m.table!r}, not {tbl!r}")
    if m is None:
        raise KeyError(f"target_measure {ref!r} not found (use 'Table.Measure' or 'Measure')")
    return m.name


def _kpi_lines(project: PbipProject, indent: int, kpi: dict) -> list[str]:
    if not isinstance(kpi, dict):
        raise ValueError("kpi must be an object like {\"target_measure\": "
                         "\"Table.Measure\", \"status_dax\": \"...\"}")
    unknown = sorted(set(kpi) - _KPI_KEYS)
    if unknown:
        raise ValueError(f"Unknown kpi keys {unknown}; allowed: {sorted(_KPI_KEYS)}")
    target_measure = kpi.get("target_measure")
    target_dax = kpi.get("target_dax")
    if bool(target_measure) == bool(target_dax):
        raise ValueError("kpi needs exactly one of target_measure or target_dax")
    status_dax = kpi.get("status_dax")
    if not status_dax or not str(status_dax).strip():
        raise ValueError("kpi.status_dax is required: DAX returning -1 / 0 / 1 "
                         "(or -2..2) for the status")
    if target_measure:
        target_expression = f"[{_resolve_target_measure(project, target_measure)}]"
    else:
        target_expression = str(target_dax)
    trend_dax = kpi.get("trend_dax")
    trend_graphic = kpi.get("trend_graphic") or (tm.DEFAULT_TREND_GRAPHIC if trend_dax else None)
    annotations = {"GoalType": "Measure" if target_measure else "Absolute",
                   "KpiStatusType": "Linear"}
    user_ann = kpi.get("annotations")
    if user_ann is not None:
        if not isinstance(user_ann, dict):
            raise ValueError("kpi.annotations must be an object {name: value}")
        annotations.update(user_ann)
    annotations = {k: v for k, v in annotations.items() if v is not None}
    return tm.emit_kpi_block(
        indent,
        target_expression=target_expression,
        status_expression=str(status_dax),
        trend_expression=str(trend_dax) if trend_dax else None,
        status_graphic=kpi.get("status_graphic") or tm.DEFAULT_STATUS_GRAPHIC,
        trend_graphic=trend_graphic,
        description=kpi.get("description"),
        target_format_string=kpi.get("target_format_string"),
        target_description=kpi.get("target_description"),
        status_description=kpi.get("status_description"),
        trend_description=kpi.get("trend_description"),
        annotations=annotations,
    )


def set_measure_properties(state: ModelState, table: str, name: str,
                           description: str | None = None,
                           is_hidden: bool | None = None,
                           kpi: dict | None = None) -> dict:
    """Measure `///` description, `isHidden` and `kpi` block (DAX untouched)."""
    project = state.require()
    path, lines, tbl = _read_table(project, table)
    meas = _measure(project, tbl, name)
    if description is None and is_hidden is None and kpi is None:
        raise ValueError("Nothing to update: pass description, is_hidden and/or kpi")

    new = lines
    if kpi is not None:
        kpi_lines = _kpi_lines(project, meas.indent + 1, kpi)
        existing = meas.child("kpi")
        if existing is not None:
            new = new[:existing.start] + kpi_lines + new[existing.end:]
        else:
            at = meas.prop_insert_at
            new = new[:at] + [""] + kpi_lines + new[at:]
    if is_hidden is not None:
        new = tm.set_property(new, tm.parse_table(new).child("measure", name),
                              "isHidden", bool(is_hidden))
    if description is not None:
        new = tm.set_description(new, tm.parse_table(new).child("measure", name),
                                 description or None)
    changed = _write_lines(project, path, lines, new)
    after = tm.parse_table(new).child("measure", name)
    kpi_block = after.child("kpi")
    return {"ok": True, "table": table, "measure": name, "changed": changed,
            "is_hidden": after.flag("isHidden"),
            "description": "\n".join(after.description) or None,
            "kpi": tm.kpi_info(kpi_block) if kpi_block else None}


def remove_kpi(state: ModelState, table: str, name: str) -> dict:
    """Delete the `kpi` block under a measure (no-op if there is none)."""
    project = state.require()
    path, lines, tbl = _read_table(project, table)
    meas = _measure(project, tbl, name)
    kpi = meas.child("kpi")
    if kpi is None:
        return {"ok": True, "table": table, "measure": name, "changed": False,
                "note": "measure has no kpi"}
    new = tm.delete_span(lines, kpi.start, kpi.end)
    changed = _write_lines(project, path, lines, new)
    return {"ok": True, "action": "removed", "table": table, "measure": name,
            "changed": changed}


# --- column deletion --------------------------------------------------------

def column_dependents(project: PbipProject, table: str, name: str) -> dict:
    """Everything that would break if Table[name] disappeared."""
    ref = f"{table}.{name}"
    measure_names = {m.name for m in project.list_measures()}
    deps: dict = {"measures": [], "calculated_columns": [], "relationships": [],
                  "sort_by": [], "hierarchy_levels": [], "report": []}

    graph = project.model_lineage()["measures"]
    deps["measures"] = sorted(m for m, node in graph.items()
                              if ref in node["columns"])

    for _, _, block in _iter_tables(project):
        for col in block.children_of("column"):
            if col.expression is None or (block.name == table and col.name == name):
                continue
            refs = parse_references(col.expression, known_measures=measure_names)
            if ((table, name) in refs.columns
                    or (block.name == table and name in refs.unqualified)):
                deps["calculated_columns"].append(f"{block.name}.{col.name}")
        if block.name == table:
            for col in block.children_of("column"):
                if col.name != name and _unquote(col.value("sortByColumn") or "") == name:
                    deps["sort_by"].append(f"{table}.{col.name}")
            for hier in block.children_of("hierarchy"):
                for lvl in hier.children_of("level"):
                    if _unquote(lvl.value("column") or "") == name:
                        deps["hierarchy_levels"].append(f"{hier.name}/{lvl.name}")

    for r in project.list_relationships():
        if ((r.from_table, r.from_column) == (table, name)
                or (r.to_table, r.to_column) == (table, name)):
            deps["relationships"].append(
                f"{r.name} ({r.from_table}.{r.from_column} -> {r.to_table}.{r.to_column})")

    deps["report"] = report_column_usage(project, table, name)
    return deps


def _remove_relationships(project: PbipProject, table: str, name: str) -> list[str]:
    rel_file = project._require_model() / "definition" / "relationships.tmdl"
    if not rel_file.exists():
        return []
    lines = rel_file.read_text(encoding="utf-8-sig").split("\n")
    removed: list[str] = []
    new = lines
    while True:
        hit = None
        for b in tm.parse_objects(new, 0):
            if b.kind != "relationship":
                continue
            ends = [_split_col_ref(v) for v in (b.value("fromColumn"), b.value("toColumn"))
                    if isinstance(v, str)]
            if (table, name) in ends:
                hit = b
                break
        if hit is None:
            break
        removed.append(hit.name)
        new = tm.delete_span(new, hit.start, hit.end)
    _write_lines(project, rel_file, lines, new)
    return removed


def delete_column(state: ModelState, table: str, name: str,
                  force: bool = False) -> dict:
    """Delete a column; refuses while anything depends on it unless force."""
    project = state.require()
    path, lines, tbl = _read_table(project, table)
    col = _column(tbl, name)
    deps = column_dependents(project, table, name)
    blocking = {k: v for k, v in deps.items() if v}
    if blocking and not force:
        parts = "; ".join(f"{k}: {v}" for k, v in blocking.items())
        raise ValueError(
            f"Refusing to delete column {table}.{name}: it is used by {parts}. "
            f"Fix those first, or pass force=true (dependent relationship blocks, "
            f"hierarchy levels and sortByColumn references are removed with it; "
            f"measures, calculated columns and report visuals are left as they are).")

    removed = {"relationships": [], "hierarchy_levels": [], "hierarchies": [],
               "sort_by_cleared": []}
    new = tm.delete_span(lines, col.start, col.end)

    # sibling columns sorted by the deleted column
    for sibling in tm.parse_table(new).children_of("column"):
        if _unquote(sibling.value("sortByColumn") or "") == name:
            new = tm.set_property(new, _column(tm.parse_table(new), sibling.name),
                                  "sortByColumn", None)
            removed["sort_by_cleared"].append(sibling.name)

    # hierarchy levels on the column (a hierarchy left empty goes too)
    while True:
        hit = None
        for hier in tm.parse_table(new).children_of("hierarchy"):
            for lvl in hier.children_of("level"):
                if _unquote(lvl.value("column") or "") == name:
                    hit = (hier, lvl)
                    break
            if hit:
                break
        if hit is None:
            break
        hier, lvl = hit
        if len(hier.children_of("level")) == 1:
            new = tm.delete_span(new, hier.start, hier.end)
            removed["hierarchies"].append(hier.name)
        else:
            new = tm.delete_span(new, lvl.start, lvl.end)
        removed["hierarchy_levels"].append(f"{hier.name}/{lvl.name}")

    _write_lines(project, path, lines, new)
    removed["relationships"] = _remove_relationships(project, table, name)
    return {"ok": True, "action": "deleted", "table": table, "column": name,
            "forced": bool(blocking), "removed": removed,
            "still_referenced_by": {"measures": deps["measures"],
                                    "calculated_columns": deps["calculated_columns"],
                                    "report": deps["report"]}}


# --- hierarchies ------------------------------------------------------------

def list_hierarchies(state: ModelState, table: str | None = None) -> list[dict]:
    """Every hierarchy (optionally one table) with its ordered levels."""
    project = state.require()
    out: list[dict] = []
    seen = False
    for _, _, block in _iter_tables(project):
        if table is not None and block.name != table:
            continue
        seen = True
        out += [tm.hierarchy_info(block.name, h) for h in block.children_of("hierarchy")]
    if table is not None and not seen:
        raise KeyError(f"Table {table!r} not found")
    return out


def create_hierarchy(state: ModelState, table: str, name: str, levels: list[dict],
                     description: str | None = None, hidden: bool = False) -> dict:
    """Add a hierarchy with ordered levels, each on an existing column."""
    project = state.require()
    path, lines, tbl = _read_table(project, table)
    if not name or not str(name).strip():
        raise ValueError("Hierarchy name must not be empty")
    if tbl.child("hierarchy", name) is not None:
        raise ValueError(f"Hierarchy {name!r} already exists in {table!r}")
    if tbl.child("column", name) is not None:
        raise ValueError(f"{table!r} already has a column named {name!r}")
    if not levels or not isinstance(levels, list):
        raise ValueError("levels must be a non-empty list of {\"name\", \"column\"}")
    columns = {c.name for c in tbl.children_of("column")}
    clean: list[dict] = []
    seen_names: set[str] = set()
    for i, lvl in enumerate(levels):
        if not isinstance(lvl, dict) or not lvl.get("column"):
            raise ValueError(f"levels[{i}] needs a \"column\" (and optional \"name\")")
        col = str(lvl["column"])
        if col not in columns:
            raise ValueError(f"levels[{i}]: column {col!r} not in {table!r}; "
                             f"columns: {sorted(columns)}")
        lname = str(lvl.get("name") or col)
        if lname in seen_names:
            raise ValueError(f"Duplicate level name {lname!r}")
        seen_names.add(lname)
        clean.append({"name": lname, "column": col})

    block_lines = tm.emit_hierarchy_block(name, clean, description, hidden)
    new = tm.insert_member(lines, tbl, block_lines,
                           after_kinds=("column", "measure", "hierarchy"))
    _write_lines(project, path, lines, new)
    return {"ok": True, "action": "created",
            "hierarchy": tm.hierarchy_info(table, _hierarchy(tm.parse_table(new), name))}


def delete_hierarchy(state: ModelState, table: str, name: str,
                     force: bool = False) -> dict:
    """Delete a hierarchy; refuses while report visuals use it unless force."""
    project = state.require()
    path, lines, tbl = _read_table(project, table)
    hier = _hierarchy(tbl, name)
    usage = report_hierarchy_usage(project, table, name)
    if usage and not force:
        raise ValueError(f"Refusing to delete hierarchy {table}.{name}: bound in "
                         f"the report by {usage}. Rebind those first, or pass force=true.")
    new = tm.delete_span(lines, hier.start, hier.end)
    _write_lines(project, path, lines, new)
    return {"ok": True, "action": "deleted", "table": table, "hierarchy": name,
            "forced_past_report_usage": usage if force else []}


# --- MCP registration -------------------------------------------------------

def register(mcp, state, tool) -> None:
    @tool(read=True)
    def pbi_list_columns(table: str | None = None) -> list[dict]:
        """List columns (all tables, or one `table`) with the properties
        Desktop stores in TMDL: name, data_type, is_hidden, is_key,
        summarize_by, format_string, data_category, sort_by_column,
        display_folder, description, is_calculated (+ its dax), source_column.
        Returns a list of column objects."""
        return list_columns(state, table)

    @tool(write=True, idempotent=True)
    def pbi_update_column(table: str, name: str, description: str | None = None,
                          format_string: str | None = None,
                          data_category: str | None = None,
                          sort_by_column: str | None = None,
                          is_hidden: bool | None = None,
                          display_folder: str | None = None,
                          summarize_by: str | None = None,
                          data_type: str | None = None) -> dict:
        """Partially update a column's properties by editing only its block:
        omitted arguments are kept, "" removes a text property. description
        (multi-line ok) becomes `///` doc lines; format_string, display_folder
        are free text; data_category must be one of Address, City, Continent,
        Country, County, Image, ImageUrl, Latitude, Longitude, Organization,
        Place, PostalCode, StateOrProvince, WebUrl, Barcode; sort_by_column
        must be another column of the table; summarize_by one of none, sum,
        count, min, max, average, distinctCount, default; data_type one of
        string, int64, double, dateTime, boolean, decimal, binary, variant.
        Returns {ok, changed, column_after}."""
        return update_column(state, table, name, description, format_string,
                             data_category, sort_by_column, is_hidden,
                             display_folder, summarize_by, data_type)

    @tool(write=True, idempotent=True)
    def pbi_update_table(table: str, description: str | None = None,
                         is_hidden: bool | None = None) -> dict:
        """Set a table's `///` description ("" removes it) and/or isHidden.
        Returns {ok, changed, is_hidden, description}."""
        return update_table(state, table, description, is_hidden)

    @tool(write=True, idempotent=True)
    def pbi_set_measure_properties(table: str, name: str,
                                   description: str | None = None,
                                   is_hidden: bool | None = None,
                                   kpi: dict | None = None) -> dict:
        """Set a measure's `///` description ("" removes), isHidden and/or its
        KPI without touching the DAX (use pbi_update_measure for DAX/format).
        kpi = {"target_measure": "Table.Measure" OR "target_dax": "<DAX>",
        "status_dax": "<DAX returning -1/0/1>", "trend_dax"?: "<DAX>",
        "status_graphic"?: "Traffic Light - Single", "trend_graphic"?:
        "Standard Arrow", "description"?, "target_format_string"?,
        "status_description"?, "trend_description"?, "annotations"?: {name:
        value}} writes (or replaces) the TMDL `kpi` block with targetExpression,
        statusExpression, statusGraphic, trendExpression/trendGraphic and the
        GoalType / KpiStatusType annotations. Returns {ok, changed, is_hidden,
        description, kpi}."""
        return set_measure_properties(state, table, name, description, is_hidden, kpi)

    @tool(write=True, destructive=True, idempotent=True)
    def pbi_remove_kpi(table: str, name: str) -> dict:
        """Remove the KPI block from a measure (its DAX and other properties
        stay). No-op when the measure has none. Returns {ok, changed}."""
        return remove_kpi(state, table, name)

    @tool(write=True, destructive=True)
    def pbi_delete_column(table: str, name: str, force: bool = False) -> dict:
        """Delete a column. Refuses (listing the dependents) while any measure
        or calculated column references it, a relationship joins on it,
        another column sorts by it, a hierarchy level uses it, or a report
        visual/filter binds it. force=true deletes anyway and also removes the
        dependent relationship blocks, hierarchy levels (empty hierarchies too)
        and sortByColumn lines; measures, calculated columns and visuals that
        reference it are left for you to fix. Returns {ok, removed,
        still_referenced_by}."""
        return delete_column(state, table, name, force)

    @tool(write=True)
    def pbi_create_hierarchy(table: str, name: str, levels: list[dict],
                             description: str | None = None,
                             hidden: bool = False) -> dict:
        """Create a hierarchy in `table`. levels = [{"name": "Year", "column":
        "Year"}, ...] in drill order (name defaults to the column); every
        column must exist in the table. Optional `///` description and hidden
        flag. Returns {ok, hierarchy}."""
        return create_hierarchy(state, table, name, levels, description, hidden)

    @tool(read=True)
    def pbi_list_hierarchies(table: str | None = None) -> list[dict]:
        """List hierarchies (all tables, or one `table`): name, description,
        is_hidden, display_folder and ordered levels [{name, column}]."""
        return list_hierarchies(state, table)

    @tool(write=True, destructive=True)
    def pbi_delete_hierarchy(table: str, name: str, force: bool = False) -> dict:
        """Delete a hierarchy. Refuses while a report visual or filter binds one
        of its levels unless force=true. Returns {ok, forced_past_report_usage}."""
        return delete_hierarchy(state, table, name, force)
