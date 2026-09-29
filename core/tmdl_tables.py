"""TMDL emitters and surgical editors for tables, partitions, shared
expressions and incremental-refresh policies.

Shapes follow the TMDL reference
(https://learn.microsoft.com/analysis-services/tmdl/tmdl-overview) and the
synthetic fixture, which mirrors what Power BI Desktop writes:

    /// Optional description, directly above the declaration
    table Sales                             # indent 0: object
        lineageTag: <guid>                  # indent 1: table property

        refreshPolicy                       # indent 1: nameless child object
            policyType: basic
            rollingWindowGranularity: year
            ...
            sourceExpression =              # multi-line expression: two
                    let                     # levels deeper than the property
                    ...

        column Amount                       # indent 1: member
            dataType: double                # indent 2: member property
            lineageTag: <guid>
            summarizeBy: sum
            sourceColumn: Amount

        partition Sales = m
            mode: import
            source =
                    let
                        Source = ...
                    in
                        Source

        annotation PBI_ResultType = Table

    expression Server = "localhost" meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]
        lineageTag: <guid>

        annotation PBI_NavigationStepName = Navigation

        annotation PBI_ResultType = Text

`ref table <Name>` lines in model.tmdl exist only for collections that are
serialized one-object-per-file (tables, roles, cultures, perspectives);
expressions.tmdl and relationships.tmdl are single files and get no refs.

Everything here is text-level: emitters build new files, editors splice into
existing text and leave every other byte alone. The parsed model is never
re-emitted, so partitions, annotations and M source survive untouched.
"""

from __future__ import annotations

import re
import textwrap
import uuid

from core.tmdl import (
    _dedent, _first_member_header, _leading_tabs, _member_block_end,
    _unquote, quote_ident,
)

_FENCE = "```"

DATA_TYPES = ("string", "int64", "double", "decimal", "dateTime", "boolean",
              "binary", "variant", "automatic", "unknown")
SUMMARIZE_BY = ("default", "none", "sum", "min", "max", "count", "average",
                "distinctCount")
GRANULARITIES = ("day", "month", "quarter", "year")
REFRESH_MODES = ("import", "hybrid")
EXPRESSION_KINDS = ("query", "parameter")

_INVALID_FILE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_META_SUFFIX = re.compile(r"^(?P<expr>.*?)\s+meta\s+(?P<meta>\[.*\])\s*$",
                          re.DOTALL)


def new_lineage_tag() -> str:
    return str(uuid.uuid4())


def _canonical(value: str, allowed: tuple[str, ...], what: str) -> str:
    """Case-insensitive lookup of an enum value; ValueError with the choices."""
    for option in allowed:
        if option.lower() == str(value).strip().lower():
            return option
    raise ValueError(f"Unknown {what} {value!r}; expected one of {list(allowed)}")


def table_file_name(name: str) -> str:
    """`tables/<name>.tmdl`; characters a file name can't hold become `_`."""
    safe = _INVALID_FILE_CHARS.sub("_", name).strip().rstrip(".")
    if not safe:
        raise ValueError(f"Table name {name!r} cannot be used as a file name")
    return f"{safe}.tmdl"


# --- expression rendering / reading -------------------------------------------

