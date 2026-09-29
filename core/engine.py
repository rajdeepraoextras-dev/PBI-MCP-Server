"""Live connection to the Analysis Services engine behind Power BI Desktop.

Everything else in pbi-mcp works on the project *files*. This module talks to
the *running* model: the local Analysis Services instance Power BI Desktop
starts for every open .pbip/.pbix, or a remote XMLA endpoint. It lets the
tools evaluate DAX against real data, ask the engine to compile a measure,
count rows and inspect VertiPaq column statistics -- without ever writing to
the project.

No pip dependencies. ADOMD.NET is driven through a spawned Windows PowerShell
(``powershell.exe``) process that ``Add-Type``s the AdomdClient DLL shipped
with Desktop, runs the queries and prints one JSON document. The query text
and the connection string travel in a temp *file*, never on the command line
(a remote connection string may carry an access token).

Discovery: Desktop writes each instance's port to
``AnalysisServicesWorkspaces/AnalysisServicesWorkspace_<guid>/Data/msmdsrv.port.txt``
(UTF-16LE) under its workspace root -- see ``workspace_roots()`` for the Store
and MSI locations. Only that port file is ever read from those folders.

On non-Windows platforms every entry point raises ``EngineUnavailable``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
DEFAULT_TIMEOUT = 120.0
PROBE_MEASURE = "__pbi_mcp_probe"

MSI_ADOMD_DLL = (r"C:\Program Files\Microsoft Power BI Desktop\bin"
                 r"\Microsoft.AnalysisServices.AdomdClient.dll")
STORE_PACKAGE = "Microsoft.MicrosoftPowerBIDesktop"
STORE_PACKAGE_FAMILY = "Microsoft.MicrosoftPowerBIDesktop_8wekyb3d8bbwe"
STORE_DLL_NAMES = ("Microsoft.PowerBI.AdomdClient.dll",
                   "Microsoft.AnalysisServices.AdomdClient.dll")
PORT_FILE = "msmdsrv.port.txt"

CATALOGS_DMV = "SELECT * FROM $SYSTEM.DBSCHEMA_CATALOGS"
TABLES_DMV = "SELECT * FROM $SYSTEM.TMSCHEMA_TABLES"
COLUMN_STORAGES_DMV = "SELECT * FROM $SYSTEM.TMSCHEMA_COLUMN_STORAGES"
STORAGE_TABLES_DMV = "SELECT * FROM $SYSTEM.DISCOVER_STORAGE_TABLES"
STORAGE_COLUMNS_DMV = "SELECT * FROM $SYSTEM.DISCOVER_STORAGE_TABLE_COLUMNS"
STORAGE_SEGMENTS_DMV = "SELECT * FROM $SYSTEM.DISCOVER_STORAGE_TABLE_COLUMN_SEGMENTS"

#: tables Desktop adds for auto date/time; never part of the TMDL project
INTERNAL_TABLE_PREFIXES = ("LocalDateTable_", "DateTableTemplate_")

HOW_TO_START = ("Open the project in Power BI Desktop (its local Analysis "
                "Services instance is what the engine tools talk to), or "
                "pin a remote XMLA endpoint with pbi_engine_connect.")
HOW_TO_DLL = ("Install Power BI Desktop, or set the PBI_ADOMD_DLL environment "
              "variable to the full path of "
              "Microsoft.AnalysisServices.AdomdClient.dll.")


class EngineError(RuntimeError):
    """Base class for live-engine failures."""


class EngineUnavailable(EngineError):
    """No engine could be reached (no Desktop instance, no DLL, timeout, ...).

    The message always says what to do about it.
    """


class QueryError(EngineError):
    """The engine accepted the connection but rejected the query (DAX/DMV error)."""


# --- secrets ------------------------------------------------------------------------

#: Password=/Pwd= followed by a quoted or bare value; a value that is already
#: '***' is left alone so redacting twice changes nothing.
_SECRET_RE = re.compile(
    r"(?i)(\b(?:password|pwd)\s*=\s*)(?!\*\*\*(?:$|[;\s)\],.'\"]))"
    r"(\"(?:[^\"]|\"\")*\"|'(?:[^']|'')*'|[^;]*)")


def redact(connection_string: str) -> str:
    """Mask every Password= / Pwd= value so a connection string can be shown."""
    return _SECRET_RE.sub(
        lambda m: m.group(1) + ("***" if m.group(2) else ""), str(connection_string))


def secret_values(connection_string: str) -> list[str]:
    """The Password=/Pwd= values in a connection string (quotes removed)."""
    out: list[str] = []
    for m in _SECRET_RE.finditer(str(connection_string)):
        val = m.group(2)
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1].replace(val[0] * 2, val[0])
        if val:
            out.append(val)
    return out


def scrub(text: str, *connection_strings: str) -> str:
    """Remove secret values (and any Password= text) from a message."""
    text = str(text)
    for cs in connection_strings:
        for secret in secret_values(cs):
            text = text.replace(secret, "***")
    return redact(text)


# --- connections ------------------------------------------------------------------------

@dataclass
class Connection:
    """A resolved engine endpoint: the connection string plus how we got it."""
    connection_string: str = field(repr=False)
    port: int | None = None
    database: str | None = None
    source: str = "local"          # local | explicit | pinned
    match: dict | None = field(default=None, repr=False)

    def __repr__(self) -> str:      # never show the raw string (may hold a token)
        return (f"Connection({redact(self.connection_string)!r}, port={self.port}, "
                f"database={self.database!r}, source={self.source!r})")

    def describe(self) -> dict:
        """JSON-safe, secret-free description for tool responses."""
        out = {
            "connection_string": redact(self.connection_string),
            "port": self.port,
            "database": self.database,
            "source": self.source,
        }
        if self.match:
            out["match"] = {k: v for k, v in self.match.items()
                            if k in ("overlap", "project_tables", "live_tables",
                                     "missing", "extra", "exact")}
        return out


def _cs_value(value: str) -> str:
    """Quote a connection-string value when it holds special characters."""
    if re.search(r"[;\"'=\s]", value):
        return '"' + value.replace('"', '""') + '"'
    return value


def local_connection(port: int, database: str | None = None,
                     source: str = "local") -> Connection:
    """Connection to the Desktop instance listening on ``localhost:<port>``."""
    cs = f"Data Source=localhost:{int(port)}"
    if database:
        cs += f";Catalog={_cs_value(database)}"
    return Connection(cs, port=int(port), database=database, source=source)


def _connection_string(conn: Connection | str) -> str:
    if isinstance(conn, Connection):
        return conn.connection_string
    if isinstance(conn, str) and conn.strip():
        return conn
    raise ValueError("conn must be a Connection or a non-empty connection string")


def _describe(conn: Connection | str) -> str:
    return redact(conn.connection_string if isinstance(conn, Connection) else str(conn))


# --- platform / powershell ----------------------------------------------------------

def _require_windows() -> None:
    if not IS_WINDOWS:
        raise EngineUnavailable(
            "The live engine needs Windows: it drives ADOMD.NET through "
            "powershell.exe and Power BI Desktop's local Analysis Services "
            f"instance (this is {sys.platform}). " + HOW_TO_START)


def _powershell_exe() -> str:
    root = os.environ.get("SystemRoot", r"C:\Windows")
    exe = Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return str(exe) if exe.exists() else "powershell.exe"


def _run_powershell(args: list[str], timeout: float) -> subprocess.CompletedProcess:
    """Spawn ``powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass``.

    The single seam the tests monkeypatch. Raises subprocess.TimeoutExpired /
    OSError like subprocess.run does; callers translate those.
    """
    cmd = [_powershell_exe(), "-NoProfile", "-NonInteractive",
           "-ExecutionPolicy", "Bypass", *args]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(cmd, capture_output=True, timeout=timeout,
                          creationflags=flags)


def _decode(data: bytes | str | None) -> str:
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8-sig", errors="replace")
    return str(data)


# --- ADOMD DLL ------------------------------------------------------------------------

_DLL_CACHE: str | None = None


def clear_caches() -> None:
    """Forget the cached DLL location (tests, or after installing Desktop)."""
    global _DLL_CACHE
    _DLL_CACHE = None


def _store_install_location(timeout: float = 60.0) -> str | None:
    """InstallLocation of the newest Store build via Get-AppxPackage, or None."""
    script = (f"(Get-AppxPackage -Name {STORE_PACKAGE} | "
              "Sort-Object { [version]$_.Version } -Descending | "
              "Select-Object -First 1).InstallLocation")
    try:
        proc = _run_powershell(["-Command", script], timeout)
    except (subprocess.TimeoutExpired, OSError):
        return None
    loc = _decode(proc.stdout).strip()
    return loc or None


def find_adomd_dll(refresh: bool = False) -> str:
    """Locate the ADOMD client DLL: env PBI_ADOMD_DLL, MSI build, Store build.

    The result is cached for the process. Raises EngineUnavailable with the
    locations tried and how to fix it.
    """
    global _DLL_CACHE
    _require_windows()
    if _DLL_CACHE and not refresh and Path(_DLL_CACHE).is_file():
        return _DLL_CACHE

    tried: list[str] = []
    env = os.environ.get("PBI_ADOMD_DLL")
    if env:
        if Path(env).is_file():
            _DLL_CACHE = str(Path(env))
            return _DLL_CACHE
        tried.append(f"PBI_ADOMD_DLL={env} (file not found)")

    if Path(MSI_ADOMD_DLL).is_file():
        _DLL_CACHE = MSI_ADOMD_DLL
        return _DLL_CACHE
    tried.append(MSI_ADOMD_DLL)

    loc = _store_install_location()
    if loc:
        for name in STORE_DLL_NAMES:
            candidate = Path(loc) / "bin" / name
            if candidate.is_file():
                _DLL_CACHE = str(candidate)
                return _DLL_CACHE
            tried.append(str(candidate))
    else:
        tried.append(f"Store package {STORE_PACKAGE} (Get-AppxPackage found nothing)")

    raise EngineUnavailable(
        "ADOMD client DLL not found; tried: " + "; ".join(tried) + ". " + HOW_TO_DLL)


# --- the PowerShell bridge ------------------------------------------------------------

# Runs inside Windows PowerShell 5.1 (.NET Framework). Reads one request JSON
#   {dll, timeout_seconds, queries: [{connection_string, query, max_rows,
#                                     then?: {query, max_rows, catalog_column}}]}
# and prints {"results": [...]}. A result is {columns, rows, row_count,
# truncated, elapsed_ms} or {error, stage}; a query with `then` also carries
# `children`: [{catalog, result}] -- `then.query` run once per database found
# in `catalog_column` of the first result (one process discovers an instance's
# databases AND their tables). Connections are opened once per connection
# string and reused across the batch.
_PS_SCRIPT = r"""
param([Parameter(Mandatory = $true)][string]$Request)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$WarningPreference = 'SilentlyContinue'
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch { }
$script:conns = @{}

