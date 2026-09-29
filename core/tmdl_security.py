"""TMDL read/write for roles (RLS/OLS), perspectives and cultures.

These objects live in their own folders next to ``tables/`` and are
registered in ``model.tmdl`` with ``ref <kind> <name>`` lines::

    roles/<Name>.tmdl           role <Name>
    perspectives/<Name>.tmdl    perspective <Name>
    cultures/<code>.tmdl        cultureInfo <code>

Shapes were checked against the official TMDL reference
(learn.microsoft.com/analysis-services/tmdl) and Desktop-emitted PBIP files
(Microsoft's ``pbidevmode`` sample, ``fabric-toolbox``, Tabular Editor
exports)::

    /// Description
    role 'Sales Managers'
        modelPermission: read

        tablePermission Sales = 'Sales'[Region] = "West"
            metadataPermission: none          <- OLS: hide the whole table
            columnPermission Cost = none      <- OLS: hide one column

    perspective 'Sales View'

        perspectiveTable Sales
            perspectiveColumn Amount
            perspectiveMeasure 'Net Revenue'
            perspectiveHierarchy 'Date Hierarchy'

    cultureInfo de-DE

        translations
            model Model
                table Sales
                    caption: Verkauf
                    column Amount
                        caption: Betrag

        linguisticMetadata =
                {"Version": "1.0.0", "Language": "de-DE"}   (pretty-printed)
            contentType: json

Desktop adds ``annotation PBI_Id = <32 hex>`` to roles it creates; Microsoft's
authoring guidance says roles authored outside Desktop must NOT carry it, so
none is emitted here.

Every edit is a surgical text edit on the raw file text (never a re-emit of a
parsed model), in the style of core/tmdl.py: only the touched block changes.
"""

from __future__ import annotations

import json
import re

from core.tmdl import _leading_tabs, _unquote, quote_ident

_FENCE = "```"

#: model.tmdl `ref` groups, in emission order. The deserializer only uses the
#: order *within* a kind (collection order); the docs' own example lists
#: tables, cultures, then roles, and TOM's collection order puts perspectives
#: between cultures and roles, so that is the order used for new groups.
REF_ORDER = ("table", "cultureInfo", "perspective", "role")

MODEL_PERMISSIONS = ("none", "read", "readRefresh", "refresh", "administrator")
METADATA_PERMISSIONS = ("none", "read", "default")

_INVALID_FILE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_file_stem(name: str) -> str:
    """Object name -> file stem; characters Windows forbids become ``_``."""
    stem = _INVALID_FILE_CHARS.sub("_", name).strip().rstrip(".")
    return stem or "_"


# --- small text helpers -----------------------------------------------------

def _split_lines(text: str) -> list[str]:
    return text.split("\n")


def _join(lines: list[str]) -> str:
    return "\n".join(lines)


def _strip_trailing_blank(lines: list[str]) -> int:
    n = 0
    while lines and not lines[-1].strip():
        lines.pop()
        n += 1
    return n


def _block_end(lines: list[str], header_idx: int, indent: int) -> int:
    """Exclusive end of the block whose header is at ``header_idx``.

    The block is every following line indented deeper than ``indent``
    (interior blank lines included); trailing blank lines stay outside.
    """
    end = header_idx + 1
    last = header_idx
    while end < len(lines):
        s = lines[end]
        if not s.strip():
            end += 1
            continue
        if _leading_tabs(s) <= indent:
            break
        last = end
        end += 1
    return last + 1


def _split_default(rest: str) -> tuple[str, str | None]:
    """``Name = expr`` -> (raw name token, expr). Honours single-quoted names.

    ``expr`` is None when there is no ``=`` and "" for a bare ``=`` (the
    expression continues on the following lines).
    """
    rest = rest.strip()
    if rest.startswith("'"):
        i = 1
        while i < len(rest):
            if rest[i] == "'":
                if i + 1 < len(rest) and rest[i + 1] == "'":
                    i += 2
                    continue
                break
            i += 1
        name = rest[:i + 1]
        tail = rest[i + 1:].strip()
        if tail.startswith("="):
            return name, tail[1:].strip()
        return name, None
    name, sep, tail = rest.partition("=")
    if not sep:
        return name.strip(), None
    return name.strip(), tail.strip()


def _header_name(line: str, keyword: str) -> str | None:
    """``keyword Name [= ...]`` -> unquoted Name, or None if not that header."""
    s = line.strip()
    if not s.startswith(keyword + " "):
        return None
    return _unquote(_split_default(s[len(keyword) + 1:])[0])


