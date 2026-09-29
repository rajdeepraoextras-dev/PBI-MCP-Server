"""Best Practice Analyzer (BPA) for Power BI semantic models.

A rules engine over the parsed model: tables, columns (with every property
read from the TMDL text), measures, relationships, calculation groups,
partitions, roles, report usage (``core.usage``) and DAX references
(``core.dax_parser``).

The rule catalog lives in ``resources/bpa_rules.json``. Most rules are ports of
the Tabular Editor / Microsoft ``BPARules.json`` standard rules (same IDs,
categories and severities; severity 3 = most severe). A few are pbi-mcp
additions. Every catalog entry is bound to a Python check registered with
``@check("RULE_ID")`` below; ``tests/test_bpa.py`` asserts the two stay in sync.

Findings are plain dicts ``{rule_id, category, severity, object_type, table,
name, message, fixable}``. A handful of rules have a *safe fixer*: a surgical,
idempotent line insertion into the table's TMDL file (default format string on
numeric measures, ``isHidden`` on foreign keys, ``dataCategory`` on columns
whose names clearly imply it). Nothing else in the file is touched.

Custom rules: ``<project root>/.pbi-mcp/bpa_rules.json`` may hold simple
pattern rules (see :func:`load_custom_rules`).
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from core.dax_parser import KEYWORDS, Token, parse_references, tokenize

CATEGORIES = ("Performance", "DAX Expressions", "Error Prevention",
              "Maintenance", "Naming Conventions", "Formatting")
RESOURCE_PATH = Path(__file__).resolve().parent.parent / "resources" / "bpa_rules.json"
CUSTOM_RULES_FILE = Path(".pbi-mcp") / "bpa_rules.json"

_NUMERIC_TYPES = frozenset({"int64", "double", "decimal"})
_TIME_INTELLIGENCE = frozenset({
    "CLOSINGBALANCEMONTH", "CLOSINGBALANCEQUARTER", "CLOSINGBALANCEYEAR",
    "DATEADD", "DATESBETWEEN", "DATESINPERIOD", "DATESMTD", "DATESQTD",
    "DATESYTD", "ENDOFMONTH", "ENDOFQUARTER", "ENDOFYEAR", "FIRSTDATE",
    "FIRSTNONBLANK", "FIRSTNONBLANKVALUE", "LASTDATE", "LASTNONBLANK",
    "LASTNONBLANKVALUE", "NEXTDAY", "NEXTMONTH", "NEXTQUARTER", "NEXTYEAR",
    "OPENINGBALANCEMONTH", "OPENINGBALANCEQUARTER", "OPENINGBALANCEYEAR",
    "PARALLELPERIOD", "PREVIOUSDAY", "PREVIOUSMONTH", "PREVIOUSQUARTER",
    "PREVIOUSYEAR", "SAMEPERIODLASTYEAR", "STARTOFMONTH", "STARTOFQUARTER",
    "STARTOFYEAR", "TOTALMTD", "TOTALQTD", "TOTALYTD",
})


# =============================================================================
# 1. TMDL structure reader
# =============================================================================
# Indentation-based (tabs). Every object header becomes a Node that remembers
# its line range, its properties (with line numbers, for surgical edits), bare
# flags (isHidden, isKey ...), `///` description lines and expression body.

_HEADERS = frozenset({
    "table", "column", "measure", "hierarchy", "level", "partition",
    "calculationGroup", "calculationItem", "role", "tablePermission",
    "relationship", "annotation", "perspective", "culture", "expression",
    "dataSource", "kpi", "model", "database", "ref", "extendedProperty",
    "changedProperty", "alternateOf", "variation", "function",
})
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_COLON_PROP = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$")
_EXPR_PROP = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
_FENCE = "```"


def _tabs(line: str) -> int:
    n = 0
    for ch in line:
        if ch != "\t":
            break
        n += 1
    return n


def unquote_name(name: str) -> str:
    """Strip TMDL single-quote quoting (``''`` is an escaped quote)."""
    name = name.strip()
    if len(name) >= 2 and name[0] == "'" and name[-1] == "'":
        return name[1:-1].replace("''", "'")
    return name


def _dedent(raw: list[str]) -> str:
    live = [ln for ln in raw if ln.strip()]
    if not live:
        return ""
    base = min(_tabs(ln) for ln in live)
    return "\n".join(ln[base:] if ln.strip() else "" for ln in raw).strip("\n")


def _split_name(rest: str) -> tuple[str, str]:
    """Split ``<name> [= ...]`` into (raw name, remainder)."""
    rest = rest.strip()
    if not rest:
        return "", ""
    if rest[0] == "'":
        i = 1
        while i < len(rest):
            if rest[i] == "'":
                if i + 1 < len(rest) and rest[i + 1] == "'":
                    i += 2
                    continue
                break
            i += 1
        return rest[:i + 1], rest[i + 1:].strip()
    m = re.match(r"[^\s=]+", rest)
    raw = m.group(0) if m else ""
    return raw, rest[len(raw):].strip()


@dataclass
class Prop:
    key: str
    value: str          # `key: value` -> value ; `key = expr` -> expression text
    line: int
    end: int            # exclusive line index
    is_expr: bool = False


@dataclass(eq=False)
class Node:
    kw: str
    name: str
    raw_name: str
    indent: int
    start: int                                   # header line index
    end: int = 0                                 # exclusive
    doc_start: int = 0                           # first `///` line (or start)
    doc: list[str] = field(default_factory=list)
    inline: str | None = None                    # text after `=` on the header
    fenced: bool = False
    body_end: int = 0                            # exclusive end of the header's expression
    expr: str = ""                               # header expression, dedented
    props: dict[str, Prop] = field(default_factory=dict)
    flags: dict[str, int] = field(default_factory=dict)
    children: list["Node"] = field(default_factory=list)
    parent: "Node | None" = None

    def kids(self, kw: str) -> list["Node"]:
        return [c for c in self.children if c.kw == kw]

    def kid(self, kw: str) -> "Node | None":
        return next((c for c in self.children if c.kw == kw), None)

    def prop(self, key: str) -> str | None:
        p = self.props.get(key)
        return p.value if p is not None else None

    def flag(self, key: str) -> bool:
        """Bare flag (``isHidden``) or ``isHidden: true``."""
        if key in self.flags:
            return True
        p = self.props.get(key)
        return p is not None and p.value.strip().lower() != "false"

    @property
    def description(self) -> str:
        if self.doc:
            return "\n".join(self.doc)
        return self.prop("description") or ""

    @property
    def is_calculated(self) -> bool:
        return self.inline is not None


def _take_body(lines: list[str], i: int, inline: str | None,
               min_indent: int) -> tuple[list[str], int, bool]:
    """Consume the expression lines that follow a header / `key =` line."""
    n = len(lines)
    if inline is not None and inline.startswith(_FENCE):
        body: list[str] = []
        first = inline[len(_FENCE):]
        if first.strip():
            if first.rstrip().endswith(_FENCE):
                return [first.rstrip()[:-len(_FENCE)]], i, True
            body.append(first)
        while i < n:
            ln = lines[i]
            i += 1
            if ln.strip() == _FENCE:
                break
            if ln.rstrip().endswith(_FENCE):
                body.append(ln.rstrip()[:-len(_FENCE)])
                break
            body.append(ln)
        return body, i, True
    j, last = i, None
    while j < n:
        ln = lines[j]
        if not ln.strip():
            j += 1
            continue
        if _tabs(ln) >= min_indent:
            last = j
            j += 1
            continue
        break
    if last is None:
        return [], i, False
    return lines[i:last + 1], last + 1, False


def _expression_text(inline: str | None, body: list[str], fenced: bool) -> str:
    if fenced:
        text = _dedent(body)
        return text
    parts = []
    if inline:
        parts.append(inline)
    tail = _dedent(body)
    if tail:
        parts.append(tail)
    return "\n".join(parts)


def _parse_block(lines: list[str], i: int, indent: int, parent: Node) -> int:
    n = len(lines)
    doc: list[tuple[int, str]] = []
    while i < n:
        raw = lines[i]
        s = raw.strip()
        if not s:
            i += 1
            continue
        ind = _tabs(raw)
        if ind < indent:
            return i
        if ind > indent:
            i += 1
            continue
        if s.startswith("///"):
            text = s[3:]
            doc.append((i, text[1:] if text.startswith(" ") else text))
            i += 1
            continue
        if s.startswith("//"):
            i += 1
            continue
        m = _WORD.match(s)
        word = m.group(0) if m else ""
        rest = s[len(word):]
        if word in _HEADERS and (rest == "" or rest[0] in " \t"):
            raw_name, after = _split_name(rest)
            if word == "ref":
                raw_name, after = rest.strip(), ""
            node = Node(word, unquote_name(raw_name), raw_name, indent, i,
                        parent=parent)
            node.doc = [t for _, t in doc]
            node.doc_start = doc[0][0] if doc else i
            doc = []
            if after.startswith("="):
                node.inline = after[1:].strip()
            i += 1
            body, i, node.fenced = _take_body(lines, i, node.inline, indent + 2)
            node.body_end = i
            node.expr = _expression_text(node.inline, body, node.fenced)
            i = _parse_block(lines, i, indent + 1, node)
            e = i
            while e > node.start + 1 and not lines[e - 1].strip():
                e -= 1
            node.end = e
            parent.children.append(node)
            continue
        doc = []
        pm = _COLON_PROP.match(s)
        if pm:
            parent.props[pm.group(1)] = Prop(pm.group(1), pm.group(2).strip(),
                                             i, i + 1)
            i += 1
            continue
        em = _EXPR_PROP.match(s)
        if em:
            key, inline = em.group(1), em.group(2).strip()
            start = i
            i += 1
            body, i, fenced = _take_body(lines, i, inline, ind + 1)
            parent.props[key] = Prop(key, _expression_text(inline, body, fenced),
                                     start, i, is_expr=True)
            continue
        if _WORD.fullmatch(s):
            parent.flags[s] = i
        i += 1
    return i


def parse_nodes(text: str) -> list[Node]:
    """Parse TMDL text into its top-level object nodes."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    root = Node("<root>", "", "", -1, -1)
    _parse_block(lines, 0, 0, root)
    return root.children


# =============================================================================
# 2. Model
# =============================================================================

@dataclass(eq=False)
class MColumn:
    table: "MTable"
    node: Node
    name: str
    data_type: str | None
    summarize_by: str | None
    data_category: str | None
    is_hidden: bool
    is_key: bool
    description: str
    format_string: str | None
    source_column: str | None
    sort_by: str | None
    expression: str | None           # None unless a calculated column
    col_type: str | None

    @property
    def is_calculated(self) -> bool:
        return self.expression is not None

    @property
    def dtype(self) -> str:
        return (self.data_type or "").lower()

    @property
    def is_numeric(self) -> bool:
        return self.dtype in _NUMERIC_TYPES

    @property
    def visible(self) -> bool:
        return not self.is_hidden and not self.table.is_hidden


@dataclass(eq=False)
class MMeasure:
    table: "MTable"
    node: Node
    name: str
    expression: str
    format_string: str | None
    has_format_expr: bool
    display_folder: str | None
    is_hidden: bool
    description: str

    @property
    def visible(self) -> bool:
        return not self.is_hidden and not self.table.is_hidden


@dataclass(eq=False)
class MPartition:
    table: "MTable"
    node: Node
    name: str
    kind: str                        # m | calculated | entity | query | calculationGroup ...
    mode: str | None
    source: str

    @property
    def is_m(self) -> bool:
        return self.kind.lower() == "m"

    @property
    def is_calculated(self) -> bool:
        return self.kind.lower() == "calculated"

    @property
    def is_direct_query(self) -> bool:
        return (self.mode or "").lower() == "directquery"


@dataclass(eq=False)
class MCalcItem:
    table: "MTable"
    node: Node
    name: str
    expression: str