function Out-Doc($obj) {
    Add-Type -AssemblyName System.Web.Extensions
    $ser = New-Object System.Web.Script.Serialization.JavaScriptSerializer
    $ser.MaxJsonLength = [int]::MaxValue
    $ser.RecursionLimit = 200
    [Console]::Out.Write($ser.Serialize($obj))
    [Console]::Out.Flush()
}

function Get-Messages($ex) {
    $parts = [System.Collections.ArrayList]::new()
    $depth = 0
    while ($null -ne $ex -and $depth -lt 8) {
        $wrapper = ($ex -is [System.Management.Automation.RuntimeException] -and $null -ne $ex.InnerException) -or
                   ($ex -is [System.Reflection.TargetInvocationException])
        if (-not $wrapper -and $ex.Message) { [void]$parts.Add(([string]$ex.Message).Trim()) }
        $ex = $ex.InnerException
        $depth++
    }
    if ($parts.Count -eq 0) { return 'unknown error' }
    return (@($parts | Select-Object -Unique) -join ' | ')
}

function Convert-Cell($v) {
    if ($null -eq $v -or $v -is [System.DBNull]) { return $null }
    if ($v -is [string]) { return $v }
    if ($v -is [bool]) { return $v }
    if ($v -is [datetime]) {
        return $v.ToString('yyyy-MM-ddTHH:mm:ss.FFFFFFF', [System.Globalization.CultureInfo]::InvariantCulture)
    }
    if ($v -is [System.DateTimeOffset]) { return $v.ToString('o', [System.Globalization.CultureInfo]::InvariantCulture) }
    if ($v -is [double] -or $v -is [single]) {
        $d = [double]$v
        if ([double]::IsNaN($d)) { return 'NaN' }
        if ([double]::IsPositiveInfinity($d)) { return 'Infinity' }
        if ([double]::IsNegativeInfinity($d)) { return '-Infinity' }
        return $d
    }
    if ($v -is [decimal]) { return $v }
    if ($v -is [byte] -or $v -is [sbyte] -or $v -is [int16] -or $v -is [uint16] -or
        $v -is [int32] -or $v -is [uint32] -or $v -is [int64] -or $v -is [uint64]) { return $v }
    if ($v -is [guid]) { return $v.ToString() }
    if ($v -is [byte[]]) { return [Convert]::ToBase64String($v) }
    if ($v -is [System.TimeSpan]) { return $v.ToString() }
    return [string]$v
}

