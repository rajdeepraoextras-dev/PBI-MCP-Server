"""DAX formatter (core/dax_format.py) and its tools (model_server/tools_dax.py).

The two properties the formatter promises are asserted over a large corpus, the
fixture's own measures, whitespace-perturbed variants and randomly generated
expressions with comments injected everywhere:

  * lossless   - tokens (ignoring whitespace/comments, IDENT case aside) and
                 the comment texts are unchanged;
  * idempotent - format(format(x)) == format(x).
"""

from __future__ import annotations

import asyncio
import json
import random
import shutil
from pathlib import Path

import pytest

from core.dax_format import DaxFormatError, format_dax
from core.dax_parser import tokenize
from core.journal import snapshot
from core.pbip import PbipProject
from model_server import tools_dax
from model_server.server import ModelState, set_project

SYNTH = Path(__file__).parent / "fixtures" / "synthetic"


# --- helpers -----------------------------------------------------------------

def sig(s: str) -> list[tuple[str, str]]:
    return [(t.kind, t.text.lower() if t.kind == "IDENT" else t.text)
            for t in tokenize(s) if t.kind not in ("WS", "COMMENT")]


def comments(s: str) -> list[str]:
    return [t.text if t.text.startswith("/*") else t.text.rstrip()
            for t in tokenize(s) if t.kind == "COMMENT"]


def check_props(src: str, style: str = "long", **kw) -> str:
    out = format_dax(src, style, **kw)
    assert sig(out) == sig(src), f"tokens changed:\n{src!r}\n{out!r}"
    assert comments(out) == comments(src), f"comments changed:\n{src!r}\n{out!r}"
    again = format_dax(out, style, **kw)
    assert again == out, f"not idempotent ({style}):\n{out}\n--- second pass\n{again}"
    assert "\n\n" not in out and not out.endswith("\n") and out == out.strip()
    return out


def fixture_measures() -> list[str]:
    return [m.dax for m in PbipProject(SYNTH / "Synthetic.pbip").list_measures()]


CORPUS = [
    "SUM(Sales[Amount])",
    "sum(sales[amount])",
    "DIVIDE([Net Revenue] - SUM(Sales[Cost]), [Net Revenue])",
    "VAR _rev = [Net Revenue]\nRETURN\n\tDIVIDE(_rev, [Order Count])",
    "CALCULATE(\n\t[Net Revenue],\n\tALL(Date)\n)",
    "IF(ISBLANK([x]), BLANK(), [x])",
    "var x = 1 return x + -2",
    "[a] IN {1,2,3} && NOT [b] || TRUE()",
    "CALCULATE([Sales], FILTER(ALL(Sales[Region]), Sales[Region] = \"a,b)\" && Sales[X] > 1), "
    "KEEPFILTERS(Date[Year] IN {2020, 2021}))",
    "SUMX(FILTER(Sales, Sales[Qty] > 0 && Sales[Price] * (1 - Sales[Discount]) > 100), "
    "Sales[Qty] * Sales[Price])",
    "- [a] - - [b] + +1 * -(2 + 3)",
    "VAR a = 1 VAR b = CALCULATE(SUM(t[x]), ALL(t)) VAR c = a + b RETURN IF(c > 0, c, -c)",
    "SWITCH(TRUE(), [a] > 1, \"hi\", [a] > 0, \"mid\", \"lo\")",
    "RANKX(ALL(Product[Name]), [Sales], , DESC, Dense)",
    "'My Table'[Some Col] & \" - \" & FORMAT(Date[Date], \"yyyy-MM-dd\")",
    "IF([x] = 1, 1,\n IF([x] = 2, 2,\n IF([x] = 3, 3, 4)))",
    "CALCULATE([m], USERELATIONSHIP(Sales[OrderDate], 'Date'[Date]), Date[Year] = 2020)",
    "1.5e3 + .5 - 2E-2",
    "TOPN(5, VALUES(Product[Name]), [Sales], DESC)",
    "COUNTROWS(FILTER(Sales, Sales[x] > 1 || Sales[y] < 2 || Sales[z] = 3 || Sales[w] <> 4 "
    "|| Sales[v] >= 5 || Sales[u] <= 6 || Sales[t] == 7))",
    "{ (1, \"a\"), (2, \"b\") }",
    "NORM.DIST(1, 2, 3, TRUE) + VAR.P(t[x])",
    "MAX(Date[Date]) - 1",
    "\"multi\nline string\"",
    "IF(a, b) // c1\n + 1 /* c2 */ + 2",
    "CALCULATE(\n  [A], // first\n  FILTER(ALL(Sales), Sales[x] > 1) /* block */,\n"
    "  // own line\n  Date[Year] = 2020\n)",
    "/* head */\nVAR x = 1 // one\n// two\nVAR y = 2\nRETURN /* r */ x + y // end",
    "f(a /* x */ , b // y\n , c)",
    "f( // open\n a, b)",
    "f(/* c */ a, b)",
    "DEFINE\n VAR x = 1\n MEASURE Sales[T] = SUM(Sales[A]) + x\n MEASURE Sales[U] = [T] * 2\n"
    "EVALUATE\n ADDCOLUMNS(VALUES(Date[Year]), \"T\", [T], \"U\", [U])\n"
    "ORDER BY Date[Year] DESC, [T]\nSTART AT 2020",
    "EVALUATE Sales",
    "SELECTEDVALUE(t[c], \"x;y\")",
    "a;b",
    "-- only a dash comment",
    "// only a comment",
    "COALESCE([a], [b], 0) -- trailing dashes",
    "VAR _x = -- why\n    1\nRETURN _x",
    "CALCULATE(SUM(Sales[Amount]), Sales[Region] IN {\"East\", \"West\"}, "
    "Date[Year] >= 2019, Date[Year] <= 2021, NOT ISBLANK(Sales[Customer]))",
    "IF(\n\tAND(ISFILTERED(Date[Year]), HASONEVALUE(Date[Year])),\n\tSELECTEDVALUE(Date[Year]),\n\tBLANK()\n)",
    "[Sales]/[Cost]-1",
    "Sales[Amount]*(1+Sales[Tax])",
    "'Product Category'[Name]<>\"x\"",
    "EOMONTH(TODAY(),-1)+1",
    "CONCATENATEX(VALUES(t[c]), t[c], \", \", t[c], ASC)",
    "SUMMARIZECOLUMNS(Date[Year], FILTER(ALL(Date[Year]), Date[Year] > 2019), \"Total\", [Total])",
]


