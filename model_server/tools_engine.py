"""Live-engine tools for the model server (optional, Windows + Power BI Desktop).

Read-only tools that talk to the Analysis Services instance behind an open
Power BI Desktop window (or a pinned remote XMLA endpoint): evaluate DAX,
have the engine compile a measure, count rows, VertiPaq column statistics,
table previews. Nothing here touches the project files.

Connection resolution, in order: explicit ``port``/``database`` arguments,
the endpoint pinned with ``pbi_engine_connect``, then the running Desktop
instance whose tables match the selected project (cached briefly; a lost
connection is re-resolved once). Passwords in connection strings are kept
in memory only and redacted from every response.
"""

from __future__ import annotations

import sys
import time
from typing import TYPE_CHECKING

from core import engine

if TYPE_CHECKING:  # pragma: no cover - type hints only, never at import time
    from model_server.server import ModelState

RESOLVE_TTL = 30.0   # seconds a matched local connection is reused


# --- session state ----------------------------------------------------------------

def _session(state: ModelState) -> dict:
    """Per-session engine state, stored on the server state object."""
    sess = getattr(state, "engine_session", None)
    if sess is None:
        sess = {"pinned": None, "resolved": None, "resolved_at": 0.0,
                "resolved_for": None}
        try:
            setattr(state, "engine_session", sess)
        except AttributeError:   # a read-only state double: keep it local
            pass
    return sess


def _forget(state: ModelState) -> None:
    sess = _session(state)
    sess["resolved"] = None
    sess["resolved_at"] = 0.0
    sess["resolved_for"] = None


def _project_key(state: ModelState) -> str | None:
    project = getattr(state, "project", None)
    return None if project is None else str(project.path)


def _instance_summary(instances: list[dict]) -> str:
    parts = []
    for inst in instances:
        dbs = ", ".join(
            f"{db['name']} ({len(db.get('tables', []))} tables)"
            for db in inst.get("databases", [])) or "no database loaded"
        parts.append(f"port {inst['port']}: {dbs}")
    return "; ".join(parts) if parts else "none"


def resolve_connection(state: ModelState, port: int | None = None,
                       database: str | None = None) -> engine.Connection:
    """Pick the engine endpoint a tool call should use (see module docstring)."""
    if port is not None:
        port = int(port)
        if database:
            return engine.local_connection(port, database, source="explicit")
        inst = next((i for i in engine.discover_instances() if i["port"] == port), None)
        if inst is None:
            raise ValueError(
                f"No reachable Power BI Desktop instance listens on port {port}. "
                "pbi_engine_status lists the running instances.")
        if not inst["databases"]:
            raise engine.EngineUnavailable(
                f"The Desktop instance on port {port} has no database loaded yet; "
                "wait for the model to finish loading and retry.")
        return engine.local_connection(port, inst["databases"][0]["name"], source="explicit")
    if database is not None:
        for inst in engine.discover_instances():
            if any(db["name"] == database for db in inst["databases"]):
                return engine.local_connection(inst["port"], database, source="explicit")
        raise ValueError(
            f"No running Desktop instance hosts a database named {database!r}. "
            "pbi_engine_status lists the running instances and their databases.")

    sess = _session(state)
    if sess["pinned"] is not None:
        return sess["pinned"]
    key = _project_key(state)
    cached = sess["resolved"]
    if (cached is not None and sess["resolved_for"] == key
            and time.monotonic() - sess["resolved_at"] < RESOLVE_TTL):
        return cached

    instances = engine.discover_instances()
    project = getattr(state, "project", None)
    if project is not None:
        match = engine.match_instance(project, instances)
        if match is not None:
            conn = engine.local_connection(match["port"], match["database"])
            conn.match = match
            sess["resolved"], sess["resolved_at"] = conn, time.monotonic()
            sess["resolved_for"] = key
            return conn
    if not instances:
        raise engine.EngineUnavailable(
            "No running Power BI Desktop instance was found. " + engine.HOW_TO_START)
    if project is not None:
        raise engine.EngineUnavailable(
            f"{len(instances)} Desktop instance(s) are running but none hosts the "
            f"tables of {project.path} ({_instance_summary(instances)}). Open this "
            "project in Power BI Desktop, or pass port/database explicitly.")
    dbs = [(i, db) for i in instances for db in i["databases"]]
    if len(dbs) == 1:
        inst, db = dbs[0]
        conn = engine.local_connection(inst["port"], db["name"])
        sess["resolved"], sess["resolved_at"] = conn, time.monotonic()
        sess["resolved_for"] = key
        return conn
    raise engine.EngineUnavailable(
        "No project is selected and several Desktop databases are running "
        f"({_instance_summary(instances)}). Call pbi_set_project so the instance "
        "can be matched by table names, or pass port/database explicitly.")


