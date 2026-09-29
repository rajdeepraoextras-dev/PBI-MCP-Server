"""pbi-model tools for tables, partitions, shared expressions and
incremental refresh.

Loaded by ``core.tooling.load_tool_modules`` (any ``model_server/tools_*.py``
with a ``register(mcp, state, tool)``). The logic lives in plain functions
that take the server ``state`` first so tests call them directly; the
``pbi_*`` wrappers in ``register`` are thin. Write tools get ``dry_run`` and
undo journaling from the ``@tool(write=True)`` decorator.

All file mutations go through ``PbipProject._write_text`` (style-preserving,
atomic, backed up) and the text-level editors in ``core.tmdl_tables`` — the
parsed model is never re-emitted.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from core import io_safe
from core import tmdl_tables as tt
from core.dax_parser import parse_references
from core.tmdl import add_table_ref_text, parse_table_file

if TYPE_CHECKING:
    from model_server.server import ModelState

RANGE_START_DEFAULT = "#datetime(2020, 1, 1, 0, 0, 0)"
RANGE_END_DEFAULT = "#datetime(2030, 1, 1, 0, 0, 0)"


# --- helpers ----------------------------------------------------------------

def _definition(project) -> Path:
    return project._require_model() / "definition"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig") if path.exists() else ""


def _rel(project, path: Path) -> str:
    return path.relative_to(project._require_model()).as_posix()


def _find_table_ci(project, name: str):
    key = name.casefold()
    for t in project.list_tables():
        if t.name.casefold() == key:
            return t
    return None


def _register_table(project, name: str) -> bool:
    """Add `ref table <name>` to model.tmdl; True when the line was added."""
    model_file = _definition(project) / "model.tmdl"
    if not model_file.exists():
        return False
    text = _read(model_file)
    new_text = add_table_ref_text(text, name)
    if new_text == text:
        return False
    project._write_text(model_file, new_text)
    return True


def _new_table(project, name: str, text: str) -> dict:
    """Write a new table file (refusing duplicates) and register it."""
    name = str(name).strip()
    if not name:
        raise ValueError("Table name must not be empty")
    existing = _find_table_ci(project, name)
    if existing is not None:
        raise ValueError(
            f"Table {existing.name!r} already exists. Use pbi_update_partition "
            f"to change its query, pbi_create_column to add columns, or "
            f"pbi_delete_table first.")
    path = _definition(project) / "tables" / tt.table_file_name(name)
    if path.exists():
        raise ValueError(
            f"tables/{path.name} already exists on disk (declares a different "
            f"table name); pick another table name")
    project._write_text(path, text)
    ref_added = _register_table(project, name)
    table = parse_table_file(path)          # must parse back, or it's a bug
    partitions = tt.parse_partitions_text(text)
    return {
        "ok": True, "action": "created", "table": table.name,
        "file": _rel(project, path), "ref_table_added": ref_added,
        "columns": [c.name for c in table.columns],
        "partition": {k: partitions[0][k] for k in ("name", "kind", "mode")},
    }


# --- tables -----------------------------------------------------------------

def create_table(state: ModelState, name: str, m_source: str,
                 columns: list[dict] | None = None,
                 description: str | None = None, hidden: bool = False) -> dict:
    """Create an import table fed by an M query (`partition <name> = m`)."""
    project = state.require()
    if not str(m_source or "").strip():
        raise ValueError("m_source must be the table's Power Query (M) expression, "
                         "e.g. let Source = ... in Source")
    text = tt.emit_table_file(name, m_source, columns or [], description, hidden)
    return _new_table(project, name, text)


def create_calculated_table(state: ModelState, name: str, dax: str,
                            description: str | None = None) -> dict:
    """Create a DAX calculated table (`partition <name> = calculated`)."""
    project = state.require()
    if not str(dax or "").strip():
        raise ValueError("dax must be the table expression, e.g. "
                         "CALENDAR(DATE(2020,1,1), DATE(2025,12,31))")
    text = tt.emit_calculated_table_file(name, dax, description)
    result = _new_table(project, name, text)
    result["note"] = ("No columns are declared; Power BI Desktop infers them "
                      "from the DAX when it next loads the model.")
    warnings = project._lint_dax(dax)
    if warnings:
        result["warnings"] = warnings
    return result


def update_partition(state: ModelState, table: str, m_source: str,
                     partition: str | None = None) -> dict:
    """Replace one partition's `source =` expression, keeping the rest."""
    project = state.require()
    if not str(m_source or "").strip():
        raise ValueError("m_source must not be empty")
    path = project._table_file(table)
    text = _read(path)
    new_text, info = tt.replace_partition_source_text(text, m_source, partition)
    action = "updated"
    if new_text == text:
        action = "unchanged"
    else:
        project._write_text(path, new_text)
    return {"ok": True, "action": action, "table": table,
            "partition": info["name"], "kind": info["kind"], "mode": info["mode"]}