# --- lossless + idempotent over the corpus ------------------------------------

@pytest.mark.parametrize("style", ["long", "short"])
@pytest.mark.parametrize("max_line", [100, 40, 12])
@pytest.mark.parametrize("src", CORPUS + fixture_measures())
def test_corpus_lossless_and_idempotent(src, style, max_line):
    check_props(src, style, max_line=max_line)


@pytest.mark.parametrize("indent", ["    ", "  ", "\t"])
def test_indent_units_are_idempotent(indent):
    for src in CORPUS:
        check_props(src, "long", indent=indent)


def perturb(src: str, rng: random.Random) -> str:
    """Re-space every token pair arbitrarily (comments keep a newline)."""
    out = []
    for t in tokenize(src):
        if t.kind == "WS":
            continue
        out.append(t.text)
        if t.kind == "COMMENT" and not t.text.startswith("/*"):
            out.append("\n")
        else:
            out.append(rng.choice([" ", " ", "\n", "  ", "\t", "\n\n  ", " \n "]))
    return "".join(out)


@pytest.mark.parametrize("src", [s for s in CORPUS + fixture_measures()
                                 if not comments(s)])
def test_output_ignores_input_whitespace(src):
    """Without comments the layout depends on the tokens only."""
    rng = random.Random(7)
    for style in ("long", "short"):
        want = format_dax(src, style)
        for _ in range(4):
            assert format_dax(perturb(src, rng), style) == want


@pytest.mark.parametrize("src", [s for s in CORPUS if comments(s)])
def test_perturbed_input_with_comments_stays_lossless(src):
    rng = random.Random(11)
    for _ in range(6):
        check_props(perturb(src, rng), "long")
        check_props(perturb(src, rng), "short")


# --- random expressions with comments injected anywhere ------------------------

_FUNCS = ["SUM", "CALCULATE", "IF", "FILTER", "DIVIDE", "SUMX", "ALL", "VALUES",
          "COUNTROWS", "SWITCH", "Max", "sum", "FORMAT", "RANKX", "TOPN", "BLANK",
          "SELECTEDMEASURE", "NORM.DIST"]
_OPS = ["+", "-", "*", "/", "&", "&&", "||", "=", "<>", "<", "<=", ">", ">=", "^", "=="]


