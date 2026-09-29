"""Minimal Fabric / Power BI REST client for the pbi-service server.

Stdlib only (``urllib.request``) so nothing here couples to the httpx major
the installed ``mcp`` package happens to pull in. Covers exactly what the
service tools need:

* ``FabricClient.get/post/patch/delete`` with bearer auth, JSON bodies,
  retries with backoff on 429/5xx (honouring ``Retry-After``) and structured
  errors (``FabricApiError`` carries the service's error code/message and
  request id, never the request headers);
* ``get_paged`` following ``continuationToken`` / ``continuationUri``;
* ``wait_for_operation`` for Fabric long-running operations (202 +
  ``Location`` / ``x-ms-operation-id``), polled with a timeout, fetching
  ``/result`` when the operation has one;
* typed helpers over the Fabric Core "Items" API (list workspaces/items, get
  / create / update item definitions) and the Power BI REST API (dataset
  refresh + history, deployment pipelines, export-to-file with download);
* PBIP definition-part encoding: every file under ``*.SemanticModel`` /
  ``*.Report`` except Desktop's ``.pbi/`` cache (and our own ``.pbi-mcp`` /
  ``*.bak-*`` artefacts) becomes ``{"path", "payload": <base64>,
  "payloadType": "InlineBase64"}``; ``definition.pbir`` is rewritten in memory
  to a ``byConnection`` reference to the published semantic model because the
  service rejects ``byPath``.

Reference docs followed: Fabric REST "Core - Items" (Create Item, Get Item
Definition, Update Item Definition, Long running operations), the
"Semantic model definition" and "Report definition" item-definition pages,
Power BI REST "Datasets - Refresh Dataset In Group / Get Refresh History In
Group", "Pipelines - Deploy All / Selective Deploy / Get Pipeline Operation",
"Reports - Export To File In Group / Get Export To File Status / Get File".
None of the calls are verified against the live service in this repo's test
suite; every request shape is asserted against a scripted fake instead.
"""

from __future__ import annotations

import base64
import email.utils
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

FABRIC_BASE = "https://api.fabric.microsoft.com/v1"
POWERBI_BASE = "https://api.powerbi.com/v1.0/myorg"
USER_AGENT = "pbi-mcp-service"

RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_BACKOFF = 30.0

#: definition formats the Items API accepts per item type
DEFINITION_FORMATS = {"SemanticModel": "TMDL", "Report": "PBIR"}

#: folders that never belong in an item definition
SKIP_DIRS = {".pbi", ".pbi-mcp", ".git", "__pycache__", ".venv", "node_modules"}

#: Power BI deployment-pipeline request keys per item type
PIPELINE_ITEM_KEYS = {
    "semanticmodel": "datasets", "dataset": "datasets",
    "report": "reports", "dashboard": "dashboards",
    "dataflow": "dataflows", "datamart": "datamarts",
}

#: in-progress statuses for dataset refresh history entries
REFRESH_PENDING = {"unknown", "inprogress", "notstarted", ""}


class FabricApiError(RuntimeError):
    """A non-success response from the service (or no response at all).

    Carries ``status``, the service ``code`` / ``message`` and ``request_id``.
    The formatted message names the method + URL path only: never the query
    string, never request or response headers.
    """

    def __init__(self, status: int, code: str | None = None,
                 message: str | None = None, request_id: str | None = None,
                 method: str | None = None, url: str | None = None):
        self.status = status
        self.code = code or "Unknown"
        self.message = message or "no message"
        self.request_id = request_id
        self.method = method
        self.path = _path_only(url) if url else None
        super().__init__(self._format())

    def _format(self) -> str:
        where = f"{self.method} {self.path} -> " if self.method else ""
        rid = f" (requestId={self.request_id})" if self.request_id else ""
        return f"{where}HTTP {self.status} {self.code}: {self.message}{rid}"

    def to_dict(self) -> dict:
        return {"status": self.status, "code": self.code,
                "message": self.message, "request_id": self.request_id}


