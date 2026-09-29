"""service_server: tool registration/annotations, resolution by name, and
every tool's logic end to end over the scripted HTTP fake from
tests/test_fabric_api.py."""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import shutil
import sys
import urllib.request
from pathlib import Path

import pytest

import service_server.server as srv
from core.auth import TokenProvider
from core.fabric_api import FABRIC_BASE as F, POWERBI_BASE as P, FabricClient
from service_server.server import (
    ServiceState, deploy_pipeline_stage, export_report, get_item_definition,
    list_deployment_pipelines, list_items, list_workspaces, publish_project,
    refresh_dataset, refresh_status, resolve_stage, resolve_workspace,
    service_login, service_status, set_project,
)
from tests.test_fabric_api import FakeClock, FakeHttp

REPO = Path(__file__).resolve().parent.parent
SYNTH = REPO / "tests" / "fixtures" / "synthetic"
WS = "11111111-1111-1111-1111-111111111111"
WS2 = "22222222-2222-2222-2222-222222222222"
MODEL_ID = "aaaaaaaa-0000-0000-0000-000000000001"
REPORT_ID = "bbbbbbbb-0000-0000-0000-000000000002"
PIPE = "cccccccc-0000-0000-0000-000000000003"
JWT = ("eyJhbGciOiJub25lIn0."
       "eyJ1cG4iOiJvcGVyYXRvckBjb250b3NvLmNvbSIsInRpZCI6InQtMSJ9.sig")


def _ann(tool, hint: str):
    a = tool.annotations
    snake = {"readOnlyHint": "read_only_hint",
             "destructiveHint": "destructive_hint",
             "idempotentHint": "idempotent_hint"}[hint]
    return getattr(a, hint, None) if hasattr(a, hint) else getattr(a, snake, None)


def _schema(tool) -> dict:
    return getattr(tool, "inputSchema", None) or getattr(tool, "input_schema")


def _tools(module) -> dict:
    return {t.name: t for t in asyncio.run(module.mcp.list_tools())}


def _payload(result):
    structured = None
    if isinstance(result, tuple):
        result, structured = result
    structured = (structured
                  or getattr(result, "structured_content", None)
                  or getattr(result, "structuredContent", None))
    if structured is not None:
        return structured.get("result", structured) if isinstance(structured, dict) else structured
    content = getattr(result, "content", result)
    items = [json.loads(c.text) for c in content]
    return items[0] if len(items) == 1 else items


@pytest.fixture
def http(monkeypatch) -> FakeHttp:
    fake = FakeHttp()
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return fake


@pytest.fixture
def state() -> ServiceState:
    st = ServiceState(auth=TokenProvider(env={}))
    st.auth.set_token("session-token-value")
    clock = FakeClock()
    st.client = FabricClient(st.auth, sleep=clock.sleep, clock=clock)
    return st


def workspaces(http: FakeHttp) -> None:
    http.on("GET", F + "/workspaces",
            (200, {"value": [{"id": WS, "displayName": "Sales WS", "type": "Workspace"},
                             {"id": WS2, "displayName": "Other"}]}))


def items(http: FakeHttp, *found: dict) -> None:
    """Workspace listing plus a by-id GET for each item (GUID resolution)."""
    http.on("GET", F + f"/workspaces/{WS}/items", (200, {"value": list(found)}))
    for item in found:
        http.on("GET", F + f"/workspaces/{WS}/items/{item['id']}", (200, item))


MODEL_ITEM = {"id": MODEL_ID, "displayName": "Synthetic", "type": "SemanticModel"}
REPORT_ITEM = {"id": REPORT_ID, "displayName": "Synthetic", "type": "Report"}


# --- registration -------------------------------------------------------------

EXPECTED_TOOLS = {
    "pbi_service_status", "pbi_service_login", "pbi_set_project",
    "pbi_list_workspaces", "pbi_list_items", "pbi_get_item_definition",
    "pbi_publish_project", "pbi_refresh_dataset", "pbi_refresh_status",
    "pbi_list_deployment_pipelines", "pbi_deploy_pipeline_stage",
    "pbi_export_report",
}
READ_TOOLS = {"pbi_service_status", "pbi_set_project", "pbi_list_workspaces",
              "pbi_list_items", "pbi_refresh_status",
              "pbi_list_deployment_pipelines"}