def _atom(r: random.Random) -> str:
    c = r.random()
    if c < .15:
        return str(r.choice([0, 1, 2.5, 100, ".5", "1e3"]))
    if c < .25:
        return r.choice(['"a"', '"x,y"', '"q)("', '""', '"say ""hi"""'])
    if c < .45:
        return r.choice(["[Net Revenue]", "[m]", "[a b]", "Sales[Amount]",
                         "'My Table'[Col X]", "Date[Year]", "_v", "x1"])
    if c < .5:
        return r.choice(["TRUE", "false", "BLANK()", "Sales", "'Dim Product'", "DESC", "asc"])
    return "[m]"


def _expr(r: random.Random, d: int) -> str:
    if d <= 0 or r.random() < .25:
        return _atom(r)
    c = r.random()
    if c < .45:
        n = r.choice([0, 1, 1, 2, 2, 3, 4])
        return r.choice(_FUNCS) + "(" + ", ".join(_expr(r, d - 1) for _ in range(n)) + ")"
    if c < .6:
        return "(" + _expr(r, d - 1) + ")"
    if c < .8:
        return _expr(r, d - 1) + " " + r.choice(_OPS) + " " + _expr(r, d - 1)
    if c < .85:
        return r.choice(["- ", "+ ", "NOT "]) + _expr(r, d - 1)
    if c < .9:
        return "{" + ", ".join(_expr(r, d - 1) for _ in range(r.choice([1, 2, 3]))) + "}"
    if c < .95:
        return _expr(r, d - 1) + " IN {" + ", ".join(_atom(r) for _ in range(3)) + "}"
    return "(VAR _v = " + _expr(r, d - 1) + " RETURN " + _expr(r, d - 1) + ")"


def _decorate(s: str, r: random.Random) -> str:
    out = []
    for t in tokenize(s):
        if t.kind == "WS":
            continue
        out.append(t.text)
        if t.kind == "COMMENT" and not t.text.startswith("/*"):
            out.append("\n")
        elif r.random() < .06:
            out.append(r.choice([" // note\n", " -- dash\n", "\n// own line\n",
                                 " /* b */ ", "\n/* blk */\n", "\n/* multi\n line */ ",
                                 "/*x*/"]))
        else:
            out.append(r.choice([" ", " ", "\n", "  ", "\t", "\n\n", " \n "]))
    return "".join(out)


def test_random_expressions_with_comments():
    rng = random.Random(20240601)
    checked = 0
    for _ in range(400):
        if rng.random() < .3:
            base = ("VAR a = " + _expr(rng, 3) + "\nVAR b = " + _expr(rng, 3)
                    + "\nRETURN " + _expr(rng, 3))
        else:
            base = _expr(rng, rng.choice([2, 3, 4]))
        src = _decorate(base, rng)
        if sig(src) != sig(base):        # the decoration glued tokens into a comment
            continue
        for style in ("long", "short"):
            for max_line in (100, 30):
                check_props(src, style, max_line=max_line)
        checked += 1
    assert checked > 300


# --- exact output ----------------------------------------------------------------

def test_case_normalisation_touches_only_functions_and_keywords():
    assert format_dax("sum(sales[amount])") == "SUM(sales[amount])"
    assert format_dax("var x = 1 return x") == "VAR x = 1\nRETURN\n    x"
    assert format_dax("[a] in {1, 2} and not true()") == "[a] IN {1, 2} AND NOT TRUE()"
    # names, strings and comments keep their case
    out = format_dax('calculate(sum(Sales[Amount]), Region[name] = "ea,st") // Keep Case')
    assert 'Region[name] = "ea,st"' in out and out.endswith("// Keep Case")
    # a variable that looks like a keyword stays as written
    assert format_dax("VAR Start = 1 RETURN Start + 1").endswith("Start + 1")
    # a table called like a keyword is not a keyword
    assert format_dax("COUNTROWS(Order)") == "COUNTROWS(Order)"
    assert format_dax("Table[x] + Order[y]") == "Table[x] + Order[y]"


def test_qualifier_glues_to_column_and_operators_are_spaced():
    assert format_dax("Sales [Amount]*2+'My Table' [c]/3") == "Sales[Amount] * 2 + 'My Table'[c] / 3"
    assert format_dax("[a]<>[b]&&[c]>=[d]") == "[a] <> [b] && [c] >= [d]"


def test_unary_operators_hug_their_operand():
    assert format_dax("-[a]") == "-[a]"
    assert format_dax("1 - -1") == "1 - -1"
    assert format_dax("(- 2)") == "(-2)"
    assert format_dax("[a] * -[b]") == "[a] * -[b]"
    assert format_dax("RETURN -1".replace("RETURN", "VAR x = 1 RETURN")) == "VAR x = 1\nRETURN\n    -1"
    out = format_dax("- -1")
    assert out == "- -1" and sig(out) == sig("- -1")     # never fuses into a `--` comment


