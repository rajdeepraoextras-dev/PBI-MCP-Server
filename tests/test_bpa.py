"""Best Practice Analyzer: rule engine, fixers, custom rules and tools.

Every rule is exercised against mutated copies of the synthetic fixture: a
positive case where it must fire on a named object, and a negative case where
it must stay silent (see CASES).
"""

from __future__ import annotations

import asyncio
import difflib
import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from core import bpa
from core.journal import snapshot
from core.pbip import PbipProject
from core.tmdl import emit_calc_group_table
from model_server import tools_bpa
from model_server.server import ModelState, set_project

SYNTH = Path(__file__).parent / "fixtures" / "synthetic"

SALES = "tables/Sales.tmdl"
DATE = "tables/Date.tmdl"
RELS = "definition/relationships.tmdl"
MODEL = "definition/model.tmdl"
SM = "Synthetic.SemanticModel/definition"


# --- project builder ----------------------------------------------------------

def base_files() -> dict[str, str]:
    out = {}
    for p in sorted(SYNTH.rglob("*")):
        if p.is_file():
            out[p.relative_to(SYNTH).as_posix()] = p.read_bytes().decode("utf-8").replace("\r\n", "\n")
    return out


def _find(files: dict[str, str], suffix: str) -> str:
    hits = [k for k in files if k.endswith(suffix)]
    assert len(hits) == 1, (suffix, hits)
    return hits[0]


def sub(suffix: str, old: str, new: str, count: int = 1):
    def apply(files):
        k = _find(files, suffix)
        assert old in files[k], f"{old!r} not in {k}"
        files[k] = files[k].replace(old, new) if count == 0 else files[k].replace(old, new, count)
    return apply


def insert_before(suffix: str, marker: str, text: str):
    return sub(suffix, marker, text + marker)


def append(suffix: str, text: str):
    def apply(files):
        k = _find(files, suffix)
        files[k] = files[k].rstrip("\n") + "\n" + text
    return apply


def add(path: str, text: str):
    def apply(files):
        assert path not in files
        files[path] = text
    return apply


def build(tmp: Path, edits=(), *, drop_report: bool = False, crlf: bool = False) -> Path:
    files = base_files()
    for e in edits:
        e(files)
    if drop_report:
        files = {k: v for k, v in files.items() if not k.startswith("Synthetic.Report")}
    for rel, text in files.items():
        path = tmp / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        data = text.replace("\n", "\r\n") if crlf and rel.endswith(".tmdl") else text
        path.write_bytes(data.encode("utf-8"))
    return tmp / "Synthetic.pbip"


def run(tmp: Path, edits=(), **kw) -> dict:
    project = PbipProject(build(tmp, edits, **{k: kw.pop(k) for k in ("drop_report",) if k in kw}))
    return bpa.analyze(project, **kw)


def keys(result: dict) -> set[tuple[str | None, str]]:
    return {(f["table"], f["name"]) for f in result["findings"]}


# --- snippets -----------------------------------------------------------------------

def q(name: str) -> str:
    return name if re.fullmatch(r"[A-Za-z_]\w*", name) else "'" + name + "'"


def col(name, dtype="string", *, hidden=False, summarize="none", extra="", source=True,
        doc="") -> str:
    lines = ([f"\t/// {doc}"] if doc else []) + [f"\tcolumn {q(name)}", f"\t\tdataType: {dtype}"]
    if hidden:
        lines.append("\t\tisHidden")
    lines.append(f"\t\tsummarizeBy: {summarize}")
    if extra:
        lines.append(extra)
    if source:
        lines.append(f"\t\tsourceColumn: {name}")
    return "\n".join(lines) + "\n\n"


def calc_col(name, dax, dtype="int64") -> str:
    return f"\tcolumn {q(name)} = {dax}\n\t\tdataType: {dtype}\n\t\tsummarizeBy: none\n\n"


def in_sales(text: str):
    return insert_before(SALES, "\tpartition Sales = m", text)


def measure(name, dax, fmt="0", extra="") -> str:
    return (f"\tmeasure {q(name)} = {dax}\n"
            + (f"\t\tformatString: {fmt}\n" if fmt else "") + extra + "\n")


def add_measure(name, dax, fmt="0"):
    return insert_before(SALES, "\tcolumn Amount\n", measure(name, dax, fmt))


def table_file(name: str, body: str):
    return add(f"{SM}/tables/{name}.tmdl", f"table {name}\n{body}")


def rel(name: str, frm: str, to: str, extra: str = ""):
    body = "".join(f"\t{line}\n" for line in extra.split("\n") if line)
    return append(RELS, f"\nrelationship {name}\n{body}\tfromColumn: {frm}\n\ttoColumn: {to}\n")


def role(name: str, table: str, expr: str):
    return add(f"{SM}/roles/{name}.tmdl",
               f"role {name}\n\tmodelPermission: read\n\n\ttablePermission {table} = {expr}\n")


def region_dim():
    return table_file("Region", "\n" + col("Year", "int64", summarize="none", doc="Region year"))


NO_TIME = sub(DATE, "\tdataCategory: Time\n", "")
DQ = sub(SALES, "\t\tmode: import", "\t\tmode: directQuery")
SUMMARIZE_NONE = sub(SALES, "summarizeBy: sum", "summarizeBy: none", count=0)


@dataclass
class Case:
    pos: list = field(default_factory=list)
    neg: list = field(default_factory=list)
    expect: set = field(default_factory=set)
    mode: str = "none"        # none: negative has no findings; absent: expected objects gone


CASES: dict[str, Case] = {}


def case(rule: str, expect, pos=(), neg=(), mode="none"):
    assert rule not in CASES
    CASES[rule] = Case(list(pos), list(neg), set(expect), mode)


# Performance -----------------------------------------------------------------------
case("AVOID_FLOATING_POINT_DATA_TYPES", {("Sales", "Amount"), ("Sales", "Cost")},
     neg=[sub(SALES, "dataType: double", "dataType: decimal", count=0)])
case("REDUCE_USAGE_OF_CALCULATED_COLUMNS_THAT_USE_THE_RELATED_FUNCTION", {("Sales", "Rel Col")},
     pos=[in_sales(calc_col("Rel Col", "RELATED(Date[Year])"))],
     neg=[in_sales(calc_col("Rel Col", "Sales[Amount] * 2"))])
case("SNOWFLAKE_SCHEMA_ARCHITECTURE", {("Date", "Date")},
     pos=[region_dim(), rel("r2", "Date.Year", "Region.Year")])
case("MODEL_SHOULD_HAVE_A_DATE_TABLE", {(None, "Model")}, pos=[NO_TIME])
case("DATE/CALENDAR_TABLES_SHOULD_BE_MARKED_AS_A_DATE_TABLE", {("Date", "Date")}, pos=[NO_TIME])
case("REMOVE_AUTO-DATE_TABLE", {("LocalDateTable_1234", "LocalDateTable_1234")},
     pos=[table_file("LocalDateTable_1234", "\tpartition L = calculated\n\t\tsource = CALENDAR(1, 2)\n")])
case("AVOID_EXCESSIVE_BI-DIRECTIONAL_OR_MANY-TO-MANY_RELATIONSHIPS", {(None, "Model")},
     pos=[sub(RELS, "\tfromColumn", "\tcrossFilteringBehavior: bothDirections\n\tfromColumn")])
case("MODEL_USING_DIRECT_QUERY_AND_NO_AGGREGATIONS", {(None, "Model")}, pos=[DQ])
case("MINIMIZE_POWER_QUERY_TRANSFORMATIONS", {("Sales", "Sales")},
     pos=[sub(SALES, 'Source = Csv.Document(File.Contents("sales.csv"))',
              'Source = Table.AddColumn(Csv.Document(File.Contents("sales.csv")), "x", each 1)')])
case("UNPIVOT_PIVOTED_(MONTH)_DATA", {("Sales", "Sales")},
     pos=[in_sales(col("Jan", "int64") + col("February", "double") + col("Mar 2020", "int64"))],
     neg=[in_sales(col("Jan", "int64") + col("Feb", "int64") + col("Marketing", "int64"))])
case("MANY-TO-MANY_RELATIONSHIPS_SHOULD_BE_SINGLE-DIRECTION", {("Sales", "Sales[OrderDate] -> Date[Date]")},
     pos=[sub(RELS, "\tfromColumn", "\tfromCardinality: many\n\ttoCardinality: many\n"
                                     "\tcrossFilteringBehavior: bothDirections\n\tfromColumn")],
     neg=[sub(RELS, "\tfromColumn", "\tfromCardinality: many\n\ttoCardinality: many\n\tfromColumn")])