def _find_header(lines: list[str], keyword: str, name: str, indent: int,
                 start: int = 0, end: int | None = None) -> int | None:
    end = len(lines) if end is None else end
    for idx in range(start, end):
        if _leading_tabs(lines[idx]) == indent and \
                _header_name(lines[idx], keyword) == name:
            return idx
    return None


def _top_header(lines: list[str], keyword: str) -> int:
    for idx, ln in enumerate(lines):
        if _leading_tabs(ln) == 0 and ln.strip().startswith(keyword + " "):
            return idx
    raise ValueError(f"No `{keyword}` declaration found")


def declared_name(text: str, keyword: str) -> str | None:
    """Name declared by the file's top-level ``keyword`` line, if any."""
    lines = _split_lines(text)
    try:
        return _header_name(lines[_top_header(lines, keyword)], keyword)
    except ValueError:
        return None


def _description_range(lines: list[str], header_idx: int) -> tuple[int, int]:
    """[start, end) of the ``///`` lines directly above a header."""
    indent = _leading_tabs(lines[header_idx])
    start = header_idx
    while start > 0 and lines[start - 1].strip().startswith("///") \
            and _leading_tabs(lines[start - 1]) == indent:
        start -= 1
    return start, header_idx


def description_lines(description: str | None, indent: int = 0) -> list[str]:
    """``///`` lines for a description (multi-line descriptions supported)."""
    if not description or not description.strip():
        return []
    tabs = "\t" * indent
    return [f"{tabs}/// {ln}".rstrip() for ln in description.strip().splitlines()]


def _read_description(lines: list[str], header_idx: int) -> str | None:
    start, end = _description_range(lines, header_idx)
    if start == end:
        return None
    return "\n".join(lines[i].strip()[3:].strip() for i in range(start, end))


def _set_description(lines: list[str], header: int, description: str | None) -> int:
    """Replace the description above ``header``; returns the new header index."""
    start, end = _description_range(lines, header)
    new = description_lines(description, _leading_tabs(lines[header]))
    lines[start:end] = new
    return start + len(new)


def _expression_lines(prefix: str, expr: str, indent: int) -> list[str]:
    """``prefix = expr`` on one line, or the body two tabs deeper than
    ``indent`` (the level Desktop uses for multi-line expressions)."""
    expr = expr.strip("\n")
    if "\n" not in expr:
        return [f"{prefix} = {expr.strip()}"]
    body = "\t" * (indent + 2)
    return [f"{prefix} ="] + [body + ln if ln.strip() else "" for ln in expr.split("\n")]


def _dedent_expr(raw: list[str]) -> str:
    tabbed = [ln for ln in raw if ln.strip()]
    if not tabbed:
        return ""
    base = min(_leading_tabs(ln) for ln in tabbed)
    return "\n".join(ln[base:] if ln.strip() else "" for ln in raw).strip("\n")


def _insert_block(lines: list[str], header: int, prefixes: tuple[str, ...],
                  block: list[str], after_props: tuple[str, ...] = ()) -> None:
    """Insert ``block`` (preceded by a blank line) as a child of ``header``.

    It goes after the last existing indent-1 child starting with one of
    ``prefixes``; otherwise after the last property in ``after_props``;
    otherwise right below the header. Members / annotations that follow stay
    after it.
    """
    end = _block_end(lines, header, 0)
    at = None
    for idx in range(header + 1, end):
        if _leading_tabs(lines[idx]) != 1:
            continue
        s = lines[idx].strip()
        if s.startswith(prefixes):
            at = _block_end(lines, idx, 1)
        elif at is None and s.startswith(after_props):
            at = idx + 1
    if at is None:
        at = header + 1
    lines[at:at] = [""] + block


# --- model.tmdl refs --------------------------------------------------------

def _parse_ref(line: str) -> tuple[str, str] | None:
    """``ref <kind> <name>`` -> (normalised kind, name); None for other lines.

    ``ref culture X`` (docs spelling) and ``ref cultureInfo X`` (Desktop's)
    both normalise to ``cultureInfo``.
    """
    parts = line.strip().split(None, 2)
    if len(parts) < 3 or parts[0] != "ref":
        return None
    kind = "cultureInfo" if parts[1] == "culture" else parts[1]
    if kind not in REF_ORDER:
        return None
    return kind, _unquote(_split_default(parts[2])[0])


