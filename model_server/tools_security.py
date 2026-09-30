"""Model-server tools: row/object-level security, perspectives, translations.

Roles, perspectives and cultures are one-file-per-object under
``*.SemanticModel/definition/{roles,perspectives,cultures}`` and are
registered in ``model.tmdl`` with ``ref`` lines. The plain functions here take
an explicit ``state`` (``state.require()`` -> ``PbipProject``) so they can be
unit-tested without an MCP client; ``register()`` wraps them as ``pbi_*``
tools. The TMDL text work lives in ``core/tmdl_security.py``.
"""

from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import TYPE_CHECKING

from core import io_safe
from core import tmdl_security as ts
from core.dax_parser import parse_references, tokenize

if TYPE_CHECKING:  # pragma: no cover - typing only (never import the server here)
    from core.pbip import PbipProject
    from core.schemas import Table
    from model_server.server import ModelState


# --- shared helpers ---------------------------------------------------------

def _definition(project: PbipProject) -> Path:
    return project._require_model() / "definition"


def _rel(project: PbipProject, path: Path) -> str:
    return path.relative_to(project._require_model()).as_posix()


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def _clean_name(name: str, kind: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"{kind} name must be a non-empty string")
    n = name.strip()
    if any(ord(c) < 32 for c in n):
        raise ValueError(f"{kind} name must not contain line breaks or control characters")
    if len(n) > 128:
        raise ValueError(f"{kind} name is too long (max 128 characters)")
    return n


def _scan(folder: Path, keyword: str) -> dict[str, Path]:
    """{declared object name: file} for every ``*.tmdl`` in ``folder``."""
    out: dict[str, Path] = {}
    if not folder.is_dir():
        return out
    for f in sorted(folder.glob("*.tmdl")):
        try:
            name = ts.declared_name(_read(f), keyword)
        except OSError:
            continue
        if name is not None:
            out[name] = f
    return out


def _suggest(name: str, choices: list[str]) -> str:
    close = difflib.get_close_matches(name, choices, n=3, cutoff=0.6)
    if close:
        return f" Did you mean {', '.join(repr(c) for c in close)}?"
    if choices:
        shown = ", ".join(repr(c) for c in choices[:15])
        more = f" (+{len(choices) - 15} more)" if len(choices) > 15 else ""
        return f" Available: {shown}{more}."
    return ""


def _lookup(objects: dict[str, Path], name: str, kind: str) -> tuple[str, Path]:
    """Find an object by name (exact, then case-insensitive)."""
    if name in objects:
        return name, objects[name]
    folded = [k for k in objects if k.casefold() == str(name).casefold()]
    if len(folded) == 1:
        return folded[0], objects[folded[0]]
    raise ValueError(f"{kind} {name!r} not found.{_suggest(str(name), list(objects))}"
                     if objects else f"{kind} {name!r} not found (the model has none).")


def _ensure_free(objects: dict[str, Path], name: str, kind: str) -> None:
    for existing in objects:
        if existing.casefold() == name.casefold():
            raise ValueError(f"{kind} {existing!r} already exists (names are case-insensitive)")


def _new_path(folder: Path, name: str, objects: dict[str, Path]) -> Path:
    path = folder / f"{ts.safe_file_stem(name)}.tmdl"
    if path.exists():
        owner = next((k for k, v in objects.items() if v == path), None)
        raise ValueError(
            f"File {path.name} is already used by {owner!r}; choose a different name"
            if owner else f"File {path.name} already exists; choose a different name")
    return path


def _tables(project: PbipProject) -> dict[str, Table]:
    return {t.name: t for t in project.list_tables()}


def _resolve_table(tables: dict[str, Table], name: str) -> Table:
    if name in tables:
        return tables[name]
    folded = [t for k, t in tables.items() if k.casefold() == str(name).casefold()]
    if len(folded) == 1:
        return folded[0]
    raise ValueError(f"Table {name!r} not found.{_suggest(str(name), list(tables))}")


def _write_if_changed(project: PbipProject, path: Path, new: str) -> bool:
    old = _read(path) if path.exists() else None
    if old == new:
        return False
    project._write_text(path, new)
    return True