case("REDUCE_USAGE_OF_CALCULATED_TABLES", {("Date", "Date")},
     neg=[sub(DATE, "partition Date = calculated", "partition Date = m")])
case("REMOVE_REDUNDANT_COLUMNS_IN_RELATED_TABLES", {("Sales", "Year")},
     pos=[in_sales(col("Year", "int64"))])
case("MEASURES_USING_TIME_INTELLIGENCE_AND_MODEL_IS_USING_DIRECT_QUERY", {("Sales", "YTD")},
     pos=[DQ, add_measure("YTD", "TOTALYTD([Net Revenue], Date[Date])")],
     neg=[add_measure("YTD", "TOTALYTD([Net Revenue], Date[Date])")])
case("REDUCE_NUMBER_OF_CALCULATED_COLUMNS", {(None, "Model")},
     pos=[in_sales("".join(calc_col(f"C{i}", "1") for i in range(6)))],
     neg=[in_sales("".join(calc_col(f"C{i}", "1") for i in range(5)))])
case("CHECK_IF_BI-DIRECTIONAL_AND_MANY-TO-MANY_RELATIONSHIPS_ARE_VALID",
     {("Sales", "Sales[OrderDate] -> Date[Date]")},
     pos=[sub(RELS, "\tfromColumn", "\tcrossFilteringBehavior: bothDirections\n\tfromColumn")])
case("LIMIT_ROW_LEVEL_SECURITY_(RLS)_LOGIC", {("Sales", "Reader")},
     pos=[role("Reader", "Sales", 'LEFT(Sales[Amount], 1) = "1"')],
     neg=[role("Reader", "Sales", "Sales[Amount] > 0")])
case("CHECK_IF_DYNAMIC_ROW_LEVEL_SECURITY_(RLS)_IS_NECESSARY", {("Sales", "Reader")},
     pos=[role("Reader", "Sales", 'Sales[Amount] > 0 || USERPRINCIPALNAME() = "a@b.c"')],
     neg=[role("Reader", "Sales", "Sales[Amount] > 0")])
case("AVOID_USING_MANY-TO-MANY_RELATIONSHIPS_ON_TABLES_USED_FOR_DYNAMIC_ROW_LEVEL_SECURITY",
     {("Date", "Date")},
     pos=[role("Reader", "Date", "Date[Year] > 2000"),
          sub(RELS, "\tfromColumn", "\tfromCardinality: many\n\ttoCardinality: many\n\tfromColumn")],
     neg=[role("Reader", "Date", "Date[Year] > 2000")])
case("AVOID_SINGLE_ATTRIBUTE_DIMENSIONS", {("Date", "Date")},
     neg=[insert_before(DATE, "\tpartition Date", col("Month", "int64"))])

# DAX Expressions ---------------------------------------------------------------------
case("DAX_COLUMNS_FULLY_QUALIFIED", {("Sales", "Complex Measure")},
     neg=[sub(SALES, "DIVIDE(_rev, [Order Count])", "DIVIDE(_rev, SUM(Sales[Order Count]))")])
case("DAX_MEASURES_UNQUALIFIED", {("Sales", "Bad Ref")},
     pos=[add_measure("Bad Ref", "Sales[Net Revenue] * 2")],
     neg=[add_measure("Bad Ref", "[Net Revenue] * 2")])
case("AVOID_DUPLICATE_MEASURES", {("Sales", "Net Revenue"), ("Sales", "Net Revenue Copy")},
     pos=[add_measure("Net Revenue Copy", "SUM( Sales[Amount] )", "#,0")])
case("USE_THE_TREATAS_FUNCTION_INSTEAD_OF_INTERSECT", {("Sales", "Virtual")},
     pos=[add_measure("Virtual", "CALCULATE([Net Revenue], INTERSECT(VALUES(Sales[OrderDate]), VALUES(Date[Date])))")],
     neg=[add_measure("Virtual", "CALCULATE([Net Revenue], TREATAS(VALUES(Sales[OrderDate]), Date[Date]))")])
case("USE_THE_DIVIDE_FUNCTION_FOR_DIVISION", {("Sales", "Ratio")},
     pos=[add_measure("Ratio", "[Net Revenue] / SUM(Sales[Cost])")],
     neg=[add_measure("Ratio", "DIVIDE([Net Revenue], SUM(Sales[Cost])) + [Net Revenue] / 100 - 1 // 2 / 3")])
case("AVOID_USING_THE_IFERROR_FUNCTION", {("Sales", "Safe")},
     pos=[add_measure("Safe", "IFERROR([Net Revenue], 0)")],
     neg=[add_measure("Safe", "IF(ISERROR([Net Revenue]), 0, [Net Revenue])")])
case("MEASURES_SHOULD_NOT_BE_DIRECT_REFERENCES_OF_OTHER_MEASURES", {("Sales", "Alias")},
     pos=[add_measure("Alias", "[Net Revenue]")],
     neg=[add_measure("Alias", "[Net Revenue] * 2")])
case("FILTER_COLUMN_VALUES", {("Sales", "Big")},
     pos=[add_measure("Big", "CALCULATE([Net Revenue], FILTER(Sales, Sales[Amount] > 0))")],
     neg=[add_measure("Big", "CALCULATE([Net Revenue], FILTER(ALL(Sales[Amount]), Sales[Amount] > 0))")])
case("FILTER_MEASURE_VALUES_BY_COLUMNS", {("Sales", "Top")},
     pos=[add_measure("Top", "CALCULATE([Net Revenue], FILTER(Sales, [Net Revenue] > 100))")],
     neg=[add_measure("Top", "CALCULATE([Net Revenue], FILTER(VALUES(Sales[OrderDate]), [Net Revenue] > 100))")])
case("NO_CALCULATE_FILTER_ON_WHOLE_TABLE", {("Sales", "Whole"), ("Sales", "Whole2")},
     pos=[add_measure("Whole", "CALCULATE([Net Revenue], Sales)"),
          add_measure("Whole2", "CALCULATE([Net Revenue], FILTER(Sales, NOT ISBLANK(Sales[Cost])))")],
     neg=[add_measure("Whole", "CALCULATE([Net Revenue], ALL(Date))")])
case("INACTIVE_RELATIONSHIPS_THAT_ARE_NEVER_ACTIVATED", {("Sales", "Sales[OrderDate] -> Date[Year]")},
     pos=[rel("r2", "Sales.OrderDate", "Date.Year", "isActive: false")],
     neg=[rel("r2", "Sales.OrderDate", "Date.Year", "isActive: false"),
          add_measure("Ship", "CALCULATE([Net Revenue], USERELATIONSHIP(Date[Year], Sales[OrderDate]))")])
case("AVOID_USING_'1-(X/Y)'_SYNTAX", {("Sales", "Drop")},
     pos=[add_measure("Drop", "1 - DIVIDE([Net Revenue], SUM(Sales[Cost]))")],
     neg=[add_measure("Drop", 'DIVIDE([Net Revenue] - SUM(Sales[Cost]), SUM(Sales[Cost])) // 1 - DIVIDE(')])
case("EVALUATEANDLOG_SHOULD_NOT_BE_USED_IN_PRODUCTION_MODELS", {("Sales", "Dbg")},
     pos=[add_measure("Dbg", "EVALUATEANDLOG([Net Revenue])")],
     neg=[add_measure("Dbg", "[Net Revenue] + 1")])
case("DAX_TODO", {("Sales", "Later")},
     pos=[add_measure("Later", "1 // TODO fix me")], neg=[add_measure("Later", "1 // fine")])

# Error Prevention ----------------------------------------------------------------------
case("DATA_COLUMNS_MUST_HAVE_A_SOURCE_COLUMN", {("Sales", "Order Count")},
     neg=[sub(SALES, "\t\tisHidden\n\n\tpartition", "\t\tisHidden\n\t\tsourceColumn: Order Count\n\n\tpartition")])
case("EXPRESSION_RELIANT_OBJECTS_MUST_HAVE_AN_EXPRESSION", {("Sales", "Empty")},
     pos=[insert_before(SALES, "\tcolumn Amount\n", "\tmeasure Empty =\n\t\tformatString: 0\n\n")],
     neg=[add_measure("Empty", "1")])
