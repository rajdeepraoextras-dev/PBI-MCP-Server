"""DAX tokenizer, reference extractor and reference renamer (no dependencies).

Three public functions plus two dataclasses:

  * `tokenize(dax)`             — lossless token stream; re-joining the token
                                  texts reproduces the input byte for byte.
  * `parse_references(dax)`     — measures / columns / tables / functions /
                                  variables referenced by a DAX expression.
  * `rename_references(dax, …)` — token-level rename of a measure, column or
                                  table that leaves everything else untouched.

Reference semantics (shared by the last two):

  * `Table[Col]`, `'Table'[Col]`  -> column (table, col)
  * bare `[Name]`                 -> measure (or `unqualified` when a
                                     `known_measures` set says it is not one)
  * `IDENT(`                      -> function (upper-cased)
  * bare `IDENT` / `'Quoted'`     -> table, unless it is a keyword or a name
                                     declared earlier with `VAR`
  * `VAR x`                       -> declares variable `x`

Nothing inside a string literal or a comment is ever a reference. The
tokenizer never raises: malformed input yields `UNKNOWN` tokens.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field

KEYWORDS = frozenset({
    "VAR", "RETURN", "EVALUATE", "DEFINE", "MEASURE", "TABLE", "COLUMN",
    "ORDER", "BY", "START", "AT", "ASC", "DESC", "TRUE", "FALSE", "NOT",
    "IN", "AND", "OR",
})
# Keywords that also exist in function form (`NOT(x)`, `TRUE()`, `AND(a, b)`).
_FUNC_KEYWORDS = frozenset({"TRUE", "FALSE", "NOT", "AND", "OR"})

_TOKEN = re.compile(
    r"(?P<WS>\s+)"
    r"|(?P<COMMENT>/\*.*?\*/|//[^\r\n]*|--[^\r\n]*)"
    r'|(?P<STRING>"(?:[^"]|"")*")'
    r"|(?P<COLUMN>\[(?:[^\]\r\n]|\]\])*\])"
    r"|(?P<TABLE>'(?:[^'\r\n]|'')*')"
    r"|(?P<NUMBER>\d+(?:\.\d*)?(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?)"
    r"|(?P<IDENT>[^\W\d]\w*(?:\.\w+)*)"          # dotted: NORM.DIST, VAR.P
    r"|(?P<OP>:=|<=|>=|<>|==|&&|\|\||[-+*/^&=<>!])"
    r"|(?P<PUNCT>[(),{}.;])",
    re.DOTALL,
)
_SIMPLE_NAME = re.compile(r"[^\W\d]\w*")


@dataclass(frozen=True)
class Token:
    """One lexical unit. `start`/`end` are offsets into the source; `line`
    and `col` are 1-based."""

    kind: str
    text: str
    start: int
    end: int
    line: int
    col: int


@dataclass
class DaxRefs:
    """References found in a DAX expression."""

    measures: set[str] = field(default_factory=set)
    columns: set[tuple[str, str]] = field(default_factory=set)
    tables: set[str] = field(default_factory=set)
    functions: set[str] = field(default_factory=set)
    variables: set[str] = field(default_factory=set)
    unqualified: set[str] = field(default_factory=set)


# --- tokenizer --------------------------------------------------------------

def tokenize(dax: str) -> list[Token]:
    """Split `dax` into tokens. `"".join(t.text for t in tokens) == dax`."""
    tokens: list[Token] = []
    pos, n = 0, len(dax)
    line, col = 1, 1

    def emit(kind: str, end: int) -> None:
        nonlocal pos, line, col
        text = dax[pos:end]
        tokens.append(Token(kind, text, pos, end, line, col))
        nl = text.count("\n")
        if nl:
            line += nl
            col = len(text) - text.rfind("\n")
        else:
            col += len(text)
        pos = end

    while pos < n:
        m = _TOKEN.match(dax, pos)
        if m is not None:
            emit(m.lastgroup, m.end())
            continue
        ch = dax[pos]
        if ch == '"' or dax.startswith("/*", pos):
            emit("UNKNOWN", n)          # unterminated string / block comment
        else:
            emit("UNKNOWN", pos + 1)    # stray char, or an unterminated [ / '
    return tokens


# --- shared classification --------------------------------------------------

def _is_keyword(tok: Token) -> bool:
    return tok.kind == "IDENT" and tok.text.upper() in KEYWORDS


def _column_name(tok: Token) -> str:
    return tok.text[1:-1].replace("]]", "]").strip()


def _table_name(tok: Token) -> str:
    if tok.kind == "TABLE":
        return tok.text[1:-1].replace("''", "'")
    return tok.text


@dataclass(frozen=True)
class _Ref:
    """One classified reference; `index`/`qualifier` are token indexes."""

    kind: str                   # function | column | bare | table | variable
    index: int
    name: str
    table: str | None = None
    qualifier: int | None = None


def _classify(tokens: list[Token]) -> Iterator[_Ref]:
    sig = [i for i, t in enumerate(tokens) if t.kind not in ("WS", "COMMENT")]
    declared: dict[str, str] = {}      # UPPER -> name as declared
    declare_next = False

    for j, i in enumerate(sig):
        tok = tokens[i]
        nxt = tokens[sig[j + 1]] if j + 1 < len(sig) else None
        prv = tokens[sig[j - 1]] if j > 0 else None
        declaring, declare_next = declare_next, False

        if tok.kind == "COLUMN":
            name = _column_name(tok)
            if prv is not None and (
                    prv.kind == "TABLE"
                    or (prv.kind == "IDENT" and not _is_keyword(prv))):
                yield _Ref("column", i, name, _table_name(prv), sig[j - 1])
            else:
                yield _Ref("bare", i, name)
            continue

        if tok.kind == "TABLE":
            if nxt is None or nxt.kind != "COLUMN":
                yield _Ref("table", i, _table_name(tok))
            continue

        if tok.kind != "IDENT":
            continue

        if declaring and not (nxt is not None and nxt.kind == "COLUMN"):
            declared[tok.text.upper()] = tok.text
            yield _Ref("variable", i, tok.text)
            continue

        upper = tok.text.upper()
        is_kw = upper in KEYWORDS
        follows_paren = nxt is not None and nxt.kind == "PUNCT" and nxt.text == "("
        if is_kw and upper not in _FUNC_KEYWORDS:
            declare_next = upper in ("VAR", "TABLE")
        elif follows_paren:
            yield _Ref("function", i, upper)
        elif nxt is not None and nxt.kind == "COLUMN":
            pass                        # qualifier: reported with the column
        elif is_kw:
            pass
        elif upper in declared:
            yield _Ref("variable", i, declared[upper])
        else:
            yield _Ref("table", i, tok.text)


# --- reference extraction ---------------------------------------------------

def parse_references(dax: str,
                     known_measures: set[str] | None = None) -> DaxRefs:
    """Collect the references in `dax`.

    A bare `[Name]` is a measure unless `known_measures` is given and does
    not contain it, in which case it lands in `unqualified` (it may be a
    column read in row context).
    """
    refs = DaxRefs()
    for ref in _classify(tokenize(dax)):
        if ref.kind == "function":
            refs.functions.add(ref.name)
        elif ref.kind == "column":
            refs.columns.add((ref.table, ref.name))
        elif ref.kind == "bare":
            if known_measures is not None and ref.name not in known_measures:
                refs.unqualified.add(ref.name)
            else:
                refs.measures.add(ref.name)
        elif ref.kind == "table":
            refs.tables.add(ref.name)
        elif ref.kind == "variable":
            refs.variables.add(ref.name)
    return refs


# --- renaming ---------------------------------------------------------------

def _needs_quoting(name: str) -> bool:
    return _SIMPLE_NAME.fullmatch(name) is None or name.upper() in KEYWORDS


def _render_table(name: str, like: Token) -> str:
    """Render a table name, keeping `like`'s quoting unless quoting is forced."""
    if _needs_quoting(name) or like.kind == "TABLE":
        return "'" + name.replace("'", "''") + "'"
    return name