def _run(state: ModelState, fn, port: int | None = None,
         database: str | None = None) -> dict:
    """Resolve a connection, run ``fn(conn)``; re-resolve once if it vanished."""
    conn = resolve_connection(state, port, database)
    try:
        result = fn(conn)
    except engine.EngineUnavailable:
        if conn.source != "local" or port is not None or database is not None:
            raise
        _forget(state)
        conn = resolve_connection(state)
        result = fn(conn)
    result["connection"] = conn.describe()
    return result


# --- tool logic ---------------------------------------------------------------------

def engine_status(state: ModelState) -> dict:
    """What the live-engine tools can reach right now. Never raises."""
    out: dict = {
        "platform": sys.platform,
        "ok": False,
        "dll": None,
        "dll_error": None,
        "instances": [],
        "discovery_error": None,
        "project": None,
        "matched": None,
        "pinned_connection": None,
        "reason": None,
    }
    try:
        out["dll"] = engine.find_adomd_dll()
    except Exception as exc:  # noqa: BLE001 - status must never raise
        out["dll_error"] = str(exc)
    try:
        out["instances"] = engine.discover_instances(include_errors=True)
    except Exception as exc:  # noqa: BLE001
        out["discovery_error"] = str(exc)
    project = getattr(state, "project", None)
    if project is not None:
        out["project"] = str(project.path)
        try:
            live = [i for i in out["instances"] if i.get("reachable") and not i.get("error")]
            out["matched"] = engine.match_instance(project, live)
        except Exception as exc:  # noqa: BLE001
            out["match_error"] = str(exc)
    pinned = _session(state)["pinned"]
    if pinned is not None:
        out["pinned_connection"] = pinned.describe()

    live = [i for i in out["instances"] if i.get("reachable") and not i.get("error")]
    if pinned is not None:
        out["ok"], out["reason"] = True, "using the pinned connection"
    elif out["matched"] is not None:
        out["ok"] = True
        out["reason"] = (f"project matched to Desktop on port {out['matched']['port']}, "
                         f"database {out['matched']['database']}")
    elif out["dll_error"]:
        out["reason"] = out["dll_error"]
    elif out["discovery_error"]:
        out["reason"] = out["discovery_error"]
    elif not live:
        errors = [i["error"] for i in out["instances"] if i.get("error")]
        out["reason"] = ("No running Power BI Desktop instance. " + engine.HOW_TO_START
                         + (f" (skipped: {'; '.join(errors)})" if errors else ""))
    elif project is None:
        out["ok"] = len([db for i in live for db in i["databases"]]) == 1
        out["reason"] = ("No project selected; " + ("the single running database will "
                         "be used" if out["ok"] else "call pbi_set_project or pass "
                         "port/database") + f" ({_instance_summary(live)})")
    else:
        out["reason"] = (f"Desktop is running ({_instance_summary(live)}) but no "
                         "database shares table names with the selected project. "
                         "Open this project in Desktop or pass port/database.")
    return out


def evaluate_dax(state: ModelState, query: str, max_rows: int = 200,
                 port: int | None = None,
                 database: str | None = None) -> dict:
    """Run a DAX or DMV query against the live model."""
    return _run(state, lambda c: engine.execute_dax(c, query, max_rows=max_rows),
                port, database)


def validate_dax(state: ModelState, dax: str, table: str | None = None) -> dict:
    """Have the engine compile a measure expression (nothing is persisted)."""
    return _run(state, lambda c: engine.validate_dax(c, dax, table))


def table_row_counts(state: ModelState) -> dict:
    """Row count per table in the live model."""
    return _run(state, engine.table_row_counts)


def column_stats(state: ModelState, table: str | None = None) -> dict:
    """VertiPaq column statistics, optionally for one table."""
    return _run(state, lambda c: engine.column_stats(c, table))


def preview_table(state: ModelState, table: str, top: int = 20) -> dict:
    """First ``top`` rows of a table."""
    return _run(state, lambda c: engine.preview_table(c, table, top))