def _normalize_expression(expr: str) -> str:
    """Common indentation removed, line endings unified, trailing blanks cut."""
    text = str(expr).replace("\r\n", "\n").replace("\r", "\n")
    text = textwrap.dedent(text)
    lines = [ln.rstrip() for ln in text.split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def expression_lines(expr: str, indent: int) -> list[str]:
    """Body lines of a multi-line expression at `indent` tabs.

    Relative indentation is kept, blank lines stay empty (TMDL allows them
    inside an expression and trims whitespace-only lines to empty anyway).
    """
    body = _normalize_expression(expr)
    return [("\t" * indent + ln) if ln else "" for ln in body.split("\n")]


def default_property_lines(header: str, expr: str, header_indent: int,
                           suffix: str = "") -> list[str]:
    """`<header> = expr` inline for a one-liner, else `<header> =` + block.

    `header` is the already-indented text before the `=` (e.g. "\\t\\tsource"
    or "expression Name"); `suffix` (a `meta [...]` record) is only legal on
    a single-line value.
    """
    body = _normalize_expression(expr)
    if not body:
        raise ValueError("Expression body must not be empty")
    if "\n" in body:
        if suffix:
            raise ValueError("A `meta` record needs a single-line expression")
        return [f"{header} ="] + expression_lines(body, header_indent + 2)
    return [f"{header} = {body}{(' ' + suffix) if suffix else ''}"]


def _read_expression(lines: list[str], header_idx: int, inline: str,
                     min_indent: int) -> tuple[str, int, str]:
    """Value of an expression whose header is `lines[header_idx]`.

    `inline` is the text after `=` on the header line. Returns
    (expression text, exclusive end index of the body, form) where form is
    "inline", "block" or "fenced". A block body is every following line
    indented at least `min_indent` tabs (blank lines allowed); trailing blank
    lines are not part of it.
    """
    n = len(lines)
    if inline.startswith(_FENCE):
        j = header_idx + 1
        block: list[str] = []
        while j < n and lines[j].strip() != _FENCE:
            block.append(lines[j])
            j += 1
        return _dedent(block), min(j + 1, n), "fenced"
    if inline:
        return inline, header_idx + 1, "inline"
    j = header_idx + 1
    last = header_idx
    while j < n:
        if not lines[j].strip():
            j += 1
            continue
        if _leading_tabs(lines[j]) < min_indent:
            break
        last = j
        j += 1
    return _dedent(lines[header_idx + 1:last + 1]), last + 1, "block"


def _split_header(header: str) -> tuple[str, str | None]:
    """Split `Name = rest` honouring a quoted name ('A = B' = 1)."""
    header = header.strip()
    if header.startswith("'"):
        j = 1
        while j < len(header):
            if header[j] == "'":
                if j + 1 < len(header) and header[j + 1] == "'":
                    j += 2
                    continue
                break
            j += 1
        name = _unquote(header[:j + 1])
        rest = header[j + 1:].strip()
        if rest.startswith("="):
            return name, rest[1:].strip()
        return name, None
    name_part, sep, rest = header.partition("=")
    if not sep:
        return name_part.strip(), None
    return name_part.strip(), rest.strip()


def _prop_value(value: str) -> str:
    """Quote a property value only when TMDL would otherwise mangle it."""
    s = str(value)
    if s != s.strip() or s.startswith('"') or s.endswith('"'):
        return '"' + s.replace('"', '""') + '"'
    return s


def _description_lines(description: str | None, indent: int) -> list[str]:
    if not description:
        return []
    out = []
    for raw in str(description).replace("\r\n", "\n").split("\n"):
        out.append("\t" * indent + "/// " + raw.strip() if raw.strip()
                   else "\t" * indent + "///")
    return out


# --- M meta records -------------------------------------------------------------

def render_m_value(value) -> str:
    """Python -> M literal: bool, None, numbers, str (quoted), list ({..}),
    or {"m": "<raw M>"} to pass an expression through untouched."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict) and set(value) == {"m"}:
        return str(value["m"])
    if isinstance(value, (list, tuple)):
        return "{" + ", ".join(render_m_value(v) for v in value) + "}"
    return '"' + str(value).replace('"', '""') + '"'


def render_meta(meta: dict) -> str:
    return "[" + ", ".join(f"{k}={render_m_value(v)}" for k, v in meta.items()) + "]"


def _split_top_level(s: str, sep: str = ",") -> list[str]:
    parts, depth, buf, in_str = [], 0, [], False
    i = 0
    while i < len(s):
        ch = s[i]
        if in_str:
            buf.append(ch)
            if ch == '"':
                if i + 1 < len(s) and s[i + 1] == '"':
                    buf.append('"')
                    i += 1
                else:
                    in_str = False
        elif ch == '"':
            in_str = True
            buf.append(ch)
        elif ch in "[{(":
            depth += 1
            buf.append(ch)
        elif ch in "]})":
            depth -= 1
            buf.append(ch)
        elif ch == sep and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    if buf:
        parts.append("".join(buf))
    return parts


def _parse_m_value(raw: str):
    raw = raw.strip()
    if raw in ("true", "false"):
        return raw == "true"
    if raw == "null":
        return None
    if len(raw) >= 2 and raw.startswith('"') and raw.endswith('"'):
        return raw[1:-1].replace('""', '"')
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    if re.fullmatch(r"-?\d+\.\d+", raw):
        return float(raw)
    if raw.startswith("{") and raw.endswith("}"):
        return [_parse_m_value(p) for p in _split_top_level(raw[1:-1]) if p.strip()]
    return {"m": raw}


def parse_meta(meta_text: str) -> dict:
    """`[IsParameterQuery=true, Type="Text"]` -> {"IsParameterQuery": True, ...}."""
    inner = meta_text.strip()
    if inner.startswith("[") and inner.endswith("]"):
        inner = inner[1:-1]
    out: dict = {}
    for part in _split_top_level(inner):
        if "=" not in part:
            continue
        key, _, val = part.partition("=")
        out[key.strip()] = _parse_m_value(val)
    return out


def split_meta(inline: str) -> tuple[str, str | None]:
    """Separate a trailing `meta [...]` record from an inline M expression."""
    m = _META_SUFFIX.match(inline)
    if m:
        return m.group("expr").strip(), m.group("meta").strip()
    return inline.strip(), None


def infer_parameter_type(m: str) -> str:
    """Power Query parameter `Type` implied by the value's M literal."""
    v = m.strip()
    if v.startswith('"'):
        return "Text"
    if v.startswith("#datetimezone"):
        return "DateTimeZone"
    if v.startswith("#datetime"):
        return "DateTime"
    if v.startswith("#date"):
        return "Date"
    if v.startswith("#time"):
        return "Time"
    if v.startswith("#duration"):
        return "Duration"
    if v in ("true", "false"):
        return "Logical"
    if re.fullmatch(r"-?\d+(\.\d+)?([eE][+-]?\d+)?", v):
        return "Number"
    if v.startswith("#binary"):
        return "Binary"
    return "Any"


# --- columns --------------------------------------------------------------------

def normalize_column_spec(col: dict) -> dict:
    """Validate a column spec dict and fill defaults (raises ValueError)."""
    if not isinstance(col, dict):
        raise ValueError(f"Column spec must be an object, got {col!r}")
    name = str(col.get("name") or "").strip()
    if not name:
        raise ValueError(f"Column spec needs a non-empty 'name': {col!r}")
    if not col.get("data_type"):
        raise ValueError(f"Column {name!r} needs a 'data_type' "
                         f"(one of {list(DATA_TYPES)})")
    out = {
        "name": name,
        "data_type": _canonical(col["data_type"], DATA_TYPES, "data_type"),
        "summarize_by": _canonical(col.get("summarize_by") or "none",
                                   SUMMARIZE_BY, "summarize_by"),
        "source_column": str(col.get("source_column") or name),
        "format_string": col.get("format_string") or None,
        "is_hidden": bool(col.get("is_hidden", False)),
        "data_category": col.get("data_category") or None,
        "description": col.get("description") or None,
        "lineage_tag": col.get("lineage_tag") or None,
    }
    return out


def emit_column_lines(col: dict) -> list[str]:
    """Tab-indented lines for a data column member (indent 1), Desktop order:
    dataType, formatString, isHidden, lineageTag, dataCategory, summarizeBy,
    sourceColumn."""
    c = normalize_column_spec(col)
    lines = _description_lines(c["description"], indent=1)
    lines.append(f"\tcolumn {quote_ident(c['name'])}")
    lines.append(f"\t\tdataType: {c['data_type']}")
    if c["format_string"]:
        lines.append(f"\t\tformatString: {_prop_value(c['format_string'])}")
    if c["is_hidden"]:
        lines.append("\t\tisHidden")
    lines.append(f"\t\tlineageTag: {c['lineage_tag'] or new_lineage_tag()}")
    if c["data_category"]:
        lines.append(f"\t\tdataCategory: {c['data_category']}")
    lines.append(f"\t\tsummarizeBy: {c['summarize_by']}")
    lines.append(f"\t\tsourceColumn: {_prop_value(c['source_column'])}")
    return lines


# --- table files ------------------------------------------------------------------

def _table_head(name: str, description: str | None, hidden: bool,
                lineage_tag: str | None) -> list[str]:
    out = _description_lines(description, indent=0)
    out.append(f"table {quote_ident(name)}")
    if hidden:
        out.append("\tisHidden")
    out.append(f"\tlineageTag: {lineage_tag or new_lineage_tag()}")
    return out


def emit_table_file(name: str, m_source: str, columns: list[dict] | None = None,
                    description: str | None = None, hidden: bool = False,
                    lineage_tag: str | None = None) -> str:
    """Full `tables/<name>.tmdl` text for an import table fed by an M query:
    table header (+lineageTag, isHidden), columns, one `partition <name> = m`
    with `mode: import` and the `source =` block, then the
    `PBI_NavigationStepName` / `PBI_ResultType = Table` annotations Desktop
    writes."""
    if not str(name).strip():
        raise ValueError("Table name must not be empty")
    specs = [normalize_column_spec(c) for c in (columns or [])]
    seen: set[str] = set()
    for c in specs:
        key = c["name"].casefold()
        if key in seen:
            raise ValueError(f"Duplicate column {c['name']!r} in the column list")
        seen.add(key)

    out = _table_head(name, description, hidden, lineage_tag)
    for c in specs:
        out.append("")
        out.extend(emit_column_lines(c))
    out.append("")
    out.append(f"\tpartition {quote_ident(name)} = m")
    out.append("\t\tmode: import")
    out.extend(default_property_lines("\t\tsource", m_source, header_indent=2))
    out.append("")
    out.append("\tannotation PBI_NavigationStepName = Navigation")
    out.append("")
    out.append("\tannotation PBI_ResultType = Table")
    out.append("")
    return "\n".join(out)


def emit_calculated_table_file(name: str, dax: str,
                               description: str | None = None,
                               hidden: bool = False,
                               lineage_tag: str | None = None) -> str:
    """Full table file for a DAX calculated table: `partition <name> =
    calculated`, `mode: import`, `source =` holding the DAX. No columns are
    declared — Desktop infers them from the expression on load — plus the
    `PBI_Id` annotation Desktop adds to calculated tables."""
    if not str(name).strip():
        raise ValueError("Table name must not be empty")
    out = _table_head(name, description, hidden, lineage_tag)
    out.append("")
    out.append(f"\tpartition {quote_ident(name)} = calculated")
    out.append("\t\tmode: import")
    out.extend(default_property_lines("\t\tsource", dax, header_indent=2))
    out.append("")
    out.append(f"\tannotation PBI_Id = {uuid.uuid4().hex}")
    out.append("")
    return "\n".join(out)


# --- partitions -----------------------------------------------------------------------

def _scan_partitions(lines: list[str]) -> list[dict]:
    out: list[dict] = []
    for idx, line in enumerate(lines):
        if _leading_tabs(line) != 1 or not line.strip().startswith("partition "):
            continue
        name, kind = _split_header(line.strip()[len("partition "):])
        end = _member_block_end(lines, idx)
        part = {"name": name, "kind": (kind or "").strip() or None,
                "mode": None, "source": None, "source_form": None,
                "properties": {}, "_header": idx, "_end": end,
                "_source_line": None, "_source_end": None}
        j = idx + 1
        while j < end:
            cur = lines[j]
            if not cur.strip() or _leading_tabs(cur) != 2:
                j += 1
                continue
            s = cur.strip()
            if s == "source =" or s.startswith("source = ") or s == "source=":
                inline = s.partition("=")[2].strip()
                body, body_end, form = _read_expression(lines, j, inline, 3)
                part.update(source=body, source_form=form,
                            _source_line=j, _source_end=body_end)
                j = body_end
                continue
            if ":" in s:
                key, _, val = s.partition(":")
                part["properties"][key.strip()] = val.strip()
                if key.strip() == "mode":
                    part["mode"] = val.strip()
            else:
                part["properties"][s] = True
            j += 1
        out.append(part)
    return out


def parse_partitions_text(text: str) -> list[dict]:
    """Partitions declared in table text: [{name, kind, mode, source,
    source_form, properties}]. `kind` is the word after `=` (m, calculated,
    entity, calculationGroup, ...); `source` is the M/DAX text (None for
    entity partitions, which carry `entityName` in `properties`)."""
    lines = text.replace("\r\n", "\n").split("\n")
    return [{k: v for k, v in p.items() if not k.startswith("_")}
            for p in _scan_partitions(lines)]


def replace_partition_source_text(text: str, new_source: str,
                                  partition: str | None = None) -> tuple[str, dict]:
    """Swap the `source =` expression of one partition; every other byte of
    the file is kept. With `partition=None` the table must have exactly one
    partition. Returns (new text, partition info)."""
    lines = text.split("\n")
    parts = _scan_partitions(lines)
    if not parts:
        raise ValueError("The table declares no partitions")
    if partition is None:
        if len(parts) > 1:
            names = [p["name"] for p in parts]
            raise ValueError(f"The table has {len(parts)} partitions {names}; "
                             f"pass partition=<name> to pick one")
        target = parts[0]
    else:
        matches = [p for p in parts if p["name"] == partition]
        if not matches:
            names = [p["name"] for p in parts]
            raise KeyError(f"Partition {partition!r} not found; the table has {names}")
        target = matches[0]
    if target["_source_line"] is None:
        raise ValueError(f"Partition {target['name']!r} ({target['kind']}) has no "
                         f"`source =` expression to replace")
    new_lines = default_property_lines("\t\tsource", new_source, header_indent=2)
    lines[target["_source_line"]:target["_source_end"]] = new_lines
    info = {k: v for k, v in target.items() if not k.startswith("_")}
    return "\n".join(lines), info


# --- calculated columns / DAX in a table file --------------------------------------------

def calculated_column_expressions_text(text: str) -> list[tuple[str, str]]:
    """[(column name, DAX)] for every `column X = <dax>` in table text."""
    lines = text.replace("\r\n", "\n").split("\n")
    out: list[tuple[str, str]] = []
    for idx, line in enumerate(lines):
        if _leading_tabs(line) != 1 or not line.strip().startswith("column "):
            continue
        name, inline = _split_header(line.strip()[len("column "):])
        if inline is None:
            continue
        dax, _, _ = _read_expression(lines, idx, inline, 3)
        out.append((name, dax))
    return out


# --- refresh policy ----------------------------------------------------------------------

def normalize_refresh_policy(policy: dict) -> dict:
    """Validate/normalise a refresh-policy spec (raises ValueError)."""
    def _periods(key: str) -> int:
        val = policy.get(key)
        if isinstance(val, bool) or not isinstance(val, int) or val < 1:
            raise ValueError(f"{key} must be a positive integer, got {val!r}")
        return val
    offset = policy.get("incremental_periods_offset", 0) or 0
    if isinstance(offset, bool) or not isinstance(offset, int):
        raise ValueError(f"incremental_periods_offset must be an integer, got {offset!r}")
    source = _normalize_expression(policy.get("source_expression") or "")
    if not source:
        raise ValueError("source_expression (M) is required")
    missing = [p for p in ("RangeStart", "RangeEnd")
               if not re.search(rf"\b{p}\b", source)]
    if missing:
        raise ValueError(
            f"source_expression must filter on the RangeStart and RangeEnd "
            f"parameters (missing {missing}), e.g. Table.SelectRows(Source, "
            f"each [OrderDate] >= RangeStart and [OrderDate] < RangeEnd)")
    return {
        "rolling_window_granularity": _canonical(
            policy["rolling_window_granularity"], GRANULARITIES,
            "rolling_window_granularity"),
        "rolling_window_periods": _periods("rolling_window_periods"),
        "incremental_granularity": _canonical(
            policy["incremental_granularity"], GRANULARITIES,
            "incremental_granularity"),
        "incremental_periods": _periods("incremental_periods"),
        "incremental_periods_offset": offset,
        "mode": _canonical(policy.get("mode") or "import", REFRESH_MODES, "mode"),
        "source_expression": source,
    }


def emit_refresh_policy_lines(policy: dict) -> list[str]:
    """Tab-indented `refreshPolicy` block (indent 1) for a table."""
    p = normalize_refresh_policy(policy)
    lines = [
        "\trefreshPolicy",
        "\t\tpolicyType: basic",
        f"\t\trollingWindowGranularity: {p['rolling_window_granularity']}",
        f"\t\trollingWindowPeriods: {p['rolling_window_periods']}",
        f"\t\tincrementalGranularity: {p['incremental_granularity']}",
        f"\t\tincrementalPeriods: {p['incremental_periods']}",
    ]
    if p["incremental_periods_offset"]:
        lines.append(f"\t\tincrementalPeriodsOffset: {p['incremental_periods_offset']}")
    if p["mode"] == "hybrid":
        lines.append("\t\tmode: hybrid")
    lines.extend(default_property_lines("\t\tsourceExpression",
                                        p["source_expression"], header_indent=2))
    return lines


def _find_refresh_policy(lines: list[str]) -> int | None:
    for idx, line in enumerate(lines):
        if _leading_tabs(line) == 1 and line.strip() == "refreshPolicy":
            return idx
    return None


def _member_insert_point(lines: list[str]) -> int | None:
    """Index of the first member header, backed up over its `///` lines."""
    first = _first_member_header(lines)
    if first is None:
        return None
    while first > 0 and lines[first - 1].strip().startswith("///"):
        first -= 1
    return first


def set_refresh_policy_text(text: str, policy: dict) -> str:
    """Insert (before the first member, where Desktop keeps it) or replace the
    table's `refreshPolicy` block; nothing else changes."""
    lines = text.split("\n")
    block = emit_refresh_policy_lines(policy)
    idx = _find_refresh_policy(lines)
    if idx is not None:
        lines[idx:_member_block_end(lines, idx)] = block
        return "\n".join(lines)
    at = _member_insert_point(lines)
    if at is not None:
        lines[at:at] = block + [""]
        return "\n".join(lines)
    trailing = 0
    while lines and not lines[-1].strip():
        lines.pop()
        trailing += 1
    lines += [""] + block + [""] * max(trailing, 1)
    return "\n".join(lines)


def remove_refresh_policy_text(text: str) -> str:
    """Delete the `refreshPolicy` block (and its blank separator)."""
    lines = text.split("\n")
    idx = _find_refresh_policy(lines)
    if idx is None:
        raise KeyError("The table has no refreshPolicy block")
    end = _member_block_end(lines, idx)
    del lines[idx:end]
    # collapse the doubled blank line the removal leaves behind
    if idx < len(lines) and not lines[idx].strip() and (
            idx == 0 or not lines[idx - 1].strip() or idx == len(lines) - 1):
        del lines[idx]
    return "\n".join(lines)


def parse_refresh_policy_text(text: str) -> dict | None:
    """The table's refreshPolicy as a dict (TMDL property names as keys,
    `sourceExpression`/`pollingExpression` as text), or None."""
    lines = text.replace("\r\n", "\n").split("\n")
    idx = _find_refresh_policy(lines)
    if idx is None:
        return None
    end = _member_block_end(lines, idx)
    out: dict = {}
    j = idx + 1
    while j < end:
        cur = lines[j]
        if not cur.strip() or _leading_tabs(cur) != 2:
            j += 1
            continue
        s = cur.strip()
        key, sep, inline = s.partition("=")
        if sep and key.strip() in ("sourceExpression", "pollingExpression"):
            body, body_end, _ = _read_expression(lines, j, inline.strip(), 3)
            out[key.strip()] = body
            j = body_end
            continue
        if ":" in s:
            key, _, val = s.partition(":")
            val = val.strip()
            out[key.strip()] = int(val) if re.fullmatch(r"-?\d+", val) else val
        else:
            out[s] = True
        j += 1
    return out


# --- model.tmdl / relationships.tmdl -----------------------------------------------------

def remove_table_ref_text(model_text: str, table: str) -> str:
    """Drop the `ref table <table>` line from model.tmdl text (if present)."""
    lines = model_text.split("\n")
    kept = [ln for ln in lines
            if not (ln.strip().startswith("ref table ")
                    and _unquote(ln.strip()[len("ref table "):]) == table)]
    return "\n".join(kept)


def _ref_table(col_ref: str) -> str:
    """Table part of a `Table.Column` / `'Tbl'.'Col'` reference."""
    ref = col_ref.strip()
    if ref.startswith("'"):
        name, _ = _split_header(ref)
        return name
    return ref.partition(".")[0].strip()


def remove_relationships_for_table_text(text: str, table: str) -> tuple[str, list[dict]]:
    """Remove every relationship block whose fromColumn/toColumn table is
    `table`. Returns (new text, [{name, from, to}] removed)."""
    lines = text.split("\n")
    n = len(lines)
    removed: list[dict] = []
    keep: list[str] = []
    i = 0
    while i < n:
        line = lines[i]
        if _leading_tabs(line) == 0 and line.strip().startswith("relationship "):
            start = i
            while start > 0 and lines[start - 1].strip().startswith("///") \
                    and _leading_tabs(lines[start - 1]) == 0:
                start -= 1
            # descriptions already copied into keep: pull them back out
            del keep[len(keep) - (i - start):]
            j = i + 1
            while j < n and (not lines[j].strip() or _leading_tabs(lines[j]) > 0):
                j += 1
            block = lines[i:j]
            props = {}
            for b in block[1:]:
                s = b.strip()
                if ":" in s:
                    k, _, v = s.partition(":")
                    props[k.strip()] = v.strip()
            frm, to = props.get("fromColumn", ""), props.get("toColumn", "")
            if table.casefold() in (_ref_table(frm).casefold(), _ref_table(to).casefold()):
                removed.append({"name": line.strip()[len("relationship "):].strip(),
                                "from": frm, "to": to})
            else:
                keep.extend(lines[start:j])
            i = j
            continue
        keep.append(line)
        i += 1
    if not any(ln.strip() for ln in keep):
        return "", removed
    result = "\n".join(keep)
    if text.endswith("\n") and not result.endswith("\n"):
        result += "\n"
    return result, removed


# --- expressions.tmdl ------------------------------------------------------------------------

def _scan_expressions(lines: list[str]) -> list[dict]:
    out: list[dict] = []
    n = len(lines)
    i = 0
    desc: list[str] = []
    desc_start: int | None = None
    while i < n:
        line = lines[i]
        s = line.strip()
        if not s:
            desc, desc_start = [], None
            i += 1
            continue
        if _leading_tabs(line) != 0:
            i += 1
            continue
        if s.startswith("///"):
            if desc_start is None:
                desc_start = i
            desc.append(s[3:].strip())
            i += 1
            continue
        if not s.startswith("expression "):
            desc, desc_start = [], None
            i += 1
            continue

        header_idx = i
        name, inline = _split_header(s[len("expression "):])
        inline = inline or ""
        meta_raw: str | None = None
        if inline and not inline.startswith(_FENCE):
            inline, meta_raw = split_meta(inline)
        body, body_end, form = _read_expression(lines, i, inline, 2)
        j = body_end
        props: dict = {}
        annotations: dict = {}
        last = body_end - 1 if body_end > header_idx + 1 else header_idx
        while j < n:
            cur = lines[j]
            if not cur.strip():
                j += 1
                continue
            if _leading_tabs(cur) == 0:
                break
            s2 = cur.strip()
            if s2.startswith("annotation "):
                k, _, v = s2[len("annotation "):].partition("=")
                annotations[k.strip()] = v.strip()
            elif ":" in s2:
                k, _, v = s2.partition(":")
                props[k.strip()] = v.strip()
            else:
                props[s2] = True
            last = j
            j += 1
        meta = parse_meta(meta_raw) if meta_raw else None
        out.append({
            "name": name,
            "kind": "parameter" if meta and meta.get("IsParameterQuery") else "query",
            "m": body,
            "meta": meta,
            "meta_raw": meta_raw,
            "lineage_tag": props.get("lineageTag"),
            "query_group": props.get("queryGroup"),
            "description": "\n".join(desc) if desc else None,
            "result_type": annotations.get("PBI_ResultType"),
            "annotations": annotations,
            "properties": props,
            "_start": desc_start if desc_start is not None else header_idx,
            "_header": header_idx,
            "_body_end": body_end,
            "_form": form,
            "_end": last + 1,
        })
        desc, desc_start = [], None
        i = j
    return out


def parse_expressions_text(text: str) -> list[dict]:
    """Shared expressions in expressions.tmdl text: [{name, kind
    ("parameter" when the meta record says IsParameterQuery=true, else
    "query"), m, meta (dict|None), lineage_tag, query_group, description,
    result_type, annotations, properties}]."""
    lines = text.replace("\r\n", "\n").split("\n")
    return [{k: v for k, v in e.items() if not k.startswith("_")}
            for e in _scan_expressions(lines)]


def emit_expression_block(name: str, m: str, kind: str = "query",
                          description: str | None = None,
                          parameter_meta: dict | None = None,
                          result_type: str | None = None,
                          lineage_tag: str | None = None) -> list[str]:
    """Lines for one shared expression, the way Desktop writes it.

    kind="parameter": single-line value + `meta [IsParameterQuery=true,
    Type=<inferred or given>, IsParameterQueryRequired=true]`, merged with
    `parameter_meta`; result type = the parameter Type.
    kind="query": the M (multi-line block when needed); result type
    "Function" for a `(x) => ...` value, else "Table".
    """
    kind = _canonical(kind, EXPRESSION_KINDS, "kind")
    if not str(name).strip():
        raise ValueError("Expression name must not be empty")
    body = _normalize_expression(m)
    if not body:
        raise ValueError("Expression body (m) must not be empty")
    suffix = ""
    if kind == "parameter":
        if "\n" in body:
            raise ValueError("A parameter value must be a single-line M literal, "
                             "e.g. \"text\", 42, true, #datetime(2024, 1, 1, 0, 0, 0)")
        meta = {"IsParameterQuery": True, "Type": infer_parameter_type(body),
                "IsParameterQueryRequired": True}
        meta.update(parameter_meta or {})
        meta["IsParameterQuery"] = True
        suffix = "meta " + render_meta(meta)
        result_type = result_type or str(meta.get("Type") or "Any")
    else:
        if parameter_meta:
            raise ValueError("parameter_meta only applies to kind='parameter'")
        if result_type is None:
            first = body.split("\n", 1)[0].lstrip()
            result_type = "Function" if first.startswith("(") and "=>" in body else "Table"

    out = _description_lines(description, indent=0)
    out.extend(default_property_lines(f"expression {quote_ident(name)}", body,
                                      header_indent=0, suffix=suffix))
    out.append(f"\tlineageTag: {lineage_tag or new_lineage_tag()}")
    out.append("")
    out.append("\tannotation PBI_NavigationStepName = Navigation")
    out.append("")
    out.append(f"\tannotation PBI_ResultType = {result_type}")
    return out


def append_expression_text(text: str, block: list[str]) -> str:
    """Append an expression block to expressions.tmdl text (blank-line
    separated, trailing newline kept)."""
    lines = text.split("\n") if text else []
    while lines and not lines[-1].strip():
        lines.pop()
    if lines:
        lines.append("")
    lines.extend(block)
    lines.append("")
    return "\n".join(lines)


def replace_expression_body_text(text: str, name: str, m: str) -> tuple[str, dict]:
    """Replace one expression's M, keeping its meta record, lineageTag,
    description and annotations byte for byte."""
    lines = text.split("\n")
    matches = [e for e in _scan_expressions(lines) if e["name"] == name]
    if not matches:
        names = [e["name"] for e in _scan_expressions(lines)]
        raise KeyError(f"Expression {name!r} not found; expressions.tmdl has {names}")
    e = matches[0]
    suffix = f"meta {e['meta_raw']}" if e["meta_raw"] else ""
    new_lines = default_property_lines(f"expression {quote_ident(name)}", m,
                                       header_indent=0, suffix=suffix)
    lines[e["_header"]:e["_body_end"]] = new_lines
    return "\n".join(lines), {k: v for k, v in e.items() if not k.startswith("_")}
