"""Roles (RLS/OLS), perspectives and translations: TMDL text + model tools."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from core import tmdl_security as ts
from core.journal import delta, snapshot
from core.pbip import PbipProject
from model_server.server import ModelState, set_project
import model_server.tools_security as sec

SYNTH = Path(__file__).parent / "fixtures" / "synthetic"

HIERARCHY_BLOCK = (
    "\n\thierarchy 'Date Hierarchy'\n"
    "\t\tlevel Year\n"
    "\t\t\tcolumn: Year\n"
)


@pytest.fixture
def proj(tmp_path) -> Path:
    dst = tmp_path / "synthetic"
    shutil.copytree(SYNTH, dst)
    return dst


@pytest.fixture
def state(proj) -> ModelState:
    st = ModelState()
    set_project(st, str(proj / "Synthetic.pbip"))
    return st


def defn(proj: Path) -> Path:
    return proj / "Synthetic.SemanticModel" / "definition"


def model_text(proj: Path) -> str:
    return (defn(proj) / "model.tmdl").read_text(encoding="utf-8")


def changes(before: dict, proj: Path) -> dict:
    return delta(before, snapshot(proj))


def only_model_and(before: dict, proj: Path, *new_files: str) -> None:
    """Exactly `new_files` were added, model.tmdl changed, nothing else."""
    d = changes(before, proj)
    assert d["deleted"] == []
    assert sorted(d["added"]) == sorted(
        f"Synthetic.SemanticModel/definition/{f}" for f in new_files)
    assert d["modified"] == ["Synthetic.SemanticModel/definition/model.tmdl"]


# --- ref lines --------------------------------------------------------------

def test_ref_groups_follow_table_refs_and_keep_order():
    text = "model Model\n\tculture: en-US\n\nref table A\nref table B\n"
    text = ts.add_ref_text(text, "role", "R1")
    text = ts.add_ref_text(text, "cultureInfo", "de-DE")
    text = ts.add_ref_text(text, "perspective", "P 1")
    text = ts.add_ref_text(text, "role", "R2")
    assert text == (
        "model Model\n\tculture: en-US\n\nref table A\nref table B\n\n"
        "ref cultureInfo de-DE\n\nref perspective 'P 1'\n\nref role R1\nref role R2\n")
    assert ts.list_refs_text(text, "role") == ["R1", "R2"]
    assert ts.add_ref_text(text, "role", "R1") == text          # idempotent
    # removal restores the previous shape, group by group
    text = ts.remove_ref_text(text, "role", "R2")
    text = ts.remove_ref_text(text, "role", "R1")
    text = ts.remove_ref_text(text, "perspective", "P 1")
    text = ts.remove_ref_text(text, "cultureInfo", "de-DE")
    assert text == "model Model\n\tculture: en-US\n\nref table A\nref table B\n"


def test_ref_understands_docs_spelling_and_indentation():
    text = "model Model\n\n\tref table A\n\n\tref culture en-US\n"
    assert ts.list_refs_text(text, "cultureInfo") == ["en-US"]
    out = ts.add_ref_text(text, "cultureInfo", "pt-PT")
    assert "\tref culture pt-PT\n" in out                      # follows the file's spelling
    out = ts.add_ref_text(out, "role", "Stores Cluster 1")
    assert out.endswith("\n\tref role 'Stores Cluster 1'\n")   # neighbour's indentation


# --- roles: RLS -------------------------------------------------------------

def test_create_role_writes_file_and_registers_ref(state, proj):
    orig_model = model_text(proj)
    before = snapshot(proj)
    res = sec.create_role(state, "Sales West", "West only", "read",
                          {"Sales": "'Sales'[Amount] > 100"})
    assert res["ok"] and res["action"] == "created"
    assert res["file"] == "definition/roles/Sales West.tmdl"
    assert (defn(proj) / "roles" / "Sales West.tmdl").read_text(encoding="utf-8") == (
        "/// West only\n"
        "role 'Sales West'\n"
        "\tmodelPermission: read\n"
        "\n"
        "\ttablePermission Sales = 'Sales'[Amount] > 100\n")
    assert model_text(proj) == orig_model + "\n\tref role 'Sales West'\n"
    only_model_and(before, proj, "roles/Sales West.tmdl")


def test_create_role_without_filters_and_default_permission(state, proj):
    sec.create_role(state, "Everyone")
    assert (defn(proj) / "roles" / "Everyone.tmdl").read_text(encoding="utf-8") == (
        "role Everyone\n\tmodelPermission: read\n")


def test_create_role_multiline_dax_round_trips(state, proj):
    dax = "VAR _min = 2021\nRETURN\n    [Year] >= _min"
    sec.create_role(state, "Recent", table_filters={"Date": dax})
    text = (defn(proj) / "roles" / "Recent.tmdl").read_text(encoding="utf-8")
    assert "\ttablePermission Date =\n\t\t\tVAR _min = 2021\n\t\t\tRETURN\n\t\t\t    [Year] >= _min\n" in text
    role = sec.list_roles(state)[0]
    assert role["tables"][0]["filter"] == dax


def test_create_role_rejections_leave_project_untouched(state, proj):
    before = snapshot(proj)
    bad = [
        (dict(name="  "), "non-empty"),
        (dict(name="R", model_permission="write"), "model_permission"),
        (dict(name="R", table_filters={"Ghost": "1=1"}), "Table 'Ghost' not found"),
        (dict(name="R", table_filters={"Sales": ""}), "empty"),
        (dict(name="R", table_filters={"Sales": "'Sales'[Amount] > (1"}), "closing"),
        (dict(name="R", table_filters={"Sales": "'Sales'[Region] = \"West"}), "not valid DAX"),
        (dict(name="R", table_filters={"Sales": "'Sales'[Nope] = 1"}), "no such column"),
        (dict(name="R", table_filters={"Sales": "'Nope'[Amount] = 1"}), "does not exist"),
        (dict(name="R", table_filters=["Sales"]), "object"),
    ]
    for kwargs, msg in bad:
        with pytest.raises(ValueError, match=msg):
            sec.create_role(state, **kwargs)
    assert snapshot(proj) == before


def test_create_role_duplicate_name_is_case_insensitive(state):
    sec.create_role(state, "Managers")
    with pytest.raises(ValueError, match="already exists"):
        sec.create_role(state, "managers")


def test_create_role_warns_on_unknown_bare_name(state):
    res = sec.create_role(state, "Loose", table_filters={"Sales": "[Regoin] = \"W\""})
    assert "Regoin" in res["warnings"][0]


def test_filter_may_use_variable_tables_and_measures(state):
    dax = ("VAR _t = FILTER(ALL('Date'), 'Date'[Year] > 2020)\n"
           "RETURN COUNTROWS(_t) > 0 && [Net Revenue] > 0")
    res = sec.create_role(state, "Vars", table_filters={"Sales": dax})
    assert "warnings" not in res


def test_list_roles_follows_ref_order(state):
    sec.create_role(state, "Zed")
    sec.create_role(state, "Alpha", "First!", "readRefresh", {"Sales": "[Amount] > 0"})
    roles = sec.list_roles(state)
    assert [r["name"] for r in roles] == ["Zed", "Alpha"]
    alpha = roles[1]
    assert alpha["description"] == "First!" and alpha["model_permission"] == "readRefresh"
    assert alpha["tables"] == [{"table": "Sales", "filter": "[Amount] > 0",
                                "metadata_permission": None, "columns": {}}]


def test_update_role_partial_and_idempotent(state, proj):
    sec.create_role(state, "R", "old", "read",
                    {"Sales": "'Sales'[Amount] > 1", "Date": "'Date'[Year] > 2020"})
    p = defn(proj) / "roles" / "R.tmdl"
    sec.set_column_permission(state, "R", "Sales", "Cost")           # OLS must survive
    before = p.read_text(encoding="utf-8")

    res = sec.update_role(state, "R", description="new text",
                          model_permission="readRefresh",
                          table_filters={"Sales": "'Sales'[Amount] > 2"},
                          remove_tables=["Date"])
    assert res["action"] == "updated"
    assert set(res["changed"]) == {"description", "model_permission",
                                   "filter:Sales", "removed_filter:Date"}
    text = p.read_text(encoding="utf-8")
    assert text == (
        "/// new text\n"
        "role R\n"
        "\tmodelPermission: readRefresh\n"
        "\n"
        "\ttablePermission Sales = 'Sales'[Amount] > 2\n"
        "\t\tcolumnPermission Cost = none\n")
    assert before != text
    again = sec.update_role(state, "R", description="new text",
                            table_filters={"Sales": "'Sales'[Amount] > 2"})
    assert again["action"] == "unchanged" and again["changed"] == []
    assert p.read_text(encoding="utf-8") == text
    # description "" removes it
    sec.update_role(state, "R", description="")
    assert p.read_text(encoding="utf-8").startswith("role R\n")


def test_update_role_clearing_filter_keeps_ols_block(state, proj):
    sec.create_role(state, "R", table_filters={"Sales": "[Amount] > 0"})
    sec.set_column_permission(state, "R", "Sales", "Cost")
    sec.update_role(state, "R", remove_tables=["Sales"])
    text = (defn(proj) / "roles" / "R.tmdl").read_text(encoding="utf-8")
    assert "\ttablePermission Sales\n\t\tcolumnPermission Cost = none\n" in text
    assert "= [Amount]" not in text


def test_update_role_errors(state):
    sec.create_role(state, "R", table_filters={"Sales": "[Amount] > 0"})
    with pytest.raises(ValueError, match="Nothing to update"):
        sec.update_role(state, "R")
    with pytest.raises(ValueError, match="no row filter"):
        sec.update_role(state, "R", remove_tables=["Date"])
    with pytest.raises(ValueError, match="both in"):
        sec.update_role(state, "R", table_filters={"Sales": "[Amount] > 1"},
                        remove_tables=["Sales"])
    with pytest.raises(ValueError, match="Role 'Nope' not found"):
        sec.update_role(state, "Nope", description="x")
    with pytest.raises(ValueError, match="model_permission"):
        sec.update_role(state, "R", model_permission="admin")


def test_delete_role_restores_model_bytes_and_keeps_backup(state, proj):
    orig = (defn(proj) / "model.tmdl").read_bytes()
    sec.create_role(state, "Temp", table_filters={"Sales": "[Amount] > 0"})
    assert (defn(proj) / "model.tmdl").read_bytes() != orig
    res = sec.delete_role(state, "temp")            # case-insensitive lookup
    assert res["action"] == "deleted" and res["role"] == "Temp"
    assert not (defn(proj) / "roles" / "Temp.tmdl").exists()
    assert (defn(proj) / "model.tmdl").read_bytes() == orig
    assert list((defn(proj) / "roles").glob("Temp.tmdl.bak-*"))
    with pytest.raises(ValueError, match="not found"):
        sec.delete_role(state, "Temp")


# --- roles: OLS -------------------------------------------------------------

def test_column_permission_creates_filterless_block_then_removes_it(state, proj):
    sec.create_role(state, "R", table_filters={"Date": "'Date'[Year] > 2020"})
    p = defn(proj) / "roles" / "R.tmdl"
    base = p.read_text(encoding="utf-8")

    res = sec.set_column_permission(state, "R", "Sales", "cost")   # name is case-folded
    assert res["column"] == "Cost" and res["permission"] == "none"
    text = p.read_text(encoding="utf-8")
    assert text == base + "\n\ttablePermission Sales\n\t\tcolumnPermission Cost = none\n"

    sec.set_column_permission(state, "R", "Sales", "Amount", "read")
    role = sec.list_roles(state)[0]
    sales = next(t for t in role["tables"] if t["table"] == "Sales")
    assert sales["filter"] is None
    assert sales["columns"] == {"Cost": "none", "Amount": "read"}

    sec.set_column_permission(state, "R", "Sales", "Cost", "default")
    sec.set_column_permission(state, "R", "Sales", "Amount", "default")
    assert p.read_text(encoding="utf-8") == base          # empty block dropped


def test_column_permission_keeps_row_filter(state, proj):
    sec.create_role(state, "R", table_filters={"Sales": "[Amount] > 0"})
    sec.set_column_permission(state, "R", "Sales", "Cost")
    sec.set_column_permission(state, "R", "Sales", "Cost", "default")
    assert (defn(proj) / "roles" / "R.tmdl").read_text(encoding="utf-8").endswith(
        "\ttablePermission Sales = [Amount] > 0\n")


def test_column_permission_with_multiline_filter(state, proj):
    dax = "VAR y = 2020\nRETURN [Year] > y"
    sec.create_role(state, "R", table_filters={"Date": dax})
    sec.set_column_permission(state, "R", "Date", "Year")
    role = sec.list_roles(state)[0]
    assert role["tables"][0]["filter"] == dax
    assert role["tables"][0]["columns"] == {"Year": "none"}
    sec.set_column_permission(state, "R", "Date", "Year", "default")
    assert sec.list_roles(state)[0]["tables"][0]["columns"] == {}


def test_column_permission_validation(state):
    sec.create_role(state, "R")
    with pytest.raises(ValueError, match="permission"):
        sec.set_column_permission(state, "R", "Sales", "Cost", "deny")
    with pytest.raises(ValueError, match="no such|not found in table"):
        sec.set_column_permission(state, "R", "Sales", "Ghost")
    with pytest.raises(ValueError, match="is a measure"):
        sec.set_column_permission(state, "R", "Sales", "Net Revenue")
    with pytest.raises(ValueError, match="Table 'Ghost' not found"):
        sec.set_column_permission(state, "R", "Ghost", "Cost")
    with pytest.raises(ValueError, match="Role 'X' not found"):
        sec.set_column_permission(state, "X", "Sales", "Cost")


def test_table_metadata_permission(state, proj):
    sec.create_role(state, "R", table_filters={"Sales": "[Amount] > 0"})
    p = defn(proj) / "roles" / "R.tmdl"
    sec.set_table_permission_metadata(state, "R", "Date")                # none
    sec.set_table_permission_metadata(state, "R", "Sales", "read")
    t = p.read_text(encoding="utf-8")
    assert "\ttablePermission Date\n\t\tmetadataPermission: none\n" in t
    assert "\ttablePermission Sales = [Amount] > 0\n\t\tmetadataPermission: read\n" in t
    roles = {x["table"]: x for x in sec.list_roles(state)[0]["tables"]}
    assert roles["Date"]["metadata_permission"] == "none"
    # switching value replaces the line, "default" removes it
    sec.set_table_permission_metadata(state, "R", "Date", "read")
    assert "metadataPermission: read" in p.read_text(encoding="utf-8")
    sec.set_table_permission_metadata(state, "R", "Date", "default")
    sec.set_table_permission_metadata(state, "R", "Sales", "default")
    assert p.read_text(encoding="utf-8") == (
        "role R\n\tmodelPermission: read\n\n\ttablePermission Sales = [Amount] > 0\n")
    assert sec.set_table_permission_metadata(state, "R", "Date", "default")["action"] == "unchanged"


def test_parse_role_text_reads_desktop_shapes():
    text = (
        "/// Regional\n"
        "role 'Stores Cluster 1'\n"
        "\tmodelPermission: read\n"
        "\n"
        "\ttablePermission Store = 'Store'[Store Code] IN {\"1\",\"2\"}\n"
        "\t\tcolumnPermission 'Store Code' = none\n"
        "\ttablePermission Fenced = ```\n"
        "\t\t\t[A] = 1\n"
        "\t\t\t    && [B] = 2\n"
        "\t\t\t```\n"
        "\t\tmetadataPermission: read\n"
        "\n"
        "\tmember 'user1@company.com'\n"
        "\tmember 'g@d.com' = group\n"
        "\n"
        "\tannotation PBI_Id = dbdfbff19eea4f0d84010925e3c7f23e\n")
    role = ts.parse_role_text(text)
    assert role["name"] == "Stores Cluster 1" and role["description"] == "Regional"
    assert role["tables"]["Store"] == {"filter": "'Store'[Store Code] IN {\"1\",\"2\"}",
                                       "metadata_permission": None,
                                       "columns": {"Store Code": "none"}}
    assert role["tables"]["Fenced"]["filter"] == "[A] = 1\n    && [B] = 2"
    assert role["tables"]["Fenced"]["metadata_permission"] == "read"
    assert role["members"] == ["user1@company.com", "g@d.com"]
    assert role["annotations"] == {"PBI_Id": "dbdfbff19eea4f0d84010925e3c7f23e"}
    # editing a fenced filter keeps its OLS line and drops the old fence body
    out = ts.upsert_table_permission_text(text, "Fenced", "[C] = 3")
    assert "\ttablePermission Fenced = [C] = 3\n\t\tmetadataPermission: read\n" in out
    assert "[A] = 1" not in out and "```" not in out
    assert out.count("member ") == 2 and "annotation PBI_Id" in out


def test_role_edits_preserve_crlf_and_unrelated_bytes(state, proj):
    mp = defn(proj) / "model.tmdl"
    crlf = mp.read_bytes().replace(b"\n", b"\r\n")
    mp.write_bytes(crlf)
    sec.create_role(state, "R", table_filters={"Sales": "[Amount] > 0"})
    data = mp.read_bytes()
    assert data.startswith(crlf.rstrip(b"\r\n"))
    assert b"\n" not in data.replace(b"\r\n", b"")          # no bare LFs
    assert data.endswith(b"\r\n\tref role R\r\n")
    # new files default to LF
    assert b"\r" not in (defn(proj) / "roles" / "R.tmdl").read_bytes()


# --- perspectives -----------------------------------------------------------

def add_hierarchy(proj: Path) -> None:
    p = defn(proj) / "tables" / "Date.tmdl"
    text = p.read_text(encoding="utf-8")
    marker = "\n\tpartition Date"
    p.write_text(text.replace(marker, HIERARCHY_BLOCK + marker), encoding="utf-8")


def test_create_perspective(state, proj):
    add_hierarchy(proj)
    orig_model = model_text(proj)
    before = snapshot(proj)
    res = sec.create_perspective(state, "Sales View", "For sellers", {
        "Sales": {"columns": ["Amount", "Order Count"], "measures": ["Net Revenue"]},
        "Date": {"hierarchies": ["Date Hierarchy"], "columns": "Year"},
    })
    assert res["tables"] == ["Date", "Sales"]
    assert (defn(proj) / "perspectives" / "Sales View.tmdl").read_text(encoding="utf-8") == (
        "/// For sellers\n"
        "perspective 'Sales View'\n"
        "\n"
        "\tperspectiveTable Sales\n"
        "\t\tperspectiveColumn Amount\n"
        "\t\tperspectiveColumn 'Order Count'\n"
        "\t\tperspectiveMeasure 'Net Revenue'\n"
        "\n"
        "\tperspectiveTable Date\n"
        "\t\tperspectiveColumn Year\n"
        "\t\tperspectiveHierarchy 'Date Hierarchy'\n")
    assert model_text(proj) == orig_model + "\n\tref perspective 'Sales View'\n"
    only_model_and(before, proj, "perspectives/Sales View.tmdl")


def test_create_perspective_include_all_and_list(state):
    sec.create_perspective(state, "Everything", tables={"Sales": {"include_all": True}})
    sec.create_perspective(state, "Small", "tiny", {"Date": {"columns": ["Date"]}})
    listed = sec.list_perspectives(state)
    assert [p["name"] for p in listed] == ["Everything", "Small"]
    assert listed[0]["tables"]["Sales"] == {"include_all": True, "columns": [],
                                            "measures": [], "hierarchies": []}
    assert listed[1]["description"] == "tiny"
    assert listed[1]["tables"]["Date"]["columns"] == ["Date"]


def test_create_perspective_validates_every_reference(state, proj):
    before = snapshot(proj)
    cases = [
        ({"Ghost": {"columns": ["A"]}}, "Table 'Ghost' not found"),
        ({"Sales": {"columns": ["Nope"]}}, "Column 'Nope' not found in table 'Sales'"),
        ({"Sales": {"measures": ["Nope"]}}, "Measure 'Nope' not found in table 'Sales'"),
        ({"Sales": {"measures": ["Days In Period"]}}, "Measure 'Days In Period' not found"),
        ({"Sales": {"hierarchies": ["H"]}}, "Hierarchy 'H' not found"),
        ({"Sales": {}}, "lists no objects"),
        ({"Sales": {"column": ["Amount"]}}, "Unknown key"),
        ({"Sales": {"columns": [1]}}, "list of names"),
        (["Sales"], "object"),
    ]
    for tables, msg in cases:
        with pytest.raises(ValueError, match=msg):
            sec.create_perspective(state, "P", tables=tables)
    assert snapshot(proj) == before
    sec.create_perspective(state, "P")
    with pytest.raises(ValueError, match="already exists"):
        sec.create_perspective(state, "p")


def test_update_perspective(state, proj):
    add_hierarchy(proj)
    sec.create_perspective(state, "P", "d", {"Sales": {"columns": ["Amount"]}})
    p = defn(proj) / "perspectives" / "P.tmdl"

    res = sec.update_perspective(state, "P", description="e", tables={
        "Sales": {"columns": ["Amount", "Cost"], "measures": ["Net Revenue"]},
        "Date": {"hierarchies": ["Date Hierarchy"]}})
    assert res["action"] == "updated"
    assert p.read_text(encoding="utf-8") == (
        "/// e\n"
        "perspective P\n"
        "\n"
        "\tperspectiveTable Sales\n"
        "\t\tperspectiveColumn Amount\n"
        "\t\tperspectiveColumn Cost\n"
        "\t\tperspectiveMeasure 'Net Revenue'\n"
        "\n"
        "\tperspectiveTable Date\n"
        "\t\tperspectiveHierarchy 'Date Hierarchy'\n")
    same = sec.update_perspective(state, "P", tables={"Sales": {"columns": ["Amount"]}})
    assert same["action"] == "unchanged"

    sec.update_perspective(state, "P", remove_objects={"Sales": {"columns": ["Amount"]}})
    assert sec.list_perspectives(state)[0]["tables"]["Sales"]["columns"] == ["Cost"]
    # removing the last object drops the table entry
    sec.update_perspective(state, "P", remove_objects={"Date": {"hierarchies": ["Date Hierarchy"]}})
    assert "Date" not in sec.list_perspectives(state)[0]["tables"]
    # remove_tables + tables in one call replaces a table's entry
    sec.update_perspective(state, "P", remove_tables=["Sales"],
                           tables={"Sales": {"columns": ["OrderDate"]}})
    assert sec.list_perspectives(state)[0]["tables"]["Sales"]["columns"] == ["OrderDate"]
    sec.update_perspective(state, "P", description="")
    assert p.read_text(encoding="utf-8").startswith("perspective P\n")


def test_update_perspective_errors(state):
    sec.create_perspective(state, "P", tables={"Sales": {"columns": ["Amount"]}})
    with pytest.raises(ValueError, match="Nothing to update"):
        sec.update_perspective(state, "P")
    with pytest.raises(ValueError, match="not part of perspective"):
        sec.update_perspective(state, "P", remove_tables=["Date"])
    with pytest.raises(ValueError, match="are not in perspective"):
        sec.update_perspective(state, "P", remove_objects={"Sales": {"columns": ["Cost"]}})
    with pytest.raises(ValueError, match="Column 'Ghost' not found"):
        sec.update_perspective(state, "P", tables={"Sales": {"columns": ["Ghost"]}})


def test_delete_perspective_restores_model_bytes(state, proj):
    orig = (defn(proj) / "model.tmdl").read_bytes()
    sec.create_perspective(state, "P", tables={"Sales": {"columns": ["Amount"]}})
    res = sec.delete_perspective(state, "P")
    assert res["action"] == "deleted"
    assert not (defn(proj) / "perspectives" / "P.tmdl").exists()
    assert (defn(proj) / "model.tmdl").read_bytes() == orig
    assert sec.list_perspectives(state) == []


def test_perspective_text_edits_preserve_foreign_lines():
    text = ("perspective P\n\n\tperspectiveTable Sales\n\t\tperspectiveColumn A\n"
            "\t\tannotation X = 1\n\n\tannotation Y = 2\n")
    out = ts.add_perspective_objects_text(text, "Sales", {"columns": ["B"]})
    assert "\t\tannotation X = 1\n" in out and "\tannotation Y = 2\n" in out
    out = ts.add_perspective_objects_text(out, "Date", {"measures": ["M"]})
    assert out.index("perspectiveTable Date") < out.index("annotation Y")
    out = ts.remove_perspective_objects_text(out, "Sales", {"columns": ["A", "B"]})
    assert "annotation X" in out and "perspectiveColumn" not in out


# --- cultures / translations -----------------------------------------------

def test_add_culture(state, proj):
    orig_model = model_text(proj)
    before = snapshot(proj)
    res = sec.add_culture(state, "de-DE")
    assert res == {"ok": True, "action": "created", "culture": "de-DE",
                   "file": "definition/cultures/de-DE.tmdl", "is_model_default": False}
    text = (defn(proj) / "cultures" / "de-DE.tmdl").read_text(encoding="utf-8")
    assert text.startswith("cultureInfo de-DE\n\n\tlinguisticMetadata =\n")
    assert text.endswith("\t\tcontentType: json\n")
    payload = "\n".join(ln.strip() for ln in text.split("\n")[3:-2])
    assert json.loads(payload) == {"Version": "1.0.0", "Language": "de-DE"}
    assert model_text(proj) == orig_model + "\n\tref cultureInfo de-DE\n"
    only_model_and(before, proj, "cultures/de-DE.tmdl")
    assert sec.add_culture(state, "en-US")["is_model_default"] is True


def test_add_culture_validation(state):
    for bad in ("", "german", "de_DE", "de-", "1234", "de DE"):
        with pytest.raises(ValueError, match="BCP-47"):
            sec.add_culture(state, bad)
    sec.add_culture(state, "fr-FR")
    with pytest.raises(ValueError, match="already exists"):
        sec.add_culture(state, "fr-fr")


def test_set_translation_all_object_kinds(state, proj):
    add_hierarchy(proj)
    sec.add_culture(state, "de-DE")
    sec.set_translation(state, "de-DE", "table", "Sales", caption="Verkauf",
                        description="Verkaufstabelle")
    sec.set_translation(state, "de-DE", "column", "Sales", "Amount", caption="Betrag")
    sec.set_translation(state, "de-DE", "measure", "Sales", "Net Revenue",
                        caption="Nettoumsatz", display_folder="Kennzahlen")
    sec.set_translation(state, "de-DE", "hierarchy", "Date", "Date Hierarchy",
                        caption="Datumshierarchie")
    sec.set_translation(state, "de-DE", "table", "Date", caption="Datum")
    text = (defn(proj) / "cultures" / "de-DE.tmdl").read_text(encoding="utf-8")
    assert text.split("\tlinguisticMetadata")[0] == (
        "cultureInfo de-DE\n"
        "\n"
        "\ttranslations\n"
        "\t\tmodel Model\n"
        "\t\t\ttable Sales\n"
        "\t\t\t\tcaption: Verkauf\n"
        "\t\t\t\tdescription: Verkaufstabelle\n"
        "\t\t\t\tcolumn Amount\n"
        "\t\t\t\t\tcaption: Betrag\n"
        "\t\t\t\tmeasure 'Net Revenue'\n"
        "\t\t\t\t\tcaption: Nettoumsatz\n"
        "\t\t\t\t\tdisplayFolder: Kennzahlen\n"
        "\t\t\ttable Date\n"
        "\t\t\t\tcaption: Datum\n"
        "\t\t\t\thierarchy 'Date Hierarchy'\n"
        "\t\t\t\t\tcaption: Datumshierarchie\n"
        "\n")
    assert text.endswith("\t\tcontentType: json\n")

    rows = {(r["object_type"], r["table"], r["name"]): r
            for r in sec.list_translations(state, "de-DE")["translations"]}
    assert rows[("table", "Sales", "Sales")]["caption"] == "Verkauf"
    assert rows[("table", "Sales", "Sales")]["description"] == "Verkaufstabelle"
    assert rows[("column", "Sales", "Amount")]["caption"] == "Betrag"
    assert rows[("measure", "Sales", "Net Revenue")]["display_folder"] == "Kennzahlen"
    assert rows[("hierarchy", "Date", "Date Hierarchy")]["caption"] == "Datumshierarchie"
    assert rows[("table", "Date", "Date")]["caption"] == "Datum"


def test_set_translation_update_and_remove(state, proj):
    sec.add_culture(state, "de-DE")
    sec.set_translation(state, "de-DE", "column", "Sales", "Amount",
                        caption="Betrag", description="Der Betrag")
    p = defn(proj) / "cultures" / "de-DE.tmdl"
    sec.set_translation(state, "de-DE", "column", "Sales", "Amount", caption="Summe")
    text = p.read_text(encoding="utf-8")
    assert "caption: Summe" in text and "caption: Betrag" not in text
    assert "description: Der Betrag" in text                       # untouched
    assert sec.set_translation(state, "de-DE", "column", "Sales", "Amount",
                               caption="Summe")["action"] == "unchanged"
    sec.set_translation(state, "de-DE", "column", "Sales", "Amount",
                        caption="", description="")
    text = p.read_text(encoding="utf-8")
    assert "column Amount" not in text and "table Sales" not in text   # scaffolding pruned
    assert sec.list_translations(state, "de-DE")["translations"] == []


def test_set_translation_quotes_only_when_needed(state, proj):
    sec.add_culture(state, "de-DE")
    sec.set_translation(state, "de-DE", "measure", "Sales", "Net Revenue",
                        caption='  "Umsatz"  ', display_folder="A: B")
    text = (defn(proj) / "cultures" / "de-DE.tmdl").read_text(encoding="utf-8")
    assert 'caption: "  ""Umsatz""  "' in text
    assert "displayFolder: A: B" in text
    row = sec.list_translations(state, "de-DE")["translations"][0]
    assert row["caption"] == '  "Umsatz"  ' and row["display_folder"] == "A: B"
    with pytest.raises(ValueError, match="single-line"):
        sec.set_translation(state, "de-DE", "table", "Sales", caption="a\nb")


def test_set_translation_validation(state, proj):
    with pytest.raises(ValueError, match="pbi_add_culture"):
        sec.set_translation(state, "de-DE", "table", "Sales", caption="x")
    sec.add_culture(state, "de-DE")
    before = snapshot(proj)
    cases = [
        (dict(object_type="widget", table="Sales", caption="x"), "object_type"),
        (dict(object_type="table", table="Ghost", caption="x"), "Table 'Ghost' not found"),
        (dict(object_type="column", table="Sales", name="Ghost", caption="x"), "Column 'Ghost' not found"),
        (dict(object_type="column", table="Sales", caption="x"), "name is required"),
        (dict(object_type="measure", table="Sales", name="Amount", caption="x"), "Measure 'Amount' not found"),
        (dict(object_type="hierarchy", table="Sales", name="H", caption="x"), "Hierarchy 'H' not found"),
        (dict(object_type="table", table="Sales"), "at least one"),
        (dict(object_type="table", table="Sales", display_folder="f"), "no displayFolder"),
    ]
    for kwargs, msg in cases:
        with pytest.raises(ValueError, match=msg):
            sec.set_translation(state, "de-DE", **kwargs)
    assert snapshot(proj) == before


def test_list_translations_overview(state):
    assert sec.list_translations(state) == {"cultures": []}
    sec.add_culture(state, "en-US")
    sec.add_culture(state, "de-DE")
    sec.set_translation(state, "de-DE", "table", "Sales", caption="Verkauf")
    cultures = {c["culture"]: c for c in sec.list_translations(state)["cultures"]}
    assert cultures["en-US"]["is_model_default"] is True
    assert cultures["en-US"]["translated_objects"] == 0
    assert cultures["de-DE"]["translated_objects"] == 1
    assert cultures["de-DE"]["has_linguistic_metadata"] is True
    with pytest.raises(ValueError, match="Culture 'xx' not found"):
        sec.list_translations(state, "xx")


def test_delete_culture_restores_model_bytes(state, proj):
    orig = (defn(proj) / "model.tmdl").read_bytes()
    sec.add_culture(state, "de-DE")
    res = sec.delete_culture(state, "de-DE")
    assert res["action"] == "deleted" and "note" not in res
    assert not (defn(proj) / "cultures" / "de-DE.tmdl").exists()
    assert (defn(proj) / "model.tmdl").read_bytes() == orig
    sec.add_culture(state, "en-US")
    assert "default culture" in sec.delete_culture(state, "en-US")["note"]


def test_parse_culture_text_reads_docs_shape():
    text = (
        "cultureInfo pt-PT\n"
        "\n"
        "\ttranslations\n"
        "\t\tmodel Model\n"
        "\t\t\tcaption: Modelo\n"
        "\t\t\ttable Sales\n"
        "\t\t\t\tcaption: Vendas\n"
        "\t\t\t\tmeasure 'Sales Amount'\n"
        "\t\t\t\t\tcaption: Total de Vendas\n"
        "\t\t\t\t\tdisplayFolder: Métricas Base\n"
        "\t\t\ttable Product\n"
        "\t\t\t\tcolumn Weight\n"
        "\t\t\t\t\tcaption: Peso\n"
        "\n"
        "\tlinguisticMetadata =\n"
        "\t\t\t{}\n"
        "\t\tcontentType: json\n")
    info = ts.parse_culture_text(text)
    assert info["code"] == "pt-PT" and info["has_linguistic_metadata"]
    assert info["model"] == {"caption": "Modelo"}
    assert info["tables"]["Sales"]["caption"] == "Vendas"
    assert info["tables"]["Sales"]["measures"]["Sales Amount"] == {
        "caption": "Total de Vendas", "displayFolder": "Métricas Base"}
    assert info["tables"]["Product"]["columns"]["Weight"] == {"caption": "Peso"}
    out = ts.set_translation_text(text, "column", "Product", "Weight", caption="Gewicht")
    assert out.replace("Gewicht", "Peso") == text                 # only that line changed


# --- model integration -------------------------------------------------------

def test_new_file_kinds_do_not_disturb_the_model(state, proj):
    tables_before = [t.model_dump() for t in PbipProject(proj / "Synthetic.pbip").list_tables()]
    sec.create_role(state, "R", table_filters={"Sales": "[Amount] > 0"})
    sec.set_column_permission(state, "R", "Sales", "Cost")
    sec.create_perspective(state, "P", tables={"Sales": {"columns": ["Amount"]}})
    sec.add_culture(state, "de-DE")
    sec.set_translation(state, "de-DE", "table", "Sales", caption="Verkauf")
    fresh = PbipProject(proj / "Synthetic.pbip")
    assert [t.model_dump() for t in fresh.list_tables()] == tables_before
    assert fresh.get_model()["tables"]
    # refs end up grouped in the documented order
    refs = [ln.strip() for ln in model_text(proj).split("\n") if "ref " in ln]
    assert refs == ["ref table Sales", "ref table Date", "ref cultureInfo de-DE",
                    "ref perspective P", "ref role R"]


def test_tools_need_a_project():
    with pytest.raises(ValueError, match="pbi_set_project"):
        sec.list_roles(ModelState())


# --- MCP layer ----------------------------------------------------------------

def _payload(result):
    structured = None
    if isinstance(result, tuple):
        result, structured = result
    structured = (structured or getattr(result, "structured_content", None)
                  or getattr(result, "structuredContent", None))
    if structured is not None:
        return structured.get("result", structured) if isinstance(structured, dict) else structured
    content = getattr(result, "content", result)
    items = [json.loads(c.text) for c in content]
    return items[0] if len(items) == 1 else items


def _hint(tool, name: str):
    a = tool.annotations
    snake = {"readOnlyHint": "read_only_hint", "destructiveHint": "destructive_hint",
             "idempotentHint": "idempotent_hint"}[name]
    return getattr(a, name) if hasattr(a, name) else getattr(a, snake)


SECURITY_TOOLS = {
    "pbi_create_role": "write", "pbi_update_role": "write", "pbi_list_roles": "read",
    "pbi_delete_role": "delete", "pbi_set_column_permission": "write",
    "pbi_set_table_permission_metadata": "write",
    "pbi_create_perspective": "write", "pbi_update_perspective": "write",
    "pbi_list_perspectives": "read", "pbi_delete_perspective": "delete",
    "pbi_add_culture": "write", "pbi_set_translation": "write",
    "pbi_list_translations": "read", "pbi_delete_culture": "delete",
}


def test_tools_are_registered_with_correct_annotations():
    import model_server.server as srv
    import report_server.server as rsrv
    tools = {t.name: t for t in asyncio.run(srv.mcp.list_tools())}
    report_names = {t.name for t in asyncio.run(rsrv.mcp.list_tools())}
    for name, kind in SECURITY_TOOLS.items():
        t = tools[name]
        assert name not in report_names
        assert t.description and len(t.description) > 60
        props = (getattr(t, "inputSchema", None) or t.input_schema).get("properties", {})
        if kind == "read":
            assert _hint(t, "readOnlyHint") is True and "dry_run" not in props
        else:
            assert _hint(t, "readOnlyHint") is False and "dry_run" in props
            assert _hint(t, "destructiveHint") is (kind == "delete")


def test_end_to_end_dry_run_write_and_undo(proj):
    import model_server.server as srv

    def call(tool_name, **args):
        return _payload(asyncio.run(srv.mcp.call_tool(tool_name, args)))

    call("pbi_set_project", path=str(proj / "Synthetic.pbip"))
    before = snapshot(proj)
    args = dict(name="Sales West", table_filters={"Sales": "'Sales'[Amount] > 100"})

    preview = call("pbi_create_role", dry_run=True, **args)
    assert preview["dry_run"] is True
    assert "roles/Sales West.tmdl" in preview["diff"] and "ref role" in preview["diff"]
    assert snapshot(proj) == before

    real = call("pbi_create_role", **args)
    assert real["ok"] is True and (defn(proj) / "roles" / "Sales West.tmdl").exists()
    assert call("pbi_list_roles")[0]["name"] == "Sales West"
    assert call("pbi_undo_history")[0]["tool"] == "pbi_create_role"

    call("pbi_undo")
    assert snapshot(proj) == before

    with pytest.raises(Exception):   # detail is masked by the MCP layer
        call("pbi_create_role", name="Bad", table_filters={"Ghost": "1=1"})
    assert snapshot(proj) == before