case("RELATIONSHIP_COLUMNS_SAME_DATA_TYPE", {("Sales", "Sales[OrderDate] -> Date[Date]")},
     pos=[sub(SALES, "column OrderDate\n\t\tdataType: dateTime", "column OrderDate\n\t\tdataType: string")])
case("AVOID_INVALID_NAME_CHARACTERS", {("Sales", "Bad\x01Name")},
     pos=[in_sales(col("Bad\x01Name", "string"))])
case("AVOID_INVALID_DESCRIPTION_CHARACTERS", {("Sales", "Described")},
     pos=[in_sales(col("Described", "string", doc="bad\x01text"))],
     neg=[in_sales(col("Described", "string", doc="perfectly fine"))])
case("AVOID_THE_USERELATIONSHIP_FUNCTION_AND_RLS_AGAINST_THE_SAME_TABLE", {("Date", "Date")},
     pos=[role("Reader", "Date", "Date[Year] > 2000"),
          add_measure("Ship", "CALCULATE([Net Revenue], USERELATIONSHIP(Sales[OrderDate], Date[Date]))")],
     neg=[add_measure("Ship", "CALCULATE([Net Revenue], USERELATIONSHIP(Sales[OrderDate], Date[Date]))")])
_CG = [{"name": "Current", "dax": "SELECTEDMEASURE()"}]
case("CALCULATION_GROUPS_NO_PRECEDENCE_CONFLICT", {("CG1", "CG1"), ("CG2", "CG2")},
     pos=[add(f"{SM}/tables/CG1.tmdl", emit_calc_group_table("CG1", 10, _CG)),
          add(f"{SM}/tables/CG2.tmdl", emit_calc_group_table("CG2", 10, _CG))],
     neg=[add(f"{SM}/tables/CG1.tmdl", emit_calc_group_table("CG1", 10, _CG)),
          add(f"{SM}/tables/CG2.tmdl", emit_calc_group_table("CG2", 20, _CG))])

# Maintenance -------------------------------------------------------------------------------
case("REMOVE_UNUSED_COLUMNS", {("Sales", "Order Count")},
     neg=[in_sales(calc_col("Doubled", "Sales[Order Count] * 2"))], mode="absent")
case("UNUSED_MEASURES", {("Sales", "Hidden Helper")},
     neg=[sub(SALES, "SUM(Sales[Amount])\n", "SUM(Sales[Amount]) + [Hidden Helper]\n")], mode="absent")
case("ENSURE_TABLES_HAVE_RELATIONSHIPS", {("Lonely", "Lonely")},
     pos=[table_file("Lonely", "\n" + col("Name", "string"))],
     neg=[table_file("Lonely", "\n" + col("Year", "int64")), rel("r2", "Lonely.Year", "Date.Year")])
case("OBJECTS_WITH_NO_DESCRIPTION", {("Sales", "Net Revenue")},
     neg=[sub(SALES, "\tmeasure 'Net Revenue'", "\t/// Revenue after returns\n\tmeasure 'Net Revenue'")],
     mode="absent")
case("CALCULATION_GROUPS_WITH_NO_CALCULATION_ITEMS", {("Empty CG", "Empty CG")},
     pos=[add(f"{SM}/tables/Empty CG.tmdl", emit_calc_group_table("Empty CG", 5, _CG).replace(
         "\n\t\tcalculationItem Current = SELECTEDMEASURE()\n", "\n"))],
     neg=[add(f"{SM}/tables/Empty CG.tmdl", emit_calc_group_table("Empty CG", 5, _CG))])

# Naming Conventions ---------------------------------------------------------------------
case("PARTITION_NAME_SHOULD_MATCH_TABLE_NAME_FOR_SINGLE_PARTITION_TABLES", {("Sales", "Sales-1")},
     pos=[sub(SALES, "partition Sales = m", "partition Sales-1 = m")])
case("SPECIAL_CHARS_IN_OBJECT_NAMES", {("Sales", "Tab\tName")},
     pos=[add_measure("Tab\tName", "1")])
case("UPPERCASE_FIRST_LETTER_MEASURES_TABLES", {("Sales", "lower measure")},
     pos=[add_measure("lower measure", "1")], neg=[add_measure("Lower measure", "1")])
case("UPPERCASE_FIRST_LETTER_COLUMNS_HIERARCHIES", {("Sales", "lowerCol")},
     pos=[in_sales(col("lowerCol", "string"))],
     neg=[in_sales(col("lowerCol", "string", hidden=True))])
case("RELATIONSHIP_COLUMN_NAMES", {("Sales", "Sales[OrderDate] -> Date[Date]")},
     neg=[sub(SALES, "column OrderDate", "column Date"), sub(RELS, "Sales.OrderDate", "Sales.Date")])

# Formatting --------------------------------------------------------------------------------------
case("FORMAT_FLAG_COLUMNS_AS_YES/NO_VALUE_STRINGS", {("Sales", "IsActive")},
     pos=[in_sales(col("IsActive", "int64"))], neg=[in_sales(col("IsActive", "string"))])
case("OBJECTS_SHOULD_NOT_START_OR_END_WITH_A_SPACE", {("Sales", " Padded ")},
     pos=[add_measure(" Padded ", "1")], neg=[add_measure("Padded", "1")])
case("PROVIDE_FORMAT_STRING_FOR_MEASURES", {("Sales", "Net Revenue")},
     pos=[sub(SALES, "formatString: #,0\n\t\tdisplayFolder: KPIs\n\n\tmeasure 'Margin",
              "displayFolder: KPIs\n\n\tmeasure 'Margin")])
case("NUMERIC_COLUMN_SUMMARIZE_BY", {("Sales", "Amount"), ("Sales", "Cost")}, neg=[SUMMARIZE_NONE])
case("PERCENTAGE_FORMATTING", {("Sales", "Margin %")},
     neg=[sub(SALES, "formatString: 0.0%", "formatString: #,0.0%;-#,0.0%;#,0.0%")])
case("RELATIONSHIP_COLUMNS_SHOULD_BE_OF_INTEGER_DATA_TYPE", {("Sales", "OrderDate"), ("Date", "Date")},
     pos=[NO_TIME])
case("ADD_DATA_CATEGORY_FOR_COLUMNS", {("Sales", "Latitude"), ("Sales", "WebUrl"), ("Sales", "Country")},
     pos=[in_sales(col("Latitude", "double") + col("WebUrl", "string") + col("Country", "string"))],
     neg=[in_sales(col("Latitude", "double", extra="\t\tdataCategory: Latitude")
                   + col("WebUrl", "string", extra="\t\tdataCategory: WebUrl")
                   + col("Country", "string", extra="\t\tdataCategory: Country"))])
case("HIDE_FOREIGN_KEYS", {("Sales", "OrderDate")},
     neg=[sub(SALES, "column OrderDate\n\t\tdataType: dateTime\n", "column OrderDate\n\t\tdataType: dateTime\n\t\tisHidden\n")])
case("MARK_PRIMARY_KEYS", {("Date", "Date")},
     pos=[NO_TIME, sub(DATE, "\t\tisKey\n", "")])
case("HIDE_FACT_TABLE_COLUMNS", {("Sales", "Amount"), ("Sales", "Cost")},
     neg=[sub(SALES, "column Amount\n", "column Amount\n\t\tisHidden\n"),
          sub(SALES, "column Cost\n", "column Cost\n\t\tisHidden\n")])
case("MONTH_(AS_A_STRING)_MUST_BE_SORTED", {("Sales", "Month Name")},
     pos=[in_sales(col("Month Name", "string"))],
     neg=[in_sales(col("Month Name", "string", extra="\t\tsortByColumn: Amount"))])
case("APPLY_FORMAT_STRING_COLUMNS", {("Sales", "Amount")},
     neg=[sub(SALES, "column Amount\n", "column Amount\n\t\tformatString: #,0.00\n")], mode="absent")


def test_every_rule_has_a_case():
    assert set(CASES) == {r.id for r in bpa.builtin_rules()}


@pytest.mark.parametrize("rule_id", sorted(CASES))
def test_rule_fires_on_positive_and_stays_silent_on_negative(rule_id, tmp_path):
    c = CASES[rule_id]
    pos = run(tmp_path / "pos", c.pos, rules=[rule_id])
    assert pos["warnings"] == [], pos["warnings"]
    got = keys(pos)
    assert c.expect <= got, f"{rule_id}: expected {c.expect}, got {got}"
    assert all(f["rule_id"] == rule_id for f in pos["findings"])
    neg = run(tmp_path / "neg", c.neg, rules=[rule_id])
    assert neg["warnings"] == [], neg["warnings"]
    if c.mode == "absent":
        assert not (c.expect & keys(neg)), f"{rule_id}: still flagged {c.expect & keys(neg)}"
    else:
        assert neg["findings"] == [], f"{rule_id}: negative case fired: {keys(neg)}"