def test_call_with_nested_call_breaks_one_argument_per_line():
    assert format_dax("DIVIDE([Net Revenue]-SUM(Sales[Cost]),[Net Revenue])") == (
        "DIVIDE(\n"
        "    [Net Revenue] - SUM(Sales[Cost]),\n"
        "    [Net Revenue]\n"
        ")")
    assert format_dax("CALCULATE([Net Revenue], ALL(Date))") == (
        "CALCULATE(\n    [Net Revenue],\n    ALL(Date)\n)")
    # no nested call and it fits: stays inline
    assert format_dax("DIVIDE([a], [b], 0)") == "DIVIDE([a], [b], 0)"
    assert format_dax("SUM(Sales[Amount])") == "SUM(Sales[Amount])"
    assert format_dax("BLANK()") == "BLANK()"


def test_nested_calculate_filter_layout():
    out = format_dax("CALCULATE(SUM(Sales[Amount]), FILTER(ALL(Sales[Region]), "
                     'Sales[Region] = "East"), Date[Year] = 2020)')
    assert out == (
        "CALCULATE(\n"
        "    SUM(Sales[Amount]),\n"
        "    FILTER(\n"
        "        ALL(Sales[Region]),\n"
        '        Sales[Region] = "East"\n'
        "    ),\n"
        "    Date[Year] = 2020\n"
        ")")


def test_long_call_without_nested_calls_breaks_at_max_line():
    src = 'CONCATENATE("aaaaaaaaaaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbbbbbbbbbbbb", "cccccccccccccccccccccccc")'
    assert len(src) > 80
    assert format_dax(src, max_line=200) == src
    out = format_dax(src, max_line=60)
    assert out == ('CONCATENATE(\n    "aaaaaaaaaaaaaaaaaaaaaaaa",\n    "bbbbbbbbbbbbbbbbbbbbbbbbbb",\n'
                   '    "cccccccccccccccccccccccc"\n)')
    assert all(len(line) <= 60 for line in out.split("\n"))


def test_var_return_layout():
    assert format_dax("VAR _rev = [Net Revenue] RETURN DIVIDE(_rev, [Order Count])") == (
        "VAR _rev = [Net Revenue]\n"
        "RETURN\n"
        "    DIVIDE(_rev, [Order Count])")
    out = format_dax("VAR a = CALCULATE(SUM(t[x]), ALL(t)) VAR b = a * 2 RETURN IF(b > 0, b, 0)")
    assert out == (
        "VAR a =\n"
        "    CALCULATE(\n"
        "        SUM(t[x]),\n"
        "        ALL(t)\n"
        "    )\n"
        "VAR b = a * 2\n"
        "RETURN\n"
        "    IF(b > 0, b, 0)")


def test_var_return_inside_an_argument():
    out = format_dax("CALCULATE(VAR y = 1 RETURN y + [m], ALL(t))")
    assert out == (
        "CALCULATE(\n"
        "    VAR y = 1\n"
        "    RETURN\n"
        "        y + [m],\n"
        "    ALL(t)\n"
        ")")


def test_in_lists_and_braces():
    assert format_dax("[a] in {1,2,3}") == "[a] IN {1, 2, 3}"
    assert format_dax("Sales[Region] IN {\"East\",\"West\"}") == 'Sales[Region] IN {"East", "West"}'
    assert format_dax("{ (1, \"a\"), (2, \"b\") }") == '{(1, "a"), (2, "b")}'
    long_list = "[a] IN {" + ", ".join(f'"value number {i}"' for i in range(8)) + "}"
    out = format_dax(long_list, max_line=50)
    assert out.startswith("[a] IN {\n    \"value number 0\",")
    assert out.endswith("\n}")


def test_strings_with_commas_and_parens_are_untouched():
    src = 'IF(x = "a,b)(c", "it\'s ""quoted""", 0)'
    out = check_props(src)
    assert '"a,b)(c"' in out and '"it\'s ""quoted"""' in out


def test_empty_arguments_are_kept():
    assert format_dax("RANKX(ALL(P[N]),[Sales],,DESC,Dense)") == (
        "RANKX(\n    ALL(P[N]),\n    [Sales],\n    ,\n    DESC,\n    Dense\n)")
    assert format_dax("TOPN(5,t,[m],,DESC)".replace(",,", ", ,")) == "TOPN(5, t, [m], , DESC)"