def _register_ref(project: PbipProject, kind: str, name: str) -> bool:
    model = _definition(project) / "model.tmdl"
    if not model.exists():
        return False
    return _write_if_changed(project, model, ts.add_ref_text(_read(model), kind, name))


def _unregister_ref(project: PbipProject, kind: str, name: str) -> bool:
    model = _definition(project) / "model.tmdl"
    if not model.exists():
        return False
    return _write_if_changed(project, model, ts.remove_ref_text(_read(model), kind, name))


def _delete_file(project: PbipProject, path: Path) -> None:
    """Remove a file; a ``.bak-*`` sibling is left (same recovery model as
    edits) unless the project runs with backups off."""
    if getattr(project, "backups", True):
        io_safe.backup(path)
    path.unlink()


def _ordered(objects: dict[str, Path], project: PbipProject, kind: str) -> list[str]:
    """Object names in model.tmdl ``ref`` order, unreferenced ones last."""
    model = _definition(project) / "model.tmdl"
    refs = ts.list_refs_text(_read(model), kind) if model.exists() else []
    rank = {n: i for i, n in enumerate(refs)}
    return sorted(objects, key=lambda n: (rank.get(n, len(rank)), n.casefold()))


def _model_default_culture(project: PbipProject) -> str | None:
    model = _definition(project) / "model.tmdl"
    if not model.exists():
        return None
    m = re.search(r"(?m)^\tculture:\s*(\S+)", _read(model))
    return m.group(1) if m else None


# --- roles: RLS -------------------------------------------------------------

def _check_filter(tables: dict[str, Table], measures: set[str], table: Table,
                  dax: str) -> list[str]:
    """Validate a row-filter expression; returns non-blocking warnings.

    Malformed DAX (unterminated string/quote/bracket, unbalanced brackets) and
    explicit ``Table[Column]`` references to something that does not exist are
    errors - Desktop refuses to load such a role. Anything the checker cannot
    be sure about (bare ``[Name]``) is only a warning.
    """
    if not isinstance(dax, str) or not dax.strip():
        raise ValueError(f"Filter expression for table {table.name!r} is empty")
    depth = {"(": 0, "{": 0}
    close = {")": "(", "}": "{"}
    for tok in tokenize(dax):
        if tok.kind == "UNKNOWN":
            raise ValueError(
                f"Filter for {table.name!r} is not valid DAX: unexpected {tok.text[:20]!r} "
                f"at line {tok.line}, column {tok.col} "
                "(unterminated string, quote, bracket or comment?)")
        if tok.kind == "PUNCT" and tok.text in ("(", "{"):
            depth[tok.text] += 1
        elif tok.kind == "PUNCT" and tok.text in close:
            depth[close[tok.text]] -= 1
            if depth[close[tok.text]] < 0:
                raise ValueError(f"Filter for {table.name!r} has an unmatched {tok.text!r} "
                                 f"at line {tok.line}, column {tok.col}")
    for opener, n in depth.items():
        if n:
            raise ValueError(f"Filter for {table.name!r} is missing a closing "
                             f"{')' if opener == '(' else '}'}")
    refs = parse_references(dax, known_measures=measures)
    by_fold = {k.casefold(): v for k, v in tables.items()}
    warnings: list[str] = []
    for tname, col in sorted(refs.columns):
        if any(tname.casefold() == v.casefold() for v in refs.variables):
            continue   # column of a VAR table
        ref_table = by_fold.get(tname.casefold())
        if ref_table is None:
            raise ValueError(
                f"Filter for {table.name!r} references table {tname!r}, which does not "
                f"exist.{_suggest(tname, list(tables))}")
        known = {c.name.casefold() for c in ref_table.columns} | \
                {m.name.casefold() for m in ref_table.measures}
        if col.casefold() not in known:
            raise ValueError(
                f"Filter for {table.name!r} references {tname}[{col}], but table "
                f"{ref_table.name!r} has no such column."
                f"{_suggest(col, [c.name for c in ref_table.columns])}")
    own = {c.name.casefold() for c in table.columns}
    for name in sorted(refs.unqualified):
        if name.casefold() not in own:
            warnings.append(
                f"[{name}] in the filter for {table.name!r} is neither a column of that "
                "table nor a measure; check the spelling.")
    return warnings


