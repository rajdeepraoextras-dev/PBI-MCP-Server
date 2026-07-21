"""TMDL read/write — the MODEL layer.

Tab-indented, declarative. One table per file under
`*.SemanticModel/definition/tables/*.tmdl`. Measures live under their home
table; calc groups are their own table type.

Structure this parser relies on (verified against the synthetic fixture and
what Power BI Desktop / pbi-tools emit):

    table Sales                         # indent 0: object header
        lineageTag: ...                 # indent 1: table property
        measure 'Net Revenue' = SUM(..) # indent 1: member header
            formatString: #,0           # indent 2: member property
        measure 'Complex' =             # inline expr empty ->
                VAR x = ...             # indent >=3: expression continuation
            formatString: 0             # indent 2: member property (ends expr)
        column Amount                   # indent 1: member header
            dataType: double            # indent 2: member property

Rule that disambiguates a multi-line measure body from its properties: an
expression continuation line is indented *deeper* (>= member_indent + 2) than
the member's properties (member_indent + 1). Fenced bodies (` ``` `) are also
supported.

TODO(Day 4): implement emit (write valid tab-indented TMDL).

Leverage: PBICompass model parser informs this (Part G).
"""

from __future__ import annotations

import re
from pathlib import Path

from core.schemas import Column, Measure, Relationship, Table

_FENCE = "```"
_SIMPLE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MEMBER_KEYWORDS = ("measure ", "column ", "partition ", "hierarchy ",
                    "calculationGroup", "calculationItem ")


def _leading_tabs(line: str) -> int:
    n = 0
    for ch in line:
        if ch == "\t":
            n += 1
        else:
            break
    return n


def _unquote(name: str) -> str:
    """Strip TMDL single-quote quoting from an identifier (handles '' escape)."""
    name = name.strip()
    if len(name) >= 2 and name.startswith("'") and name.endswith("'"):
        return name[1:-1].replace("''", "'")
    return name


def _dedent(raw_lines: list[str]) -> str:
    """Remove the common leading-tab indent from a block, preserve nesting."""
    tabbed = [ln for ln in raw_lines if ln.strip()]
    if not tabbed:
        return ""
    base = min(_leading_tabs(ln) for ln in tabbed)
    out = []
    for ln in raw_lines:
        out.append(ln[base:] if ln.strip() else "")
    return "\n".join(out).strip("\n")


def parse_table_file(path: str | Path) -> Table:
    """Parse a single `tables/<Name>.tmdl` file into a `Table`."""
    # utf-8-sig tolerates a stray BOM without polluting the first token.
    text = Path(path).read_text(encoding="utf-8-sig")
    lines = [ln.rstrip("\r") for ln in text.split("\n")]

    table: Table | None = None
    i, n = 0, len(lines)

    # Locate the table header (indent 0).
    while i < n:
        s = lines[i].strip()
        if s.startswith("table "):
            table = Table(name=_unquote(s[len("table "):]))
            i += 1
            break
        i += 1
    if table is None:
        raise ValueError(f"No `table` declaration found in {path}")

    # Parse members at indent 1.
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        indent = _leading_tabs(line)
        if indent == 0:
            break  # start of a new top-level object (shouldn't happen per file)
        s = line.strip()

        if indent == 1 and s.startswith("measure "):
            measure, i = _parse_measure(lines, i, table.name)
            table.measures.append(measure)
            continue
        if indent == 1 and s.startswith("column "):
            column, i = _parse_column(lines, i)
            table.columns.append(column)
            continue
        if indent == 1 and s.startswith("calculationGroup"):
            table.is_calc_group = True
            i += 1
            continue
        if indent == 1 and s == "isHidden":
            table.is_hidden = True
            i += 1
            continue
        # Other table-level properties / partitions / hierarchies: skip.
        i += 1

    return table


