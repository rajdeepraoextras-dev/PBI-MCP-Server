"""core/refactor: cascading renames across TMDL and PBIR."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from core import journal
from core.journal import snapshot
from core.refactor import Renamer, _rewrite_dax_regions, find_references
from core.usage import collect_direct_refs
from model_server.server import ModelState, set_project

SYNTH = Path(__file__).parent / "fixtures" / "synthetic"


@pytest.fixture
def proj(tmp_path):
    dst = tmp_path / "p"
    shutil.copytree(SYNTH, dst)
    state = ModelState()
    set_project(state, str(dst / "Synthetic.pbip"))
    return dst, state


def _tables_dir(root: Path) -> Path:
    return root / "Synthetic.SemanticModel" / "definition" / "tables"


def _all_report_text(root: Path) -> str:
    out = []
    for f in sorted((root / "Synthetic.Report" / "definition").rglob("*.json")):
        out.append(f.read_text(encoding="utf-8-sig"))
    return "\n".join(out)


def test_rewrite_dax_regions_touches_only_dax():
    text = ("table A\n\tmeasure X = [A]\n\t\tformatString: A\n\n"
            "\tmeasure Y =\n\t\t\tVAR v = [A]\n\t\t\tRETURN v\n\t\tdisplayFolder: A\n\n"
            "\tcolumn A\n\t\tsourceColumn: A\n\n\tcolumn C = [A] * 2\n\n"
            "\tpartition A = m\n\t\tmode: import\n\t\tsource =\n\t\t\t\tlet x = [A] in x\n\n"
            "\tpartition Z = calculated\n\t\tsource = [A] + 1\n")
    out = _rewrite_dax_regions(text, lambda s: s.replace("[A]", "[B]"))
    assert "measure X = [B]" in out and "formatString: A" in out
    assert "VAR v = [B]" in out and "displayFolder: A" in out
    assert "column A\n\t\tsourceColumn: A" in out              # header + property untouched
    assert "column C = [B] * 2" in out
    assert "let x = [A] in x" in out                           # M untouched
    assert "source = [B] + 1" in out                           # calculated partition is DAX
    assert out.count("\n") == text.count("\n")


def test_rename_measure_cascades(proj):
    root, state = proj
    before_refs = collect_direct_refs(state.project)
    res = Renamer(state.project).rename_measure("Sales", "Net Revenue", "Revenue Net")
    assert res["ok"] and res["kind"] == "measure"
    sales = (_tables_dir(root) / "Sales.tmdl").read_text(encoding="utf-8")
    assert "measure 'Revenue Net' = SUM(Sales[Amount])" in sales
    assert "[Net Revenue]" not in sales and "[Revenue Net]" in sales   # DAX refs
    assert 'Csv.Document(File.Contents("sales.csv"))' in sales          # M untouched
    names = {m.name for m in state.project.list_measures()}
    assert "Revenue Net" in names and "Net Revenue" not in names
    # lineage still resolves through the renamed measure
    lin = state.project.model_lineage("Margin %")
    assert "Revenue Net" in lin["depends_on_measures"]
    # report bindings followed
    text = _all_report_text(root)
    assert "Net Revenue" not in text and '"Sales.Revenue Net"' in text
    after_refs = collect_direct_refs(state.project)
    assert ("Sales", "Revenue Net") in after_refs and ("Sales", "Net Revenue") not in after_refs
    assert len(after_refs) == len(before_refs)
    assert any(f.endswith("visual.json") for f in res["report_files"])


def test_rename_column_cascades_including_relationships_and_own_table_bare_refs(proj):
    root, state = proj
    sales_path = _tables_dir(root) / "Sales.tmdl"
    text = sales_path.read_text(encoding="utf-8")
    # add a calculated column using a bare [OrderDate] and an M step touching [OrderDate]
    text = text.replace(
        "\tpartition Sales = m",
        "\tcolumn 'Order Year' = YEAR([OrderDate])\n\t\tdataType: int64\n\n\tpartition Sales = m")
    text = text.replace("\t\t\t\t\tSource\n", "\t\t\t\t\tTable.AddColumn(Source, \"x\", each [OrderDate])\n")
    sales_path.write_text(text, encoding="utf-8")

    res = Renamer(state.project).rename_column("Sales", "OrderDate", "Order Date")
    sales = sales_path.read_text(encoding="utf-8")
    assert "\tcolumn 'Order Date'\n" in sales
    assert "YEAR([Order Date])" in sales                     # bare ref in own table
    assert 'each [OrderDate]' in sales                       # M untouched
    rel = (root / "Synthetic.SemanticModel" / "definition" / "relationships.tmdl").read_text()
    assert "fromColumn: Sales.'Order Date'" in rel
    assert "Order Date" in [c.name for t in state.project.list_tables()
                            if t.name == "Sales" for c in t.columns]
    assert res["kind"] == "column"


def test_rename_table_cascades_and_renames_file(proj):
    root, state = proj
    res = Renamer(state.project).rename_table("Date", "Calendar")
    tdir = _tables_dir(root)
    assert not (tdir / "Date.tmdl").exists() and (tdir / "Calendar.tmdl").exists()
    cal = (tdir / "Calendar.tmdl").read_text(encoding="utf-8")
    assert cal.startswith("table Calendar\n")
    assert "partition Calendar = calculated" in cal
    assert "COUNTROWS(Calendar)" in cal
    assert "DATE(2020,1,1)" in cal                       # function named like the table untouched
    sales = (tdir / "Sales.tmdl").read_text(encoding="utf-8")
    assert "ALL(Calendar)" in sales and "ALL(Date)" not in sales
    model = (root / "Synthetic.SemanticModel" / "definition" / "model.tmdl").read_text()
    assert "ref table Calendar" in model and "ref table Date" not in model
    rel = (root / "Synthetic.SemanticModel" / "definition" / "relationships.tmdl").read_text()
    assert "toColumn: Calendar.Date" in rel
    text = _all_report_text(root)
    assert '"Entity": "Date"' not in text and '"Entity": "Calendar"' in text
    assert '"Calendar.Year"' in text and '"Date.Year"' not in text
    assert {t.name for t in state.project.list_tables()} == {"Sales", "Calendar"}
    assert res["kind"] == "table" and any("Calendar.tmdl" in f for f in res["model_files"])


def test_rename_table_needing_quotes(proj):
    root, state = proj
    Renamer(state.project).rename_table("Sales", "Sales Facts")
    tdir = _tables_dir(root)
    assert (tdir / "Sales Facts.tmdl").exists()
    text = (tdir / "Sales Facts.tmdl").read_text(encoding="utf-8")
    assert text.startswith("table 'Sales Facts'\n")
    assert "SUM('Sales Facts'[Amount])" in text
    rel = (root / "Synthetic.SemanticModel" / "definition" / "relationships.tmdl").read_text()
    assert "fromColumn: 'Sales Facts'.OrderDate" in rel
    assert "'Sales Facts.Net Revenue'" not in _all_report_text(root)
    assert '"Sales Facts.Net Revenue"' in _all_report_text(root)


@pytest.mark.parametrize("call, msg", [
    (("rename_measure", "Sales", "Nope", "X"), "not found"),
    (("rename_measure", "Sales", "Net Revenue", "Margin %"), "already used"),
    (("rename_measure", "Sales", "Net Revenue", "net revenue"), "identical"),
    (("rename_column", "Sales", "Amount", "Cost"), "already exists"),
    (("rename_column", "Nope", "Amount", "X"), "not found"),
    (("rename_table", "Date", "Sales"), "already exists"),
    (("rename_table", "Nope", "X"), "not found"),
])
def test_rename_validation(proj, call, msg):
    _, state = proj
    method, *args = call
    with pytest.raises(ValueError, match=msg):
        getattr(Renamer(state.project), method)(*args)


def test_rename_is_fully_undoable_through_the_journal(proj):
    root, state = proj
    before = snapshot(root)
    j = journal.for_state(state)
    j.record("pbi_rename_table", lambda: Renamer(state.project).rename_table("Date", "Calendar"))
    assert snapshot(root) != before
    j.undo()
    assert snapshot(root) == before


def test_find_references(proj):
    _, state = proj
    refs = find_references(state.project, "measure", None, "Net Revenue")
    assert {"table": "Sales", "measure": "Margin %"} in refs["dax"]
    assert "Sales.Net Revenue" in refs["report_fields"]
    trefs = find_references(state.project, "table", None, "Date")
    assert "Date.Year" in trefs["report_fields"]
    assert any(h["measure"] == "Days In Period" for h in trefs["dax"])


def test_tools_registered():
    import asyncio
    from model_server.server import mcp
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert {"pbi_rename_measure", "pbi_rename_column", "pbi_rename_table",
            "pbi_find_references"} <= names
