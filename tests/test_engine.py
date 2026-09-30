"""core/engine + model_server/tools_engine: the live Analysis Services bridge.

Unit tests mock the PowerShell runner (``engine._run_powershell``) so they run
anywhere; the live tests at the bottom run only when a Power BI Desktop
instance is up (``python scripts/desktop_launch.py tests/fixtures/engine/Engine.pbip``).
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import engine
from core.pbip import PbipProject
from model_server import tools_engine

ENGINE_PBIP = Path(__file__).parent / "fixtures" / "engine" / "Engine.pbip"


# --- helpers -----------------------------------------------------------------------

def _completed(doc, returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    out = doc if isinstance(doc, str) else json.dumps(doc)
    return subprocess.CompletedProcess(["powershell.exe"], returncode,
                                       stdout=out.encode("utf-8"),
                                       stderr=stderr.encode("utf-8"))


def _result(columns, rows, truncated=False, elapsed=7):
    return {"columns": columns, "rows": rows, "row_count": len(rows),
            "truncated": truncated, "elapsed_ms": elapsed}


class FakeRunner:
    """Stands in for engine._run_powershell.

    ``handler(query, connection_string, max_rows)`` returns one result dict
    per query; the request file is read while it still exists so tests can
    assert on what the bridge would have received. A query with ``then`` is
    fanned out like the real bridge does: ``then.query`` runs once per
    database named in ``then.catalog_column`` of the first result, on the
    connection string plus ``;Catalog=<name>``.
    """

    def __init__(self, handler=None, store_location: str | None = None):
        self.handler = handler or (lambda q, cs, n: _result(["v"], [[1]]))
        self.store_location = store_location
        self.requests: list[dict] = []
        self.calls: list[list[str]] = []

    def __call__(self, args, timeout):
        self.calls.append(list(args))
        if args[0] == "-Command":                 # Get-AppxPackage probe
            return _completed(self.store_location or "")
        assert args[0] == "-File" and args[2] == "-Request"
        req = json.loads(Path(args[3]).read_text(encoding="utf-8"))
        self.requests.append(req)
        assert Path(args[1]).read_text(encoding="utf-8-sig").lstrip().startswith("param(")
        results = []
        for q in req["queries"]:
            res = self.handler(q["query"], q["connection_string"], q["max_rows"])
            then = q.get("then")
            if then and "error" not in res:
                col = [c.lower() for c in res["columns"]].index(then["catalog_column"].lower())
                res = dict(res, children=[{
                    "catalog": row[col],
                    "result": self.handler(then["query"],
                                           f"{q['connection_string']};Catalog={row[col]}",
                                           then["max_rows"])}
                    for row in res["rows"] if row[col]])
            results.append(res)
        return _completed({"results": results})


@pytest.fixture(autouse=True)
def _dll(request, monkeypatch, tmp_path):
    """Unit tests get a fake DLL and a clean cache; tests that use the
    ``real_engine`` fixture (the live ones) keep the machine's real DLL."""
    engine.clear_caches()
    if "real_engine" not in request.fixturenames:
        dll = tmp_path / "Microsoft.AnalysisServices.AdomdClient.dll"
        dll.write_bytes(b"MZ")
        monkeypatch.setenv("PBI_ADOMD_DLL", str(dll))
        monkeypatch.setattr(engine, "IS_WINDOWS", True)
    yield
    engine.clear_caches()


@pytest.fixture
def real_engine():
    """Marker fixture: use the real ADOMD DLL and platform (live tests)."""
    return None


def _fake_project(*names):
    return SimpleNamespace(list_tables=lambda: [SimpleNamespace(name=n) for n in names],
                           path="fake.pbip")


# --- result parsing ----------------------------------------------------------------

def test_execute_dax_parses_columns_rows_and_types(monkeypatch):
    doc = {"results": [_result(
        ["Sales[Id]", "Sales[When]", "Sales[Amount]", "Sales[Flag]"],
        [[1, "2024-01-15T00:00:00.0000000", 120.5, True],
         [None, None, 0, False]], truncated=True)]}
    monkeypatch.setattr(engine, "_run_powershell", lambda a, t: _completed(doc))
    res = engine.execute_dax("Data Source=localhost:1234", "EVALUATE Sales", max_rows=2)
    assert res["columns"] == ["Sales[Id]", "Sales[When]", "Sales[Amount]", "Sales[Flag]"]
    assert res["rows"][0] == [1, "2024-01-15T00:00:00.0000000", 120.5, True]
    assert res["rows"][1][0] is None and res["rows"][1][1] is None   # DBNull -> None
    assert res["row_count"] == 2 and res["truncated"] is True and res["elapsed_ms"] == 7


def test_legacy_json_dates_become_iso(monkeypatch):
    doc = {"results": [_result(["d"], [["/Date(1705276800000)/"]])]}
    monkeypatch.setattr(engine, "_run_powershell", lambda a, t: _completed(doc))
    res = engine.execute_dax("Data Source=localhost:1", "EVALUATE ROW(\"d\", NOW())")
    assert res["rows"][0][0] == "2024-01-15T00:00:00"


def test_stray_output_around_json_is_tolerated(monkeypatch):
    text = "WARNING: something\n" + json.dumps({"results": [_result(["v"], [[2]])]}) + "\n"
    monkeypatch.setattr(engine, "_run_powershell", lambda a, t: _completed(text))
    assert engine.execute_dax("Data Source=localhost:1", "EVALUATE ROW(\"v\", 2)")["rows"] == [[2]]


def test_query_and_connection_string_travel_in_the_request_file(monkeypatch):
    runner = FakeRunner()
    monkeypatch.setattr(engine, "_run_powershell", runner)
    engine.execute_dax("Data Source=x;Password=s3cret", "EVALUATE ROW(\"v\", 1)", max_rows=5)
    req = runner.requests[0]
    assert req["queries"] == [{"connection_string": "Data Source=x;Password=s3cret",
                               "query": "EVALUATE ROW(\"v\", 1)", "max_rows": 5}]
    assert req["dll"].endswith("AdomdClient.dll")
    joined = " ".join(runner.calls[0])
    assert "s3cret" not in joined and "EVALUATE" not in joined
    assert runner.calls[0][:1] == ["-File"]


def test_execute_dax_rejects_bad_input():
    with pytest.raises(ValueError):
        engine.execute_dax("Data Source=localhost:1", "   ")
    with pytest.raises(ValueError):
        engine.execute_dax("Data Source=localhost:1", "EVALUATE T", max_rows=0)
    with pytest.raises(ValueError):
        engine.execute_dax("", "EVALUATE T")


# --- error propagation -------------------------------------------------------------

def test_no_json_output_is_engine_unavailable(monkeypatch):
    monkeypatch.setattr(engine, "_run_powershell",
                        lambda a, t: _completed("", returncode=1, stderr="boom"))
    with pytest.raises(engine.EngineUnavailable) as exc:
        engine.execute_dax("Data Source=localhost:1", "EVALUATE T")
    assert "boom" in str(exc.value) and "PBI_ADOMD_DLL" in str(exc.value)


def test_dll_load_failure_is_engine_unavailable(monkeypatch):
    doc = {"error": "cannot load ADOMD client from x.dll: bad image", "stage": "load"}
    monkeypatch.setattr(engine, "_run_powershell", lambda a, t: _completed(doc, 2))
    with pytest.raises(engine.EngineUnavailable) as exc:
        engine.execute_dax("Data Source=localhost:1", "EVALUATE T")
    assert "bad image" in str(exc.value) and "PBI_ADOMD_DLL" in str(exc.value)


def test_connect_failure_is_engine_unavailable_and_redacted(monkeypatch):
    doc = {"results": [{"error": "connection refused", "stage": "connect"}]}
    monkeypatch.setattr(engine, "_run_powershell", lambda a, t: _completed(doc))
    with pytest.raises(engine.EngineUnavailable) as exc:
        engine.execute_dax("Data Source=localhost:9;Password=hunter2", "EVALUATE T")
    msg = str(exc.value)
    assert "connection refused" in msg and "Power BI Desktop" in msg
    assert "hunter2" not in msg and "Password=***" in msg


def test_execute_failure_is_query_error(monkeypatch):
    doc = {"results": [{"error": "The syntax for 'SUM' is incorrect.", "stage": "execute"}]}
    monkeypatch.setattr(engine, "_run_powershell", lambda a, t: _completed(doc))
    with pytest.raises(engine.QueryError, match="syntax"):
        engine.execute_dax("Data Source=localhost:1", "EVALUATE ROW(\"v\", SUM(")