@dataclass(eq=False)
class MTable:
    name: str
    node: Node
    file: Path
    is_hidden: bool
    data_category: str | None
    description: str
    columns: list[MColumn] = field(default_factory=list)
    measures: list[MMeasure] = field(default_factory=list)
    partitions: list[MPartition] = field(default_factory=list)
    hierarchies: list[tuple[str, list[str]]] = field(default_factory=list)
    calc_items: list[MCalcItem] = field(default_factory=list)
    is_calc_group: bool = False
    precedence: int | None = None
    annotations: dict[str, str] = field(default_factory=dict)

    @property
    def is_calculated(self) -> bool:
        return any(p.is_calculated for p in self.partitions)

    @property
    def is_auto_date(self) -> bool:
        return (self.name.startswith(("LocalDateTable_", "DateTableTemplate_"))
                or "__PBI_LocalDateTable" in self.annotations
                or "__PBI_TemplateDateTable" in self.annotations)

    @property
    def has_key_date_column(self) -> bool:
        return any(c.is_key and c.dtype == "datetime" for c in self.columns)

    @property
    def is_date_table(self) -> bool:
        return (self.data_category or "").lower() == "time" and self.has_key_date_column


@dataclass(eq=False)
class MRel:
    name: str
    node: Node
    from_table: str
    from_column: str
    to_table: str
    to_column: str
    from_card: str
    to_card: str
    cross_filter: str
    is_active: bool

    @property
    def is_many_to_many(self) -> bool:
        return self.from_card == "many" and self.to_card == "many"

    @property
    def is_bidirectional(self) -> bool:
        return self.cross_filter == "bothdirections"

    @property
    def label(self) -> str:
        return f"{self.from_table}[{self.from_column}] -> {self.to_table}[{self.to_column}]"


@dataclass(eq=False)
class MRole:
    name: str
    filters: list[tuple[str, str]]   # (table, DAX filter expression)


@dataclass(eq=False)
class DaxObj:
    kind: str            # measure | column | calculation_item | table | role
    table: str
    name: str
    expr: str
    obj: object


def _split_ref(ref: str) -> tuple[str, str]:
    """``Sales.OrderDate`` / ``'My Table'.'My Col'`` -> (table, column)."""
    ref = ref.strip()
    if ref.startswith("'"):
        i = 1
        while i < len(ref):
            if ref[i] == "'":
                if i + 1 < len(ref) and ref[i + 1] == "'":
                    i += 2
                    continue
                break
            i += 1
        table = unquote_name(ref[:i + 1])
        rest = ref[i + 1:].lstrip()
        col = rest[1:] if rest.startswith(".") else rest
        return table, unquote_name(col)
    table, _, col = ref.partition(".")
    return unquote_name(table), unquote_name(col)


class BpaModel:
    """Everything the rules look at, loaded once per analysis."""

    def __init__(self):
        self.tables: list[MTable] = []
        self.relationships: list[MRel] = []
        self.roles: list[MRole] = []
        self.props: dict[str, str] = {}
        self.usage: dict | None = None
        self.notes: list[str] = []
        self._tbl: dict[str, MTable] = {}
        self._meas: dict[str, MMeasure] = {}
        self._dax: list[DaxObj] | None = None
        self._refs: dict[int, _Refs] = {}
        self._toks: dict[int, list[Token]] = {}
        self._kinds: dict[str, str] = {}

    # --- lookups -----------------------------------------------------------
    def index(self) -> None:
        self._tbl = {t.name.lower(): t for t in self.tables}
        self._meas = {}
        for t in self.tables:
            for m in t.measures:
                self._meas.setdefault(m.name.lower(), m)

    def table(self, name: str | None) -> MTable | None:
        return self._tbl.get((name or "").lower())

    def column(self, table: str | None, name: str | None) -> MColumn | None:
        t = self.table(table)
        if t is None:
            return None
        low = (name or "").lower()
        return next((c for c in t.columns if c.name.lower() == low), None)

    def measure(self, name: str | None) -> MMeasure | None:
        return self._meas.get((name or "").lower())

    def columns(self) -> Iterator[MColumn]:
        for t in self.tables:
            yield from t.columns

    def measures(self) -> Iterator[MMeasure]:
        for t in self.tables:
            yield from t.measures

    @property
    def has_direct_query(self) -> bool:
        return any(p.is_direct_query for t in self.tables for p in t.partitions)

    # --- relationships -------------------------------------------------------
    def rel_columns(self) -> set[tuple[str, str]]:
        out: set[tuple[str, str]] = set()
        for r in self.relationships:
            out.add((r.from_table.lower(), r.from_column.lower()))
            out.add((r.to_table.lower(), r.to_column.lower()))
        return out

    def rel_tables(self) -> set[str]:
        out: set[str] = set()
        for r in self.relationships:
            out.add(r.from_table.lower())
            out.add(r.to_table.lower())
        return out

    # --- DAX -----------------------------------------------------------------
    def dax_objects(self) -> list[DaxObj]:
        if self._dax is None:
            out: list[DaxObj] = []
            for t in self.tables:
                for m in t.measures:
                    out.append(DaxObj("measure", t.name, m.name, m.expression, m))
                for c in t.columns:
                    if c.expression is not None:
                        out.append(DaxObj("column", t.name, c.name, c.expression, c))
                for ci in t.calc_items:
                    out.append(DaxObj("calculation_item", t.name, ci.name,
                                      ci.expression, ci))
                for p in t.partitions:
                    if p.is_calculated:
                        out.append(DaxObj("table", t.name, t.name, p.source, p))
            for r in self.roles:
                for tbl, expr in r.filters:
                    out.append(DaxObj("role", tbl, r.name, expr, r))
            self._dax = out
        return self._dax

    def tokens(self, d: DaxObj) -> list[Token]:
        key = id(d)
        if key not in self._toks:
            self._toks[key] = [t for t in tokenize(d.expr)
                               if t.kind not in ("WS", "COMMENT")]
        return self._toks[key]

    def refs(self, d: DaxObj) -> "_Refs":
        key = id(d)
        if key not in self._refs:
            self._refs[key] = _resolve_refs(self, d.expr)
        return self._refs[key]


@dataclass
class _Refs:
    measures: set[str] = field(default_factory=set)          # canonical measure names
    columns: set[tuple[str, str]] = field(default_factory=set)   # qualified refs
    unqualified: set[str] = field(default_factory=set)       # bare, not a measure
    functions: set[str] = field(default_factory=set)
    tables: set[str] = field(default_factory=set)


def _resolve_refs(model: BpaModel, expr: str) -> _Refs:
    raw = parse_references(expr)
    out = _Refs(columns=set(raw.columns), functions=set(raw.functions),
                tables=set(raw.tables))
    for name in raw.measures:
        m = model.measure(name)
        if m is not None:
            out.measures.add(m.name)
        else:
            out.unqualified.add(name)
    return out


def _flag(node: Node, key: str) -> bool:
    return node.flag(key)


def _build_table(tn: Node, path: Path) -> MTable:
    t = MTable(name=tn.name, node=tn, file=path, is_hidden=_flag(tn, "isHidden"),
               data_category=tn.prop("dataCategory"), description=tn.description)
    for ch in tn.children:
        if ch.kw == "column":
            fs = ch.prop("formatString")
            t.columns.append(MColumn(
                table=t, node=ch, name=ch.name,
                data_type=ch.prop("dataType"),
                summarize_by=ch.prop("summarizeBy"),
                data_category=ch.prop("dataCategory"),
                is_hidden=_flag(ch, "isHidden"),
                is_key=_flag(ch, "isKey"),
                description=ch.description,
                format_string=fs if fs and fs.strip() else None,
                source_column=ch.prop("sourceColumn"),
                sort_by=ch.prop("sortByColumn"),
                expression=ch.expr if ch.is_calculated else None,
                col_type=ch.prop("type")))
        elif ch.kw == "measure":
            fs = ch.prop("formatString")
            t.measures.append(MMeasure(
                table=t, node=ch, name=ch.name, expression=ch.expr,
                format_string=fs if fs and fs.strip() else None,
                has_format_expr="formatStringDefinition" in ch.props,
                display_folder=ch.prop("displayFolder"),
                is_hidden=_flag(ch, "isHidden"),
                description=ch.description))
        elif ch.kw == "partition":
            src = ch.props.get("source")
            t.partitions.append(MPartition(
                table=t, node=ch, name=ch.name, kind=(ch.inline or "").strip(),
                mode=ch.prop("mode"), source=src.value if src else ""))
        elif ch.kw == "hierarchy":
            levels = []
            for lv in ch.kids("level"):
                col = lv.prop("column")
                levels.append(unquote_name(col) if col else lv.name)
            t.hierarchies.append((ch.name, levels))
        elif ch.kw == "calculationGroup":
            t.is_calc_group = True
            prec = ch.prop("precedence")
            try:
                t.precedence = int(prec) if prec is not None else None
            except ValueError:
                t.precedence = None
            for item in ch.kids("calculationItem"):
                t.calc_items.append(MCalcItem(t, item, item.name, item.expr))
        elif ch.kw == "annotation":
            t.annotations[ch.name] = ch.inline or ""
    return t


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def _build_relationship(n: Node) -> MRel | None:
    fc, tc = n.prop("fromColumn"), n.prop("toColumn")
    if not fc or not tc:
        return None
    ft, fcol = _split_ref(fc)
    tt, tcol = _split_ref(tc)
    active = True
    p = n.props.get("isActive")
    if p is not None and p.value.strip().lower() == "false":
        active = False
    return MRel(
        name=n.name, node=n, from_table=ft, from_column=fcol,
        to_table=tt, to_column=tcol,
        from_card=(n.prop("fromCardinality") or "many").strip().lower(),
        to_card=(n.prop("toCardinality") or "one").strip().lower(),
        cross_filter=(n.prop("crossFilteringBehavior") or "oneDirection").strip().lower(),
        is_active=active)


def load_model(project, with_usage: bool = True) -> BpaModel:
    """Parse the project's TMDL (and report usage) into a :class:`BpaModel`."""
    model = BpaModel()
    definition = project._require_model() / "definition"
    tables_dir = definition / "tables"
    if tables_dir.is_dir():
        for f in sorted(tables_dir.glob("*.tmdl")):
            for tn in parse_nodes(_read(f)):
                if tn.kw == "table":
                    model.tables.append(_build_table(tn, f))
    rel_file = definition / "relationships.tmdl"
    if rel_file.is_file():
        for n in parse_nodes(_read(rel_file)):
            if n.kw == "relationship":
                rel = _build_relationship(n)
                if rel is not None:
                    model.relationships.append(rel)
    model_file = definition / "model.tmdl"
    if model_file.is_file():
        for n in parse_nodes(_read(model_file)):
            if n.kw == "model":
                model.props = {k: p.value for k, p in n.props.items()}
    role_files = sorted((definition / "roles").glob("*.tmdl")) \
        if (definition / "roles").is_dir() else []
    if (definition / "roles.tmdl").is_file():
        role_files.append(definition / "roles.tmdl")
    for f in role_files:
        for n in parse_nodes(_read(f)):
            if n.kw == "role":
                model.roles.append(MRole(n.name, [
                    (tp.name, tp.expr) for tp in n.kids("tablePermission")]))
    model.index()
    if with_usage:
        try:
            model.usage = project._report_usage_or_none()
        except Exception as exc:  # unreadable report JSON must not block the model rules
            model.usage = None
            model.notes.append(f"Report usage unavailable ({exc}); usage-based rules skipped.")
        else:
            if model.usage is None:
                model.notes.append(
                    "No report layer found; usage-based rules were skipped.")
    return model


# =============================================================================
# 3. DAX helpers
# =============================================================================

_FUNC_KW = frozenset({"TRUE", "FALSE", "NOT", "AND", "OR"})