def list_partitions(state: ModelState, table: str | None = None) -> list[dict]:
    """[{table, partitions: [{name, kind, mode, source, ...}]}] per table."""
    project = state.require()
    tables_dir = _definition(project) / "tables"
    out: list[dict] = []
    if tables_dir.is_dir():
        for f in sorted(tables_dir.glob("*.tmdl")):
            t = project._parse_table_cached(f)
            if table is not None and t.name != table:
                continue
            out.append({"table": t.name,
                        "partitions": tt.parse_partitions_text(_read(f))})
    if table is not None and not out:
        raise KeyError(f"Table {table!r} not found")
    return out


# --- shared expressions -----------------------------------------------------

def _expressions_file(project) -> Path:
    return _definition(project) / "expressions.tmdl"


def list_expressions(state: ModelState) -> list[dict]:
    project = state.require()
    return tt.parse_expressions_text(_read(_expressions_file(project)))


def create_expression(state: ModelState, name: str, m: str, kind: str = "query",
                      description: str | None = None,
                      parameter_meta: dict | None = None) -> dict:
    """Append a shared query or parameter to expressions.tmdl."""
    project = state.require()
    name = str(name).strip()
    if not name:
        raise ValueError("Expression name must not be empty")
    path = _expressions_file(project)
    text = _read(path)
    for e in tt.parse_expressions_text(text):
        if e["name"].casefold() == name.casefold():
            raise ValueError(f"Expression {e['name']!r} already exists; use "
                             f"pbi_update_expression to change its M")
    clash = _find_table_ci(project, name)
    if clash is not None:
        raise ValueError(f"A table named {clash.name!r} exists; Power Query names "
                         f"are unique across tables and shared expressions")
    block = tt.emit_expression_block(name, m, kind, description, parameter_meta)
    new_text = tt.append_expression_text(text, block)
    project._write_text(path, new_text)
    entry = [e for e in tt.parse_expressions_text(new_text) if e["name"] == name][0]
    return {"ok": True, "action": "created", "name": name, "kind": entry["kind"],
            "meta": entry["meta"], "result_type": entry["result_type"],
            "file": _rel(project, path)}


def update_expression(state: ModelState, name: str, m: str) -> dict:
    """Replace an expression's M; meta record, lineageTag, annotations kept."""
    project = state.require()
    if not str(m or "").strip():
        raise ValueError("m must not be empty")
    path = _expressions_file(project)
    text = _read(path)
    if not text.strip():
        raise KeyError("The model has no expressions.tmdl (no shared expressions)")
    new_text, info = tt.replace_expression_body_text(text, name, m)
    action = "updated"
    if new_text == text:
        action = "unchanged"
    else:
        project._write_text(path, new_text)
    return {"ok": True, "action": action, "name": info["name"],
            "kind": info["kind"], "meta": info["meta"]}


def _ensure_range_parameters(project) -> list[str]:
    """Add RangeStart / RangeEnd DateTime parameters when missing."""
    path = _expressions_file(project)
    text = _read(path)
    present = {e["name"] for e in tt.parse_expressions_text(text)}
    added: list[str] = []
    for pname, value in (("RangeStart", RANGE_START_DEFAULT),
                         ("RangeEnd", RANGE_END_DEFAULT)):
        if pname in present:
            continue
        text = tt.append_expression_text(text, tt.emit_expression_block(
            pname, value, "parameter", parameter_meta={"Type": "DateTime"}))
        added.append(pname)
    if added:
        project._write_text(path, text)
    return added


# --- incremental refresh ----------------------------------------------------