def _path_only(url: str) -> str:
    try:
        p = urllib.parse.urlsplit(url)
        return p.path or url
    except ValueError:
        return url


@dataclass
class Response:
    """One HTTP response: status, lower-cased headers, raw body."""
    status: int
    headers: dict = field(default_factory=dict)
    body: bytes = b""

    def header(self, name: str, default: str | None = None) -> str | None:
        return self.headers.get(name.lower(), default)

    def json(self) -> Any:
        if not self.body or not self.body.strip():
            return None
        return json.loads(self.body.decode("utf-8-sig"))


def parse_error_body(body: bytes) -> tuple[str | None, str | None, str | None]:
    """(code, message, requestId) from a Fabric or Power BI error body."""
    text = body.decode("utf-8-sig", errors="replace") if body else ""
    try:
        data = json.loads(text) if text.strip() else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        return None, text[:300] or None, None
    # Fabric: {"requestId", "errorCode", "message", "moreDetails": [...]}
    if "errorCode" in data:
        msg = data.get("message")
        details = data.get("moreDetails") or []
        extra = "; ".join(d.get("message", "") for d in details
                          if isinstance(d, dict) and d.get("message"))
        if extra:
            msg = f"{msg} [{extra}]" if msg else extra
        return data.get("errorCode"), msg, data.get("requestId")
    err = data.get("error")
    # Power BI: {"error": {"code", "message", "pbi.error": {...}}}
    if isinstance(err, dict):
        msg = err.get("message")
        pbi = err.get("pbi.error") or {}
        details = pbi.get("details") or []
        extra = "; ".join(str(d.get("detail", {}).get("value", ""))
                          for d in details if isinstance(d, dict)
                          and isinstance(d.get("detail"), dict))
        if extra.strip("; "):
            msg = f"{msg} [{extra}]" if msg else extra
        return err.get("code"), msg, data.get("requestId") or pbi.get("requestId")
    # OAuth-style: {"error": "invalid_grant", "error_description": "..."}
    if isinstance(err, str):
        return err, data.get("error_description"), None
    if data.get("message"):
        return data.get("code"), data.get("message"), data.get("requestId")
    return None, text[:300] or None, None


def retry_after_seconds(headers: dict) -> float | None:
    """Parse ``Retry-After`` (delta-seconds or HTTP-date) if present."""
    raw = headers.get("retry-after") if headers else None
    if raw is None:
        return None
    raw = str(raw).strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    return max(0.0, when.timestamp() - time.time())