def _is_call_name(t: Token) -> bool:
    if t.kind != "IDENT":
        return False
    u = t.text.upper()
    return u not in KEYWORDS or u in _FUNC_KW


def _matching(toks: list[Token]) -> dict[int, int]:
    match: dict[int, int] = {}
    stack: list[int] = []
    for i, t in enumerate(toks):
        if t.kind == "PUNCT":
            if t.text in "({":
                stack.append(i)
            elif t.text in ")}" and stack:
                match[stack.pop()] = i
    return match


def _split_args(toks: list[Token]) -> list[list[Token]]:
    args: list[list[Token]] = []
    cur: list[Token] = []
    depth = 0
    for x in toks:
        if x.kind == "PUNCT" and x.text in "({":
            depth += 1
        elif x.kind == "PUNCT" and x.text in ")}":
            depth -= 1
        elif x.kind == "PUNCT" and x.text in ",;" and depth == 0:
            args.append(cur)
            cur = []
            continue
        cur.append(x)
    if cur or args:
        args.append(cur)
    return args


def iter_calls(toks: list[Token]) -> Iterator[tuple[str, list[list[Token]]]]:
    """Every function call in a significant-token list: (NAME, [arg tokens])."""
    match = _matching(toks)
    for i, t in enumerate(toks):
        if (_is_call_name(t) and i + 1 < len(toks) and toks[i + 1].kind == "PUNCT"
                and toks[i + 1].text == "(" and (i + 1) in match):
            yield t.text.upper(), _split_args(toks[i + 2:match[i + 1]])


def _qualified_col(arg: list[Token]) -> tuple[str, str] | None:
    """[Table, [Col]] -> (table, col)."""
    if (len(arg) == 2 and arg[0].kind in ("IDENT", "TABLE") and arg[1].kind == "COLUMN"
            and not (arg[0].kind == "IDENT" and arg[0].text.upper() in KEYWORDS)):
        tbl = arg[0].text
        if arg[0].kind == "TABLE":
            tbl = tbl[1:-1].replace("''", "'")
        return tbl, arg[1].text[1:-1].replace("]]", "]").strip()
    return None


def _bare_table(arg: list[Token]) -> str | None:
    if (len(arg) == 1 and arg[0].kind in ("IDENT", "TABLE")
            and arg[0].text.upper() not in KEYWORDS):
        t = arg[0].text
        return t[1:-1].replace("''", "'") if arg[0].kind == "TABLE" else t
    return None


def code_only(expr: str) -> str:
    """`expr` with comments and string contents blanked (same length)."""
    out: list[str] = []
    for t in tokenize(expr):
        if t.kind == "COMMENT":
            out.append(re.sub(r"[^\n]", " ", t.text))
        elif t.kind == "STRING" and len(t.text) >= 2:
            out.append('"' + " " * (len(t.text) - 2) + '"')
        else:
            out.append(t.text)
    return "".join(out)


def _calc_filter_kinds(toks: list[Token]) -> Iterator[str]:
    """Classify each filter argument of every CALCULATE / CALCULATETABLE."""
    for name, args in iter_calls(toks):
        if name not in ("CALCULATE", "CALCULATETABLE"):
            continue
        for arg in args[1:]:
            if _bare_table(arg) is not None:
                yield "whole_table"
                continue
            if (len(arg) >= 3 and arg[0].kind == "IDENT" and arg[0].text.upper() == "FILTER"
                    and arg[1].kind == "PUNCT" and arg[1].text == "("):
                fargs = _split_args(arg[2:-1]) if arg[-1].text == ")" else []
                if len(fargs) >= 2 and _bare_table(fargs[0]) is not None and fargs[1]:
                    pred = fargs[1]
                    if len(pred) >= 2 and pred[0].kind in ("IDENT", "TABLE") \
                            and pred[1].kind == "COLUMN":
                        yield "column_filter"
                    elif pred[0].kind == "COLUMN":
                        yield "measure_filter"
                    else:
                        yield "whole_table_filter"


# --- rough result-type inference (only used to decide "numeric measure") -------

_NUM_FUNCS = frozenset("""
SUM SUMX AVERAGE AVERAGEX AVERAGEA COUNT COUNTA COUNTX COUNTAX COUNTROWS COUNTBLANK
DISTINCTCOUNT DISTINCTCOUNTNOBLANK APPROXIMATEDISTINCTCOUNT DIVIDE ROUND ROUNDUP ROUNDDOWN INT ABS
FLOOR CEILING MROUND TRUNC POWER SQRT EXP LN LOG LOG10 MOD QUOTIENT SIGN PRODUCT PRODUCTX
MEDIAN MEDIANX PERCENTILE.INC PERCENTILE.EXC PERCENTILEX.INC PERCENTILEX.EXC STDEV.S STDEV.P
STDEVX.S STDEVX.P VAR.S VAR.P VARX.S VARX.P GEOMEAN GEOMEANX RANKX RANK DATEDIFF YEAR MONTH DAY
QUARTER WEEKDAY WEEKNUM HOUR MINUTE SECOND LEN VALUE RAND RANDBETWEEN PI XIRR IRR NPV XNPV PV FV
PMT RATE FACT GCD LCM CURRENCY SIN COS TAN ASIN ACOS ATAN DEGREES RADIANS CONVERT
""".split())
_TEXT_FUNCS = frozenset("""
FORMAT CONCATENATE CONCATENATEX LEFT RIGHT MID UPPER LOWER TRIM SUBSTITUTE REPLACE REPT UNICHAR
FIXED PROPER CLEAN USERNAME USERPRINCIPALNAME SELECTEDMEASURENAME SELECTEDMEASUREFORMATSTRING
TOCSV TOJSON
""".split())
_BOOL_FUNCS = frozenset("""
ISBLANK ISERROR ISNUMBER ISTEXT ISLOGICAL ISNONTEXT ISEVEN ISODD ISFILTERED ISCROSSFILTERED
ISINSCOPE HASONEVALUE HASONEFILTER CONTAINS CONTAINSROW CONTAINSSTRING CONTAINSSTRINGEXACT AND OR
NOT TRUE FALSE EXACT ISSELECTEDMEASURE
""".split())
_DATE_FUNCS = frozenset("TODAY NOW DATE DATEVALUE EDATE EOMONTH TIME TIMEVALUE UTCNOW UTCTODAY".split())
_PASS_FIRST = frozenset("""
CALCULATE TOTALYTD TOTALQTD TOTALMTD CLOSINGBALANCEMONTH CLOSINGBALANCEQUARTER
CLOSINGBALANCEYEAR OPENINGBALANCEMONTH OPENINGBALANCEQUARTER OPENINGBALANCEYEAR IFERROR
SELECTEDVALUE
""".split())
_CMP = frozenset({"=", "==", "<>", "<", "<=", ">", ">="})


def _merge_kinds(kinds: list[str]) -> str:
    live = [k for k in kinds if k != "blank"]
    if not live:
        return "unknown"
    return live[0] if all(k == live[0] for k in live) else "unknown"


def _depth0(toks: list[Token], pred) -> list[int]:
    out, depth = [], 0
    for i, t in enumerate(toks):
        if t.kind == "PUNCT" and t.text in "({":
            depth += 1
        elif t.kind == "PUNCT" and t.text in ")}":
            depth -= 1
        elif depth == 0 and pred(i, t):
            out.append(i)
    return out


def _binary_ops(toks: list[Token], ops: set[str]) -> list[int]:
    def is_bin(i: int, t: Token) -> bool:
        if t.kind == "OP" and t.text in ops:
            if t.text in ("+", "-"):
                prev = toks[i - 1] if i else None
                if prev is None or prev.kind == "OP" or (
                        prev.kind == "IDENT" and prev.text.upper() in KEYWORDS
                        and prev.text.upper() not in _FUNC_KW) or (
                        prev.kind == "PUNCT" and prev.text in ",;("):
                    return False
            return True
        return t.kind == "IDENT" and t.text.upper() in ops
    return _depth0(toks, is_bin)


def _split_at(toks: list[Token], idx: list[int]) -> list[list[Token]]:
    cuts = [-1, *idx, len(toks)]
    return [toks[a + 1:b] for a, b in zip(cuts, cuts[1:])]


def _kind(model: "BpaModel", toks: list[Token], owner: str, env: dict[str, str],
          seen: frozenset[str]) -> str:
    """numeric | text | bool | date | blank | unknown for a token slice."""
    n = len(toks)
    if n == 0:
        return "unknown"
    ret = _depth0(toks, lambda i, t: t.kind == "IDENT" and t.text.upper() == "RETURN")
    if ret:
        env = dict(env)
        vars_ = _depth0(toks[:ret[-1]],
                        lambda i, t: t.kind == "IDENT" and t.text.upper() == "VAR")
        bounds = vars_ + [ret[-1]]
        for a, b in zip(bounds, bounds[1:]):
            seg = toks[a + 1:b]
            if len(seg) >= 3 and seg[0].kind == "IDENT" and seg[1].text == "=":
                env[seg[0].text.upper()] = _kind(model, seg[2:], owner, env, seen)
        return _kind(model, toks[ret[-1] + 1:], owner, env, seen)
    for ops, result in ((("||",), "bool"), (("&&",), "bool"),
                        (tuple(_CMP) + ("IN",), "bool"), (("&",), "text")):
        if _binary_ops(toks, set(ops)):
            return result
    for ops in (("+", "-"), ("*", "/"), ("^",)):
        idx = _binary_ops(toks, set(ops))
        if idx:
            kinds = [_kind(model, p, owner, env, seen) for p in _split_at(toks, idx)]
            if any(k in ("text", "date", "bool") for k in kinds):
                return "unknown"
            if ops == ("+", "-") and "numeric" not in kinds:
                return "unknown"
            return "numeric"
    t0 = toks[0]
    if t0.kind == "OP" and t0.text in ("+", "-"):
        return "numeric"
    if t0.kind == "IDENT" and t0.text.upper() == "NOT" and n > 1 and toks[1].text != "(":
        return "bool"
    if n == 1:
        if t0.kind == "NUMBER":
            return "numeric"
        if t0.kind == "STRING":
            return "text"
        if t0.kind == "IDENT":
            up = t0.text.upper()
            if up in ("TRUE", "FALSE"):
                return "bool"
            return env.get(up, "unknown")
        if t0.kind == "COLUMN":
            name = t0.text[1:-1].replace("]]", "]").strip()
            meas = model.measure(name)
            if meas is not None:
                if meas.name.lower() in seen:
                    return "unknown"
                return _measure_kind(model, meas, seen)
            return _column_kind(model.column(owner, name))
        return "unknown"
    if n == 2 and t0.kind in ("IDENT", "TABLE") and toks[1].kind == "COLUMN":
        q = _qualified_col(toks)
        if q:
            col = model.column(q[0], q[1])
            if col is not None:
                return _column_kind(col)
            meas = model.measure(q[1])
            if meas is not None and meas.name.lower() not in seen:
                return _measure_kind(model, meas, seen)
        return "unknown"
    match = _matching(toks)
    if t0.kind == "PUNCT" and t0.text == "(" and match.get(0) == n - 1:
        return _kind(model, toks[1:-1], owner, env, seen)
    if (n >= 3 and _is_call_name(t0) and toks[1].kind == "PUNCT" and toks[1].text == "("
            and match.get(1) == n - 1):
        return _call_kind(model, t0.text.upper(), _split_args(toks[2:-1]), owner, env, seen)
    return "unknown"


def _column_kind(col: "MColumn | None") -> str:
    if col is None:
        return "unknown"
    return {"int64": "numeric", "double": "numeric", "decimal": "numeric",
            "string": "text", "datetime": "date", "boolean": "bool"}.get(col.dtype, "unknown")