def _parse_measure(lines: list[str], i: int, table_name: str) -> tuple[Measure, int]:
    member_indent = _leading_tabs(lines[i])
    body = lines[i].strip()[len("measure "):]
    name_part, _, expr_inline = body.partition("=")
    name = _unquote(name_part)
    expr_inline = expr_inline.strip()
    i += 1

    fmt: str | None = None
    folder: str | None = None
    hidden = False
    expr = ""
    cont_raw: list[str] = []

    if expr_inline.startswith(_FENCE):
        # Fenced block: collect verbatim lines until the closing fence.
        block: list[str] = []
        while i < len(lines):
            if lines[i].strip() == _FENCE:
                i += 1
                break
            block.append(lines[i])
            i += 1
        expr = _dedent(block)
    elif expr_inline:
        expr = expr_inline

    # Properties (member_indent + 1) and, for a non-inline body, expression
    # continuation lines (deeper). Stops at the next member / dedent.
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        indent = _leading_tabs(line)
        if indent <= member_indent:
            break
        s = line.strip()
        if indent == member_indent + 1 and ":" in s and not s.startswith("//"):
            key, _, val = s.partition(":")
            key, val = key.strip(), val.strip()
            if key == "formatString":
                fmt = val
            elif key == "displayFolder":
                folder = _unquote(val) if val.startswith("'") else val
            i += 1
            continue
        if indent == member_indent + 1 and s == "isHidden":
            hidden = True
            i += 1
            continue
        # Deeper (or an unrecognised +1 line): treat as expression body.
        if not expr:
            cont_raw.append(line)
        i += 1

    if not expr and cont_raw:
        expr = _dedent(cont_raw)

    measure = Measure(
        table=table_name,
        name=name,
        dax=expr,
        format_string=fmt,
        display_folder=folder,
        is_hidden=hidden,
    )
    return measure, i


def _parse_column(lines: list[str], i: int) -> tuple[Column, int]:
    member_indent = _leading_tabs(lines[i])
    body = lines[i].strip()[len("column "):]
    # Calculated columns have `name = expr`; we only need the name here.
    name_part, _, _ = body.partition("=")
    name = _unquote(name_part)
    i += 1

    data_type: str | None = None
    summarize_by: str | None = None
    data_category: str | None = None
    hidden = False
    is_key = False

    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        indent = _leading_tabs(line)
        if indent <= member_indent:
            break
        s = line.strip()
        if indent == member_indent + 1:
            if s.startswith("dataType:"):
                data_type = s.split(":", 1)[1].strip()
            elif s.startswith("summarizeBy:"):
                summarize_by = s.split(":", 1)[1].strip()
            elif s.startswith("dataCategory:"):
                data_category = s.split(":", 1)[1].strip()
            elif s == "isHidden":
                hidden = True
            elif s == "isKey":
                is_key = True
        i += 1

    column = Column(
        name=name,
        data_type=data_type,
        summarize_by=summarize_by,
        data_category=data_category,
        is_hidden=hidden,
        is_key=is_key,
    )
    return column, i


def _split_col_ref(ref: str) -> tuple[str, str]:
    """Split a `Table.Column` reference, unquoting the column part.

    Column names may be quoted (`Ethnicity.'Ethnic Group'`); table names in
    practice aren't, so we split on the first dot.
    """
    table, _, col = ref.strip().partition(".")
    return _unquote(table), _unquote(col)


