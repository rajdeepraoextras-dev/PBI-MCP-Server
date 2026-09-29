"""core/fabric_api: every client helper against a scripted urlopen, plus the
PBIP definition-part encoding from the synthetic fixture.

`FakeHttp` replaces ``urllib.request.urlopen``: it records every request
(method, URL, JSON body, headers) and serves scripted (status, body, headers)
responses per route, consuming them in order and repeating the last one.
Non-2xx statuses raise ``urllib.error.HTTPError`` exactly like urllib does.
"""

from __future__ import annotations

import base64
import email.message
import io
import json
import re
import shutil
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from core.fabric_api import (
    FABRIC_BASE as F,
    POWERBI_BASE as P,
    FabricApiError,
    FabricClient,
    collect_report_parts,
    collect_semantic_model_parts,
    decode_parts,
    encode_parts,
    iter_layer_files,
    parse_error_body,
    retry_after_seconds,
    rewrite_pbir_by_connection,
    write_parts,
)

SYNTH = Path(__file__).parent / "fixtures" / "synthetic"
TOKEN = "eyJhbGciOiJub25lIn0.eyJ1cG4iOiJ1c2VyQGV4YW1wbGUuY29tIn0.signature"


# --- fake HTTP layer -------------------------------------------------------

class Call:
    def __init__(self, method: str, url: str, body, headers: dict):
        self.method, self.url, self.body, self.headers = method, url, body, headers

    @property
    def path(self) -> str:
        return urllib.parse.urlsplit(self.url).path

    @property
    def query(self) -> dict:
        return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.url).query))

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{self.method} {self.url}>"


class FakeResponse:
    def __init__(self, status: int, headers, data: bytes):
        self.status, self.headers, self._data = status, headers, data

    def read(self) -> bytes:
        return self._data

    def getcode(self) -> int:
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _headers(headers: dict | None) -> email.message.Message:
    msg = email.message.Message()
    for k, v in (headers or {}).items():
        msg[k] = str(v)
    return msg


def _build(url: str, status: int, body=None, headers: dict | None = None):
    msg = _headers(headers)
    if isinstance(body, (dict, list)):
        data = json.dumps(body).encode("utf-8")
        if "Content-Type" not in msg:
            msg["Content-Type"] = "application/json"
    elif body is None:
        data = b""
    elif isinstance(body, str):
        data = body.encode("utf-8")
    else:
        data = bytes(body)
    if status >= 400:
        raise urllib.error.HTTPError(url, status, "error", msg, io.BytesIO(data))
    return FakeResponse(status, msg, data)


class FakeHttp:
    """Scripted stand-in for urllib.request.urlopen (see module doc)."""

    def __init__(self):
        self.calls: list[Call] = []
        self.routes: list[tuple[str, object, list]] = []

    def on(self, method: str, url, *responses) -> "FakeHttp":
        """Route `method url` (exact URL, 'prefix*' or compiled regex)."""
        assert responses, "a route needs at least one response"
        self.routes.append((method.upper(), url, list(responses)))
        return self

    @staticmethod
    def _match(pattern, url: str) -> bool:
        if isinstance(pattern, re.Pattern):
            return bool(pattern.search(url))
        if pattern.endswith("*"):
            return url.startswith(pattern[:-1])
        return url == pattern or url.split("?", 1)[0] == pattern

    def __call__(self, req, timeout=None):
        method = req.get_method()
        url = req.full_url
        body = req.data
        self.calls.append(Call(method, url,
                               json.loads(body) if body else None,
                               dict(req.header_items())))
        for m, pattern, responses in self.routes:
            if m == method and self._match(pattern, url):
                resp = responses.pop(0) if len(responses) > 1 else responses[0]
                return _build(url, *resp)
        raise AssertionError(f"unexpected request {method} {url}")

    def sent(self, method: str, path_part: str) -> list[Call]:
        return [c for c in self.calls if c.method == method and path_part in c.url]


class FakeClock:
    """Monotonic clock advanced by the client's own sleeps."""

    def __init__(self):
        self.t = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


class StaticToken:
    def __init__(self, token: str = TOKEN):
        self.token = token

    def get_token(self) -> str:
        return self.token