def _measure_kind(model: "BpaModel", meas: "MMeasure", seen: frozenset[str]) -> str:
    key = f"{meas.table.name}\0{meas.name}".lower()
    if key in model._kinds:
        return model._kinds[key]
    toks = [t for t in tokenize(meas.expression) if t.kind not in ("WS", "COMMENT")]
    kind = _kind(model, toks, meas.table.name, {}, seen | {meas.name.lower()})
    model._kinds[key] = kind
    return kind


def _call_kind(model, name, args, owner, env, seen) -> str:
    def k(i: int) -> str:
        return _kind(model, args[i], owner, env, seen) if i < len(args) else "unknown"
    if name in _NUM_FUNCS:
        return "numeric"
    if name in _TEXT_FUNCS:
        return "text"
    if name in _BOOL_FUNCS:
        return "bool"
    if name in _DATE_FUNCS:
        return "date"
    if name == "BLANK":
        return "blank"
    if name in _PASS_FIRST:
        return k(0)
    if name in ("MAX", "MIN"):
        return k(0) if len(args) == 1 else _merge_kinds([k(0), k(1)])
    if name in ("MAXX", "MINX", "FIRSTNONBLANKVALUE", "LASTNONBLANKVALUE"):
        return k(1)
    if name == "IF":
        return _merge_kinds([k(1), k(2)] if len(args) > 2 else [k(1)])
    if name == "COALESCE":
        return _merge_kinds([k(i) for i in range(len(args))])
    if name == "SWITCH":
        rest = list(range(1, len(args)))
        pairs = rest[:len(rest) // 2 * 2]
        results = [k(i) for i in pairs[1::2]]
        if len(rest) % 2 == 1:
            results.append(k(rest[-1]))
        return _merge_kinds(results)
    return "unknown"


def measure_result_kind(model: "BpaModel", meas: "MMeasure") -> str:
    """Best-effort result type of a measure: numeric | text | bool | date | unknown."""
    return _measure_kind(model, meas, frozenset())


_PERCENT_NAME = re.compile(r"%|pct|percent|ratio", re.IGNORECASE)


def default_format_string(name: str) -> str:
    """`0.0%` for percentage-like names, else `#,0`."""
    return "0.0%" if _PERCENT_NAME.search(name) else "#,0"


# =============================================================================
# 4. Rule engine
# =============================================================================

@dataclass
class Finding:
    rule_id: str
    category: str
    severity: int
    object_type: str
    table: str | None
    name: str
    message: str
    fixable: bool = False
    tables: tuple[str, ...] = ()
    data: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"rule_id": self.rule_id, "category": self.category,
                "severity": self.severity, "object_type": self.object_type,
                "table": self.table, "name": self.name, "message": self.message,
                "fixable": self.fixable}


@dataclass
class RuleDef:
    id: str
    category: str
    severity: int
    description: str
    scopes: tuple[str, ...]
    aliases: tuple[str, ...] = ()
    fixer: str | None = None
    origin: str = "TabularEditor"
    needs_report: bool = False
    title: str = ""
    check: Callable | None = None

    def listing(self) -> dict:
        return {"id": self.id, "title": self.title, "aliases": list(self.aliases),
                "category": self.category, "severity": self.severity,
                "description": self.description, "scopes": list(self.scopes),
                "fixable": self.fixer is not None, "origin": self.origin,
                "needs_report": self.needs_report}


class _Ctx:
    def __init__(self, model: BpaModel, rule: RuleDef):
        self.m = model
        self.rule = rule
        self.findings: list[Finding] = []

    def emit(self, object_type: str, table: str | None, name: str, message: str,
             *, fixable: bool = False, tables: tuple[str, ...] | None = None,
             data: dict | None = None) -> None:
        if tables is None:
            tables = (table,) if table else ()
        self.findings.append(Finding(
            self.rule.id, self.rule.category, self.rule.severity, object_type,
            table, name, message, fixable, tables, data or {}))


_CHECKS: dict[str, Callable[[_Ctx], None]] = {}


def check(rule_id: str):
    """Register the Python check for a catalog rule."""
    def deco(fn):
        _CHECKS[rule_id] = fn
        return fn
    return deco


def norm_id(rule_id: str) -> str:
    """Rule IDs compare case- and punctuation-insensitively."""
    return re.sub(r"[^A-Z0-9]+", "_", rule_id.upper()).strip("_")


def _names(m: BpaModel) -> Iterator[tuple[str, str | None, str, object]]:
    """(object_type, table, name, obj) for every named model object."""
    for t in m.tables:
        yield ("calculation_group" if t.is_calc_group else "table"), t.name, t.name, t
        for c in t.columns:
            yield "column", t.name, c.name, c
        for x in t.measures:
            yield "measure", t.name, x.name, x
        for hn, _ in t.hierarchies:
            yield "hierarchy", t.name, hn, None
        for p in t.partitions:
            yield "partition", t.name, p.name, p
        for ci in t.calc_items:
            yield "calculation_item", t.name, ci.name, ci


def _has_invalid_chars(s: str) -> bool:
    return any(unicodedata.category(ch) == "Cc" and not ch.isspace() for ch in s)


def _col_label(col: MColumn) -> str:
    return f"{col.table.name}[{col.name}]"


# --- Performance --------------------------------------------------------------

@check("AVOID_FLOATING_POINT_DATA_TYPES")
def _floating_point(c: _Ctx) -> None:
    for col in c.m.columns():
        if col.dtype == "double":
            c.emit("column", col.table.name, col.name,
                   f"{_col_label(col)} uses the floating point 'double' data type; "
                   "use 'decimal' (fixed decimal number) or 'int64' to avoid "
                   "round-off errors and slower storage.")


@check("REDUCE_USAGE_OF_CALCULATED_COLUMNS_THAT_USE_THE_RELATED_FUNCTION")
def _calc_col_related(c: _Ctx) -> None:
    for d in c.m.dax_objects():
        if d.kind == "column" and "RELATED" in c.m.refs(d).functions:
            c.emit("column", d.table, d.name,
                   f"Calculated column {d.table}[{d.name}] uses RELATED(); calculated "
                   "columns compress poorly. Bring the column in through Power Query "
                   "or the source query instead.")


@check("SNOWFLAKE_SCHEMA_ARCHITECTURE")
def _snowflake(c: _Ctx) -> None:
    many = {r.from_table.lower() for r in c.m.relationships}
    one = {r.to_table.lower() for r in c.m.relationships}
    for t in c.m.tables:
        if not t.is_calc_group and t.name.lower() in many and t.name.lower() in one:
            c.emit("table", t.name, t.name,
                   f"Table '{t.name}' is on the many side of one relationship and the "
                   "one side of another (snowflake). Prefer a star schema: flatten "
                   "the chain into a single dimension.")


@check("MODEL_SHOULD_HAVE_A_DATE_TABLE")
def _has_date_table(c: _Ctx) -> None:
    if c.m.tables and not any(t.is_date_table for t in c.m.tables):
        c.emit("model", None, "Model",
               "No date table found (a table with dataCategory: Time and a key "
               "dateTime column). Add one and mark it as the date table to enable "
               "time intelligence.")


@check("DATE/CALENDAR_TABLES_SHOULD_BE_MARKED_AS_A_DATE_TABLE")
def _mark_date_table(c: _Ctx) -> None:
    for t in c.m.tables:
        if t.is_auto_date or t.is_calc_group:
            continue
        low = t.name.lower()
        if ("date" in low or "calendar" in low) and not t.is_date_table:
            c.emit("table", t.name, t.name,
                   f"Table '{t.name}' looks like a date table but is not marked as one. "
                   "Set 'dataCategory: Time' on the table and 'isKey' on its date column.")


@check("REMOVE_AUTO-DATE_TABLE")
def _auto_date(c: _Ctx) -> None:
    for t in c.m.tables:
        if t.is_auto_date:
            c.emit("table", t.name, t.name,
                   f"'{t.name}' is an automatic date/time table. Turn off Auto date/time "
                   "and use your own date table to save memory.")


@check("AVOID_EXCESSIVE_BI-DIRECTIONAL_OR_MANY-TO-MANY_RELATIONSHIPS")
def _excessive_bidi(c: _Ctx) -> None:
    rels = c.m.relationships
    bad = sum(r.is_bidirectional for r in rels) + sum(r.is_many_to_many for r in rels)
    if rels and bad / max(len(rels), 1) > 0.3:
        c.emit("model", None, "Model",
               f"{bad} of {len(rels)} relationships are bi-directional or many-to-many "
               "(more than 30%). Prefer single-direction one-to-many relationships.")


@check("MODEL_USING_DIRECT_QUERY_AND_NO_AGGREGATIONS")
def _dq_no_agg(c: _Ctx) -> None:
    if not c.m.has_direct_query:
        return
    if any(col.node.kid("alternateOf") is not None for col in c.m.columns()):
        return
    if c.m.props.get("defaultPowerBIDataSourceVersion", "").lower() != "powerbi_v3":
        return
    c.emit("model", None, "Model",
           "The model has DirectQuery tables but no aggregation tables. Consider "
           "user-defined aggregations to speed up DirectQuery.")


_M_TRANSFORMS = (
    "Table.Combine(", "Table.Join(", "Table.NestedJoin(", "Table.AddColumn(",
    "Table.Group(", "Table.Sort(", "Table.Pivot(", "Table.Unpivot(",
    "Table.UnpivotOtherColumns(", "Table.Distinct(", '[Query="SELECT',
    "Value.NativeQuery", "OleDb.Query", "Odbc.Query",
)


@check("MINIMIZE_POWER_QUERY_TRANSFORMATIONS")
def _m_transforms(c: _Ctx) -> None:
    for t in c.m.tables:
        for p in t.partitions:
            if p.is_m:
                found = [f for f in _M_TRANSFORMS if f in p.source]
                if found:
                    c.emit("partition", t.name, p.name,
                           f"Partition '{p.name}' does heavy work in Power Query "
                           f"({', '.join(f.rstrip('(') for f in found)}). Push the "
                           "transformation upstream (source query / warehouse) and "
                           "check that query folding still happens.")


_MONTH = re.compile(
    r"^(january|february|march|april|may|june|july|august|september|october|"
    r"november|december|jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec)(?![a-z])")
_YEAR = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")


@check("UNPIVOT_PIVOTED_(MONTH)_DATA")
def _pivoted(c: _Ctx) -> None:
    for t in c.m.tables:
        if t.is_calc_group or (t.data_category or "").lower() == "time":
            continue
        months, years = set(), set()
        for col in t.columns:
            if not col.is_numeric or col.is_calculated:
                continue
            low = col.name.strip().lower()
            m = _MONTH.match(low)
            if m:
                months.add(m.group(1)[:3])
            y = _YEAR.search(low)
            if y:
                years.add(y.group(0))
        if len(months) >= 3 or len(years) >= 3:
            what = "months" if len(months) >= 3 else "years"
            c.emit("table", t.name, t.name,
                   f"Table '{t.name}' has several numeric columns named after {what}; "
                   "this looks pivoted. Unpivot it into (period, value) rows.")


@check("MANY-TO-MANY_RELATIONSHIPS_SHOULD_BE_SINGLE-DIRECTION")
def _m2m_single(c: _Ctx) -> None:
    for r in c.m.relationships:
        if r.is_many_to_many and r.is_bidirectional:
            c.emit("relationship", r.from_table, r.label,
                   f"Many-to-many relationship {r.label} filters in both directions; "
                   "make it single-direction.", tables=(r.from_table, r.to_table))


@check("REDUCE_USAGE_OF_CALCULATED_TABLES")
def _calc_tables(c: _Ctx) -> None:
    for t in c.m.tables:
        if t.is_calculated and not t.is_calc_group and not t.is_auto_date:
            c.emit("table", t.name, t.name,
                   f"'{t.name}' is a calculated table. Move that logic to the data "
                   "warehouse or Power Query where practical.")