function Get-Conn([string]$cs) {
    if ($script:conns.ContainsKey($cs)) { return $script:conns[$cs] }
    $c = New-Object -TypeName Microsoft.AnalysisServices.AdomdClient.AdomdConnection -ArgumentList @($cs)
    try { $c.Open() } catch { try { $c.Dispose() } catch { }; throw }
    $script:conns[$cs] = $c
    return $c
}

function Invoke-Query([string]$cs, [string]$query, [int]$maxRows, [int]$timeoutSeconds) {
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    try { $conn = Get-Conn $cs } catch {
        return @{ error = (Get-Messages $_.Exception); stage = 'connect'; elapsed_ms = [int]$sw.ElapsedMilliseconds }
    }
    $sw.Restart()
    $reader = $null
    try {
        $cmd = $conn.CreateCommand()
        $cmd.CommandText = $query
        if ($timeoutSeconds -gt 0) { $cmd.CommandTimeout = $timeoutSeconds }
        $reader = $cmd.ExecuteReader()
        $n = $reader.FieldCount
        # raw .NET collections only: New-Object wraps its result in a PSObject,
        # which JavaScriptSerializer would walk as an object graph
        $cols = [System.Collections.ArrayList]::new()
        for ($i = 0; $i -lt $n; $i++) { [void]$cols.Add([string]$reader.GetName($i)) }
        $rows = [System.Collections.ArrayList]::new()
        $count = 0
        $truncated = $false
        while ($reader.Read()) {
            if ($maxRows -ge 0 -and $count -ge $maxRows) { $truncated = $true; break }
            $row = [object[]]::new($n)
            for ($i = 0; $i -lt $n; $i++) { $row[$i] = Convert-Cell $reader.GetValue($i) }
            [void]$rows.Add($row)
            $count++
        }
        return @{ columns = $cols.ToArray(); rows = $rows.ToArray(); row_count = $count; truncated = $truncated; elapsed_ms = [int]$sw.ElapsedMilliseconds }
    } catch {
        $msg = Get-Messages $_.Exception
        if ($conn.State -ne [System.Data.ConnectionState]::Open) {
            try { $conn.Dispose() } catch { }
            $script:conns.Remove($cs)
        }
        return @{ error = $msg; stage = 'execute'; elapsed_ms = [int]$sw.ElapsedMilliseconds }
    } finally {
        if ($reader) { try { $reader.Close() } catch { } }
    }
}

function Quote-Value([string]$v) {
    if ($v -match '[;"''=\s]') { return '"' + $v.Replace('"', '""') + '"' }
    return $v
}

