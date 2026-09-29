"""TMDL emit/parse for field parameters and what-if parameters.

Both are ordinary calculated tables (``partition <Name> = calculated``), so
``core.tmdl.parse_table_file`` and ``PbipProject.list_tables`` read them like
any other table. What marks them as parameters is an ``extendedProperty
ParameterMetadata`` on one column.

The shapes below are copied from files Power BI Desktop itself wrote:

* field parameter - Microsoft's ``pbidevmode`` sample ("Parameter - Measure")
  and the field-parameter reference in microsoft/skills-for-fabric::

      table 'Slice by'
          lineageTag: <guid>

          column 'Slice by'                    <- label, what a slicer binds to
              dataType: string
              lineageTag: <guid>
              summarizeBy: none
              isDataTypeInferred
              sourceColumn: [Value1]
              sortByColumn: 'Slice by Order'

              relatedColumnDetails
                  groupByColumn: 'Slice by Fields'

              annotation SummarizationSetBy = Automatic

          column 'Slice by Fields'             <- hidden, holds NAMEOF(...)
              dataType: string
              isHidden
              ...
              sourceColumn: [Value2]
              sortByColumn: 'Slice by Order'

              extendedProperty ParameterMetadata =
                      {"version": 3, "kind": 2}   (pretty-printed)

          column 'Slice by Order'              <- hidden, dense 0-based order
              dataType: int64 / isHidden / formatString: 0 / summarizeBy: sum
              sourceColumn: [Value3]

          partition 'Slice by' = calculated
              mode: import
              source =
                      {
                          ("Label", NAMEOF('Table'[Column]), 0),
                          ...
                      }

          annotation PBI_Id = <32 hex>

* what-if parameter - Microsoft/BCApps ("Pareto Scale") and fabric-toolbox::

      table Discount
          lineageTag: <guid>

          measure 'Discount Value' = SELECTEDVALUE('Discount'[Discount], 0)
              formatString: 0.00
              lineageTag: <guid>

          column Discount
              dataType: double
              formatString: 0.00
              lineageTag: <guid>
              summarizeBy: none
              sourceColumn: [Value]

              extendedProperty ParameterMetadata =
                      {"version": 0}

              annotation SummarizationSetBy = User

          partition Discount = calculated
              mode: import
              source = GENERATESERIES(0, 0.5, 0.05)

          annotation PBI_Id = <32 hex>

Uncertain details (no public schema exists for them): whether the what-if
column carries ``dataType`` / ``isDataTypeInferred`` (Desktop versions differ;
both are accepted by the loader) and whether ``PBI_Id`` is required (Desktop
writes it; nothing is known to read it back for these tables).
"""

from __future__ import annotations

import json
import re
import uuid
from decimal import Decimal, InvalidOperation

from core.tmdl import _leading_tabs, _unquote, quote_ident
from core.tmdl_security import (
    _block_end, _dedent_expr, _header_name, _read_description, _split_default,
    _split_lines, description_lines,
)

FIELD_PARAMETER_KIND = 2
MAX_WHATIF_ROWS = 1_000_000


# --- DAX literal / reference builders --------------------------------------

def dax_string(s: str) -> str:
    return '"' + s.replace('"', '""') + '"'


def dax_table_ref(table: str) -> str:
    return "'" + table.replace("'", "''") + "'"


def dax_column_ref(table: str, name: str) -> str:
    return f"{dax_table_ref(table)}[{name.replace(']', ']]')}]"