def list_refs_text(model_text: str, kind: str) -> list[str]:
    """Names referenced by ``ref <kind> ...`` lines in model.tmdl text."""
    out = []
    for ln in _split_lines(model_text):
        r = _parse_ref(ln)
        if r is not None and r[0] == kind:
            out.append(r[1])
    return out


def add_ref_text(model_text: str, kind: str, name: str) -> str:
    """Add ``ref <kind> <name>`` to model.tmdl text, inside its group.

    Same-kind refs stay contiguous; a new group is separated by one blank
    line and placed per REF_ORDER (after the ``ref table`` lines). Adding a
    ref that already exists is a no-op.
    """
    if kind not in REF_ORDER:
        raise ValueError(f"Unknown ref kind {kind!r}")
    lines = _split_lines(model_text)
    positions: dict[str, list[int]] = {}
    spelling = "cultureInfo"
    for idx, ln in enumerate(lines):
        r = _parse_ref(ln)
        if r is None:
            continue
        if r[0] == kind and r[1] == name:
            return model_text
        positions.setdefault(r[0], []).append(idx)
        if r[0] == "cultureInfo":
            spelling = ln.strip().split(None, 2)[1]   # follow the file's own spelling
    word = spelling if kind == "cultureInfo" else kind
    # Desktop writes culture codes bare (`ref cultureInfo en-US`); a hyphen
    # does not need TMDL quoting, only . = : ' and whitespace do.
    bare = kind == "cultureInfo" and re.fullmatch(r"[A-Za-z0-9-]+", name)
    ref_line = f"ref {word} {name if bare else quote_ident(name)}"

    def lead(idx: int) -> str:
        # Desktop writes refs at column 0, hand-made models sometimes indent
        # them; a new line copies the indentation of the neighbour it follows.
        return lines[idx][:len(lines[idx]) - len(lines[idx].lstrip())]

    if kind in positions:
        at = positions[kind][-1]
        lines.insert(at + 1, lead(at) + ref_line)
        return _join(lines)
    order = REF_ORDER.index(kind)
    earlier = [positions[k][-1] for k in REF_ORDER[:order] if k in positions]
    later = [positions[k][0] for k in REF_ORDER[order + 1:] if k in positions]
    if earlier:
        anchor = max(earlier)
        lines[anchor + 1:anchor + 1] = ["", lead(anchor) + ref_line]
    elif later:
        anchor = min(later)
        lines[anchor:anchor] = [lead(anchor) + ref_line, ""]
    else:
        trailing = _strip_trailing_blank(lines)
        lines += ["", ref_line] + [""] * max(trailing, 1)
    return _join(lines)


def remove_ref_text(model_text: str, kind: str, name: str) -> str:
    """Remove ``ref <kind> <name>`` from model.tmdl text (no-op if absent).

    When that empties the group, the blank separator goes with it so the file
    does not keep a double blank line.
    """
    lines = _split_lines(model_text)
    for i, ln in enumerate(lines):
        r = _parse_ref(ln)
        if r == (kind, name):
            del lines[i]
            if i > 0 and not lines[i - 1].strip() and \
                    (i >= len(lines) or not lines[i].strip()):
                del lines[i - 1]   # the group is gone: keep one blank, not two
            return _join(lines)
    return model_text


# --- roles ------------------------------------------------------------------

def validate_model_permission(p: str) -> str:
    if p not in MODEL_PERMISSIONS:
        raise ValueError(
            f"model_permission must be one of {list(MODEL_PERMISSIONS)}, got {p!r}")
    return p


def validate_metadata_permission(p: str) -> str:
    if p not in METADATA_PERMISSIONS:
        raise ValueError(
            f"permission must be one of {list(METADATA_PERMISSIONS)}, got {p!r}")
    return p


def _table_permission_block(table: str, dax: str | None,
                            metadata_permission: str | None = None,
                            columns: dict[str, str] | None = None) -> list[str]:
    head = f"\ttablePermission {quote_ident(table)}"
    lines = _expression_lines(head, dax, 1) if dax and dax.strip() else [head]
    if metadata_permission and metadata_permission != "default":
        lines.append(f"\t\tmetadataPermission: {metadata_permission}")
    for col, perm in (columns or {}).items():
        if perm != "default":
            lines.append(f"\t\tcolumnPermission {quote_ident(col)} = {perm}")
    return lines