try {
    $req = Get-Content -LiteralPath $Request -Raw -Encoding UTF8 | ConvertFrom-Json
} catch {
    Out-Doc @{ error = "cannot read request file: $(Get-Messages $_.Exception)"; stage = 'request' }
    exit 3
}
try {
    Add-Type -Path ([string]$req.dll)
} catch {
    Out-Doc @{ error = "cannot load ADOMD client from $($req.dll): $(Get-Messages $_.Exception)"; stage = 'load' }
    exit 2
}
$timeout = [int]$req.timeout_seconds
$results = [System.Collections.ArrayList]::new()
foreach ($q in @($req.queries)) {
    $res = Invoke-Query ([string]$q.connection_string) ([string]$q.query) ([int]$q.max_rows) $timeout
    if ($q.PSObject.Properties['then'] -and $q.then -and -not $res.ContainsKey('error')) {
        $idx = -1
        for ($i = 0; $i -lt $res.columns.Count; $i++) {
            if ([string]$res.columns[$i] -eq [string]$q.then.catalog_column) { $idx = $i; break }
        }
        $children = [System.Collections.ArrayList]::new()
        if ($idx -ge 0) {
            foreach ($row in $res.rows) {
                $catalog = [string]$row[$idx]
                if (-not $catalog) { continue }
                $ccs = ([string]$q.connection_string) + ';Catalog=' + (Quote-Value $catalog)
                $child = Invoke-Query $ccs ([string]$q.then.query) ([int]$q.then.max_rows) $timeout
                [void]$children.Add(@{ catalog = $catalog; result = $child })
            }
        }
        $res['children'] = $children.ToArray()
    }
    [void]$results.Add($res)
}
foreach ($c in @($script:conns.Values)) { try { $c.Close(); $c.Dispose() } catch { } }
Out-Doc @{ results = $results.ToArray() }
exit 0
"""


def _parse_output(proc: subprocess.CompletedProcess, secrets: list[str]) -> dict:
    """The JSON document the bridge printed, or EngineUnavailable with stderr."""
    out = _decode(proc.stdout).strip()
    if out:
        doc = None
        try:
            doc = json.loads(out)
        except json.JSONDecodeError:
            start, end = out.find("{"), out.rfind("}")
            if start != -1 and end > start:
                try:
                    doc = json.loads(out[start:end + 1])
                except json.JSONDecodeError:
                    doc = None
        if isinstance(doc, dict):
            return doc
    err = _decode(proc.stderr).strip()
    detail = (err[-1200:] if err else out[-600:]) or "(no output)"
    raise EngineUnavailable(
        f"The PowerShell ADOMD bridge returned no JSON (exit code "
        f"{proc.returncode}): {_scrub_all(detail, secrets)}. " + HOW_TO_DLL)


def _scrub_all(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        text = text.replace(secret, "***")
    return redact(text)


_MS_DATE_RE = re.compile(r"^/Date\((-?\d+)\)/$")


def _normalize_value(v):
    """Post-process one cell: legacy /Date(ms)/ strings -> ISO 8601."""
    if isinstance(v, str):
        m = _MS_DATE_RE.match(v)
        if m:
            dt = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(
                milliseconds=int(m.group(1)))
            return dt.replace(tzinfo=None).isoformat()
    return v


def _normalize_result(res: dict, secrets: list[str]) -> dict:
    if not isinstance(res, dict):
        raise EngineUnavailable(
            f"Malformed result from the ADOMD bridge: {res!r}. " + HOW_TO_DLL)
    if "error" in res:
        return {"error": _scrub_all(str(res.get("error")), secrets),
                "stage": res.get("stage", "execute"),
                "elapsed_ms": int(res.get("elapsed_ms") or 0)}
    columns = [str(c) for c in (res.get("columns") or [])]
    rows = [[_normalize_value(c) for c in (row or [])]
            for row in (res.get("rows") or [])]
    out = {
        "columns": columns,
        "rows": rows,
        "row_count": int(res.get("row_count", len(rows))),
        "truncated": bool(res.get("truncated", False)),
        "elapsed_ms": int(res.get("elapsed_ms") or 0),
    }
    if "children" in res:
        out["children"] = [
            {"catalog": str(kid.get("catalog")),
             "result": _normalize_result(kid.get("result"), secrets)}
            for kid in (res.get("children") or [])]
    return out


def _run_queries(queries: list[dict], timeout: float = DEFAULT_TIMEOUT) -> list[dict]:
    """Run several queries in one PowerShell process; one result dict each.

    Each query: {"connection_string", "query", "max_rows"} and optionally
    "then": {"query", "max_rows", "catalog_column"} (see the bridge notes).
    Per-query errors come back as {"error", "stage"}; process-level failures
    (no DLL, timeout, unreadable output) raise EngineUnavailable.
    """
    _require_windows()
    if not queries:
        return []
    secrets = [s for q in queries for s in secret_values(q["connection_string"])]
    dll = find_adomd_dll()
    tmpdir = Path(tempfile.mkdtemp(prefix="pbi-mcp-engine-"))
    try:
        script = tmpdir / "adomd_query.ps1"
        # utf-8-sig: Windows PowerShell 5.1 needs a BOM to read the script as UTF-8
        script.write_text(_PS_SCRIPT, encoding="utf-8-sig")
        request = tmpdir / "request.json"
        payload = []
        for q in queries:
            item = {
                "connection_string": q["connection_string"],
                "query": q["query"],
                "max_rows": int(q.get("max_rows", 1000)),
            }
            if q.get("then"):
                item["then"] = {
                    "query": q["then"]["query"],
                    "max_rows": int(q["then"].get("max_rows", 1000)),
                    "catalog_column": q["then"]["catalog_column"],
                }
            payload.append(item)
        request.write_text(json.dumps({"dll": dll, "timeout_seconds": int(timeout),
                                       "queries": payload}), encoding="utf-8")
        try:
            proc = _run_powershell(["-File", str(script), "-Request", str(request)],
                                   timeout + 15)
        except subprocess.TimeoutExpired:
            raise EngineUnavailable(
                f"The engine did not answer within {int(timeout)} s. The model "
                "may be busy (refreshing?) or the query too heavy; retry with a "
                "smaller query or a longer timeout. " + HOW_TO_START) from None
        except OSError as exc:
            raise EngineUnavailable(
                f"Could not start powershell.exe: {exc}. " + HOW_TO_DLL) from None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    doc = _parse_output(proc, secrets)
    if "results" not in doc:
        stage = doc.get("stage", "?")
        msg = _scrub_all(str(doc.get("error", "unknown error")), secrets)
        hint = HOW_TO_DLL if stage == "load" else HOW_TO_START
        raise EngineUnavailable(f"ADOMD bridge failed at stage '{stage}': {msg}. {hint}")
    results = doc["results"]
    if not isinstance(results, list) or len(results) != len(queries):
        raise EngineUnavailable(
            f"ADOMD bridge returned {len(results) if isinstance(results, list) else '?'} "
            f"results for {len(queries)} queries. " + HOW_TO_DLL)
    return [_normalize_result(r, secrets) for r in results]


def _raise_for(res: dict, conn: Connection | str) -> dict:
    if "error" in res:
        if res.get("stage") == "connect":
            raise EngineUnavailable(
                f"Cannot connect to {_describe(conn)}: {res['error']}. " + HOW_TO_START)
        raise QueryError(res["error"])
    return res


def _execute_batch(conn: Connection | str, statements: list[tuple[str, int]],
                   timeout: float = DEFAULT_TIMEOUT,
                   optional: tuple[int, ...] = ()) -> list[dict | None]:
    """Run several statements on one connection in a single PowerShell process.

    ``statements`` is [(query, max_rows), ...]. A statement whose index is in
    ``optional`` yields None when the engine rejects it; any other rejection
    raises QueryError and an unreachable endpoint raises EngineUnavailable.
    """
    cs = _connection_string(conn)
    results = _run_queries([{"connection_string": cs, "query": q, "max_rows": n}
                            for q, n in statements], timeout)
    out: list[dict | None] = []
    for i, res in enumerate(results):
        if "error" in res and i in optional and res.get("stage") != "connect":
            out.append(None)
        else:
            out.append(_raise_for(res, conn))
    return out


def execute_dax(conn: Connection | str, query: str, max_rows: int = 1000,
                timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Run a DAX (EVALUATE ...) or DMV (SELECT ... FROM $SYSTEM...) query.

    Returns {"columns", "rows", "row_count", "truncated", "elapsed_ms"}; rows
    hold JSON scalars (DBNull -> None, DateTime -> ISO 8601 string, numbers
    as numbers). Raises QueryError for engine-rejected queries and
    EngineUnavailable when the endpoint cannot be reached.
    """
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a non-empty DAX or DMV statement, "
                         "e.g. EVALUATE ROW(\"v\", [Total Sales])")
    if int(max_rows) < 1:
        raise ValueError("max_rows must be >= 1")
    [res] = _execute_batch(conn, [(query, int(max_rows))], timeout)
    return res