def test_server_tool_registration_and_annotations():
    from core.mcp_compat import Server
    assert isinstance(srv.mcp, Server) and srv.mcp.name == "pbi-service"
    tools = _tools(srv)
    assert EXPECTED_TOOLS <= set(tools)
    for name, t in tools.items():
        assert name.startswith("pbi_")
        assert t.annotations is not None, name
        assert _ann(t, "readOnlyHint") is (name in READ_TOOLS), name
        # cloud writes are journaled=False: no injected dry_run anywhere
        assert "dry_run" not in _schema(t).get("properties", {}), name
        assert (t.description or "").strip(), name
    assert _ann(tools["pbi_deploy_pipeline_stage"], "destructiveHint") is True
    assert _ann(tools["pbi_publish_project"], "destructiveHint") is False
    props = _schema(tools["pbi_publish_project"])["properties"]
    assert {"workspace", "name", "update_if_exists", "publish_model",
            "publish_report", "dataset"} <= set(props)
    assert _schema(tools["pbi_publish_project"])["required"] == ["workspace"]
    assert set(_schema(tools["pbi_export_report"])["required"]) == \
        {"workspace", "report", "out_path"}
    # the journal's preview sentence is never appended to cloud writes
    for name in EXPECTED_TOOLS - READ_TOOLS:
        assert "dry_run=true" not in (tools[name].description or ""), name


def test_tool_names_do_not_collide_with_other_servers():
    import model_server.server as m
    import report_server.server as r
    others = set(_tools(m)) | set(_tools(r))
    shared = set(_tools(srv)) & others
    assert shared == {"pbi_set_project"}  # the one deliberately shared name


def test_local_servers_do_not_import_the_cloud_package():
    for mod in ("model_server.server", "report_server.server", "core.pbip",
                "core.tooling", "core.journal"):
        src = (REPO / Path(*mod.split("."))).with_suffix(".py").read_text("utf-8")
        assert "service_server" not in src and "fabric_api" not in src \
            and "core.auth" not in src, mod


# --- status / login / project -------------------------------------------------

def test_status_without_credentials_and_greeting_once():
    st = ServiceState(auth=TokenProvider(env={}))
    s = service_status(st)
    assert s["ok"] and s["auth"]["active_source"] is None
    assert s["fabric_base"] == F and s["powerbi_base"] == P
    assert s["project"] is None and "greeting" in s
    assert "greeting" not in service_status(st)


def test_login_token_is_stored_and_never_echoed():
    st = ServiceState(auth=TokenProvider(env={}))
    out = service_login(st, token=JWT)
    assert out["action"] == "session_token_set"
    assert out["auth"]["active_source"] == "session_token"
    assert out["auth"]["user"] == "operator@contoso.com"
    assert JWT not in json.dumps(out)
    assert st.auth.get_token() == JWT
    with pytest.raises(ValueError):
        service_login(st)
    with pytest.raises(ValueError):
        service_login(st, token="x", device_code=True)


def test_login_via_mcp_tool_result_is_redacted(monkeypatch):
    monkeypatch.setattr(srv, "STATE", ServiceState(auth=TokenProvider(env={})))
    result = asyncio.run(srv.mcp.call_tool("pbi_service_login", {"token": JWT}))
    payload = _payload(result)
    assert payload["ok"] is True
    assert JWT not in json.dumps(payload)


def test_set_project_resolves_layers(tmp_path):
    st = ServiceState()
    out = set_project(st, str(SYNTH / "Synthetic.pbip"))
    assert out["ok"] and out["semantic_model"].endswith("Synthetic.SemanticModel")
    assert out["report"].endswith("Synthetic.Report")
    assert service_status(st)["project"].endswith("Synthetic.pbip")
    with pytest.raises(FileNotFoundError):
        set_project(st, str(tmp_path / "nothing"))
    with pytest.raises(ValueError, match="pbi_set_project"):
        ServiceState().require()


# --- resolution -----------------------------------------------------------------

def test_resolve_workspace_by_name_or_guid(http, state):
    workspaces(http)
    assert resolve_workspace(state, "sales ws")["id"] == WS
    assert resolve_workspace(state, WS) == {"id": WS, "displayName": None}
    assert len(http.calls) == 1  # GUIDs never hit the network
    with pytest.raises(ValueError, match="pbi_list_workspaces"):
        resolve_workspace(state, "Nope")
    with pytest.raises(ValueError):
        resolve_workspace(state, "")