class FabricClient:
    """Bearer-authenticated JSON client over urllib (see module doc).

    `token_provider` is anything with a ``get_token()`` method, or a plain
    callable returning the token. `sleep` / `clock` are injectable for tests.
    """

    def __init__(self, token_provider, *, fabric_base: str = FABRIC_BASE,
                 powerbi_base: str = POWERBI_BASE, timeout: float = 60.0,
                 max_retries: int = 5, poll_interval: float = 2.0,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self._provider = token_provider
        self.fabric_base = fabric_base.rstrip("/")
        self.powerbi_base = powerbi_base.rstrip("/")
        self.timeout = timeout
        self.max_retries = max(0, int(max_retries))
        self.poll_interval = poll_interval
        self._sleep = sleep
        self._clock = clock

    # --- plumbing ----------------------------------------------------------

    def _token(self) -> str:
        p = self._provider
        if hasattr(p, "get_token"):
            return p.get_token()
        return p()

    def url_for(self, path: str, base: str = "fabric") -> str:
        if path.startswith(("http://", "https://")):
            return path
        root = self.powerbi_base if base == "powerbi" else self.fabric_base
        return root + "/" + path.lstrip("/")

    def request(self, method: str, path: str, *, json_body: Any = None,
                params: dict | None = None, base: str = "fabric",
                headers: dict | None = None) -> Response:
        """Perform one request with retries; raise FabricApiError on >= 400."""
        url = self.url_for(path, base)
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                sep = "&" if "?" in url else "?"
                url = url + sep + urllib.parse.urlencode(clean)
        data = None
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")

        attempt = 0
        while True:
            hdrs = {
                "Authorization": f"Bearer {self._token()}",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            }
            if data is not None:
                hdrs["Content-Type"] = "application/json"
            if headers:
                hdrs.update(headers)
            req = urllib.request.Request(url, data=data, method=method.upper(),
                                         headers=hdrs)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    status = getattr(resp, "status", None) or resp.getcode()
                    resp_headers = _lower_headers(resp.headers)
                    body = resp.read()
            except urllib.error.HTTPError as e:
                status = e.code
                resp_headers = _lower_headers(getattr(e, "headers", None))
                try:
                    body = e.read()
                except Exception:  # noqa: BLE001 - body unreadable
                    body = b""
            except urllib.error.URLError as e:
                if attempt < self.max_retries:
                    self._sleep(min(2.0 ** attempt, MAX_BACKOFF))
                    attempt += 1
                    continue
                raise FabricApiError(0, "ConnectionError", str(e.reason),
                                     method=method.upper(), url=url) from e

            if status in RETRY_STATUSES and attempt < self.max_retries:
                delay = retry_after_seconds(resp_headers)
                if delay is None:
                    delay = min(2.0 ** attempt, MAX_BACKOFF)
                self._sleep(delay)
                attempt += 1
                continue
            if status >= 400:
                code, message, request_id = parse_error_body(body)
                raise FabricApiError(status, code, message,
                                     request_id or resp_headers.get("requestid"),
                                     method=method.upper(), url=url)
            return Response(status, resp_headers, body)

    def _delay(self, headers: dict) -> float:
        """Poll delay: the service's Retry-After when given, else poll_interval."""
        delay = retry_after_seconds(headers)
        return self.poll_interval if delay is None else delay

    def get(self, path: str, **kw) -> Response:
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw) -> Response:
        return self.request("POST", path, **kw)

    def patch(self, path: str, **kw) -> Response:
        return self.request("PATCH", path, **kw)

    def delete(self, path: str, **kw) -> Response:
        return self.request("DELETE", path, **kw)

    def get_paged(self, path: str, params: dict | None = None,
                  base: str = "fabric", key: str = "value") -> list:
        """GET every page, following continuationToken / continuationUri."""
        out: list = []
        url, query = path, dict(params or {})
        seen: set[str] = set()
        while True:
            data = self.get(url, params=query or None, base=base).json() or {}
            out.extend(data.get(key) or [])
            token = data.get("continuationToken")
            uri = data.get("continuationUri")
            if uri:
                if uri in seen:
                    break
                seen.add(uri)
                url, query = uri, {}
            elif token:
                if token in seen:
                    break
                seen.add(token)
                url = path
                query = {**dict(params or {}), "continuationToken": token}
            else:
                break
        return out

    # --- long-running operations -------------------------------------------

    def wait_for_operation(self, resp: Response, timeout: float = 600.0,
                           want_result: bool = True) -> Any:
        """Resolve a Fabric LRO. Non-202 responses return their JSON as-is.

        Polls the ``Location`` (or ``/operations/{x-ms-operation-id}``) URL
        honouring ``Retry-After`` until Succeeded/Failed or `timeout` seconds.
        On success returns ``GET <operation>/result`` when the operation has
        a result, else the final operation state.
        """
        if resp.status != 202:
            return resp.json()
        op_url = resp.header("location")
        if not op_url:
            op_id = resp.header("x-ms-operation-id")
            if op_id:
                op_url = f"{self.fabric_base}/operations/{op_id}"
        if not op_url:
            raise FabricApiError(202, "MissingOperationLocation",
                                 "202 Accepted without Location or "
                                 "x-ms-operation-id header")
        delay = self._delay(resp.headers)
        deadline = self._clock() + timeout
        state: dict = {}
        while True:
            self._sleep(delay)
            r = self.get(op_url)
            state = r.json() or {}
            status = str(state.get("status") or "").lower()
            if status == "succeeded":
                break
            if status in ("failed", "cancelled", "canceled"):
                err = state.get("error") or {}
                raise FabricApiError(
                    r.status, err.get("errorCode") or err.get("code")
                    or "OperationFailed",
                    err.get("message") or f"operation {status}",
                    state.get("requestId"), method="GET", url=op_url)
            if self._clock() >= deadline:
                raise TimeoutError(
                    f"Operation {_path_only(op_url)} still "
                    f"{status or 'running'} after {timeout:.0f}s; poll it "
                    "again later.")
            delay = self._delay(r.headers)
        if not want_result:
            return state
        try:
            rr = self.get(op_url.rstrip("/") + "/result")
        except FabricApiError as e:
            if e.status in (400, 404):
                return state
            raise
        return rr.json() if rr.body.strip() else state

    # --- Fabric: workspaces + items ----------------------------------------

    def list_workspaces(self) -> list[dict]:
        return self.get_paged("/workspaces")

    def get_workspace(self, workspace_id: str) -> dict:
        return self.get(f"/workspaces/{workspace_id}").json() or {}

    def list_items(self, workspace_id: str, type: str | None = None) -> list[dict]:
        return self.get_paged(f"/workspaces/{workspace_id}/items",
                              params={"type": type} if type else None)

    def get_item(self, workspace_id: str, item_id: str) -> dict:
        return self.get(f"/workspaces/{workspace_id}/items/{item_id}").json() or {}

    def find_item(self, workspace_id: str, display_name: str,
                  type: str | None = None) -> dict | None:
        """First item whose displayName matches (case-insensitive), or None.

        `type` is sent as the server-side filter and re-checked locally, so a
        report and a model sharing a name never get confused.
        """
        wanted = display_name.strip().lower()
        for item in self.list_items(workspace_id, type):
            if type and str(item.get("type", "")).lower() != type.lower():
                continue
            if str(item.get("displayName", "")).strip().lower() == wanted:
                return item
        return None

    def get_item_definition(self, workspace_id: str, item_id: str,
                            format: str | None = None,
                            timeout: float = 600.0) -> dict[str, bytes]:
        """Decoded definition parts as {path: bytes} (Get Item Definition)."""
        r = self.post(f"/workspaces/{workspace_id}/items/{item_id}/getDefinition",
                      params={"format": format} if format else None)
        data = self.wait_for_operation(r, timeout=timeout) or {}
        parts = (data.get("definition") or {}).get("parts") or []
        return decode_parts(parts)

    def create_item_with_definition(self, workspace_id: str, type: str,
                                    display_name: str, parts,
                                    format: str | None = None,
                                    description: str | None = None,
                                    timeout: float = 600.0) -> dict:
        """Create Item with an inline definition; returns the created item."""
        definition: dict = {"parts": encode_parts(parts)}
        if format:
            definition["format"] = format
        body: dict = {"displayName": display_name, "type": type,
                      "definition": definition}
        if description:
            body["description"] = description
        r = self.post(f"/workspaces/{workspace_id}/items", json_body=body)
        item = self.wait_for_operation(r, timeout=timeout)
        return item if isinstance(item, dict) else {}

    def update_item_definition(self, workspace_id: str, item_id: str, parts,
                               format: str | None = None,
                               update_metadata: bool = False,
                               timeout: float = 600.0) -> dict:
        """Update Item Definition (replaces every part)."""
        definition: dict = {"parts": encode_parts(parts)}
        if format:
            definition["format"] = format
        r = self.post(
            f"/workspaces/{workspace_id}/items/{item_id}/updateDefinition",
            json_body={"definition": definition},
            params={"updateMetadata": "true"} if update_metadata else None)
        self.wait_for_operation(r, timeout=timeout, want_result=False)
        return {"ok": True, "item_id": item_id, "parts": len(definition["parts"])}

    # --- Power BI: dataset refresh -----------------------------------------

    def refresh_dataset(self, group_id: str, dataset_id: str,
                        notify_option: str = "NoNotification",
                        type: str | None = None, objects: list | None = None,
                        commit_mode: str | None = None,
                        max_parallelism: int | None = None,
                        retry_count: int | None = None,
                        apply_refresh_policy: bool | None = None) -> dict:
        """Refresh Dataset In Group (202 Accepted; returns the request id)."""
        body: dict = {"notifyOption": notify_option}
        for key, val in (("type", type), ("objects", objects),
                         ("commitMode", commit_mode),
                         ("maxParallelism", max_parallelism),
                         ("retryCount", retry_count),
                         ("applyRefreshPolicy", apply_refresh_policy)):
            if val is not None:
                body[key] = val
        r = self.post(f"/groups/{group_id}/datasets/{dataset_id}/refreshes",
                      json_body=body, base="powerbi")
        return {"status": r.status, "request_id": r.header("requestid"),
                "location": r.header("location")}

    def get_refresh_history(self, group_id: str, dataset_id: str,
                            top: int = 5) -> list[dict]:
        r = self.get(f"/groups/{group_id}/datasets/{dataset_id}/refreshes",
                     params={"$top": top}, base="powerbi")
        return (r.json() or {}).get("value") or []

    def get_refresh(self, group_id: str, dataset_id: str,
                    refresh_id: str) -> dict:
        r = self.get(f"/groups/{group_id}/datasets/{dataset_id}/refreshes/"
                     f"{refresh_id}", base="powerbi")
        return r.json() or {}

    def wait_for_refresh(self, group_id: str, dataset_id: str,
                         request_id: str | None = None, timeout: float = 600.0,
                         interval: float = 10.0) -> dict:
        """Poll refresh history until the refresh leaves the Unknown state."""
        deadline = self._clock() + timeout
        while True:
            history = self.get_refresh_history(group_id, dataset_id, top=10)
            entry = None
            if request_id:
                entry = next((h for h in history
                              if h.get("requestId") == request_id), None)
            elif history:
                entry = history[0]
            status = str((entry or {}).get("status") or "").lower()
            if entry is not None and status not in REFRESH_PENDING:
                return entry
            if self._clock() >= deadline:
                raise TimeoutError(
                    f"Refresh {request_id or '(latest)'} still "
                    f"{status or 'pending'} after {timeout:.0f}s; check "
                    "pbi_refresh_status later.")
            self._sleep(interval)

    # --- Power BI: deployment pipelines ------------------------------------

    def list_pipelines(self) -> list[dict]:
        return self.get_paged("/pipelines", params={"$expand": "stages"},
                              base="powerbi")

    def get_pipeline(self, pipeline_id: str) -> dict:
        r = self.get(f"/pipelines/{pipeline_id}", params={"$expand": "stages"},
                     base="powerbi")
        return r.json() or {}

    def deploy_pipeline(self, pipeline_id: str, source_stage_order: int,
                        items: list[dict] | None = None,
                        options: dict | None = None, note: str | None = None,
                        is_backward: bool = False) -> dict:
        """Deploy All (items=None) or Selective Deploy from a stage.

        `items`: ``[{"type": "SemanticModel"|"Report"|"Dashboard"|"Dataflow"|
        "Datamart", "id": "<source item id>"}, ...]``. Returns the pipeline
        operation (poll with wait_for_pipeline_operation).
        """
        opts = {"allowOverwriteArtifact": True, "allowCreateArtifact": True}
        opts.update(options or {})
        body: dict = {"sourceStageOrder": int(source_stage_order), "options": opts}
        if note:
            body["note"] = note
        if is_backward:
            body["isBackwardDeployment"] = True
        if items is None:
            action = "deployAll"
        else:
            action = "deploy"
            if not items:
                raise ValueError("items must list at least one item, or be "
                                 "omitted to deploy everything")
            for it in items:
                key = PIPELINE_ITEM_KEYS.get(str(it.get("type", "")).lower())
                if key is None or not it.get("id"):
                    raise ValueError(
                        "each item needs {'type': one of "
                        f"{sorted(set(PIPELINE_ITEM_KEYS))}, 'id': <source item "
                        f"id>}}; got {it!r}")
                body.setdefault(key, []).append({"sourceId": it["id"]})
        r = self.post(f"/pipelines/{pipeline_id}/{action}", json_body=body,
                      base="powerbi")
        op = r.json() or {}
        if not op.get("id"):
            loc = r.header("location") or ""
            if "/operations/" in loc:
                op["id"] = loc.rstrip("/").rsplit("/", 1)[-1]
        op.setdefault("status", "NotStarted")
        return op

    def get_pipeline_operation(self, pipeline_id: str, operation_id: str) -> dict:
        r = self.get(f"/pipelines/{pipeline_id}/operations/{operation_id}",
                     base="powerbi")
        return r.json() or {}

    def wait_for_pipeline_operation(self, pipeline_id: str, operation_id: str,
                                    timeout: float = 600.0,
                                    interval: float | None = None) -> dict:
        deadline = self._clock() + timeout
        while True:
            op = self.get_pipeline_operation(pipeline_id, operation_id)
            status = str(op.get("status") or "").lower()
            if status == "succeeded":
                return op
            if status in ("failed", "cancelled", "canceled"):
                err = op.get("error") or {}
                raise FabricApiError(
                    200, err.get("errorCode") or err.get("code")
                    or "DeploymentFailed",
                    err.get("message") or err.get("errorDetails")
                    or f"deployment {status}", op.get("requestId"))
            if self._clock() >= deadline:
                raise TimeoutError(
                    f"Pipeline operation {operation_id} still "
                    f"{status or 'pending'} after {timeout:.0f}s")
            self._sleep(interval or self.poll_interval)

    # --- Power BI: export to file ------------------------------------------

    def start_export(self, group_id: str, report_id: str, format: str = "PDF",
                     page_names: list[str] | None = None,
                     settings: dict | None = None,
                     report_level_filters: list[dict] | None = None) -> dict:
        body: dict = {"format": str(format).upper()}
        cfg: dict = {}
        if page_names:
            cfg["pages"] = [{"pageName": p} for p in page_names]
        if settings:
            cfg["settings"] = settings
        if report_level_filters:
            cfg["reportLevelFilters"] = report_level_filters
        if cfg:
            body["powerBIReportConfiguration"] = cfg
        r = self.post(f"/groups/{group_id}/reports/{report_id}/ExportTo",
                      json_body=body, base="powerbi")
        job = r.json() or {}
        job.setdefault("_retry_after", retry_after_seconds(r.headers))
        return job

    def get_export_status(self, group_id: str, report_id: str,
                          export_id: str) -> dict:
        r = self.get(f"/groups/{group_id}/reports/{report_id}/exports/"
                     f"{export_id}", base="powerbi")
        job = r.json() or {}
        job.setdefault("_retry_after", retry_after_seconds(r.headers))
        return job

    def download_export(self, group_id: str, report_id: str,
                        export_id: str) -> bytes:
        r = self.get(f"/groups/{group_id}/reports/{report_id}/exports/"
                     f"{export_id}/file", base="powerbi",
                     headers={"Accept": "*/*"})
        return r.body

    def export_report_to_file(self, group_id: str, report_id: str,
                              format: str = "PDF",
                              page_names: list[str] | None = None,
                              timeout: float = 600.0,
                              settings: dict | None = None) -> tuple[bytes, dict]:
        """Start an export, poll it to completion, download the file bytes."""
        job = self.start_export(group_id, report_id, format, page_names, settings)
        export_id = job.get("id")
        if not export_id:
            raise FabricApiError(202, "MissingExportId",
                                 "ExportTo response carried no export id")
        deadline = self._clock() + timeout
        while True:
            status = str(job.get("status") or "").lower()
            if status == "succeeded":
                break
            if status == "failed":
                err = job.get("error") or {}
                raise FabricApiError(
                    200, err.get("code") or "ExportFailed",
                    err.get("message") or "export failed",
                    job.get("requestId"))
            if self._clock() >= deadline:
                raise TimeoutError(
                    f"Export {export_id} still {status or 'pending'} "
                    f"({job.get('percentComplete', 0)}%) after {timeout:.0f}s")
            wait = job.get("_retry_after")
            self._sleep(self.poll_interval if wait is None else wait)
            job = self.get_export_status(group_id, report_id, export_id)
        data = self.download_export(group_id, report_id, export_id)
        job.pop("_retry_after", None)
        return data, job