def emit_role_text(name: str, description: str | None = None,
                   model_permission: str = "read",
                   table_filters: dict[str, str] | None = None) -> str:
    """Render a complete ``roles/<Name>.tmdl`` file."""
    validate_model_permission(model_permission)
    out = description_lines(description, 0)
    out.append(f"role {quote_ident(name)}")
    out.append(f"\tmodelPermission: {model_permission}")
    for table, dax in (table_filters or {}).items():
        if not str(dax).strip():
            raise ValueError(f"Empty filter expression for table {table!r}")
        out.append("")
        out.extend(_table_permission_block(table, str(dax)))
    out.append("")
    return _join(out)


def _tp_layout(lines: list[str], start: int, end: int) -> tuple[str | None, int]:
    """Where a tablePermission's filter ends and its properties begin.

    Returns ``(filter expression or None, index of the first property line)``.
    Handles the inline, indented multi-line and fenced (```) forms.
    """
    _, inline = _split_default(lines[start].strip()[len("tablePermission "):])
    if inline and inline.startswith(_FENCE):
        body: list[str] = []
        i = start + 1
        while i < end and lines[i].strip() != _FENCE:
            body.append(lines[i])
            i += 1
        return _dedent_expr(body), min(i + 1, end)
    if inline:
        return inline, start + 1
    i = start + 1
    body = []
    while i < end and (not lines[i].strip() or _leading_tabs(lines[i]) >= 3):
        body.append(lines[i])
        i += 1
    expr = _dedent_expr(body)
    return (expr or None), i


def _parse_table_permission(lines: list[str], i: int) -> tuple[str, dict, int]:
    name, _ = _split_default(lines[i].strip()[len("tablePermission "):])
    table = _unquote(name)
    end = _block_end(lines, i, 1)
    filt, props_at = _tp_layout(lines, i, end)
    info = {"filter": filt, "metadata_permission": None, "columns": {}}
    for j in range(props_at, end):
        ln = lines[j]
        s = ln.strip()
        if _leading_tabs(ln) != 2 or not s:
            continue
        if s.startswith("metadataPermission:"):
            info["metadata_permission"] = s.split(":", 1)[1].strip()
        elif s.startswith("columnPermission "):
            cname, perm = _split_default(s[len("columnPermission "):])
            info["columns"][_unquote(cname)] = (perm or "").strip()
    return table, info, end


def parse_role_text(text: str) -> dict:
    """Parse a role file into a plain dict.

    ``{"name", "description", "model_permission", "tables": {table:
    {"filter", "metadata_permission", "columns": {col: perm}}}, "members":
    [...], "annotations": {name: value}}``
    """
    lines = [ln.rstrip("\r") for ln in _split_lines(text)]
    header = _top_header(lines, "role")
    role = {
        "name": _header_name(lines[header], "role"),
        "description": _read_description(lines, header),
        "model_permission": None,
        "tables": {},
        "members": [],
        "annotations": {},
    }
    end = _block_end(lines, header, 0)
    i = header + 1
    while i < end:
        ln = lines[i]
        s = ln.strip()
        if not s or _leading_tabs(ln) != 1:
            i += 1
            continue
        if s.startswith("modelPermission:"):
            role["model_permission"] = s.split(":", 1)[1].strip()
            i += 1
        elif s.startswith("tablePermission "):
            tbl, info, tend = _parse_table_permission(lines, i)
            role["tables"][tbl] = info
            i = tend
        elif s.startswith("member "):
            role["members"].append(_header_name(ln, "member"))
            i = _block_end(lines, i, 1)
        elif s.startswith("annotation "):
            n, _, v = s[len("annotation "):].partition("=")
            role["annotations"][n.strip()] = v.strip()
            i = _block_end(lines, i, 1)
        else:
            i += 1
    return role


def set_role_description_text(text: str, description: str | None) -> str:
    """Replace (or, with ``""``, remove) the ``///`` description of a role."""
    lines = _split_lines(text)
    _set_description(lines, _top_header(lines, "role"), description)
    return _join(lines)


def set_model_permission_text(text: str, permission: str) -> str:
    validate_model_permission(permission)
    lines = _split_lines(text)
    header = _top_header(lines, "role")
    end = _block_end(lines, header, 0)
    for idx in range(header + 1, end):
        if _leading_tabs(lines[idx]) == 1 and \
                lines[idx].strip().startswith("modelPermission:"):
            lines[idx] = f"\tmodelPermission: {permission}"
            return _join(lines)
    lines.insert(header + 1, f"\tmodelPermission: {permission}")
    return _join(lines)


def _table_permission_span(lines: list[str], table: str) -> tuple[int, int] | None:
    header = _top_header(lines, "role")
    end = _block_end(lines, header, 0)
    idx = _find_header(lines, "tablePermission", table, 1, header + 1, end)
    if idx is None:
        return None
    return idx, _block_end(lines, idx, 1)