def test_and_or_chains_break_only_when_too_long():
    short = "[a] && [b] || [c]"
    assert format_dax(short) == short
    long_cond = " && ".join(f"Sales[Column{i}] = {i}" for i in range(9))
    out = format_dax(long_cond, max_line=60)
    lines = out.split("\n")
    assert lines[0].startswith("Sales[Column0] = 0")
    assert all(line.strip().startswith("&& ") for line in lines[1:])
    assert all(len(line) <= 60 for line in lines)


def test_long_sums_and_concatenations_break_before_the_operator():
    total = " + ".join(f"[Measure Number {i}]" for i in range(8))
    assert format_dax(total, max_line=200) == total
    out = format_dax(total, max_line=60)
    lines = out.split("\n")
    assert lines[0] == "[Measure Number 0]"
    assert all(line == f"    + [Measure Number {i}]" for i, line in enumerate(lines[1:], 1))
    mixed = format_dax("[A Long Measure Name] - [Another Long Measure] + [Third Long Measure Name]"
                       " - [Fourth]", max_line=50)
    assert mixed.split("\n")[1:] == ["    - [Another Long Measure]", "    + [Third Long Measure Name]",
                                     "    - [Fourth]"]
    text = format_dax('"a fairly long literal " & [Some Measure] & " and another literal" & [Other]',
                      max_line=40)
    assert text.split("\n")[1].startswith("    & ")
    # a leading sign is not a binary operator, and lower-precedence chains win
    unary = format_dax("-[Measure Number One] + -[Measure Number Two] - -3", max_line=30)
    assert unary.split("\n") == ["-[Measure Number One]", "    + -[Measure Number Two]", "    - -3"]
    boolean = format_dax("[a] + [b] > 0 && [c] + [d] > 0", max_line=20)
    assert boolean == "[a] + [b] > 0\n    && [c] + [d] > 0"
    for src in (total, mixed, text, unary):
        check_props(src, max_line=40)


def test_blank_lines_are_collapsed_and_edges_trimmed():
    out = format_dax("\n\n  VAR a = 1\n\n\n\n  VAR b = 2\n\n RETURN\n\n a + b \n\n")
    assert out == "VAR a = 1\nVAR b = 2\nRETURN\n    a + b"
    assert format_dax("") == "" and format_dax(" \n\t \n") == ""


def test_crlf_input_is_normalised():
    assert format_dax("VAR a = 1\r\nRETURN\r\n  a") == "VAR a = 1\nRETURN\n    a"


def test_comments_stay_in_position():
    out = format_dax("-- head\nCALCULATE(\n  [A], // first\n  // own line\n  Date[Year] = 2020 /* end */\n)")
    assert out == (
        "-- head\n"
        "CALCULATE(\n"
        "    [A], // first\n"
        "    // own line\n"
        "    Date[Year] = 2020 /* end */\n"
        ")")
    out = format_dax("VAR x = 1 // one\n// two\nRETURN x // end")
    assert out == "VAR x = 1 // one\n// two\nRETURN\n    x // end"
    assert format_dax("SUM(x) /* keep */ + 1") == "SUM(x) /* keep */ + 1"
    block = "/* line one\n   line two */\nSUM(x)"
    assert format_dax(block) == block


def test_line_comment_never_swallows_code():
    # lossless tokens prove nothing was commented out; also pin the layouts
    for src in ["f(a, // c\n b)", "a + // c\n b", "VAR x = // c\n 1 RETURN x", "( // c\n a )",
                "f(a // c\n)", "SUM(x) // c\n+ 1"]:
        check_props(src)
    assert format_dax("f(a, // c\n b)") == "F(\n    a, // c\n    b\n)"
    assert format_dax("f(a // c\n)") == "F(\n    a // c\n)"
    assert format_dax("a + // c\n b") == "a\n    + // c\n    b"


def test_short_style():
    assert format_dax("DIVIDE([Net Revenue]-SUM(Sales[Cost]),[Net Revenue])", "short") == (
        "DIVIDE([Net Revenue] - SUM(Sales[Cost]), [Net Revenue])")
    assert format_dax("VAR _r = [Net Revenue]\nRETURN\n DIVIDE(_r, [Order Count])", "short") == (
        "VAR _r = [Net Revenue] RETURN DIVIDE(_r, [Order Count])")
    long_src = "CALCULATE(" + ", ".join(f"Sales[Col{i}] = {i}" for i in range(10)) + ")"
    assert format_dax(long_src, "short", max_line=40) == format_dax(long_src, "long", max_line=40)
    assert "\n" in format_dax(long_src, "short", max_line=40)
    # comments cannot be flattened: falls back to the long layout
    assert format_dax("SUM(x) // c", "short") == "SUM(x) // c"
    assert format_dax("f(a, // c\n b)", "short") == format_dax("f(a, // c\n b)", "long")