def parse_relationships_file(path: str | Path) -> list[Relationship]:
    """Parse `relationships.tmdl` into a list of `Relationship`.

    Each block:  `relationship <name>` then indented `fromColumn:`/`toColumn:`
    and optional `isActive`, `crossFilteringBehavior`, `*Cardinality` props.
    """
    text = Path(path).read_text(encoding="utf-8-sig")
    lines = [ln.rstrip("\r") for ln in text.split("\n")]
    rels: list[Relationship] = []
    cur: dict | None = None

    def flush():
        if cur and cur.get("from") and cur.get("to"):
            ft, fc = _split_col_ref(cur["from"])
            tt, tc = _split_col_ref(cur["to"])
            rels.append(Relationship(
                name=cur.get("name"),
                from_table=ft, from_column=fc,
                to_table=tt, to_column=tc,
                cardinality=cur.get("cardinality"),
                cross_filter=cur.get("cross_filter"),
                is_active=cur.get("is_active", True),
            ))

    for line in lines:
        s = line.strip()
        if not s:
            continue
        if _leading_tabs(line) == 0 and s.startswith("relationship "):
            flush()
            cur = {"name": s[len("relationship "):].strip()}
        elif cur is not None:
            if s.startswith("fromColumn:"):
                cur["from"] = s.split(":", 1)[1].strip()
            elif s.startswith("toColumn:"):
                cur["to"] = s.split(":", 1)[1].strip()
            elif s == "isActive" or s.startswith("isActive:"):
                cur["is_active"] = True
            elif s.startswith("crossFilteringBehavior:"):
                cur["cross_filter"] = s.split(":", 1)[1].strip()
            elif s.endswith("Cardinality:") or "Cardinality:" in s:
                cur["cardinality"] = s.split(":", 1)[1].strip()
    flush()
    return rels


def emit_table_file(table: Table) -> str:
    """Render a `Table` back to tab-indented TMDL text.

    Intentionally NOT used for measure writes — the `Table` model is lossy
    (no partitions, lineageTag, annotations, M source). Rebuilding a file from
    it would corrupt the project. Measure writes go through the targeted text
    edit `upsert_measure_text` instead. (Reserved for future full-emit needs.)
    """
    raise NotImplementedError("Day 4+: full TMDL emit (not needed yet)")


# --- targeted, loss-free text edits ----------------------------------------

def quote_ident(name: str) -> str:
    """Quote a TMDL identifier if it isn't a bare word (escapes ' as '')."""
    if _SIMPLE_IDENT.match(name):
        return name
    return "'" + name.replace("'", "''") + "'"


def emit_measure_block(name: str, dax: str, fmt: str | None,
                       display_folder: str | None = None) -> list[str]:
    """Build the tab-indented lines for a single measure member (indent 1)."""
    dax = dax.strip("\n")
    lines: list[str]
    if "\n" in dax:
        lines = [f"\tmeasure {quote_ident(name)} = {_FENCE}"]
        for body_line in dax.split("\n"):
            lines.append("\t\t\t" + body_line)
        lines.append("\t\t\t" + _FENCE)
    else:
        lines = [f"\tmeasure {quote_ident(name)} = {dax.strip()}"]
    if fmt:
        lines.append(f"\t\tformatString: {fmt}")
    if display_folder:
        lines.append(f"\t\tdisplayFolder: {display_folder}")
    return lines


def _member_block_end(lines: list[str], header_idx: int) -> int:
    """Exclusive end index of a member block: header + its indented body.

    Excludes trailing blank lines so they aren't swallowed into the block.
    """
    end = header_idx + 1
    last_content = header_idx
    while end < len(lines):
        s = lines[end]
        if not s.strip():
            end += 1
            continue
        if _leading_tabs(s) <= 1:
            break
        last_content = end
        end += 1
    return last_content + 1


def _find_measure_header(lines: list[str], name: str) -> int | None:
    for idx, line in enumerate(lines):
        if _leading_tabs(line) != 1:
            continue
        s = line.strip()
        if not s.startswith("measure "):
            continue
        existing = _unquote(s[len("measure "):].partition("=")[0])
        if existing == name:
            return idx
    return None


def _last_measure_block_end(lines: list[str]) -> int | None:
    end = None
    for idx, line in enumerate(lines):
        if _leading_tabs(line) == 1 and line.strip().startswith("measure "):
            end = _member_block_end(lines, idx)
    return end