def test_timeout_is_engine_unavailable(monkeypatch):
    def boom(args, timeout):
        raise subprocess.TimeoutExpired(args, timeout)
    monkeypatch.setattr(engine, "_run_powershell", boom)
    with pytest.raises(engine.EngineUnavailable, match="did not answer within 3"):
        engine.execute_dax("Data Source=localhost:1", "EVALUATE T", timeout=3)


def test_missing_powershell_is_engine_unavailable(monkeypatch):
    def boom(args, timeout):
        raise FileNotFoundError("powershell.exe")
    monkeypatch.setattr(engine, "_run_powershell", boom)
    with pytest.raises(engine.EngineUnavailable, match="powershell"):
        engine.execute_dax("Data Source=localhost:1", "EVALUATE T")


def test_result_count_mismatch_is_engine_unavailable(monkeypatch):
    monkeypatch.setattr(engine, "_run_powershell", lambda a, t: _completed({"results": []}))
    with pytest.raises(engine.EngineUnavailable):
        engine.execute_dax("Data Source=localhost:1", "EVALUATE T")


def test_non_windows_raises_engine_unavailable(monkeypatch):
    monkeypatch.setattr(engine, "IS_WINDOWS", False)
    with pytest.raises(engine.EngineUnavailable, match="Windows"):
        engine.discover_instances()
    with pytest.raises(engine.EngineUnavailable):
        engine.execute_dax("Data Source=localhost:1", "EVALUATE T")
    with pytest.raises(engine.EngineUnavailable):
        engine.find_adomd_dll()


# --- DLL discovery -------------------------------------------------------------------

def test_dll_lookup_order_env_msi_store(monkeypatch, tmp_path):
    env_dll = tmp_path / "env.dll"
    env_dll.write_bytes(b"MZ")
    monkeypatch.setenv("PBI_ADOMD_DLL", str(env_dll))
    assert engine.find_adomd_dll() == str(env_dll)

    engine.clear_caches()
    monkeypatch.delenv("PBI_ADOMD_DLL")
    msi = tmp_path / "msi" / "Microsoft.AnalysisServices.AdomdClient.dll"
    msi.parent.mkdir()
    msi.write_bytes(b"MZ")
    monkeypatch.setattr(engine, "MSI_ADOMD_DLL", str(msi))
    assert engine.find_adomd_dll() == str(msi)

    engine.clear_caches()
    monkeypatch.setattr(engine, "MSI_ADOMD_DLL", str(tmp_path / "missing.dll"))
    store = tmp_path / "store"
    (store / "bin").mkdir(parents=True)
    store_dll = store / "bin" / "Microsoft.PowerBI.AdomdClient.dll"
    store_dll.write_bytes(b"MZ")
    runner = FakeRunner(store_location=str(store))
    monkeypatch.setattr(engine, "_run_powershell", runner)
    assert engine.find_adomd_dll() == str(store_dll)
    assert "Get-AppxPackage" in runner.calls[0][1]
    # cached: no second PowerShell round-trip
    assert engine.find_adomd_dll() == str(store_dll) and len(runner.calls) == 1


def test_dll_not_found_message_says_what_to_do(monkeypatch, tmp_path):
    monkeypatch.setenv("PBI_ADOMD_DLL", str(tmp_path / "nope.dll"))
    monkeypatch.setattr(engine, "MSI_ADOMD_DLL", str(tmp_path / "missing.dll"))
    monkeypatch.setattr(engine, "_run_powershell", FakeRunner(store_location=None))
    with pytest.raises(engine.EngineUnavailable) as exc:
        engine.find_adomd_dll()
    msg = str(exc.value)
    assert "nope.dll" in msg and "missing.dll" in msg and "PBI_ADOMD_DLL" in msg


# --- instance discovery over a fake workspace tree ------------------------------------

def _port_file(root: Path, guid: str, port, bom: bool = True, mtime: float | None = None):
    ws = root / f"AnalysisServicesWorkspace_{guid}" / "Data"
    ws.mkdir(parents=True)
    pf = ws / engine.PORT_FILE
    pf.write_bytes(str(port).encode("utf-16-le" if not bom else "utf-16"))
    if mtime is not None:
        import os
        os.utime(pf, (mtime, mtime))
    return pf


def test_read_port_file_utf16(tmp_path):
    assert engine.read_port_file(_port_file(tmp_path / "a", "g1", 51234)) == 51234
    assert engine.read_port_file(_port_file(tmp_path / "b", "g2", 51235, bom=False)) == 51235
    assert engine.read_port_file(_port_file(tmp_path / "c", "g3", "garbage")) is None
    assert engine.read_port_file(tmp_path / "missing.txt") is None


def _catalog_handler(query, cs, n):
    port = int(cs.split("localhost:")[1].split(";")[0])
    if query == engine.CATALOGS_DMV:
        dbs = {51001: ["db-one"], 51002: ["db-two", "db-three"]}[port]
        return _result(["CATALOG_NAME", "DATE_MODIFIED"], [[d, None] for d in dbs])
    if query == engine.TABLES_DMV:
        db = cs.split("Catalog=")[1]
        tables = {"db-one": ["Sales", "Customer", "LocalDateTable_x"],
                  "db-two": ["Other"], "db-three": ["Sales", "Product", "Customer"]}[db]
        return _result(["ID", "Name"], [[i, t] for i, t in enumerate(tables)])
    raise AssertionError(query)


def test_discover_instances_over_fake_tree(monkeypatch, tmp_path):
    store, msi = tmp_path / "store", tmp_path / "msi"
    _port_file(store, "aaaa", 51001, mtime=1_700_000_000)
    _port_file(msi, "bbbb", 51002, mtime=1_700_000_100)
    _port_file(msi, "stale", 51003)              # port not open -> dropped
    (store / "AnalysisServicesWorkspace_empty").mkdir()   # no port file -> ignored
    monkeypatch.setattr(engine, "workspace_roots", lambda: [store, msi, tmp_path / "absent"])
    monkeypatch.setattr(engine, "_port_open", lambda port, timeout=1.0: port in (51001, 51002))
    runner = FakeRunner(_catalog_handler)
    monkeypatch.setattr(engine, "_run_powershell", runner)

    found = engine.discover_instances()
    assert [i["port"] for i in found] == [51001, 51002]
    one = found[0]
    assert one["workspace_dir"].endswith("AnalysisServicesWorkspace_aaaa")
    assert one["port_file"].endswith(engine.PORT_FILE)
    assert one["port_file_mtime"] == pytest.approx(1_700_000_000, abs=2)
    assert one["databases"] == [{"name": "db-one",
                                 "tables": ["Sales", "Customer", "LocalDateTable_x"]}]
    assert [db["name"] for db in found[1]["databases"]] == ["db-two", "db-three"]
    # ONE PowerShell process: catalogs per instance, tables per catalog (fan-out)
    assert len(runner.requests) == 1
    queries = runner.requests[0]["queries"]
    assert [q["query"] for q in queries] == [engine.CATALOGS_DMV] * 2
    assert [q["connection_string"] for q in queries] == [
        "Data Source=localhost:51001", "Data Source=localhost:51002"]
    assert all(q["then"] == {"query": engine.TABLES_DMV, "max_rows": 100000,
                             "catalog_column": "CATALOG_NAME"} for q in queries)

    with_errors = engine.discover_instances(include_errors=True)
    stale = next(i for i in with_errors if i["port"] == 51003)
    assert stale["reachable"] is False and "not accepting" in stale["error"]


def test_discover_instances_bridge_failure(monkeypatch, tmp_path):
    _port_file(tmp_path / "store", "aaaa", 51001)
    monkeypatch.setattr(engine, "workspace_roots", lambda: [tmp_path / "store"])
    monkeypatch.setattr(engine, "_port_open", lambda port, timeout=1.0: True)
    monkeypatch.setattr(engine, "_run_powershell",
                        lambda a, t: _completed("", returncode=1, stderr="no clr"))
    with pytest.raises(engine.EngineUnavailable, match="no clr"):
        engine.discover_instances()
    [inst] = engine.discover_instances(include_errors=True)     # status never raises
    assert inst["reachable"] is True and "no clr" in inst["error"]