def test_query_syntax():
    out = format_dax("define measure Sales[T] = sum(Sales[A]) evaluate summarizecolumns(Date[Year], \"T\", [T]) "
                     "order by Date[Year] desc")
    assert out == (
        "DEFINE\n"
        "    MEASURE Sales[T] = SUM(Sales[A])\n"
        "EVALUATE\n"
        "    SUMMARIZECOLUMNS(Date[Year], \"T\", [T])\n"
        "ORDER BY\n"
        "    Date[Year] DESC")
    check_props("EVALUATE VALUES(Date[Year]) ORDER BY Date[Year] START AT 2019")


@pytest.mark.parametrize("bad", ['SUM("open', "SUM(x", "SUM(x))", "(x]", "{1, 2", "a /* open",
                                 "SUM(x) }"])
def test_unformattable_input_raises(bad):
    with pytest.raises(DaxFormatError):
        format_dax(bad)
    with pytest.raises(ValueError):
        format_dax(bad)


def test_bad_arguments():
    with pytest.raises(ValueError):
        format_dax("1", style="tall")
    with pytest.raises(ValueError):
        format_dax("1", max_line=2)
    with pytest.raises(ValueError):
        format_dax("1", indent="x")


def test_deep_nesting_is_reported_not_crashing():
    deep = "(" * 400 + "1" + ")" * 400
    try:
        out = format_dax(deep)
    except DaxFormatError:
        return
    assert sig(out) == sig(deep)


def test_fixture_measures_round_trip():
    for dax in fixture_measures():
        out = check_props(dax)
        assert out == format_dax(dax)


# --- tools ------------------------------------------------------------------------

@pytest.fixture
def proj(tmp_path):
    dst = tmp_path / "p"
    shutil.copytree(SYNTH, dst)
    return dst


@pytest.fixture
def state(proj):
    st = ModelState()
    set_project(st, str(proj / "Synthetic.pbip"))
    return st


def sales_file(proj: Path) -> Path:
    return proj / "Synthetic.SemanticModel" / "definition" / "tables" / "Sales.tmdl"


def test_format_dax_text_needs_no_project():
    out = tools_dax.format_dax_text(ModelState(), dax="sum(x)+1")
    assert out["formatted"] == "SUM(x) + 1" and out["changed"] is True
    assert tools_dax.format_dax_text(ModelState(), dax="SUM(x) + 1")["changed"] is False
    short = tools_dax.format_dax_text(ModelState(), dax="VAR a = 1\nRETURN a", style="short")
    assert short["formatted"] == "VAR a = 1 RETURN a"


def test_format_dax_text_errors_are_actionable():
    st = ModelState()
    with pytest.raises(ValueError, match="either"):
        tools_dax.format_dax_text(st, dax="1", measure="M")
    with pytest.raises(ValueError, match="Pass `dax`"):
        tools_dax.format_dax_text(st)
    with pytest.raises(ValueError, match="style"):
        tools_dax.format_dax_text(st, dax="1", style="wide")
    with pytest.raises(ValueError, match="Cannot format DAX"):
        tools_dax.format_dax_text(st, dax='SUM("open')
    with pytest.raises(ValueError, match="No project"):
        tools_dax.format_dax_text(st, measure="Net Revenue")


def test_format_dax_text_previews_model_measures(state, proj):
    before = snapshot(proj)
    one = tools_dax.format_dax_text(state, measure="Margin %")
    assert one["table"] == "Sales" and one["changed"] is True
    assert one["formatted"].startswith("DIVIDE(\n\t[Net Revenue]")
    assert tools_dax.format_dax_text(state, measure="Complex Measure")["changed"] is False
    many = tools_dax.format_dax_text(state, table="Sales")
    assert {m["name"] for m in many["measures"]} >= {"Net Revenue", "Margin %"}
    assert many["changed_count"] == 1
    with pytest.raises(ValueError, match="Did you mean: Margin %"):
        tools_dax.format_dax_text(state, measure="Margin")
    with pytest.raises(ValueError, match="Table 'Nope' not found"):
        tools_dax.format_dax_text(state, table="Nope")
    assert snapshot(proj) == before                       # read-only