def _insert_table_permission(lines: list[str], block: list[str]) -> None:
    _insert_block(lines, _top_header(lines, "role"), ("tablePermission ",),
                  block, after_props=("modelPermission:",))


def _drop_if_empty(lines: list[str], table: str) -> None:
    """Remove a tablePermission block left with neither filter nor OLS."""
    span = _table_permission_span(lines, table)
    if span is None:
        return
    start, end = span
    filt, props_at = _tp_layout(lines, start, end)
    if filt or any(ln.strip() for ln in lines[props_at:end]):
        return
    if start > 0 and not lines[start - 1].strip():
        start -= 1
    del lines[start:end]


def upsert_table_permission_text(text: str, table: str, dax: str | None) -> str:
    """Set (or, with None/"", clear) the row filter of one table in a role.

    ``metadataPermission`` / ``columnPermission`` lines under that table are
    preserved verbatim; only the filter expression changes. Clearing the
    filter drops the block entirely when nothing else remains in it.
    """
    lines = _split_lines(text)
    dax = (dax or "").strip("\n")
    span = _table_permission_span(lines, table)
    if span is None:
        if not dax.strip():
            raise ValueError(f"Table {table!r} has no permission block in this role")
        _insert_table_permission(lines, _table_permission_block(table, dax))
        return _join(lines)
    start, end = span
    _, props_at = _tp_layout(lines, start, end)
    props = lines[props_at:end]
    head = f"\ttablePermission {quote_ident(table)}"
    new = _expression_lines(head, dax, 1) if dax.strip() else [head]
    lines[start:end] = new + props
    if not dax.strip():
        _drop_if_empty(lines, table)
    return _join(lines)


def _set_block_property(lines: list[str], start: int, end: int,
                        matcher, new_line: str | None) -> None:
    """Replace / insert / remove one indent-2 property line in a block."""
    _, props_at = _tp_layout(lines, start, end)
    for idx in range(props_at, end):
        if _leading_tabs(lines[idx]) == 2 and matcher(lines[idx].strip()):
            if new_line is None:
                del lines[idx]
            else:
                lines[idx] = new_line
            return
    if new_line is not None:
        lines.insert(end, new_line)


def set_column_permission_text(text: str, table: str, column: str,
                               permission: str) -> str:
    """OLS: ``columnPermission <col> = none|read``; ``default`` removes it.

    Creates a filter-less ``tablePermission`` block when the table has none.
    """
    validate_metadata_permission(permission)
    lines = _split_lines(text)
    span = _table_permission_span(lines, table)
    if span is None:
        if permission == "default":
            return text
        _insert_table_permission(
            lines, _table_permission_block(table, None, None, {column: permission}))
        return _join(lines)
    start, end = span
    new = (None if permission == "default"
           else f"\t\tcolumnPermission {quote_ident(column)} = {permission}")
    _set_block_property(
        lines, start, end,
        lambda s: s.startswith("columnPermission ")
        and _unquote(_split_default(s[len("columnPermission "):])[0]) == column,
        new)
    _drop_if_empty(lines, table)
    return _join(lines)


def set_table_metadata_permission_text(text: str, table: str,
                                       permission: str) -> str:
    """OLS: ``metadataPermission: none|read`` on a table; ``default`` removes it."""
    validate_metadata_permission(permission)
    lines = _split_lines(text)
    span = _table_permission_span(lines, table)
    if span is None:
        if permission == "default":
            return text
        _insert_table_permission(
            lines, _table_permission_block(table, None, permission))
        return _join(lines)
    start, end = span
    new = None if permission == "default" else f"\t\tmetadataPermission: {permission}"
    _set_block_property(lines, start, end,
                        lambda s: s.startswith("metadataPermission:"), new)
    _drop_if_empty(lines, table)
    return _join(lines)


# --- perspectives -----------------------------------------------------------

PERSPECTIVE_KEYS = ("columns", "measures", "hierarchies")
_PERSPECTIVE_KW = {"columns": "perspectiveColumn", "measures": "perspectiveMeasure",
                   "hierarchies": "perspectiveHierarchy"}


def _perspective_child_lines(spec: dict) -> list[str]:
    out = []
    if spec.get("include_all"):
        out.append("\t\tincludeAll")
    for key in PERSPECTIVE_KEYS:
        for obj in spec.get(key) or []:
            out.append(f"\t\t{_PERSPECTIVE_KW[key]} {quote_ident(obj)}")
    return out


