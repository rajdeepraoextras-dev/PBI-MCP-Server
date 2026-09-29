"""core/doctor + pbi_doctor: environment and project health, never raising.

Windows-only probes (PowerShell, %LOCALAPPDATA%) are exercised with
monkeypatched subprocess and paths, so the suite is hermetic on any OS.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import platform
import shutil
import subprocess
from pathlib import Path

import pytest

from core import doctor, journal
from core.mcp_compat import MCP_MAJOR
from core.pbip import PbipProject

SYNTH = Path(__file__).parent / "fixtures" / "synthetic"
REAL_POWERSHELL = doctor._powershell


@pytest.fixture(autouse=True)
def hermetic(monkeypatch, tmp_path):
    """No real PowerShell and no Windows-specific probing unless a test opts in."""
    monkeypatch.setattr(doctor, "_is_windows", lambda: False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))

    def no_shell(*_a, **_k):  # pragma: no cover - guard
        raise AssertionError("a test reached the real PowerShell")

    monkeypatch.setattr(doctor, "_powershell", no_shell)


@pytest.fixture
def root(tmp_path) -> Path:
    dst = tmp_path / "proj"
    shutil.copytree(SYNTH, dst)
    return dst


def _project(root: Path) -> PbipProject:
    return PbipProject(root / "Synthetic.pbip", backups=False)


def _by_check(result: dict, check: str) -> list[dict]:
    return [f for f in result["findings"] if f["check"] == check]


def _payload(result):
    """Tool result -> python object, across mcp majors."""
    structured = None
    if isinstance(result, tuple):           # mcp 1.x: (content, structured)
        result, structured = result
    structured = (structured
                  or getattr(result, "structured_content", None)
                  or getattr(result, "structuredContent", None))
    if structured is not None:
        return (structured.get("result", structured)
                if isinstance(structured, dict) else structured)
    content = getattr(result, "content", result)
    items = [json.loads(c.text) for c in content]
    return items[0] if len(items) == 1 else items


# --- environment ---------------------------------------------------------------

def test_without_a_project_only_the_environment_is_checked():
    res = doctor.run(None)
    assert res["ok"] is True
    assert res["project"] == {"selected": False}
    checks = {f["check"] for f in res["findings"]}
    assert {"python", "mcp", "pbi_mcp", "schema_validation", "project"} <= checks
    env = res["environment"]
    assert env["python"]["version"] == platform.python_version()
    assert env["mcp"]["major"] == MCP_MAJOR
    assert env["mcp"]["version"] == importlib.metadata.version("mcp")
    assert env["pbi_mcp"]["version"] == importlib.metadata.version("pbi-mcp")
    hint = _by_check(res, "project")[0]
    assert "pbi_set_project" in hint["fix"]


def test_result_is_json_serialisable():
    json.dumps(doctor.run(None))


def test_ok_is_false_only_for_error_findings():
    rep = doctor._Report()
    rep.add("warning", "x", "just a warning")
    assert rep.result()["ok"] is True
    rep.add("error", "y", "broken", fix="repair it")
    res = rep.result()
    assert res["ok"] is False
    assert res["findings"][1]["fix"] == "repair it"


def test_a_failing_check_becomes_a_warning_not_an_exception(monkeypatch):
    def boom(_rep):
        raise RuntimeError("kaput")

    monkeypatch.setattr(doctor, "_check_python", boom)
    res = doctor.run(None)
    failed = _by_check(res, "python")
    assert failed and failed[0]["severity"] == "warning"
    assert "RuntimeError" in failed[0]["message"] and "kaput" in failed[0]["message"]
    assert res["ok"] is True
    assert _by_check(res, "mcp")           # later checks still ran


def test_python_below_the_minimum_is_an_error(monkeypatch):
    monkeypatch.setattr(doctor, "MIN_PYTHON", (99, 0))
    res = doctor.run(None)
    assert res["ok"] is False
    finding = _by_check(res, "python")[0]
    assert finding["severity"] == "error" and "99.0" in finding["message"]


# --- project formats -----------------------------------------------------------

def test_healthy_synthetic_project(root):
    project = _project(root)
    res = doctor.run(project, server="pbi-report")
    assert res["ok"] is True
    assert not [f for f in res["findings"] if f["severity"] == "error"]
    p = res["project"]
    assert p["selected"] is True
    assert p["report_format"] == "PBIR"
    assert p["model_format"] == "TMDL"
    tables = project.list_tables()
    assert p["counts"] == {
        "tables": len(tables),
        "columns": sum(len(t.columns) for t in tables),
        "measures": sum(len(t.measures) for t in tables),
        "pages": len(project.list_pages()),
        "visuals": sum(pg.visual_count for pg in project.list_pages()),
    }
    assert p["counts"]["tables"] > 0 and p["counts"]["visuals"] > 0
    assert p["backups"] == {"count": 0, "bytes": 0}
    assert p["journal"]["entries"] == 0
    assert p["trash"] == {"count": 0}
    assert p["pbip_file"].endswith("Synthetic.pbip")


def test_project_may_be_selected_via_a_layer_folder(root):
    res = doctor.run(PbipProject(root / "Synthetic.Report"))
    assert res["project"]["root"] == str(root)
    assert res["project"]["model_format"] == "TMDL"


def _legacy_root(tmp_path: Path, *, report: str, model: str) -> Path:
    """A project whose report/model is 'pbir'|'legacy'."""
    root = tmp_path / "legacy"
    (root / "L.pbip").parent.mkdir(parents=True)
    (root / "L.pbip").write_text("{}", encoding="utf-8")
    rdir, mdir = root / "L.Report", root / "L.SemanticModel"
    rdir.mkdir()
    mdir.mkdir()
    if report == "pbir":
        (rdir / "definition" / "pages").mkdir(parents=True)
        (rdir / "definition" / "report.json").write_text("{}", encoding="utf-8")
    else:
        (rdir / "report.json").write_text("{}", encoding="utf-8")
    if model == "tmdl":
        (mdir / "definition" / "tables").mkdir(parents=True)
        (mdir / "definition" / "model.tmdl").write_text("model Model\n",
                                                        encoding="utf-8")
    else:
        (mdir / "model.bim").write_text("{}", encoding="utf-8")
    return root


def test_legacy_report_is_an_error_for_the_report_server_only(tmp_path):
    project = PbipProject(_legacy_root(tmp_path, report="legacy", model="tmdl"))
    on_report = doctor.run(project, server="pbi-report")
    assert on_report["project"]["report_format"] == "legacy"
    assert on_report["ok"] is False
    finding = _by_check(on_report, "report_format")[0]
    assert finding["severity"] == "error" and "PBIR" in finding["fix"]

    on_model = doctor.run(project, server="pbi-model")
    assert _by_check(on_model, "report_format")[0]["severity"] == "warning"
    assert on_model["ok"] is True

    assert doctor.run(project)["ok"] is False        # no server: strictest


def test_legacy_model_bim_is_an_error_for_the_model_server_only(tmp_path):
    project = PbipProject(_legacy_root(tmp_path, report="pbir", model="legacy"))
    on_model = doctor.run(project, server="pbi-model")
    assert on_model["project"]["model_format"] == "legacy"
    assert on_model["ok"] is False
    finding = _by_check(on_model, "model_format")[0]
    assert finding["severity"] == "error" and "TMDL" in finding["fix"]

    on_report = doctor.run(project, server="pbi-report")
    assert _by_check(on_report, "model_format")[0]["severity"] == "warning"
    assert on_report["ok"] is True


def test_missing_layers_and_pbip_pointer_warn(tmp_path):
    (tmp_path / "bare.SemanticModel" / "definition" / "tables").mkdir(parents=True)
    res = doctor.run(PbipProject(tmp_path))
    messages = " ".join(f["message"] for f in _by_check(res, "project_layout"))
    assert "No .pbip pointer" in messages and "No *.Report folder" in messages
    assert res["project"]["report_format"] is None


def test_dataset_reference_checks(root):
    pbir = root / "Synthetic.Report" / "definition.pbir"
    project = _project(root)

    pbir.write_text(json.dumps({"version": "4.0", "datasetReference": {
        "byConnection": {"connectionString": "Data Source=x"}}}),
        encoding="utf-8")
    res = doctor.run(project)
    assert res["project"]["dataset_reference"] == "byConnection"
    assert "remote semantic model" in _by_check(res, "dataset_reference")[0]["message"]

    pbir.write_text(json.dumps({"version": "4.0", "datasetReference": {
        "byPath": {"path": "../Nope.SemanticModel"}}}), encoding="utf-8")
    finding = _by_check(doctor.run(project), "dataset_reference")[0]
    assert finding["severity"] == "warning" and "does not exist" in finding["message"]


# --- schema versions -------------------------------------------------------------

def test_vendored_schema_versions_come_from_the_index():
    vendored = doctor.vendored_schema_versions()
    assert {"page", "pagesMetadata", "report", "visualContainer"} <= set(vendored)
    for versions in vendored.values():
        assert versions and versions == sorted(versions, key=doctor._vkey)
    assert len(vendored["semanticQuery"]) >= 2      # 1.0.0 and 1.2.0 are vendored


def test_schema_versions_report_match_newer_and_unvendored(root, monkeypatch):
    monkeypatch.setattr(doctor, "vendored_schema_versions",
                        lambda: {"visualContainer": ["1.0.0"], "page": ["1.0.0"],
                                 "pagesMetadata": ["1.0.0"]})
    definition = root / "Synthetic.Report" / "definition"
    base = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition"
    visual = definition / "pages" / "overview" / "visuals" / "bar1" / "visual.json"
    data = json.loads(visual.read_text(encoding="utf-8-sig"))
    data["$schema"] = f"{base}/visualContainer/2.10.0/schema.json"
    visual.write_text(json.dumps(data), encoding="utf-8")
    (definition / "bookmarks").mkdir()
    (definition / "bookmarks" / "bookmarks.json").write_text(json.dumps(
        {"$schema": f"{base}/bookmarksMetadata/1.0.0/schema.json",
         "items": []}), encoding="utf-8")

    res = doctor.run(_project(root))
    found = res["project"]["schema_versions"]["project"]
    assert set(found["visualContainer"]) == {"1.0.0", "2.10.0"}
    assert found["bookmarksMetadata"] == {"1.0.0": 1}
    text = {f["message"]: f["severity"] for f in _by_check(res, "schema_versions")}
    assert any("visualContainer/2.10.0" in m and "newer than" in m for m in text)
    assert any("visualContainer/1.0.0" in m and "matches" in m for m in text)
    assert any("bookmarksMetadata/1.0.0" in m and "no vendored schema" in m
               for m in text)
    assert set(text.values()) == {"info"}          # drift is informational
    assert res["ok"] is True


def test_older_than_vendored_schema_is_a_warning(root, monkeypatch):
    monkeypatch.setattr(doctor, "vendored_schema_versions",
                        lambda: {"page": ["2.0.0"], "pagesMetadata": ["1.0.0"],
                                 "visualContainer": ["1.0.0"]})
    res = doctor.run(_project(root))
    older = [f for f in _by_check(res, "schema_versions")
             if "older than" in f["message"]]
    assert older and older[0]["severity"] == "warning"


# --- page integrity --------------------------------------------------------------

def test_page_integrity_is_clean_for_a_healthy_project(root):
    res = doctor.run(_project(root))
    assert res["project"]["page_integrity"] == {
        "orphan_page_folders": [], "orphan_visual_folders": [],
        "page_order_without_folder": []}
    assert "consistent" in _by_check(res, "page_integrity")[0]["message"]


def test_a_page_folder_left_with_only_backup_files_is_flagged(root):
    pages = root / "Synthetic.Report" / "definition" / "pages"
    ghost = pages / "ghost"
    ghost.mkdir()
    (ghost / "page.json.bak-20260101-000000").write_text("{}", encoding="utf-8")
    res = doctor.run(_project(root))
    warning = [f for f in _by_check(res, "page_integrity")
               if f["severity"] == "warning"][0]
    assert "'ghost'" in warning["message"] and "no page.json" in warning["message"]
    assert "only backup/temp files" in warning["message"]
    assert str(ghost) in warning["fix"]
    assert res["project"]["page_integrity"]["orphan_page_folders"] == ["ghost"]
    assert res["ok"] is True                       # a warning, not an error
    # this is what makes it dangerous: the report tools list it as a page
    assert "ghost" in {p.id for p in _project(root).list_pages()}


def test_an_orphan_folder_with_real_files_is_described_differently(root):
    ghost = root / "Synthetic.Report" / "definition" / "pages" / "ghost"
    ghost.mkdir()
    (ghost / "notes.txt").write_text("keep me", encoding="utf-8")
    message = [f for f in _by_check(doctor.run(_project(root)), "page_integrity")
               if f["severity"] == "warning"][0]["message"]
    assert "holds other files" in message


def test_dangling_page_order_and_orphan_visual_folders_are_flagged(root):
    pages = root / "Synthetic.Report" / "definition" / "pages"
    meta = pages / "pages.json"
    data = json.loads(meta.read_text(encoding="utf-8"))
    data["pageOrder"].append("gone")
    meta.write_text(json.dumps(data), encoding="utf-8")
    (pages / "overview" / "visuals" / "empty").mkdir()

    res = doctor.run(_project(root))
    text = " ".join(f["message"] for f in _by_check(res, "page_integrity"))
    assert "pages.json lists page(s) with no folder: ['gone']" in text
    assert "overview/empty" in text and "no visual.json" in text
    assert res["project"]["page_integrity"]["page_order_without_folder"] == ["gone"]


def test_page_integrity_is_skipped_without_a_report_folder(tmp_path):
    (tmp_path / "m.SemanticModel" / "definition" / "tables").mkdir(parents=True)
    res = doctor.run(PbipProject(tmp_path))
    assert "page_integrity" not in res["project"]
    assert not _by_check(res, "page_integrity")


# --- backups, journal, trash ------------------------------------------------------

def test_backup_journal_and_trash_accounting(root):
    tables = root / "Synthetic.SemanticModel" / "definition" / "tables"
    (tables / "Sales.tmdl.bak-20260101-000000").write_bytes(b"12345")
    (tables / "Date.tmdl.bak-20260101-000001-1").write_bytes(b"123")

    from model_server.server import ModelState, create_measure, set_project

    state = ModelState()
    set_project(state, str(root / "Synthetic.pbip"))
    state.project.backups = False
    journal.for_state(state).record(
        "pbi_create_measure", lambda: create_measure(state, "Sales", "Doc M", "1"))
    project = _project(root)
    project.delete_visual("overview", "bar1")

    res = doctor.run(project)
    p = res["project"]
    assert p["backups"] == {"count": 2, "bytes": 8}
    assert p["journal"]["entries"] == 1 and p["journal"]["bytes"] > 0
    assert p["journal"]["open_transaction"] is None
    assert p["trash"] == {"count": 1}
    assert "2 backup file(s)" in _by_check(res, "backups")[0]["message"]
    assert _by_check(res, "backups")[0]["severity"] == "info"


def test_a_big_backup_pile_warns_with_a_fix(root, monkeypatch):
    (root / "Synthetic.SemanticModel" / "a.bak-20260101-000000").write_bytes(b"x")
    (root / "Synthetic.SemanticModel" / "b.bak-20260101-000001").write_bytes(b"x")
    monkeypatch.setattr(doctor, "BACKUP_WARN_COUNT", 1)
    finding = _by_check(doctor.run(_project(root)), "backups")[0]
    assert finding["severity"] == "warning" and "pbi_list_backups" in finding["fix"]


def test_backup_scan_skips_the_undo_store_and_caches(root):
    for hidden in (".pbi-mcp/undo/e1/before", "Synthetic.Report/.pbi", ".git"):
        d = root / hidden
        d.mkdir(parents=True)
        (d / "stale.bak-20260101-000000").write_bytes(b"zz")
    assert doctor.run(_project(root))["project"]["backups"]["count"] == 0


def test_an_open_transaction_is_flagged(root):
    from model_server.server import ModelState, set_project

    state = ModelState()
    set_project(state, str(root / "Synthetic.pbip"))
    journal.for_state(state).begin()
    res = doctor.run(state.project)
    assert res["project"]["journal"]["open_transaction"]["started"]
    finding = [f for f in _by_check(res, "journal") if f["severity"] == "warning"][0]
    assert "pbi_commit" in finding["fix"] and "pbi_rollback" in finding["fix"]


def test_human_sizes():
    assert doctor._human(0) == "0 B"
    assert doctor._human(1023) == "1023 B"
    assert doctor._human(1536) == "1.5 KB"
    assert doctor._human(5 * 1024 * 1024) == "5.0 MB"
    assert doctor._human(3 * 1024 ** 3) == "3.0 GB"


# --- Power BI Desktop (faked) --------------------------------------------------------

def test_desktop_detection_reads_msi_and_store_versions(tmp_path, monkeypatch):
    exe = tmp_path / "PBIDesktop.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(doctor, "_is_windows", lambda: True)
    monkeypatch.setattr(doctor, "DESKTOP_MSI_EXE", exe)
    seen = {}

    def fake_ps(script):
        seen["script"] = script
        return json.dumps({"msi_version": "2.140.1", "store_version":
                           "2.141.0.0", "store_location": "C:\\WindowsApps\\pbi"})

    monkeypatch.setattr(doctor, "_powershell", fake_ps)
    res = doctor.run(None)
    desktop = res["environment"]["desktop"]
    assert desktop["msi"] == {"path": str(exe), "version": "2.140.1"}
    assert desktop["store"] == {"version": "2.141.0.0",
                                "location": "C:\\WindowsApps\\pbi"}
    msg = _by_check(res, "desktop")[0]
    assert msg["severity"] == "info"
    assert "MSI 2.140.1" in msg["message"] and "Store 2.141.0.0" in msg["message"]
    # the one PowerShell call asks about both install kinds
    assert "Get-AppxPackage -Name Microsoft.MicrosoftPowerBIDesktop" in seen["script"]
    assert str(exe) in seen["script"] and "ConvertTo-Json" in seen["script"]


def test_desktop_not_installed_warns(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "_is_windows", lambda: True)
    monkeypatch.setattr(doctor, "DESKTOP_MSI_EXE", tmp_path / "missing.exe")
    monkeypatch.setattr(doctor, "_powershell", lambda _s: "")
    finding = _by_check(doctor.run(None), "desktop")[0]
    assert finding["severity"] == "warning" and "not found" in finding["message"]
    assert finding["fix"]


def test_desktop_msi_without_powershell_is_still_reported(tmp_path, monkeypatch):
    exe = tmp_path / "PBIDesktop.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(doctor, "_is_windows", lambda: True)
    monkeypatch.setattr(doctor, "DESKTOP_MSI_EXE", exe)
    monkeypatch.setattr(doctor, "_powershell", lambda _s: None)
    res = doctor.run(None)
    assert res["environment"]["desktop"]["msi"]["version"] is None
    assert "MSI (version unknown)" in _by_check(res, "desktop")[0]["message"]


def test_desktop_powershell_unavailable_is_a_warning(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "_is_windows", lambda: True)
    monkeypatch.setattr(doctor, "DESKTOP_MSI_EXE", tmp_path / "missing.exe")
    monkeypatch.setattr(doctor, "_powershell", lambda _s: None)
    finding = _by_check(doctor.run(None), "desktop")[0]
    assert finding["severity"] == "warning" and "PowerShell" in finding["message"]


@pytest.mark.parametrize("garbage", ["not json", "[1, 2]", "null", "{"])
def test_desktop_garbage_powershell_output_never_raises(tmp_path, monkeypatch, garbage):
    monkeypatch.setattr(doctor, "_is_windows", lambda: True)
    monkeypatch.setattr(doctor, "DESKTOP_MSI_EXE", tmp_path / "missing.exe")
    monkeypatch.setattr(doctor, "_powershell", lambda _s: garbage)
    res = doctor.run(None)
    assert res["ok"] is True
    assert res["environment"]["desktop"]["store"] is None


def test_non_windows_skips_desktop_detection():
    res = doctor.run(None)
    assert res["environment"]["desktop"]["platform_supported"] is False
    assert "Windows-only" in _by_check(res, "desktop")[0]["message"]
    assert res["environment"]["desktop_instance"]["detectable"] is False


class _Done:
    def __init__(self, stdout):
        self.stdout = stdout


def test_powershell_runner_detaches_stdin_and_survives_failures(monkeypatch):
    calls = {}

    def fake_run(cmd, **kwargs):
        calls["cmd"], calls["kwargs"] = cmd, kwargs
        return _Done("  {\"a\": 1}\n")

    monkeypatch.setattr(doctor.subprocess, "run", fake_run)
    assert REAL_POWERSHELL("Get-Date") == '{"a": 1}'
    assert calls["cmd"][0] == "powershell" and "-NonInteractive" in calls["cmd"]
    assert calls["cmd"][-1] == "Get-Date"
    # MCP servers speak over stdio: a child must never inherit our stdin
    assert calls["kwargs"]["stdin"] is subprocess.DEVNULL
    assert calls["kwargs"]["timeout"] == doctor.POWERSHELL_TIMEOUT
    assert calls["kwargs"]["capture_output"] is True

    def missing(*_a, **_k):
        raise FileNotFoundError("powershell")

    def slow(*_a, **_k):
        raise subprocess.TimeoutExpired("powershell", 1)

    for failing in (missing, slow):
        monkeypatch.setattr(doctor.subprocess, "run", failing)
        assert REAL_POWERSHELL("Get-Date") is None

    monkeypatch.setattr(doctor.subprocess, "run", lambda *_a, **_k: _Done(None))
    assert REAL_POWERSHELL("Get-Date") == ""


def _port_file(base: Path, *parts: str) -> Path:
    path = base.joinpath(*parts, "Data", "msmdsrv.port.txt")
    path.parent.mkdir(parents=True)
    path.write_text("54321", encoding="utf-16")
    return path


def test_running_desktop_is_detected_from_the_port_file(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "_is_windows", lambda: True)
    monkeypatch.setattr(doctor, "_powershell", lambda _s: "")
    monkeypatch.setattr(doctor, "DESKTOP_MSI_EXE", tmp_path / "missing.exe")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert doctor.detect_desktop_instance() == {
        "detectable": True, "running": False, "port_files": []}

    msi = _port_file(tmp_path, "Microsoft", "Power BI Desktop",
                     "AnalysisServicesWorkspaces", "AnalysisServicesWorkspace_1")
    info = doctor.detect_desktop_instance()
    assert info["running"] is True and info["port_files"] == [str(msi)]

    store = _port_file(
        tmp_path, "Packages", "Microsoft.MicrosoftPowerBIDesktop_8wekyb3d8bbwe",
        "LocalCache", "Local", "Microsoft", "Power BI Desktop",
        "AnalysisServicesWorkspaces", "AnalysisServicesWorkspace_2")
    info = doctor.detect_desktop_instance()
    assert info["port_files"] == sorted([str(msi), str(store)])

    finding = _by_check(doctor.run(None), "desktop_instance")[0]
    assert "appears to be running (2 open workspaces)" in finding["message"]
    assert "reopen" in finding["fix"]


def test_no_running_desktop_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "_is_windows", lambda: True)
    monkeypatch.setattr(doctor, "_powershell", lambda _s: "")
    monkeypatch.setattr(doctor, "DESKTOP_MSI_EXE", tmp_path / "missing.exe")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    finding = _by_check(doctor.run(None), "desktop_instance")[0]
    assert "No running Power BI Desktop" in finding["message"]


def test_instance_detection_needs_localappdata(monkeypatch):
    monkeypatch.setattr(doctor, "_is_windows", lambda: True)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert doctor.detect_desktop_instance()["detectable"] is False


# --- the MCP tool ------------------------------------------------------------------------

@pytest.mark.parametrize("server", ["model", "report"])
def test_pbi_doctor_is_registered_read_only_and_callable(server, monkeypatch, root):
    if server == "model":
        import model_server.server as mod
    else:
        import report_server.server as mod
    tool = {t.name: t for t in asyncio.run(mod.mcp.list_tools())}["pbi_doctor"]
    ann = tool.annotations
    read_only = (getattr(ann, "readOnlyHint", None)
                 if hasattr(ann, "readOnlyHint")
                 else getattr(ann, "read_only_hint", None))
    assert read_only is True
    schema = getattr(tool, "inputSchema", None) or getattr(tool, "input_schema")
    assert not schema.get("properties")            # no args, no injected dry_run

    monkeypatch.setattr(mod.STATE, "project", None)
    res = _payload(asyncio.run(mod.mcp.call_tool("pbi_doctor", {})))
    assert res["project"] == {"selected": False}
    assert res["environment"]["server"] == ("pbi-model" if server == "model"
                                            else "pbi-report")

    monkeypatch.setattr(mod.STATE, "project", _project(root))
    res = _payload(asyncio.run(mod.mcp.call_tool("pbi_doctor", {})))
    assert res["ok"] is True and res["project"]["selected"] is True
    assert res["project"]["counts"]["tables"] > 0
