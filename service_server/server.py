"""Cloud MCP server: pbi-service (Power BI / Fabric REST APIs).

Optional third server. Everything here talks to the service over HTTPS via
core/fabric_api.py with a token from core/auth.py; the local project is only
read (to publish it) or written to a folder the caller names (to pull an
item definition). The model and report servers never import this package.

Same design as the other servers: tool logic in plain functions over an
explicit ``ServiceState`` (unit-testable with a fake HTTP layer), thin MCP
wrappers at the bottom. Every tool result passes through ``redact()`` so no
token, secret or ``Authorization`` value can leak into a response. Cloud
writes are registered ``journaled=False``: they change nothing on disk, so
there is no dry-run preview and no ``pbi_undo`` for them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from core.auth import AuthError, TokenProvider, redact
from core.fabric_api import (
    DEFINITION_FORMATS, FABRIC_BASE, POWERBI_BASE, FabricApiError,
    FabricClient, collect_report_parts, collect_semantic_model_parts,
    write_parts,
)
from core.mcp_compat import Server
from core.pbip import PbipProject
from core.tooling import load_tool_modules, make_tool

_GUID = re.compile(r"^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
EXPORT_FORMATS = {"PDF", "PPTX", "PNG"}
STAGE_ALIASES = {"development": 0, "dev": 0, "test": 1, "production": 2,
                 "prod": 2}


@dataclass
class ServiceState:
    """Per-session state: optional local project, auth, last operation ids."""
    project: PbipProject | None = field(default=None)
    auth: TokenProvider = field(default_factory=TokenProvider)
    client: FabricClient | None = field(default=None)
    last_operations: dict = field(default_factory=dict)
    greeted: bool = field(default=False)

    def require(self) -> PbipProject:
        if self.project is None:
            raise ValueError("No local project set — call pbi_set_project(path) "
                             "first (needed only to publish).")
        return self.project

    def api(self) -> FabricClient:
        if self.client is None:
            self.client = FabricClient(self.auth)
        return self.client

    def remember(self, kind: str, info: dict) -> None:
        self.last_operations[kind] = redact(info)


# --- resolution helpers -------------------------------------------------------

def is_guid(value: str | None) -> bool:
    return bool(value) and bool(_GUID.match(str(value).strip()))


def resolve_workspace(state: ServiceState, workspace: str) -> dict:
    """Workspace by id (GUID) or display name -> {"id", "displayName"}."""
    if not workspace or not str(workspace).strip():
        raise ValueError("workspace is required (id or display name)")
    workspace = str(workspace).strip()
    if is_guid(workspace):
        return {"id": workspace, "displayName": None}
    wanted = workspace.lower()
    matches = [w for w in state.api().list_workspaces()
               if str(w.get("displayName", "")).strip().lower() == wanted]
    if len(matches) == 1:
        return {"id": matches[0]["id"], "displayName": matches[0].get("displayName")}
    if not matches:
        raise ValueError(f"No workspace named {workspace!r} is visible to this "
                         "identity. Call pbi_list_workspaces() for the names "
                         "you can use, or pass the workspace id.")
    raise ValueError(f"{len(matches)} workspaces are named {workspace!r}; pass "
                     f"the id instead: {[m['id'] for m in matches]}")


def resolve_item(state: ServiceState, workspace_id: str, item: str,
                 type: str | None = None) -> dict:
    """Item by id or display name (optionally constrained to a type)."""
    if not item or not str(item).strip():
        raise ValueError("item is required (id or display name)")
    item = str(item).strip()
    if is_guid(item):
        found = state.api().get_item(workspace_id, item)
        if type and str(found.get("type", "")).lower() != type.lower():
            raise ValueError(f"Item {item} is a {found.get('type')}, not a {type}")
        return found
    wanted = item.lower()
    items = state.api().list_items(workspace_id, type)
    if type:  # re-check locally: never confuse a report with its model
        items = [i for i in items
                 if str(i.get("type", "")).lower() == type.lower()]
    matches = [i for i in items
               if str(i.get("displayName", "")).strip().lower() == wanted]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        names = sorted({f"{i.get('displayName')} ({i.get('type')})" for i in items})
        raise ValueError(
            f"No {type or 'item'} named {item!r} in workspace {workspace_id}. "
            f"Available: {names[:25]}")
    raise ValueError(
        f"{len(matches)} items are named {item!r} "
        f"({sorted({m.get('type') for m in matches})}); pass type= or the id.")


def resolve_pipeline(state: ServiceState, pipeline: str) -> dict:
    if not pipeline or not str(pipeline).strip():
        raise ValueError("pipeline is required (id or display name)")
    pipeline = str(pipeline).strip()
    pipelines = state.api().list_pipelines()
    if is_guid(pipeline):
        for p in pipelines:
            if p.get("id") == pipeline:
                return p
        return {"id": pipeline, "displayName": None}
    wanted = pipeline.lower()
    matches = [p for p in pipelines
               if str(p.get("displayName", "")).strip().lower() == wanted]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ValueError(f"No deployment pipeline named {pipeline!r}. Call "
                         "pbi_list_deployment_pipelines() for the names.")
    raise ValueError(f"{len(matches)} pipelines are named {pipeline!r}; pass "
                     f"the id: {[m['id'] for m in matches]}")


def resolve_stage(pipeline: dict, source_stage) -> int:
    """Stage order from an int, a numeric string or a stage name."""
    if isinstance(source_stage, bool):
        raise ValueError("source_stage must be a stage order or name")
    if isinstance(source_stage, int):
        return source_stage
    text = str(source_stage).strip()
    if text.lstrip("-").isdigit():
        return int(text)
    for st in pipeline.get("stages") or []:
        names = {str(st.get("displayName", "")).lower(),
                 str(st.get("workspaceName", "")).lower()}
        if text.lower() in names and st.get("order") is not None:
            return int(st["order"])
    if text.lower() in STAGE_ALIASES:
        return STAGE_ALIASES[text.lower()]
    raise ValueError(f"Unknown stage {source_stage!r}; pass the stage order "
                     "(0 = Development, 1 = Test, 2 = Production) or its name.")


def _item_summary(item: dict) -> dict:
    return {k: item.get(k) for k in ("id", "displayName", "type",
                                     "description", "workspaceId")
            if item.get(k) is not None}


# --- tool logic ---------------------------------------------------------------

def service_status(state: ServiceState) -> dict:
    """Auth source, identity hints, base URLs, local project, last ops."""
    out = {
        "ok": True,
        "auth": state.auth.describe(),
        "fabric_base": FABRIC_BASE,
        "powerbi_base": POWERBI_BASE,
        "project": str(state.project.path) if state.project else None,
        "last_operations": state.last_operations,
    }
    if not state.greeted:
        from core.capabilities import GREETING
        out["greeting"] = GREETING
        state.greeted = True
    return out


def service_login(state: ServiceState, token: str | None = None,
                  device_code: bool = False, wait_seconds: int = 0) -> dict:
    """Store a session token, or start/poll a device-code sign-in."""
    if token and device_code:
        raise ValueError("pass either token=... or device_code=True, not both")
    if token:
        info = state.auth.set_token(token)
        return {"ok": True, "action": "session_token_set", "auth": info}
    if device_code:
        result = state.auth.device_code_login(wait_seconds)
        return {"ok": True, "action": "device_code", **result,
                "auth": state.auth.describe()}
    raise ValueError("Nothing to do: pass token=<bearer token> or "
                     "device_code=True. Current state: "
                     f"{state.auth.describe()}")


def set_project(state: ServiceState, path: str) -> dict:
    """Select the local .pbip project that pbi_publish_project uploads."""
    project = PbipProject(path)
    project._resolve()
    if project.semantic_model_dir is None and project.report_dir is None:
        raise FileNotFoundError(
            f"No *.SemanticModel or *.Report folder found from {path}")
    state.project = project
    return {
        "ok": True,
        "path": str(project.path),
        "semantic_model": str(project.semantic_model_dir)
        if project.semantic_model_dir else None,
        "report": str(project.report_dir) if project.report_dir else None,
    }


def list_workspaces(state: ServiceState) -> list[dict]:
    return [{k: w.get(k) for k in ("id", "displayName", "type", "capacityId",
                                   "description") if w.get(k) is not None}
            for w in state.api().list_workspaces()]


def list_items(state: ServiceState, workspace: str,
               type: str | None = None) -> list[dict]:
    ws = resolve_workspace(state, workspace)
    return [_item_summary(i) for i in state.api().list_items(ws["id"], type)]


def get_item_definition(state: ServiceState, workspace: str, item: str,
                        out_dir: str, type: str | None = None,
                        overwrite: bool = False) -> dict:
    """Download an item definition into a PBIP-shaped folder under out_dir."""
    ws = resolve_workspace(state, workspace)
    found = resolve_item(state, ws["id"], item, type)
    item_type = found.get("type") or type
    if not item_type:
        raise ValueError("Could not determine the item type; pass type=")
    fmt = DEFINITION_FORMATS.get(item_type)
    name = found.get("displayName") or found.get("id")
    safe = re.sub(r"[<>:\"/\\|?*]+", "_", str(name)).strip() or found["id"]
    target = Path(out_dir) / f"{safe}.{item_type}"
    if target.exists() and any(target.iterdir()) and not overwrite:
        raise ValueError(f"{target} already exists and is not empty; pass "
                         "overwrite=true to replace its files.")
    parts = state.api().get_item_definition(ws["id"], found["id"], fmt)
    if not parts:
        raise ValueError(f"The service returned no definition parts for "
                         f"{name} ({item_type}); this item type may not "
                         "support definitions.")
    written = write_parts(parts, target)
    result = {"ok": True, "item": _item_summary(found), "format": fmt,
              "folder": str(target), "files": written}
    state.remember("get_item_definition", result)
    return result


def publish_project(state: ServiceState, workspace: str,
                    name: str | None = None, update_if_exists: bool = True,
                    publish_model: bool = True, publish_report: bool = True,
                    dataset: str | None = None) -> dict:
    """Publish the selected .pbip: SemanticModel first, then the Report."""
    if not publish_model and not publish_report:
        raise ValueError("nothing to publish: publish_model and publish_report "
                         "are both false")
    project = state.require()
    ws = resolve_workspace(state, workspace)
    api = state.api()
    result: dict = {"ok": True, "workspace": ws, "model": None, "report": None}

    dataset_id: str | None = None
    if publish_model:
        sm_dir = project.semantic_model_dir
        if sm_dir is None:
            raise ValueError("The selected project has no *.SemanticModel "
                             "folder; pass publish_model=false and dataset=<id "
                             "or name of the published model>.")
        model_name = name or sm_dir.name[: -len(".SemanticModel")]
        parts = collect_semantic_model_parts(sm_dir)
        existing = api.find_item(ws["id"], model_name, "SemanticModel")
        if existing is not None:
            if not update_if_exists:
                raise ValueError(
                    f"SemanticModel {model_name!r} already exists in the "
                    f"workspace (id {existing['id']}); pass "
                    "update_if_exists=true to overwrite it or choose another "
                    "name.")
            api.update_item_definition(ws["id"], existing["id"], parts,
                                       DEFINITION_FORMATS["SemanticModel"])
            item, action = existing, "updated"
        else:
            item = api.create_item_with_definition(
                ws["id"], "SemanticModel", model_name, parts,
                DEFINITION_FORMATS["SemanticModel"])
            action = "created"
        dataset_id = item.get("id")
        result["model"] = {"action": action, "id": dataset_id,
                           "displayName": model_name, "parts": len(parts)}

    if publish_report:
        report_dir = project.report_dir
        if report_dir is None:
            raise ValueError("The selected project has no *.Report folder; "
                             "pass publish_report=false.")
        report_name = name or report_dir.name[: -len(".Report")]
        if dataset_id is None:
            target = dataset or report_name
            found = resolve_item(state, ws["id"], target, "SemanticModel")
            dataset_id = found["id"]
        parts = collect_report_parts(report_dir, dataset_id)
        existing = api.find_item(ws["id"], report_name, "Report")
        if existing is not None:
            if not update_if_exists:
                raise ValueError(
                    f"Report {report_name!r} already exists in the workspace "
                    f"(id {existing['id']}); pass update_if_exists=true to "
                    "overwrite it or choose another name.")
            api.update_item_definition(ws["id"], existing["id"], parts,
                                       DEFINITION_FORMATS["Report"])
            item, action = existing, "updated"
        else:
            item = api.create_item_with_definition(
                ws["id"], "Report", report_name, parts,
                DEFINITION_FORMATS["Report"])
            action = "created"
        result["report"] = {"action": action, "id": item.get("id"),
                            "displayName": report_name, "parts": len(parts),
                            "bound_to_dataset": dataset_id}
    state.remember("publish", result)
    return result


def refresh_dataset(state: ServiceState, workspace: str, dataset: str,
                    wait: bool = False, timeout: int = 600) -> dict:
    ws = resolve_workspace(state, workspace)
    ds = resolve_item(state, ws["id"], dataset, "SemanticModel")
    started = state.api().refresh_dataset(ws["id"], ds["id"])
    result = {"ok": True, "dataset": _item_summary(ds), "workspace_id": ws["id"],
              "request_id": started.get("request_id"), "status": "Accepted"}
    if wait:
        entry = state.api().wait_for_refresh(
            ws["id"], ds["id"], started.get("request_id"), timeout=timeout)
        result["status"] = entry.get("status")
        result["refresh"] = entry
        result["ok"] = str(entry.get("status", "")).lower() == "completed"
    state.remember("refresh", result)
    return result


def refresh_status(state: ServiceState, workspace: str, dataset: str,
                   top: int = 5) -> list[dict]:
    ws = resolve_workspace(state, workspace)
    ds = resolve_item(state, ws["id"], dataset, "SemanticModel")
    return state.api().get_refresh_history(ws["id"], ds["id"], top=top)


def list_deployment_pipelines(state: ServiceState) -> list[dict]:
    out = []
    for p in state.api().list_pipelines():
        out.append({
            "id": p.get("id"), "displayName": p.get("displayName"),
            "description": p.get("description"),
            "stages": [{k: s.get(k) for k in ("order", "displayName",
                                              "workspaceId", "workspaceName")
                        if s.get(k) is not None}
                       for s in (p.get("stages") or [])],
        })
    return out


def deploy_pipeline_stage(state: ServiceState, pipeline: str, source_stage,
                          items: list[dict] | None = None, wait: bool = True,
                          timeout: int = 600, note: str | None = None) -> dict:
    pl = resolve_pipeline(state, pipeline)
    order = resolve_stage(pl, source_stage)
    op = state.api().deploy_pipeline(pl["id"], order, items, note=note)
    result = {"ok": True, "pipeline": {"id": pl["id"],
                                       "displayName": pl.get("displayName")},
              "source_stage_order": order, "operation_id": op.get("id"),
              "status": op.get("status"), "scope": "all" if items is None
              else items}
    if wait and op.get("id"):
        final = state.api().wait_for_pipeline_operation(
            pl["id"], op["id"], timeout=timeout)
        result["status"] = final.get("status")
        result["operation"] = {k: final.get(k) for k in (
            "id", "type", "status", "lastUpdatedTime", "executionStartTime",
            "executionEndTime") if final.get(k) is not None}
    state.remember("deploy", result)
    return result


def export_report(state: ServiceState, workspace: str, report: str,
                  out_path: str, format: str = "PDF",
                  page_ids: list[str] | None = None, timeout: int = 600,
                  overwrite: bool = False) -> dict:
    fmt = str(format or "").upper()
    if fmt not in EXPORT_FORMATS:
        raise ValueError(f"format must be one of {sorted(EXPORT_FORMATS)}")
    target = Path(out_path)
    if target.is_dir():
        raise ValueError(f"out_path {out_path} is a folder; pass a file path")
    if target.exists() and not overwrite:
        raise ValueError(f"{out_path} already exists; pass overwrite=true")
    ws = resolve_workspace(state, workspace)
    rp = resolve_item(state, ws["id"], report, "Report")
    data, job = state.api().export_report_to_file(
        ws["id"], rp["id"], fmt, page_ids, timeout=timeout)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    result = {"ok": True, "report": _item_summary(rp), "format": fmt,
              "path": str(target), "bytes": len(data),
              "export_id": job.get("id"), "status": job.get("status"),
              "pages": page_ids}
    state.remember("export", result)
    return result


# --- MCP server registration ----------------------------------------------------

STATE = ServiceState()

mcp = Server(
    "pbi-service",
    instructions=(
        "Cloud companion to pbi-model / pbi-report: talks to the Power BI and "
        "Fabric REST APIs. Auth is resolved in order: token passed to "
        "pbi_service_login(token=...), env PBI_ACCESS_TOKEN, a service "
        "principal from AZURE_TENANT_ID/AZURE_CLIENT_ID/AZURE_CLIENT_SECRET, "
        "or an interactive device-code sign-in via "
        "pbi_service_login(device_code=True) (needs env PBI_CLIENT_ID). Check "
        "pbi_service_status() first. Workspaces, items and pipelines can be "
        "named by id or display name. To publish, call pbi_set_project(path) "
        "then pbi_publish_project(workspace). Cloud writes change nothing on "
        "disk, so they have no dry_run preview and cannot be undone."
    ),
)
tool = make_tool(mcp, STATE)


def _tool_result(value):
    return redact(value)


@tool(read=True, idempotent=True)
def pbi_service_status() -> dict:
    """Report the pbi-service session: which credential source is active
    (session token, env token, service principal, device code) with identity
    hints, whether msal is installed, the Fabric / Power BI base URLs, the
    selected local project and the ids of the last publish / refresh / deploy
    / export operations. Never includes token values. No arguments."""
    return _tool_result(service_status(STATE))


@tool(write=True, journaled=False, idempotent=True)
def pbi_service_login(token: str | None = None, device_code: bool = False,
                      wait_seconds: int = 0) -> dict:
    """Authenticate the session. token=<bearer token> stores it in memory for
    this session (highest precedence; obtain one with e.g. `az account
    get-access-token --resource https://analysis.windows.net/powerbi/api`).
    device_code=true starts an interactive sign-in through msal: the first
    call returns user_code + verification_uri for the user to complete; call
    again (optionally wait_seconds>0 to block that long) to poll and finish.
    Device-code needs env PBI_CLIENT_ID = your own public client app id; no id
    is built in. Returns the auth description with secrets redacted. Session
    only: nothing is written to disk. Cannot be previewed (no dry_run)."""
    return _tool_result(service_login(STATE, token, device_code, wait_seconds))


@tool(read=True, idempotent=True)
def pbi_set_project(path: str) -> dict:
    """Select the local Power BI Project (.pbip file, project folder, or a
    *.SemanticModel / *.Report folder) that pbi_publish_project uploads.
    Only needed for publishing; every other tool works without it. Returns
    the resolved layer folders."""
    return _tool_result(set_project(STATE, path))


@tool(read=True)
def pbi_list_workspaces() -> list[dict]:
    """List the Fabric workspaces visible to the signed-in identity: id,
    displayName, type, capacityId. Follows paging. No arguments."""
    return _tool_result(list_workspaces(STATE))


@tool(read=True)
def pbi_list_items(workspace: str, type: str | None = None) -> list[dict]:
    """List items in a workspace (by id or display name): id, displayName,
    type, description. type filters server-side, e.g. "SemanticModel",
    "Report", "Dashboard", "Lakehouse"."""
    return _tool_result(list_items(STATE, workspace, type))


@tool(write=True, journaled=False)
def pbi_get_item_definition(workspace: str, item: str, out_dir: str,
                            type: str | None = None,
                            overwrite: bool = False) -> dict:
    """Download an item's definition (SemanticModel as TMDL, Report as PBIR)
    and write it as a PBIP-shaped folder <out_dir>/<name>.<Type>/... that
    pbi-model / pbi-report can open. item is an id or display name; pass
    type when a report and a model share the name. Refuses a non-empty
    target folder unless overwrite=true. Writes only under out_dir (not the
    selected project), so it is not journaled: no dry_run, no pbi_undo.
    Returns the folder and the relative files written."""
    return _tool_result(get_item_definition(STATE, workspace, item, out_dir,
                                            type, overwrite))


@tool(write=True, journaled=False)
def pbi_publish_project(workspace: str, name: str | None = None,
                        update_if_exists: bool = True,
                        publish_model: bool = True,
                        publish_report: bool = True,
                        dataset: str | None = None) -> dict:
    """Publish the project selected with pbi_set_project to a workspace (id
    or name): the *.SemanticModel is created/updated first (TMDL parts, .pbi/
    cache excluded), then the *.Report is uploaded bound to that model
    (definition.pbir rewritten in memory to byConnection). name overrides the
    item display names (default: the layer folder names). update_if_exists=
    false refuses to overwrite an existing item. With publish_model=false,
    dataset names the already-published model to bind the report to. Cloud
    write: no local files change, no dry_run, no undo. Returns item ids,
    created/updated actions and part counts."""
    return _tool_result(publish_project(STATE, workspace, name,
                                        update_if_exists, publish_model,
                                        publish_report, dataset))


@tool(write=True, journaled=False)
def pbi_refresh_dataset(workspace: str, dataset: str, wait: bool = False,
                        timeout: int = 600) -> dict:
    """Trigger a refresh of a semantic model (dataset) named by id or display
    name in a workspace. wait=true polls the refresh history until the
    refresh completes or fails (up to timeout seconds) and returns the final
    entry; otherwise returns the accepted request id. Cloud write: no
    dry_run, no undo."""
    return _tool_result(refresh_dataset(STATE, workspace, dataset, wait, timeout))


@tool(read=True)
def pbi_refresh_status(workspace: str, dataset: str, top: int = 5) -> list[dict]:
    """Recent refresh history for a semantic model (dataset): requestId,
    refreshType, startTime, endTime, status (Unknown = in progress,
    Completed, Failed, Disabled) and the service exception if any."""
    return _tool_result(refresh_status(STATE, workspace, dataset, top))


@tool(read=True)
def pbi_list_deployment_pipelines() -> list[dict]:
    """List Power BI deployment pipelines the identity can access: id,
    displayName, description and stages (order, displayName, workspaceId).
    No arguments."""
    return _tool_result(list_deployment_pipelines(STATE))


@tool(write=True, destructive=True, journaled=False)
def pbi_deploy_pipeline_stage(pipeline: str, source_stage: int | str,
                              items: list[dict] | None = None,
                              wait: bool = True, timeout: int = 600,
                              note: str | None = None) -> dict:
    """Deploy from a pipeline stage to the next one (pipeline by id or name;
    source_stage as order 0/1/2 or name Development/Test/Production).
    items=None deploys everything; otherwise a selective deploy of
    [{"type": "SemanticModel"|"Report"|"Dashboard"|"Dataflow"|"Datamart",
    "id": "<source item id>"}, ...]. Existing target items are overwritten.
    wait=true polls the operation to completion (timeout seconds). Cloud
    write: no dry_run, no undo."""
    return _tool_result(deploy_pipeline_stage(STATE, pipeline, source_stage,
                                              items, wait, timeout, note))


@tool(write=True, journaled=False)
def pbi_export_report(workspace: str, report: str, out_path: str,
                      format: str = "PDF", page_ids: list[str] | None = None,
                      timeout: int = 600, overwrite: bool = False) -> dict:
    """Export a published report (id or display name) to a local file via the
    Power BI export-to-file API. format is PDF, PPTX or PNG; page_ids
    optionally restricts to page names (the PBIR page `name`, e.g.
    "ReportSection1234"). Polls until the export finishes (timeout seconds),
    then writes the bytes to out_path (refuses an existing file unless
    overwrite=true). Writes only out_path, so not journaled: no dry_run, no
    undo. Returns path, size and export status."""
    return _tool_result(export_report(STATE, workspace, report, out_path,
                                      format, page_ids, timeout, overwrite))


load_tool_modules(__package__ or "service_server", mcp, STATE, tool)


def main() -> None:
    """Entry point (pbi-service-server): run over stdio for an MCP client."""
    mcp.run()


if __name__ == "__main__":
    main()