def test_list_workspaces_and_items(http, state):
    workspaces(http)
    items(http, MODEL_ITEM, REPORT_ITEM)
    assert [w["displayName"] for w in list_workspaces(state)] == ["Sales WS", "Other"]
    out = list_items(state, "Sales WS", type="Report")
    assert out[0] == {"id": MODEL_ID, "displayName": "Synthetic",
                      "type": "SemanticModel"}
    assert http.calls[-1].query == {"type": "Report"}


def test_resolve_stage_accepts_order_name_and_alias():
    pipeline = {"stages": [{"order": 0, "displayName": "Development"},
                           {"order": 1, "displayName": "Test"},
                           {"order": 2, "displayName": "Production"}]}
    assert resolve_stage(pipeline, 1) == 1
    assert resolve_stage(pipeline, "2") == 2
    assert resolve_stage(pipeline, "test") == 1
    assert resolve_stage({}, "prod") == 2
    with pytest.raises(ValueError):
        resolve_stage(pipeline, "staging")


# --- get item definition --------------------------------------------------------

def test_get_item_definition_writes_pbip_folder(http, state, tmp_path):
    items(http, MODEL_ITEM, REPORT_ITEM)
    parts = [{"path": "definition.pbism", "payload": b64('{"version":"4.0"}'),
              "payloadType": "InlineBase64"},
             {"path": "definition/model.tmdl", "payload": b64("model Model"),
              "payloadType": "InlineBase64"},
             {"path": ".platform", "payload": b64("{}"),
              "payloadType": "InlineBase64"}]
    http.on("POST", F + f"/workspaces/{WS}/items/{MODEL_ID}/getDefinition",
            (200, {"definition": {"parts": parts}}))
    with pytest.raises(ValueError, match="pass type="):
        get_item_definition(state, WS, "Synthetic", str(tmp_path))

    out = get_item_definition(state, WS, "Synthetic", str(tmp_path),
                              type="SemanticModel")
    folder = tmp_path / "Synthetic.SemanticModel"
    assert out["folder"] == str(folder) and out["format"] == "TMDL"
    assert sorted(out["files"]) == [".platform", "definition.pbism",
                                    "definition/model.tmdl"]
    assert (folder / "definition" / "model.tmdl").read_text() == "model Model"
    assert http.calls[-1].query == {"format": "TMDL"}

    with pytest.raises(ValueError, match="overwrite=true"):
        get_item_definition(state, WS, MODEL_ID, str(tmp_path))
    out2 = get_item_definition(state, WS, MODEL_ID, str(tmp_path), overwrite=True)
    assert out2["item"]["id"] == MODEL_ID
    assert state.last_operations["get_item_definition"]["folder"] == str(folder)


# --- publish --------------------------------------------------------------------

@pytest.fixture
def project_state(state, tmp_path) -> ServiceState:
    dst = tmp_path / "proj"
    shutil.copytree(SYNTH, dst)
    (dst / "Synthetic.Report" / ".pbi").mkdir()
    (dst / "Synthetic.Report" / ".pbi" / "localSettings.json").write_text("{}")
    set_project(state, str(dst / "Synthetic.pbip"))
    return state