def set_refresh_policy(state: ModelState, table: str,
                       rolling_window_granularity: str,
                       rolling_window_periods: int,
                       incremental_granularity: str,
                       incremental_periods: int,
                       source_expression: str | None = None,
                       mode: str = "import",
                       incremental_periods_offset: int = 0) -> dict:
    """Write or replace the table's refreshPolicy block (+ RangeStart/RangeEnd)."""
    project = state.require()
    path = project._table_file(table)
    text = _read(path)
    m_parts = [p for p in tt.parse_partitions_text(text)
               if (p["kind"] or "").lower() == "m"]
    if not m_parts:
        raise ValueError(
            f"Table {table!r} has no M (Power Query) partition; incremental "
            f"refresh only applies to import tables fed by an M query")
    warnings: list[str] = []
    if source_expression is None:
        source_expression = m_parts[0]["source"] or ""
        if "RangeStart" not in source_expression or "RangeEnd" not in source_expression:
            raise ValueError(
                f"Partition {m_parts[0]['name']!r} of {table!r} does not filter on "
                f"RangeStart/RangeEnd. Either pbi_update_partition it to add e.g. "
                f"Table.SelectRows(Source, each [OrderDate] >= RangeStart and "
                f"[OrderDate] < RangeEnd), or pass source_expression explicitly.")
    else:
        for p in m_parts:
            src = p["source"] or ""
            if "RangeStart" not in src or "RangeEnd" not in src:
                warnings.append(
                    f"Partition {p['name']!r} does not reference RangeStart/"
                    f"RangeEnd; Desktop expects the table's own query to filter "
                    f"on them too (see pbi_update_partition).")
    policy = {
        "rolling_window_granularity": rolling_window_granularity,
        "rolling_window_periods": rolling_window_periods,
        "incremental_granularity": incremental_granularity,
        "incremental_periods": incremental_periods,
        "incremental_periods_offset": incremental_periods_offset,
        "mode": mode,
        "source_expression": source_expression,
    }
    new_text = tt.set_refresh_policy_text(text, policy)   # validates first
    action = "unchanged"
    if new_text != text:
        project._write_text(path, new_text)
        action = "created" if tt.parse_refresh_policy_text(text) is None else "updated"
    added = _ensure_range_parameters(project)
    result = {"ok": True, "action": action, "table": table,
              "policy": tt.parse_refresh_policy_text(new_text),
              "parameters_added": added}
    if added:
        result["note"] = (f"Added {added} as DateTime parameters with placeholder "
                          f"values; adjust them with pbi_update_expression.")
    if warnings:
        result["warnings"] = warnings
    return result


def remove_refresh_policy(state: ModelState, table: str) -> dict:
    project = state.require()
    path = project._table_file(table)
    text = _read(path)
    try:
        new_text = tt.remove_refresh_policy_text(text)
    except KeyError:
        raise ValueError(f"Table {table!r} has no refreshPolicy to remove") from None
    project._write_text(path, new_text)
    return {"ok": True, "action": "removed", "table": table,
            "note": "RangeStart/RangeEnd parameters were left in place; other "
                    "tables may use them."}


# --- delete -----------------------------------------------------------------

def _references_table(dax: str, table: str, own_measures: set[str]) -> list[str]:
    """How `dax` depends on `table`: [] when it doesn't."""
    refs = parse_references(dax)
    via: list[str] = []
    key = table.casefold()
    if key in {t.casefold() for t in refs.tables} | {t.casefold() for t, _ in refs.columns}:
        via.append("table")
    used = sorted(own_measures & refs.measures)
    if used:
        via.append("measures: " + ", ".join(used))
    return via


def table_dependents(project, table: str) -> dict:
    """Everything outside `table` that would break if it were deleted."""
    from core.usage import collect_direct_refs

    tables = project.list_tables()
    target = next((t for t in tables if t.name == table), None)
    if target is None:
        raise KeyError(f"Table {table!r} not found")
    own_measures = {m.name for m in target.measures}
    deps: dict = {"measures": [], "calculated_columns": [],
                  "calculated_tables": [], "relationships": [], "report_fields": []}

    for m in project.list_measures():
        if m.table == table:
            continue
        via = _references_table(m.dax, table, own_measures)
        if via:
            deps["measures"].append({"table": m.table, "name": m.name, "via": via})

    tables_dir = _definition(project) / "tables"
    for f in sorted(tables_dir.glob("*.tmdl")):
        t = project._parse_table_cached(f)
        if t.name == table:
            continue
        text = _read(f)
        for col, dax in tt.calculated_column_expressions_text(text):
            via = _references_table(dax, table, own_measures)
            if via:
                deps["calculated_columns"].append(
                    {"table": t.name, "name": col, "via": via})
        for p in tt.parse_partitions_text(text):
            if (p["kind"] or "").lower() == "calculated" and p["source"]:
                via = _references_table(p["source"], table, own_measures)
                if via:
                    deps["calculated_tables"].append(
                        {"table": t.name, "partition": p["name"], "via": via})

    for r in project.list_relationships():
        if table in (r.from_table, r.to_table):
            deps["relationships"].append({
                "name": r.name, "from": f"{r.from_table}.{r.from_column}",
                "to": f"{r.to_table}.{r.to_column}"})

    try:
        project._require_report()
        refs = collect_direct_refs(project)
    except FileNotFoundError:
        refs = set()
    deps["report_fields"] = sorted(f"{e}.{p}" for e, p in refs if e == table)
    return deps