def _first_member_header(lines: list[str]) -> int | None:
    for idx, line in enumerate(lines):
        if _leading_tabs(line) != 1:
            continue
        s = line.strip()
        if any(s.startswith(k) for k in _MEMBER_KEYWORDS):
            return idx
    return None


def emit_column_block(name: str, data_type: str,
                      summarize_by: str | None = None,
                      source_column: str | None = None,
                      dax: str | None = None) -> list[str]:
    """Build the tab-indented lines for a column member (indent 1).

    `dax` set -> calculated column (`column Name = expr`, no sourceColumn).
    Otherwise a data column; `source_column` defaults to `name`.
    """
    if dax is not None:
        lines = [f"\tcolumn {quote_ident(name)} = {dax.strip()}"]
    else:
        lines = [f"\tcolumn {quote_ident(name)}"]
    lines.append(f"\t\tdataType: {data_type}")
    lines.append(f"\t\tsummarizeBy: {summarize_by or 'none'}")
    if dax is None:
        lines.append(f"\t\tsourceColumn: {source_column or name}")
    return lines


def _last_block_end(lines: list[str], keyword: str) -> int | None:
    """Exclusive end index of the last `keyword` member block, if any."""
    end = None
    for idx, line in enumerate(lines):
        if _leading_tabs(line) == 1 and line.strip().startswith(keyword):
            end = _member_block_end(lines, idx)
    return end


def insert_column_text(text: str, name: str, data_type: str,
                       summarize_by: str | None = None,
                       source_column: str | None = None,
                       dax: str | None = None) -> str:
    """Insert a new column into raw TMDL table text (surgical, loss-free).

    Placement: after the last existing column; else after the last measure;
    else before the first member (e.g. the partition); else appended.
    """
    lines = text.split("\n")
    block = emit_column_block(name, data_type, summarize_by, source_column, dax)

    after = _last_block_end(lines, "column ")
    if after is None:
        after = _last_block_end(lines, "measure ")
    if after is not None:
        lines[after:after] = [""] + block
        return "\n".join(lines)

    first_member = _first_member_header(lines)
    if first_member is not None:
        lines[first_member:first_member] = block + [""]
        return "\n".join(lines)

    trailing = 0
    while lines and not lines[-1].strip():
        lines.pop()
        trailing += 1
    lines += [""] + block + [""] * max(trailing, 1)
    return "\n".join(lines)


def _quote_col_ref(table: str, column: str) -> str:
    """Render a Table.Column ref the way relationships.tmdl expects."""
    tbl = quote_ident(table)
    col = quote_ident(column)
    return f"{tbl}.{col}"


def append_relationship_text(text: str, name: str,
                             from_table: str, from_column: str,
                             to_table: str, to_column: str,
                             cardinality: str | None = None,
                             cross_filter: str | None = None,
                             is_active: bool = True) -> str:
    """Append a relationship block to relationships.tmdl text."""
    block = [f"relationship {name}"]
    if not is_active:
        block.append("\tisActive: false")
    if cross_filter:
        block.append(f"\tcrossFilteringBehavior: {cross_filter}")
    if cardinality:
        block.append(f"\ttoCardinality: {cardinality}")
    block.append(f"\tfromColumn: {_quote_col_ref(from_table, from_column)}")
    block.append(f"\ttoColumn: {_quote_col_ref(to_table, to_column)}")

    lines = text.split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    if lines:
        lines.append("")  # blank line between blocks
    lines.extend(block)
    lines.append("")  # trailing newline
    return "\n".join(lines)