@check("REMOVE_REDUNDANT_COLUMNS_IN_RELATED_TABLES")
def _redundant_cols(c: _Ctx) -> None:
    rel_cols = c.m.rel_columns()
    seen: set[tuple[str, str]] = set()
    for r in c.m.relationships:
        src, dst = c.m.table(r.from_table), c.m.table(r.to_table)
        if src is None or dst is None or src is dst:
            continue
        dst_names = {x.name.lower() for x in dst.columns}
        for col in src.columns:
            key = (src.name.lower(), col.name.lower())
            if key in rel_cols or key in seen or col.name.lower() not in dst_names:
                continue
            seen.add(key)
            c.emit("column", src.name, col.name,
                   f"{_col_label(col)} duplicates {dst.name}[{col.name}], reachable "
                   f"through the relationship {r.label}. Remove the redundant column.",
                   tables=(src.name, dst.name))


@check("MEASURES_USING_TIME_INTELLIGENCE_AND_MODEL_IS_USING_DIRECT_QUERY")
def _ti_dq(c: _Ctx) -> None:
    if not c.m.has_direct_query:
        return
    for d in c.m.dax_objects():
        if d.kind in ("measure", "calculation_item"):
            used = sorted(c.m.refs(d).functions & _TIME_INTELLIGENCE)
            if used:
                c.emit(d.kind, d.table, d.name,
                       f"'{d.name}' uses time intelligence ({', '.join(used)}) on a model "
                       "with DirectQuery tables, which performs poorly. Consider "
                       "precomputed prior-period columns.")


@check("REDUCE_NUMBER_OF_CALCULATED_COLUMNS")
def _many_calc_cols(c: _Ctx) -> None:
    n = sum(1 for col in c.m.columns() if col.is_calculated)
    if n > 5:
        c.emit("model", None, "Model",
               f"The model has {n} calculated columns (limit 5). Calculated columns "
               "compress poorly and slow refresh; move the logic upstream.")


@check("CHECK_IF_BI-DIRECTIONAL_AND_MANY-TO-MANY_RELATIONSHIPS_ARE_VALID")
def _check_bidi(c: _Ctx) -> None:
    for r in c.m.relationships:
        if r.is_bidirectional or r.is_many_to_many:
            kind = " and ".join(k for k, on in (("bi-directional", r.is_bidirectional),
                                                ("many-to-many", r.is_many_to_many)) if on)
            c.emit("relationship", r.from_table, r.label,
                   f"Relationship {r.label} is {kind}. Confirm it is really needed: "
                   "it can slow queries and make results ambiguous.",
                   tables=(r.from_table, r.to_table))


_RLS_HEAVY = frozenset({"RIGHT", "LEFT", "UPPER", "LOWER", "FIND"})


@check("LIMIT_ROW_LEVEL_SECURITY_(RLS)_LOGIC")
def _rls_logic(c: _Ctx) -> None:
    for d in c.m.dax_objects():
        if d.kind == "role":
            used = sorted(c.m.refs(d).functions & _RLS_HEAVY)
            if used:
                c.emit("role", d.table, d.name,
                       f"Role '{d.name}' filters {d.table} with {', '.join(used)}; "
                       "simplify RLS DAX and do string work upstream.")


@check("CHECK_IF_DYNAMIC_ROW_LEVEL_SECURITY_(RLS)_IS_NECESSARY")
def _dynamic_rls(c: _Ctx) -> None:
    for d in c.m.dax_objects():
        if d.kind == "role" and c.m.refs(d).functions & {"USERNAME", "USERPRINCIPALNAME"}:
            c.emit("role", d.table, d.name,
                   f"Role '{d.name}' uses dynamic RLS (USERNAME/USERPRINCIPALNAME) on "
                   f"{d.table}. It adds memory and query overhead; make sure it is "
                   "necessary.")


def _rls_tables(m: BpaModel) -> set[str]:
    return {d.table.lower() for d in m.dax_objects() if d.kind == "role" and d.expr.strip()}


@check("AVOID_USING_MANY-TO-MANY_RELATIONSHIPS_ON_TABLES_USED_FOR_DYNAMIC_ROW_LEVEL_SECURITY")
def _m2m_rls(c: _Ctx) -> None:
    rls = _rls_tables(c.m)
    for t in c.m.tables:
        if t.name.lower() in rls and any(
                r.is_many_to_many and t.name.lower() in (r.from_table.lower(), r.to_table.lower())
                for r in c.m.relationships):
            c.emit("table", t.name, t.name,
                   f"Table '{t.name}' has row-level security and a many-to-many "
                   "relationship, which degrades query performance. Relate a single "
                   "dimension many-to-one to a security table instead.")


@check("AVOID_SINGLE_ATTRIBUTE_DIMENSIONS")
def _single_attr(c: _Ctx) -> None:
    rel_cols = c.m.rel_columns()
    for t in c.m.tables:
        if t.is_calc_group or not t.columns:
            continue
        loose = [x for x in t.columns
                 if x.visible and (t.name.lower(), x.name.lower()) not in rel_cols]
        into = sum(1 for r in c.m.relationships if r.to_table.lower() == t.name.lower())
        if len(loose) <= 1 and into == 1:
            c.emit("table", t.name, t.name,
                   f"Dimension '{t.name}' holds a single attribute and feeds one table. "
                   "Consider moving the attribute into the fact table.")


# --- DAX Expressions ----------------------------------------------------------

def _col_names(m: BpaModel) -> set[str]:
    return {col.name.lower() for col in m.columns()}


@check("DAX_COLUMNS_FULLY_QUALIFIED")
def _cols_qualified(c: _Ctx) -> None:
    names = _col_names(c.m)
    for d in c.m.dax_objects():
        if d.kind not in ("measure", "calculation_item", "role"):
            continue
        bare = sorted(n for n in c.m.refs(d).unqualified if n.lower() in names)
        if bare:
            c.emit(d.kind, d.table, d.name,
                   f"'{d.name}' references column(s) {', '.join('[' + b + ']' for b in bare)} "
                   "without a table name. Write Table[Column] so columns and measures "
                   "are easy to tell apart.")


@check("DAX_MEASURES_UNQUALIFIED")
def _measures_unqualified(c: _Ctx) -> None:
    for d in c.m.dax_objects():
        if d.kind == "role":
            continue
        hits = sorted(f"{t}[{n}]" for t, n in c.m.refs(d).columns
                      if c.m.measure(n) is not None and c.m.column(t, n) is None)
        if hits:
            c.emit(d.kind, d.table, d.name,
                   f"'{d.name}' references measure(s) {', '.join(hits)} with a table "
                   "name. Refer to measures as [Measure] only.")


@check("AVOID_DUPLICATE_MEASURES")
def _dup_measures(c: _Ctx) -> None:
    groups: dict[str, list[MMeasure]] = {}
    for m in c.m.measures():
        key = re.sub(r"\s+", "", m.expression)
        if key:
            groups.setdefault(key, []).append(m)
    for grp in groups.values():
        if len(grp) > 1:
            for m in grp:
                others = ", ".join(x.name for x in grp if x is not m)
                c.emit("measure", m.table.name, m.name,
                       f"Measure '{m.name}' has the same DAX as: {others}. Remove the "
                       "duplicates.")


@check("USE_THE_TREATAS_FUNCTION_INSTEAD_OF_INTERSECT")
def _treatas(c: _Ctx) -> None:
    for d in c.m.dax_objects():
        if d.kind in ("measure", "calculation_item") and "INTERSECT" in c.m.refs(d).functions:
            c.emit(d.kind, d.table, d.name,
                   f"'{d.name}' uses INTERSECT for a virtual relationship; TREATAS is "
                   "faster.")


@check("USE_THE_DIVIDE_FUNCTION_FOR_DIVISION")
def _divide(c: _Ctx) -> None:
    for d in c.m.dax_objects():
        if d.kind not in ("measure", "column", "calculation_item"):
            continue
        toks = c.m.tokens(d)
        hits = 0
        for i, t in enumerate(toks):
            if t.kind == "OP" and t.text == "/":
                nxt = toks[i + 1:i + 3]
                if len(nxt) > 1 and nxt[0].kind == "OP" and nxt[0].text in ("+", "-"):
                    nxt = nxt[1:]
                if not (nxt and nxt[0].kind == "NUMBER"):
                    hits += 1
        if hits:
            c.emit(d.kind, d.table, d.name,
                   f"'{d.name}' divides with '/' ({hits}x). Use DIVIDE(numerator, "
                   "denominator) to handle divide-by-zero (dividing by a constant is "
                   "fine).")


@check("AVOID_USING_THE_IFERROR_FUNCTION")
def _iferror(c: _Ctx) -> None:
    for d in c.m.dax_objects():
        if d.kind in ("measure", "column", "calculation_item") \
                and "IFERROR" in c.m.refs(d).functions:
            c.emit(d.kind, d.table, d.name,
                   f"'{d.name}' uses IFERROR, which can hurt performance. Use DIVIDE "
                   "for divide-by-zero, or fix the source of the error.")


@check("MEASURES_SHOULD_NOT_BE_DIRECT_REFERENCES_OF_OTHER_MEASURES")
def _direct_refs(c: _Ctx) -> None:
    for m in c.m.measures():
        toks = [t for t in tokenize(m.expression) if t.kind not in ("WS", "COMMENT")]
        if len(toks) == 1 and toks[0].kind == "COLUMN":
            target = c.m.measure(toks[0].text[1:-1].strip())
            if target is not None and target is not m:
                c.emit("measure", m.table.name, m.name,
                       f"Measure '{m.name}' only returns [{target.name}]. Remove the "
                       "duplicate and use the original measure.")


def _filter_rule(c: _Ctx, kinds: set[str], message: str) -> None:
    for d in c.m.dax_objects():
        if d.kind not in ("measure", "column", "calculation_item"):
            continue
        n = sum(1 for k in _calc_filter_kinds(c.m.tokens(d)) if k in kinds)
        if n:
            c.emit(d.kind, d.table, d.name, message.format(name=d.name, n=n))


@check("FILTER_COLUMN_VALUES")
def _filter_column(c: _Ctx) -> None:
    _filter_rule(c, {"column_filter"},
                 "'{name}' filters a whole table with FILTER(Table, Table[Col] ...) inside "
                 "CALCULATE ({n}x). Use a column predicate instead: "
                 "CALCULATE(..., Table[Col] = value) or KEEPFILTERS(Table[Col] = value).")


@check("FILTER_MEASURE_VALUES_BY_COLUMNS")
def _filter_measure(c: _Ctx) -> None:
    _filter_rule(c, {"measure_filter"},
                 "'{name}' filters a whole table by a measure with FILTER(Table, [Measure] "
                 "...) inside CALCULATE ({n}x). Filter a column instead, e.g. "
                 "FILTER(VALUES(Table[Col]), [Measure] > value).")


@check("NO_CALCULATE_FILTER_ON_WHOLE_TABLE")
def _whole_table(c: _Ctx) -> None:
    _filter_rule(c, {"whole_table", "whole_table_filter"},
                 "'{name}' passes a whole table as a CALCULATE filter ({n}x). Filter the "
                 "specific column(s) instead of the entire table.")


@check("INACTIVE_RELATIONSHIPS_THAT_ARE_NEVER_ACTIVATED")
def _inactive(c: _Ctx) -> None:
    inactive = [r for r in c.m.relationships if not r.is_active]
    if not inactive:
        return
    used: set[frozenset] = set()
    for d in c.m.dax_objects():
        for name, args in iter_calls(c.m.tokens(d)):
            if name == "USERELATIONSHIP" and len(args) >= 2:
                a, b = _qualified_col(args[0]), _qualified_col(args[1])
                if a and b:
                    used.add(frozenset({(a[0].lower(), a[1].lower()),
                                        (b[0].lower(), b[1].lower())}))
    for r in inactive:
        key = frozenset({(r.from_table.lower(), r.from_column.lower()),
                         (r.to_table.lower(), r.to_column.lower())})
        if key not in used:
            c.emit("relationship", r.from_table, r.label,
                   f"Inactive relationship {r.label} is never activated with "
                   "USERELATIONSHIP. Use it in a measure or remove it.",
                   tables=(r.from_table, r.to_table))