def test_findings_have_the_documented_shape(tmp_path):
    result = run(tmp_path, [])
    assert result["findings"]
    for f in result["findings"]:
        assert set(f) == {"rule_id", "category", "severity", "object_type", "table",
                          "name", "message", "fixable"}
        assert f["category"] in bpa.CATEGORIES and f["severity"] in (1, 2, 3)
        assert isinstance(f["fixable"], bool) and f["message"]
        json.dumps(f)


# --- catalog ----------------------------------------------------------------------------------

def test_catalog_is_consistent():
    rules = bpa.builtin_rules()
    assert len(rules) >= 30
    ids = [r.id for r in rules]
    assert len(set(ids)) == len(ids)
    assert set(bpa._CHECKS) == set(ids)
    norm = [bpa.norm_id(x) for r in rules for x in (r.id, *r.aliases)]
    assert len(set(norm)) == len(norm), "rule ids/aliases collide"
    for r in rules:
        assert r.category in bpa.CATEGORIES, r.id
        assert r.severity in (1, 2, 3), r.id
        assert r.description.strip() and r.scopes and r.title, r.id
        assert r.origin in ("TabularEditor", "pbi-mcp")
        assert r.fixer is None or r.fixer in bpa.FIXERS
    assert {r.category for r in rules} == set(bpa.CATEGORIES)
    assert sum(1 for r in rules if r.fixer) == 3
    assert sum(1 for r in rules if r.origin == "TabularEditor") >= 30


def test_ported_rules_keep_tabular_editor_ids_categories_and_severities():
    by_id = {r.id: r for r in bpa.builtin_rules()}
    expected = {
        "AVOID_FLOATING_POINT_DATA_TYPES": ("Performance", 2),
        "HIDE_FOREIGN_KEYS": ("Formatting", 2),
        "PROVIDE_FORMAT_STRING_FOR_MEASURES": ("Formatting", 3),
        "ADD_DATA_CATEGORY_FOR_COLUMNS": ("Formatting", 1),
        "OBJECTS_WITH_NO_DESCRIPTION": ("Maintenance", 1),
        "USE_THE_DIVIDE_FUNCTION_FOR_DIVISION": ("DAX Expressions", 2),
        "AVOID_USING_THE_IFERROR_FUNCTION": ("DAX Expressions", 2),
        "RELATIONSHIP_COLUMNS_SAME_DATA_TYPE": ("Error Prevention", 3),
        "TRIM": None,
        "SPECIAL_CHARS_IN_OBJECT_NAMES": ("Naming Conventions", 2),
        "MINIMIZE_POWER_QUERY_TRANSFORMATIONS": ("Performance", 2),
        "MODEL_SHOULD_HAVE_A_DATE_TABLE": ("Performance", 2),
        "ENSURE_TABLES_HAVE_RELATIONSHIPS": ("Maintenance", 1),
    }
    for rid, want in expected.items():
        if want is None:
            continue
        assert (by_id[rid].category, by_id[rid].severity) == want, rid


@pytest.mark.parametrize("alias, canonical", [
    ("DO_NOT_USE_FLOATING_POINT_DATA_TYPES", "AVOID_FLOATING_POINT_DATA_TYPES"),
    ("AVOID_BI_DIRECTIONAL_RELATIONSHIPS", "CHECK_IF_BI-DIRECTIONAL_AND_MANY-TO-MANY_RELATIONSHIPS_ARE_VALID"),
    ("avoid_bi-directional_relationships", "CHECK_IF_BI-DIRECTIONAL_AND_MANY-TO-MANY_RELATIONSHIPS_ARE_VALID"),
    ("PERCENTAGES_SHOULD_BE_FORMATTED_WITH_THOUSANDS_SEPARATORS", "PERCENTAGE_FORMATTING"),
    ("PROVIDE_FORMAT_STRING_FOR_VISIBLE_NUMERIC_COLUMNS", "APPLY_FORMAT_STRING_COLUMNS"),
    ("CAPITALIZE_FIRST_LETTER_OF_MEASURES", "UPPERCASE_FIRST_LETTER_MEASURES_TABLES"),
    ("CAPITALIZE_FIRST_LETTER_OF_COLUMNS", "UPPERCASE_FIRST_LETTER_COLUMNS_HIERARCHIES"),
    ("NO_SPECIAL_CHARACTERS_IN_NAMES", "SPECIAL_CHARS_IN_OBJECT_NAMES"),
    ("TRIM_OBJECT_NAMES", "OBJECTS_SHOULD_NOT_START_OR_END_WITH_A_SPACE"),
    ("MARK_AS_DATE_TABLE", "DATE/CALENDAR_TABLES_SHOULD_BE_MARKED_AS_A_DATE_TABLE"),
    ("DATE_CALENDAR_TABLES_SHOULD_BE_MARKED_AS_A_DATE_TABLE", "DATE/CALENDAR_TABLES_SHOULD_BE_MARKED_AS_A_DATE_TABLE"),
    ("UNPIVOT_PIVOTED_DATA", "UNPIVOT_PIVOTED_(MONTH)_DATA"),
    ("FILTER_COLUMN_VALUES_WITH_PROPER_SYNTAX", "FILTER_COLUMN_VALUES"),
    ("FILTER_MEASURE_VALUES_BY_COLUMNS_NOT_TABLES", "FILTER_MEASURE_VALUES_BY_COLUMNS"),
    ("USE_TREATAS_INSTEAD_OF_INTERSECT", "USE_THE_TREATAS_FUNCTION_INSTEAD_OF_INTERSECT"),
    ("AVOID_USING_1_X_Y_SYNTAX", "AVOID_USING_'1-(X/Y)'_SYNTAX"),
    ("PERF_UNUSED_COLUMNS", "REMOVE_UNUSED_COLUMNS"),
    ("PERF_UNUSED_MEASURES", "UNUSED_MEASURES"),
])
def test_rule_aliases_resolve(alias, canonical):
    assert bpa.resolve_rules([alias], bpa.builtin_rules()) == {canonical}


# --- baseline on the synthetic fixture -------------------------------------------------------------

def test_fixture_baseline(tmp_path):
    r = run(tmp_path, [])
    assert r["warnings"] == [] and r["notes"] == []
    by_rule = r["summary"]["by_rule"]
    for rid in ("HIDE_FOREIGN_KEYS", "DATA_COLUMNS_MUST_HAVE_A_SOURCE_COLUMN",
                "DAX_COLUMNS_FULLY_QUALIFIED", "NUMERIC_COLUMN_SUMMARIZE_BY",
                "AVOID_FLOATING_POINT_DATA_TYPES", "REMOVE_UNUSED_COLUMNS", "UNUSED_MEASURES"):
        assert rid in by_rule, rid
    for rid in ("MODEL_SHOULD_HAVE_A_DATE_TABLE", "USE_THE_DIVIDE_FUNCTION_FOR_DIVISION",
                "PROVIDE_FORMAT_STRING_FOR_MEASURES", "RELATIONSHIP_COLUMNS_SAME_DATA_TYPE",
                "RELATIONSHIP_COLUMNS_SHOULD_BE_OF_INTEGER_DATA_TYPE", "AVOID_DUPLICATE_MEASURES",
                "MARK_PRIMARY_KEYS", "ENSURE_TABLES_HAVE_RELATIONSHIPS"):
        assert rid not in by_rule, rid
    s = r["summary"]
    assert s["total"] == len(r["findings"]) == sum(by_rule.values())
    assert sum(s["by_category"].values()) == s["total"] == sum(s["by_severity"].values())
    assert set(s["by_category"]) >= set(bpa.CATEGORIES)
    assert r["fixable_count"] == 1
    assert r["rules_evaluated"] == len(bpa.builtin_rules())
    # ordered by severity, then category order
    sev = [f["severity"] for f in r["findings"]]
    assert sev == sorted(sev, reverse=True)