def _normalize_filters(project: PbipProject, table_filters) -> tuple[dict[str, str], list[str]]:
    if table_filters is None:
        return {}, []
    if not isinstance(table_filters, dict):
        raise ValueError("table_filters must be an object {table: DAX filter expression}")
    tables = _tables(project)
    measures = {m.name for t in tables.values() for m in t.measures}
    out: dict[str, str] = {}
    warnings: list[str] = []
    for tname, dax in table_filters.items():
        table = _resolve_table(tables, tname)
        if table.name in out:
            raise ValueError(f"Table {table.name!r} appears twice in table_filters")
        warnings += _check_filter(tables, measures, table, dax)
        out[table.name] = dax.strip("\n")
    return out, warnings


def create_role(state: ModelState, name: str, description: str | None = None,
                model_permission: str = "read",
                table_filters: dict[str, str] | None = None) -> dict:
    """Create ``roles/<name>.tmdl`` and register it in model.tmdl."""
    project = state.require()
    name = _clean_name(name, "Role")
    ts.validate_model_permission(model_permission)
    folder = _definition(project) / "roles"
    existing = _scan(folder, "role")
    _ensure_free(existing, name, "Role")
    filters, warnings = _normalize_filters(project, table_filters)
    path = _new_path(folder, name, existing)
    project._write_text(path, ts.emit_role_text(name, description, model_permission, filters))
    _register_ref(project, "role", name)
    result = {"ok": True, "action": "created", "role": name,
              "file": _rel(project, path), "model_permission": model_permission,
              "tables": sorted(filters)}
    if warnings:
        result["warnings"] = warnings
    return result


def _role_view(project: PbipProject, name: str, path: Path) -> dict:
    info = ts.parse_role_text(_read(path))
    return {
        "name": name,
        "description": info["description"],
        "model_permission": info["model_permission"],
        "file": _rel(project, path),
        "tables": [{"table": t, **spec} for t, spec in info["tables"].items()],
        "members": info["members"],
    }


def list_roles(state: ModelState) -> list[dict]:
    """Every role with its row filters and object-level-security settings."""
    project = state.require()
    objs = _scan(_definition(project) / "roles", "role")
    return [_role_view(project, n, objs[n]) for n in _ordered(objs, project, "role")]


def update_role(state: ModelState, name: str, description: str | None = None,
                model_permission: str | None = None,
                table_filters: dict[str, str] | None = None,
                remove_tables: list[str] | None = None) -> dict:
    """Change a role in place; omitted arguments leave that part untouched."""
    project = state.require()
    objs = _scan(_definition(project) / "roles", "role")
    role_name, path = _lookup(objs, name, "Role")
    if isinstance(remove_tables, str):
        remove_tables = [remove_tables]
    if description is None and model_permission is None and not table_filters \
            and not remove_tables:
        raise ValueError("Nothing to update: pass description, model_permission, "
                         "table_filters and/or remove_tables")
    if model_permission is not None:
        ts.validate_model_permission(model_permission)
    filters, warnings = _normalize_filters(project, table_filters)
    tables = _tables(project)
    text = _read(path)
    info = ts.parse_role_text(text)
    removals = []
    for t in remove_tables or []:
        tname = _resolve_table(tables, t).name if t not in info["tables"] else t
        if not (info["tables"].get(tname) or {}).get("filter"):
            raise ValueError(f"Role {role_name!r} has no row filter on table {tname!r}; "
                             f"filtered tables: {sorted(k for k, v in info['tables'].items() if v['filter'])}")
        if tname in filters:
            raise ValueError(f"Table {tname!r} is both in table_filters and remove_tables")
        removals.append(tname)

    changed: list[str] = []
    if description is not None:
        text = ts.set_role_description_text(text, description)
        changed.append("description")
    if model_permission is not None:
        text = ts.set_model_permission_text(text, model_permission)
        changed.append("model_permission")
    for tname, dax in filters.items():
        text = ts.upsert_table_permission_text(text, tname, dax)
        changed.append(f"filter:{tname}")
    for tname in removals:
        text = ts.upsert_table_permission_text(text, tname, None)
        changed.append(f"removed_filter:{tname}")
    wrote = _write_if_changed(project, path, text)
    result = {"ok": True, "action": "updated" if wrote else "unchanged",
              "role": role_name, "changed": changed if wrote else []}
    if warnings:
        result["warnings"] = warnings
    return result