_ONE_MINUS = (
    re.compile(r"[0-9]+\s*[-+]\s*\(*\s*SUM\s*\(\s*'*[A-Za-z0-9 _]+'*\s*\[[A-Za-z0-9 _]+\]\s*\)\s*/",
               re.IGNORECASE),
    re.compile(r"[0-9]+\s*[-+]\s*DIVIDE\s*\(", re.IGNORECASE),
)


@check("AVOID_USING_'1-(X/Y)'_SYNTAX")
def _one_minus(c: _Ctx) -> None:
    for d in c.m.dax_objects():
        if d.kind in ("measure", "column", "calculation_item"):
            code = code_only(d.expr)
            if any(rx.search(code) for rx in _ONE_MINUS):
                c.emit(d.kind, d.table, d.name,
                       f"'{d.name}' uses the 1-(x/y) / 1+(x/y) pattern. Compute the "
                       "percentage change directly (DIVIDE(new - old, old)); the "
                       "1+/- form never returns BLANK and inflates result rows.")


@check("EVALUATEANDLOG_SHOULD_NOT_BE_USED_IN_PRODUCTION_MODELS")
def _evaluateandlog(c: _Ctx) -> None:
    for d in c.m.dax_objects():
        if d.kind == "measure" and "EVALUATEANDLOG" in c.m.refs(d).functions:
            c.emit("measure", d.table, d.name,
                   f"Measure '{d.name}' calls EVALUATEANDLOG, which is for debugging "
                   "only. Remove it before publishing.")


@check("DAX_TODO")
def _todo(c: _Ctx) -> None:
    for d in c.m.dax_objects():
        if d.kind in ("measure", "column", "calculation_item", "table") \
                and "todo" in d.expr.lower():
            c.emit(d.kind, d.table, d.name, f"'{d.name}' contains a TODO note; revisit it.")


# --- Error Prevention -----------------------------------------------------------

@check("DATA_COLUMNS_MUST_HAVE_A_SOURCE_COLUMN")
def _source_column(c: _Ctx) -> None:
    for col in c.m.columns():
        t = col.table
        if col.is_calculated or t.is_calculated or t.is_calc_group:
            continue
        if (col.col_type or "").lower() in ("rownumber", "calculated", "calculatedtablecolumn"):
            continue
        if not (col.source_column or "").strip():
            c.emit("column", t.name, col.name,
                   f"Data column {_col_label(col)} has no sourceColumn; processing the "
                   "model will fail. Set sourceColumn or make it a calculated column.")


@check("EXPRESSION_RELIANT_OBJECTS_MUST_HAVE_AN_EXPRESSION")
def _has_expression(c: _Ctx) -> None:
    for d in c.m.dax_objects():
        if d.kind in ("measure", "column", "calculation_item") and not d.expr.strip():
            c.emit(d.kind, d.table, d.name,
                   f"{d.kind.replace('_', ' ').capitalize()} '{d.name}' has no DAX "
                   "expression and will show nothing.")


@check("RELATIONSHIP_COLUMNS_SAME_DATA_TYPE")
def _rel_same_type(c: _Ctx) -> None:
    for r in c.m.relationships:
        a, b = c.m.column(r.from_table, r.from_column), c.m.column(r.to_table, r.to_column)
        if a and b and a.dtype and b.dtype and a.dtype != b.dtype:
            c.emit("relationship", r.from_table, r.label,
                   f"Relationship {r.label} joins {a.data_type} to {b.data_type}. "
                   "Use the same data type (ideally integer) on both sides.",
                   tables=(r.from_table, r.to_table))


@check("AVOID_INVALID_NAME_CHARACTERS")
def _bad_name_chars(c: _Ctx) -> None:
    for otype, table, name, _ in _names(c.m):
        if _has_invalid_chars(name):
            c.emit(otype, table, name,
                   f"The name {name!r} contains a control character that is invalid in "
                   "object names.")


@check("AVOID_INVALID_DESCRIPTION_CHARACTERS")
def _bad_desc_chars(c: _Ctx) -> None:
    for otype, table, name, obj in _names(c.m):
        desc = getattr(obj, "description", "") or ""
        if desc and _has_invalid_chars(desc):
            c.emit(otype, table, name,
                   f"The description of '{name}' contains a control character that is "
                   "invalid.")


@check("AVOID_THE_USERELATIONSHIP_FUNCTION_AND_RLS_AGAINST_THE_SAME_TABLE")
def _userel_rls(c: _Ctx) -> None:
    rls = _rls_tables(c.m)
    if not rls:
        return
    hit: dict[str, set[str]] = {}
    for d in c.m.dax_objects():
        if d.kind not in ("measure", "calculation_item"):
            continue
        for name, args in iter_calls(c.m.tokens(d)):
            if name == "USERELATIONSHIP":
                for a in args[:2]:
                    q = _qualified_col(a)
                    if q and q[0].lower() in rls:
                        hit.setdefault(q[0].lower(), set()).add(d.name)
    for t in c.m.tables:
        if t.name.lower() in hit:
            c.emit("table", t.name, t.name,
                   f"Table '{t.name}' has row-level security and is used in "
                   f"USERELATIONSHIP by {', '.join(sorted(hit[t.name.lower()]))}; that "
                   "combination raises an error at query time.")


@check("CALCULATION_GROUPS_NO_PRECEDENCE_CONFLICT")
def _precedence(c: _Ctx) -> None:
    by: dict[int, list[MTable]] = {}
    for t in c.m.tables:
        if t.is_calc_group and t.precedence is not None:
            by.setdefault(t.precedence, []).append(t)
    for prec, grp in by.items():
        if len(grp) > 1:
            for t in grp:
                others = ", ".join(x.name for x in grp if x is not t)
                c.emit("calculation_group", t.name, t.name,
                       f"Calculation group '{t.name}' has precedence {prec}, the same as "
                       f"{others}. Precedence values must be unique.",
                       tables=tuple(x.name for x in grp))


# --- Maintenance -----------------------------------------------------------------

def _dax_referenced_columns(m: BpaModel) -> set[tuple[str, str]]:
    """Columns that non-measure DAX, a sort-by or a hierarchy keeps alive."""
    out: set[tuple[str, str]] = set()
    by_name: dict[str, list[str]] = {}
    for col in m.columns():
        by_name.setdefault(col.name.lower(), []).append(col.table.name.lower())
    for d in m.dax_objects():
        if d.kind == "measure":
            continue
        refs = m.refs(d)
        for t, n in refs.columns:
            out.add((t.lower(), n.lower()))
        for n in refs.unqualified:
            for t in by_name.get(n.lower(), []):
                out.add((t, n.lower()))
    for t in m.tables:
        for col in t.columns:
            if col.sort_by:
                out.add((t.name.lower(), unquote_name(col.sort_by).lower()))
        for _, levels in t.hierarchies:
            for lv in levels:
                out.add((t.name.lower(), lv.lower()))
    return out


@check("REMOVE_UNUSED_COLUMNS")
def _unused_columns(c: _Ctx) -> None:
    if c.m.usage is None:
        return
    unused = set(c.m.usage["unused"]["columns"])
    kept = _dax_referenced_columns(c.m)
    for col in c.m.columns():
        t = col.table
        if t.is_calc_group or f"{t.name}.{col.name}" not in unused:
            continue
        if (t.name.lower(), col.name.lower()) in kept:
            continue
        c.emit("column", t.name, col.name,
               f"{_col_label(col)} is not used by any visual, filter, relationship or "
               "measure the report depends on. Remove it to shrink the model.")


@check("UNUSED_MEASURES")
def _unused_measures(c: _Ctx) -> None:
    if c.m.usage is None:
        return
    unused = set(c.m.usage["unused"]["measures"])
    kept: set[str] = set()
    for d in c.m.dax_objects():
        if d.kind != "measure":
            kept |= {n.lower() for n in c.m.refs(d).measures}
    for m in c.m.measures():
        if m.name in unused and m.name.lower() not in kept:
            c.emit("measure", m.table.name, m.name,
                   f"Measure '{m.name}' is not shown in the report and no used measure "
                   "depends on it. Remove it if it is no longer needed.")


@check("ENSURE_TABLES_HAVE_RELATIONSHIPS")
def _tables_related(c: _Ctx) -> None:
    related = c.m.rel_tables()
    for t in c.m.tables:
        if t.is_calc_group or t.is_auto_date or t.name.lower() in related:
            continue
        if t.measures and not any(col.visible for col in t.columns):
            continue                       # measure-only table
        c.emit("table", t.name, t.name,
               f"Table '{t.name}' has no relationship to any other table.")


@check("OBJECTS_WITH_NO_DESCRIPTION")
def _no_description(c: _Ctx) -> None:
    for t in c.m.tables:
        if t.is_auto_date:
            continue
        if not t.is_hidden and not t.description.strip():
            c.emit("calculation_group" if t.is_calc_group else "table", t.name, t.name,
                   f"Table '{t.name}' has no description.")
        if t.is_hidden:
            continue
        for col in ([] if t.is_calc_group else t.columns):
            if not col.is_hidden and not col.description.strip():
                c.emit("column", t.name, col.name, f"{_col_label(col)} has no description.")
        for m in t.measures:
            if not m.is_hidden and not m.description.strip():
                c.emit("measure", t.name, m.name, f"Measure '{m.name}' has no description.")


@check("CALCULATION_GROUPS_WITH_NO_CALCULATION_ITEMS")
def _empty_calc_group(c: _Ctx) -> None:
    for t in c.m.tables:
        if t.is_calc_group and not t.calc_items:
            c.emit("calculation_group", t.name, t.name,
                   f"Calculation group '{t.name}' has no calculation items and does nothing.")


# --- Naming Conventions -------------------------------------------------------------

@check("PARTITION_NAME_SHOULD_MATCH_TABLE_NAME_FOR_SINGLE_PARTITION_TABLES")
def _partition_name(c: _Ctx) -> None:
    for t in c.m.tables:
        if len(t.partitions) == 1 and t.partitions[0].name != t.name:
            p = t.partitions[0]
            c.emit("partition", t.name, p.name,
                   f"The only partition of '{t.name}' is named '{p.name}'; name it "
                   f"'{t.name}'.")


@check("SPECIAL_CHARS_IN_OBJECT_NAMES")
def _special_chars(c: _Ctx) -> None:
    for otype, table, name, _ in _names(c.m):
        if any(ch in name for ch in ("\t", "\n", "\r")):
            c.emit(otype, table, name, f"The name {name!r} contains a tab or line break.")


def _lower_first(name: str) -> bool:
    return bool(name) and name[0].islower()


@check("UPPERCASE_FIRST_LETTER_MEASURES_TABLES")
def _cap_measures_tables(c: _Ctx) -> None:
    for t in c.m.tables:
        if not t.is_hidden and _lower_first(t.name):
            c.emit("table", t.name, t.name,
                   f"Table '{t.name}' should start with an upper-case letter "
                   "(avoid prefixes such as 'dimSales').")
        for m in t.measures:
            if m.visible and _lower_first(m.name):
                c.emit("measure", t.name, m.name,
                       f"Measure '{m.name}' should start with an upper-case letter.")


@check("UPPERCASE_FIRST_LETTER_COLUMNS_HIERARCHIES")
def _cap_columns(c: _Ctx) -> None:
    for col in c.m.columns():
        if col.visible and _lower_first(col.name):
            c.emit("column", col.table.name, col.name,
                   f"Column {_col_label(col)} should start with an upper-case letter.")