# --- instance discovery -----------------------------------------------------------------

def workspace_roots() -> list[Path]:
    """Folders under which Desktop creates ``AnalysisServicesWorkspace_<guid>``.

    * ``%USERPROFILE%\\Microsoft\\Power BI Desktop Store App\\...`` -- where the
      Store build (verified on 2.157.1354.0) actually puts its workspaces;
    * the package's virtualised ``LocalCache`` copy of the classic path, which
      older Store builds used;
    * ``%LOCALAPPDATA%\\Microsoft\\Power BI Desktop\\...`` -- the MSI build.
    """
    roots: list[Path] = []
    profile = os.environ.get("USERPROFILE")
    if profile:
        roots.append(Path(profile) / "Microsoft" / "Power BI Desktop Store App"
                     / "AnalysisServicesWorkspaces")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        base = Path(local)
        roots.append(base / "Packages" / STORE_PACKAGE_FAMILY / "LocalCache"
                     / "Local" / "Microsoft" / "Power BI Desktop"
                     / "AnalysisServicesWorkspaces")
        roots.append(base / "Microsoft" / "Power BI Desktop"
                     / "AnalysisServicesWorkspaces")
    return roots


def read_port_file(path: Path) -> int | None:
    """Port number from msmdsrv.port.txt (UTF-16LE, BOM optional)."""
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return None
    for enc in ("utf-16", "utf-16-le", "utf-8-sig"):
        try:
            text = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        text = text.strip().lstrip("\ufeff").strip("\x00 \r\n\t")
        if text.isdigit():
            port = int(text)
            if 0 < port < 65536:
                return port
    return None


def _port_file_candidates() -> list[dict]:
    """Every workspace folder that has a parseable port file."""
    out: list[dict] = []
    for root in workspace_roots():
        if not root.is_dir():
            continue
        try:
            workspaces = sorted(root.glob("AnalysisServicesWorkspace_*"))
        except OSError:
            continue
        for ws in workspaces:
            port_file = ws / "Data" / PORT_FILE
            if not port_file.is_file():
                continue
            port = read_port_file(port_file)
            if port is None:
                continue
            try:
                mtime = port_file.stat().st_mtime
            except OSError:
                mtime = 0.0
            out.append({
                "workspace_dir": str(ws),
                "port": port,
                "port_file": str(port_file),
                "port_file_mtime": mtime,
                "reachable": False,
                "databases": [],
                "error": None,
            })
    return out