def test_publish_project_creates_model_then_report_bound_to_it(http, project_state):
    workspaces(http)
    items(http)  # nothing published yet
    http.on("POST", F + f"/workspaces/{WS}/items",
            (201, {"id": MODEL_ID, "type": "SemanticModel", "displayName": "Synthetic"}),
            (201, {"id": REPORT_ID, "type": "Report", "displayName": "Synthetic"}))

    out = publish_project(project_state, "Sales WS")
    assert out["model"] == {"action": "created", "id": MODEL_ID,
                            "displayName": "Synthetic", "parts": out["model"]["parts"]}
    assert out["report"]["action"] == "created" and out["report"]["id"] == REPORT_ID
    assert out["report"]["bound_to_dataset"] == MODEL_ID

    creates = http.sent("POST", "/items")
    assert [c.body["type"] for c in creates] == ["SemanticModel", "Report"]
    model_body, report_body = creates[0].body, creates[1].body
    assert model_body["definition"]["format"] == "TMDL"
    model_paths = {p["path"] for p in model_body["definition"]["parts"]}
    assert {"definition.pbism", ".platform", "definition/model.tmdl",
            "definition/tables/Sales.tmdl"} <= model_paths
    assert not any(p.startswith(".pbi/") for p in model_paths)

    assert report_body["definition"]["format"] == "PBIR"
    report_parts = {p["path"]: p for p in report_body["definition"]["parts"]}
    assert not any(p.startswith(".pbi/") for p in report_parts)
    assert "definition/report.json" in report_parts
    pbir = json.loads(base64.b64decode(report_parts["definition.pbir"]["payload"]))
    assert "byPath" not in pbir["datasetReference"]
    assert pbir["datasetReference"]["byConnection"]["pbiModelDatabaseName"] == MODEL_ID
    # the local file is still byPath
    local = json.loads((Path(project_state.project.report_dir) / "definition.pbir")
                       .read_text("utf-8-sig"))
    assert "byPath" in local["datasetReference"]
    assert project_state.last_operations["publish"]["report"]["id"] == REPORT_ID


def test_publish_project_updates_existing_or_refuses(http, project_state):
    items(http, MODEL_ITEM, REPORT_ITEM)
    http.on("POST", F + f"/workspaces/{WS}/items/{MODEL_ID}/updateDefinition",
            (200, None))
    http.on("POST", F + f"/workspaces/{WS}/items/{REPORT_ID}/updateDefinition",
            (200, None))
    out = publish_project(project_state, WS, name="Synthetic")
    assert out["model"]["action"] == "updated" and out["model"]["id"] == MODEL_ID
    assert out["report"]["action"] == "updated"
    assert out["report"]["bound_to_dataset"] == MODEL_ID
    updates = http.sent("POST", "updateDefinition")
    assert [c.path.split("/")[-2] for c in updates] == [MODEL_ID, REPORT_ID]
    assert updates[1].body["definition"]["format"] == "PBIR"

    with pytest.raises(ValueError, match="update_if_exists=true"):
        publish_project(project_state, WS, update_if_exists=False)


def test_publish_report_only_binds_to_named_dataset(http, project_state):
    items(http, MODEL_ITEM)
    http.on("POST", F + f"/workspaces/{WS}/items",
            (201, {"id": REPORT_ID, "type": "Report"}))
    out = publish_project(project_state, WS, publish_model=False,
                          dataset="synthetic")
    assert out["model"] is None
    assert out["report"]["bound_to_dataset"] == MODEL_ID
    assert http.sent("POST", "/items")[0].body["type"] == "Report"
    with pytest.raises(ValueError, match="nothing to publish"):
        publish_project(project_state, WS, publish_model=False,
                        publish_report=False)


def test_publish_requires_a_project(state):
    with pytest.raises(ValueError, match="pbi_set_project"):
        publish_project(state, WS)


# --- refresh --------------------------------------------------------------------

def test_refresh_dataset_wait_and_status(http, state):
    items(http, MODEL_ITEM)
    http.on("POST", P + f"/groups/{WS}/datasets/{MODEL_ID}/refreshes",
            (202, None, {"RequestId": "req-9"}))
    http.on("GET", P + f"/groups/{WS}/datasets/{MODEL_ID}/refreshes",
            (200, {"value": [{"requestId": "req-9", "status": "Unknown"}]}),
            (200, {"value": [{"requestId": "req-9", "status": "Completed"}]}))
    out = refresh_dataset(state, WS, "Synthetic")
    assert out == {"ok": True, "dataset": MODEL_ITEM, "workspace_id": WS,
                   "request_id": "req-9", "status": "Accepted"}

    done = refresh_dataset(state, WS, MODEL_ID, wait=True, timeout=120)
    assert done["ok"] is True and done["status"] == "Completed"
    assert done["refresh"]["requestId"] == "req-9"
    assert state.last_operations["refresh"]["request_id"] == "req-9"

    history = refresh_status(state, WS, "Synthetic", top=2)
    assert history[0]["status"] == "Completed"
    assert http.calls[-1].query == {"$top": "2"}


# --- pipelines ------------------------------------------------------------------

