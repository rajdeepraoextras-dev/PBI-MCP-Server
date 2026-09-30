"""DAX tokenizer / reference extractor / renamer (core.dax_parser)."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.dax_parser import DaxRefs, Token, parse_references, rename_references, tokenize
from core.lineage import extract_references
from core.tmdl import parse_table_file

TABLES = (Path(__file__).parent / "fixtures" / "synthetic"
          / "Synthetic.SemanticModel" / "definition" / "tables")

SNIPPETS = [
    "SUM(Sales[Amount])",
    "DIVIDE([Net Revenue] - SUM(Sales[Cost]), [Net Revenue])",
    "VAR _rev = [Net Revenue]\nRETURN\n\tDIVIDE(_rev, [Order Count])",
    "CALCULATE(\n\t[Net Revenue],\n\tALL(Date)\n)",
    "CALCULATE([X], ALL('Dim Date'), 'Dim Date'[Year] = 2024)",
    'IF([X] > 0, "not a [ref]", [Y]) -- [comment]',
    "/* block [Nope]\n   spans lines */ [Real] + 1 // trailing [Nope]",
    "COUNTROWS(FILTER(Sales, Sales[Amount] >= 100 && NOT ISBLANK([Cost])))",
    "SUMX(VALUES('Product'[Category]), [Net Revenue] * 1.5e2)",
    "[Total ]] Amount] + 'O''Brien'[Col]",
    "VAR t = FILTER(Sales, [Amount] > 0)\r\nRETURN COUNTROWS(t)",
    'SELECTEDVALUE(\'Date\'[Year], "n/a") & " :: " & FORMAT([X], "#,0")',
    "  [A] IN {1, 2}  ",
    "NORM.DIST(1, 0, 1, TRUE()) + VAR.P(Sales[Amount])",
    "",
]


@pytest.mark.parametrize("dax", SNIPPETS)
def test_roundtrip_rejoin(dax):
    toks = tokenize(dax)
    assert "".join(t.text for t in toks) == dax
    assert all(isinstance(t, Token) for t in toks)
    # offsets are exact and contiguous
    pos = 0
    for t in toks:
        assert (t.start, t.end) == (pos, pos + len(t.text))
        assert dax[t.start:t.end] == t.text
        pos = t.end
    assert pos == len(dax)


def test_roundtrip_fixture_measures():
    """Every measure in the synthetic model (incl. fenced multi-line) rejoins."""
    seen = 0
    for path in sorted(TABLES.glob("*.tmdl")):
        for m in parse_table_file(path).measures:
            assert "".join(t.text for t in tokenize(m.dax)) == m.dax
            seen += 1
    assert seen >= 6


def test_token_kinds_and_positions():
    toks = tokenize("VAR x = 'T'[C]\n  + [M] // c")
    kinds = [t.kind for t in toks if t.kind != "WS"]
    assert kinds == ["IDENT", "IDENT", "OP", "TABLE", "COLUMN", "OP",
                     "COLUMN", "COMMENT"]
    plus = next(t for t in toks if t.text == "+")
    assert (plus.line, plus.col) == (2, 3)


def test_operators_and_punct():
    toks = [t for t in tokenize("a := b <> c <= d >= e && f || g ^ h & i ! (j, {k})")
            if t.kind != "WS"]
    ops = [t.text for t in toks if t.kind == "OP"]
    assert ops == [":=", "<>", "<=", ">=", "&&", "||", "^", "&", "!"]
    assert [t.text for t in toks if t.kind == "PUNCT"] == ["(", ",", "{", "}", ")"]


# --- comments / strings never count ----------------------------------------

def test_comments_ignored():
    dax = ("// [Line1] Sales[Skip]\n"
           "-- [Line2]\n"
           "/* [Block] 'T'[Skip]\n [Multi] */\n"
           "[Real] + Sales[Amount]")
    refs = parse_references(dax)
    assert refs.measures == {"Real"}
    assert refs.columns == {("Sales", "Amount")}
    assert refs.tables == set()


def test_strings_ignored_including_escapes_and_brackets():
    dax = '"He said ""[Nope]"" -- not a comment" & [Real] & "x[Y]" & Sales[Amt]'
    toks = tokenize(dax)
    assert [t.text for t in toks if t.kind == "STRING"] == [
        '"He said ""[Nope]"" -- not a comment"', '"x[Y]"']
    refs = parse_references(dax)
    assert refs.measures == {"Real"}
    assert refs.columns == {("Sales", "Amt")}


def test_comment_markers_inside_string_do_not_start_comment():
    dax = '"http://example.com" & [X]'
    assert parse_references(dax).measures == {"X"}


# --- names ------------------------------------------------------------------

def test_bracket_escape_in_column_name():
    refs = parse_references("Sales[Total ]] Amount] + [A ]] B]")
    assert refs.columns == {("Sales", "Total ] Amount")}
    assert refs.measures == {"A ] B"}


def test_quoted_tables_with_spaces_and_apostrophes():
    refs = parse_references("'Dim Date'[Year] + 'O''Brien'[Col] + COUNTROWS('Dim Date')")
    assert refs.columns == {("Dim Date", "Year"), ("O'Brien", "Col")}
    assert refs.tables == {"Dim Date"}
    assert refs.functions == {"COUNTROWS"}


def test_qualified_vs_bare():
    refs = parse_references("Sales[Amount] + [Net Revenue] + Sales [Cost]")
    assert refs.columns == {("Sales", "Amount"), ("Sales", "Cost")}
    assert refs.measures == {"Net Revenue"}
    assert refs.tables == set()          # the qualifier is not a bare table ref


def test_keyword_before_bracket_is_not_a_qualifier():
    refs = parse_references("VAR a = 1\nRETURN [X] + NOT [Flag]")
    assert refs.measures == {"X", "Flag"}
    assert refs.columns == set()
    assert refs.variables == {"a"}


# --- functions / tables / variables ----------------------------------------

def test_function_vs_table():
    refs = parse_references("ALL(Date) + DATE(2024, 1, 1)")
    assert refs.functions == {"ALL", "DATE"}
    assert refs.tables == {"Date"}


def test_function_names_are_canonical_upper():
    refs = parse_references("calculate([X], all('Dim Date'), Norm.Dist(1,0,1,true()))")
    assert refs.functions == {"CALCULATE", "ALL", "NORM.DIST", "TRUE"}
    assert refs.tables == {"Dim Date"}
    assert refs.measures == {"X"}


def test_var_shadows_table():
    dax = ("VAR Sales = FILTER(Customers, [Amount] > 0)\n"
           "VAR n = COUNTROWS(Sales)\n"
           "RETURN n + COUNTROWS(Customers) + COUNTROWS(sales)")
    refs = parse_references(dax)
    assert refs.variables == {"Sales", "n"}
    assert refs.tables == {"Customers"}
    assert refs.functions == {"FILTER", "COUNTROWS"}


def test_keywords_are_not_tables():
    refs = parse_references("EVALUATE Sales ORDER BY Sales[Amount] ASC, [M] DESC")
    assert refs.tables == {"Sales"}
    assert refs.columns == {("Sales", "Amount")}
    assert refs.measures == {"M"}
    assert refs.functions == set()


def test_return_paren_is_not_a_function():
    refs = parse_references("VAR a = [X]\nRETURN ( a + 1 )")
    assert refs.functions == set()
    assert refs.variables == {"a"}


# --- known_measures ---------------------------------------------------------

def test_known_measures_moves_bare_refs_to_unqualified():
    dax = "SUMX(Sales, [Amount] * [Rate]) + [Net Revenue]"
    default = parse_references(dax)
    assert default.measures == {"Amount", "Rate", "Net Revenue"}
    assert default.unqualified == set()
    known = parse_references(dax, known_measures={"Net Revenue"})
    assert known.measures == {"Net Revenue"}
    assert known.unqualified == {"Amount", "Rate"}


def test_daxrefs_default_is_empty():
    assert parse_references("") == DaxRefs()


# --- rename ---------------------------------------------------------------------

def test_rename_measure_only_bare_refs():
    dax = 'CALCULATE([Net Revenue], Sales[Net Revenue] > 0) -- [Net Revenue]\n& "[Net Revenue]"'
    out = rename_references(dax, measure=("Net Revenue", "Revenue"))
    assert out == ('CALCULATE([Revenue], Sales[Net Revenue] > 0) -- [Net Revenue]\n'
                   '& "[Net Revenue]"')


def test_rename_measure_escapes_brackets_and_matches_case_insensitively():
    out = rename_references("[net revenue] + 1", measure=("Net Revenue", "A]B"))
    assert out == "[A]]B] + 1"


def test_rename_column_qualified_forms_and_quoting_rule():
    dax = "SUM(Sales[Amount]) + SUM('Sales'[Amount]) + SUM(Sales[Cost]) + [Amount]"
    # plain new table name keeps each token's original quoting style
    out = rename_references(dax, column=(("Sales", "Amount"), ("Sales", "Net Amount")))
    assert out == ("SUM(Sales[Net Amount]) + SUM('Sales'[Net Amount]) "
                   "+ SUM(Sales[Cost]) + [Amount]")
    # a new table name with a space forces quoting everywhere
    out = rename_references(dax, column=(("Sales", "Amount"), ("Fact Sales", "Amount")))
    assert out == ("SUM('Fact Sales'[Amount]) + SUM('Fact Sales'[Amount]) "
                   "+ SUM(Sales[Cost]) + [Amount]")


def test_rename_column_bare_refs_need_known_measures():
    dax = "SUMX(Sales, [Amount] * 2) + [Net Revenue]"
    untouched = rename_references(dax, column=(("Sales", "Amount"), ("Sales", "Amt")))
    assert untouched == dax
    out = rename_references(dax, column=(("Sales", "Amount"), ("Sales", "Amt")),
                            known_measures={"Net Revenue"})
    assert out == "SUMX(Sales, [Amt] * 2) + [Net Revenue]"
    # a bare ref that IS a known measure is never touched by column=
    same = rename_references(dax, column=(("Sales", "Net Revenue"), ("Sales", "NR")),
                             known_measures={"Net Revenue"})
    assert same == dax


def test_rename_table_all_forms_but_not_vars_or_strings():
    dax = ("VAR Date = 1 /* Date */ RETURN\n"
           "CALCULATE([X], ALL(Date), 'Date'[Year] = 2024, Date[Month] = 1)"
           ' & "Date" & DATE(2024, 1, 1) & COUNTROWS(Date)')
    out = rename_references(dax, table=("Date", "Calendar"))
    # `Date` is a VAR here, so bare uses are variables and stay put; the
    # qualifiers, the string, the comment and the DATE() function are handled.
    assert out == ("VAR Date = 1 /* Date */ RETURN\n"
                   "CALCULATE([X], ALL(Date), 'Calendar'[Year] = 2024, "
                   "Calendar[Month] = 1)"
                   ' & "Date" & DATE(2024, 1, 1) & COUNTROWS(Date)')
    plain = "CALCULATE([X], ALL(Date), 'Date'[Year] = 2024) + COUNTROWS(date)"
    assert rename_references(plain, table=("Date", "Dim Date")) == (
        "CALCULATE([X], ALL('Dim Date'), 'Dim Date'[Year] = 2024) "
        "+ COUNTROWS('Dim Date')")
    assert rename_references(plain, table=("Date", "Cal")) == (
        "CALCULATE([X], ALL(Cal), 'Cal'[Year] = 2024) + COUNTROWS(Cal)")


def test_rename_table_quotes_keyword_and_apostrophe_names():
    assert rename_references("ALL(Sales)", table=("Sales", "Table")) == "ALL('Table')"
    assert rename_references("ALL(Sales)", table=("Sales", "O'Brien")) == "ALL('O''Brien')"
    assert rename_references("ALL(Sales)", table=("Sales", "2024")) == "ALL('2024')"


def test_rename_noop_when_nothing_matches():
    dax = "  /* keep */ SUM ( Sales[Amount] )  -- tail\n"
    assert rename_references(dax) == dax
    assert rename_references(dax, measure=("Ghost", "X")) == dax
    assert rename_references(dax, column=(("Other", "Amount"), ("Other", "B"))) == dax
    assert rename_references(dax, table=("Nope", "X")) == dax


def test_rename_combined_preserves_layout():
    dax = "VAR _r = [Net Revenue]\n\tRETURN\n\t\tDIVIDE(_r, SUM(Sales[Cost]))  // note"
    out = rename_references(dax, measure=("Net Revenue", "Revenue"),
                            column=(("Sales", "Cost"), ("Sales", "COGS")))
    assert out == "VAR _r = [Revenue]\n\tRETURN\n\t\tDIVIDE(_r, SUM(Sales[COGS]))  // note"


# --- malformed input --------------------------------------------------------

@pytest.mark.parametrize("dax", [
    '"unterminated string [X]',
    "/* unterminated comment [X]",
    "[unterminated bracket + Sales[Amount]",
    "'unterminated table [X]",
    "SUM(Sales[Amount]",
    "))) ,, @#$ ~ `",
    "[X]]",
    '"a" "b',
])
def test_malformed_input_never_raises(dax):
    toks = tokenize(dax)
    assert "".join(t.text for t in toks) == dax
    parse_references(dax)
    rename_references(dax, measure=("X", "Y"), table=("Sales", "S"),
                      column=(("Sales", "Amount"), ("Sales", "A")))


def test_malformed_unknown_tokens():
    toks = tokenize('"open [X]')
    assert [t.kind for t in toks] == ["UNKNOWN"]
    # `[` is legal inside a name (only `]` needs escaping), so a bracket is
    # only unterminated when the line ends first; then `[` alone is UNKNOWN
    # and tokenizing resumes right after it.
    dax = "[open + 1\nSales[Amount]"
    toks = tokenize(dax)
    assert toks[0].kind == "UNKNOWN" and toks[0].text == "["
    assert [t.kind for t in toks if t.kind != "WS"] == [
        "UNKNOWN", "IDENT", "OP", "NUMBER", "IDENT", "COLUMN"]
    assert parse_references(dax).columns == {("Sales", "Amount")}


# --- lineage integration ----------------------------------------------------

def test_lineage_extract_references_uses_parser():
    dax = 'RETURN [X] + Sales[Amount] + "[Nope]" -- [Nope]'
    assert extract_references(dax) == [("Sales", "Amount"), (None, "X")]