def format_number(x: float | int) -> str:
    """A finite number as a plain DAX literal (no exponent, no trailing .0)."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        raise ValueError(f"Expected a number, got {x!r}")
    if isinstance(x, float):
        if x != x or x in (float("inf"), float("-inf")):
            raise ValueError(f"Expected a finite number, got {x!r}")
        if x.is_integer():
            return str(int(x))
        return format(Decimal(repr(x)), "f")
    return str(x)


def _decimals(x: float | int) -> int:
    if isinstance(x, int) or (isinstance(x, float) and x.is_integer()):
        return 0
    exp = Decimal(repr(x)).as_tuple().exponent
    return max(0, -exp) if isinstance(exp, int) else 0


def default_format_string(*values: float | int) -> str:
    """``0`` for whole numbers, else ``0.0..`` with just enough decimals."""
    d = min(6, max(_decimals(v) for v in values))
    return "0" if d == 0 else "0." + "0" * d


def series_length(minimum: float, maximum: float, increment: float) -> int:
    span = Decimal(repr(maximum)) - Decimal(repr(minimum))
    return int(span / Decimal(repr(increment))) + 1


def _guid() -> str:
    return str(uuid.uuid4())


def _pbi_id() -> str:
    return uuid.uuid4().hex


def _json_block(indent_tabs: int, obj: dict) -> list[str]:
    """Pretty-printed JSON as Desktop lays it out under ``property =``."""
    tabs = "\t" * indent_tabs
    return [tabs + ln for ln in json.dumps(obj, indent=2).split("\n")]


# --- emit -------------------------------------------------------------------

def emit_field_parameter_table(name: str, fields: list[dict],
                               description: str | None = None) -> str:
    """Render ``tables/<name>.tmdl`` for a field parameter.

    ``fields``: ``[{"label", "table", "name"}, ...]`` in slicer order; the
    ``order`` value of each tuple is its index.
    """
    if not fields:
        raise ValueError("A field parameter needs at least one field")
    q = quote_ident
    label_col, fields_col, order_col = name, f"{name} Fields", f"{name} Order"
    tuples = [
        f"\t\t\t\t    ({dax_string(f['label'])}, "
        f"NAMEOF({dax_column_ref(f['table'], f['name'])}), {i})"
        + ("," if i < len(fields) - 1 else "")
        for i, f in enumerate(fields)
    ]
    out = description_lines(description, 0)
    out += [
        f"table {q(name)}",
        f"\tlineageTag: {_guid()}",
        "",
        f"\tcolumn {q(label_col)}",
        "\t\tdataType: string",
        f"\t\tlineageTag: {_guid()}",
        "\t\tsummarizeBy: none",
        "\t\tisDataTypeInferred",
        "\t\tsourceColumn: [Value1]",
        f"\t\tsortByColumn: {q(order_col)}",
        "",
        "\t\trelatedColumnDetails",
        f"\t\t\tgroupByColumn: {q(fields_col)}",
        "",
        "\t\tannotation SummarizationSetBy = Automatic",
        "",
        f"\tcolumn {q(fields_col)}",
        "\t\tdataType: string",
        "\t\tisHidden",
        f"\t\tlineageTag: {_guid()}",
        "\t\tsummarizeBy: none",
        "\t\tisDataTypeInferred",
        "\t\tsourceColumn: [Value2]",
        f"\t\tsortByColumn: {q(order_col)}",
        "",
        "\t\textendedProperty ParameterMetadata =",
        *_json_block(4, {"version": 3, "kind": FIELD_PARAMETER_KIND}),
        "",
        "\t\tannotation SummarizationSetBy = Automatic",
        "",
        f"\tcolumn {q(order_col)}",
        "\t\tdataType: int64",
        "\t\tisHidden",
        "\t\tformatString: 0",
        f"\t\tlineageTag: {_guid()}",
        "\t\tsummarizeBy: sum",
        "\t\tisDataTypeInferred",
        "\t\tsourceColumn: [Value3]",
        "",
        "\t\tannotation SummarizationSetBy = Automatic",
        "",
        f"\tpartition {q(name)} = calculated",
        "\t\tmode: import",
        "\t\tsource =",
        "\t\t\t\t{",
        *tuples,
        "\t\t\t\t}",
        "",
        f"\tannotation PBI_Id = {_pbi_id()}",
        "",
    ]
    return "\n".join(out)


def emit_whatif_table(name: str, minimum: float, maximum: float,
                      increment: float, default: float,
                      format_string: str, description: str | None = None,
                      measure_name: str | None = None) -> str:
    """Render ``tables/<name>.tmdl`` for a what-if parameter.

    Column ``<name>`` holds the series, measure ``<name> Value`` returns the
    selected value (``default`` when nothing or several are selected).
    """
    q = quote_ident
    measure_name = measure_name or f"{name} Value"
    whole = all(_decimals(v) == 0 for v in (minimum, maximum, increment))
    dtype = "int64" if whole else "double"
    if "\n" in format_string or "\r" in format_string:
        raise ValueError("format_string must be a single line")
    out = description_lines(description, 0)
    out += [
        f"table {q(name)}",
        f"\tlineageTag: {_guid()}",
        "",
        f"\tmeasure {q(measure_name)} = SELECTEDVALUE("
        f"{dax_column_ref(name, name)}, {format_number(default)})",
        f"\t\tformatString: {format_string}",
        f"\t\tlineageTag: {_guid()}",
        "",
        f"\tcolumn {q(name)}",
        f"\t\tdataType: {dtype}",
        f"\t\tformatString: {format_string}",
        f"\t\tlineageTag: {_guid()}",
        "\t\tsummarizeBy: none",
        "\t\tisDataTypeInferred",
        "\t\tsourceColumn: [Value]",
        "",
        "\t\textendedProperty ParameterMetadata =",
        *_json_block(4, {"version": 0}),
        "",
        "\t\tannotation SummarizationSetBy = User",
        "",
        f"\tpartition {q(name)} = calculated",
        "\t\tmode: import",
        f"\t\tsource = GENERATESERIES({format_number(minimum)}, "
        f"{format_number(maximum)}, {format_number(increment)})",
        "",
        f"\tannotation PBI_Id = {_pbi_id()}",
        "",
    ]
    return "\n".join(out)


# --- parse ------------------------------------------------------------------

def _column_blocks(lines: list[str]) -> list[tuple[str, int, int]]:
    """[(column name, header idx, exclusive end idx)] for indent-1 columns."""
    out = []
    for idx, ln in enumerate(lines):
        if _leading_tabs(ln) == 1 and ln.strip().startswith("column "):
            out.append((_header_name(ln, "column"), idx, _block_end(lines, idx, 1)))
    return out


def column_parameter_metadata(text: str) -> dict[str, dict]:
    """``{column name: ParameterMetadata JSON}`` for columns that carry one."""
    lines = _split_lines(text)
    out: dict[str, dict] = {}
    for cname, start, end in _column_blocks(lines):
        for j in range(start + 1, end):
            s = lines[j].strip()
            if _leading_tabs(lines[j]) != 2 or \
                    not s.startswith("extendedProperty ParameterMetadata"):
                continue
            _, inline = _split_default(s[len("extendedProperty "):])
            if inline:
                raw = inline
            else:
                body = []
                k = j + 1
                while k < end and (not lines[k].strip() or _leading_tabs(lines[k]) >= 3):
                    body.append(lines[k])
                    k += 1
                raw = _dedent_expr(body)
            try:
                meta = json.loads(raw)
            except ValueError:
                continue
            if isinstance(meta, dict):
                out[cname] = meta
    return out


def partition_source(text: str) -> str | None:
    """The DAX/M ``source`` expression of the table's first partition."""
    lines = _split_lines(text)
    for i, ln in enumerate(lines):
        if not (_leading_tabs(ln) == 1 and ln.strip().startswith("partition ")):
            continue
        end = _block_end(lines, i, 1)
        for j in range(i + 1, end):
            s = lines[j].strip()
            if _leading_tabs(lines[j]) == 2 and re.match(r"source\s*=", s):
                inline = s.split("=", 1)[1].strip()
                if inline.startswith("```"):
                    body = []
                    k = j + 1
                    while k < end and lines[k].strip() != "```":
                        body.append(lines[k])
                        k += 1
                    return _dedent_expr(body)
                if inline:
                    return inline
                body = []
                k = j + 1
                while k < end and (not lines[k].strip() or _leading_tabs(lines[k]) >= 3):
                    body.append(lines[k])
                    k += 1
                return _dedent_expr(body)
    return None