def test_discover_instances_database_level_error(monkeypatch, tmp_path):
    _port_file(tmp_path / "store", "aaaa", 51001)
    monkeypatch.setattr(engine, "workspace_roots", lambda: [tmp_path / "store"])
    monkeypatch.setattr(engine, "_port_open", lambda port, timeout=1.0: True)

    def handler(query, cs, n):
        if query == engine.CATALOGS_DMV:
            return _result(["CATALOG_NAME"], [["good"], ["bad"]])
        if cs.endswith("Catalog=bad"):
            return {"error": "database is offline", "stage": "connect"}
        return _result(["Name"], [["Sales"]])
    monkeypatch.setattr(engine, "_run_powershell", FakeRunner(handler))
    [inst] = engine.discover_instances()
    assert inst["databases"] == [{"name": "good", "tables": ["Sales"]},
                                 {"name": "bad", "tables": [], "error": "database is offline"}]


def test_local_connection_quotes_odd_database_names():
    assert engine.local_connection(1, "plain-guid").connection_string.endswith("Catalog=plain-guid")
    assert engine.local_connection(1, "a;b").connection_string.endswith('Catalog="a;b"')
    assert engine.local_connection(1, 'q"x y').connection_string.endswith('Catalog="q""x y"')


def test_discover_instances_reports_dmv_failure(monkeypatch, tmp_path):
    _port_file(tmp_path / "store", "aaaa", 51001)
    monkeypatch.setattr(engine, "workspace_roots", lambda: [tmp_path / "store"])
    monkeypatch.setattr(engine, "_port_open", lambda port, timeout=1.0: True)
    monkeypatch.setattr(engine, "_run_powershell", FakeRunner(
        lambda q, cs, n: {"error": "not an AS server", "stage": "connect"}))
    assert engine.discover_instances() == []
    [inst] = engine.discover_instances(include_errors=True)
    assert inst["reachable"] is True and inst["error"] == "not an AS server"


def test_discover_instances_without_desktop_folders(monkeypatch, tmp_path):
    monkeypatch.setattr(engine, "workspace_roots", lambda: [tmp_path / "nothing"])
    monkeypatch.setattr(engine, "_run_powershell", FakeRunner())
    assert engine.discover_instances() == []


def test_workspace_roots_cover_every_desktop_build(monkeypatch):
    # POSIX-style fake environment so the assertions hold on any OS (CI runs on Linux)
    monkeypatch.setenv("LOCALAPPDATA", "/users/me/AppData/Local")
    monkeypatch.setenv("USERPROFILE", "/users/me")
    roots = [Path(p).as_posix() for p in engine.workspace_roots()]
    # Store build 2.157: %USERPROFILE%\Microsoft\Power BI Desktop Store App
    assert any(r.endswith("/users/me/Microsoft/Power BI Desktop Store App/AnalysisServicesWorkspaces")
               for r in roots)
    # package-virtualised LocalCache copy
    assert any("Microsoft.MicrosoftPowerBIDesktop_8wekyb3d8bbwe" in r and
               r.endswith("AnalysisServicesWorkspaces") for r in roots)
    # MSI build
    assert any(r.endswith("AppData/Local/Microsoft/Power BI Desktop/AnalysisServicesWorkspaces")
               and "Packages" not in r for r in roots)
    monkeypatch.delenv("USERPROFILE")
    monkeypatch.delenv("LOCALAPPDATA")
    assert engine.workspace_roots() == []


# --- instance matching ---------------------------------------------------------------

def _inst(port, mtime, *dbs):
    return {"port": port, "port_file_mtime": mtime, "workspace_dir": f"ws{port}",
            "databases": [{"name": name, "tables": list(tables)} for name, tables in dbs]}


def test_match_instance_prefers_best_overlap_then_newest_port_file():
    project = _fake_project("Sales", "Customer", "Product")
    instances = [
        _inst(1, 10.0, ("partial", ["Sales", "LocalDateTable_1"])),
        _inst(2, 20.0, ("full-old", ["Sales", "Customer", "Product"])),
        _inst(3, 30.0, ("full-new", ["Sales", "Customer", "Product"]),
              ("unrelated", ["Foo"])),
    ]
    match = engine.match_instance(project, instances)
    assert match["port"] == 3 and match["database"] == "full-new"
    assert match["exact"] is True and match["missing"] == [] and match["extra"] == []
    assert match["overlap"] == 3

    partial = engine.match_instance(project, instances[:1])
    assert partial["database"] == "partial" and partial["missing"] == ["Customer", "Product"]
    assert partial["exact"] is False and partial["extra"] == []   # internal tables ignored


def test_match_instance_returns_none_without_overlap():
    project = _fake_project("Sales")
    assert engine.match_instance(project, [_inst(1, 1.0, ("x", ["Foo", "Bar"]))]) is None
    assert engine.match_instance(project, []) is None
    assert engine.match_instance(_fake_project(), [_inst(1, 1.0, ("x", ["Sales"]))]) is None


def test_match_instance_with_real_project():
    project = PbipProject(ENGINE_PBIP)
    inst = _inst(7, 1.0, ("guid-db", ["Customer", "Product", "Sales"]))
    assert engine.match_instance(project, [inst])["exact"] is True


# --- DAX helpers ---------------------------------------------------------------------------

def test_probe_query_construction():
    assert engine.probe_query("SUM(Sales[Amount])") == 'EVALUATE ROW("v", SUM(Sales[Amount]))'
    q = engine.probe_query("  DIVIDE([A], [B])\n", table="O'Brien Sales")
    assert q == ("DEFINE MEASURE 'O''Brien Sales'[__pbi_mcp_probe] = DIVIDE([A], [B])\n"
                 'EVALUATE ROW("v", [__pbi_mcp_probe])')
    with pytest.raises(ValueError):
        engine.probe_query("  ")
    assert engine.dax_table_ref("Sales") == "'Sales'"


def test_probe_query_rejects_full_queries():
    for text in ("EVALUATE ROW(\"v\", 1)", "  evaluate Sales", "DEFINE MEASURE T[m] = 1"):
        with pytest.raises(ValueError, match="pbi_evaluate_dax"):
            engine.probe_query(text)
    # a name that merely starts with those letters is a normal expression
    assert engine.probe_query("EVALUATED + 1") == 'EVALUATE ROW("v", EVALUATED + 1)'


def test_validate_dax_sends_probe_and_maps_errors(monkeypatch):
    seen: list[str] = []

    def handler(query, cs, n):
        seen.append(query)
        if "BAD(" in query:
            return {"error": "The syntax for 'BAD' is incorrect.", "stage": "execute"}
        return _result(["[v]"], [[42]])

    monkeypatch.setattr(engine, "_run_powershell", FakeRunner(handler))
    good = engine.validate_dax("Data Source=localhost:1", "1 + 41", table="Sales")
    assert good == {"ok": True, "error": None, "value": 42, "elapsed_ms": 7,
                    "query": engine.probe_query("1 + 41", "Sales")}
    bad = engine.validate_dax("Data Source=localhost:1", "BAD(")
    assert bad["ok"] is False and "BAD" in bad["error"]
    assert seen == [engine.probe_query("1 + 41", "Sales"), 'EVALUATE ROW("v", BAD()']


def test_validate_dax_error_positions_point_into_the_expression(monkeypatch):
    # column 47 of "DEFINE MEASURE 'Sales'[__pbi_mcp_probe] = SUM(Sales[Nope])" is the S of
    # Sales[Nope], the 5th character of the caller's expression
    msg = ("Query (1, 47) Column 'Nope' in table 'Sales' cannot be found or may not be "
           "used in this expression.")
    monkeypatch.setattr(engine, "_run_powershell", FakeRunner(
        lambda q, cs, n: {"error": msg, "stage": "execute"}))
    bad = engine.validate_dax("Data Source=localhost:1", "SUM(Sales[Nope])", table="Sales")
    assert bad["error"].startswith("Expression (1, 5) Column 'Nope'")
    # no table: the prefix is 'EVALUATE ROW("v", ' (18 characters)
    msg2 = "Query (1, 19) The value for 'Nope' cannot be determined."
    monkeypatch.setattr(engine, "_run_powershell", FakeRunner(
        lambda q, cs, n: {"error": msg2, "stage": "execute"}))
    assert engine.validate_dax("Data Source=localhost:1", "[Nope] + 1")["error"].startswith(
        "Expression (1, 1) The value")
    # later lines keep their own column; positions inside the prefix are left alone
    assert engine._relocate_positions("Query (3, 9) x", 40) == "Expression (3, 9) x"
    assert engine._relocate_positions("Query (1, 3) x", 40) == "Query (1, 3) x"
    # positions on the wrapper's own lines (beyond the expression) are not remapped
    assert engine._relocate_positions("Query (4, 2) x", 40, expr_lines=3) == "Query (4, 2) x"