def test_no_report_layer_skips_usage_rules_with_a_note(tmp_path):
    r = run(tmp_path, [], drop_report=True)
    assert "REMOVE_UNUSED_COLUMNS" not in r["summary"]["by_rule"]
    assert "UNUSED_MEASURES" not in r["summary"]["by_rule"]
    assert any("No report layer" in n for n in r["notes"])
    quiet = run(tmp_path / "q", [], rules=["HIDE_FOREIGN_KEYS"])
    assert quiet["notes"] == []


def test_report_that_binds_nothing_skips_usage_rules(tmp_path):
    def drop_visuals(files):
        for k in [k for k in files if "/visuals/" in k]:
            del files[k]

    r = run(tmp_path, [drop_visuals])
    assert "REMOVE_UNUSED_COLUMNS" not in r["summary"]["by_rule"]
    assert "UNUSED_MEASURES" not in r["summary"]["by_rule"]
    assert any("does not reference any model field" in n for n in r["notes"])
    assert "HIDE_FOREIGN_KEYS" in r["summary"]["by_rule"]          # everything else still runs


def test_date_table_name_heuristic_ignores_lookalikes():
    f = bpa._looks_like_date_table_name
    for name in ("Date", "Dates", "Calendar", "Dim Date", "tbl_dimensiondate", "fiscalcalendar", "DateTable"):
        assert f(name), name
    for name in ("Mandates", "Gold measures_vwfactsalesmandates", "Candidates", "Updates Log",
                 "Validation", "Sales", "Consolidated"):
        assert not f(name), name


def test_missing_rule_catalog_gives_an_actionable_error(monkeypatch, tmp_path):
    monkeypatch.setattr(bpa, "_CATALOG", None)
    monkeypatch.setattr(bpa, "RESOURCE_PATH", tmp_path / "nope.json")
    with pytest.raises(RuntimeError, match="resources/bpa_rules.json"):
        bpa.builtin_rules()


def test_model_with_no_tables_does_not_crash(tmp_path):
    files = base_files()
    project = build(tmp_path, [])
    for f in (tmp_path / "Synthetic.SemanticModel" / "definition" / "tables").glob("*.tmdl"):
        f.unlink()
    (tmp_path / "Synthetic.SemanticModel" / "definition" / "relationships.tmdl").unlink()
    r = bpa.analyze(PbipProject(project))
    assert r["warnings"] == [] and files


def test_auto_date_tables_do_not_add_noise(tmp_path):
    """Desktop's hidden auto date/time tables are reported once, not rule by rule."""
    auto = (
        "table LocalDateTable_abc\n\tisHidden\n\n"
        + col("Date", "dateTime", hidden=True)
        + "".join(calc_col(n, f"YEAR([Date]) + {i}") for i, n in enumerate(
            ["Year", "MonthNo", "Month", "QuarterNo", "Quarter", "Day"]))
        + "\tpartition LocalDateTable_abc = calculated\n\t\tmode: import\n"
          "\t\tsource = CALENDAR(DATE(2020,1,1), DATE(2021,1,1))\n\n"
          "\tannotation __PBI_LocalDateTable = true\n")
    project = PbipProject(build(tmp_path, [
        add(f"{SM}/tables/LocalDateTable_abc.tmdl", auto),
        in_sales(col("ShipDate", "dateTime")),
        rel("r2", "Sales.ShipDate", "LocalDateTable_abc.Date"),
    ]))
    r = bpa.analyze(project)
    assert r["warnings"] == []
    auto_hits = [f for f in r["findings"] if "LocalDateTable_abc" in (f["table"] or "") + f["name"]]
    assert {f["rule_id"] for f in auto_hits} == {"REMOVE_AUTO-DATE_TABLE"}
    ship = {f["rule_id"] for f in r["findings"] if f["name"] == "ShipDate"}
    assert not ship & {"HIDE_FOREIGN_KEYS", "RELATIONSHIP_COLUMNS_SHOULD_BE_OF_INTEGER_DATA_TYPE",
                       "RELATIONSHIP_COLUMN_NAMES", "REMOVE_REDUNDANT_COLUMNS_IN_RELATED_TABLES"}
    assert "REDUCE_NUMBER_OF_CALCULATED_COLUMNS" not in r["summary"]["by_rule"]
    assert "MARK_PRIMARY_KEYS" not in r["summary"]["by_rule"]
    assert "REMOVE_UNUSED_COLUMNS" in r["summary"]["by_rule"]         # user columns still count


# --- filters ---------------------------------------------------------------------------------------------

def test_filters(tmp_path):
    project = PbipProject(build(tmp_path, []))
    everything = bpa.analyze(project)
    sev2 = bpa.analyze(project, severity_min=2)
    assert sev2["findings"] and all(f["severity"] >= 2 for f in sev2["findings"])
    assert sev2["summary"]["total"] < everything["summary"]["total"]
    only3 = bpa.analyze(project, severity_min=3)
    assert {f["severity"] for f in only3["findings"]} == {3}

    fmt = bpa.analyze(project, categories=["formatting"])
    assert {f["category"] for f in fmt["findings"]} == {"Formatting"}
    two = bpa.analyze(project, categories=["Formatting", "Error Prevention"])
    assert {f["category"] for f in two["findings"]} == {"Formatting", "Error Prevention"}
    assert bpa.analyze(project, categories="Maintenance")["summary"]["by_category"]["Formatting"] == 0

    date = bpa.analyze(project, table="Date")
    own = {(f["rule_id"], f["name"]) for f in everything["findings"] if f["table"] == "Date"}
    got = {(f["rule_id"], f["name"]) for f in date["findings"]}
    assert own and own <= got
    assert "AVOID_FLOATING_POINT_DATA_TYPES" not in {r for r, _ in got}      # Sales-only
    assert ("HIDE_FOREIGN_KEYS", "OrderDate") in got                          # via the relationship
    assert date["summary"]["total"] < everything["summary"]["total"]
    rel = bpa.analyze(project, table="Date", rules=["RELATIONSHIP_COLUMN_NAMES"])
    assert len(rel["findings"]) == 1                     # found through the relationship's other end

    picked = bpa.analyze(project, rules=["DO_NOT_USE_FLOATING_POINT_DATA_TYPES", "hide_foreign_keys"])
    assert set(picked["summary"]["by_rule"]) == {"AVOID_FLOATING_POINT_DATA_TYPES", "HIDE_FOREIGN_KEYS"}
    skipped = bpa.analyze(project, exclude_rules=["OBJECTS_WITH_NO_DESCRIPTION", "UNUSED_MEASURES"])
    assert "OBJECTS_WITH_NO_DESCRIPTION" not in skipped["summary"]["by_rule"]
    assert skipped["rules_evaluated"] == everything["rules_evaluated"] - 2
    assert skipped["summary"]["total"] < everything["summary"]["total"]

    few = bpa.analyze(project, max_findings=3)
    assert len(few["findings"]) == 3 and few["truncated"] is True
    assert few["summary"]["total"] == everything["summary"]["total"]
    assert everything["truncated"] is False


def test_bad_filter_arguments_are_actionable(tmp_path):
    project = PbipProject(build(tmp_path, []))
    with pytest.raises(ValueError, match="Unknown category.*Valid categories"):
        bpa.analyze(project, categories=["Perf"])
    with pytest.raises(ValueError, match=r"did you mean HIDE_FOREIGN_KEYS"):
        bpa.analyze(project, rules=["HIDE_FOREIGN_KEY"])
    with pytest.raises(ValueError, match="Unknown exclude_rules rule"):
        bpa.analyze(project, exclude_rules=["NOPE_NOPE"])
    with pytest.raises(ValueError, match="severity_min"):
        bpa.analyze(project, severity_min=0)
    with pytest.raises(ValueError, match="severity_min"):
        bpa.analyze(project, severity_min="high")
    with pytest.raises(ValueError, match="Table 'Nope' not found"):
        bpa.analyze(project, table="Nope")


# --- node parser ---------------------------------------------------------------------------------------------