def _lower_headers(headers) -> dict:
    if headers is None:
        return {}
    items = headers.items() if hasattr(headers, "items") else []
    return {str(k).lower(): v for k, v in items}


# --- definition parts ----------------------------------------------------------

def encode_parts(parts) -> list[dict]:
    """{path: bytes|str} (or already-encoded part dicts) -> InlineBase64 parts."""
    if isinstance(parts, dict):
        items = parts.items()
    else:
        out: list[dict] = []
        for p in parts:
            if isinstance(p, dict) and "payloadType" in p:
                out.append(p)
            elif isinstance(p, dict) and "path" in p:
                out.append(_part(p["path"], p.get("payload", b"")))
            else:
                raise ValueError(f"unrecognised definition part {p!r}")
        return out
    return [_part(path, data) for path, data in items]


def _part(path: str, data) -> dict:
    if isinstance(data, str):
        data = data.encode("utf-8")
    if not isinstance(data, (bytes, bytearray)):
        raise ValueError(f"part {path!r}: payload must be bytes or str")
    return {"path": Path(path).as_posix(),
            "payload": base64.b64encode(bytes(data)).decode("ascii"),
            "payloadType": "InlineBase64"}


def decode_parts(parts: list[dict]) -> dict[str, bytes]:
    """InlineBase64 parts -> {path: bytes}."""
    out: dict[str, bytes] = {}
    for p in parts or []:
        ptype = p.get("payloadType", "InlineBase64")
        if ptype != "InlineBase64":
            raise ValueError(f"part {p.get('path')!r}: unsupported payloadType "
                             f"{ptype!r} (expected InlineBase64)")
        out[str(p["path"]).replace("\\", "/")] = base64.b64decode(p.get("payload") or "")
    return out