def delete_role(state: ModelState, name: str) -> dict:
    """Delete a role's file and its ``ref role`` line."""
    project = state.require()
    objs = _scan(_definition(project) / "roles", "role")
    role_name, path = _lookup(objs, name, "Role")
    _delete_file(project, path)
    _unregister_ref(project, "role", role_name)
    return {"ok": True, "action": "deleted", "role": role_name, "file": _rel(project, path)}


# --- roles: OLS -------------------------------------------------------------

def set_column_permission(state: ModelState, role: str, table: str, column: str,
                          permission: str = "none") -> dict:
    """Set object-level security for one column in a role."""
    project = state.require()
    ts.validate_metadata_permission(permission)
    objs = _scan(_definition(project) / "roles", "role")
    role_name, path = _lookup(objs, role, "Role")
    tbl = _resolve_table(_tables(project), table)
    col = next((c for c in tbl.columns if c.name.casefold() == str(column).casefold()), None)
    if col is None:
        hint = (" It is a measure; object-level security applies to columns and whole tables."
                if any(m.name.casefold() == str(column).casefold() for m in tbl.measures) else "")
        raise ValueError(f"Column {column!r} not found in table {tbl.name!r}.{hint}"
                         f"{_suggest(str(column), [c.name for c in tbl.columns])}")
    new = ts.set_column_permission_text(_read(path), tbl.name, col.name, permission)
    wrote = _write_if_changed(project, path, new)
    return {"ok": True, "action": "updated" if wrote else "unchanged", "role": role_name,
            "table": tbl.name, "column": col.name, "permission": permission}


def set_table_permission_metadata(state: ModelState, role: str, table: str,
                                  metadata_permission: str = "none") -> dict:
    """Set object-level security for a whole table in a role."""
    project = state.require()
    ts.validate_metadata_permission(metadata_permission)
    objs = _scan(_definition(project) / "roles", "role")
    role_name, path = _lookup(objs, role, "Role")
    tbl = _resolve_table(_tables(project), table)
    new = ts.set_table_metadata_permission_text(_read(path), tbl.name, metadata_permission)
    wrote = _write_if_changed(project, path, new)
    return {"ok": True, "action": "updated" if wrote else "unchanged", "role": role_name,
            "table": tbl.name, "metadata_permission": metadata_permission}


# --- perspectives -----------------------------------------------------------

_SPEC_KEYS = {"columns", "measures", "hierarchies", "include_all"}


def _as_names(value, what: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"{what} must be a list of names")
    seen: list[str] = []
    for v in value:
        if v not in seen:
            seen.append(v)
    return seen


def _hierarchies(project: PbipProject, table: str) -> list[str]:
    return ts.list_hierarchies_text(_read(project._table_file(table)))


def _normalize_perspective_tables(project: PbipProject, tables_arg,
                                  *, require_content: bool = True) -> dict[str, dict]:
    """Validate ``{table: {columns, measures, hierarchies, include_all}}``
    against the model and return it with canonical names."""
    if tables_arg is None:
        return {}
    if not isinstance(tables_arg, dict):
        raise ValueError('tables must be an object {table: {"columns": [...], '
                         '"measures": [...], "hierarchies": [...]}}')
    model = _tables(project)
    out: dict[str, dict] = {}
    for tname, spec in tables_arg.items():
        tbl = _resolve_table(model, tname)
        if tbl.name in out:
            raise ValueError(f"Table {tbl.name!r} appears twice")
        spec = spec or {}
        if not isinstance(spec, dict):
            raise ValueError(f"Spec for table {tbl.name!r} must be an object")
        unknown = set(spec) - _SPEC_KEYS
        if unknown:
            raise ValueError(f"Unknown key(s) {sorted(unknown)} for table {tbl.name!r}; "
                             f"use {sorted(_SPEC_KEYS)}")
        canon = {"include_all": bool(spec.get("include_all"))}
        pools = {"columns": [c.name for c in tbl.columns],
                 "measures": [m.name for m in tbl.measures],
                 "hierarchies": None}
        for key in ts.PERSPECTIVE_KEYS:
            names = _as_names(spec.get(key), f"{key} of table {tbl.name!r}")
            pool = pools[key] if pools[key] is not None else _hierarchies(project, tbl.name)
            resolved = []
            for n in names:
                hit = next((p for p in pool if p == n), None) or \
                    next((p for p in pool if p.casefold() == n.casefold()), None)
                if hit is None:
                    raise ValueError(
                        f"{key[:-1].capitalize() if key != 'hierarchies' else 'Hierarchy'} "
                        f"{n!r} not found in table {tbl.name!r}.{_suggest(n, pool)}")
                resolved.append(hit)
            canon[key] = resolved
        if require_content and not (canon["include_all"] or any(canon[k] for k in ts.PERSPECTIVE_KEYS)):
            raise ValueError(
                f"Table {tbl.name!r} lists no objects: give columns/measures/hierarchies "
                "or set include_all=true")
        out[tbl.name] = canon
    return out