def emit_perspective_text(name: str, description: str | None = None,
                          tables: dict[str, dict] | None = None) -> str:
    """Render ``perspectives/<Name>.tmdl``.

    ``tables``: ``{table: {"include_all": bool, "columns": [...],
    "measures": [...], "hierarchies": [...]}}``.
    """
    out = description_lines(description, 0)
    out.append(f"perspective {quote_ident(name)}")
    for table, spec in (tables or {}).items():
        out.append("")
        out.append(f"\tperspectiveTable {quote_ident(table)}")
        out.extend(_perspective_child_lines(spec or {}))
    out.append("")
    return _join(out)


def parse_perspective_text(text: str) -> dict:
    """``{"name", "description", "tables": {table: {"include_all", "columns",
    "measures", "hierarchies"}}}``"""
    lines = [ln.rstrip("\r") for ln in _split_lines(text)]
    header = _top_header(lines, "perspective")
    out = {"name": _header_name(lines[header], "perspective"),
           "description": _read_description(lines, header), "tables": {}}
    end = _block_end(lines, header, 0)
    i = header + 1
    while i < end:
        ln = lines[i]
        if _leading_tabs(ln) == 1 and ln.strip().startswith("perspectiveTable "):
            tname = _header_name(ln, "perspectiveTable")
            spec = {"include_all": False, "columns": [], "measures": [],
                    "hierarchies": []}
            tend = _block_end(lines, i, 1)
            for j in range(i + 1, tend):
                t = lines[j].strip()
                if _leading_tabs(lines[j]) != 2 or not t:
                    continue
                if t == "includeAll" or t.startswith("includeAll:"):
                    spec["include_all"] = not t.endswith("false")
                for key, kw in _PERSPECTIVE_KW.items():
                    if t.startswith(kw + " "):
                        spec[key].append(_header_name(lines[j], kw))
            out["tables"][tname] = spec
            i = tend
        else:
            i += 1
    return out


def set_perspective_description_text(text: str, description: str | None) -> str:
    lines = _split_lines(text)
    _set_description(lines, _top_header(lines, "perspective"), description)
    return _join(lines)


def _child_key(line: str) -> tuple[str, str] | None:
    s = line.strip()
    if s == "includeAll":
        return ("includeAll", "")
    for kw in _PERSPECTIVE_KW.values():
        if s.startswith(kw + " "):
            return (kw, _header_name(line, kw))
    return None


def add_perspective_objects_text(text: str, table: str, spec: dict) -> str:
    """Add columns / measures / hierarchies (or ``include_all``) for a table.

    Objects already listed are left alone; a missing ``perspectiveTable``
    block is created after the last one.
    """
    lines = _split_lines(text)
    header = _top_header(lines, "perspective")
    end = _block_end(lines, header, 0)
    wanted = _perspective_child_lines(spec)
    tidx = _find_header(lines, "perspectiveTable", table, 1, header + 1, end)
    if tidx is None:
        _insert_block(lines, header, ("perspectiveTable ",),
                      [f"\tperspectiveTable {quote_ident(table)}"] + wanted)
        return _join(lines)
    t_end = _block_end(lines, tidx, 1)
    have = {_child_key(lines[j]) for j in range(tidx + 1, t_end)
            if _leading_tabs(lines[j]) == 2}
    fresh = [ln for ln in wanted if _child_key(ln) not in have]
    lines[t_end:t_end] = fresh
    return _join(lines)


def remove_perspective_objects_text(text: str, table: str, spec: dict) -> str:
    """Remove the listed objects from a table; an emptied table entry goes too."""
    lines = _split_lines(text)
    header = _top_header(lines, "perspective")
    end = _block_end(lines, header, 0)
    tidx = _find_header(lines, "perspectiveTable", table, 1, header + 1, end)
    if tidx is None:
        raise ValueError(f"Table {table!r} is not part of this perspective")
    drop = {_child_key(ln) for ln in _perspective_child_lines(spec)}
    t_end = _block_end(lines, tidx, 1)
    keep = [lines[j] for j in range(tidx + 1, t_end)
            if not (_leading_tabs(lines[j]) == 2 and _child_key(lines[j]) in drop)]
    lines[tidx + 1:t_end] = keep
    t_end = _block_end(lines, tidx, 1)
    if not any(ln.strip() for ln in lines[tidx + 1:t_end]):
        return remove_perspective_table_text(_join(lines), table)
    return _join(lines)