def test_validate_dax_unbalanced_expression_gets_a_clear_message(monkeypatch):
    # what the engine really says when the closing parenthesis is missing (captured live):
    # it blames the wrapper's EVALUATE line and echoes the composed query
    msg = ("Query (2, 1) The syntax for 'EVALUATE' is incorrect. (DEFINE MEASURE "
           "'Sales'[__pbi_mcp_probe] = SUM(Sales[Amount]\nEVALUATE ROW(\"v\", [__pbi_mcp_probe])).")
    monkeypatch.setattr(engine, "_run_powershell", FakeRunner(
        lambda q, cs, n: {"error": msg, "stage": "execute"}))
    bad = engine.validate_dax("Data Source=localhost:1", "SUM(Sales[Amount]", table="Sales")
    assert bad["ok"] is False
    assert bad["error"] == ("The expression is incomplete or unbalanced (a missing ')' or "
                            "argument?). Engine message: The syntax for 'EVALUATE' is incorrect.")
    assert "__pbi_mcp_probe" not in bad["error"]
    # an error inside a multi-line expression keeps its own line and column
    multi = "Query (2, 7) Column 'X' in table 'Sales' cannot be found."
    assert engine._clean_probe_error(multi, 42, expr_lines=3) == (
        "Expression (2, 7) Column 'X' in table 'Sales' cannot be found.")
    # ... and the throw-away measure's name (captured live) never reaches the caller
    live = ("Query (2, 3) Calculation error in measure 'Sales'[__pbi_mcp_probe]: The value "
            "for 'Nope' cannot be determined.")
    assert engine._clean_probe_error(live, 42, expr_lines=2) == (
        "Expression (2, 3) Calculation error in the measure: The value for 'Nope' "
        "cannot be determined.")
    assert "__pbi_mcp_probe" not in engine._clean_probe_error(
        "Something about [__pbi_mcp_probe] failed", 42, 1)


def _storage_tables_handler(rows, names):
    def handler(query, cs, n):
        if query == engine.STORAGE_TABLES_DMV:
            return _result(["DIMENSION_NAME", "TABLE_ID", "ROWS_COUNT"], rows)
        assert query == engine.TABLES_DMV
        return _result(["Name"], [[n_] for n_ in names])
    return handler


def test_table_row_counts_from_storage_dmv(monkeypatch):
    runner = FakeRunner(_storage_tables_handler(
        [["Sales", "Sales (12)", 8], ["Sales", "H$Sales (12)$Id (3)", 11],
         ["Sales", "R$Sales (12)$abc (19)", 7], ["Customer", "Customer (20)", 4],
         ["LocalDateTable_1", "LocalDateTable_1 (30)", 365]],
        ["Sales", "Customer", "LocalDateTable_1", "Measures Only"]))
    monkeypatch.setattr(engine, "_run_powershell", runner)
    res = engine.table_row_counts("Data Source=localhost:1")
    assert res["source"] == "DISCOVER_STORAGE_TABLES"
    assert res["tables"] == [
        {"name": "Customer", "rows": 4, "internal": False},
        {"name": "Measures Only", "rows": None, "internal": False},   # no storage
        {"name": "Sales", "rows": 8, "internal": False},
        {"name": "LocalDateTable_1", "rows": 365, "internal": True}]
    assert len(runner.requests) == 1          # both DMVs in one PowerShell process


def test_table_row_counts_falls_back_to_countrows(monkeypatch):
    queries: list[str] = []

    def handler(query, cs, n):
        queries.append(query)
        if query == engine.STORAGE_TABLES_DMV:
            return {"error": "The DMV is not available", "stage": "execute"}
        if query == engine.TABLES_DMV:
            return _result(["Name"], [["Sales"], ["Customer"]])
        return _result(["[Table]", "[Rows]"], [["Sales", 8], ["Customer", 4]])
    monkeypatch.setattr(engine, "_run_powershell", FakeRunner(handler))
    res = engine.table_row_counts("Data Source=localhost:1")
    assert res["source"] == "COUNTROWS"
    assert {t["name"]: t["rows"] for t in res["tables"]} == {"Sales": 8, "Customer": 4}
    assert queries[-1].startswith("EVALUATE UNION(") and "COUNTROWS('Sales')" in queries[-1]


def _column_stats_handler(with_storages: bool = True):
    def handler(query, cs, n):
        if query == engine.STORAGE_COLUMNS_DMV:
            return _result(
                ["DIMENSION_NAME", "TABLE_ID", "COLUMN_ID", "COLUMN_TYPE",
                 "ATTRIBUTE_NAME", "DATATYPE", "COLUMN_ENCODING", "DICTIONARY_SIZE"],
                [["Sales", "Sales (12)", "Amount (5)", "BASIC_DATA", "Amount", "DBTYPE_R8", 2, 0],
                 ["Sales", "Sales (12)", "Id (3)", "BASIC_DATA", "Id", "DBTYPE_I8", 1, 128],
                 ["Sales", "Sales (12)", "RowNumber 2662979B 1795 4F74 8F37 6A1BA8059B61 (1)",
                  "BASIC_DATA", "RowNumber-2662979B-1795-4F74-8F37-6A1BA8059B61", "DBTYPE_I8", 2, 144],
                 ["Customer", "Customer (20)", "Name (22)", "BASIC_DATA", "Name", "DBTYPE_WSTR", 1, 300],
                 ["Sales", "H$Sales (12)$Id (3)", "POS_TO_ID", "HIERARCHY_POSITION_TO_DATAID",
                  "Id", "N/A", 0, 0],
                 ["Sales", "R$Sales (12)$abc (19)", "INDEX", "RELATIONSHIP", None, "N/A", 0, 0]])
        if query == engine.STORAGE_SEGMENTS_DMV:
            return _result(
                ["TABLE_ID", "COLUMN_ID", "SEGMENT_NUMBER", "RECORDS_COUNT", "USED_SIZE"],
                [["Sales (12)", "Amount (5)", 0, 8, 40], ["Sales (12)", "Amount (5)", 1, 2, 10],
                 ["Sales (12)", "Id (3)", 0, 10, 24],
                 ["Sales (12)", "RowNumber 2662979B 1795 4F74 8F37 6A1BA8059B61 (1)", 0, 10, 128],
                 ["H$Sales (12)$Id (3)", "POS_TO_ID", 0, 0, 40],
                 ["H$Sales (12)$Id (3)", "POS_TO_ID", 1, 0, 16],
                 ["Customer (20)", "Name (22)", 0, 4, 32],
                 ["R$Sales (12)$abc (19)", "INDEX", 0, 0, 8]])
        assert query == engine.COLUMN_STORAGES_DMV
        if not with_storages:
            return {"error": "Unknown rowset TMSCHEMA_COLUMN_STORAGES", "stage": "execute"}
        return _result(
            ["ID", "ColumnID", "Name", "Statistics_DistinctStates"],
            [[1, 3, "Id (3)", 10], [2, 5, "Amount (5)", 8], [3, 22, "Name (22)", 4],
             [4, 212, "POS_TO_ID", 1]])
    return handler


def test_column_stats_joins_columns_segments_and_storages(monkeypatch):
    runner = FakeRunner(_column_stats_handler())
    monkeypatch.setattr(engine, "_run_powershell", runner)
    res = engine.column_stats("Data Source=localhost:1")
    by = {(c["table"], c["column"]): c for c in res["columns"]}
    # RowNumber columns, hierarchy and relationship structures are not columns
    assert set(by) == {("Sales", "Amount"), ("Sales", "Id"), ("Customer", "Name")}
    assert by[("Sales", "Amount")] == {
        "table": "Sales", "column": "Amount", "data_type": "DBTYPE_R8", "encoding": "VALUE",
        "cardinality": 8, "rows": 10, "dictionary_size": 0, "data_size": 50,
        "hierarchy_size": 0, "total_size": 50}
    idc = by[("Sales", "Id")]
    assert idc["encoding"] == "HASH" and idc["cardinality"] == 10 and idc["rows"] == 10
    assert (idc["dictionary_size"], idc["data_size"], idc["hierarchy_size"]) == (128, 24, 56)
    assert idc["total_size"] == 128 + 24 + 56
    assert by[("Customer", "Name")]["cardinality"] == 4
    assert res["total_size"] == 50 + 208 + 332
    assert [c["column"] for c in res["columns"]] == ["Name", "Id", "Amount"]   # by size desc
    assert res["tables"] == [{"table": "Customer", "columns": 1, "total_size": 332},
                             {"table": "Sales", "columns": 2, "total_size": 258}]
    assert len(runner.requests) == 1          # three DMVs, one PowerShell process

    only = engine.column_stats("Data Source=localhost:1", table="Customer")
    assert [c["column"] for c in only["columns"]] == ["Name"]
    with pytest.raises(ValueError, match="not in the live model"):
        engine.column_stats("Data Source=localhost:1", table="Nope")


