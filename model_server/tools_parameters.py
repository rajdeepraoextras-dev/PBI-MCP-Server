"""Model-server tools: field parameters and what-if parameters.

Both are calculated tables written as ``tables/<Name>.tmdl`` and registered
with a ``ref table`` line in model.tmdl. The TMDL shapes (copied from files
Desktop wrote) live in ``core/tmdl_parameters.py``; this module resolves and
validates the user's input against the model and does the file writes.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from core import tmdl_parameters as tp
from core.tmdl import quote_ident
from core.tmdl_security import add_ref_text, safe_file_stem
from model_server.tools_security import (
    _clean_name, _definition, _read, _rel, _resolve_table, _suggest, _tables,
)

if TYPE_CHECKING:  # pragma: no cover - typing only (never import the server here)
    from core.schemas import Table
    from model_server.server import ModelState


# --- shared helpers ---------------------------------------------------------

def _ensure_new_table(project, name: str) -> Path:
    for existing in _tables(project):
        if existing.casefold() == name.casefold():
            raise ValueError(f"Table {existing!r} already exists (names are case-insensitive)")
    path = _definition(project) / "tables" / f"{safe_file_stem(name)}.tmdl"
    if path.exists():
        raise ValueError(f"File {path.name} already exists; choose a different name")
    return path


def _register_table(project, name: str) -> None:
    model = _definition(project) / "model.tmdl"
    if not model.exists():
        return
    old = _read(model)
    new = add_ref_text(old, "table", name)
    if new != old:
        project._write_text(model, new)


# --- field parameters -------------------------------------------------------

def resolve_field(tables: dict[str, Table], ref: str) -> tuple[Table, str, str]:
    """``"Table.Column"``, ``"Table.[Measure]"``, ``"'My Table'.Column"`` or the
    DAX spelling ``Table[Name]`` -> (table, object name, "column"|"measure")."""
    if not isinstance(ref, str) or not ref.strip():
        raise ValueError("Each field must be a non-empty string like 'Table.Column' "
                         "or 'Table.[Measure]'")
    s = ref.strip()
    explicit_measure = False
    dax = re.fullmatch(r"(?:'((?:[^']|'')+)'|([^\[\].']+))\[(.+)\]", s)
    if dax:
        tname = (dax.group(1) or dax.group(2)).replace("''", "'")
        obj = dax.group(3).replace("]]", "]")
    else:
        tname = rest = None
        for t in sorted(tables, key=len, reverse=True):
            for prefix in (t, quote_ident(t), "'" + t.replace("'", "''") + "'"):
                if s.casefold().startswith((prefix + ".").casefold()):
                    tname, rest = t, s[len(prefix) + 1:]
                    break
            if tname:
                break
        if tname is None:
            raise ValueError(
                f"Field {ref!r}: no table in the model matches the start of it. Use "
                f"'Table.Column' or 'Table.[Measure]'.{_suggest(s.split('.')[0], list(tables))}")
        if rest.startswith("[") and rest.endswith("]") and len(rest) > 2:
            explicit_measure, obj = True, rest[1:-1].replace("]]", "]")
        else:
            obj = rest
    table = _resolve_table(tables, tname)
    col = next((c.name for c in table.columns if c.name.casefold() == obj.casefold()), None)
    meas = next((m.name for m in table.measures if m.name.casefold() == obj.casefold()), None)
    if explicit_measure:
        if meas is None:
            hint = f" ({obj!r} is a column; write '{table.name}.{obj}')" if col else ""
            raise ValueError(f"Field {ref!r}: measure {obj!r} not found in table "
                             f"{table.name!r}{hint}.{_suggest(obj, [m.name for m in table.measures])}")
        return table, meas, "measure"
    if col is not None:
        return table, col, "column"
    if meas is not None:
        return table, meas, "measure"
    raise ValueError(f"Field {ref!r}: {obj!r} is neither a column nor a measure of table "
                     f"{table.name!r}."
                     f"{_suggest(obj, [c.name for c in table.columns] + [m.name for m in table.measures])}")


def create_field_parameter(state: ModelState, name: str, fields: list,
                           default_index: int = 0,
                           description: str | None = None) -> dict:
    """Create a field-parameter calculated table and register it."""
    project = state.require()
    name = _clean_name(name, "Field parameter")
    path = _ensure_new_table(project, name)
    if not isinstance(fields, list) or not fields:
        raise ValueError('fields must be a non-empty list such as '
                         '["Sales.Amount", "Sales.[Net Revenue]"]')
    tables = _tables(project)
    resolved: list[dict] = []
    seen_targets: set[tuple[str, str]] = set()
    seen_labels: set[str] = set()
    for item in fields:
        if isinstance(item, dict):
            unknown = set(item) - {"field", "label"}
            if unknown or "field" not in item:
                raise ValueError('A field object must look like {"field": "Table.Column", '
                                 '"label": "Shown in the slicer"}')
            ref, label = item["field"], item.get("label")
        else:
            ref, label = item, None
        table, obj, kind = resolve_field(tables, ref)
        label = (label if label is not None else obj)
        if not isinstance(label, str) or not label.strip() or "\n" in label or "\r" in label:
            raise ValueError(f"Label for {ref!r} must be a non-empty single-line string")
        label = label.strip()
        target = (table.name.casefold(), obj.casefold())
        if target in seen_targets:
            raise ValueError(f"Field {table.name}.{obj} is listed more than once")
        if label.casefold() in seen_labels:
            raise ValueError(f"Label {label!r} is used twice; give one of them a "
                             'different label with {"field": ..., "label": ...}')
        seen_targets.add(target)
        seen_labels.add(label.casefold())
        resolved.append({"label": label, "table": table.name, "name": obj, "kind": kind})
    if isinstance(default_index, bool) or not isinstance(default_index, int) \
            or not 0 <= default_index < len(resolved):
        raise ValueError(f"default_index must be an integer from 0 to {len(resolved) - 1}")
    if default_index:
        # Power BI stores no "default" for a field parameter; the first item
        # (order 0) is what a single-select slicer starts on.
        resolved.insert(0, resolved.pop(default_index))
    project._write_text(path, tp.emit_field_parameter_table(name, resolved, description))
    _register_table(project, name)
    return {"ok": True, "action": "created", "table": name, "file": _rel(project, path),
            "label_column": name, "fields_column": f"{name} Fields",
            "order_column": f"{name} Order",
            "fields": [{"label": f["label"], "field": f"{f['table']}.{f['name']}",
                        "kind": f["kind"], "order": i} for i, f in enumerate(resolved)]}


def list_field_parameters(state: ModelState) -> list[dict]:
    """Field-parameter tables (a column carrying ParameterMetadata kind 2)."""
    project = state.require()
    model = _tables(project)
    out = []
    folder = _definition(project) / "tables"
    for f in sorted(folder.glob("*.tmdl")) if folder.is_dir() else []:
        text = _read(f)
        if "ParameterMetadata" not in text:
            continue
        info = tp.parse_field_parameter(text)
        if info is None:
            continue
        for fld in info["fields"]:
            tbl = model.get(fld["table"])
            kind = None
            if tbl is not None:
                if any(c.name == fld["name"] for c in tbl.columns):
                    kind = "column"
                elif any(m.name == fld["name"] for m in tbl.measures):
                    kind = "measure"
            fld["kind"] = kind
            fld["exists"] = kind is not None
        info["file"] = _rel(project, f)
        out.append(info)
    return out


# --- what-if parameters -----------------------------------------------------

def create_whatif_parameter(state: ModelState, name: str, minimum: float,
                            maximum: float, increment: float,
                            default: float | None = None,
                            format_string: str | None = None,
                            description: str | None = None) -> dict:
    """Create a what-if parameter table (+ its ``<name> Value`` measure)."""
    project = state.require()
    name = _clean_name(name, "What-if parameter")
    path = _ensure_new_table(project, name)
    for label, v in (("minimum", minimum), ("maximum", maximum), ("increment", increment)):
        try:
            tp.format_number(v)   # type / finiteness check
        except ValueError as e:
            raise ValueError(f"{label}: {e}") from None
    if increment <= 0:
        raise ValueError("increment must be greater than 0")
    if minimum >= maximum:
        raise ValueError("minimum must be less than maximum")
    rows = tp.series_length(minimum, maximum, increment)
    if rows > tp.MAX_WHATIF_ROWS:
        raise ValueError(f"That range would generate {rows:,} rows (limit "
                         f"{tp.MAX_WHATIF_ROWS:,}); use a larger increment")
    if default is None:
        default = minimum
    tp.format_number(default)
    if not minimum <= default <= maximum:
        raise ValueError(f"default {default} is outside the range {minimum}..{maximum}")
    fmt = (format_string.strip() if format_string is not None else "") or \
        tp.default_format_string(minimum, maximum, increment, default)
    measure = f"{name} Value"
    for t in project.list_tables():
        for m in t.measures:
            if m.name.casefold() == measure.casefold():
                raise ValueError(f"Measure {m.name!r} already exists in table {t.name!r} "
                                 "(measure names are unique model-wide); pick another name")
    project._write_text(path, tp.emit_whatif_table(
        name, minimum, maximum, increment, default, fmt, description, measure))
    _register_table(project, name)
    return {"ok": True, "action": "created", "table": name, "column": name,
            "measure": measure, "file": _rel(project, path), "rows": rows,
            "minimum": minimum, "maximum": maximum, "increment": increment,
            "default": default, "format_string": fmt}


def list_whatif_parameters(state: ModelState) -> list[dict]:
    """What-if parameter tables (ParameterMetadata version 0, no kind)."""
    project = state.require()
    model = _tables(project)
    out = []
    folder = _definition(project) / "tables"
    for f in sorted(folder.glob("*.tmdl")) if folder.is_dir() else []:
        text = _read(f)
        if "ParameterMetadata" not in text:
            continue
        name = tp.table_name(text)
        tbl = model.get(name)
        info = tp.parse_whatif_parameter(text, tbl.measures if tbl else [])
        if info is None:
            continue
        info["file"] = _rel(project, f)
        out.append(info)
    return out


# --- MCP registration -------------------------------------------------------

def register(mcp, state, tool) -> None:
    """Define the thin ``pbi_*`` wrappers (called by ``load_tool_modules``)."""

    @tool(write=True)
    def pbi_create_field_parameter(name: str, fields: list[str | dict],
                                   default_index: int = 0,
                                   description: str | None = None) -> dict:
        """Create a field parameter (a slicer that switches which column or
        measure a visual shows) as a calculated table `{("Label",
        NAMEOF('T'[C]), 0), ...}` with the Desktop column layout: visible
        `<name>`, hidden `<name> Fields` (ParameterMetadata kind 2) and hidden
        `<name> Order`, registered in model.tmdl. `fields` are "Table.Column"
        or "Table.[Measure]" strings (or {"field": ..., "label": ...} to set
        the slicer label; default label is the field name); all must exist.
        `default_index` moves that field to the first position, which is what a
        single-select slicer starts on (Power BI stores no other default).
        Returns the table, file and fields in order."""
        return create_field_parameter(state, name, fields, default_index, description)

    @tool(read=True, idempotent=True)
    def pbi_list_field_parameters() -> list[dict]:
        """List field-parameter tables: label/fields/order column names, and the
        fields in order with label, table, name, kind (column|measure) and
        whether the referenced object still exists."""
        return list_field_parameters(state)

    @tool(write=True)
    def pbi_create_whatif_parameter(name: str, minimum: float, maximum: float,
                                    increment: float, default: float | None = None,
                                    format_string: str | None = None,
                                    description: str | None = None) -> dict:
        """Create a what-if parameter: a calculated table
        `GENERATESERIES(minimum, maximum, increment)` with column `<name>`
        (ParameterMetadata version 0) and the measure `<name> Value` =
        SELECTEDVALUE('<name>'[<name>], default), registered in model.tmdl.
        `default` defaults to `minimum` and must lie in the range;
        `format_string` defaults to 0 / 0.00 based on the decimals used.
        Rejects an existing table or measure name and ranges above 1,000,000
        rows. Returns the table, column, measure and row count."""
        return create_whatif_parameter(state, name, minimum, maximum, increment,
                                       default, format_string, description)

    @tool(read=True, idempotent=True)
    def pbi_list_whatif_parameters() -> list[dict]:
        """List what-if parameter tables: table, column, minimum / maximum /
        increment (parsed from GENERATESERIES), the value measure and its
        default, and the format string."""
        return list_whatif_parameters(state)