def table_name(text: str) -> str | None:
    for ln in _split_lines(text):
        if _leading_tabs(ln) == 0 and ln.strip().startswith("table "):
            return _header_name(ln, "table")
    return None


def table_description(text: str) -> str | None:
    lines = _split_lines(text)
    for idx, ln in enumerate(lines):
        if _leading_tabs(ln) == 0 and ln.strip().startswith("table "):
            return _read_description(lines, idx)
    return None


def column_property(text: str, column: str, prop: str) -> str | None:
    """Value of a ``prop: value`` line directly under ``column``."""
    lines = _split_lines(text)
    for cname, start, end in _column_blocks(lines):
        if cname != column:
            continue
        for j in range(start + 1, end):
            s = lines[j].strip()
            if _leading_tabs(lines[j]) == 2 and s.startswith(prop + ":"):
                return s.split(":", 1)[1].strip()
    return None


_TUPLE = re.compile(
    r'\(\s*"((?:[^"]|"")*)"\s*,\s*NAMEOF\s*\(\s*'
    r"(?:'((?:[^']|'')+)'|([A-Za-z_]\w*))\s*"
    r"\[((?:[^\]]|\]\])+)\]\s*\)\s*,\s*(-?\d+)\s*\)",
    re.IGNORECASE,
)