def remove_perspective_table_text(text: str, table: str) -> str:
    """Drop a table's whole ``perspectiveTable`` block."""
    lines = _split_lines(text)
    header = _top_header(lines, "perspective")
    end = _block_end(lines, header, 0)
    tidx = _find_header(lines, "perspectiveTable", table, 1, header + 1, end)
    if tidx is None:
        raise ValueError(f"Table {table!r} is not part of this perspective")
    start, stop = tidx, _block_end(lines, tidx, 1)
    if start > 0 and not lines[start - 1].strip():
        start -= 1
    del lines[start:stop]
    return _join(lines)


# --- cultures / translations ------------------------------------------------

_CULTURE_CODE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")

TRANSLATION_OBJECTS = ("table", "column", "measure", "hierarchy")
TRANSLATION_PROPS = ("caption", "description", "displayFolder")
_CHILD_KW = (("column", "columns"), ("measure", "measures"),
             ("hierarchy", "hierarchies"))


def validate_culture_code(code: str) -> str:
    code = (code or "").strip()
    if not _CULTURE_CODE.match(code):
        raise ValueError(
            f"Culture code {code!r} is not a BCP-47 tag like 'de-DE' or 'en-US'")
    return code


def emit_culture_text(code: str) -> str:
    """Render ``cultures/<code>.tmdl`` the way Desktop starts a culture: the
    header plus a minimal ``linguisticMetadata`` payload (JSON, ``contentType:
    json``). Translations are added afterwards by ``set_translation_text``."""
    code = validate_culture_code(code)
    payload = json.dumps({"Version": "1.0.0", "Language": code}, indent=2)
    out = [f"cultureInfo {code}", "", "\tlinguisticMetadata ="]
    out += ["\t\t\t" + ln for ln in payload.split("\n")]
    out += ["\t\tcontentType: json", ""]
    return _join(out)