def create_perspective(state: ModelState, name: str, description: str | None = None,
                       tables: dict[str, dict] | None = None) -> dict:
    """Create ``perspectives/<name>.tmdl`` and register it in model.tmdl."""
    project = state.require()
    name = _clean_name(name, "Perspective")
    folder = _definition(project) / "perspectives"
    existing = _scan(folder, "perspective")
    _ensure_free(existing, name, "Perspective")
    canon = _normalize_perspective_tables(project, tables)
    path = _new_path(folder, name, existing)
    project._write_text(path, ts.emit_perspective_text(name, description, canon))
    _register_ref(project, "perspective", name)
    return {"ok": True, "action": "created", "perspective": name,
            "file": _rel(project, path), "tables": sorted(canon)}


def list_perspectives(state: ModelState) -> list[dict]:
    """Every perspective with the objects it exposes per table."""
    project = state.require()
    objs = _scan(_definition(project) / "perspectives", "perspective")
    out = []
    for n in _ordered(objs, project, "perspective"):
        info = ts.parse_perspective_text(_read(objs[n]))
        out.append({"name": n, "description": info["description"],
                    "file": _rel(project, objs[n]), "tables": info["tables"]})
    return out


def update_perspective(state: ModelState, name: str, description: str | None = None,
                       tables: dict[str, dict] | None = None,
                       remove_tables: list[str] | None = None,
                       remove_objects: dict[str, dict] | None = None) -> dict:
    """Edit a perspective: remove first (remove_tables, remove_objects), then add."""
    project = state.require()
    objs = _scan(_definition(project) / "perspectives", "perspective")
    pname, path = _lookup(objs, name, "Perspective")
    if isinstance(remove_tables, str):
        remove_tables = [remove_tables]
    if description is None and not tables and not remove_tables and not remove_objects:
        raise ValueError("Nothing to update: pass description, tables, remove_tables "
                         "and/or remove_objects")
    add = _normalize_perspective_tables(project, tables)
    model = _tables(project)
    text = _read(path)
    current = ts.parse_perspective_text(text)["tables"]
    changed: list[str] = []

    if description is not None:
        text = ts.set_perspective_description_text(text, description)
        changed.append("description")
    for t in remove_tables or []:
        tname = t if t in current else _resolve_table(model, t).name
        if tname not in current:
            raise ValueError(f"Table {t!r} is not part of perspective {pname!r}; "
                             f"it has {sorted(current)}")
        text = ts.remove_perspective_table_text(text, tname)
        current.pop(tname, None)
        changed.append(f"removed_table:{tname}")
    for t, spec in (remove_objects or {}).items():
        tname = t if t in current else _resolve_table(model, t).name
        if tname not in current:
            raise ValueError(f"Table {t!r} is not part of perspective {pname!r}")
        if not isinstance(spec, dict) or set(spec) - _SPEC_KEYS:
            raise ValueError(f"remove_objects[{t!r}] must be an object with keys "
                             f"{sorted(_SPEC_KEYS)}")
        drop = {k: _as_names(spec.get(k), f"{k} of table {tname!r}") for k in ts.PERSPECTIVE_KEYS}
        drop["include_all"] = bool(spec.get("include_all"))
        for k in ts.PERSPECTIVE_KEYS:
            missing = [n for n in drop[k] if n not in current[tname][k]]
            if missing:
                raise ValueError(f"{k} {missing} are not in perspective {pname!r} "
                                 f"for table {tname!r} (has {current[tname][k]})")
        text = ts.remove_perspective_objects_text(text, tname, drop)
        current = ts.parse_perspective_text(text)["tables"]
        changed.append(f"removed_objects:{tname}")
    for tname, spec in add.items():
        text = ts.add_perspective_objects_text(text, tname, spec)
        changed.append(f"added:{tname}")
    wrote = _write_if_changed(project, path, text)
    return {"ok": True, "action": "updated" if wrote else "unchanged",
            "perspective": pname, "changed": changed if wrote else []}