@pytest.fixture
def http(monkeypatch) -> FakeHttp:
    fake = FakeHttp()
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return fake


def make_client(**kw) -> FabricClient:
    clock = FakeClock()
    c = FabricClient(StaticToken(), sleep=clock.sleep, clock=clock, **kw)
    c.clock = clock  # type: ignore[attr-defined]
    return c


def b64(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return base64.b64encode(data).decode("ascii")


# --- plumbing ---------------------------------------------------------------

def test_request_sends_bearer_json_headers_and_body(http):
    http.on("POST", F + "/workspaces/ws1/items", (201, {"id": "i1"}))
    c = make_client()
    r = c.post("/workspaces/ws1/items", json_body={"displayName": "x"})
    assert r.status == 201 and r.json() == {"id": "i1"}
    call = http.calls[0]
    assert call.headers["Authorization"] == f"Bearer {TOKEN}"
    assert call.headers["Content-type"].startswith("application/json")
    assert call.headers["Accept"] == "application/json"
    assert call.body == {"displayName": "x"}


def test_query_params_drop_none_and_powerbi_base(http):
    http.on("GET", F + "/workspaces/ws1/items", (200, {"value": []}))
    http.on("GET", P + "/groups/g/datasets", (200, {"value": []}))
    c = make_client()
    c.get("/workspaces/ws1/items", params={"type": None})
    assert "?" not in http.calls[0].url
    c.get("/workspaces/ws1/items", params={"type": "Report"})
    assert http.calls[1].query == {"type": "Report"}
    c.get("/groups/g/datasets", base="powerbi")
    assert http.calls[2].url == P + "/groups/g/datasets"
    c.get(P + "/groups/g/datasets")  # absolute URLs pass through
    assert http.calls[3].url == P + "/groups/g/datasets"


def test_error_fabric_shape_without_headers(http):
    http.on("GET", F + "/workspaces/ws1/items/nope",
            (404, {"requestId": "r-1", "errorCode": "ItemNotFound",
                   "message": "Item not found",
                   "moreDetails": [{"message": "check the id"}]}))
    with pytest.raises(FabricApiError) as ei:
        make_client().get("/workspaces/ws1/items/nope")
    e = ei.value
    assert (e.status, e.code, e.request_id) == (404, "ItemNotFound", "r-1")
    assert "Item not found" in e.message and "check the id" in e.message
    text = str(e)
    assert "GET /v1/workspaces/ws1/items/nope" in text and "404" in text
    assert "Bearer" not in text and TOKEN not in text and "Authorization" not in text
    assert e.to_dict()["code"] == "ItemNotFound"


def test_error_powerbi_and_oauth_and_text_shapes(http):
    http.on("POST", P + "/groups/g/datasets/d/refreshes",
            (400, {"error": {"code": "InvalidRequest", "message": "bad",
                             "pbi.error": {"details": [
                                 {"detail": {"value": "x is required"}}]}}}))
    with pytest.raises(FabricApiError) as ei:
        make_client(max_retries=0).post("/groups/g/datasets/d/refreshes",
                                        json_body={}, base="powerbi")
    assert ei.value.code == "InvalidRequest" and "x is required" in ei.value.message

    assert parse_error_body(b'{"error":"invalid_grant","error_description":"exp"}') \
        == ("invalid_grant", "exp", None)
    assert parse_error_body(b"<html>oops</html>")[1] == "<html>oops</html>"
    assert parse_error_body(b"") == (None, None, None)


def test_retry_429_honours_retry_after(http):
    http.on("GET", F + "/workspaces",
            (429, {"errorCode": "TooManyRequests", "message": "slow"},
             {"Retry-After": "3"}),
            (200, {"value": [{"id": "w"}]}))
    c = make_client()
    assert c.list_workspaces() == [{"id": "w"}]
    assert c.clock.sleeps == [3.0] and len(http.calls) == 2


def test_retry_5xx_exponential_backoff_then_success(http):
    http.on("GET", F + "/workspaces", (503, None), (502, "gateway"),
            (200, {"value": []}))
    c = make_client()
    assert c.list_workspaces() == []
    assert c.clock.sleeps == [1.0, 2.0] and len(http.calls) == 3


def test_retry_gives_up_after_max_retries(http):
    http.on("GET", F + "/workspaces", (503, {"errorCode": "Busy", "message": "m"}))
    c = make_client(max_retries=2)
    with pytest.raises(FabricApiError) as ei:
        c.list_workspaces()
    assert ei.value.status == 503 and len(http.calls) == 3


def test_retry_after_http_date_is_parsed():
    import email.utils
    import time
    future = email.utils.formatdate(time.time() + 5, usegmt=True)
    delay = retry_after_seconds({"retry-after": future})
    assert delay is not None and 0 <= delay <= 6
    assert retry_after_seconds({"retry-after": "garbage"}) is None
    assert retry_after_seconds({}) is None


def test_connection_error_is_reported_after_retries(monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.URLError("dns failure")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    c = make_client(max_retries=1)
    with pytest.raises(FabricApiError) as ei:
        c.get("/workspaces")
    assert ei.value.status == 0 and ei.value.code == "ConnectionError"
    assert "dns failure" in ei.value.message and c.clock.sleeps == [1.0]


def test_get_paged_follows_uri_then_token(http):
    http.on("GET", F + "/workspaces",
            (200, {"value": [{"id": "a"}], "continuationToken": "T1",
                   "continuationUri": F + "/workspaces?continuationToken=T1"}))
    http.on("GET", F + "/workspaces?continuationToken=T1",
            (200, {"value": [{"id": "b"}], "continuationToken": "T2"}))
    http.on("GET", F + "/workspaces?continuationToken=T2",
            (200, {"value": [{"id": "c"}]}))
    # exact-URL routes above shadow the bare one for query URLs, so order the
    # routing so each page URL resolves to its own script
    http.routes.reverse()
    c = make_client()
    assert [w["id"] for w in c.list_workspaces()] == ["a", "b", "c"]
    urls = [k.url for k in http.calls]
    assert urls == [F + "/workspaces", F + "/workspaces?continuationToken=T1",
                    F + "/workspaces?continuationToken=T2"]


# --- long-running operations ---------------------------------------------

def test_lro_location_polls_with_retry_after_and_fetches_result(http):
    parts = [{"path": "definition.pbism", "payload": b64("{}"),
              "payloadType": "InlineBase64"},
             {"path": "definition/model.tmdl", "payload": b64("model Model"),
              "payloadType": "InlineBase64"}]
    http.on("POST", F + "/workspaces/ws/items/it/getDefinition",
            (202, None, {"Location": F + "/operations/op1",
                         "x-ms-operation-id": "op1", "Retry-After": "1"}))
    http.on("GET", F + "/operations/op1",
            (200, {"status": "Running", "percentComplete": 40}),
            (200, {"status": "Succeeded", "percentComplete": 100}))
    http.on("GET", F + "/operations/op1/result",
            (200, {"definition": {"parts": parts}}))
    c = make_client()
    got = c.get_item_definition("ws", "it", format="TMDL")
    assert got == {"definition.pbism": b"{}", "definition/model.tmdl": b"model Model"}
    assert http.calls[0].query == {"format": "TMDL"}
    assert c.clock.sleeps == [1.0, 2.0]
    assert [k.path for k in http.calls[1:]] == [
        "/v1/operations/op1", "/v1/operations/op1", "/v1/operations/op1/result"]


def test_lro_falls_back_to_operation_id_and_no_result_endpoint(http):
    http.on("POST", F + "/workspaces/ws/items/it/updateDefinition",
            (202, None, {"x-ms-operation-id": "op2"}))
    http.on("GET", F + "/operations/op2", (200, {"status": "Succeeded"}))
    http.on("GET", F + "/operations/op2/result",
            (400, {"errorCode": "OperationHasNoResult", "message": "none"}))
    c = make_client()
    out = c.update_item_definition("ws", "it", {"definition.pbism": b"{}"},
                                   format="TMDL", update_metadata=True)
    assert out == {"ok": True, "item_id": "it", "parts": 1}
    assert http.calls[0].query == {"updateMetadata": "true"}
    assert http.calls[1].path == "/v1/operations/op2"


def test_lro_failed_raises_service_error(http):
    http.on("POST", F + "/workspaces/ws/items", (202, None,
                                                  {"Location": F + "/operations/op3"}))
    http.on("GET", F + "/operations/op3",
            (200, {"status": "Failed", "requestId": "r9",
                   "error": {"errorCode": "InvalidDefinition",
                             "message": "table Sales is broken"}}))
    c = make_client()
    with pytest.raises(FabricApiError) as ei:
        c.create_item_with_definition("ws", "SemanticModel", "M",
                                      {"definition.pbism": b"{}"})
    assert ei.value.code == "InvalidDefinition" and "Sales" in ei.value.message
    assert ei.value.request_id == "r9"


def test_lro_times_out(http):
    http.on("POST", F + "/workspaces/ws/items/it/getDefinition",
            (202, None, {"Location": F + "/operations/op4"}))
    http.on("GET", F + "/operations/op4", (200, {"status": "Running"}))
    c = make_client(poll_interval=2.0)
    with pytest.raises(TimeoutError) as ei:
        c.get_item_definition("ws", "it", timeout=5)
    assert "op4" in str(ei.value)
    assert sum(c.clock.sleeps) >= 5


def test_lro_missing_location_is_an_error():
    from core.fabric_api import Response
    c = make_client()
    with pytest.raises(FabricApiError) as ei:
        c.wait_for_operation(Response(202, {}, b""))
    assert ei.value.code == "MissingOperationLocation"


# --- Fabric items ---------------------------------------------------------

def test_list_items_get_item_and_find_item(http):
    http.on("GET", F + "/workspaces/ws/items",
            (200, {"value": [{"id": "r1", "displayName": "Sales", "type": "Report"},
                             {"id": "m1", "displayName": "Sales",
                              "type": "SemanticModel"}]}))
    http.on("GET", F + "/workspaces/ws/items/m1",
            (200, {"id": "m1", "type": "SemanticModel"}))
    c = make_client()
    assert len(c.list_items("ws")) == 2
    assert len(c.list_items("ws", type="Report")) == 2  # fake ignores filter
    assert http.calls[1].query == {"type": "Report"}
    assert c.get_item("ws", "m1")["type"] == "SemanticModel"
    # same display name, two types: the type filter is re-checked locally
    assert c.find_item("ws", "sales", "SemanticModel")["id"] == "m1"
    assert c.find_item("ws", "SALES", "Report")["id"] == "r1"
    assert c.find_item("ws", "sales")["id"] == "r1"  # untyped: first match
    assert c.find_item("ws", "missing") is None


def test_create_item_with_definition_encodes_parts(http):
    http.on("POST", F + "/workspaces/ws/items",
            (201, {"id": "new", "type": "SemanticModel", "displayName": "M"}))
    c = make_client()
    item = c.create_item_with_definition(
        "ws", "SemanticModel", "M",
        {"definition.pbism": b'{"version":"4.0"}',
         "definition/model.tmdl": "model Model\n"},
        format="TMDL", description="d")
    assert item["id"] == "new"
    body = http.calls[0].body
    assert body["displayName"] == "M" and body["type"] == "SemanticModel"
    assert body["description"] == "d"
    assert body["definition"]["format"] == "TMDL"
    parts = {p["path"]: p for p in body["definition"]["parts"]}
    assert set(parts) == {"definition.pbism", "definition/model.tmdl"}
    assert all(p["payloadType"] == "InlineBase64" for p in parts.values())
    assert base64.b64decode(parts["definition/model.tmdl"]["payload"]) == b"model Model\n"


def test_create_item_via_lro_returns_result_item(http):
    http.on("POST", F + "/workspaces/ws/items",
            (202, None, {"Location": F + "/operations/op5", "Retry-After": "0"}))
    http.on("GET", F + "/operations/op5", (200, {"status": "Succeeded"}))
    http.on("GET", F + "/operations/op5/result",
            (200, {"id": "created", "type": "Report"}))
    c = make_client()
    item = c.create_item_with_definition("ws", "Report", "R",
                                         {"definition.pbir": b"{}"})
    assert item == {"id": "created", "type": "Report"}


# --- Power BI: refresh ----------------------------------------------------

def test_refresh_dataset_history_and_wait(http):
    http.on("POST", P + "/groups/g/datasets/d/refreshes",
            (202, None, {"RequestId": "req-1"}))
    http.on("GET", P + "/groups/g/datasets/d/refreshes",
            (200, {"value": [{"requestId": "req-1", "status": "Unknown"}]}),
            (200, {"value": [{"requestId": "other", "status": "Completed"},
                             {"requestId": "req-1", "status": "Completed",
                              "endTime": "t"}]}))
    c = make_client()
    started = c.refresh_dataset("g", "d")
    assert started == {"status": 202, "request_id": "req-1", "location": None}
    assert http.calls[0].body == {"notifyOption": "NoNotification"}

    entry = c.wait_for_refresh("g", "d", "req-1", timeout=100, interval=10)
    assert entry["status"] == "Completed" and entry["endTime"] == "t"
    assert c.clock.sleeps == [10.0]
    assert http.calls[1].query == {"$top": "10"}

    http.on("GET", P + "/groups/g/datasets/d/refreshes/req-1",
            (200, {"status": "Completed", "objects": []}))
    assert c.get_refresh("g", "d", "req-1")["status"] == "Completed"


def test_refresh_dataset_enhanced_body_and_history_top(http):
    http.on("POST", P + "/groups/g/datasets/d/refreshes",
            (202, None, {"RequestId": "req-2",
                         "Location": P + "/groups/g/datasets/d/refreshes/req-2"}))
    http.on("GET", P + "/groups/g/datasets/d/refreshes", (200, {"value": []}))
    c = make_client()
    out = c.refresh_dataset("g", "d", type="Full",
                            objects=[{"table": "Sales"}], max_parallelism=2)
    assert out["location"].endswith("/refreshes/req-2")
    assert http.calls[0].body == {"notifyOption": "NoNotification", "type": "Full",
                                  "objects": [{"table": "Sales"}],
                                  "maxParallelism": 2}
    assert c.get_refresh_history("g", "d", top=3) == []
    assert http.calls[1].query == {"$top": "3"}


def test_wait_for_refresh_times_out(http):
    http.on("GET", P + "/groups/g/datasets/d/refreshes",
            (200, {"value": [{"requestId": "req-1", "status": "Unknown"}]}))
    c = make_client()
    with pytest.raises(TimeoutError):
        c.wait_for_refresh("g", "d", "req-1", timeout=15, interval=10)


# --- Power BI: pipelines --------------------------------------------------

def test_list_pipelines_deploy_all_and_wait(http):
    http.on("GET", P + "/pipelines",
            (200, {"value": [{"id": "p1", "displayName": "Sales",
                              "stages": [{"order": 0}, {"order": 1}]}]}))
    http.on("POST", P + "/pipelines/p1/deployAll",
            (202, {"id": "op-1", "status": "NotStarted"}))
    http.on("GET", P + "/pipelines/p1/operations/op-1",
            (200, {"id": "op-1", "status": "Executing"}),
            (200, {"id": "op-1", "status": "Succeeded"}))
    c = make_client()
    assert c.list_pipelines()[0]["id"] == "p1"
    assert http.calls[0].query == {"$expand": "stages"}
    op = c.deploy_pipeline("p1", 0, note="release")
    assert op["id"] == "op-1"
    assert http.calls[1].body == {
        "sourceStageOrder": 0, "note": "release",
        "options": {"allowOverwriteArtifact": True, "allowCreateArtifact": True}}
    final = c.wait_for_pipeline_operation("p1", "op-1", timeout=60)
    assert final["status"] == "Succeeded" and len(c.clock.sleeps) == 1


def test_deploy_selective_maps_item_types(http):
    http.on("POST", P + "/pipelines/p1/deploy",
            (202, None, {"Location": P + "/pipelines/p1/operations/op-7"}))
    c = make_client()
    op = c.deploy_pipeline("p1", 1, items=[
        {"type": "SemanticModel", "id": "m1"}, {"type": "Report", "id": "r1"},
        {"type": "dataset", "id": "m2"}])
    assert op["id"] == "op-7" and op["status"] == "NotStarted"
    body = http.calls[0].body
    assert body["datasets"] == [{"sourceId": "m1"}, {"sourceId": "m2"}]
    assert body["reports"] == [{"sourceId": "r1"}]
    with pytest.raises(ValueError):
        c.deploy_pipeline("p1", 0, items=[{"type": "Notebook", "id": "x"}])
    with pytest.raises(ValueError):
        c.deploy_pipeline("p1", 0, items=[])


def test_pipeline_operation_failure_raises(http):
    http.on("GET", P + "/pipelines/p1/operations/op-9",
            (200, {"status": "Failed",
                   "error": {"errorCode": "DeployFailed", "message": "no capacity"}}))
    c = make_client()
    with pytest.raises(FabricApiError) as ei:
        c.wait_for_pipeline_operation("p1", "op-9")
    assert ei.value.code == "DeployFailed" and "capacity" in ei.value.message


# --- Power BI: export -----------------------------------------------------

def test_export_report_to_file_polls_then_downloads(http):
    http.on("POST", P + "/groups/g/reports/r/ExportTo",
            (202, {"id": "ex1", "status": "NotStarted"}, {"Retry-After": "1"}))
    http.on("GET", P + "/groups/g/reports/r/exports/ex1",
            (200, {"id": "ex1", "status": "Running", "percentComplete": 50},
             {"Retry-After": "4"}),
            (200, {"id": "ex1", "status": "Succeeded",
                   "resourceFileExtension": ".pdf"}))
    http.on("GET", P + "/groups/g/reports/r/exports/ex1/file",
            (200, b"%PDF-1.7 fake", {"Content-Type": "application/pdf"}))
    c = make_client()
    data, job = c.export_report_to_file("g", "r", "pdf", page_names=["ReportSection1"])
    assert data == b"%PDF-1.7 fake" and job["status"] == "Succeeded"
    assert "_retry_after" not in job
    assert http.calls[0].body == {
        "format": "PDF",
        "powerBIReportConfiguration": {"pages": [{"pageName": "ReportSection1"}]}}
    assert c.clock.sleeps == [1.0, 4.0]
    assert http.calls[-1].headers["Accept"] == "*/*"


def test_export_failure_and_missing_id(http):
    http.on("POST", P + "/groups/g/reports/r/ExportTo",
            (202, {"id": "ex2", "status": "Failed",
                   "error": {"code": "ExportError", "message": "too big"}}))
    c = make_client()
    with pytest.raises(FabricApiError) as ei:
        c.export_report_to_file("g", "r", "PDF")
    assert ei.value.code == "ExportError"

    http.on("POST", P + "/groups/g/reports/r2/ExportTo", (202, {"status": "x"}))
    with pytest.raises(FabricApiError) as ei:
        c.export_report_to_file("g", "r2", "PDF")
    assert ei.value.code == "MissingExportId"


# --- PBIP definition parts ------------------------------------------------

@pytest.fixture
def project(tmp_path) -> Path:
    dst = tmp_path / "proj"
    shutil.copytree(SYNTH, dst)
    return dst


def test_collect_semantic_model_parts_excludes_cache_and_backups(project):
    sm = project / "Synthetic.SemanticModel"
    (sm / ".pbi").mkdir()
    (sm / ".pbi" / "cache.abf").write_bytes(b"\x00binary")
    (sm / ".pbi" / "localSettings.json").write_text("{}")
    (sm / "definition" / "tables" / "Sales.tmdl.bak-20240101-000000").write_text("old")
    (sm / ".pbi-mcp").mkdir()
    (sm / ".pbi-mcp" / "x.json").write_text("{}")

    parts = collect_semantic_model_parts(sm)
    assert {"definition.pbism", ".platform", "definition/model.tmdl",
            "definition/tables/Sales.tmdl", "definition/tables/Date.tmdl",
            "definition/relationships.tmdl"} <= set(parts)
    assert not any(p.startswith((".pbi/", ".pbi-mcp/")) for p in parts)
    assert not any(".bak-" in p for p in parts)
    assert parts["definition/tables/Sales.tmdl"] == \
        (sm / "definition" / "tables" / "Sales.tmdl").read_bytes()
    assert [f.as_posix() for f in iter_layer_files(sm)] == sorted(parts)


def test_collect_report_parts_rewrites_pbir_in_memory_only(project):
    rp = project / "Synthetic.Report"
    (rp / ".pbi").mkdir()
    (rp / ".pbi" / "localSettings.json").write_text("{}")
    original = (rp / "definition.pbir").read_bytes()

    parts = collect_report_parts(rp, "0000-dataset-id")
    assert not any(p.startswith(".pbi/") for p in parts)
    assert {"definition.pbir", "definition/report.json",
            "definition/pages/pages.json",
            "definition/pages/overview/visuals/card1/visual.json"} <= set(parts)
    pbir = json.loads(parts["definition.pbir"].decode("utf-8"))
    assert pbir["version"] == "4.0"
    assert "byPath" not in pbir["datasetReference"]
    conn = pbir["datasetReference"]["byConnection"]
    assert conn["pbiModelDatabaseName"] == "0000-dataset-id"
    assert conn["connectionType"] == "pbiServiceXmlaStyleLive"
    assert conn["pbiModelVirtualServerName"] == "sobe_wowvirtualserver"
    # disk untouched: the local project stays byPath
    assert (rp / "definition.pbir").read_bytes() == original
    assert b"byPath" in original


def test_rewrite_pbir_preserves_other_keys_and_requires_dataset():
    out = rewrite_pbir_by_connection(
        b'\xef\xbb\xbf{"version": "1.0", "extra": true, '
        b'"datasetReference": {"byPath": {"path": "../x"}}}', "ds")
    data = json.loads(out)
    assert data["version"] == "1.0" and data["extra"] is True
    assert list(data["datasetReference"]) == ["byConnection"]
    with pytest.raises(ValueError):
        rewrite_pbir_by_connection({"version": "4.0"}, "")


def test_missing_required_files_raise(tmp_path):
    (tmp_path / "Empty.SemanticModel" / "definition").mkdir(parents=True)
    with pytest.raises(ValueError, match="definition.pbism"):
        collect_semantic_model_parts(tmp_path / "Empty.SemanticModel")
    (tmp_path / "Empty.Report").mkdir()
    with pytest.raises(ValueError, match="definition.pbir"):
        collect_report_parts(tmp_path / "Empty.Report", "ds")
    with pytest.raises(FileNotFoundError):
        iter_layer_files(tmp_path / "nope")


def test_encode_decode_roundtrip_and_passthrough():
    src = {"a/b.json": b"{}", "c.tmdl": "text \xe9"}
    encoded = encode_parts(src)
    assert all(p["payloadType"] == "InlineBase64" for p in encoded)
    assert decode_parts(encoded) == {"a/b.json": b"{}", "c.tmdl": "text \xe9".encode()}
    assert encode_parts(encoded) == encoded  # already encoded -> unchanged
    assert encode_parts([{"path": "x", "payload": b"y"}])[0]["path"] == "x"
    with pytest.raises(ValueError):
        decode_parts([{"path": "x", "payload": "", "payloadType": "Other"}])
    with pytest.raises(ValueError):
        encode_parts({"x": 42})


def test_write_parts_writes_tree_and_rejects_escapes(tmp_path):
    out = tmp_path / "out"
    written = write_parts({"definition.pbism": b"{}",
                           "definition/tables/Sales.tmdl": b"table Sales"}, out)
    assert sorted(written) == ["definition.pbism", "definition/tables/Sales.tmdl"]
    assert (out / "definition" / "tables" / "Sales.tmdl").read_bytes() == b"table Sales"
    with pytest.raises(ValueError):
        write_parts({"../evil.txt": b"x"}, out)
    with pytest.raises(ValueError):
        write_parts({str(tmp_path / "abs.txt"): b"x"}, out)
    assert not (tmp_path / "evil.txt").exists()