def _render_column(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def _same(a: str, b: str) -> bool:
    return a.casefold() == b.casefold()


def rename_references(
    dax: str,
    *,
    measure: tuple[str, str] | None = None,
    column: tuple[tuple[str, str], tuple[str, str]] | None = None,
    table: tuple[str, str] | None = None,
    known_measures: set[str] | None = None,
) -> str:
    """Rename references in `dax`; whitespace, comments, strings and every
    other token are left exactly as they were.

      measure=(old, new)                      bare `[old]` -> `[new]`
      column=((old_t, old_c), (new_t, new_c)) `old_t[old_c]` -> `new_t[new_c]`;
                                              bare `[old_c]` too, but only when
                                              `known_measures` is given and
                                              says it is not a measure
      table=(old, new)                        qualifiers and bare table refs

    Old names match case-insensitively (DAX identifiers are). A new table
    name is quoted when it needs to be; otherwise the token's original
    quoting style is kept.
    """
    tokens = tokenize(dax)
    out: dict[int, str] = {}

    for ref in _classify(tokens):
        if ref.kind == "bare":
            if measure is not None and _same(ref.name, measure[0]):
                out[ref.index] = _render_column(measure[1])
            elif (column is not None and known_measures is not None
                    and _same(ref.name, column[0][1])
                    and ref.name not in known_measures):
                out[ref.index] = _render_column(column[1][1])
        elif ref.kind == "column":
            if (column is not None and _same(ref.table, column[0][0])
                    and _same(ref.name, column[0][1])):
                out[ref.index] = _render_column(column[1][1])
                out[ref.qualifier] = _render_table(
                    column[1][0], tokens[ref.qualifier])
            elif table is not None and _same(ref.table, table[0]):
                out[ref.qualifier] = _render_table(
                    table[1], tokens[ref.qualifier])
        elif ref.kind == "table":
            if table is not None and _same(ref.name, table[0]):
                out[ref.index] = _render_table(table[1], tokens[ref.index])

    if not out:
        return dax
    return "".join(out.get(i, t.text) for i, t in enumerate(tokens))