def test_tmdl_node_parser():
    text = (
        "/// Sales table\n"
        "table Sales\n"
        "\tisHidden\n"
        "\tdataCategory: Time\n"
        "\n"
        "\t/// Amount after tax\n"
        "\t/// second line\n"
        "\tmeasure 'A = B' =\n"
        "\t\t\tVAR x = 1\n"
        "\t\t\tRETURN x\n"
        "\t\tformatString: 0\n"
        "\t\tformatStringDefinition = SELECTEDMEASUREFORMATSTRING()\n"
        "\n"
        "\tmeasure Fenced = ```\n"
        "\t\t\tSUM(x)\n"
        "\t\t\t```\n"
        "\t\tisHidden\n"
        "\n"
        "\tcolumn Calc = Sales[a] + 1\n"
        "\t\tdataType: int64\n"
        "\t\tannotation Note = hi\n"
        "\n"
        "\tpartition P = m\n"
        "\t\tmode: import\n"
        "\t\tsource =\n"
        "\t\t\t\tlet\n"
        "\t\t\t\t\tS = 1\n"
        "\t\t\t\tin S\n"
    )
    (t,) = bpa.parse_nodes(text)
    assert t.name == "Sales" and t.flag("isHidden") and t.prop("dataCategory") == "Time"
    assert t.doc == ["Sales table"] and t.doc_start == 0 and t.start == 1
    m, f, c, p = t.children
    assert m.name == "A = B" and m.raw_name == "'A = B'" and m.expr == "VAR x = 1\nRETURN x"
    assert m.doc == ["Amount after tax", "second line"] and m.doc_start == 5
    assert m.props["formatString"].value == "0" and m.props["formatString"].line == 10
    assert m.props["formatStringDefinition"].is_expr
    assert m.body_end == 10 and not m.fenced
    assert f.fenced and f.expr == "SUM(x)" and f.flag("isHidden") and f.body_end == 16
    assert c.is_calculated and c.expr == "Sales[a] + 1" and c.kid("annotation").name == "Note"
    assert p.kw == "partition" and p.inline == "m" and p.props["mode"].value == "import"
    assert p.props["source"].value == "let\n\tS = 1\nin S"
    assert c.props["dataType"].end == c.props["dataType"].line + 1


@pytest.mark.parametrize("ref, want", [
    ("Sales.OrderDate", ("Sales", "OrderDate")),
    ("'Sales Table'.'Order Date'", ("Sales Table", "Order Date")),
    ("Sales.'Order Date'", ("Sales", "Order Date")),
    ("'It''s'.Col", ("It's", "Col")),
    ("'a.b'.c", ("a.b", "c")),
])
def test_relationship_endpoint_parsing(ref, want):
    assert bpa._split_ref(ref) == want


def test_relationship_properties_are_read_correctly(tmp_path):
    """isActive: false, cardinalities and cross-filter direction (core.tmdl misses isActive)."""
    project = PbipProject(build(tmp_path, [
        table_file("Extra", "\n" + col("Key", "int64") + col("Other", "int64")),
        rel("inactive", "Extra.Key", "Date.Year", "isActive: false"),
        rel("onetoone", "Extra.Other", "Date.Year", "fromCardinality: one\ntoCardinality: one"),
    ]))
    model = bpa.load_model(project, with_usage=False)
    by = {r.name: r for r in model.relationships}
    assert by["inactive"].is_active is False and by["f1a2b3c4-0000-0000-0000-000000000001"].is_active
    assert (by["onetoone"].from_card, by["onetoone"].to_card) == ("one", "one")
    assert by["inactive"].from_card == "many" and by["inactive"].to_card == "one"
    assert by["inactive"].cross_filter == "onedirection"
    fk = bpa.analyze(project, rules=["HIDE_FOREIGN_KEYS"])
    assert ("Extra", "Key") in keys(fk) and ("Extra", "Other") not in keys(fk)   # 1:1 is not a foreign key


def test_excessive_bidirectional_threshold(tmp_path):
    bidi = "crossFilteringBehavior: bothDirections"
    three = [table_file("A", "\n" + col("K", "int64")), table_file("B", "\n" + col("K", "int64")),
             rel("r2", "A.K", "Date.Year", bidi), rel("r3", "B.K", "Date.Year")]
    at_33 = run(tmp_path / "a", three, rules=["AVOID_EXCESSIVE_BI-DIRECTIONAL_OR_MANY-TO-MANY_RELATIONSHIPS"])
    assert keys(at_33) == {(None, "Model")}                        # 1 of 3 > 30%
    four = three + [table_file("C", "\n" + col("K", "int64")), rel("r4", "C.K", "Date.Year")]
    at_25 = run(tmp_path / "b", four, rules=["AVOID_EXCESSIVE_BI-DIRECTIONAL_OR_MANY-TO-MANY_RELATIONSHIPS"])
    assert at_25["findings"] == []                                # 1 of 4


def test_measure_only_tables_and_calc_groups_are_exempt_from_relationship_rule(tmp_path):
    r = run(tmp_path, [
        table_file("_Measures", measure("Total", "SUM(Sales[Amount])", "#,0")
                   + "\n" + col("Dummy", "string", hidden=True)),
        add(f"{SM}/tables/CG.tmdl", emit_calc_group_table("CG", 1, _CG)),
    ], rules=["ENSURE_TABLES_HAVE_RELATIONSHIPS"])
    assert r["findings"] == []


def test_nested_calc_group_and_roles_parse():
    text = emit_calc_group_table("CG", 7, [{"name": "YTD", "dax": "TOTALYTD(SELECTEDMEASURE(), Date[Date])"},
                                           {"name": "Multi", "dax": "VAR x = 1\nRETURN x"}])
    (t,) = bpa.parse_nodes(text)
    group = t.kid("calculationGroup")
    assert group.prop("precedence") == "7"
    items = group.kids("calculationItem")
    assert [i.name for i in items] == ["YTD", "Multi"]
    assert items[1].expr == "VAR x = 1\nRETURN x"


# --- type inference / default format ------------------------------------------------------------------------------

def _kinds(tmp_path, measures: dict[str, str]) -> dict[str, str]:
    project = PbipProject(build(tmp_path, [add_measure(n, d, fmt="") for n, d in measures.items()]))
    model = bpa.load_model(project, with_usage=False)
    return {m.name: bpa.measure_result_kind(model, m) for m in model.measures()}


def test_measure_result_kind(tmp_path):
    kinds = _kinds(tmp_path, {
        "T Text": '"hello"', "T Concat": "[Net Revenue] & \" units\"", "T Format": 'FORMAT([Net Revenue], "0")',
        "T Bool": "[Net Revenue] > 10", "T Date": "TODAY()", "T DateMax": "MAX(Date[Date])",
        "T Count": "COUNTROWS(Sales)", "T Ratio": "DIVIDE([Net Revenue], 2)", "T Var": "VAR a = 1 RETURN a + [Net Revenue]",
        "T If": 'IF([Net Revenue] > 1, 1, BLANK())', "T IfMixed": 'IF([Net Revenue] > 1, 1, "x")',
        "T Switch": 'SWITCH(TRUE(), [Net Revenue] > 1, 1, 0)', "T Alias": "[Net Revenue]",
        "T Calc": "CALCULATE([Net Revenue], ALL(Date))", "T Col": "MAX(Sales[Amount])",
        "T Unknown": "SELECTEDMEASURE()", "T Neg": "-[Net Revenue]",
    })
    want = {"T Text": "text", "T Concat": "text", "T Format": "text", "T Bool": "bool", "T Date": "date",
            "T DateMax": "date", "T Count": "numeric", "T Ratio": "numeric", "T Var": "numeric",
            "T If": "numeric", "T IfMixed": "unknown", "T Switch": "numeric", "T Alias": "numeric",
            "T Calc": "numeric", "T Col": "numeric", "T Unknown": "unknown", "T Neg": "numeric"}
    for name, kind in want.items():
        assert kinds[name] == kind, (name, kinds[name])


@pytest.mark.parametrize("name, fmt", [
    ("Margin %", "0.0%"), ("Win pct", "0.0%"), ("Conversion Percent", "0.0%"),
    ("Cost Ratio", "0.0%"), ("Total Sales", "#,0"),
])
def test_default_format_string(name, fmt):
    assert bpa.default_format_string(name) == fmt


# --- fixers -----------------------------------------------------------------------------------------------------------

def added_lines(before: str, after: str) -> tuple[list[str], list[str]]:
    plus, minus = [], []
    for line in difflib.unified_diff(before.split("\n"), after.split("\n"), lineterm="", n=0):
        if line.startswith("+") and not line.startswith("+++"):
            plus.append(line[1:])
        elif line.startswith("-") and not line.startswith("---"):
            minus.append(line[1:])
    return plus, minus


def read(p: Path) -> str:
    return p.read_bytes().decode("utf-8").replace("\r\n", "\n")