def delete_perspective(state: ModelState, name: str) -> dict:
    """Delete a perspective's file and its ``ref perspective`` line."""
    project = state.require()
    objs = _scan(_definition(project) / "perspectives", "perspective")
    pname, path = _lookup(objs, name, "Perspective")
    _delete_file(project, path)
    _unregister_ref(project, "perspective", pname)
    return {"ok": True, "action": "deleted", "perspective": pname, "file": _rel(project, path)}


# --- cultures / translations ------------------------------------------------

def add_culture(state: ModelState, code: str) -> dict:
    """Create ``cultures/<code>.tmdl`` and register it in model.tmdl."""
    project = state.require()
    code = ts.validate_culture_code(code)
    folder = _definition(project) / "cultures"
    existing = _scan(folder, "cultureInfo")
    _ensure_free(existing, code, "Culture")
    path = _new_path(folder, code, existing)
    project._write_text(path, ts.emit_culture_text(code))
    _register_ref(project, "cultureInfo", code)
    return {"ok": True, "action": "created", "culture": code, "file": _rel(project, path),
            "is_model_default": code.casefold() == (_model_default_culture(project) or "").casefold()}


def _flatten_translations(info: dict) -> list[dict]:
    rows = []
    for tname, t in info["tables"].items():
        if any(k in t for k in ("caption", "description")):
            rows.append({"object_type": "table", "table": tname, "name": tname,
                         "caption": t.get("caption"), "description": t.get("description"),
                         "display_folder": None})
        for key, kind in (("columns", "column"), ("measures", "measure"),
                          ("hierarchies", "hierarchy")):
            for oname, props in t[key].items():
                rows.append({"object_type": kind, "table": tname, "name": oname,
                             "caption": props.get("caption"),
                             "description": props.get("description"),
                             "display_folder": props.get("displayFolder")})
    return rows


def list_translations(state: ModelState, culture: str | None = None) -> dict:
    """All cultures (no argument) or every translated object of one culture."""
    project = state.require()
    objs = _scan(_definition(project) / "cultures", "cultureInfo")
    default = (_model_default_culture(project) or "").casefold()
    if culture is None:
        rows = []
        for n in _ordered(objs, project, "cultureInfo"):
            info = ts.parse_culture_text(_read(objs[n]))
            rows.append({"culture": n, "file": _rel(project, objs[n]),
                         "is_model_default": n.casefold() == default,
                         "translated_objects": len(_flatten_translations(info)),
                         "has_linguistic_metadata": info["has_linguistic_metadata"]})
        return {"cultures": rows}
    cname, path = _lookup(objs, culture, "Culture")
    info = ts.parse_culture_text(_read(path))
    return {"culture": cname, "file": _rel(project, path),
            "translations": _flatten_translations(info)}