def delete_table(state: ModelState, table: str, force: bool = False) -> dict:
    """Delete a table file; guarded by DAX lineage, relationships and report
    usage unless `force`."""
    project = state.require()
    path = project._table_file(table)               # KeyError when unknown
    deps = table_dependents(project, table)
    blocking = {k: v for k, v in deps.items() if v}
    if blocking and not force:
        parts = []
        for kind, items in blocking.items():
            labels = [i if isinstance(i, str) else
                      ".".join(str(i[k]) for k in ("table", "name", "partition") if k in i)
                      for i in items]
            parts.append(f"{kind}: {labels}")
        raise ValueError(
            f"Refusing to delete table {table!r}: still referenced by "
            f"{'; '.join(parts)}. Fix or rebind those first, or pass force=true "
            f"(relationships touching the table are dropped; other references "
            f"are left dangling).")

    removed: dict = {"file": _rel(project, path), "ref_table": False,
                     "relationships": [], "relationships_file_deleted": False}
    if project.backups:
        io_safe.backup(path)
    path.unlink()
    project._table_cache.pop(str(path), None)

    model_file = _definition(project) / "model.tmdl"
    if model_file.exists():
        text = _read(model_file)
        new_text = tt.remove_table_ref_text(text, table)
        if new_text != text:
            project._write_text(model_file, new_text)
            removed["ref_table"] = True

    rel_file = _definition(project) / "relationships.tmdl"
    if rel_file.exists():
        text = _read(rel_file)
        new_text, dropped = tt.remove_relationships_for_table_text(text, table)
        if dropped:
            removed["relationships"] = dropped
            if new_text.strip():
                project._write_text(rel_file, new_text)
            else:                       # Desktop omits the file when empty
                if project.backups:
                    io_safe.backup(rel_file)
                rel_file.unlink()
                removed["relationships_file_deleted"] = True

    dangling = {k: v for k, v in deps.items() if v and k != "relationships"}
    return {"ok": True, "action": "deleted", "table": table, "forced": bool(blocking),
            "removed": removed, "dangling_references": dangling}


# --- MCP registration -------------------------------------------------------