def test_list_and_deploy_pipeline_stage_by_name(http, state):
    http.on("GET", P + "/pipelines",
            (200, {"value": [{"id": PIPE, "displayName": "Sales Pipeline",
                              "stages": [{"order": 0, "displayName": "Development",
                                          "workspaceId": WS},
                                         {"order": 1, "displayName": "Test"}]}]}))
    http.on("POST", P + f"/pipelines/{PIPE}/deployAll",
            (202, {"id": "op-1", "status": "NotStarted"}))
    http.on("POST", P + f"/pipelines/{PIPE}/deploy",
            (202, {"id": "op-2", "status": "NotStarted"}))
    http.on("GET", P + f"/pipelines/{PIPE}/operations/op-1",
            (200, {"id": "op-1", "status": "Executing"}),
            (200, {"id": "op-1", "type": "Deploy", "status": "Succeeded",
                   "executionEndTime": "t"}))
    listed = list_deployment_pipelines(state)
    assert listed[0]["displayName"] == "Sales Pipeline"
    assert listed[0]["stages"][0] == {"order": 0, "displayName": "Development",
                                      "workspaceId": WS}

    out = deploy_pipeline_stage(state, "sales pipeline", "Development")
    assert out["status"] == "Succeeded" and out["source_stage_order"] == 0
    assert out["operation"]["executionEndTime"] == "t" and out["scope"] == "all"
    assert http.sent("POST", "deployAll")[0].body["sourceStageOrder"] == 0

    sel = deploy_pipeline_stage(state, PIPE, 1, items=[
        {"type": "SemanticModel", "id": MODEL_ID}], wait=False, note="n")
    assert sel["operation_id"] == "op-2" and sel["status"] == "NotStarted"
    body = http.sent("POST", "/deploy")[-1].body
    assert body["datasets"] == [{"sourceId": MODEL_ID}] and body["note"] == "n"
    with pytest.raises(ValueError, match="pbi_list_deployment_pipelines"):
        deploy_pipeline_stage(state, "Missing", 0)


# --- export ---------------------------------------------------------------------

def test_export_report_writes_file_and_guards(http, state, tmp_path):
    items(http, REPORT_ITEM)
    http.on("POST", P + f"/groups/{WS}/reports/{REPORT_ID}/ExportTo",
            (202, {"id": "ex1", "status": "NotStarted"}))
    http.on("GET", P + f"/groups/{WS}/reports/{REPORT_ID}/exports/ex1",
            (200, {"id": "ex1", "status": "Succeeded"}))
    http.on("GET", P + f"/groups/{WS}/reports/{REPORT_ID}/exports/ex1/file",
            (200, b"PK\x03\x04pptx"))
    target = tmp_path / "out" / "deck.pptx"
    out = export_report(state, WS, "Synthetic", str(target), format="pptx",
                        page_ids=["overview"])
    assert out["ok"] and out["format"] == "PPTX" and out["bytes"] == 8
    assert target.read_bytes() == b"PK\x03\x04pptx"
    assert http.sent("POST", "ExportTo")[0].body == {
        "format": "PPTX",
        "powerBIReportConfiguration": {"pages": [{"pageName": "overview"}]}}
    with pytest.raises(ValueError, match="overwrite=true"):
        export_report(state, WS, REPORT_ID, str(target))
    with pytest.raises(ValueError, match="format must be"):
        export_report(state, WS, REPORT_ID, str(tmp_path / "x.xlsx"), format="XLSX")
    with pytest.raises(ValueError, match="folder"):
        export_report(state, WS, REPORT_ID, str(tmp_path))


# --- launcher -------------------------------------------------------------------

def _load_launcher():
    spec = importlib.util.spec_from_file_location(
        "pbi_launcher_under_test", REPO / "scripts" / "launcher.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("arg", ["service", "pbi-service"])
def test_launcher_dispatches_service(monkeypatch, arg):
    launcher = _load_launcher()
    calls = []
    monkeypatch.setattr(srv, "main", lambda: calls.append(arg))
    monkeypatch.setattr(sys, "argv", ["pbi-mcp", arg])
    launcher.main()
    assert calls == [arg]


def test_launcher_rejects_unknown_server(monkeypatch, capsys):
    launcher = _load_launcher()
    monkeypatch.setattr(sys, "argv", ["pbi-mcp", "cloud"])
    with pytest.raises(SystemExit) as ei:
        launcher.main()
    assert ei.value.code == 2 and "service" in capsys.readouterr().err


def b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")