def _skip_file(name: str) -> bool:
    return ".bak-" in name or ".tmp-" in name


def iter_layer_files(layer_dir: str | Path) -> list[Path]:
    """Files in a *.SemanticModel / *.Report folder that belong in a definition.

    Skips Desktop's ``.pbi/`` cache, our ``.pbi-mcp`` journal, VCS folders and
    ``*.bak-*`` / ``*.tmp-*`` artefacts. Sorted, relative paths.
    """
    root = Path(layer_dir)
    if not root.is_dir():
        raise FileNotFoundError(f"{root} is not a folder")
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            if _skip_file(name):
                continue
            out.append((Path(dirpath) / name).relative_to(root))
    return out


def collect_semantic_model_parts(sm_dir: str | Path) -> dict[str, bytes]:
    """Every definition part of a *.SemanticModel folder (TMDL layout).

    ``definition.pbism`` is required by the service; ``.platform`` is passed
    through when present (the Items API accepts it as a part); ``.pbi/`` is
    Desktop cache and never uploaded.
    """
    root = Path(sm_dir)
    files = iter_layer_files(root)
    rel = {f.as_posix() for f in files}
    if "definition.pbism" not in rel:
        raise ValueError(f"{root} has no definition.pbism; is it a PBIP "
                         "semantic model saved with the TMDL format?")
    if not any(p.startswith("definition/") for p in rel):
        raise ValueError(f"{root} has no definition/ folder (TMDL files)")
    return {f.as_posix(): (root / f).read_bytes() for f in files}