def test_fixers_are_surgical_and_idempotent(tmp_path):
    no_format = [
        sub(SALES, "formatString: #,0\n\t\tdisplayFolder: KPIs\n\n\tmeasure 'Margin", "displayFolder: KPIs\n\n\tmeasure 'Margin"),
        sub(SALES, "\t\tformatString: 0.0%\n", ""),
        sub(SALES, "\t\tformatString: #,0\n\n\tmeasure 'Fenced", "\n\tmeasure 'Fenced"),
        sub(SALES, "```\n\t\tformatString: #,0\n", "```\n"),
        add_measure("Note Text", '"hello"', fmt=""),
        add_measure("Dynamic", "[Net Revenue]", fmt="", ) if False else add_measure("Dynamic2", "[Net Revenue] + 1", fmt="0"),
        in_sales(col("Latitude", "double") + col("Longitude", "decimal") + col("WebUrl", "string")
                 + col("ImageUrl", "string") + col("Country", "string") + col("Lat", "double")),
    ]
    root = tmp_path
    proj_path = build(root, no_format)
    sales = root / "Synthetic.SemanticModel" / "definition" / "tables" / "Sales.tmdl"
    before = read(sales)
    others_before = {k: v for k, v in snapshot(root).items() if not k.endswith("Sales.tmdl")}

    project = PbipProject(proj_path)
    pre = bpa.analyze(project, rules=["PROVIDE_FORMAT_STRING_FOR_MEASURES"])
    fixable = {f["name"]: f["fixable"] for f in pre["findings"]}
    assert fixable == {"Net Revenue": True, "Margin %": True, "Complex Measure": True,
                       "Fenced Measure": True, "Note Text": False}

    res = bpa.apply_fixes(project)
    assert res["count"] == len(res["changed"]) and res["files"] == [
        "Synthetic.SemanticModel/definition/tables/Sales.tmdl"]
    changes = {(c["rule_id"], c["name"]): c["change"] for c in res["changed"]}
    assert changes[("PROVIDE_FORMAT_STRING_FOR_MEASURES", "Net Revenue")] == "formatString: #,0"
    assert changes[("PROVIDE_FORMAT_STRING_FOR_MEASURES", "Margin %")] == "formatString: 0.0%"
    assert ("PROVIDE_FORMAT_STRING_FOR_MEASURES", "Note Text") not in changes
    assert changes[("HIDE_FOREIGN_KEYS", "OrderDate")] == "isHidden"
    for name, cat in (("Latitude", "Latitude"), ("Longitude", "Longitude"),
                      ("WebUrl", "WebUrl"), ("ImageUrl", "ImageUrl")):
        assert changes[("ADD_DATA_CATEGORY_FOR_COLUMNS", name)] == f"dataCategory: {cat}"
    assert not any(k[1] in ("Country", "Lat") for k in changes)        # only clear implications

    after = read(sales)
    plus, minus = added_lines(before, after)
    assert minus == [], "fixers must only add lines"
    assert len(plus) == res["count"]
    assert sorted(plus) == sorted(
        ["\t\tformatString: #,0"] * 3 + ["\t\tformatString: 0.0%", "\t\tisHidden"]
        + ["\t\tdataCategory: Latitude", "\t\tdataCategory: Longitude",
           "\t\tdataCategory: WebUrl", "\t\tdataCategory: ImageUrl"])
    # every other file is byte-identical
    assert {k: v for k, v in snapshot(root).items() if not k.endswith("Sales.tmdl")} == others_before

    # placement: the format string sits right after the expression, before other properties
    assert "measure 'Net Revenue' = SUM(Sales[Amount])\n\t\tformatString: #,0\n\t\tdisplayFolder: KPIs" in after
    assert "```\n\t\tformatString: #,0\n" in after            # after the closing fence
    assert "\t\t\t\tDIVIDE(_rev, [Order Count])\n\t\tformatString: #,0\n" in after
    assert "column Latitude\n\t\tdataType: double\n\t\tdataCategory: Latitude\n" in after

    # the project still loads and re-analysis no longer reports them
    again = bpa.analyze(PbipProject(proj_path), rules=["HIDE_FOREIGN_KEYS", "ADD_DATA_CATEGORY_FOR_COLUMNS",
                                                        "PROVIDE_FORMAT_STRING_FOR_MEASURES"])
    assert {(f["rule_id"], f["name"]) for f in again["findings"]} == {
        ("ADD_DATA_CATEGORY_FOR_COLUMNS", "Country"),      # geographic but ambiguous: report only
        ("PROVIDE_FORMAT_STRING_FOR_MEASURES", "Note Text")}
    assert again["fixable_count"] == 0

    # idempotent
    snap = snapshot(root)
    second = bpa.apply_fixes(PbipProject(proj_path))
    assert second["count"] == 0 and second["changed"] == [] and second["files"] == []
    assert snapshot(root) == snap


def test_fixer_scoping(tmp_path):
    project = PbipProject(build(tmp_path, [
        in_sales(col("Latitude", "double")),
        table_file("Other", "\n" + col("Key", "int64")),
        rel("r2", "Other.Key", "Date.Year"),
    ]))
    with pytest.raises(ValueError, match="No automatic fixer for: AVOID_FLOATING_POINT_DATA_TYPES"):
        bpa.apply_fixes(project, rules=["AVOID_FLOATING_POINT_DATA_TYPES"])
    with pytest.raises(ValueError, match="Unknown rule"):
        bpa.apply_fixes(project, rules=["NOPE"])
    with pytest.raises(ValueError, match="Table 'Nope' not found"):
        bpa.apply_fixes(project, table="Nope")
    only_other = bpa.apply_fixes(project, table="Other")
    assert [(c["table"], c["name"]) for c in only_other["changed"]] == [("Other", "Key")]
    only_cat = bpa.apply_fixes(project, rules=["add_data_category_for_columns"])
    assert [c["rule_id"] for c in only_cat["changed"]] == ["ADD_DATA_CATEGORY_FOR_COLUMNS"]
    rest = bpa.apply_fixes(project)
    assert {c["rule_id"] for c in rest["changed"]} == {"HIDE_FOREIGN_KEYS"}
    assert [c["name"] for c in rest["changed"]] == ["OrderDate"]


def test_fixers_keep_crlf_and_bom(tmp_path):
    proj_path = build(tmp_path, [], crlf=True)
    sales = tmp_path / "Synthetic.SemanticModel" / "definition" / "tables" / "Sales.tmdl"
    sales.write_bytes(b"\xef\xbb\xbf" + sales.read_bytes())
    before = sales.read_bytes()
    bpa.apply_fixes(PbipProject(proj_path))
    after = sales.read_bytes()
    assert after.startswith(b"\xef\xbb\xbf") and after != before
    body = after[3:]
    assert body.count(b"\r\n") == body.count(b"\n") == before[3:].count(b"\r\n") + 1
    assert b"\t\tisHidden\r\n" in body


def test_fixer_does_not_touch_measures_with_a_dynamic_format(tmp_path):
    proj_path = build(tmp_path, [
        insert_before(SALES, "\tcolumn Amount\n",
                      "\tmeasure Dynamic = [Net Revenue]\n\t\tformatStringDefinition = SELECTEDMEASUREFORMATSTRING()\n\n")])
    assert not any(f["name"] == "Dynamic" for f in bpa.analyze(
        PbipProject(proj_path), rules=["PROVIDE_FORMAT_STRING_FOR_MEASURES"])["findings"])


# --- custom rules ---------------------------------------------------------------------------------------------------

def write_custom(tmp_path: Path, content) -> None:
    d = tmp_path / ".pbi-mcp"
    d.mkdir(exist_ok=True)
    text = content if isinstance(content, str) else json.dumps(content)
    (d / "bpa_rules.json").write_text(text, encoding="utf-8")