def test_column_stats_without_column_storages_dmv(monkeypatch):
    monkeypatch.setattr(engine, "_run_powershell", FakeRunner(_column_stats_handler(False)))
    res = engine.column_stats("Data Source=localhost:1")
    assert all(c["cardinality"] is None for c in res["columns"])
    assert len(res["columns"]) == 3           # the optional DMV is not required


def test_preview_table_query(monkeypatch):
    runner = FakeRunner(lambda q, cs, n: _result(["Sales[Id]"], [[1], [2]]))
    monkeypatch.setattr(engine, "_run_powershell", runner)
    res = engine.preview_table("Data Source=localhost:1", "Sales", top=2)
    assert res["query"] == "EVALUATE TOPN(2, 'Sales')" and res["table"] == "Sales"
    assert runner.requests[0]["queries"][0]["max_rows"] == 2
    with pytest.raises(ValueError):
        engine.preview_table("Data Source=localhost:1", "Sales", top=0)


# --- connections / redaction -------------------------------------------------------------

def test_redact_masks_passwords_case_insensitively():
    cs = ("Data Source=powerbi://api.powerbi.com/v1.0/myorg/WS;Initial Catalog=DS;"
          "User ID=;Password=eyJ0eXAi.secret;pwd=again;Timeout=30")
    out = engine.redact(cs)
    assert "eyJ0eXAi" not in out and "again" not in out
    assert out == ("Data Source=powerbi://api.powerbi.com/v1.0/myorg/WS;Initial Catalog=DS;"
                   "User ID=;Password=***;pwd=***;Timeout=30")
    assert engine.redact("Data Source=localhost:1;Password=") == "Data Source=localhost:1;Password="
    # quoted values (may contain semicolons) are masked whole
    assert engine.redact('A=1;Password="pa;ss";B=2') == "A=1;Password=***;B=2"
    assert engine.redact("A=1;pwd='x;y';B=2") == "A=1;pwd=***;B=2"


def test_secret_values_and_scrub():
    assert engine.secret_values('A=1;Password="pa;ss";pwd=t0k') == ["pa;ss", "t0k"]
    assert engine.secret_values("Data Source=localhost:1") == []
    cs = "Data Source=x;Password=tok3n-123"
    # the value is removed even where the engine echoes it without 'Password='
    assert engine.scrub("login failed for token tok3n-123 (Password=tok3n-123)", cs) == (
        "login failed for token *** (Password=***)")


def test_engine_errors_never_echo_secrets(monkeypatch):
    doc = {"results": [{"error": "bad token tok3n-123 rejected", "stage": "execute"}]}
    monkeypatch.setattr(engine, "_run_powershell", lambda a, t: _completed(doc))
    with pytest.raises(engine.QueryError) as exc:
        engine.execute_dax("Data Source=x;Password=tok3n-123", "EVALUATE T")
    assert "tok3n-123" not in str(exc.value) and "bad token *** rejected" in str(exc.value)
    monkeypatch.setattr(engine, "_run_powershell",
                        lambda a, t: _completed("", 1, stderr="failed with tok3n-123"))
    with pytest.raises(engine.EngineUnavailable) as exc2:
        engine.execute_dax("Data Source=x;Password=tok3n-123", "EVALUATE T")
    assert "tok3n-123" not in str(exc2.value)


def test_local_connection_and_describe():
    conn = engine.local_connection(51234, "guid-db")
    assert conn.connection_string == "Data Source=localhost:51234;Catalog=guid-db"
    assert conn.describe() == {"connection_string": "Data Source=localhost:51234;Catalog=guid-db",
                               "port": 51234, "database": "guid-db", "source": "local"}
    pinned = engine.Connection("Data Source=x;Password=p4ss", source="pinned")
    assert "p4ss" not in pinned.describe()["connection_string"]
    assert "p4ss" not in repr(pinned) and "p4ss" not in str(pinned)


# --- tools_engine ----------------------------------------------------------------------------

@pytest.fixture
def desktop(monkeypatch, tmp_path):
    """One fake Desktop instance hosting the engine fixture's tables."""
    _port_file(tmp_path / "store", "aaaa", 51001)
    monkeypatch.setattr(engine, "workspace_roots", lambda: [tmp_path / "store"])
    monkeypatch.setattr(engine, "_port_open", lambda port, timeout=1.0: port == 51001)
    log: list[tuple[str, str]] = []

    def handler(query, cs, n):
        log.append((query, cs))
        if query == engine.CATALOGS_DMV:
            return _result(["CATALOG_NAME"], [["guid-db"]])
        if query == engine.TABLES_DMV:
            return _result(["Name"], [["Customer"], ["Product"], ["Sales"]])
        if "BAD(" in query:
            return {"error": "The syntax for 'BAD' is incorrect.", "stage": "execute"}
        return _result(["[v]"], [[3]])
    monkeypatch.setattr(engine, "_run_powershell", FakeRunner(handler))
    return log


def _state(project: bool = True):
    from model_server.server import ModelState
    st = ModelState()
    if project:
        st.project = PbipProject(ENGINE_PBIP)
    return st


def test_status_matches_project_to_instance(desktop):
    st = _state()
    status = tools_engine.engine_status(st)
    assert status["ok"] is True and status["dll"].endswith(".dll")
    assert status["matched"]["port"] == 51001 and status["matched"]["database"] == "guid-db"
    assert status["instances"][0]["port"] == 51001 and status["pinned_connection"] is None
    assert "51001" in status["reason"]


def test_status_never_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(engine, "discover_instances", boom)
    monkeypatch.setattr(engine, "find_adomd_dll", boom)
    status = tools_engine.engine_status(_state())
    assert status["ok"] is False
    assert status["dll_error"] == "kaboom" and status["discovery_error"] == "kaboom"
    assert status["reason"] == "kaboom"


def test_status_on_non_windows_explains_itself(monkeypatch):
    monkeypatch.setattr(engine, "IS_WINDOWS", False)
    status = tools_engine.engine_status(_state())          # never raises
    assert status["ok"] is False and "Windows" in status["reason"]
    assert status["instances"] == [] and status["matched"] is None


def test_status_explains_no_instance(monkeypatch, tmp_path):
    monkeypatch.setattr(engine, "workspace_roots", lambda: [tmp_path / "none"])
    monkeypatch.setattr(engine, "_run_powershell", FakeRunner())
    status = tools_engine.engine_status(_state())
    assert status["ok"] is False and "Power BI Desktop" in status["reason"]
    assert status["instances"] == [] and status["matched"] is None


def test_evaluate_dax_uses_matched_instance_and_caches(desktop):
    st = _state()
    res = tools_engine.evaluate_dax(st, 'EVALUATE ROW("v", 3)', max_rows=10)
    assert res["rows"] == [[3]]
    assert res["connection"]["port"] == 51001 and res["connection"]["database"] == "guid-db"
    assert res["connection"]["match"]["exact"] is True
    n = len(desktop)
    tools_engine.evaluate_dax(st, 'EVALUATE ROW("v", 3)')
    assert len(desktop) == n + 1          # no rediscovery within the TTL
    assert desktop[-1][1] == "Data Source=localhost:51001;Catalog=guid-db"


def test_explicit_port_and_database(desktop):
    st = _state(project=False)
    res = tools_engine.evaluate_dax(st, 'EVALUATE ROW("v", 3)', port=51001)
    assert res["connection"] == {"connection_string": "Data Source=localhost:51001;Catalog=guid-db",
                                 "port": 51001, "database": "guid-db", "source": "explicit"}
    res = tools_engine.evaluate_dax(st, 'EVALUATE ROW("v", 3)', port=51001, database="other")
    assert res["connection"]["database"] == "other"
    with pytest.raises(ValueError, match="port 4"):
        tools_engine.evaluate_dax(st, 'EVALUATE ROW("v", 3)', port=4)
    with pytest.raises(ValueError, match="nope"):
        tools_engine.evaluate_dax(st, 'EVALUATE ROW("v", 3)', database="nope")