@check("RELATIONSHIP_COLUMN_NAMES")
def _rel_names(c: _Ctx) -> None:
    pairs: dict[tuple[str, str], int] = {}
    for r in c.m.relationships:
        key = (r.from_table.lower(), r.to_table.lower())
        pairs[key] = pairs.get(key, 0) + 1
    for r in c.m.relationships:
        n = pairs[(r.from_table.lower(), r.to_table.lower())]
        f, t = r.from_column.lower(), r.to_column.lower()
        if (n == 1 and f != t) or (n > 1 and not f.endswith(t)):
            c.emit("relationship", r.from_table, r.label,
                   f"Relationship {r.label}: " + (
                       "the columns should have the same name."
                       if n == 1 else
                       "with several relationships between the two tables, the "
                       "from-column name must end with the to-column name."),
                   tables=(r.from_table, r.to_table))


# --- Formatting ---------------------------------------------------------------------

@check("FORMAT_FLAG_COLUMNS_AS_YES/NO_VALUE_STRINGS")
def _flag_columns(c: _Ctx) -> None:
    for col in c.m.columns():
        if not col.visible:
            continue
        if (re.match(r"Is[A-Z_ ]", col.name) and col.dtype == "int64") or (
                col.name.endswith(" Flag") and col.dtype != "string"):
            c.emit("column", col.table.name, col.name,
                   f"Flag column {_col_label(col)} should be text ('Yes'/'No') rather "
                   "than 0/1.")


@check("OBJECTS_SHOULD_NOT_START_OR_END_WITH_A_SPACE")
def _trim_names(c: _Ctx) -> None:
    for otype, table, name, _ in _names(c.m):
        if name != name.strip(" "):
            c.emit(otype, table, name, f"The name {name!r} starts or ends with a space.")


@check("PROVIDE_FORMAT_STRING_FOR_MEASURES")
def _measure_format(c: _Ctx) -> None:
    for m in c.m.measures():
        if not m.visible or m.format_string or m.has_format_expr:
            continue
        numeric = measure_result_kind(c.m, m) == "numeric"
        fmt = default_format_string(m.name)
        c.emit("measure", m.table.name, m.name,
               f"Measure '{m.name}' has no format string."
               + (f" A numeric default ({fmt}) can be applied automatically." if numeric else ""),
               fixable=numeric, data={"format": fmt} if numeric else {})


@check("NUMERIC_COLUMN_SUMMARIZE_BY")
def _summarize_by(c: _Ctx) -> None:
    for col in c.m.columns():
        if col.visible and col.is_numeric and (col.summarize_by or "default").lower() != "none":
            c.emit("column", col.table.name, col.name,
                   f"Numeric column {_col_label(col)} summarizes by "
                   f"'{col.summarize_by or 'default'}'. Set summarizeBy: none and use "
                   "a measure to avoid accidental summation.")


@check("PERCENTAGE_FORMATTING")
def _percent_format(c: _Ctx) -> None:
    for m in c.m.measures():
        fs = m.format_string or ""
        if "%" in fs and "," not in fs:
            c.emit("measure", m.table.name, m.name,
                   f"Percentage format '{fs}' of '{m.name}' has no thousands separator; "
                   "use e.g. '#,0.0%;-#,0.0%;#,0.0%'.")


@check("RELATIONSHIP_COLUMNS_SHOULD_BE_OF_INTEGER_DATA_TYPE")
def _rel_integer(c: _Ctx) -> None:
    seen: set[tuple[str, str]] = set()
    for r in c.m.relationships:
        for tname, cname, other in ((r.from_table, r.from_column, r.to_table),
                                    (r.to_table, r.to_column, r.from_table)):
            col = c.m.column(tname, cname)
            if col is None or not col.dtype or col.dtype == "int64":
                continue
            ot = c.m.table(other)
            if col.dtype == "datetime" and (
                    (col.table.data_category or "").lower() == "time"
                    or (ot is not None and (ot.data_category or "").lower() == "time")):
                continue                    # joins to a date table are the norm
            if (tname.lower(), cname.lower()) in seen:
                continue
            seen.add((tname.lower(), cname.lower()))
            c.emit("column", col.table.name, col.name,
                   f"Relationship column {_col_label(col)} is {col.data_type}; integer "
                   "keys are smaller and faster.", tables=(col.table.name, other))


_CATEGORY_NAMES = {"latitude": ("Latitude", {"double", "decimal"}),
                   "longitude": ("Longitude", {"double", "decimal"}),
                   "weburl": ("WebUrl", {"string"}),
                   "imageurl": ("ImageUrl", {"string"})}
_GEO_WORDS = ("country", "continent", "city")


@check("ADD_DATA_CATEGORY_FOR_COLUMNS")
def _data_category(c: _Ctx) -> None:
    for col in c.m.columns():
        if (col.data_category or "").strip():
            continue
        key = re.sub(r"[\s_\-]+", "", col.name.lower())
        cat = _CATEGORY_NAMES.get(key)
        if cat and col.dtype in cat[1]:
            c.emit("column", col.table.name, col.name,
                   f"Column {_col_label(col)} should have dataCategory: {cat[0]}.",
                   fixable=True, data={"category": cat[0]})
        elif col.dtype == "string" and any(w in col.name.lower() for w in _GEO_WORDS):
            c.emit("column", col.table.name, col.name,
                   f"Column {_col_label(col)} looks geographic; set its data category "
                   "(Country/Region, City, Continent) so maps can geocode it.")


@check("HIDE_FOREIGN_KEYS")
def _hide_fk(c: _Ctx) -> None:
    done: set[tuple[str, str]] = set()
    for r in c.m.relationships:
        if r.from_card != "many":
            continue
        col = c.m.column(r.from_table, r.from_column)
        if col is None or col.is_hidden or col.table.is_hidden:
            continue
        key = (col.table.name.lower(), col.name.lower())
        if key in done:
            continue
        done.add(key)
        c.emit("column", col.table.name, col.name,
               f"Foreign key {_col_label(col)} is visible. Hide it; filter through the "
               f"related dimension '{r.to_table}' instead.",
               fixable=True, tables=(col.table.name, r.to_table))


@check("MARK_PRIMARY_KEYS")
def _primary_keys(c: _Ctx) -> None:
    done: set[tuple[str, str]] = set()
    for r in c.m.relationships:
        if r.to_card != "one":
            continue
        col = c.m.column(r.to_table, r.to_column)
        if col is None or col.is_key or (col.table.data_category or "").lower() == "time":
            continue
        key = (col.table.name.lower(), col.name.lower())
        if key not in done:
            done.add(key)
            c.emit("column", col.table.name, col.name,
                   f"Column {_col_label(col)} is the one side of {r.label} but is not "
                   "marked as a key (isKey).")


_AGG_FUNCS = frozenset({"COUNT", "COUNTBLANK", "SUM", "AVERAGE", "VALUES", "DISTINCT",
                        "DISTINCTCOUNT", "MIN", "MAX", "COUNTA", "AVERAGEA", "MAXA", "MINA"})


@check("HIDE_FACT_TABLE_COLUMNS")
def _hide_fact_columns(c: _Ctx) -> None:
    agg: dict[tuple[str, str], str] = {}
    for m in c.m.measures():
        toks = [t for t in tokenize(m.expression) if t.kind not in ("WS", "COMMENT")]
        for name, args in iter_calls(toks):
            if name in _AGG_FUNCS and len(args) == 1:
                q = _qualified_col(args[0])
                if q:
                    agg.setdefault((q[0].lower(), q[1].lower()), m.name)
    for col in c.m.columns():
        key = (col.table.name.lower(), col.name.lower())
        if col.visible and col.is_numeric and key in agg:
            c.emit("column", col.table.name, col.name,
                   f"Column {_col_label(col)} is aggregated by measure '{agg[key]}'. "
                   "Hide it so users work with the measure.")


@check("MONTH_(AS_A_STRING)_MUST_BE_SORTED")
def _month_sorted(c: _Ctx) -> None:
    for col in c.m.columns():
        up = col.name.upper()
        if "MONTH" in up and "MONTHS" not in up and col.dtype == "string" and not col.sort_by:
            c.emit("column", col.table.name, col.name,
                   f"Month column {_col_label(col)} is text with no sortByColumn; it will "
                   "sort alphabetically (April, August ...). Sort it by the month number.")


@check("APPLY_FORMAT_STRING_COLUMNS")
def _column_format(c: _Ctx) -> None:
    for col in c.m.columns():
        if col.visible and not col.format_string and col.dtype in (
                "int64", "datetime", "double", "decimal"):
            c.emit("column", col.table.name, col.name,
                   f"Visible column {_col_label(col)} has no format string.")


# =============================================================================
# 5. Safe fixers (surgical line insertions)
# =============================================================================
# A fixer receives the table file's text (LF newlines; PbipProject._write_text
# re-applies the file's own line endings / BOM) and a finding, and returns
# ``(new_text, description)`` or ``None`` when there is nothing to do. They only
# ever *insert* one property line, and refuse when it is already present, so
# they are idempotent and every other byte of the file is untouched.

def _find_member(text: str, table: str, kw: str, name: str) -> Node | None:
    tn = next((n for n in parse_nodes(text)
               if n.kw == "table" and n.name.lower() == (table or "").lower()), None)
    if tn is None:
        return None
    exact = [c for c in tn.kids(kw) if c.name == name]
    if exact:
        return exact[0]
    low = name.lower()
    return next((c for c in tn.kids(kw) if c.name.lower() == low), None)


def _insert_line(text: str, at: int, line: str) -> str:
    lines = text.split("\n")
    lines.insert(at, line)
    return "\n".join(lines)


def _property_slot(node: Node) -> int:
    """Line index where a new property of a column belongs (after dataType)."""
    dt = node.props.get("dataType")
    return dt.end if dt is not None else node.body_end


def _fix_format_string(text: str, f: Finding):
    fmt = f.data.get("format")
    node = _find_member(text, f.table or "", "measure", f.name)
    if (not fmt or node is None or "formatString" in node.props
            or "formatStringDefinition" in node.props):
        return None
    line = "\t" * (node.indent + 1) + f"formatString: {fmt}"
    return _insert_line(text, node.body_end, line), f"formatString: {fmt}"


def _fix_hide_column(text: str, f: Finding):
    node = _find_member(text, f.table or "", "column", f.name)
    if node is None or node.flag("isHidden"):
        return None
    line = "\t" * (node.indent + 1) + "isHidden"
    return _insert_line(text, _property_slot(node), line), "isHidden"


def _fix_data_category(text: str, f: Finding):
    cat = f.data.get("category")
    node = _find_member(text, f.table or "", "column", f.name)
    if not cat or node is None or node.prop("dataCategory"):
        return None
    line = "\t" * (node.indent + 1) + f"dataCategory: {cat}"
    return _insert_line(text, _property_slot(node), line), f"dataCategory: {cat}"


FIXERS: dict[str, Callable] = {
    "default_format_string": _fix_format_string,
    "hide_column": _fix_hide_column,
    "set_data_category": _fix_data_category,
}


# =============================================================================
# 6. Catalog, custom rules, engine
# =============================================================================

_CATALOG: list[RuleDef] | None = None


def builtin_rules() -> list[RuleDef]:
    """The rule catalog from ``resources/bpa_rules.json``, bound to its checks."""
    global _CATALOG
    if _CATALOG is None:
        data = json.loads(RESOURCE_PATH.read_text(encoding="utf-8"))
        rules: list[RuleDef] = []
        for r in data["rules"]:
            fn = _CHECKS.get(r["id"])
            if fn is None:
                raise RuntimeError(f"BPA rule {r['id']} in {RESOURCE_PATH.name} has no "
                                   "check implementation (core/bpa.py).")
            if r.get("fixer") and r["fixer"] not in FIXERS:
                raise RuntimeError(f"BPA rule {r['id']} names unknown fixer {r['fixer']!r}.")
            rules.append(RuleDef(
                id=r["id"], category=r["category"], severity=int(r["severity"]),
                description=r.get("description", ""), scopes=tuple(r.get("scopes", ())),
                aliases=tuple(r.get("aliases", ())), fixer=r.get("fixer"),
                origin=r.get("origin", "TabularEditor"),
                needs_report=bool(r.get("needs_report", False)),
                title=r.get("title", ""), check=fn))
        _CATALOG = rules
    return _CATALOG


