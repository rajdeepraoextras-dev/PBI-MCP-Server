"""Member-level TMDL surgery: columns, tables, measures, hierarchies, KPIs.

core/tmdl.py keeps a few column/measure fields and creates or replaces whole
measure/column blocks. The lifecycle tools (model_server/tools_members.py)
need finer edits — one property line inside an existing block, a `///`
description above it, a `kpi` child under a measure, a `hierarchy` with
`level` children — without disturbing anything else in the file.

So this module has a small generic TMDL object parser that records *where*
every piece of an object lives (line spans for the description, the header,
the expression body, each property and each child object) plus editors that
splice exactly those lines:

    table Sales                      <- Block(kind="table")
        lineageTag: ...              <- Prop  (`key: value`)
        /// Net sales                <- description of the next block
        measure 'Net Revenue' = ...  <- child Block(kind="measure")
            formatString: #,0        <- Prop
            isHidden                 <- Prop  (bare boolean -> value True)
            kpi                      <- child Block(kind="kpi", nameless)
                targetExpression = … <- Prop  (`key = expr`, is_expr=True)
            annotation A = B         <- child Block(kind="annotation")

Everything works on `text.split("\\n")` lines with tab indentation (what
Power BI Desktop emits). Editors return a new line list and leave the input
untouched; callers re-parse before the next edit because indexes shift.
Nothing here reads or writes files.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field

from core.tmdl import _FENCE, _dedent, _leading_tabs, _unquote, quote_ident

# Objects declared without a name; any other lone word at an object's
# property indent is a boolean property (`isHidden`, `isKey`, ...).
_NAMELESS_OBJECTS = frozenset({"kpi", "calculationGroup"})
_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
_PROP_COLON = re.compile(rf"^({_IDENT})\s*:(.*)$")
_PROP_EXPR = re.compile(rf"^({_IDENT})\s*=(.*)$")
_BARE = re.compile(rf"^{_IDENT}$")

#: dataCategory values Power BI Desktop accepts for columns.
DATA_CATEGORIES = (
    "Address", "City", "Continent", "Country", "County", "Image", "ImageUrl",
    "Latitude", "Longitude", "Organization", "Place", "PostalCode",
    "StateOrProvince", "WebUrl", "Barcode",
)
#: TMDL dataType enum values (camelCase as Desktop writes them).
DATA_TYPES = ("string", "int64", "double", "dateTime", "boolean", "decimal",
              "binary", "variant")
#: TMDL summarizeBy enum values.
SUMMARIZE_BY = ("none", "sum", "count", "min", "max", "average",
                "distinctCount", "default")
DEFAULT_STATUS_GRAPHIC = "Traffic Light - Single"
DEFAULT_TREND_GRAPHIC = "Standard Arrow"


def canonical(value: str, allowed: tuple[str, ...], what: str) -> str:
    """Map `value` onto the canonical spelling in `allowed` (case-insensitive).

    Raises ValueError naming the accepted values when it isn't one of them.
    """
    for candidate in allowed:
        if candidate.lower() == str(value).strip().lower():
            return candidate
    raise ValueError(f"Unknown {what} {value!r}; expected one of "
                     f"{', '.join(allowed)}")


# --- object model -----------------------------------------------------------

@dataclass
class Prop:
    """One property line (or `key = expr` with its continuation lines)."""

    key: str
    value: str | bool           # True for a bare boolean keyword
    start: int
    end: int                    # exclusive
    is_expr: bool = False       # written with `=` (DAX / M expression)


@dataclass
class Block:
    """One TMDL object with the line spans of everything inside it."""

    kind: str                   # table | measure | column | hierarchy | ...
    name: str                   # "" for nameless objects (kpi, ...)
    indent: int
    start: int                  # first `///` description line, else header
    header: int
    body_end: int               # exclusive end of the header's expression
    end: int                    # exclusive; trailing blank lines excluded
    expression: str | None = None
    description: list[str] = field(default_factory=list)
    props: list[Prop] = field(default_factory=list)
    children: list[Block] = field(default_factory=list)

    def prop(self, key: str) -> Prop | None:
        k = key.lower()
        for p in self.props:
            if p.key.lower() == k:
                return p
        return None

    def value(self, key: str) -> str | bool | None:
        p = self.prop(key)
        return None if p is None else p.value

    def text(self, key: str) -> str | None:
        """A string property with TMDL double-quote wrapping removed."""
        v = self.value(key)
        return unquote_value(v) if isinstance(v, str) else None

    def flag(self, key: str) -> bool:
        v = self.value(key)
        return v is True or (isinstance(v, str) and v.strip().lower() == "true")

    def child(self, kind: str, name: str | None = None) -> Block | None:
        for c in self.children:
            if c.kind == kind and (name is None or c.name == name):
                return c
        return None

    def children_of(self, *kinds: str) -> list[Block]:
        return [c for c in self.children if c.kind in kinds]

    @property
    def prop_insert_at(self) -> int:
        """Where a new property line goes: after the last property, else
        right after the header (and its multi-line expression, if any)."""
        at = self.body_end
        for p in self.props:
            at = max(at, p.end)
        return at


# --- parsing ----------------------------------------------------------------

def _desc_text(line: str) -> str:
    s = line.strip()[3:]
    return s[1:] if s.startswith(" ") else s


def _split_header(s: str) -> tuple[str, str, str | None]:
    """`measure 'Net Revenue' = SUM(x)` -> ("measure", "Net Revenue", "SUM(x)").

    The third item is None when the header has no `=`, "" when the `=` opens
    a multi-line expression on the following lines, or the fence marker.
    """
    parts = s.split(None, 1)
    kind = parts[0]
    rest = parts[1].strip() if len(parts) > 1 else ""
    if not rest:
        return kind, "", None
    if rest.startswith("'"):
        j = 1
        while j < len(rest):
            if rest[j] == "'":
                if j + 1 < len(rest) and rest[j + 1] == "'":
                    j += 2
                    continue
                break
            j += 1
        name = _unquote(rest[:j + 1])
        tail = rest[j + 1:].strip()
    else:
        name_part, sep, after = rest.partition("=")
        name = name_part.strip()
        tail = ("=" + after) if sep else ""
    if not tail:
        return kind, name, None
    if tail.startswith("="):
        return kind, name, tail[1:].strip()
    return kind, f"{name} {tail}".strip(), None


def _consume_open(lines: list[str], i: int, min_indent: int) -> tuple[str, int]:
    """Multi-line expression body: lines indented >= min_indent (blank lines
    allowed); trailing blank lines are not part of it."""
    body: list[str] = []
    j = i
    while j < len(lines):
        ln = lines[j]
        if ln.strip() and _leading_tabs(ln) < min_indent:
            break
        body.append(ln)
        j += 1
    while body and not body[-1].strip():
        body.pop()
        j -= 1
    return _dedent(body), j


def _consume_fenced(lines: list[str], i: int) -> tuple[str, int]:
    body: list[str] = []
    j = i
    while j < len(lines):
        if lines[j].strip() == _FENCE:
            return _dedent(body), j + 1
        body.append(lines[j])
        j += 1
    return _dedent(body), j


def _has_deeper_body(lines: list[str], i: int, indent: int) -> bool:
    """True when the next non-blank line is indented deeper than `indent`:
    a lone word with a body is a nameless child object (`relatedColumnDetails`
    and friends), never a boolean property."""
    j = i + 1
    while j < len(lines) and not lines[j].strip():
        j += 1
    return j < len(lines) and _leading_tabs(lines[j]) > indent


def _is_object_header(s: str) -> bool:
    if s.startswith("///") or _PROP_COLON.match(s) or _PROP_EXPR.match(s):
        return False
    if _BARE.match(s):
        return s in _NAMELESS_OBJECTS
    return " " in s or "\t" in s


def _parse_block(lines: list[str], header_idx: int, indent: int) -> Block:
    start = header_idx
    desc: list[str] = []
    k = header_idx - 1
    while (k >= 0 and lines[k].strip().startswith("///")
           and _leading_tabs(lines[k]) == indent):
        desc.insert(0, _desc_text(lines[k]))
        start = k
        k -= 1

    kind, name, rhs = _split_header(lines[header_idx].strip())
    i = header_idx + 1
    expression: str | None = None
    if rhs is not None:
        if rhs == "":
            expression, i = _consume_open(lines, i, indent + 2)
        elif rhs.startswith(_FENCE):
            expression, i = _consume_fenced(lines, i)
        else:
            expression = rhs
    block = Block(kind=kind, name=name, indent=indent, start=start,
                  header=header_idx, body_end=i, end=i,
                  expression=expression, description=desc)
    last = i - 1

    n = len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        ind = _leading_tabs(line)
        if ind <= indent:
            break
        s = line.strip()
        if ind == indent + 1:
            if s.startswith("///"):
                i += 1
                continue
            m = _PROP_COLON.match(s)
            if m:
                block.props.append(Prop(m.group(1), m.group(2).strip(), i, i + 1))
                last = i
                i += 1
                continue
            m = _PROP_EXPR.match(s)
            if m:
                key, prhs = m.group(1), m.group(2).strip()
                j = i + 1
                if prhs == "":
                    val, j = _consume_open(lines, j, indent + 2)
                elif prhs.startswith(_FENCE):
                    val, j = _consume_fenced(lines, j)
                else:
                    val = prhs
                block.props.append(Prop(key, val, i, j, is_expr=True))
                last = j - 1
                i = j
                continue
            if (_BARE.match(s) and s not in _NAMELESS_OBJECTS
                    and not _has_deeper_body(lines, i, indent + 1)):
                block.props.append(Prop(s, True, i, i + 1))
                last = i
                i += 1
                continue
            child = _parse_block(lines, i, indent + 1)
            block.children.append(child)
            last = child.end - 1
            i = child.end
            continue
        # a deeper line not claimed by an expression: keep it in the block
        last = i
        i += 1

    block.end = last + 1
    return block


def parse_objects(lines: list[str], indent: int = 0,
                  lo: int = 0, hi: int | None = None) -> list[Block]:
    """All object blocks declared at exactly `indent` within lines[lo:hi]."""
    hi = len(lines) if hi is None else hi
    out: list[Block] = []
    i = lo
    while i < hi:
        line = lines[i]
        s = line.strip()
        if not s or _leading_tabs(line) != indent or not _is_object_header(s):
            i += 1
            continue
        block = _parse_block(lines, i, indent)
        out.append(block)
        i = max(block.end, i + 1)
    return out


def parse_table(lines: list[str]) -> Block:
    """The `table` block of a tables/<Name>.tmdl file (members = children)."""
    for block in parse_objects(lines, 0):
        if block.kind == "table":
            return block
    raise ValueError("No `table` declaration found")


# --- values -----------------------------------------------------------------

def prop_value(value: str) -> str:
    """Render a text property value; TMDL needs double quotes when the text
    has leading/trailing whitespace (inner quotes are doubled)."""
    value = str(value)
    if value != value.strip() or value.startswith('"'):
        return '"' + value.replace('"', '""') + '"'
    return value


def unquote_value(value: str) -> str:
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1].replace('""', '"')
    return value


def _ref_name(value: str | bool | None) -> str | None:
    """A named-object reference (`sortByColumn: 'Order Date'`) unquoted."""
    return _unquote(value) if isinstance(value, str) and value else None


# --- read-outs --------------------------------------------------------------

def column_info(table: str, col: Block) -> dict:
    return {
        "table": table,
        "name": col.name,
        "data_type": col.text("dataType"),
        "is_hidden": col.flag("isHidden"),
        "is_key": col.flag("isKey"),
        "summarize_by": col.text("summarizeBy"),
        "format_string": col.text("formatString"),
        "data_category": col.text("dataCategory"),
        "sort_by_column": _ref_name(col.value("sortByColumn")),
        "display_folder": col.text("displayFolder"),
        "description": "\n".join(col.description) if col.description else None,
        "is_calculated": col.expression is not None,
        "dax": col.expression,
        "source_column": col.text("sourceColumn"),
    }


def hierarchy_info(table: str, hier: Block) -> dict:
    return {
        "table": table,
        "name": hier.name,
        "description": "\n".join(hier.description) if hier.description else None,
        "is_hidden": hier.flag("isHidden"),
        "display_folder": hier.text("displayFolder"),
        "levels": [{"name": lvl.name, "column": _ref_name(lvl.value("column"))}
                   for lvl in hier.children_of("level")],
    }


def kpi_info(kpi: Block) -> dict:
    return {
        "description": "\n".join(kpi.description) if kpi.description else None,
        "target_expression": kpi.text("targetExpression"),
        "target_format_string": kpi.text("targetFormatString"),
        "status_expression": kpi.text("statusExpression"),
        "status_graphic": kpi.text("statusGraphic"),
        "trend_expression": kpi.text("trendExpression"),
        "trend_graphic": kpi.text("trendGraphic"),
        "annotations": {a.name: a.expression for a in kpi.children_of("annotation")},
    }


# --- editors (pure: return new line lists) ----------------------------------

def description_lines(indent: int, description: str) -> list[str]:
    tabs = "\t" * indent
    return [f"{tabs}/// {ln}".rstrip()
            for ln in description.replace("\r\n", "\n").split("\n")]


def set_description(lines: list[str], block: Block,
                    description: str | None) -> list[str]:
    """Replace (or with None/"" remove) the `///` lines above `block`."""
    new = lines[:block.start]
    if description:
        new += description_lines(block.indent, description)
    return new + lines[block.header:]


def set_property(lines: list[str], block: Block, key: str,
                 value: str | bool | None) -> list[str]:
    """Set (`key: value`), switch on (True -> bare `key`) or remove
    (None/False/"") one property line of `block`, in place when it exists,
    else after the block's last property."""
    tabs = "\t" * (block.indent + 1)
    p = block.prop(key)
    if value is None or value is False or value == "":
        if p is None:
            return list(lines)
        return lines[:p.start] + lines[p.end:]
    line = f"{tabs}{key}" if value is True else f"{tabs}{key}: {value}"
    if p is not None:
        return lines[:p.start] + [line] + lines[p.end:]
    at = block.prop_insert_at
    return lines[:at] + [line] + lines[at:]