def set_translation(state: ModelState, culture: str, object_type: str, table: str,
                    name: str | None = None, caption: str | None = None,
                    description: str | None = None,
                    display_folder: str | None = None) -> dict:
    """Set a translated caption / description / display folder for one object."""
    project = state.require()
    if object_type not in ts.TRANSLATION_OBJECTS:
        raise ValueError(f"object_type must be one of {list(ts.TRANSLATION_OBJECTS)}")
    objs = _scan(_definition(project) / "cultures", "cultureInfo")
    if not objs or (culture not in objs and
                    culture.casefold() not in {k.casefold() for k in objs}):
        raise ValueError(f"Culture {culture!r} not found. Create it first with "
                         f"pbi_add_culture.{_suggest(str(culture), list(objs))}")
    cname, path = _lookup(objs, culture, "Culture")
    tbl = _resolve_table(_tables(project), table)
    target = tbl.name
    if object_type != "table":
        if not name:
            raise ValueError(f"name is required when object_type is {object_type!r}")
        pool = {"column": [c.name for c in tbl.columns],
                "measure": [m.name for m in tbl.measures],
                "hierarchy": _hierarchies(project, tbl.name)}[object_type]
        target = next((p for p in pool if p == name), None) or \
            next((p for p in pool if p.casefold() == name.casefold()), None)
        if target is None:
            raise ValueError(f"{object_type.capitalize()} {name!r} not found in table "
                             f"{tbl.name!r}.{_suggest(name, pool)}")
    new = ts.set_translation_text(
        _read(path), object_type, tbl.name,
        None if object_type == "table" else target,
        caption, description, display_folder)
    wrote = _write_if_changed(project, path, new)
    return {"ok": True, "action": "updated" if wrote else "unchanged", "culture": cname,
            "object_type": object_type, "table": tbl.name, "name": target}


def delete_culture(state: ModelState, code: str) -> dict:
    """Delete a culture's file (all its translations) and its ref line."""
    project = state.require()
    objs = _scan(_definition(project) / "cultures", "cultureInfo")
    cname, path = _lookup(objs, code, "Culture")
    _delete_file(project, path)
    _unregister_ref(project, "cultureInfo", cname)
    result = {"ok": True, "action": "deleted", "culture": cname, "file": _rel(project, path)}
    if cname.casefold() == (_model_default_culture(project) or "").casefold():
        result["note"] = ("This is the model's default culture (model.tmdl `culture:`); "
                          "Power BI Desktop may re-create its cultureInfo on the next save.")
    return result


# --- MCP registration -------------------------------------------------------