def _port_open(port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _column(result: dict, name: str) -> list:
    cols = [c.lower() for c in result.get("columns", [])]
    try:
        idx = cols.index(name.lower())
    except ValueError:
        return []
    return [row[idx] for row in result.get("rows", []) if idx < len(row)]


def is_internal_table(name: str) -> bool:
    """Auto date/time helper tables that never appear in the TMDL project."""
    return str(name).startswith(INTERNAL_TABLE_PREFIXES)


def discover_instances(include_errors: bool = False,
                       timeout: float = DEFAULT_TIMEOUT) -> list[dict]:
    """Every running Power BI Desktop Analysis Services instance.

    Each entry: {"workspace_dir", "port", "port_file", "port_file_mtime",
    "reachable", "databases": [{"name", "tables": [...]}], "error"}. The port
    must accept a TCP connection; databases come from DBSCHEMA_CATALOGS and
    table names from TMSCHEMA_TABLES (queried with the Catalog set), all in a
    single PowerShell process. Instances whose port is dead or whose DMVs
    fail are dropped unless ``include_errors`` is set (then they carry an
    ``error`` string so status tools can explain what is wrong). Returns []
    without touching PowerShell when no workspace has a port file.
    """
    _require_windows()
    candidates = _port_file_candidates()
    for c in candidates:
        c["reachable"] = _port_open(c["port"])
        if not c["reachable"]:
            c["error"] = ("port is not accepting connections (stale workspace "
                          "folder from a closed or crashed Desktop?)")
    reachable = [c for c in candidates if c["reachable"]]

    if reachable:
        try:
            results = _run_queries([{
                "connection_string": local_connection(c["port"]).connection_string,
                "query": CATALOGS_DMV,
                "max_rows": 1000,
                "then": {"query": TABLES_DMV, "max_rows": 100000,
                         "catalog_column": "CATALOG_NAME"},
            } for c in reachable], timeout)
        except EngineUnavailable as exc:
            if not include_errors:
                raise
            for c in reachable:
                c["error"] = str(exc)
            results = []
        for c, res in zip(reachable, results):
            if "error" in res:
                c["error"] = res["error"]
                continue
            for kid in res.get("children", []):
                db: dict = {"name": kid["catalog"], "tables": []}
                child = kid["result"]
                if "error" in child:
                    db["error"] = child["error"]
                else:
                    db["tables"] = [str(n) for n in _column(child, "Name") if n is not None]
                c["databases"].append(db)

    if include_errors:
        return candidates
    return [c for c in candidates if c["reachable"] and not c["error"]]


def match_instance(project, instances: list[dict]) -> dict | None:
    """Pick the (instance, database) whose tables best overlap the project's.

    ``project`` is a PbipProject (anything with ``list_tables()`` returning
    objects with ``.name``). The database sharing the most table names wins;
    of equally overlapping ones the exact match (fewest extra tables) beats a
    superset, and remaining ties go to the most recently written port file.
    Desktop's auto date/time helper tables are ignored. Returns None when no
    database shares a single table name with the project.
    """
    names = {t.name for t in project.list_tables()}
    if not names:
        return None
    best: dict | None = None
    best_key: tuple = ()
    for inst in instances:
        for db in inst.get("databases", []) or []:
            live = {t for t in db.get("tables", []) if not is_internal_table(t)}
            overlap = len(names & live)
            if overlap == 0:
                continue
            key = (overlap, -len(live - names), inst.get("port_file_mtime", 0.0))
            if best is None or key > best_key:
                best_key = key
                best = {
                    "port": inst["port"],
                    "database": db["name"],
                    "workspace_dir": inst.get("workspace_dir"),
                    "overlap": overlap,
                    "project_tables": len(names),
                    "live_tables": len(live),
                    "missing": sorted(names - live),
                    "extra": sorted(live - names),
                    "exact": names == live,
                }
    return best


# --- DAX helpers ------------------------------------------------------------------------

def dax_table_ref(name: str) -> str:
    """Quote a table name for DAX: 'Sales', 'O''Brien'."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("table name must be a non-empty string")
    return "'" + name.replace("'", "''") + "'"


def _probe_parts(dax: str, table: str | None) -> tuple[str, int]:
    """(query, length of the text placed before the expression on line 1)."""
    if not isinstance(dax, str) or not dax.strip():
        raise ValueError("dax must be a non-empty measure expression")
    body = dax.strip()
    if re.match(r"(?is)^(evaluate|define)\b", body):
        raise ValueError(
            "validate_dax takes a measure expression such as SUM(Sales[Amount]), "
            "not a query; use pbi_evaluate_dax to run EVALUATE statements.")
    if table is None:
        head = 'EVALUATE ROW("v", '
        return f"{head}{body})", len(head)
    head = f"DEFINE MEASURE {dax_table_ref(table)}[{PROBE_MEASURE}] = "
    return f'{head}{body}\nEVALUATE ROW("v", [{PROBE_MEASURE}])', len(head)


def probe_query(dax: str, table: str | None = None) -> str:
    """The DAX query that compiles ``dax`` as a measure without persisting it.

    Without a table: EVALUATE ROW("v", <dax>). With a table the expression is
    defined as a query-scoped measure on that table so row context, implicit
    CALCULATE and table-relative references resolve exactly as they would for
    a real measure there.
    """
    return _probe_parts(dax, table)[0]


_POS_RE = re.compile(r"\bQuery \((\d+), (\d+)\)")
#: the engine echoes the whole composed query in parentheses after some errors
_ECHO_RE = re.compile(r"\s*\((?:DEFINE MEASURE|EVALUATE)\b[\s\S]*\)\.?\s*$")


def _relocate_positions(message: str, prefix_len: int, expr_lines: int = 1 << 30) -> str:
    """Rewrite 'Query (line, col)' so it points into the caller's expression.

    Only positions inside the expression (line <= expr_lines) are rewritten;
    anything else refers to the wrapper query and is left as the engine said.
    """
    def fix(m: re.Match) -> str:
        line, col = int(m.group(1)), int(m.group(2))
        if line > expr_lines:
            return m.group(0)
        if line == 1:
            return f"Expression ({line}, {col - prefix_len})" if col > prefix_len else m.group(0)
        return f"Expression ({line}, {col})"
    return _POS_RE.sub(fix, message)


def _clean_probe_error(message: str, prefix_len: int, expr_lines: int) -> str:
    """Make an engine error about the probe query read as an error about the DAX."""
    echo = _ECHO_RE.search(message)
    if echo and (PROBE_MEASURE in echo.group(0) or 'ROW("v"' in echo.group(0)):
        message = message[:echo.start()].rstrip()
    # the engine names the throw-away measure ("measure 'Sales'[__pbi_mcp_probe]")
    message = re.sub(r"(?:'(?:[^']|'')*')?\[" + PROBE_MEASURE + r"\]", "the measure", message)
    message = message.replace("measure the measure", "the measure")
    m = _POS_RE.search(message)
    if m and int(m.group(1)) > expr_lines:
        # the error is at the wrapper (its EVALUATE line): the expression ended too soon
        detail = _POS_RE.sub("", message).strip()
        return ("The expression is incomplete or unbalanced (a missing ')' or argument?). "
                f"Engine message: {detail}")
    return _relocate_positions(message, prefix_len, expr_lines)


def validate_dax(conn: Connection | str, dax: str, table: str | None = None,
                 timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Ask the engine to compile (and evaluate once) a measure expression.

    Returns {"ok": bool, "error": str|None, "query": str, "value": ...,
    "elapsed_ms": int}. Nothing is written to the model: the measure exists
    only for the query. Error positions are relative to ``dax``.
    """
    query, prefix_len = _probe_parts(dax, table)
    expr_lines = dax.strip().count("\n") + 1
    try:
        res = execute_dax(conn, query, max_rows=1, timeout=timeout)
    except QueryError as exc:
        return {"ok": False, "error": _clean_probe_error(str(exc), prefix_len, expr_lines),
                "query": query, "elapsed_ms": 0}
    value = res["rows"][0][0] if res["rows"] and res["rows"][0] else None
    return {"ok": True, "error": None, "query": query,
            "value": value, "elapsed_ms": res["elapsed_ms"]}


def _split_id(text: str) -> tuple[str, int | None]:
    """'Sales (12)' -> ('Sales', 12); anything else -> (text, None)."""
    m = re.match(r"^(.*) \((\d+)\)$", str(text))
    if not m:
        return str(text), None
    return m.group(1), int(m.group(2))


def _is_storage_helper(table_id: str) -> bool:
    """H$ (attribute hierarchy), R$ (relationship), U$ (user hierarchy) structures."""
    return str(table_id).startswith(("H$", "R$", "U$"))


def table_row_counts(conn: Connection | str, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Row count per table.

    Uses DISCOVER_STORAGE_TABLES (rows of the loaded segments), lists tables
    without storage (DirectQuery, never processed) with ``rows: None``, and
    falls back to one COUNTROWS query when the storage DMV is unavailable.
    Returns {"tables": [{"name", "rows", "internal"}], "source",
    "elapsed_ms"}; ``internal`` marks Desktop's auto date/time helper tables.
    """
    storage, meta = _execute_batch(
        conn, [(STORAGE_TABLES_DMV, 100000), (TABLES_DMV, 100000)], timeout,
        optional=(0, 1))
    names = [str(n) for n in _column(meta, "Name") if n] if meta else []
    elapsed = sum(r["elapsed_ms"] for r in (storage, meta) if r)
    rows: dict[str, int | None] = {}
    source = "DISCOVER_STORAGE_TABLES"
    if storage:
        for name, tid, n in zip(_column(storage, "DIMENSION_NAME"),
                                _column(storage, "TABLE_ID"),
                                _column(storage, "ROWS_COUNT")):
            if name is None or _is_storage_helper(tid):
                continue
            try:
                rows[str(name)] = int(n or 0)
            except (TypeError, ValueError):
                rows[str(name)] = 0
    if not rows and names:
        parts = ", ".join(
            f'ROW("Table", "{t.replace(chr(34), chr(34) * 2)}", '
            f'"Rows", COUNTROWS({dax_table_ref(t)}))' for t in names)
        query = f"EVALUATE UNION({parts})" if len(names) > 1 else f"EVALUATE {parts}"
        counted = execute_dax(conn, query, max_rows=100000, timeout=timeout)
        elapsed += counted["elapsed_ms"]
        for row in counted["rows"]:
            if len(row) >= 2:
                rows[str(row[0])] = int(row[1] or 0)
        source = "COUNTROWS"
    for name in names:
        rows.setdefault(name, None)
    tables = [{"name": n, "rows": rows[n], "internal": is_internal_table(n)}
              for n in sorted(rows, key=lambda s: (is_internal_table(s), s.lower()))]
    return {"tables": tables, "source": source, "elapsed_ms": elapsed}


_ENCODINGS = {1: "HASH", 2: "VALUE"}


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def column_stats(conn: Connection | str, table: str | None = None,
                 timeout: float = DEFAULT_TIMEOUT) -> dict:
    """VertiPaq statistics per column, largest first.

    Reads DISCOVER_STORAGE_TABLE_COLUMNS (data type, encoding, dictionary
    size) and DISCOVER_STORAGE_TABLE_COLUMN_SEGMENTS (data size of the column
    and of its attribute-hierarchy structure), plus the exact distinct count
    from TMSCHEMA_COLUMN_STORAGES when the engine offers it (``cardinality``
    is None otherwise). Internal RowNumber columns, relationship and
    user-hierarchy structures are skipped. Sizes are bytes; ``total_size`` is
    dictionary + data + hierarchy. Returns {"columns", "tables",
    "total_size", "elapsed_ms"}.
    """
    cols_res, segs_res, stor_res = _execute_batch(
        conn, [(STORAGE_COLUMNS_DMV, 500000), (STORAGE_SEGMENTS_DMV, 1000000),
               (COLUMN_STORAGES_DMV, 500000)], timeout, optional=(2,))

    def records(res: dict) -> list[dict]:
        keys = [c.upper() for c in res["columns"]]
        return [dict(zip(keys, row)) for row in res["rows"]]

    data_size: dict[tuple[str, str], int] = {}
    seg_rows: dict[tuple[str, str], int] = {}
    hier_size: dict[int, int] = {}
    for seg in records(segs_res):
        tid = str(seg.get("TABLE_ID", ""))
        cid = str(seg.get("COLUMN_ID", ""))
        used = _int(seg.get("USED_SIZE"))
        if tid.startswith("H$"):
            col_no = _split_id(tid)[1]           # H$<table (id)>$<column (id)>
            if col_no is not None:
                hier_size[col_no] = hier_size.get(col_no, 0) + used
            continue
        if _is_storage_helper(tid):
            continue
        key = (tid, cid)
        data_size[key] = data_size.get(key, 0) + used
        seg_rows[key] = seg_rows.get(key, 0) + _int(seg.get("RECORDS_COUNT"))

    cardinality: dict[int, int] = {}
    if stor_res:
        for st in records(stor_res):
            if st.get("COLUMNID") is not None:
                cardinality[_int(st["COLUMNID"])] = _int(st.get("STATISTICS_DISTINCTSTATES"))

    out: list[dict] = []
    known_tables: set[str] = set()
    for col in records(cols_res):
        tid = str(col.get("TABLE_ID", ""))
        if _is_storage_helper(tid):
            continue
        attr = str(col.get("ATTRIBUTE_NAME", ""))
        if attr.startswith("RowNumber"):
            continue
        ctype = str(col.get("COLUMN_TYPE", "") or "")
        if ctype and ctype.upper() != "BASIC_DATA":
            continue
        tname = str(col.get("DIMENSION_NAME") or _split_id(tid)[0])
        known_tables.add(tname)
        if table is not None and tname != table:
            continue
        cid = str(col.get("COLUMN_ID", ""))
        col_no = _split_id(cid)[1]
        enc_raw = col.get("COLUMN_ENCODING")
        encoding = _ENCODINGS.get(_int(enc_raw, -1), str(enc_raw) if enc_raw is not None else None)
        dict_size = _int(col.get("DICTIONARY_SIZE"))
        data = data_size.get((tid, cid), 0)
        hier = hier_size.get(col_no, 0) if col_no is not None else 0
        out.append({
            "table": tname,
            "column": attr,
            "data_type": col.get("DATATYPE"),
            "encoding": encoding,
            "cardinality": cardinality.get(col_no) if col_no is not None else None,
            "rows": seg_rows.get((tid, cid)),
            "dictionary_size": dict_size,
            "data_size": data,
            "hierarchy_size": hier,
            "total_size": dict_size + data + hier,
        })
    if table is not None and not out:
        raise ValueError(f"Table {table!r} is not in the live model; tables: "
                         f"{sorted(known_tables)}")
    out.sort(key=lambda c: (-c["total_size"], c["table"], c["column"]))
    per_table: dict[str, dict] = {}
    for c in out:
        t = per_table.setdefault(c["table"], {"table": c["table"], "columns": 0, "total_size": 0})
        t["columns"] += 1
        t["total_size"] += c["total_size"]
    return {
        "columns": out,
        "tables": sorted(per_table.values(), key=lambda t: (-t["total_size"], t["table"])),
        "total_size": sum(c["total_size"] for c in out),
        "elapsed_ms": sum(r["elapsed_ms"] for r in (cols_res, segs_res, stor_res) if r),
    }


def preview_table(conn: Connection | str, table: str, top: int = 20,
                  timeout: float = DEFAULT_TIMEOUT) -> dict:
    """First ``top`` rows of a table via EVALUATE TOPN(top, 'Table')."""
    if int(top) < 1:
        raise ValueError("top must be >= 1")
    query = f"EVALUATE TOPN({int(top)}, {dax_table_ref(table)})"
    res = execute_dax(conn, query, max_rows=int(top), timeout=timeout)
    res["table"] = table
    res["query"] = query
    return res