def delete_span(lines: list[str], start: int, end: int) -> list[str]:
    """Remove lines[start:end] and tidy the blank-line separators so what is
    left reads as if the block had never been there (two blanks collapse to
    one; the file keeps or lacks its final newline as before)."""
    had_final_newline = bool(lines) and lines[-1] == ""
    new = lines[:start] + lines[end:]
    if (0 < start < len(new) and not new[start].strip()
            and not new[start - 1].strip()):
        del new[start]
    if start >= len(new) or all(not ln.strip() for ln in new[start:]):
        while len(new) > 1 and not new[-1].strip() and not new[-2].strip():
            new.pop()
        if had_final_newline and new and new[-1] != "":
            new.append("")
        elif not had_final_newline and new and new[-1] == "":
            new.pop()
    return new


def insert_member(lines: list[str], table: Block, block_lines: list[str],
                  after_kinds: tuple[str, ...]) -> list[str]:
    """Insert a member block into a table: after the last child whose kind
    is in `after_kinds`; else before the first `partition` (or first child);
    else at the end of the table."""
    anchor = max((c.end for c in table.children_of(*after_kinds)), default=None)
    if anchor is not None:
        return lines[:anchor] + [""] + block_lines + lines[anchor:]
    first = table.child("partition") or (table.children[0] if table.children else None)
    if first is not None:
        return lines[:first.start] + block_lines + [""] + lines[first.start:]
    had_final_newline = bool(lines) and lines[-1] == ""
    new = list(lines)
    while new and not new[-1].strip():
        new.pop()
    new += [""] + block_lines
    if had_final_newline:
        new.append("")
    return new