def test_single_database_used_without_project(desktop):
    st = _state(project=False)
    assert tools_engine.evaluate_dax(st, 'EVALUATE ROW("v", 3)')["connection"]["port"] == 51001
    assert tools_engine.engine_status(st)["ok"] is True


def test_no_overlap_is_engine_unavailable(desktop):
    st = _state(project=False)
    st.project = _fake_project("Unrelated")
    with pytest.raises(engine.EngineUnavailable, match="none hosts the tables"):
        tools_engine.evaluate_dax(st, 'EVALUATE ROW("v", 3)')
    status = tools_engine.engine_status(st)
    assert status["ok"] is False and "no database shares table names" in status["reason"]


def test_switching_project_drops_the_cached_match(desktop):
    st = _state()
    tools_engine.evaluate_dax(st, 'EVALUATE ROW("v", 3)')           # caches the match
    st.project = _fake_project("Unrelated")                           # user selected another project
    with pytest.raises(engine.EngineUnavailable, match="none hosts the tables"):
        tools_engine.evaluate_dax(st, 'EVALUATE ROW("v", 3)')


def test_no_instance_error_says_how_to_start_desktop(monkeypatch, tmp_path):
    monkeypatch.setattr(engine, "workspace_roots", lambda: [tmp_path / "none"])
    monkeypatch.setattr(engine, "_run_powershell", FakeRunner())
    with pytest.raises(engine.EngineUnavailable, match="Open the project in Power BI Desktop"):
        tools_engine.evaluate_dax(_state(), 'EVALUATE ROW("v", 3)')


