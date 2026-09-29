"""DAX formatter built on ``core.dax_parser.tokenize`` (no dependencies).

``format_dax(dax, style="long"|"short", indent="    ", max_line=100)``

Two guarantees, both asserted by the test-suite over many snippets:

  * lossless   - the token sequence, ignoring whitespace and comments, is
                 identical before and after, except for the case of IDENT
                 tokens (function names and keywords are upper-cased; table,
                 column and measure names, variables, strings and comments are
                 never touched);
  * idempotent - ``format_dax(format_dax(x)) == format_dax(x)``.

Long style (DAX Formatter-like): keywords and function names upper-cased, one
argument per line when a call does not fit ``max_line`` or contains nested
calls, ``VAR name =`` / ``RETURN`` on their own lines with the body indented,
binary operators spaced, commas at line ends, blank lines collapsed, comments
kept in position. Short style: a single line when the whole expression fits
``max_line`` (normalised spacing), otherwise the long layout.

Input that cannot be formatted safely (unterminated string / block comment,
unbalanced brackets) raises :class:`DaxFormatError` (a ``ValueError``) rather
than guessing.

Implementation: tokens -> comment-aware structure tree (call / paren / brace
groups, argument lists, ``VAR``/``RETURN`` statements) -> a Wadler-style
document -> width-aware printer.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.dax_parser import KEYWORDS, tokenize

__all__ = ["format_dax", "DaxFormatError"]


class DaxFormatError(ValueError):
    """The DAX text cannot be formatted (lexical or bracket error)."""


# Keywords that also exist in function form: NOT(x), TRUE(), AND(a, b) ...
_FUNC_KEYWORDS = frozenset({"TRUE", "FALSE", "NOT", "AND", "OR"})
_BLOCK = ("VAR", "RETURN")
_QUERY_STARTERS = ("DEFINE", "EVALUATE")


# --- lexing -------------------------------------------------------------------

@dataclass
class _Atom:
    kind: str                # IDENT NUMBER STRING COLUMN TABLE OP PUNCT COMMENT
    text: str
    before_nl: bool = False  # comments: a line break precedes it (or text start)
    after_nl: bool = False   # comments: a line break follows it (or text end)


def _lex(dax: str) -> list[_Atom]:
    toks = tokenize(dax)
    for t in toks:
        if t.kind == "UNKNOWN":
            raise DaxFormatError(
                f"Cannot format DAX: unterminated string/comment or unexpected "
                f"character {t.text[:12]!r} at line {t.line}, column {t.col}.")
    sig = [t for t in toks if t.kind != "WS"]
    gaps: list[str] = []          # gaps[k] = whitespace before sig[k]
    cur = ""
    for t in toks:
        if t.kind == "WS":
            cur += t.text
        else:
            gaps.append(cur)
            cur = ""
    gaps.append(cur)
    for k in range(len(sig) - 1):   # the tokenizer reads an unterminated `/*` as `/` `*`
        if (sig[k].kind == "OP" and sig[k].text == "/" and sig[k + 1].kind == "OP"
                and sig[k + 1].text == "*" and not gaps[k + 1]):
            raise DaxFormatError(
                f"Cannot format DAX: unterminated block comment at line {sig[k].line}, "
                f"column {sig[k].col}.")
    atoms: list[_Atom] = []
    last = len(sig) - 1
    for k, t in enumerate(sig):
        text = t.text
        a = _Atom(t.kind, text)
        if t.kind == "COMMENT":
            if not text.startswith("/*"):
                a.text = text.rstrip()          # line comment: drop trailing blanks
            a.before_nl = k == 0 or "\n" in gaps[k]
            a.after_nl = k == last or "\n" in gaps[k + 1]
        atoms.append(a)
    return atoms


def _normalize(atoms: list[_Atom]) -> None:
    """Upper-case function names and keywords in place (nothing else)."""
    sig = [i for i, a in enumerate(atoms) if a.kind != "COMMENT"]
    declared: set[str] = set()
    declare_next = False
    in_define = False
    depth = 0
    for pos, i in enumerate(sig):
        a = atoms[i]
        prv = atoms[sig[pos - 1]] if pos else None
        nxt = atoms[sig[pos + 1]] if pos + 1 < len(sig) else None
        if a.kind == "PUNCT":
            if a.text in "({":
                depth += 1
            elif a.text in ")}":
                depth -= 1
            declare_next = False
            continue
        if a.kind != "IDENT":
            declare_next = False
            continue
        upper = a.text.upper()
        declaring, declare_next = declare_next, False
        if declaring and not (nxt is not None and nxt.kind == "COLUMN"):
            declared.add(upper)
            continue                                  # variable: keep its case
        follows_paren = (nxt is not None and nxt.kind == "PUNCT"
                         and nxt.text == "(")
        is_kw = upper in KEYWORDS
        if is_kw and upper not in _FUNC_KEYWORDS:
            if upper in declared:
                continue
            nxt_up = nxt.text.upper() if nxt is not None and nxt.kind == "IDENT" else ""
            prv_up = prv.text.upper() if prv is not None and prv.kind == "IDENT" else ""
            if upper == "VAR":
                a.text = "VAR"
                declare_next = True
            elif upper in ("RETURN", "EVALUATE", "IN"):
                a.text = upper
            elif upper == "DEFINE":
                a.text = upper
                in_define = True
            elif upper == "ORDER":
                if nxt_up == "BY":
                    a.text = upper
            elif upper == "BY":
                if prv_up == "ORDER":
                    a.text = upper
            elif upper == "START":
                if nxt_up == "AT":
                    a.text = upper
            elif upper == "AT":
                if prv_up == "START":
                    a.text = upper
            elif upper in ("ASC", "DESC"):
                if (prv is not None
                        and (prv.kind == "COLUMN"
                             or (prv.kind == "PUNCT" and prv.text in ",)"))
                        and not (nxt is not None and nxt.kind == "COLUMN")
                        and not follows_paren):
                    a.text = upper
            elif upper in ("MEASURE", "COLUMN", "TABLE"):
                if (in_define and depth == 0 and nxt is not None
                        and nxt.kind in ("IDENT", "TABLE")):
                    a.text = upper
            continue
        if follows_paren:
            a.text = upper                            # function call
            continue
        if is_kw and upper not in declared:           # bare TRUE / FALSE / NOT ...
            a.text = upper


# --- structure ----------------------------------------------------------------

@dataclass
class _T:
    a: _Atom


@dataclass
class _C:
    a: _Atom
    cat: str            # standalone | trailing | inline


@dataclass
class _G:
    kind: str                                   # call | paren | brace
    name: _Atom | None = None
    open_cmts: list = field(default_factory=list)
    args: list = field(default_factory=lambda: [[]])
    seps: list = field(default_factory=list)
    after: list = field(default_factory=list)   # comments after each separator
    # parse-time state
    _open_zone: bool = True
    _after_zone: bool = False
    _carry: list = field(default_factory=list)


def _cat(a: _Atom) -> str:
    """standalone: own line(s); trailing: line comment after code on its line;
    inline: block comment that stays between its neighbours."""
    if a.text.startswith("/*"):
        return "standalone" if a.before_nl and a.after_nl else "inline"
    return "standalone" if a.before_nl else "trailing"


def _is_func_name(a: _Atom) -> bool:
    if a.kind != "IDENT":
        return False
    u = a.text.upper()
    return u not in KEYWORDS or u in _FUNC_KEYWORDS


def _end_arg(g: _G) -> list:
    """Close a non-last argument of ``g``: trailing comments move after the
    separator (same-line ones) or to the start of the next argument (own-line
    ones). Returns the carry for the next argument."""
    items = g.args[-1]
    k = len(items)
    while k > 0 and isinstance(items[k - 1], _C):
        k -= 1
    suffix = items[k:]
    g.args[-1] = items[:k]
    j = 0
    while j < len(suffix) and not suffix[j].a.before_nl:
        j += 1
    g.after.append(list(suffix[:j]))
    return list(suffix[j:])


def _build(atoms: list[_Atom]) -> list:
    top = _G("top")
    stack: list[_G] = [top]
    for a in atoms:
        g = stack[-1]
        is_top = len(stack) == 1
        if a.kind == "COMMENT":
            c = _C(a, _cat(a))
            if not is_top and g._open_zone and not a.before_nl:
                g.open_cmts.append(c)
            elif not is_top and g._after_zone and not a.before_nl:
                if g.args[-1]:                  # earlier own-line comments were
                    g.args[-1].append(c)        # carried over: keep source order
                else:
                    g.after[-1].append(c)
            else:
                if a.before_nl:
                    g._open_zone = False
                    g._after_zone = False
                g.args[-1].append(c)
            continue
        if a.kind == "PUNCT" and a.text in "({":
            kind = "brace" if a.text == "{" else "paren"
            name = None
            cur = g.args[-1]
            if (a.text == "(" and cur and isinstance(cur[-1], _T)
                    and _is_func_name(cur[-1].a)):
                name = cur.pop().a
                kind = "call"
            g._open_zone = False
            g._after_zone = False
            stack.append(_G(kind, name))
            continue
        if a.kind == "PUNCT" and a.text in ")}":
            if is_top:
                raise DaxFormatError(f"Unbalanced {a.text!r}: no matching opening bracket.")
            want = "}" if g.kind == "brace" else ")"
            if a.text != want:
                raise DaxFormatError(f"Mismatched brackets: expected {want!r} but found {a.text!r}.")
            stack.pop()
            stack[-1].args[-1].append(g)
            stack[-1]._open_zone = False
            stack[-1]._after_zone = False
            continue
        if a.kind == "PUNCT" and a.text in ",;" and not is_top:
            carry = _end_arg(g)
            g.seps.append(a)
            g.args.append(carry)
            g._open_zone = False
            g._after_zone = True
            continue
        g._open_zone = False
        g._after_zone = False
        g.args[-1].append(_T(a))
    if len(stack) > 1:
        raise DaxFormatError("Unbalanced brackets: a '(' or '{' is never closed.")
    return top.args[0]


# --- documents ---------------------------------------------------------------

class _Text:
    __slots__ = ("s",)

    def __init__(self, s: str):
        self.s = s


class _Line:
    __slots__ = ("flat",)

    def __init__(self, flat: str = " "):
        self.flat = flat


class _Hard:
    __slots__ = ()


class _Nest:
    __slots__ = ("n", "d")

    def __init__(self, n: int, d):
        self.n, self.d = n, d


class _Group:
    __slots__ = ("d", "force")

    def __init__(self, d, force: bool = False):
        self.d, self.force = d, force


class _Cat:
    __slots__ = ("ds",)

    def __init__(self, ds):
        self.ds = ds


_HARD = _Hard()


@dataclass
class _D:
    doc: object
    call: bool = False        # subtree contains a function call


@dataclass
class _Ctx:
    oneline: bool = False


def _wrap(d: _D, ctx: _Ctx, force: bool = False) -> _D:
    return _D(_Group(d.doc, force and not ctx.oneline), d.call)


def _is_tok(it, kind: str | None = None, text: str | None = None) -> bool:
    if not isinstance(it, _T):
        return False
    if kind is not None and it.a.kind != kind:
        return False
    return text is None or it.a.text == text


# Words that are keywords in queries but also perfectly good table names
# (`Order[Amount]`, `Table[x]`); directly before a [Column] they are qualifiers.
_TABLE_LIKE = frozenset({"TABLE", "COLUMN", "MEASURE", "ORDER", "START", "ASC", "DESC"})


def _is_qualifier(it) -> bool:
    if not isinstance(it, _T):
        return False
    if it.a.kind == "TABLE":
        return True
    if it.a.kind != "IDENT":
        return False
    up = it.a.text.upper()
    return up not in KEYWORDS or up in _TABLE_LIKE


def _unary_position(last) -> bool:
    if last is None:
        return True
    if not isinstance(last, _T):
        return False
    if last.a.kind == "OP":
        return True
    if last.a.kind == "PUNCT" and last.a.text in ",;":
        return True
    return last.a.kind == "IDENT" and last.a.text.upper() in (
        "RETURN", "IN", "NOT", "AND", "OR")


def _inline_cmts(cmts: list, hug_first: bool = False) -> list:
    """Comments printed right after a bracket / separator. A line comment ends
    the line, so it forces the enclosing group to break; a comment that starts
    a fresh line carries no leading space. ``hug_first``: the first block
    comment sits directly against the bracket:  F(/* c */ a, b)."""
    out: list = []
    at_line_start = False
    for i, c in enumerate(cmts):
        t = c.a.text
        block = t.startswith("/*")
        if at_line_start or (hug_first and i == 0 and block):
            out.append(_Text(t))
        else:
            out.append(_Text(" " + t))
        at_line_start = False
        if not block:
            out.append(_HARD)
            at_line_start = True
    return out


def _group_doc(g: _G, ctx: _Ctx) -> _D:
    opener, closer = ("{", "}") if g.kind == "brace" else ("(", ")")
    head = (g.name.text if g.name else "") + opener
    args = g.args
    has_cmts = bool(g.open_cmts) or any(g.after) or any(
        isinstance(x, _C) for a in args for x in a)
    if len(args) == 1 and not args[0] and not has_cmts:
        return _D(_Text(head + closer), g.kind == "call")

    arg_ds = [_stmts(a, ctx) for a in args]
    nested_call = any(d.call for d in arg_ds)
    parts: list = []
    for i, d in enumerate(arg_ds):
        if i:
            parts.append(_Line(" "))
        parts.append(d.doc)
        if i < len(g.seps):
            parts.append(_Text(g.seps[i].text))
            parts.extend(_inline_cmts(g.after[i]))
    open_docs = _inline_cmts(g.open_cmts, hug_first=True)
    first = _Line(" ") if (g.open_cmts and args[0]) else _Line("")
    doc = _Cat([_Text(head),
                _Nest(1, _Cat([*open_docs, first, *parts])),
                _Line(""), _Text(closer)])
    force = g.kind == "call" and nested_call
    return _D(_Group(doc, force and not ctx.oneline),
              nested_call or g.kind == "call")


def _item_doc(it, ctx: _Ctx) -> _D:
    if isinstance(it, _G):
        return _group_doc(it, ctx)
    return _D(_Text(it.a.text))


def _flat(items: list, ctx: _Ctx) -> _D:
    """Items on one logical line: spacing rules, unary operators, comments."""
    out: list = []
    call = False
    last = None                 # last non-comment item
    last_unary = False
    line_start = True
    owe_space = False
    for it in items:
        if isinstance(it, _C):
            text = it.a.text
            cat = it.cat
            if last is None:
                # Nothing precedes it on this logical line: a line comment can
                # only be printed as a comment line; a block comment stays
                # inline (it may follow `VAR ` or `&& ` on the same line).
                if cat == "trailing":
                    cat = "standalone"
                elif cat == "standalone" and text.startswith("/*"):
                    cat = "inline"
            if cat == "standalone":
                if not line_start:
                    out.append(_HARD)
                out.append(_Text(text))
                out.append(_HARD)
                line_start, owe_space = True, False
            elif cat == "trailing":
                out.append(_Text(" " + text))
                out.append(_HARD)
                line_start, owe_space = True, False
            else:
                if not line_start:
                    out.append(_Text(" "))
                out.append(_Text(text))
                line_start, owe_space = False, True
            continue

        unary = False
        if (isinstance(it, _T) and it.a.kind == "OP"
                and (it.a.text == "!" or (it.a.text in "+-" and _unary_position(last)))):
            unary = True
        if not line_start:
            if owe_space:
                space = True
            elif last is None:
                space = False
            elif last_unary:
                space = isinstance(it, _T) and it.a.kind == "OP"
            elif isinstance(it, _T) and it.a.kind == "PUNCT" and it.a.text in ",;":
                space = False
            elif (isinstance(it, _T) and it.a.kind == "COLUMN"
                  and _is_qualifier(last)):
                space = False
            else:
                space = True
            if space:
                out.append(_Text(" "))
        d = _item_doc(it, ctx)
        out.append(d.doc)
        call = call or d.call
        last, last_unary = it, unary
        line_start, owe_space = False, False
    return _D(_Cat(out), call)


def _expr(items: list, ctx: _Ctx, peel: bool = True) -> _D:
    """One expression: `||` / `&&` chains get line-break opportunities.
    Own-line comments before it stay on their own lines (``peel``)."""
    if peel:
        k = 0
        while (k < len(items) and isinstance(items[k], _C)
               and items[k].cat in ("standalone", "trailing")):
            k += 1
        if k:
            rest = _expr(items[k:], ctx, peel=False)
            return _D(_Cat([*_lead_docs(items[:k]), rest.doc]), rest.call)
    if not ctx.oneline:
        for op in ("||", "&&"):
            idx = [i for i, it in enumerate(items) if _is_tok(it, "OP", op)]
            if not idx:
                continue
            cuts = [-1, *idx, len(items)]
            parts = [items[a + 1:b] for a, b in zip(cuts, cuts[1:])]
            if any(not any(not isinstance(x, _C) for x in p) for p in parts):
                break                       # dangling operator: leave it flat
            ds = [_expr(p, ctx, peel=False) if op == "||" else _flat(p, ctx)
                  for p in parts]
            rest: list = []
            for d in ds[1:]:
                rest += [_Line(" "), _Text(op + " "), d.doc]
            doc = _Group(_Cat([ds[0].doc, _Nest(1, _Cat(rest))]))
            return _D(doc, any(d.call for d in ds))
    return _flat(items, ctx)


def _split_tail(items: list) -> tuple[list, list, list]:
    """Split trailing comments off ``items``: (body, same_line, next_leading)."""
    k = len(items)
    while k > 0 and isinstance(items[k - 1], _C):
        k -= 1
    suffix = items[k:]
    j = 0
    while j < len(suffix) and not suffix[j].a.before_nl:
        j += 1
    return items[:k], suffix[:j], suffix[j:]


def _lead_docs(cmts: list) -> list:
    out: list = []
    for c in cmts:
        out += [_Text(c.a.text), _HARD]
    return out


def _decl(head: str, value: _D, ctx: _Ctx) -> _D:
    """`VAR x = value` / `MEASURE t[m] = value`."""
    if ctx.oneline:
        return _D(_Cat([_Text(head + " = "), value.doc]), value.call)
    doc = _Group(_Cat([_Text(head + " ="),
                       _Nest(1, _Cat([_Line(" "), value.doc]))]))
    return _D(doc, value.call)


def _stmts(items: list, ctx: _Ctx) -> _D:
    """An expression that may be a VAR ... RETURN chain."""
    idx = [i for i, it in enumerate(items)
           if _is_tok(it, "IDENT") and it.a.text in _BLOCK]
    if not idx:
        return _expr(items, ctx)

    sep = _Text(" ") if ctx.oneline else _HARD
    docs: list = []
    call = False
    pre_body, pre_same, carry = _split_tail(items[:idx[0]])
    if pre_body or pre_same:
        d = _expr(pre_body + pre_same, ctx)
        docs += [d.doc, sep]
        call = d.call
    bounds = idx + [len(items)]
    for n, (a, b) in enumerate(zip(bounds, bounds[1:])):
        kw = items[a].a.text
        seg = items[a + 1:b]
        last_seg = n == len(idx) - 1
        if last_seg:
            same, nxt = [], []
        else:
            seg, same, nxt = _split_tail(seg)
        docs.extend(_lead_docs(carry))
        carry = nxt

        if kw == "VAR":
            if (len(seg) >= 2 and _is_tok(seg[0], "IDENT")
                    and _is_tok(seg[1], "OP", "=")):
                value = _expr(seg[2:], ctx)
                d = _decl("VAR " + seg[0].a.text, value, ctx)
            else:
                v = _flat(seg, ctx)
                d = _D(_Cat([_Text("VAR "), v.doc]), v.call)
        else:  # RETURN
            body = _expr(seg, ctx)
            if not seg:
                d = _D(_Text("RETURN"))
            elif ctx.oneline:
                d = _D(_Cat([_Text("RETURN "), body.doc]), body.call)
            else:
                d = _D(_Cat([_Text("RETURN"),
                             _Nest(1, _Cat([_HARD, body.doc]))]), body.call)
        docs.append(d.doc)
        call = call or d.call
        for c in same:
            docs.append(_Text(" " + c.a.text))
        if not last_seg:
            docs.append(sep)
    return _D(_Cat(docs), call)


# --- query mode (DEFINE / EVALUATE / ORDER BY / START AT) ---------------------

def _query(items: list, ctx: _Ctx) -> _D:
    starters: list[tuple[int, str, int]] = []       # (index, keyword, width)
    for i, it in enumerate(items):
        if not _is_tok(it, "IDENT"):
            continue
        t = it.a.text
        if t in _QUERY_STARTERS:
            starters.append((i, t, 1))
        elif t == "ORDER" and i + 1 < len(items) and _is_tok(items[i + 1], "IDENT", "BY"):
            starters.append((i, "ORDER BY", 2))
        elif t == "START" and i + 1 < len(items) and _is_tok(items[i + 1], "IDENT", "AT"):
            starters.append((i, "START AT", 2))
    docs: list = []
    call = False
    if starters and starters[0][0] > 0:
        d = _flat(items[:starters[0][0]], ctx)
        docs += [d.doc, _HARD]
        call = call or d.call
    for n, (i, kw, w) in enumerate(starters):
        end = starters[n + 1][0] if n + 1 < len(starters) else len(items)
        body = items[i + w:end]
        if n + 1 < len(starters):
            body, same, nxt = _split_tail(body)
        else:
            same, nxt = [], []
        if kw == "DEFINE":
            d = _define(body, ctx)
        elif kw == "EVALUATE":
            b = _stmts(body, ctx)
            d = _D(_Cat([_Text("EVALUATE"), _Nest(1, _Cat([_HARD, b.doc]))]), b.call)
        elif kw == "ORDER BY":
            cuts = [-1] + [j for j, x in enumerate(body) if _is_tok(x, "PUNCT", ",")] + [len(body)]
            parts = [_flat(body[a + 1:b], ctx) for a, b in zip(cuts, cuts[1:])]
            lines: list = []
            for j, p in enumerate(parts):
                lines.append(p.doc)
                if j < len(parts) - 1:
                    lines += [_Text(","), _HARD]
            d = _D(_Cat([_Text("ORDER BY"), _Nest(1, _Cat([_HARD, *lines]))]))
        else:  # START AT
            b = _flat(body, ctx)
            d = _D(_Cat([_Text("START AT "), b.doc]), b.call)
        docs.append(d.doc)
        call = call or d.call
        for c in same:
            docs.append(_Text(" " + c.a.text))
        for c in nxt:
            docs += [_HARD, _Text(c.a.text)]
        if n + 1 < len(starters):
            docs.append(_HARD)
    return _D(_Cat(docs), call)


def _define(body: list, ctx: _Ctx) -> _D:
    defs = [i for i, it in enumerate(body)
            if _is_tok(it, "IDENT") and it.a.text in ("MEASURE", "COLUMN", "TABLE", "VAR")]
    if not defs:
        d = _flat(body, ctx)
        return _D(_Cat([_Text("DEFINE"), _Nest(1, _Cat([_HARD, d.doc]))]), d.call)
    docs: list = []
    call = False
    bounds = defs + [len(body)]
    if defs[0] > 0:
        d = _flat(body[:defs[0]], ctx)
        docs += [d.doc, _HARD]
    for n, (a, b) in enumerate(zip(bounds, bounds[1:])):
        seg = body[a:b]
        same: list = []
        if n + 1 < len(defs):
            seg, same, nxt = _split_tail(seg)
        else:
            nxt = []
        eq = next((j for j, x in enumerate(seg) if _is_tok(x, "OP", "=")), None)
        if eq is None:
            d = _flat(seg, ctx)
        else:
            head = _flat(seg[:eq], ctx)
            head_text = _render(head.doc, 10**9, "")
            is_var = seg[0].a.text == "VAR"
            value = (_expr if is_var else _stmts)(seg[eq + 1:], ctx)
            d = _decl(head_text, value, ctx)
        docs.append(d.doc)
        call = call or d.call
        for c in same:
            docs.append(_Text(" " + c.a.text))
        for c in nxt:
            docs += [_HARD, _Text(c.a.text)]
        if n + 1 < len(defs):
            docs.append(_HARD)
    return _D(_Cat([_Text("DEFINE"), _Nest(1, _Cat([_HARD, *docs]))]), call)


# --- printer -------------------------------------------------------------------

def _fits(rem: int, first: list, rest: list) -> bool:
    stack = list(first)
    ri = len(rest) - 1
    while True:
        if rem < 0:
            return False
        if stack:
            ind, flat, d = stack.pop()
        elif ri >= 0:
            ind, flat, d = rest[ri]
            ri -= 1
        else:
            return True
        t = type(d)
        if t is _Text:
            if "\n" in d.s:
                if flat:
                    return False
                return rem - len(d.s.split("\n", 1)[0]) >= 0
            rem -= len(d.s)
        elif t is _Cat:
            stack.extend((ind, flat, x) for x in reversed(d.ds))
        elif t is _Nest:
            stack.append((ind + d.n, flat, d.d))
        elif t is _Line:
            if flat:
                rem -= len(d.flat)
            else:
                return True
        elif t is _Hard:
            return not flat
        else:  # _Group
            if flat:
                if d.force:
                    return False
                stack.append((ind, True, d.d))
            else:
                stack.append((ind, False, d.d))


def _render(doc, width: int, unit: str) -> str:
    lines: list[str] = []
    cur = ""
    stack: list = [(0, False, doc)]

    def newline(ind: int) -> None:
        nonlocal cur
        if cur.strip() == "":
            cur = unit * ind            # nothing on this line yet: no blank line
        else:
            lines.append(cur.rstrip())
            cur = unit * ind

    while stack:
        ind, flat, d = stack.pop()
        t = type(d)
        if t is _Text:
            cur += d.s
        elif t is _Cat:
            stack.extend((ind, flat, x) for x in reversed(d.ds))
        elif t is _Nest:
            stack.append((ind + d.n, flat, d.d))
        elif t is _Line:
            if flat:
                cur += d.flat
            else:
                newline(ind)
        elif t is _Hard:
            newline(ind)
        else:  # _Group
            if flat:
                stack.append((ind, True, d.d))
            elif d.force:
                stack.append((ind, False, d.d))
            else:
                col = len(cur[cur.rfind(chr(10)) + 1:].expandtabs(4))   # a tab counts 4
                fit = _fits(width - col, [(ind, True, d.d)], stack)
                stack.append((ind, fit, d.d))
    if cur.strip():
        lines.append(cur.rstrip())
    return "\n".join(lines)


# --- public API -------------------------------------------------------------------

def format_dax(dax: str, style: str = "long", indent: str = "    ",
               max_line: int = 100) -> str:
    """Format a DAX expression (or DEFINE/EVALUATE query).

    ``style="long"``: one argument per line when a call exceeds ``max_line`` or
    contains nested calls; ``VAR``/``RETURN`` on their own lines.
    ``style="short"``: a single line if the whole expression fits ``max_line``
    (and holds no comments), otherwise the long layout.

    Raises :class:`DaxFormatError` for unterminated strings/comments or
    unbalanced brackets. The result has no trailing newline.
    """
    if style not in ("long", "short"):
        raise ValueError(f"style must be 'long' or 'short', got {style!r}")
    if not isinstance(max_line, int) or max_line < 10:
        raise ValueError("max_line must be an integer >= 10")
    if not isinstance(indent, str) or indent.strip():
        raise ValueError("indent must be a string of spaces/tabs")
    text = dax.replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        return ""
    try:
        atoms = _lex(text)
        _normalize(atoms)
        items = _build(atoms)
        query = any(_is_tok(it, "IDENT") and it.a.text in _QUERY_STARTERS
                    for it in items)
        if (style == "short" and not query
                and not any(a.kind == "COMMENT" for a in atoms)):
            one = _stmts(items, _Ctx(oneline=True))
            line = _render(one.doc, 10**9, "")
            if len(line) <= max_line:
                return line
        ctx = _Ctx()
        d = _query(items, ctx) if query else _stmts(items, ctx)
        return _render(d.doc, max_line, indent)
    except RecursionError:
        raise DaxFormatError("Expression is nested too deeply to format.") from None