# --- emitters ---------------------------------------------------------------

def expr_prop_lines(indent: int, key: str, dax: str) -> list[str]:
    """`key = expr` at `indent`; multi-line DAX goes in a verbatim fence."""
    dax = dax.strip("\n")
    tabs = "\t" * indent
    if "\n" not in dax:
        return [f"{tabs}{key} = {dax.strip()}"]
    inner = "\t" * (indent + 1)
    out = [f"{tabs}{key} = {_FENCE}"]
    out += [inner + ln if ln.strip() else "" for ln in dax.split("\n")]
    out.append(inner + _FENCE)
    return out


def emit_kpi_block(indent: int, *, target_expression: str,
                   status_expression: str, trend_expression: str | None = None,
                   status_graphic: str = DEFAULT_STATUS_GRAPHIC,
                   trend_graphic: str | None = None,
                   description: str | None = None,
                   target_format_string: str | None = None,
                   target_description: str | None = None,
                   status_description: str | None = None,
                   trend_description: str | None = None,
                   annotations: dict[str, str] | None = None) -> list[str]:
    """Lines of a `kpi` child object at `indent` (the measure's indent + 1).

    Shape follows what Desktop serialises: `kpi` on its own line, expression
    properties with `=`, other properties with `:`, then one blank-separated
    `annotation` child per entry of `annotations`.
    """
    tabs = "\t" * indent
    inner = "\t" * (indent + 1)
    out: list[str] = []
    if description:
        out += description_lines(indent, description)
    out.append(f"{tabs}kpi")
    out += expr_prop_lines(indent + 1, "targetExpression", target_expression)
    if target_format_string:
        out.append(f"{inner}targetFormatString: {prop_value(target_format_string)}")
    if target_description:
        out.append(f"{inner}targetDescription: {prop_value(target_description)}")
    out.append(f"{inner}statusGraphic: {prop_value(status_graphic)}")
    if status_description:
        out.append(f"{inner}statusDescription: {prop_value(status_description)}")
    out += expr_prop_lines(indent + 1, "statusExpression", status_expression)
    if trend_graphic:
        out.append(f"{inner}trendGraphic: {prop_value(trend_graphic)}")
    if trend_description:
        out.append(f"{inner}trendDescription: {prop_value(trend_description)}")
    if trend_expression:
        out += expr_prop_lines(indent + 1, "trendExpression", trend_expression)
    for name, value in (annotations or {}).items():
        out += ["", f"{inner}annotation {quote_ident(name)} = {value}"]
    return out


def emit_hierarchy_block(name: str, levels: list[dict],
                         description: str | None = None,
                         hidden: bool = False) -> list[str]:
    """Lines of a `hierarchy` member (indent 1) with its `level` children.

    levels: [{"name": ..., "column": ...}, ...] in display order. Each object
    gets a fresh lineageTag, as Desktop does.
    """
    out: list[str] = []
    if description:
        out += description_lines(1, description)
    out.append(f"\thierarchy {quote_ident(name)}")
    if hidden:
        out.append("\t\tisHidden")
    out.append(f"\t\tlineageTag: {uuid.uuid4()}")
    for lvl in levels:
        out.append("")
        out.append(f"\t\tlevel {quote_ident(lvl['name'])}")
        out.append(f"\t\t\tlineageTag: {uuid.uuid4()}")
        out.append(f"\t\t\tcolumn: {quote_ident(lvl['column'])}")
    return out