def register(mcp, state: ModelState, tool) -> None:

    @tool(write=True)
    def pbi_create_table(name: str, m_source: str,
                         columns: list[dict] | None = None,
                         description: str | None = None,
                         hidden: bool = False) -> dict:
        """Create an import table fed by a Power Query (M) expression: writes
        tables/<name>.tmdl (lineageTag, columns, one `partition <name> = m`
        with mode import and the M source, Desktop's PBI_ResultType=Table
        annotation) and registers `ref table <name>` in model.tmdl. Refuses
        if the table exists. columns: [{"name", "data_type" (string | int64 |
        double | decimal | dateTime | boolean), "source_column"?,
        "summarize_by"? (none | sum | ...), "format_string"?, "is_hidden"?,
        "data_category"?}]; description becomes `///` doc lines; hidden sets
        isHidden. Returns {ok, table, file, columns, partition}."""
        return create_table(state, name, m_source, columns, description, hidden)

    @tool(write=True)
    def pbi_create_calculated_table(name: str, dax: str,
                                    description: str | None = None) -> dict:
        """Create a DAX calculated table: tables/<name>.tmdl with `partition
        <name> = calculated`, mode import and the DAX as source, registered in
        model.tmdl. No columns are written — Power BI Desktop infers them from
        the expression when it loads the model. Refuses if the table exists.
        Returns {ok, table, file, partition, warnings?}."""
        return create_calculated_table(state, name, dax, description)

    @tool(write=True, idempotent=True)
    def pbi_update_partition(table: str, m_source: str,
                             partition: str | None = None) -> dict:
        """Replace the `source =` expression (M, or DAX for a calculated
        partition) of one partition of `table`, leaving every other byte of
        the file intact. partition defaults to the table's only partition and
        is required when the table has several. Returns {ok, action, table,
        partition, kind, mode}."""
        return update_partition(state, table, m_source, partition)

    @tool(read=True)
    def pbi_list_partitions(table: str | None = None) -> list[dict]:
        """Partitions per table (all tables, or just `table`): [{table,
        partitions: [{name, kind (m | calculated | entity | calculationGroup),
        mode (import | directQuery | ...), source (M or DAX text; null for
        entity partitions, whose entityName sits in properties),
        source_form, properties}]}]."""
        return list_partitions(state, table)

    @tool(read=True)
    def pbi_list_expressions() -> list[dict]:
        """Shared Power Query expressions from expressions.tmdl: [{name, kind
        ("parameter" when meta says IsParameterQuery=true, else "query"), m,
        meta (parsed record, e.g. {IsParameterQuery, Type,
        IsParameterQueryRequired}), lineage_tag, query_group, description,
        result_type, annotations}]."""
        return list_expressions(state)

    @tool(write=True)
    def pbi_create_expression(name: str, m: str, kind: str = "query",
                              description: str | None = None,
                              parameter_meta: dict | None = None) -> dict:
        """Add a shared expression to expressions.tmdl. kind="query": a named
        M query (multi-line `let ... in` allowed) with lineageTag and
        Desktop's PBI_NavigationStepName / PBI_ResultType annotations.
        kind="parameter": a single-line M literal ("text", 42, true,
        #datetime(2024, 1, 1, 0, 0, 0)) emitted as `expression Name = value
        meta [IsParameterQuery=true, Type=<inferred>,
        IsParameterQueryRequired=true]`; parameter_meta merges extra/override
        keys into that record (e.g. {"Type": "Number", "List": [1, 2]}).
        Refuses duplicate names. Returns {ok, name, kind, meta, result_type}."""
        return create_expression(state, name, m, kind, description, parameter_meta)

    @tool(write=True, idempotent=True)
    def pbi_update_expression(name: str, m: str) -> dict:
        """Replace the M of an existing shared expression or parameter value;
        its meta record, lineageTag, description and annotations are kept.
        Returns {ok, action, name, kind, meta}."""
        return update_expression(state, name, m)

    @tool(write=True, idempotent=True)
    def pbi_set_refresh_policy(table: str, rolling_window_granularity: str,
                               rolling_window_periods: int,
                               incremental_granularity: str,
                               incremental_periods: int,
                               source_expression: str | None = None,
                               mode: str = "import",
                               incremental_periods_offset: int = 0) -> dict:
        """Configure incremental refresh on an import table: writes or
        replaces its `refreshPolicy` block (policyType basic,
        rollingWindowGranularity/Periods = data kept, incrementalGranularity/
        Periods = window refreshed each time, optional
        incrementalPeriodsOffset, mode hybrid for a DirectQuery tail,
        sourceExpression = the M used for new partitions). Granularities:
        day | month | quarter | year. source_expression defaults to the
        table's M partition source and must filter on RangeStart/RangeEnd
        (e.g. Table.SelectRows(Source, each [OrderDate] >= RangeStart and
        [OrderDate] < RangeEnd)). Adds RangeStart/RangeEnd DateTime
        parameters to expressions.tmdl when missing (Desktop requires them).
        Returns {ok, action, table, policy, parameters_added, warnings?}."""
        return set_refresh_policy(
            state, table, rolling_window_granularity, rolling_window_periods,
            incremental_granularity, incremental_periods, source_expression,
            mode, incremental_periods_offset)

    @tool(write=True, destructive=True)
    def pbi_remove_refresh_policy(table: str) -> dict:
        """Remove the `refreshPolicy` block from `table` (turns incremental
        refresh off; the partition query is untouched and RangeStart/RangeEnd
        stay). Errors if the table has no policy. Returns {ok, table}."""
        return remove_refresh_policy(state, table)

    @tool(write=True, destructive=True)
    def pbi_delete_table(table: str, force: bool = False) -> dict:
        """Delete a table: removes tables/<name>.tmdl (with a backup), its
        `ref table` line in model.tmdl and every relationship touching it.
        Refuses — listing the dependents — when other tables' measures,
        calculated columns or calculated tables reference it (DAX lineage),
        when relationships touch it, or when report visuals/filters use its
        fields; force=true deletes anyway and reports what was removed plus
        the references left dangling. Returns {ok, table, removed: {file,
        ref_table, relationships}, dangling_references}."""
        return delete_table(state, table, force)