def test_lost_connection_is_re_resolved_once(desktop, monkeypatch):
    st = _state()
    tools_engine.evaluate_dax(st, 'EVALUATE ROW("v", 3)')
    calls = {"n": 0}
    real = engine.execute_dax

    def flaky(conn, query, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise engine.EngineUnavailable("gone")
        return real(conn, query, **kw)
    monkeypatch.setattr(engine, "execute_dax", flaky)
    assert tools_engine.evaluate_dax(st, 'EVALUATE ROW("v", 3)')["rows"] == [[3]]
    assert calls["n"] == 2


def test_validate_rowcounts_stats_preview_wrappers(desktop, monkeypatch):
    st = _state()
    assert tools_engine.validate_dax(st, "1 + 2", table="Sales")["ok"] is True
    assert tools_engine.validate_dax(st, "BAD(")["ok"] is False
    monkeypatch.setattr(engine, "table_row_counts", lambda c, **k: {"tables": []})
    monkeypatch.setattr(engine, "column_stats", lambda c, t=None, **k: {"columns": [], "t": t})
    monkeypatch.setattr(engine, "preview_table", lambda c, t, top=20, **k: {"rows": [], "top": top})
    assert tools_engine.table_row_counts(st)["connection"]["port"] == 51001
    assert tools_engine.column_stats(st, "Sales")["t"] == "Sales"
    assert tools_engine.preview_table(st, "Sales", 5)["top"] == 5


def test_engine_connect_pins_in_memory_and_redacts(desktop):
    st = _state()
    cs = "Data Source=powerbi://api.powerbi.com/v1.0/myorg/WS;Initial Catalog=DS;User ID=;Password=tok3n"
    res = tools_engine.engine_connect(st, cs)
    assert res["ok"] is True and "tok3n" not in json.dumps(res)
    assert res["pinned"]["source"] == "pinned" and "Password=***" in res["pinned"]["connection_string"]
    assert desktop[-1] == ('EVALUATE ROW("v", 1)', cs)          # probe hit the real string
    out = tools_engine.evaluate_dax(st, 'EVALUATE ROW("v", 3)')
    assert out["connection"]["source"] == "pinned" and desktop[-1][1] == cs
    assert "tok3n" not in json.dumps(tools_engine.engine_status(st))
    assert tools_engine.engine_status(st)["ok"] is True

    assert tools_engine.engine_connect(st, "")["pinned"] is None
    assert tools_engine.evaluate_dax(st, 'EVALUATE ROW("v", 3)')["connection"]["source"] == "local"
    with pytest.raises(ValueError):
        tools_engine.engine_connect(st, "not a connection string")


def test_engine_connect_rejects_unreachable(monkeypatch):
    monkeypatch.setattr(engine, "_run_powershell", FakeRunner(
        lambda q, cs, n: {"error": "no route", "stage": "connect"}))
    with pytest.raises(engine.EngineUnavailable, match="no route"):
        tools_engine.engine_connect(_state(project=False), "Data Source=nowhere;Password=x")


def test_tools_registered_on_model_server_as_read_only():
    import model_server.server as mod
    tools = {t.name: t for t in asyncio.run(mod.mcp.list_tools())}
    expected = {"pbi_engine_status", "pbi_evaluate_dax", "pbi_validate_dax",
                "pbi_table_row_counts", "pbi_column_stats", "pbi_preview_table",
                "pbi_engine_connect"}
    assert expected <= set(tools)
    for name in expected:
        ann = tools[name].annotations
        read_only = getattr(ann, "readOnlyHint", None)
        if read_only is None:
            read_only = getattr(ann, "read_only_hint", None)
        assert read_only is True, name
        schema = getattr(tools[name], "inputSchema", None) or tools[name].input_schema
        assert "dry_run" not in schema.get("properties", {}), name
    assert (tools["pbi_engine_connect"].description or "").count("Password") >= 1


# --- scripts/desktop_launch.py (mocked) ---------------------------------------------------

@pytest.fixture
def launcher():
    import importlib.util
    path = Path(__file__).resolve().parent.parent / "scripts" / "desktop_launch.py"
    spec = importlib.util.spec_from_file_location("desktop_launch_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fake_time(step: float | None = None):
    """A stand-in for the launcher's `time` module: no sleeping; monotonic() either
    advances `step` seconds per call or follows the real clock."""
    if step is None:
        import time as real
        return SimpleNamespace(monotonic=real.monotonic, sleep=lambda s: None)
    ticks = iter(i * step for i in range(10_000))
    return SimpleNamespace(monotonic=lambda: next(ticks), sleep=lambda s: None)


def test_launcher_desktop_pids_parses_get_process_output(launcher, monkeypatch):
    monkeypatch.setattr(launcher, "_ps", lambda cmd, timeout=60.0: (
        "1234|2026-09-29T20:24:02.0000000+05:30\nnoise\n5678|2026-09-29T20:30:00.0000000+05:30"))
    assert launcher.desktop_pids() == {1234: "2026-09-29T20:24:02.0000000+05:30",
                                       5678: "2026-09-29T20:30:00.0000000+05:30"}
    monkeypatch.setattr(launcher, "_ps", lambda cmd, timeout=60.0: "")
    assert launcher.desktop_pids() == {}


def test_launcher_close_answers_the_save_prompt_then_waits(launcher, monkeypatch):
    calls: list = []
    monkeypatch.setattr(launcher, "_run_script", lambda text, *a, **k: calls.append(a) or "answered")
    alive = iter([{7: "t"}, {7: "t"}] + [{}] * 10)     # gone after the prompt is answered
    monkeypatch.setattr(launcher, "desktop_pids", lambda: next(alive))
    stopped: list = []
    monkeypatch.setattr(launcher, "_ps", lambda cmd, timeout=60.0: stopped.append(cmd) or "")
    monkeypatch.setattr(launcher, "time", _fake_time())
    res = launcher.close(7, grace=5, verbose=False)
    assert calls == [("-ProcessId", "7")]
    assert res["closed"] == [7] and res["forced"] == [] and stopped == []


def test_launcher_close_falls_back_to_stop_process(launcher, monkeypatch):
    monkeypatch.setattr(launcher, "_run_script", lambda text, *a, **k: "no-prompt")
    state = {"alive": True}
    monkeypatch.setattr(launcher, "desktop_pids", lambda: {9: "t"} if state["alive"] else {})

    def fake_ps(cmd, timeout=60.0):
        if "Stop-Process" in cmd:
            state["alive"] = False
        return ""
    monkeypatch.setattr(launcher, "_ps", fake_ps)
    monkeypatch.setattr(launcher, "time", _fake_time(20.0))   # every monotonic() call jumps 20 s
    res = launcher.close(9, grace=5, verbose=False)
    assert res == {"closed": [], "forced": [9], "remaining": []}


def test_launcher_refresh_data_clicks_until_rows_arrive(launcher, monkeypatch):
    totals = iter([0, 0, 0, 15])
    monkeypatch.setattr(launcher, "_total_rows", lambda conn: next(totals))
    monkeypatch.setattr(engine, "table_row_counts", lambda conn, **k: {"tables": [
        {"name": "Sales", "rows": 8, "internal": False},
        {"name": "LocalDateTable_x", "rows": 365, "internal": True}]})
    scripts: list = []
    monkeypatch.setattr(launcher, "_run_script", lambda text, *a, **k: scripts.append(a) or "clicked")
    monkeypatch.setattr(launcher, "time", _fake_time(13.0))   # every poll is > `poll` s apart
    res = launcher.refresh_data(1234, "db", timeout=10_000, verbose=False, pid=22296)
    assert res == {"rows": {"Sales": 8}, "clicks": 3}
    assert scripts == [("-ProcessId", "22296")] * 3        # the window that holds the model


def test_launcher_refresh_data_timeout_quotes_desktops_load_error(launcher, monkeypatch):
    monkeypatch.setattr(launcher, "_total_rows", lambda conn: 0)

    def fake_script(text, *a, **k):
        if a == ("-DiagnoseOnly",):
            return ("note: 2 queries are blocked by the following error:\n"
                    "note: A cyclic reference was encountered during evaluation.")
        return "no-banner"
    monkeypatch.setattr(launcher, "_run_script", fake_script)
    monkeypatch.setattr(launcher, "time", _fake_time(60.0))
    with pytest.raises(TimeoutError, match="cyclic reference"):
        launcher.refresh_data(1234, "db", timeout=120, verbose=False)


class _FakeDesktop:
    """A tiny model of Desktop processes + engine instances for launch() tests."""

    def __init__(self, launcher, monkeypatch, running=()):
        self.pids = {p: "t" for p in running}
        self.started: list = []
        self.refreshed: list = []
        self.next_pid = 100
        # workspace folder name -> owning pid; the model window is started after the host
        self.owner = {"AnalysisServicesWorkspace_host": 100, "AnalysisServicesWorkspace_model": 101}
        self.instances = [
            {"port": 1, "port_file_mtime": 1.0, "workspace_dir": "w/AnalysisServicesWorkspace_host",
             "reachable": True, "error": None, "databases": [{"name": "empty", "tables": []}]},
            {"port": 2, "port_file_mtime": 2.0, "workspace_dir": "w/AnalysisServicesWorkspace_model",
             "reachable": True, "error": None,
             "databases": [{"name": "guid-db", "tables": ["Customer", "Product", "Sales"]}]},
            {"port": 3, "port_file_mtime": 3.0, "workspace_dir": "w/AnalysisServicesWorkspace_old",
             "reachable": False, "error": "stale", "databases": []}]
        monkeypatch.setattr(launcher, "desktop_pids", lambda: dict(self.pids))
        monkeypatch.setattr(launcher, "_start", self._start)
        monkeypatch.setattr(launcher, "_store_alias", lambda: Path("PBIDesktopStore.exe"))
        monkeypatch.setattr(launcher, "workspace_pids", lambda: dict(self.owner))
        monkeypatch.setattr(launcher, "HOST_SETTLE_SECONDS", 0)
        monkeypatch.setattr(launcher, "time", _fake_time())
        monkeypatch.setattr(engine, "discover_instances",
                            lambda include_errors=False, **k: self.instances)
        monkeypatch.setattr(launcher, "refresh_data", self._refresh)

    def _start(self, pbip, how):
        self.started.append((None if pbip is None else pbip.name, how))
        self.pids[self.next_pid] = "t"
        self.next_pid += 1

    def _refresh(self, port, db, **kw):
        self.refreshed.append((port, db, kw.get("pid")))
        return {"rows": {}, "clicks": 1}


def test_launcher_starts_a_bare_host_when_desktop_is_not_running(launcher, monkeypatch):
    fake = _FakeDesktop(launcher, monkeypatch)
    info = launcher.launch(ENGINE_PBIP, timeout=5, verbose=False, refresh=True)
    # a cold start with the file hangs on Desktop 2.158: host first, then the project
    assert fake.started == [(None, "store-alias"), ("Engine.pbip", "association")]
    assert (info["port"], info["database"]) == (2, "guid-db")
    assert info["host_pid"] == 100 and info["pid"] == 101      # pid of the window holding the model
    assert info["tables"] == ["Customer", "Product", "Sales"]
    assert fake.refreshed == [(2, "guid-db", 101)] and info["refresh"] == {"rows": {}, "clicks": 1}
    with pytest.raises(FileNotFoundError):
        launcher.launch(ENGINE_PBIP.with_name("Missing.pbip"), verbose=False)


def test_launcher_host_modes(launcher, monkeypatch):
    # Desktop already running: open straight into it, no second host
    fake = _FakeDesktop(launcher, monkeypatch, running=[100])
    fake.next_pid = 101
    info = launcher.launch(ENGINE_PBIP, timeout=5, verbose=False)
    assert fake.started == [("Engine.pbip", "association")]
    assert "host_pid" not in info and "refresh" not in info and info["pid"] == 101
    # host=False: cold start even though nothing is running
    fake = _FakeDesktop(launcher, monkeypatch)
    fake.owner["AnalysisServicesWorkspace_model"] = 100
    info = launcher.launch(ENGINE_PBIP, timeout=5, verbose=False, host=False)
    assert fake.started == [("Engine.pbip", "association")] and info["pid"] == 100
    # host=True: a fresh host even though Desktop is running
    fake = _FakeDesktop(launcher, monkeypatch, running=[7])
    launcher.launch(ENGINE_PBIP, timeout=5, verbose=False, host=True)
    assert fake.started == [(None, "store-alias"), ("Engine.pbip", "association")]


def test_launcher_workspace_pids_come_from_msmdsrv_command_lines(launcher, monkeypatch):
    guid = "6ab821e5-842e-4277-ae5e-07b4b614ee22"
    out = "\n".join([
        f'5544|"C:\\Program Files\\WindowsApps\\X\\bin\\msmdsrv.exe" -c -n AnalysisServicesWorkspace_{guid}'
        f' -s "C:\\Users\\me\\Microsoft\\Power BI Desktop Store App\\AnalysisServicesWorkspaces'
        f'\\AnalysisServicesWorkspace_{guid}\\Data"',
        '6001|"msmdsrv.exe" -c -n AnalysisServicesWorkspace_ABC-123 -s "C:\\x\\Data"',
        "garbage line without a pid",
    ])
    monkeypatch.setattr(launcher, "_ps", lambda cmd, timeout=60.0: out)
    assert launcher.workspace_pids() == {f"AnalysisServicesWorkspace_{guid}": 5544,
                                         "AnalysisServicesWorkspace_ABC-123": 6001}
    assert launcher.pid_for_workspace("AnalysisServicesWorkspace_ABC-123") == 6001
    assert launcher.pid_for_workspace("AnalysisServicesWorkspace_missing") is None


def test_launcher_cli_close(launcher, monkeypatch, capsys):
    monkeypatch.setattr(launcher, "close", lambda pid=None: {"closed": [1], "forced": [], "remaining": []})
    assert launcher.main(["--close"]) == 0
    assert json.loads(capsys.readouterr().out)["closed"] == [1]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell only")
def test_embedded_powershell_scripts_are_syntactically_valid(launcher, tmp_path):
    """The bridge and launcher scripts only otherwise run against a live Desktop."""
    check = ("$e = $null; $t = $null; "
             "[void][System.Management.Automation.Language.Parser]::ParseFile("
             "$args[0], [ref]$t, [ref]$e); "
             "if ($e.Count) { $e | ForEach-Object { $_.Message + ' @ line ' + $_.Extent.StartLineNumber }; exit 1 }")
    checker = tmp_path / "check.ps1"
    checker.write_text(check, encoding="utf-8-sig")
    for name, text in (("bridge", engine._PS_SCRIPT), ("refresh", launcher._PS_REFRESH),
                       ("close", launcher._PS_CLOSE)):
        script = tmp_path / f"{name}.ps1"
        script.write_text(text, encoding="utf-8-sig")
        try:
            proc = engine._run_powershell(["-File", str(checker), str(script)], 60)
        except (OSError, subprocess.TimeoutExpired):
            pytest.skip("powershell.exe is not usable here")
        assert proc.returncode == 0, f"{name}: {engine._decode(proc.stdout)}"


# --- live tests (need a running Power BI Desktop) ------------------------------------------

def _live_instances() -> list[dict]:
    if sys.platform != "win32":
        return []
    try:
        engine.clear_caches()
        return [i for i in engine.discover_instances()
                if any(db["tables"] for db in i["databases"])]
    except engine.EngineError:
        return []


LIVE = _live_instances()
live = pytest.mark.skipif(not LIVE, reason="no Power BI Desktop instance running")


@pytest.fixture
def live_conn(real_engine, monkeypatch):
    """Connection to the running instance; prefers the Engine fixture's model."""
    monkeypatch.delenv("PBI_ADOMD_DLL", raising=False)
    engine.clear_caches()
    match = engine.match_instance(PbipProject(ENGINE_PBIP), LIVE)
    if match is not None:
        port, name = match["port"], match["database"]
    else:
        inst = LIVE[0]
        port, name = inst["port"], next(d["name"] for d in inst["databases"] if d["tables"])
    tables = next(d["tables"] for i in LIVE if i["port"] == port
                  for d in i["databases"] if d["name"] == name)
    conn = engine.local_connection(port, name)
    conn.tables = [t for t in tables if not engine.is_internal_table(t)]
    conn.is_fixture = bool(match and match["exact"])
    return conn


def _need_fixture_data(conn):
    """Skip unless the running model is the Engine fixture *with data loaded*."""
    if not conn.is_fixture:
        pytest.skip("the running model is not tests/fixtures/engine/Engine.pbip")
    rows = {t["name"]: t["rows"] for t in engine.table_row_counts(conn)["tables"]}
    if rows.get("Sales") != 8:
        pytest.skip("Engine.pbip is open but not refreshed (click 'Refresh now' in Desktop, "
                    "or run scripts/desktop_launch.py --refresh)")


@live
def test_live_evaluate_query(live_conn):
    res = engine.execute_dax(live_conn, 'EVALUATE ROW("v", 1 + 1, "d", DATE(2024, 1, 15), "n", BLANK())')
    assert res["row_count"] == 1 and res["truncated"] is False
    assert res["rows"][0][0] == 2
    assert str(res["rows"][0][1]).startswith("2024-01-15")
    assert res["rows"][0][2] is None
    assert res["elapsed_ms"] >= 0


@live
def test_live_value_types_round_trip(live_conn):
    # accent, quotes, backslash and a non-BMP character (kept ASCII in this source file)
    text = "h" + chr(0xE9) + 'llo "q" \\ ' + chr(0x1F600)
    literal = '"' + text.replace('"', '""') + '"'              # DAX doubles quotes
    res = engine.execute_dax(
        live_conn,
        f'EVALUATE ROW("i", 9007199254740993, "f", 1 / 3, "s", {literal}, "b", TRUE(), '
        '"dt", DATE(2024, 1, 15) + TIME(13, 45, 30), "inf", 1 / 0)')
    i, f, s, b, dt, inf = res["rows"][0]
    assert i == 9007199254740993 and isinstance(i, int)         # beyond 2**53, still exact
    assert f == pytest.approx(1 / 3, rel=1e-15)                 # full double precision
    assert s == text
    assert b is True
    assert dt == "2024-01-15T13:45:30"
    assert inf == "Infinity"


@live
def test_live_truncation_flag(live_conn):
    res = engine.execute_dax(live_conn, "EVALUATE GENERATESERIES(1, 10)", max_rows=4)
    assert res["row_count"] == 4 and res["truncated"] is True
    res = engine.execute_dax(live_conn, "EVALUATE GENERATESERIES(1, 4)", max_rows=4)
    assert res["row_count"] == 4 and res["truncated"] is False


@live
def test_live_bad_query_is_a_query_error(live_conn):
    with pytest.raises(engine.QueryError, match="(?i)table|column|syntax|end of the input"):
        engine.execute_dax(live_conn, "EVALUATE 'No Such Table'")


@live
def test_live_validate_good_and_bad_measure(live_conn):
    table = live_conn.tables[0]
    good = engine.validate_dax(live_conn, f"COUNTROWS({engine.dax_table_ref(table)})", table=table)
    assert good["ok"] is True and good["error"] is None
    bad = engine.validate_dax(live_conn, "SUM(", table=table)
    assert bad["ok"] is False and bad["error"]
    bad2 = engine.validate_dax(live_conn, "[__no_such_measure__] + 1")
    assert bad2["ok"] is False and "__no_such_measure__" in bad2["error"]


@live
def test_live_row_counts_stats_preview(live_conn):
    counts = engine.table_row_counts(live_conn)
    names = {t["name"] for t in counts["tables"]}
    assert set(live_conn.tables) <= names
    assert all(isinstance(t["rows"], int) and t["rows"] >= 0 for t in counts["tables"])

    stats = engine.column_stats(live_conn)
    assert isinstance(stats["columns"], list) and stats["total_size"] >= 0
    for col in stats["columns"]:
        assert not col["column"].startswith("RowNumber")
        assert col["table"] in names
        assert col["encoding"] in ("HASH", "VALUE") or col["encoding"] is None
        assert col["total_size"] == (col["dictionary_size"] + col["data_size"]
                                     + col["hierarchy_size"])
        assert col["cardinality"] is None or col["cardinality"] >= 0
    sizes = [c["total_size"] for c in stats["columns"]]
    assert sizes == sorted(sizes, reverse=True)

    table = live_conn.tables[0]
    preview = engine.preview_table(live_conn, table, top=5)
    assert preview["row_count"] <= 5 and preview["columns"]
    assert all(c.startswith(f"{table}[") for c in preview["columns"])


@live
def test_live_engine_fixture_values(live_conn):
    """Exact numbers for tests/fixtures/engine (skipped unless it is open and refreshed)."""
    _need_fixture_data(live_conn)
    counts = {t["name"]: t["rows"] for t in engine.table_row_counts(live_conn)["tables"]}
    assert counts == {"Customer": 4, "Product": 3, "Sales": 8}

    row = engine.execute_dax(
        live_conn, 'EVALUATE ROW("t", [Total Amount], "n", [Order Count], "a", [Average Amount])'
    )["rows"][0]
    assert row[0] == pytest.approx(936.24) and row[1] == 8 and row[2] == pytest.approx(117.03)

    ok = engine.validate_dax(live_conn, "SUM(Sales[Amount])", table="Sales")
    assert ok["ok"] is True and ok["value"] == pytest.approx(936.24)
    bad = engine.validate_dax(live_conn, "SUM(Sales[Nope])", table="Sales")
    assert bad["ok"] is False and "Nope" in bad["error"]
    assert bad["error"].startswith("Expression (1, 5)")          # points into the caller's DAX

    stats = engine.column_stats(live_conn)
    by = {(c["table"], c["column"]): c for c in stats["columns"]}
    assert len(by) == 12                                          # 3 + 4 + 5 columns, no RowNumber
    assert by[("Customer", "CustomerId")]["cardinality"] == 4
    assert by[("Product", "Category")]["cardinality"] == 2
    assert by[("Sales", "Amount")]["cardinality"] == 8
    assert by[("Sales", "ProductId")]["cardinality"] == 3
    assert by[("Customer", "Name")]["encoding"] == "HASH"
    assert by[("Sales", "Id")]["encoding"] == "VALUE"
    assert by[("Sales", "Amount")]["rows"] == 8

    preview = engine.preview_table(live_conn, "Customer", top=2)
    assert preview["columns"] == ["Customer[CustomerId]", "Customer[Name]", "Customer[Region]"]
    assert preview["row_count"] == 2 and preview["rows"][0][0] == 1


@live
def test_live_tools_through_state(real_engine, monkeypatch):
    monkeypatch.delenv("PBI_ADOMD_DLL", raising=False)
    engine.clear_caches()
    st = _state()
    status = tools_engine.engine_status(st)
    assert status["dll"] and status["instances"]
    if not status["matched"]:
        pytest.skip("Engine.pbip is not the model running in Desktop")
    assert status["ok"] is True and status["matched"]["exact"] is True
    res = tools_engine.evaluate_dax(st, 'EVALUATE ROW("v", [Total Amount])')
    assert res["row_count"] == 1 and res["connection"]["source"] == "local"
    counts = tools_engine.table_row_counts(st)
    assert {"Customer", "Product", "Sales"} <= {t["name"] for t in counts["tables"]}
    assert tools_engine.validate_dax(st, "COUNTROWS(Sales)", "Sales")["ok"] is True
    assert tools_engine.column_stats(st, "Sales")["columns"]
    assert tools_engine.preview_table(st, "Product", 3)["columns"]