def test_format_measures_is_surgical_and_idempotent(state, proj):
    path = sales_file(proj)
    before = path.read_text(encoding="utf-8").split("\n")
    result = tools_dax.format_measures(state)
    assert result["changed"] == [{"table": "Sales", "name": "Margin %"}]
    assert result["unchanged"] == 5 and result["skipped"] == []
    after = path.read_text(encoding="utf-8").split("\n")
    assert after != before
    # everything outside the rewritten measure's expression is byte-identical
    i = before.index("\tmeasure 'Margin %' = DIVIDE([Net Revenue] - SUM(Sales[Cost]), [Net Revenue])")
    assert after[:i] == before[:i]
    tail = len(before) - (i + 1)
    assert after[-tail:] == before[i + 1:]
    assert after[i:len(after) - tail] == [
        "\tmeasure 'Margin %' = ```",
        "\t\t\tDIVIDE(",
        "\t\t\t\t[Net Revenue] - SUM(Sales[Cost]),",
        "\t\t\t\t[Net Revenue]",
        "\t\t\t)",
        "\t\t\t```",
    ]
    assert "\t\tformatString: 0.0%" in after and "\t\tdisplayFolder: KPIs" in after
    # the project still loads and the measure kept its identity
    proj_after = PbipProject(proj / "Synthetic.pbip")
    m = next(x for x in proj_after.list_measures() if x.name == "Margin %")
    assert m.format_string == "0.0%" and "SUM(Sales[Cost])" in m.dax
    # idempotent
    snap = snapshot(proj)
    again = tools_dax.format_measures(state)
    assert again["count"] == 0 and again["changed"] == []
    assert snapshot(proj) == snap


def test_format_measures_preserves_crlf_and_bom(state, proj):
    path = sales_file(proj)
    raw = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    path.write_bytes(b"\xef\xbb\xbf" + raw)
    tools_dax.format_measures(state, measure="Margin %")
    out = path.read_bytes()
    assert out.startswith(b"\xef\xbb\xbf")
    body = out[3:]
    assert b"\r\n" in body and b"\n" not in body.replace(b"\r\n", b"")


def test_format_measures_scoping_and_short_style(state, proj):
    only = tools_dax.format_measures(state, table="Date")
    assert only["count"] == 0
    res = tools_dax.format_measures(state, measure="Complex Measure", style="short")
    assert res["changed"] == [{"table": "Sales", "name": "Complex Measure"}]
    text = sales_file(proj).read_text(encoding="utf-8")
    assert "\tmeasure 'Complex Measure' = VAR _rev = [Net Revenue] RETURN DIVIDE(_rev, [Order Count])\n" in text
    assert "\t\tformatString: #,0" in text
    # re-running short is a no-op, running long converts it back
    assert tools_dax.format_measures(state, measure="Complex Measure", style="short")["count"] == 0
    back = tools_dax.format_measures(state, measure="Complex Measure", style="long")
    assert back["count"] == 1
    assert tools_dax.format_measures(state, measure="Complex Measure")["count"] == 0
    with pytest.raises(ValueError, match="Measure 'Nope' not found"):
        tools_dax.format_measures(state, measure="Nope")