def parse_field_tuples(source: str) -> list[dict]:
    """``[{"label", "table", "name", "order"}]`` from a field-parameter body."""
    out = []
    for m in _TUPLE.finditer(source or ""):
        out.append({
            "label": m.group(1).replace('""', '"'),
            "table": (m.group(2) or m.group(3)).replace("''", "'"),
            "name": m.group(4).replace("]]", "]"),
            "order": int(m.group(5)),
        })
    return out


def parse_field_parameter(text: str) -> dict | None:
    """Describe a field-parameter table file, or None if it is not one."""
    meta = column_parameter_metadata(text)
    fields_cols = [c for c, m in meta.items() if m.get("kind") == FIELD_PARAMETER_KIND]
    if not fields_cols:
        return None
    fields_col = fields_cols[0]
    lines = _split_lines(text)
    label_col = None
    for cname, start, end in _column_blocks(lines):
        for j in range(start + 1, end):
            s = lines[j].strip()
            if s.startswith("groupByColumn:") and \
                    _unquote(s.split(":", 1)[1].strip()) == fields_col:
                label_col = cname
    sort_raw = column_property(text, fields_col, "sortByColumn")
    return {
        "table": table_name(text),
        "description": table_description(text),
        "label_column": label_col,
        "fields_column": fields_col,
        "order_column": _unquote(sort_raw) if sort_raw else None,
        "fields": parse_field_tuples(partition_source(text) or ""),
    }


_SERIES = re.compile(
    r"GENERATESERIES\s*\(\s*([^,()]+?)\s*,\s*([^,()]+?)\s*,\s*([^,()]+?)\s*\)",
    re.IGNORECASE)


def _number(s: str) -> float | int | None:
    s = s.strip()
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        return None


def parse_whatif_parameter(text: str, measures: list) -> dict | None:
    """Describe a what-if table file, or None if it is not one.

    ``measures``: the table's parsed ``Measure`` objects (``.name`` / ``.dax``).
    """
    meta = column_parameter_metadata(text)
    cols = [c for c, m in meta.items()
            if "kind" not in m and m.get("version") == 0]
    if not cols:
        return None
    col = cols[0]
    tname = table_name(text)
    src = partition_source(text) or ""
    m = _SERIES.search(src)
    lo, hi, inc = ((_number(g) for g in m.groups()) if m else (None, None, None))
    sel = re.compile(
        r"SELECTEDVALUE\s*\(\s*(?:'(?:[^']|'')+'|[A-Za-z_]\w*)\s*"
        r"\[" + re.escape(col.replace("]", "]]")) + r"\]\s*(?:,\s*(.+?))?\s*\)\s*$",
        re.IGNORECASE | re.DOTALL)
    measure = default = None
    for ms in measures:
        mm = sel.search(ms.dax.strip())
        if mm:
            measure = ms.name
            default = _number(mm.group(1)) if mm.group(1) else None
            break
    return {
        "table": tname,
        "description": table_description(text),
        "column": col,
        "minimum": lo, "maximum": hi, "increment": inc,
        "measure": measure,
        "default": default,
        "format_string": column_property(text, col, "formatString"),
        "source": src.strip(),
    }
