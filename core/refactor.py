"""Cascading rename of measures, columns and tables across model and report.

Renaming an object rewrites every place that refers to it:

* TMDL: the object's own header line; every DAX expression in every table,
  role, calculation-group and culture file (through
  core.dax_parser.rename_references, which leaves strings, comments and
  whitespace exactly as they were); ``sortByColumn`` and hierarchy
  ``column:`` lines; ``relationships.tmdl``; ``model.tmdl`` ``ref table``
  lines; role ``tablePermission`` / ``columnPermission``, perspective and
  translation members. Power Query (M) partition sources and
  ``expressions.tmdl`` are never touched: M field names are *source* names,
  not model names.
* PBIR: every JSON file under the report definition (report.json, pages,
  visuals, bookmarks, reportExtensions.json): field references of the form
  ``{"Expression": {"SourceRef": {"Entity": T}}, "Property": P}`` (also when
  the entity is reached through a ``From`` alias), ``Entity`` names, and the
  ``queryRef`` / selector strings ("T.P", "Sum(T.P)") that mirror them.

Every write goes through PbipProject helpers, so backups and the undo
journal cover the whole cascade, including a renamed table file.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from core import io_safe
from core.dax_parser import rename_references
from core.tmdl import _quote_col_ref, _split_col_ref, _unquote, quote_ident

_IDENT = r"'(?:[^']|'')*'|[^\s=]+"
_RE_TABLE_HDR = re.compile(rf"^table\s+(?P<name>{_IDENT})\s*$")
_RE_MEASURE_HDR = re.compile(rf"^(?P<ind>\t+)measure\s+(?P<name>{_IDENT})(?P<rest>\s*=.*)$")
_RE_COLUMN_HDR = re.compile(rf"^(?P<ind>\t+)column\s+(?P<name>{_IDENT})(?P<rest>\s*=.*|\s*)$")
_RE_PARTITION_HDR = re.compile(rf"^(?P<ind>\t+)partition\s+(?P<name>{_IDENT})(?P<rest>\s*=\s*(?P<kind>\w+)\s*)$")
_RE_SORT_BY = re.compile(r"^(?P<ind>\t+)sortByColumn:\s*(?P<name>.+?)\s*$")
_RE_LEVEL_COL = re.compile(r"^(?P<ind>\t+)column:\s*(?P<name>.+?)\s*$")
_RE_REF_TABLE = re.compile(r"^(?P<ind>\t+)ref table\s+(?P<name>.+?)\s*$")
_RE_REL_COL = re.compile(r"^(?P<ind>\t+)(?P<key>fromColumn|toColumn):\s*(?P<ref>.+?)\s*$")

# scoped members: (scope keyword, member keyword) per folder
_SCOPES = {
    "roles": ("tablePermission", {"column": "columnPermission"}),
    "perspectives": ("perspectiveTable", {"column": "perspectiveColumn",
                                          "measure": "perspectiveMeasure"}),
    "cultures": ("table", {"column": "column", "measure": "measure"}),
}


def _same(a: str, b: str) -> bool:
    return a.casefold() == b.casefold()


def _tabs(line: str) -> int:
    return len(line) - len(line.lstrip("\t"))


# --- TMDL text helpers ------------------------------------------------------

_RE_DAX_HDR = re.compile(
    rf"^(?P<ind>\t*)(?:(?:measure|column|calculationItem|tablePermission)\s+(?:{_IDENT})"
    r"|source|expression|targetExpression|statusExpression|trendExpression|filterExpression)"
    r"\s*=(?P<dax>.*)$")


def _rewrite_dax_regions(text: str, fn) -> str:
    """Apply `fn` to every DAX expression in a TMDL file and nothing else.

    A region is the text after `=` on a measure / calculated column /
    calculation item / role filter / calculated-partition `source` header plus
    the continuation lines indented deeper than the header's properties.
    Property lines, headers, Power Query (M) sources and expressions.tmdl-style
    M are never handed to `fn`. `fn` must preserve line structure (the DAX
    renamer does); if it does not, the region is left unchanged.
    """
    lines = text.split("\n")
    out = list(lines)
    i = 0
    m_partition_depth: int | None = None
    while i < len(lines):
        line = lines[i]
        pm = _RE_PARTITION_HDR.match(line)
        if pm:
            m_partition_depth = None if pm.group("kind").lower() == "calculated" else _tabs(line)
            i += 1
            continue
        if m_partition_depth is not None and line.strip() and _tabs(line) <= m_partition_depth:
            m_partition_depth = None
        hm = _RE_DAX_HDR.match(line)
        if hm is None or (m_partition_depth is not None and _tabs(line) > m_partition_depth):
            i += 1
            continue
        depth = _tabs(line)
        j = i + 1
        while j < len(lines) and (not lines[j].strip() or _tabs(lines[j]) > depth + 1):
            j += 1
        while j > i + 1 and not lines[j - 1].strip():
            j -= 1                                   # trailing blank lines stay outside
        region = "\n".join([hm.group("dax")] + lines[i + 1:j])
        new = fn(region)
        if new != region and new.count("\n") == region.count("\n"):
            parts = new.split("\n")
            out[i] = line[:len(line) - len(hm.group("dax"))] + parts[0]
            out[i + 1:j] = parts[1:]
        i = j
    return "\n".join(out)


def _rename_scope_lines(text: str, keywords: tuple[str, ...], old: str, new: str) -> str:
    """Rename `<keyword> <old>` object headers (tablePermission / perspectiveTable / table)."""
    regex = re.compile(rf"^(?P<ind>\t*)(?P<kw>{'|'.join(keywords)})\s+(?P<name>{_IDENT})(?P<rest>\s*=.*|\s*)$")
    out = []
    for line in text.split("\n"):
        m = regex.match(line)
        if m and _same(_unquote(m.group("name")), old):
            line = f"{m.group('ind')}{m.group('kw')} {quote_ident(new)}{m.group('rest')}"
        out.append(line)
    return "\n".join(out)


def _rename_header(text: str, regex: re.Pattern, old: str, new: str) -> tuple[str, bool]:
    changed = False
    out = []
    for line in text.split("\n"):
        m = regex.match(line)
        if m and _same(_unquote(m.group("name")), old):
            ind = m.groupdict().get("ind", "")
            keyword = line[len(ind):].split(None, 1)[0]
            rest = m.groupdict().get("rest", "")
            line = f"{ind}{keyword} {quote_ident(new)}{rest}"
            changed = True
        out.append(line)
    return "\n".join(out), changed


def _rename_partition_headers(text: str, old: str, new: str) -> str:
    out = []
    for line in text.split("\n"):
        m = _RE_PARTITION_HDR.match(line)
        if m and _same(_unquote(m.group("name")), old):
            line = f"{m.group('ind')}partition {quote_ident(new)}{m.group('rest')}"
        out.append(line)
    return "\n".join(out)


def _rename_value_lines(text: str, regex: re.Pattern, old: str, new: str) -> str:
    out = []
    for line in text.split("\n"):
        m = regex.match(line)
        if m and _same(_unquote(m.group("name")), old):
            key = line[len(m.group("ind")):].split(":", 1)[0]
            line = f"{m.group('ind')}{key}: {quote_ident(new)}"
        out.append(line)
    return "\n".join(out)


def _rename_scoped(text: str, folder: str, kind: str, table: str,
                   old: str, new: str) -> str:
    """Rename a column/measure member inside the block of `table` in a
    role / perspective / culture file."""
    scope_kw, members = _SCOPES[folder]
    member_kw = members.get(kind)
    if member_kw is None:
        return text
    scope_re = re.compile(rf"^(\t*){scope_kw}\s+(?P<name>{_IDENT})(\s*=.*|\s*)$")
    member_re = re.compile(rf"^(?P<ind>\t+){member_kw}\s+(?P<name>{_IDENT})(?P<rest>\s*=.*|\s*)$")
    out, in_scope = [], False
    for line in text.split("\n"):
        sm = scope_re.match(line)
        if sm:
            in_scope = _same(_unquote(sm.group("name")), table)
        else:
            mm = member_re.match(line)
            if in_scope and mm and _same(_unquote(mm.group("name")), old):
                line = f"{mm.group('ind')}{member_kw} {quote_ident(new)}{mm.group('rest')}"
        out.append(line)
    return "\n".join(out)


def _rename_relationships(text: str, *, table: str | None = None,
                          column: tuple[str, str, str] | None = None) -> str:
    """table=(old,new) or column=(table, old, new) in relationships.tmdl."""
    out = []
    for line in text.split("\n"):
        m = _RE_REL_COL.match(line)
        if m:
            t, c = _split_col_ref(m.group("ref"))
            if table is not None and _same(t, table[0]):
                t = table[1]
            if column is not None and _same(t, column[0]) and _same(c, column[1]):
                c = column[2]
            line = f"{m.group('ind')}{m.group('key')}: {_quote_col_ref(t, c)}"
        out.append(line)
    return "\n".join(out)


# --- PBIR JSON helpers ------------------------------------------------------

def _entity_of(node: dict, aliases: dict) -> str | None:
    src = (node.get("Expression") or {}).get("SourceRef") or {}
    if "Entity" in src:
        return src["Entity"]
    if "Source" in src:
        return aliases.get(src["Source"])
    return None


def _transform(node, rule, aliases: dict | None = None):
    """Rebuild `node`, applying `rule(dict|str, aliases) -> replacement|None`."""
    aliases = dict(aliases or {})
    if isinstance(node, dict):
        if isinstance(node.get("From"), list):
            for entry in node["From"]:
                if isinstance(entry, dict) and "Name" in entry and "Entity" in entry:
                    aliases[entry["Name"]] = entry["Entity"]
        replaced = rule(node, aliases)
        if replaced is not None:
            node = replaced
        return {k: _transform(v, rule, aliases) for k, v in node.items()}
    if isinstance(node, list):
        return [_transform(v, rule, aliases) for v in node]
    if isinstance(node, str):
        replaced = rule(node, aliases)
        return node if replaced is None else replaced
    return node


def _string_rule(old_ref: str, new_ref: str):
    def rule(s: str) -> str | None:
        if s == old_ref:
            return new_ref
        if f"({old_ref})" in s:
            return s.replace(f"({old_ref})", f"({new_ref})")
        return None
    return rule


def _field_rule(table: str, old: str, new: str):
    strings = _string_rule(f"{table}.{old}", f"{table}.{new}")

    def rule(node, aliases):
        if isinstance(node, str):
            return strings(node)
        if (isinstance(node.get("Property"), str) and _same(node["Property"], old)
                and (_entity_of(node, aliases) or "") .casefold() == table.casefold()):
            return {**node, "Property": new}
        return None
    return rule


def _table_rule(old: str, new: str):
    prefix = f"{old}."

    def rule(node, aliases):
        if isinstance(node, str):
            if node.startswith(prefix):
                return new + node[len(old):]
            if f"({prefix}" in node:
                return node.replace(f"({prefix}", f"({new}.")
            return None
        if isinstance(node.get("Entity"), str) and _same(node["Entity"], old):
            return {**node, "Entity": new}
        return None
    return rule


# --- driver -----------------------------------------------------------------

class Renamer:
    def __init__(self, project):
        self.project = project
        self.changed: list[str] = []

    # model files
    def _model_def(self) -> Path:
        return self.project._require_model() / "definition"

    def _rel(self, path: Path) -> str:
        root = self._root()
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            return str(path)

    def _root(self) -> Path:
        p = Path(self.project.path)
        if p.is_file():
            return p.parent
        if p.name.endswith((".SemanticModel", ".Report")):
            return p.parent
        return p

    def _tmdl_files(self, folder: str) -> list[Path]:
        d = self._model_def() / folder
        return sorted(d.glob("*.tmdl")) if d.is_dir() else []

    def _edit_text(self, path: Path, fn) -> None:
        text = path.read_text(encoding="utf-8-sig")
        new = fn(text)
        if new != text:
            self.project._write_text(path, new)
            self.changed.append(self._rel(path))

    def _report_files(self) -> list[Path]:
        try:
            rd = self.project._require_report() / "definition"
        except FileNotFoundError:
            return []
        files = [rd / "report.json", rd / "reportExtensions.json"]
        for sub in ("pages", "bookmarks"):
            if (rd / sub).is_dir():
                files.extend(sorted((rd / sub).rglob("*.json")))
        return [f for f in files if f.is_file() and ".pbi" not in f.parts]

    def _edit_report(self, rule) -> None:
        for path in self._report_files():
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            new = _transform(data, rule)
            if new != data:
                self.project._write_json(path, new)
                self.changed.append(self._rel(path))

    # --- public operations ----------------------------------------------

    def rename_measure(self, table: str, old: str, new: str) -> dict:
        old, new = old.strip(), new.strip()
        measures = self.project.list_measures()
        target = next((m for m in measures if _same(m.table, table) and _same(m.name, old)), None)
        if target is None:
            raise ValueError(f"Measure '{table}'[{old}] not found.")
        if _same(old, new):
            raise ValueError("New name is identical to the old one.")
        if not new or any(_same(m.name, new) for m in measures):
            raise ValueError(f"Measure name '{new}' is empty or already used (measure names are unique model-wide).")
        table = target.table

        def dax(text: str) -> str:
            return rename_references(text, measure=(old, new))

        for path in self._tmdl_files("tables"):
            def fn(text, path=path):
                text = _rewrite_dax_regions(text, dax)
                if _same(self._declared_table(text), table):
                    text, _ = _rename_header(text, _RE_MEASURE_HDR, old, new)
                return text
            self._edit_text(path, fn)
        for folder in ("roles", "perspectives", "cultures"):
            for path in self._tmdl_files(folder):
                self._edit_text(path, lambda t, f=folder: _rename_scoped(
                    _rewrite_dax_regions(t, dax) if f == "roles" else t,
                    f, "measure", table, old, new))
        self._edit_report(_field_rule(table, old, new))
        return self._result("measure", table, old, new)

    def rename_column(self, table: str, old: str, new: str) -> dict:
        old, new = old.strip(), new.strip()
        tbl = next((t for t in self.project.list_tables() if _same(t.name, table)), None)
        if tbl is None:
            raise ValueError(f"Table '{table}' not found.")
        cols = [c.name for c in tbl.columns]
        if not any(_same(c, old) for c in cols):
            raise ValueError(f"Column '{table}'[{old}] not found.")
        if _same(old, new):
            raise ValueError("New name is identical to the old one.")
        if not new or any(_same(c, new) for c in cols):
            raise ValueError(f"Column name '{new}' is empty or already exists in '{table}'.")
        table = tbl.name
        known = {m.name for m in self.project.list_measures()}

        def dax_qualified(text: str) -> str:
            return rename_references(text, column=((table, old), (table, new)))

        def dax_own(text: str) -> str:
            return rename_references(text, column=((table, old), (table, new)),
                                     known_measures=known)

        for path in self._tmdl_files("tables"):
            def fn(text, path=path):
                own = _same(self._declared_table(text), table)
                text = _rewrite_dax_regions(text, dax_own if own else dax_qualified)
                if own:
                    text, _ = _rename_header(text, _RE_COLUMN_HDR, old, new)
                    text = _rename_value_lines(text, _RE_SORT_BY, old, new)
                    text = _rename_value_lines(text, _RE_LEVEL_COL, old, new)
                return text
            self._edit_text(path, fn)
        rel = self._model_def() / "relationships.tmdl"
        if rel.exists():
            self._edit_text(rel, lambda t: _rename_relationships(t, column=(table, old, new)))
        for folder in ("roles", "perspectives", "cultures"):
            for path in self._tmdl_files(folder):
                self._edit_text(path, lambda t, f=folder: _rename_scoped(
                    _rewrite_dax_regions(t, dax_qualified) if f == "roles" else t,
                    f, "column", table, old, new))
        self._edit_report(_field_rule(table, old, new))
        return self._result("column", table, old, new)

    def rename_table(self, old: str, new: str) -> dict:
        old, new = old.strip(), new.strip()
        tables = self.project.list_tables()
        tbl = next((t for t in tables if _same(t.name, old)), None)
        if tbl is None:
            raise ValueError(f"Table '{old}' not found.")
        if _same(old, new):
            raise ValueError("New name is identical to the old one.")
        if not new or any(_same(t.name, new) for t in tables):
            raise ValueError(f"Table name '{new}' is empty or already exists.")
        old = tbl.name

        def dax(text: str) -> str:
            return rename_references(text, table=(old, new))

        for path in self._tmdl_files("tables"):
            own = _same(self._declared_table(path.read_text(encoding="utf-8-sig")), old)

            def fn(text):
                text = _rewrite_dax_regions(text, dax)
                text = _rename_partition_headers(text, old, new)
                if own:
                    text, _ = _rename_header(text, _RE_TABLE_HDR, old, new)
                return text
            if own and _same(path.stem, old):
                text = fn(path.read_text(encoding="utf-8-sig"))
                target = path.with_name(f"{new}.tmdl")
                if self.project.backups:
                    io_safe.backup(path)
                self.project._write_text(target, text)
                path.unlink()
                self.changed.extend([self._rel(path), self._rel(target)])
            else:
                self._edit_text(path, fn)
        model = self._model_def() / "model.tmdl"
        if model.exists():
            self._edit_text(model, lambda t: _rename_scope_lines(
                t, ("ref table",), old, new))
        rel = self._model_def() / "relationships.tmdl"
        if rel.exists():
            self._edit_text(rel, lambda t: _rename_relationships(t, table=(old, new)))
        scope_kw = {"roles": ("tablePermission",), "perspectives": ("perspectiveTable",),
                    "cultures": ("table",)}
        for folder in ("roles", "perspectives", "cultures"):
            for path in self._tmdl_files(folder):
                self._edit_text(path, lambda t, f=folder: _rename_scope_lines(
                    _rewrite_dax_regions(t, dax) if f == "roles" else t,
                    scope_kw[f], old, new))
        self._edit_report(_table_rule(old, new))
        return self._result("table", None, old, new)

    # --- helpers ----------------------------------------------------------

    @staticmethod
    def _declared_table(text: str) -> str:
        for line in text.split("\n"):
            m = _RE_TABLE_HDR.match(line)
            if m:
                return _unquote(m.group("name"))
        return ""

    def _result(self, kind: str, table: str | None, old: str, new: str) -> dict:
        files = sorted(set(self.changed))
        return {
            "ok": True,
            "kind": kind,
            "table": table,
            "old": old,
            "new": new,
            "files_changed": files,
            "model_files": [f for f in files if ".SemanticModel/" in f],
            "report_files": [f for f in files if ".Report/" in f],
        }


def find_references(project, kind: str, table: str | None, name: str) -> dict:
    """Where a measure / column / table is referenced (model DAX + report)."""
    from core.dax_parser import parse_references
    from core.usage import collect_direct_refs

    dax_hits: list[dict] = []
    for m in project.list_measures():
        refs = parse_references(m.dax)
        hit = (
            (kind == "measure" and any(_same(r, name) for r in refs.measures))
            or (kind == "column" and any(_same(t, table or "") and _same(c, name)
                                         for t, c in refs.columns))
            or (kind == "table" and (any(_same(t, name) for t in refs.tables)
                                     or any(_same(t, name) for t, _ in refs.columns)))
        )
        if hit:
            dax_hits.append({"table": m.table, "measure": m.name})
    report_hits = []
    try:
        for entity, prop in sorted(collect_direct_refs(project)):
            if kind == "table" and _same(entity, name):
                report_hits.append(f"{entity}.{prop}")
            elif kind in ("measure", "column") and _same(prop, name) \
                    and (table is None or _same(entity, table)):
                report_hits.append(f"{entity}.{prop}")
    except FileNotFoundError:
        pass
    return {"kind": kind, "table": table, "name": name,
            "dax": dax_hits, "report_fields": report_hits}