def emit_calc_group_table(name: str, precedence: int,
                          items: list[dict],
                          column_name: str = "Name") -> str:
    """Render a full calculation-group table file.

    `items`: [{"name": ..., "dax": ..., "ordinal"?: int}, ...] — order is kept.
    Mirrors Desktop's TMDL shape: calculationGroup block, Name/Ordinal
    columns, and a calculationGroup partition.
    """
    q = quote_ident
    out = [f"table {q(name)}"]
    out.append("\tcalculationGroup")
    out.append(f"\t\tprecedence: {precedence}")
    for item in items:
        dax = str(item["dax"]).strip("\n")
        out.append("")
        if "\n" in dax:
            out.append(f"\t\tcalculationItem {q(item['name'])} = {_FENCE}")
            for line in dax.split("\n"):
                out.append("\t\t\t\t" + line)
            out.append("\t\t\t\t" + _FENCE)
        else:
            out.append(f"\t\tcalculationItem {q(item['name'])} = {dax}")
    out.append("")
    out.append(f"\tcolumn {q(column_name)}")
    out.append("\t\tdataType: string")
    out.append("\t\tsummarizeBy: none")
    out.append("\t\tsourceColumn: Name")
    out.append("\t\tsortByColumn: Ordinal")
    out.append("")
    out.append("\tcolumn Ordinal")
    out.append("\t\tdataType: int64")
    out.append("\t\tisHidden")
    out.append("\t\tsummarizeBy: none")
    out.append("\t\tsourceColumn: Ordinal")
    out.append("")
    out.append(f"\tpartition {q(name)} = calculationGroup")
    out.append("\t\tmode: import")
    out.append("")
    return "\n".join(out)


def add_table_ref_text(model_text: str, table: str) -> str:
    """Add a `ref table <name>` line to model.tmdl after the last ref table."""
    ref_line = f"ref table {quote_ident(table)}"
    lines = model_text.split("\n")
    if any(ln.strip() == ref_line for ln in lines):
        return model_text
    last_ref = None
    for idx, ln in enumerate(lines):
        if ln.strip().startswith("ref table "):
            last_ref = idx
    if last_ref is not None:
        lines.insert(last_ref + 1, ref_line)
    else:
        while lines and not lines[-1].strip():
            lines.pop()
        lines += ["", ref_line, ""]
    return "\n".join(lines)


def delete_measure_text(text: str, name: str) -> str:
    """Remove a measure block from raw TMDL text (surgical, loss-free).

    Also removes one adjacent blank separator line so spacing stays tidy.
    Raises KeyError if the measure isn't in this file.
    """
    lines = text.split("\n")
    header = _find_measure_header(lines, name)
    if header is None:
        raise KeyError(f"Measure {name!r} not found in this table file")
    end = _member_block_end(lines, header)
    # swallow the blank line(s) directly after the block (or before, at EOF)
    while end < len(lines) and not lines[end].strip() and (
            end + 1 < len(lines)):
        end += 1
        break
    del lines[header:end]
    return "\n".join(lines)


def upsert_measure_text(text: str, name: str, dax: str,
                        fmt: str | None = None,
                        display_folder: str | None = None) -> str:
    """Insert or replace a measure in raw TMDL text, preserving everything else.

    This is a surgical edit: only the target measure's block is touched. All
    other members, table properties, partitions, and annotations are left byte
    for byte as they were.
    """
    lines = text.split("\n")
    block = emit_measure_block(name, dax, fmt, display_folder)

    header = _find_measure_header(lines, name)
    if header is not None:
        end = _member_block_end(lines, header)
        lines[header:end] = block
        return "\n".join(lines)

    # Not present — insert. Prefer just after the last existing measure.
    after_measures = _last_measure_block_end(lines)
    if after_measures is not None:
        lines[after_measures:after_measures] = [""] + block
        return "\n".join(lines)

    # No measures yet — insert before the first member (column/partition/...).
    first_member = _first_member_header(lines)
    if first_member is not None:
        lines[first_member:first_member] = block + [""]
        return "\n".join(lines)

    # Table has no members at all — append after the table header/properties.
    trailing_blanks = 0
    while lines and not lines[-1].strip():
        lines.pop()
        trailing_blanks += 1
    lines += [""] + block + [""] * max(trailing_blanks, 1)
    return "\n".join(lines)