_CUSTOM_SCOPES = ("measure", "column", "table")


def _custom_rule(item, taken: set[str]) -> RuleDef:
    if not isinstance(item, dict):
        raise ValueError("each rule must be a JSON object")
    rid = item.get("id")
    if not isinstance(rid, str) or not rid.strip():
        raise ValueError("missing string 'id'")
    if norm_id(rid) in taken:
        raise ValueError(f"id {rid!r} duplicates an existing rule id or alias")
    scope = item.get("scope")
    if scope not in _CUSTOM_SCOPES:
        raise ValueError(f"'scope' must be one of {', '.join(_CUSTOM_SCOPES)} (got {scope!r})")
    severity = item.get("severity", 2)
    if not isinstance(severity, int) or isinstance(severity, bool) or not 1 <= severity <= 3:
        raise ValueError("'severity' must be an integer from 1 to 3")
    regexes: dict[str, re.Pattern | None] = {}
    for key in ("name_regex", "expression_regex"):
        pat = item.get(key)
        if pat is None:
            regexes[key] = None
            continue
        if not isinstance(pat, str):
            raise ValueError(f"'{key}' must be a string")
        try:
            regexes[key] = re.compile(pat)
        except re.error as exc:
            raise ValueError(f"'{key}' is not a valid regular expression: {exc}") from None
    if regexes["name_regex"] is None and regexes["expression_regex"] is None:
        raise ValueError("give at least one of 'name_regex' or 'expression_regex'")
    category = item.get("category") or "Custom"
    message = item.get("message") or f"Custom rule {rid} matched."
    if not isinstance(category, str) or not isinstance(message, str):
        raise ValueError("'category' and 'message' must be strings")
    name_re, expr_re = regexes["name_regex"], regexes["expression_regex"]

    def run(c: _Ctx) -> None:
        for otype, table, name, expr in _custom_targets(c.m, scope):
            if name_re is not None and not name_re.search(name):
                continue
            if expr_re is not None and (expr is None or not expr_re.search(expr)):
                continue
            c.emit(otype, table, name,
                   message.replace("{name}", name).replace("{table}", table or ""))

    return RuleDef(id=rid.strip(), category=category, severity=severity,
                   description=str(item.get("description") or message),
                   scopes=(scope,), origin="custom", check=run)


def _custom_targets(m: BpaModel, scope: str):
    if scope == "measure":
        for x in m.measures():
            yield "measure", x.table.name, x.name, x.expression
    elif scope == "column":
        for col in m.columns():
            yield "column", col.table.name, col.name, col.expression
    else:
        for t in m.tables:
            src = "\n".join(p.source for p in t.partitions if p.source)
            yield "table", t.name, t.name, src or None


def _project_root(project) -> Path:
    from core.journal import project_root
    return project_root(project)


def load_custom_rules(project) -> tuple[list[RuleDef], list[str]]:
    """Rules from ``<project root>/.pbi-mcp/bpa_rules.json`` (simple patterns).

    The file holds a JSON list (or ``{"rules": [...]}``) of objects::

        {"id": "NO_TEMP_MEASURES", "severity": 2, "category": "Naming Conventions",
         "scope": "measure",          # measure | column | table
         "name_regex": "^tmp_",       # regex searched in the object name
         "expression_regex": "...",   # regex searched in the DAX / M expression
         "message": "Remove temporary measure {name}"}

    Both regexes given means both must match. Invalid entries are skipped and
    reported as warnings; a bad file never blocks the built-in rules.
    """
    path = _project_root(project) / CUSTOM_RULES_FILE
    if not path.is_file():
        return [], []
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        return [], [f"{CUSTOM_RULES_FILE.as_posix()}: cannot read custom rules ({exc})"]
    items = data.get("rules") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return [], [f"{CUSTOM_RULES_FILE.as_posix()}: expected a JSON list of rules "
                    "(or an object with a 'rules' list)"]
    taken = {norm_id(x) for r in builtin_rules() for x in (r.id, *r.aliases)}
    rules: list[RuleDef] = []
    warnings: list[str] = []
    for i, item in enumerate(items, 1):
        try:
            rule = _custom_rule(item, taken)
        except ValueError as exc:
            warnings.append(f"{CUSTOM_RULES_FILE.as_posix()} rule #{i}: {exc}")
            continue
        taken.add(norm_id(rule.id))
        rules.append(rule)
    return rules, warnings


def all_rules(project=None) -> tuple[list[RuleDef], list[str]]:
    rules = list(builtin_rules())
    warnings: list[str] = []
    if project is not None:
        custom, warnings = load_custom_rules(project)
        rules.extend(custom)
    return rules, warnings


def resolve_rules(names, catalog: list[RuleDef], what: str = "rule") -> set[str]:
    """Map rule IDs / aliases (case- and punctuation-insensitive) to canonical IDs."""
    if isinstance(names, str):
        names = [names]
    lookup: dict[str, str] = {}
    for r in catalog:
        for x in (r.id, *r.aliases):
            lookup.setdefault(norm_id(x), r.id)
    out: set[str] = set()
    unknown: list[str] = []
    for n in names:
        rid = lookup.get(norm_id(str(n)))
        if rid is None:
            unknown.append(str(n))
        else:
            out.add(rid)
    if unknown:
        import difflib
        hints = []
        for u in unknown:
            close = difflib.get_close_matches(norm_id(u), list(lookup), n=2, cutoff=0.6)
            if close:
                hints.append(f"{u} (did you mean {', '.join(lookup[c] for c in close)}?)")
            else:
                hints.append(u)
        raise ValueError(f"Unknown {what}(s): {'; '.join(hints)}. "
                         "Call pbi_bpa_rules to list the available rule IDs.")
    return out


def _resolve_categories(categories, catalog: list[RuleDef]) -> set[str]:
    if isinstance(categories, str):
        categories = [categories]
    known = {r.category.lower(): r.category for r in catalog}
    out: set[str] = set()
    bad = []
    for c in categories:
        key = str(c).strip().lower()
        if key in known:
            out.add(known[key])
        else:
            bad.append(str(c))
    if bad:
        valid = sorted(set(CATEGORIES) | set(known.values()))
        raise ValueError(f"Unknown category(ies): {', '.join(bad)}. Valid categories: "
                         f"{', '.join(valid)}.")
    return out


def _check_severity(severity_min) -> int:
    if isinstance(severity_min, bool) or not isinstance(severity_min, int) \
            or not 1 <= severity_min <= 3:
        raise ValueError("severity_min must be 1, 2 or 3 (3 = most severe).")
    return severity_min


def _run_rule(model: BpaModel, rule: RuleDef, warnings: list[str]) -> list[Finding]:
    ctx = _Ctx(model, rule)
    try:
        rule.check(ctx)
    except Exception as exc:  # a broken rule must not hide the others
        warnings.append(f"Rule {rule.id} failed: {type(exc).__name__}: {exc}")
        return []
    return ctx.findings


def analyze(project, *, categories=None, severity_min: int = 1, table: str | None = None,
            rules=None, exclude_rules=None, max_findings: int | None = None) -> dict:
    """Run the BPA rules over the project and return findings + summary."""
    catalog, warnings = all_rules(project)
    sev = _check_severity(severity_min)
    selected = [r for r in catalog if r.severity >= sev]
    if categories:
        cats = _resolve_categories(categories, catalog)
        selected = [r for r in selected if r.category in cats]
    if rules:
        wanted = resolve_rules(rules, catalog)
        selected = [r for r in selected if r.id in wanted]
    if exclude_rules:
        dropped = resolve_rules(exclude_rules, catalog, "exclude_rules rule")
        selected = [r for r in selected if r.id not in dropped]

    model = load_model(project, with_usage=any(r.needs_report for r in selected))
    findings: list[Finding] = []
    for rule in selected:
        findings.extend(_run_rule(model, rule, warnings))
    if table:
        low = table.lower()
        if model.table(table) is None:
            raise ValueError(f"Table {table!r} not found. Tables: "
                             f"{', '.join(t.name for t in model.tables)}.")
        findings = [f for f in findings
                    if low in (t.lower() for t in f.tables) or (f.table or "").lower() == low]

    order = {c: i for i, c in enumerate(CATEGORIES)}
    findings.sort(key=lambda f: (-f.severity, order.get(f.category, 99), f.rule_id,
                                 (f.table or ""), f.name))
    by_cat = {c: 0 for c in CATEGORIES}
    by_sev = {"3": 0, "2": 0, "1": 0}
    by_rule: dict[str, int] = {}
    for f in findings:
        by_cat[f.category] = by_cat.get(f.category, 0) + 1
        by_sev[str(f.severity)] += 1
        by_rule[f.rule_id] = by_rule.get(f.rule_id, 0) + 1
    shown = findings if max_findings is None else findings[:max(0, max_findings)]
    return {
        "findings": [f.as_dict() for f in shown],
        "summary": {"total": len(findings), "by_category": by_cat,
                    "by_severity": by_sev, "by_rule": by_rule},
        "fixable_count": sum(1 for f in findings if f.fixable),
        "rules_evaluated": len(selected),
        "truncated": len(shown) < len(findings),
        "notes": list(dict.fromkeys(model.notes)),
        "warnings": warnings,
    }


def apply_fixes(project, *, rules=None, table: str | None = None) -> dict:
    """Apply the safe fixers; returns what changed. Idempotent."""
    catalog, warnings = all_rules(project)
    fixable = [r for r in catalog if r.fixer]
    if rules:
        wanted = resolve_rules(rules, catalog)
        not_fixable = sorted(r for r in wanted if r not in {x.id for x in fixable})
        if not_fixable:
            raise ValueError(
                f"No automatic fixer for: {', '.join(not_fixable)}. Fixable rules: "
                f"{', '.join(x.id for x in fixable)}.")
        targets = [r for r in fixable if r.id in wanted]
    else:
        targets = fixable
    changed: list[dict] = []
    files: list[str] = []
    root = _project_root(project)
    for rule in targets:
        model = load_model(project, with_usage=False)
        if table and model.table(table) is None:
            raise ValueError(f"Table {table!r} not found. Tables: "
                             f"{', '.join(t.name for t in model.tables)}.")
        by_file: dict[Path, list[Finding]] = {}
        for f in _run_rule(model, rule, warnings):
            if not f.fixable or (table and (f.table or "").lower() != table.lower()):
                continue
            tbl = model.table(f.table)
            if tbl is not None:
                by_file.setdefault(tbl.file, []).append(f)
        fixer = FIXERS[rule.fixer]
        for path, fs in by_file.items():
            text = _read(path)
            new = text
            for f in fs:
                res = fixer(new, f)
                if res is not None:
                    new, what = res
                    changed.append({"rule_id": rule.id, "object_type": f.object_type,
                                    "table": f.table, "name": f.name, "change": what})
            if new != text:
                project._write_text(path, new)
                try:
                    rel = path.relative_to(root).as_posix()
                except ValueError:
                    rel = str(path)
                if rel not in files:
                    files.append(rel)
    return {"count": len(changed), "changed": changed, "files": files,
            "rules_applied": sorted({c["rule_id"] for c in changed}),
            "warnings": warnings}


def list_rules(project=None) -> list[dict]:
    """The catalog (plus the project's custom rules, when a project is given)."""
    rules, _ = all_rules(project)
    return [r.listing() for r in rules]