def register(mcp, state, tool) -> None:
    """Define the thin ``pbi_*`` wrappers (called by ``load_tool_modules``)."""

    @tool(write=True)
    def pbi_create_role(name: str, description: str | None = None,
                        model_permission: str = "read",
                        table_filters: dict[str, str] | None = None) -> dict:
        """Create a row-level-security role (roles/<name>.tmdl, registered with
        `ref role` in model.tmdl). `table_filters` maps a table name to a DAX
        boolean filter, e.g. {"Sales": "[Region] = USERPRINCIPALNAME()"}; the
        expressions are checked for balanced brackets and existing Table[Column]
        references before anything is written. `model_permission` is read
        (default), readRefresh, refresh, administrator or none. Role members
        are assigned in the Power BI service, not here. Returns the file, the
        filtered tables and any non-blocking warnings."""
        return create_role(state, name, description, model_permission, table_filters)

    @tool(write=True, idempotent=True)
    def pbi_update_role(name: str, description: str | None = None,
                        model_permission: str | None = None,
                        table_filters: dict[str, str] | None = None,
                        remove_tables: list[str] | None = None) -> dict:
        """Edit a role in place; only the parts you pass change. `description`
        ("" removes it), `model_permission`, `table_filters` {table: DAX} to
        add or replace row filters, and `remove_tables` [table, ...] to clear
        the row filter of those tables (object-level-security settings on the
        table are kept). Returns what changed."""
        return update_role(state, name, description, model_permission,
                           table_filters, remove_tables)

    @tool(read=True, idempotent=True)
    def pbi_list_roles() -> list[dict]:
        """List security roles in model.tmdl order: name, description,
        model_permission, file, per-table row filter plus object-level security
        (metadata_permission, columns {name: permission}), and members."""
        return list_roles(state)

    @tool(write=True, destructive=True)
    def pbi_delete_role(name: str) -> dict:
        """Delete a security role: removes roles/<name>.tmdl (a .bak copy stays
        beside it) and its `ref role` line. Revert with pbi_undo."""
        return delete_role(state, name)

    @tool(write=True, idempotent=True)
    def pbi_set_column_permission(role: str, table: str, column: str,
                                  permission: str = "none") -> dict:
        """Object-level security: set `columnPermission` for one column inside
        a role's tablePermission block (created without a row filter when the
        table has none). `permission` is "none" (column hidden from the role),
        "read", or "default" (removes the setting). Returns the resulting
        permission."""
        return set_column_permission(state, role, table, column, permission)

    @tool(write=True, idempotent=True)
    def pbi_set_table_permission_metadata(role: str, table: str,
                                          metadata_permission: str = "none") -> dict:
        """Object-level security for a whole table: `metadataPermission` on the
        role's tablePermission block. "none" hides the entire table from the
        role, "read" grants it explicitly, "default" removes the setting.
        Returns the resulting permission."""
        return set_table_permission_metadata(state, role, table, metadata_permission)

    @tool(write=True)
    def pbi_create_perspective(name: str, description: str | None = None,
                               tables: dict[str, dict] | None = None) -> dict:
        """Create a perspective (perspectives/<name>.tmdl, registered with
        `ref perspective`). `tables` maps a table to {"columns": [...],
        "measures": [...], "hierarchies": [...]} (or {"include_all": true});
        every listed object must exist in that table. Returns the file and
        tables."""
        return create_perspective(state, name, description, tables)

    @tool(write=True, idempotent=True)
    def pbi_update_perspective(name: str, description: str | None = None,
                               tables: dict[str, dict] | None = None,
                               remove_tables: list[str] | None = None,
                               remove_objects: dict[str, dict] | None = None) -> dict:
        """Edit a perspective. Removals run first, then additions: `remove_tables`
        [table, ...], `remove_objects` {table: {"columns": [...], "measures":
        [...], "hierarchies": [...]}}, then `tables` (same shape as
        pbi_create_perspective) adds objects. `description` "" clears it.
        Returns what changed."""
        return update_perspective(state, name, description, tables,
                                  remove_tables, remove_objects)

    @tool(read=True, idempotent=True)
    def pbi_list_perspectives() -> list[dict]:
        """List perspectives: name, description, file and, per table, the
        include_all flag and the columns / measures / hierarchies exposed."""
        return list_perspectives(state)

    @tool(write=True, destructive=True)
    def pbi_delete_perspective(name: str) -> dict:
        """Delete a perspective: removes perspectives/<name>.tmdl (a .bak copy
        stays beside it) and its `ref perspective` line. Revert with pbi_undo."""
        return delete_perspective(state, name)

    @tool(write=True)
    def pbi_add_culture(code: str) -> dict:
        """Add a translation culture, e.g. "de-DE": creates cultures/<code>.tmdl
        (cultureInfo plus the minimal linguisticMetadata Desktop writes) and a
        `ref cultureInfo` line in model.tmdl. Add captions afterwards with
        pbi_set_translation. Returns the file."""
        return add_culture(state, code)

    @tool(write=True, idempotent=True)
    def pbi_set_translation(culture: str, object_type: str, table: str,
                            name: str | None = None, caption: str | None = None,
                            description: str | None = None,
                            display_folder: str | None = None) -> dict:
        """Set translated metadata in an existing culture. `object_type` is
        "table", "column", "measure" or "hierarchy"; `table` is the owning table
        and `name` the column/measure/hierarchy (omit for a table). Pass any of
        `caption`, `description`, `display_folder` (tables have no display
        folder); "" removes a value, omitted values are kept. The object must
        exist in the model. Returns the translated object."""
        return set_translation(state, culture, object_type, table, name,
                               caption, description, display_folder)

    @tool(read=True, idempotent=True)
    def pbi_list_translations(culture: str | None = None) -> dict:
        """Without `culture`: the model's cultures (file, whether it is the
        model default, how many objects are translated). With `culture`: every
        translated table / column / measure / hierarchy with its caption,
        description and display_folder."""
        return list_translations(state, culture)

    @tool(write=True, destructive=True)
    def pbi_delete_culture(code: str) -> dict:
        """Delete a culture and all its translations: removes cultures/<code>.tmdl
        (a .bak copy stays beside it) and its `ref cultureInfo` line. Revert
        with pbi_undo."""
        return delete_culture(state, code)