def engine_connect(state: ModelState, connection_string: str) -> dict:
    """Pin a connection string for the session (memory only); "" clears it."""
    sess = _session(state)
    if connection_string is None or not str(connection_string).strip():
        sess["pinned"] = None
        _forget(state)
        return {"ok": True, "pinned": None,
                "message": "Pinned connection cleared; Desktop auto-discovery is back on."}
    cs = str(connection_string).strip()
    if "=" not in cs:
        raise ValueError(
            "connection_string must be an ADOMD connection string, e.g. "
            "Data Source=powerbi://api.powerbi.com/v1.0/myorg/<Workspace>;"
            "Initial Catalog=<Dataset>;User ID=;Password=<access token>")
    conn = engine.Connection(cs, source="pinned")
    try:
        probe = engine.execute_dax(conn, 'EVALUATE ROW("v", 1)', max_rows=1)
    except engine.QueryError as exc:
        raise ValueError(f"Connected, but the probe query failed: {exc}") from None
    sess["pinned"] = conn
    _forget(state)
    return {"ok": True, "pinned": conn.describe(), "probe_ms": probe["elapsed_ms"],
            "message": "Connection pinned for this session (password kept in memory only)."}


# --- MCP registration -------------------------------------------------------------------

def register(mcp, state, tool) -> None:
    """Register the pbi_engine_* / pbi_*_dax tools on the model server."""

    @tool(read=True)
    def pbi_engine_status() -> dict:
        """Report what the live-engine tools can reach: the ADOMD DLL in use,
        every running Power BI Desktop Analysis Services instance (port,
        databases, table names), the instance matched to the selected project
        by table names, any pinned remote connection, and otherwise the reason
        nothing is reachable. Never raises; safe to call first."""
        return engine_status(state)

    @tool(read=True)
    def pbi_evaluate_dax(query: str, max_rows: int = 200, port: int | None = None,
                         database: str | None = None) -> dict:
        """Run a DAX query (EVALUATE ...) or a DMV (SELECT * FROM $SYSTEM....)
        against the live model of the selected project in Power BI Desktop (or
        the pinned connection); the running instance is matched to the project
        by table names. Pass port/database to target a specific instance
        (pbi_engine_status lists them). Returns columns, rows (JSON scalars:
        null for blank, dates as ISO 8601, "Infinity"/"NaN" as strings),
        row_count, truncated (true when max_rows cut the result), elapsed_ms
        and the connection used. Read-only: nothing is written anywhere."""
        return evaluate_dax(state, query, max_rows, port, database)

    @tool(read=True)
    def pbi_validate_dax(dax: str, table: str | None = None) -> dict:
        """Compile a measure expression against the real model before
        creating it. `dax` is the bare expression (e.g. SUM(Sales[Amount])),
        not a query and without a "Name =" prefix; pass `table` to define it on
        that table so row context and table-relative references resolve as
        they would for a real measure. The engine evaluates it once via a
        query-scoped measure, so nothing is persisted. Returns {ok, error
        (the engine's message, with positions relative to `dax`, when ok is
        false), value (the result when ok), query, connection}. Use it before
        pbi_create_measure / pbi_update_measure."""
        return validate_dax(state, dax, table)

    @tool(read=True)
    def pbi_table_row_counts() -> dict:
        """Row count of every table in the live model (from the storage engine
        DMV, falling back to COUNTROWS). Returns {tables: [{name, rows,
        internal}], source, connection}; `rows` is null for tables without
        loaded storage and `internal` marks Desktop's auto date/time helper
        tables. A freshly opened .pbip has no data until it is refreshed in
        Desktop, so every count is 0 until then."""
        return table_row_counts(state)

    @tool(read=True)
    def pbi_column_stats(table: str | None = None) -> dict:
        """VertiPaq statistics per column from the live model, largest first:
        cardinality (distinct values), rows, encoding (HASH or VALUE),
        data_type, and sizes in bytes -- dictionary_size, data_size,
        hierarchy_size, total_size. Also returns per-table totals. Optional
        `table` filters to one table. Use it to find the columns that make
        the model big or slow (high-cardinality text columns first)."""
        return column_stats(state, table)

    @tool(read=True)
    def pbi_preview_table(table: str, top: int = 20) -> dict:
        """First `top` rows of a table from the live model via EVALUATE
        TOPN(top, 'table'). Column names come back as Table[Column]. Returns
        columns, rows, row_count, truncated, elapsed_ms, connection."""
        return preview_table(state, table, top)

    @tool(read=True, idempotent=True)
    def pbi_engine_connect(connection_string: str) -> dict:
        """Pin an XMLA connection string for this session so the engine tools
        query it instead of a local Desktop instance, e.g. "Data Source=
        powerbi://api.powerbi.com/v1.0/myorg/<Workspace>;Initial Catalog=
        <Dataset>;User ID=;Password=<access token>". The string is verified
        with a probe query, kept in memory only and never written or logged;
        Password= is redacted in every response. Pass "" to unpin."""
        return engine_connect(state, connection_string)