def _read_prop_value(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v.startswith('"') and v.endswith('"'):
        return v[1:-1].replace('""', '"')
    return v


def _prop_value(value: str) -> str:
    """Quote a translation value only when TMDL needs it."""
    if "\n" in value or "\r" in value:
        raise ValueError("Translation values must be single-line")
    if value != value.strip() or value.startswith('"'):
        return '"' + value.replace('"', '""') + '"'
    return value


def _translations_span(lines: list[str], header: int) -> tuple[int, int] | None:
    end = _block_end(lines, header, 0)
    for idx in range(header + 1, end):
        if _leading_tabs(lines[idx]) == 1 and lines[idx].strip() == "translations":
            return idx, _block_end(lines, idx, 1)
    return None


def parse_culture_text(text: str) -> dict:
    """``{"code", "model": {prop: value}, "tables": {table: {"caption"?,
    "description"?, "columns": {name: props}, "measures": {...},
    "hierarchies": {...}}}, "has_linguistic_metadata": bool}``"""
    lines = [ln.rstrip("\r") for ln in _split_lines(text)]
    header = _top_header(lines, "cultureInfo")
    out = {"code": lines[header].strip()[len("cultureInfo "):].strip(),
           "model": {}, "tables": {},
           "has_linguistic_metadata": any(
               _leading_tabs(ln) == 1 and ln.strip().startswith("linguisticMetadata")
               for ln in lines)}
    span = _translations_span(lines, header)
    if span is None:
        return out
    tr, tr_end = span
    model = next((i for i in range(tr + 1, tr_end)
                  if _leading_tabs(lines[i]) == 2
                  and lines[i].strip().startswith("model ")), None)
    if model is None:
        return out
    m_end = _block_end(lines, model, 2)
    i = model + 1
    while i < m_end:
        ln = lines[i]
        s = ln.strip()
        if _leading_tabs(ln) == 3 and s.startswith("table "):
            tname = _header_name(ln, "table")
            t_end = _block_end(lines, i, 3)
            tinfo: dict = {"columns": {}, "measures": {}, "hierarchies": {}}
            j = i + 1
            while j < t_end:
                lj, sj = lines[j], lines[j].strip()
                if _leading_tabs(lj) == 4 and sj:
                    kind = next(((kw, key) for kw, key in _CHILD_KW
                                 if sj.startswith(kw + " ")), None)
                    if kind is not None:
                        o_end = _block_end(lines, j, 4)
                        props = {}
                        for q in range(j + 1, o_end):
                            sq = lines[q].strip()
                            if _leading_tabs(lines[q]) == 5 and ":" in sq:
                                k, _, v = sq.partition(":")
                                props[k.strip()] = _read_prop_value(v)
                        tinfo[kind[1]][_header_name(lj, kind[0])] = props
                        j = o_end
                        continue
                    if ":" in sj:
                        k, _, v = sj.partition(":")
                        tinfo[k.strip()] = _read_prop_value(v)
                j += 1
            out["tables"][tname] = tinfo
            i = t_end
        elif _leading_tabs(ln) == 3 and ":" in s:
            k, _, v = s.partition(":")
            out["model"][k.strip()] = _read_prop_value(v)
            i += 1
        else:
            i += 1
    return out


def set_translation_text(text: str, object_type: str, table: str,
                         name: str | None = None,
                         caption: str | None = None,
                         description: str | None = None,
                         display_folder: str | None = None) -> str:
    """Set translation properties for one object (surgical edit).

    ``None`` keeps a property as it is, ``""`` removes it. Creates the
    ``translations`` / ``model Model`` / ``table`` / object scaffolding as
    needed, and removes an object (or table) entry left without properties.
    """
    if object_type not in TRANSLATION_OBJECTS:
        raise ValueError(f"object_type must be one of {list(TRANSLATION_OBJECTS)}")
    if object_type != "table" and not name:
        raise ValueError(f"name is required for object_type={object_type!r}")
    if object_type == "table" and display_folder is not None:
        raise ValueError("Tables have no displayFolder translation")
    changes = {"caption": caption, "description": description,
               "displayFolder": display_folder}
    if all(v is None for v in changes.values()):
        raise ValueError("Pass at least one of caption, description, display_folder")
    for v in changes.values():
        if v:
            _prop_value(v)   # single-line check up front, before any edit

    lines = _split_lines(text)
    header = _top_header(lines, "cultureInfo")

    span = _translations_span(lines, header)
    if span is None:
        lines[header + 1:header + 1] = ["", "\ttranslations", "\t\tmodel Model"]
        span = _translations_span(lines, header)
    tr, tr_end = span
    model = next((i for i in range(tr + 1, tr_end)
                  if _leading_tabs(lines[i]) == 2
                  and lines[i].strip().startswith("model ")), None)
    if model is None:
        lines.insert(tr + 1, "\t\tmodel Model")
        model = tr + 1
    m_end = _block_end(lines, model, 2)

    tbl = _find_header(lines, "table", table, 3, model + 1, m_end)
    if tbl is None:
        lines.insert(m_end, f"\t\t\ttable {quote_ident(table)}")
        tbl = m_end
    t_end = _block_end(lines, tbl, 3)

    if object_type == "table":
        obj, prop_indent = tbl, 4
        first_child = next(
            (i for i in range(tbl + 1, t_end)
             if _leading_tabs(lines[i]) == 4
             and lines[i].strip().startswith(tuple(kw + " " for kw, _ in _CHILD_KW))),
            t_end)
        limit = first_child
    else:
        obj = _find_header(lines, object_type, name, 4, tbl + 1, t_end)
        if obj is None:
            lines.insert(t_end, f"\t\t\t\t{object_type} {quote_ident(name)}")
            obj = t_end
        limit = _block_end(lines, obj, 4)
        prop_indent = 5

    tabs = "\t" * prop_indent
    for key in TRANSLATION_PROPS:
        val = changes[key]
        if val is None:
            continue
        found = next((i for i in range(obj + 1, limit)
                      if _leading_tabs(lines[i]) == prop_indent
                      and lines[i].strip().startswith(key + ":")), None)
        if found is not None:
            if val == "":
                del lines[found]
                limit -= 1
            else:
                lines[found] = f"{tabs}{key}: {_prop_value(val)}"
        elif val != "":
            lines.insert(limit, f"{tabs}{key}: {_prop_value(val)}")
            limit += 1

    if object_type != "table":
        o_end = _block_end(lines, obj, 4)
        if not any(ln.strip() for ln in lines[obj + 1:o_end]):
            del lines[obj:o_end]
    t_end = _block_end(lines, tbl, 3)
    if not any(ln.strip() for ln in lines[tbl + 1:t_end]):
        del lines[tbl:t_end]
    return _join(lines)


def list_hierarchies_text(table_text: str) -> list[str]:
    """Names of ``hierarchy X`` members (indent 1) in a table file's text."""
    return [_header_name(ln, "hierarchy")
            for ln in _split_lines(table_text)
            if _leading_tabs(ln) == 1 and ln.strip().startswith("hierarchy ")]