def test_format_measures_skips_what_it_cannot_format(state, proj):
    path = sales_file(proj)
    text = path.read_text(encoding="utf-8")
    text = text.replace("measure 'Hidden Helper' = 1", "measure 'Hidden Helper' = SUM(Sales[Amount]")
    path.write_text(text, encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    res = tools_dax.format_measures(state, measure="Hidden Helper")
    assert res["count"] == 0 and res["skipped"][0]["name"] == "Hidden Helper"
    assert "Unbalanced" in res["skipped"][0]["reason"]
    assert path.read_text(encoding="utf-8") == before


def test_format_measures_never_lets_a_comment_share_the_header_line(state, proj):
    path = sales_file(proj)
    text = path.read_text(encoding="utf-8").replace(
        "\tmeasure 'Hidden Helper' = 1\n",
        "\tmeasure Noted = sum(Sales[Amount]) // total\n\t\tformatString: 0\n\n"
        "\tmeasure 'Hidden Helper' = 1\n")
    path.write_text(text, encoding="utf-8")
    res = tools_dax.format_measures(state, measure="Noted")
    assert res["changed"] == [{"table": "Sales", "name": "Noted"}]
    new = path.read_text(encoding="utf-8")
    assert "\tmeasure Noted = ```\n\t\t\tSUM(Sales[Amount]) // total\n\t\t\t```\n\t\tformatString: 0\n" in new
    m = next(x for x in PbipProject(proj / "Synthetic.pbip").list_measures() if x.name == "Noted")
    assert m.dax == "SUM(Sales[Amount]) // total" and m.format_string == "0"
    assert tools_dax.format_measures(state, measure="Noted")["count"] == 0


def test_format_measures_keeps_other_properties(state, proj):
    path = sales_file(proj)
    text = path.read_text(encoding="utf-8").replace(
        "\tmeasure 'Net Revenue' = SUM(Sales[Amount])\n",
        "\t/// Revenue after discounts\n"
        "\tmeasure 'Net Revenue' = sum(sales[amount])\n"
        "\t\tlineageTag: abc\n")
    path.write_text(text, encoding="utf-8")
    tools_dax.format_measures(state, measure="Net Revenue")
    new = path.read_text(encoding="utf-8")
    assert "\t/// Revenue after discounts\n\tmeasure 'Net Revenue' = SUM(sales[amount])\n\t\tlineageTag: abc\n" in new


def test_dax_references_without_and_with_model(state):
    plain = tools_dax.dax_references(ModelState(), "CALCULATE([Net Revenue], Sales[OrderDate] > 1) + [Order Count]")
    assert plain["resolved_against_model"] is False
    assert plain["measures"] == ["Net Revenue", "Order Count"]
    assert plain["columns"] == [{"table": "Sales", "column": "OrderDate"}]
    resolved = tools_dax.dax_references(
        state, "CALCULATE([net revenue], Sales[OrderDate] > 1) + [Order Count] + Sales[Net Revenue]")
    assert resolved["resolved_against_model"] is True
    assert resolved["measures"] == ["Net Revenue"]
    assert resolved["unqualified"] == ["Order Count"]
    assert resolved["unqualified_matches"] == {"Order Count": ["Sales"]}
    kinds = {(c["table"], c["column"]): c["kind"] for c in resolved["columns"]}
    assert kinds[("Sales", "OrderDate")] == "column"
    assert kinds[("Sales", "Net Revenue")] == "measure"
    off = tools_dax.dax_references(state, "[Order Count]", resolve_against_model=False)
    assert off["resolved_against_model"] is False and off["measures"] == ["Order Count"]
    # strings and comments are never references
    quiet = tools_dax.dax_references(state, '"[Net Revenue]" // [Order Count]\n1')
    assert quiet["measures"] == [] and quiet["columns"] == []
    with pytest.raises(ValueError):
        tools_dax.dax_references(state, "  ")


def test_tools_are_registered_with_the_right_annotations():
    import model_server.server as srv

    tools = {t.name: t for t in asyncio.run(srv.mcp.list_tools())}

    def hint(tool, name):
        a = tool.annotations
        snake = {"readOnlyHint": "read_only_hint", "destructiveHint": "destructive_hint",
                 "idempotentHint": "idempotent_hint"}[name]
        return getattr(a, name) if hasattr(a, name) else getattr(a, snake)

    def props(tool):
        schema = getattr(tool, "inputSchema", None) or tool.input_schema
        return schema["properties"]

    for name in ("pbi_format_dax", "pbi_dax_references"):
        assert hint(tools[name], "readOnlyHint") is True
        assert "dry_run" not in props(tools[name])
    fm = tools["pbi_format_measures"]
    assert hint(fm, "readOnlyHint") is False and hint(fm, "idempotentHint") is True
    assert "dry_run" in props(fm) and set(props(fm)) >= {"table", "measure", "style"}
    assert set(props(tools["pbi_format_dax"])) == {"dax", "table", "measure", "style"}
    assert set(props(tools["pbi_dax_references"])) == {"dax", "resolve_against_model"}


def test_format_measures_dry_run_then_write_then_undo(proj):
    import model_server.server as srv

    def call(name, **args):
        res = asyncio.run(srv.mcp.call_tool(name, args))
        if isinstance(res, tuple):
            res = res[0]
        structured = getattr(res, "structured_content", None) or getattr(res, "structuredContent", None)
        if structured is not None:
            return structured.get("result", structured)
        return json.loads(res.content[0].text)

    call("pbi_set_project", path=str(proj / "Synthetic.pbip"))
    before = snapshot(proj)
    preview = call("pbi_format_measures", dry_run=True)
    assert preview["dry_run"] is True and "Margin %" in preview["diff"]
    assert snapshot(proj) == before
    real = call("pbi_format_measures")
    assert real["count"] == 1 and snapshot(proj) != before
    call("pbi_undo")
    assert snapshot(proj) == before
    plain = call("pbi_format_dax", dax="sum(x)")
    assert plain["formatted"] == "SUM(x)"
    refs = call("pbi_dax_references", dax="[Net Revenue] + Sales[Amount]")
    assert refs["measures"] == ["Net Revenue"]