def test_custom_rules(tmp_path):
    project = PbipProject(build(tmp_path, [add_measure("tmp_scratch", "1"), add_measure("Keep", "RAND()")]))
    write_custom(tmp_path, [
        {"id": "NO_TMP_MEASURES", "severity": 3, "category": "Naming Conventions", "scope": "measure",
         "name_regex": "^tmp_", "message": "Remove temporary measure {name} from {table}"},
        {"id": "NO_RAND", "scope": "measure", "expression_regex": r"\bRAND\(", "message": "RAND is volatile"},
        {"id": "AMOUNT_COLUMNS", "scope": "column", "name_regex": "Amount|Cost", "severity": 1},
        {"id": "CSV_SOURCES", "scope": "table", "expression_regex": r"Csv\.Document", "message": "CSV source"},
        {"id": "BOTH", "scope": "measure", "name_regex": "^Keep$", "expression_regex": "NEVER_MATCHES"},
    ])
    r = bpa.analyze(project)
    assert r["warnings"] == []
    got = {(f["rule_id"], f["table"], f["name"]) for f in r["findings"]
           if f["rule_id"] in ("NO_TMP_MEASURES", "NO_RAND", "AMOUNT_COLUMNS", "CSV_SOURCES", "BOTH")}
    assert got == {("NO_TMP_MEASURES", "Sales", "tmp_scratch"), ("NO_RAND", "Sales", "Keep"),
                   ("AMOUNT_COLUMNS", "Sales", "Amount"), ("AMOUNT_COLUMNS", "Sales", "Cost"),
                   ("CSV_SOURCES", "Sales", "Sales")}
    tmp = next(f for f in r["findings"] if f["rule_id"] == "NO_TMP_MEASURES")
    assert (tmp["severity"], tmp["category"], tmp["fixable"]) == (3, "Naming Conventions", False)
    assert tmp["message"] == "Remove temporary measure tmp_scratch from Sales"
    assert next(f for f in r["findings"] if f["rule_id"] == "NO_RAND")["category"] == "Custom"
    assert next(f for f in r["findings"] if f["rule_id"] == "NO_RAND")["severity"] == 2
    # selectable like any other rule, alone or by category, and listed with the catalog
    only = bpa.analyze(project, rules=["no_tmp_measures"])
    assert [f["name"] for f in only["findings"]] == ["tmp_scratch"]
    assert {f["rule_id"] for f in bpa.analyze(project, categories=["custom"])["findings"]} >= {"NO_RAND"}
    assert bpa.analyze(project, severity_min=3, rules=["AMOUNT_COLUMNS"])["findings"] == []
    listed = {x["id"]: x for x in bpa.list_rules(project)}
    assert listed["NO_TMP_MEASURES"]["origin"] == "custom" and not listed["NO_TMP_MEASURES"]["fixable"]
    assert len(listed) == len(bpa.builtin_rules()) + 5
    assert "NO_TMP_MEASURES" not in {x["id"] for x in bpa.list_rules()}    # no project -> catalog only


def test_custom_rules_wrapper_object_and_bad_entries(tmp_path):
    project = PbipProject(build(tmp_path, []))
    write_custom(tmp_path, {"rules": [
        {"id": "OK_RULE", "scope": "measure", "name_regex": "Net"},
        {"id": "HIDE_FOREIGN_KEYS", "scope": "column", "name_regex": "x"},        # collides with built-in
        {"id": "hide_foreign_keys", "scope": "column", "name_regex": "x"},         # normalised collision
        {"scope": "measure", "name_regex": "x"},                                   # no id
        {"id": "BAD_SCOPE", "scope": "partition", "name_regex": "x"},
        {"id": "BAD_SEVERITY", "scope": "measure", "name_regex": "x", "severity": 9},
        {"id": "BAD_REGEX", "scope": "measure", "name_regex": "("},
        {"id": "NO_PATTERN", "scope": "measure"},
        {"id": "OK_RULE", "scope": "measure", "name_regex": "dup"},               # duplicate custom id
        "not an object",
    ]})
    r = bpa.analyze(project)
    assert [f["name"] for f in r["findings"] if f["rule_id"] == "OK_RULE"] == ["Net Revenue"]
    text = "\n".join(r["warnings"])
    for needle in ("duplicates an existing rule id", "missing string 'id'", "'scope' must be one of",
                   "'severity' must be an integer", "not a valid regular expression",
                   "at least one of", "each rule must be a JSON object"):
        assert needle in text, needle
    assert len(r["warnings"]) == 9


def test_malformed_custom_rules_file_never_blocks_the_builtin_rules(tmp_path):
    project = PbipProject(build(tmp_path, []))
    write_custom(tmp_path, "{ this is not json")
    r = bpa.analyze(project, rules=["HIDE_FOREIGN_KEYS"])
    assert len(r["findings"]) == 1 and "cannot read custom rules" in r["warnings"][0]
    write_custom(tmp_path, '{"unexpected": 1}')
    r = bpa.analyze(project, rules=["HIDE_FOREIGN_KEYS"])
    assert len(r["findings"]) == 1 and "expected a JSON list" in r["warnings"][0]


def test_a_crashing_check_is_reported_not_fatal(tmp_path, monkeypatch):
    project = PbipProject(build(tmp_path, []))

    def boom(ctx):
        raise RuntimeError("kaput")

    monkeypatch.setitem(bpa._CHECKS, "HIDE_FOREIGN_KEYS", boom)
    monkeypatch.setattr(bpa, "_CATALOG", None)
    try:
        r = bpa.analyze(project)
    finally:
        monkeypatch.undo()
        bpa._CATALOG = None
    assert any("HIDE_FOREIGN_KEYS failed: RuntimeError: kaput" in w for w in r["warnings"])
    assert r["findings"]                                              # the other rules still ran


# --- tools -------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def state(tmp_path):
    st = ModelState()
    set_project(st, str(build(tmp_path, [])))
    return st


def test_tool_functions(state, tmp_path):
    with pytest.raises(ValueError, match="No project"):
        tools_bpa.run_bpa(ModelState())
    r = tools_bpa.run_bpa(state, categories=["Formatting"], severity_min=2, table="Sales")
    assert r["findings"] and {f["category"] for f in r["findings"]} == {"Formatting"}
    assert json.loads(json.dumps(r)) == r
    listing = tools_bpa.list_bpa_rules(ModelState())               # no project needed
    assert listing["count"] == len(bpa.builtin_rules()) >= 30
    assert listing["categories"] == list(bpa.CATEGORIES)
    assert all(x["description"] and x["category"] and x["severity"] for x in listing["rules"])
    assert {x["id"] for x in listing["rules"] if x["fixable"]} == {
        "PROVIDE_FORMAT_STRING_FOR_MEASURES", "HIDE_FOREIGN_KEYS", "ADD_DATA_CATEGORY_FOR_COLUMNS"}
    fixed = tools_bpa.run_bpa_fix(state)
    assert fixed["count"] == 1
    assert tools_bpa.run_bpa_fix(state)["count"] == 0


def test_tools_are_registered_with_the_right_annotations():
    import model_server.server as srv

    tools = {t.name: t for t in asyncio.run(srv.mcp.list_tools())}

    def hint(tool, name):
        a = tool.annotations
        snake = {"readOnlyHint": "read_only_hint", "destructiveHint": "destructive_hint",
                 "idempotentHint": "idempotent_hint"}[name]
        return getattr(a, name) if hasattr(a, name) else getattr(a, snake)

    def props(tool):
        return (getattr(tool, "inputSchema", None) or tool.input_schema)["properties"]

    for name in ("pbi_bpa", "pbi_bpa_rules"):
        assert hint(tools[name], "readOnlyHint") is True and "dry_run" not in props(tools[name])
    assert set(props(tools["pbi_bpa"])) >= {"categories", "severity_min", "table", "rules", "exclude_rules"}
    fix = tools["pbi_bpa_fix"]
    assert hint(fix, "readOnlyHint") is False and hint(fix, "destructiveHint") is False
    assert hint(fix, "idempotentHint") is True
    assert "dry_run" in props(fix) and set(props(fix)) >= {"rules", "table"}
    # names are unique across both servers
    import report_server.server as rep
    other = {t.name for t in asyncio.run(rep.mcp.list_tools())}
    mine = {n for n in tools if n.startswith(("pbi_bpa", "pbi_format", "pbi_dax_ref"))}
    assert len(mine) == 6 and not (mine & other)


def test_bpa_fix_dry_run_write_and_undo_through_the_server(tmp_path):
    import model_server.server as srv

    proj = tmp_path / "p"
    shutil.copytree(SYNTH, proj)

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
    report = call("pbi_bpa", categories=["Formatting"])
    assert report["summary"]["total"] > 0 and snapshot(proj) == before

    preview = call("pbi_bpa_fix", dry_run=True)
    assert preview["dry_run"] is True and "+\t\tisHidden" in preview["diff"]
    assert preview["result"]["count"] == 1 and snapshot(proj) == before

    real = call("pbi_bpa_fix")
    assert real["count"] == 1 and snapshot(proj) != before
    assert call("pbi_bpa_fix")["count"] == 0
    undo = call("pbi_undo")
    assert undo["undone"][0]["tool"] == "pbi_bpa_fix"
    assert snapshot(proj) == before
    rules = call("pbi_bpa_rules")
    assert rules["count"] >= 30