def rewrite_pbir_by_connection(pbir: bytes | str | dict,
                               dataset_id: str) -> bytes:
    """definition.pbir -> byConnection reference to a published model.

    The REST API rejects ``byPath`` (a local folder reference), so the report
    is bound to the semantic model item `dataset_id` in the same workspace
    using the connection shape Desktop itself writes for live-connected
    reports. Other top-level keys (``version`` ...) are preserved.
    """
    if isinstance(pbir, (bytes, bytearray)):
        data = json.loads(bytes(pbir).decode("utf-8-sig"))
    elif isinstance(pbir, str):
        data = json.loads(pbir)
    else:
        data = dict(pbir)
    if not dataset_id:
        raise ValueError("dataset_id is required to bind the report")
    data = dict(data)
    data.setdefault("version", "4.0")
    data["datasetReference"] = {
        "byConnection": {
            "connectionString": None,
            "pbiServiceModelId": None,
            "pbiModelVirtualServerName": "sobe_wowvirtualserver",
            "pbiModelDatabaseName": dataset_id,
            "name": "EntityDataSource",
            "connectionType": "pbiServiceXmlaStyleLive",
        }
    }
    return (json.dumps(data, indent=2) + "\n").encode("utf-8")


def collect_report_parts(report_dir: str | Path,
                         dataset_id: str) -> dict[str, bytes]:
    """Every definition part of a *.Report folder, bound to `dataset_id`.

    ``definition.pbir`` is rewritten in memory (never on disk) to
    ``byConnection``; ``.pbi/`` (localSettings etc.) is excluded.
    """
    root = Path(report_dir)
    files = iter_layer_files(root)
    parts = {f.as_posix(): (root / f).read_bytes() for f in files}
    if "definition.pbir" not in parts:
        raise ValueError(f"{root} has no definition.pbir; is it a PBIP report "
                         "saved with the PBIR (enhanced metadata) format?")
    if "definition/report.json" not in parts:
        raise ValueError(f"{root} has no definition/report.json (PBIR "
                         "enhanced metadata is required to publish)")
    parts["definition.pbir"] = rewrite_pbir_by_connection(
        parts["definition.pbir"], dataset_id)
    return parts


def write_parts(parts: dict[str, bytes], out_dir: str | Path) -> list[str]:
    """Write decoded parts under `out_dir`; refuses paths that escape it."""
    root = Path(out_dir)
    root.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    resolved_root = root.resolve()
    for rel, data in parts.items():
        rel_path = Path(rel.replace("\\", "/"))
        if rel_path.is_absolute() or ".." in rel_path.parts:
            raise ValueError(f"refusing to write definition part {rel!r}: "
                             "path escapes the output folder")
        target = root / rel_path
        if resolved_root not in target.resolve().parents:
            raise ValueError(f"refusing to write definition part {rel!r}: "
                             "path escapes the output folder")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        written.append(rel_path.as_posix())
    return written


__all__ = [
    "FABRIC_BASE", "POWERBI_BASE", "DEFINITION_FORMATS", "FabricApiError",
    "FabricClient", "Response", "parse_error_body", "retry_after_seconds",
    "encode_parts", "decode_parts", "iter_layer_files",
    "collect_semantic_model_parts", "collect_report_parts",
    "rewrite_pbir_by_connection", "write_parts",
]
