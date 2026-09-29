"""DAX formatting and reference tools: pbi_format_dax, pbi_format_measures,
pbi_dax_references.

The formatter is ``core.dax_format`` (lossless and idempotent); measure
rewrites are surgical edits of the measure's expression lines in the table's
TMDL file, so lineageTag, formatString, displayFolder, annotations and every
other measure are left byte for byte as they were.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.bpa import Node, parse_nodes, table_files
from core.dax_format import DaxFormatError, format_dax
from core.dax_parser import parse_references, tokenize

if TYPE_CHECKING:  # pragma: no cover
    from model_server.server import ModelState

_STYLES = ("long", "short")
_TMDL_INDENT = chr(9)


def _check_style(style: str) -> str:
    if style not in _STYLES:
        raise ValueError(f"style must be 'long' or 'short', got {style!r}.")
    return style


def _fingerprint(dax: str) -> tuple[list, list]:
    """Token sequence (IDENT case-folded) and comment texts, ignoring layout."""
    toks = tokenize(dax)
    sig = [(t.kind, t.text.lower() if t.kind == "IDENT" else t.text)
           for t in toks if t.kind not in ("WS", "COMMENT")]
    cmts = [t.text.rstrip() if not t.text.startswith("/*") else t.text
            for t in toks if t.kind == "COMMENT"]
    return sig, cmts


def _safe_format(dax: str, style: str, indent: str = "    ") -> str:
    """format_dax plus a runtime proof that no token or comment changed."""
    out = format_dax(dax, style, indent=indent)
    if _fingerprint(out) != _fingerprint(dax.replace("\r\n", "\n").replace("\r", "\n")):
        raise DaxFormatError("internal safety check failed: formatting would change "
                             "the expression's tokens, so it was left alone")
    return out


def _measures_in_scope(state: ModelState, table: str | None, measure: str | None):
    project = state.require()
    tables = project.list_tables()
    names = {t.name.lower(): t.name for t in tables}
    if table is not None and table.lower() not in names:
        raise ValueError(f"Table {table!r} not found. Tables: "
                         f"{', '.join(sorted(names.values()))}.")
    found = []
    for t in tables:
        if table is not None and t.name.lower() != table.lower():
            continue
        for m in t.measures:
            if measure is not None and m.name.lower() != measure.lower():
                continue
            found.append(m)
    if measure is not None and not found:
        allm = [m.name for t in tables for m in t.measures]
        import difflib
        close = difflib.get_close_matches(measure, allm, n=3, cutoff=0.5)
        where = f" in table {table!r}" if table else ""
        hint = f" Did you mean: {', '.join(close)}?" if close else ""
        raise ValueError(f"Measure {measure!r} not found{where}.{hint}")
    return found


# --- pbi_format_dax ------------------------------------------------------------

def format_dax_text(state: ModelState, dax: str | None = None,
                    table: str | None = None, measure: str | None = None,
                    style: str = "long") -> dict:
    """Format DAX text, or preview the formatting of model measures (no writes)."""
    _check_style(style)
    if dax is not None:
        if measure is not None or table is not None:
            raise ValueError("Pass either `dax` or `table`/`measure`, not both.")
        try:
            out = _safe_format(dax, style)
        except DaxFormatError as exc:
            raise ValueError(str(exc)) from None
        return {"source": "dax", "style": style, "formatted": out,
                "changed": out != dax.strip()}
    if table is None and measure is None:
        raise ValueError("Pass `dax` to format text, or `measure` (and optionally "
                         "`table`) to preview a model measure's formatting.")
    found = _measures_in_scope(state, table, measure)
    if measure is not None:
        if len(found) > 1:
            raise ValueError(f"Measure {measure!r} is ambiguous: found in tables "
                             f"{', '.join(m.table for m in found)}.")
        m = found[0]
        try:
            out = _safe_format(m.dax, style, indent=_TMDL_INDENT)
        except DaxFormatError as exc:
            raise ValueError(f"{m.table}[{m.name}]: {exc}") from None
        return {"source": f"{m.table}[{m.name}]", "table": m.table, "name": m.name,
                "style": style, "original": m.dax, "formatted": out,
                "changed": out != m.dax.strip()}
    items = []
    for m in found:
        item = {"table": m.table, "name": m.name}
        try:
            out = _safe_format(m.dax, style, indent=_TMDL_INDENT)
            item.update(formatted=out, changed=out != m.dax.strip())
        except DaxFormatError as exc:
            item.update(error=str(exc))
        items.append(item)
    return {"source": f"table {table}", "style": style, "measures": items,
            "changed_count": sum(1 for i in items if i.get("changed"))}


# --- pbi_format_measures -----------------------------------------------------------

def _replacement_lines(node: Node, formatted: str) -> list[str]:
    """New header + expression lines for a measure node."""
    tabs = "\t" * node.indent
    head = f"{tabs}measure {node.raw_name}"
    body_indent = "\t" * (node.indent + 2)
    if "\n" not in formatted:
        return [f"{head} = {formatted}"]
    body = [body_indent + ln for ln in formatted.split("\n")]
    if node.fenced or node.inline:                 # fenced, or was one line
        return [f"{head} = ```", *body, body_indent + "```"]
    return [f"{head} =", *body]                    # plain multi-line body


def _same_layout(node: Node, formatted: str) -> bool:
    return formatted == node.expr.replace("\r\n", "\n").strip()


def format_measures(state: ModelState, table: str | None = None,
                    measure: str | None = None, style: str = "long") -> dict:
    """Rewrite measure expressions in place with the formatter."""
    _check_style(style)
    project = state.require()
    targets = _measures_in_scope(state, table, measure)
    by_table: dict[str, list[str]] = {}
    for m in targets:
        by_table.setdefault(m.table, []).append(m.name)

    changed: list[dict] = []
    skipped: list[dict] = []
    unchanged = 0
    files = table_files(project)
    for tname, names in by_table.items():
        path = files.get(tname)
        if path is None:
            skipped.extend({"table": tname, "name": n, "reason": "table file not found"}
                           for n in names)
            continue
        text = path.read_text(encoding="utf-8-sig")
        nodes = [n for n in parse_nodes(text) if n.kw == "table"]
        tn = next((n for n in nodes if n.name == tname), None)
        if tn is None:
            continue
        edits: list[tuple[int, int, list[str], str]] = []
        for name in names:
            node = next((c for c in tn.kids("measure") if c.name == name), None)
            if node is None:
                skipped.append({"table": tname, "name": name,
                                "reason": "measure block not found in the table file"})
                continue
            if not node.expr.strip():
                skipped.append({"table": tname, "name": name, "reason": "empty expression"})
                continue
            try:
                out = _safe_format(node.expr, style, indent=_TMDL_INDENT)   # TMDL nests with tabs
            except DaxFormatError as exc:
                skipped.append({"table": tname, "name": name, "reason": str(exc)})
                continue
            if _same_layout(node, out):
                unchanged += 1
                continue
            edits.append((node.start, node.body_end, _replacement_lines(node, out), name))
        if not edits:
            continue
        lines = text.split("\n")
        for start, end, new_lines, _ in sorted(edits, key=lambda e: e[0], reverse=True):
            lines[start:end] = new_lines
        project._write_text(path, "\n".join(lines))
        changed.extend({"table": tname, "name": e[3]} for e in sorted(edits, key=lambda e: e[0]))
    return {"style": style, "count": len(changed), "changed": changed,
            "unchanged": unchanged, "skipped": skipped}


# --- pbi_dax_references ----------------------------------------------------------------

def dax_references(state: ModelState, dax: str, resolve_against_model: bool = True) -> dict:
    """References (measures, columns, tables, functions, variables) in DAX."""
    if not isinstance(dax, str) or not dax.strip():
        raise ValueError("`dax` must be a non-empty DAX expression.")
    project = getattr(state, "project", None) if resolve_against_model else None
    raw = parse_references(dax)
    result: dict = {
        "resolved_against_model": project is not None,
        "measures": sorted(raw.measures),
        "columns": [{"table": t, "column": c} for t, c in sorted(raw.columns)],
        "tables": sorted(raw.tables),
        "functions": sorted(raw.functions),
        "variables": sorted(raw.variables),
        "unqualified": [],
    }
    if project is None:
        return result

    tables = project.list_tables()
    measures = {m.name.lower(): m.name for t in tables for m in t.measures}
    cols: dict[str, list[str]] = {}
    for t in tables:
        for c in t.columns:
            cols.setdefault(c.name.lower(), []).append(t.name)
    real_measures = sorted({measures[n.lower()] for n in raw.measures if n.lower() in measures})
    bare = [n for n in raw.measures if n.lower() not in measures]
    result["measures"] = real_measures
    result["unqualified"] = sorted(bare)
    result["unqualified_matches"] = {n: sorted(cols.get(n.lower(), [])) for n in sorted(bare)}
    table_names = {t.name.lower(): t.name for t in tables}
    annotated = []
    for t, c in sorted(raw.columns):
        tbl = table_names.get(t.lower())
        is_col = tbl is not None and any(
            x.name.lower() == c.lower() for x in next(
                tb for tb in tables if tb.name == tbl).columns)
        kind = "column" if is_col else ("measure" if c.lower() in measures else "unknown")
        annotated.append({"table": tbl or t, "column": c, "kind": kind})
    result["columns"] = annotated
    result["tables"] = sorted(table_names.get(x.lower(), x) for x in raw.tables)
    return result


# --- MCP registration ---------------------------------------------------------------------

def register(mcp, state, tool) -> None:
    @tool(read=True)
    def pbi_format_dax(dax: str | None = None, table: str | None = None,
                       measure: str | None = None, style: str = "long") -> dict:
        """Format DAX (read-only). Pass `dax` to format text, or `measure`
        (optionally with `table`) to preview how a model measure would be
        formatted; `table` alone previews every measure of that table.

        Long style (DAX Formatter-like): keywords and function names upper-cased,
        one argument per line when a call is long or contains nested calls,
        VAR / RETURN on their own lines with the body indented, spaced
        operators, commas at line ends, comments kept in place. Short style: one
        line when the whole expression fits in 100 characters, else long.
        Table, column and measure names, strings and comments are never changed;
        formatting is idempotent."""
        return format_dax_text(state, dax, table, measure, style)

    @tool(write=True, destructive=False, idempotent=True)
    def pbi_format_measures(table: str | None = None, measure: str | None = None,
                            style: str = "long") -> dict:
        """Reformat measure DAX in place (all measures, one table, or one
        measure) and report which measures changed. Only the expression lines
        are rewritten; format strings, folders, lineage tags and annotations are
        untouched, and already-formatted measures are skipped, so re-running is
        a no-op. Measures the formatter cannot handle safely are listed under
        `skipped` and left as they were."""
        return format_measures(state, table, measure, style)

    @tool(read=True)
    def pbi_dax_references(dax: str, resolve_against_model: bool = True) -> dict:
        """Parse a DAX expression and list what it references: measures,
        qualified columns (Table[Col]), tables, functions, VAR variables and bare
        [Name] references that are not measures ('unqualified', likely columns
        read in row context). With a project selected, bare references are
        resolved against the model's measure names and columns are marked
        column / measure / unknown. Comments and strings are never references."""
        return dax_references(state, dax, resolve_against_model)
