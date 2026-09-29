"""Open a .pbip in Power BI Desktop and wait until its engine hosts the model.

    python scripts/desktop_launch.py tests/fixtures/engine/Engine.pbip
    python scripts/desktop_launch.py tests/fixtures/engine/Engine.pbip --refresh
    python scripts/desktop_launch.py --close            # close Desktop again

``launch()`` starts Desktop through the shell association of the .pbip file
(so the Store build resolves; the ``PBIDesktopStore.exe`` app-execution alias
and the MSI ``PBIDesktop.exe`` are fallbacks), then polls
``core.engine.discover_instances()`` until an instance hosts a database whose
tables are the project's tables. A freshly opened .pbip has no data: pass
``refresh=True`` (``--refresh``) to press the "Refresh now" banner in the
Desktop window and wait until tables have rows -- the engine fixture's data is
inline, so no credentials are needed.

``close()`` asks Desktop to close its main window, answers the "Do you want to
save your changes?" prompt with "Don't save" (a test fixture must never be
rewritten by Desktop), and only falls back to ``Stop-Process`` when Desktop
does not exit. A forced stop leaves a stale workspace folder behind, which
``discover_instances`` reports as unreachable.

Cold starts: on Power BI Desktop 2.158.1177.0 (Store) a cold start with the
.pbip as its argument never finishes loading ("Working on it", 0 % CPU, an
engine with an empty database), while the same file opened into an already
running Desktop loads normally. ``launch()`` therefore starts a bare "host"
instance first when no Desktop is running (``host=True/False`` overrides), and
maps each engine instance to its Desktop process via the ``msmdsrv`` command
line so that refresh and close act on the window that holds the model.

Nothing under Desktop's workspace folders is touched except reading the port
file (that is all ``discover_instances`` does).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from core import engine  # noqa: E402
from core.pbip import PbipProject  # noqa: E402

PROCESS_NAME = "PBIDesktop"
MSI_EXE = r"C:\Program Files\Microsoft Power BI Desktop\bin\PBIDesktop.exe"
STORE_APP_ID = "Microsoft.MicrosoftPowerBIDesktop_8wekyb3d8bbwe!Microsoft.MicrosoftPowerBIDesktop"
HOST_SETTLE_SECONDS = 15.0     # let a bare host instance finish starting before opening a file

# --- PowerShell helpers --------------------------------------------------------------

# Press the first visible "Refresh now" banner button (UI Automation), then print
# any load-error text Desktop shows (the Load pane names the failing query).
_PS_REFRESH = r"""
param([int]$ProcessId = 0, [switch]$DiagnoseOnly)
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
if ($ProcessId) { $p = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue }
else { $p = Get-Process PBIDesktop -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1 }
if (-not $p -or $p.MainWindowHandle -eq 0) { 'no-window'; exit 0 }
$AE = [System.Windows.Automation.AutomationElement]
$root = $AE::FromHandle($p.MainWindowHandle)
if (-not $DiagnoseOnly) {
    $cond = New-Object System.Windows.Automation.PropertyCondition($AE::NameProperty, 'Refresh now')
    $target = $null
    foreach ($e in $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $cond)) {
        if ($e.Current.IsEnabled -and -not $e.Current.IsOffscreen) { $target = $e; break }
    }
    if ($target) {
        $pat = $null
        if ($target.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$pat)) { $pat.Invoke(); 'clicked' }
        else { 'no-invoke' }
    } else { 'no-banner' }
}
$seen = @{}
foreach ($e in $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)) {
    $n = $e.Current.Name
    if ($n -and $n.Length -lt 200 -and $n -match 'blocked by|cyclic|error occurred|Load was cancel|failed to load' -and -not $seen.ContainsKey($n)) {
        $seen[$n] = 1; "note: $n"
    }
}
"""

# CloseMainWindow, then answer Desktop's save prompt with "Don't save". The prompt is a
# modal window that hosts an embedded HTML view (Internet Explorer_Server); UI Automation
# cannot see inside it, so its document is fetched over COM and the button is clicked.
_PS_CLOSE = r"""
param([int]$ProcessId, [int]$DialogSeconds = 12)
Add-Type -TypeDefinition @"
using System;
using System.Collections.Generic;
using System.Text;
using System.Runtime.InteropServices;
public static class PbiClose {
    public delegate bool EnumProc(IntPtr h, IntPtr l);
    [DllImport("user32.dll")] static extern bool EnumWindows(EnumProc cb, IntPtr l);
    [DllImport("user32.dll")] static extern bool EnumChildWindows(IntPtr p, EnumProc cb, IntPtr l);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int GetClassName(IntPtr h, StringBuilder s, int n);
    [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr h);
    [DllImport("user32.dll")] static extern IntPtr GetWindow(IntPtr h, uint cmd);
    [DllImport("user32.dll", CharSet = CharSet.Auto)] static extern uint RegisterWindowMessage(string m);
    [DllImport("user32.dll")] static extern IntPtr SendMessageTimeout(IntPtr h, uint msg, IntPtr w, IntPtr l, uint flags, uint timeout, out IntPtr result);
    [DllImport("oleacc.dll")] static extern int ObjectFromLresult(IntPtr lres, [MarshalAs(UnmanagedType.LPStruct)] Guid riid, IntPtr wParam, [MarshalAs(UnmanagedType.IUnknown)] out object obj);

    // Embedded-HTML windows that live in a visible, owned (= modal dialog) window of the process.
    public static List<long> DialogHtmlWindows(uint pid) {
        var found = new List<long>();
        EnumWindows((h, l) => {
            uint p; GetWindowThreadProcessId(h, out p);
            if (p != pid || !IsWindowVisible(h) || GetWindow(h, 4) == IntPtr.Zero) return true;
            EnumChildWindows(h, (c, l2) => {
                var cls = new StringBuilder(256); GetClassName(c, cls, 256);
                if (cls.ToString() == "Internet Explorer_Server") found.Add(c.ToInt64());
                return true;
            }, IntPtr.Zero);
            return true;
        }, IntPtr.Zero);
        return found;
    }

    public static object Document(long hwnd) {
        uint msg = RegisterWindowMessage("WM_HTML_GETOBJECT");
        IntPtr res;
        SendMessageTimeout(new IntPtr(hwnd), msg, IntPtr.Zero, IntPtr.Zero, 2, 2000, out res);
        object doc;
        if (ObjectFromLresult(res, new Guid("626FC520-A41E-11cf-A731-00A0C9082637"), IntPtr.Zero, out doc) != 0) return null;
        return doc;
    }
}
"@
$p = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
if (-not $p) { 'gone'; exit 0 }
[void]$p.CloseMainWindow()
$deadline = (Get-Date).AddSeconds($DialogSeconds)
while ((Get-Date) -lt $deadline) {
    if (-not (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) { 'exited'; exit 0 }
    foreach ($h in [PbiClose]::DialogHtmlWindows([uint32]$ProcessId)) {
        $doc = [PbiClose]::Document($h)
        if ($null -eq $doc) { continue }
        $all = $doc.getElementsByTagName('*')
        for ($i = 0; $i -lt $all.length; $i++) {
            $e = $all.item($i)
            $txt = ''
            try { $txt = ([string]$e.innerText).Trim() } catch { }
            if ($txt -match "^Don.t save$" -and ([string]$e.className -match 'action-button' -or $e.tagName -in 'BUTTON', 'INPUT', 'A')) {
                $e.click(); 'answered'; exit 0
            }
        }
    }
    Start-Sleep -Milliseconds 400
}
'no-prompt'
"""


def _run_script(text: str, *args: str, timeout: float = 90.0) -> str:
    """Run a PowerShell script (written to a temp .ps1) and return its stdout."""
    tmp = Path(tempfile.mkdtemp(prefix="pbi-mcp-launch-"))
    try:
        script = tmp / "script.ps1"
        script.write_text(text, encoding="utf-8-sig")
        proc = engine._run_powershell(["-File", str(script), *args], timeout)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    err = engine._decode(proc.stderr).strip()
    if proc.returncode != 0 and err:
        raise RuntimeError(f"PowerShell failed ({proc.returncode}): {err[-800:]}")
    return engine._decode(proc.stdout).strip()


def _ps(command: str, timeout: float = 60.0) -> str:
    proc = engine._run_powershell(["-Command", command], timeout)
    err = engine._decode(proc.stderr).strip()
    # powershell.exe exits 1 whenever the last statement set $? to false --
    # including a silenced "no such process" -- so only a stderr message counts.
    if proc.returncode != 0 and err:
        raise RuntimeError(f"PowerShell failed ({proc.returncode}): {err[-800:]}")
    return engine._decode(proc.stdout).strip()


def desktop_pids() -> dict[int, str]:
    """{pid: start time} of every running PBIDesktop process."""
    out = _ps(f"Get-Process {PROCESS_NAME} -ErrorAction SilentlyContinue | "
              "ForEach-Object { \"$($_.Id)|$($_.StartTime.ToString('o'))\" }")
    pids: dict[int, str] = {}
    for line in out.splitlines():
        pid, _, started = line.strip().partition("|")
        if pid.isdigit():
            pids[int(pid)] = started
    return pids


def _store_alias() -> Path | None:
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return None
    alias = Path(local) / "Microsoft" / "WindowsApps" / "PBIDesktopStore.exe"
    return alias if alias.exists() else None


def _start(pbip: Path | None, how: str) -> None:
    """Start Desktop: open ``pbip`` (or, with None, a bare instance) the given way."""
    if pbip is None:
        if how == "store-alias":
            _ps(f"Start-Process -FilePath '{str(_store_alias()).replace(chr(39), chr(39) * 2)}'")
        elif how == "msi":
            _ps(f"Start-Process -FilePath '{MSI_EXE.replace(chr(39), chr(39) * 2)}'")
        elif how == "appsfolder":
            _ps(f"Start-Process explorer.exe -ArgumentList 'shell:AppsFolder\\{STORE_APP_ID}'")
        else:
            raise ValueError(how)
        return
    quoted = str(pbip).replace("'", "''")
    if how == "association":
        _ps(f"Start-Process -FilePath '{quoted}'")
    elif how == "store-alias":
        alias = str(_store_alias()).replace("'", "''")
        _ps(f"Start-Process -FilePath '{alias}' -ArgumentList @('\"{pbip}\"')")
    elif how == "msi":
        exe = MSI_EXE.replace("'", "''")
        _ps(f"Start-Process -FilePath '{exe}' -ArgumentList @('\"{pbip}\"')")
    else:
        raise ValueError(how)


def workspace_pids() -> dict[str, int]:
    """{AnalysisServicesWorkspace_<guid>: PBIDesktop pid} from the msmdsrv command lines.

    Desktop starts one msmdsrv per window as its child and names the workspace
    folder on the command line, which is how an engine instance found through
    its port file is tied back to the window that owns it.
    """
    out = _ps("Get-CimInstance Win32_Process -Filter \"Name='msmdsrv.exe'\" | "
              "ForEach-Object { \"$($_.ParentProcessId)|$($_.CommandLine)\" }")
    mapping: dict[str, int] = {}
    for line in out.splitlines():
        pid, _, cmd = line.partition("|")
        m = re.search(r"AnalysisServicesWorkspace_[0-9A-Fa-f-]+", cmd)
        if pid.strip().isdigit() and m:
            mapping[m.group(0)] = int(pid.strip())
    return mapping


def pid_for_workspace(workspace_dir: str | os.PathLike) -> int | None:
    """The PBIDesktop process that owns the engine of ``workspace_dir``."""
    return workspace_pids().get(Path(workspace_dir).name)


def _start_host(log, timeout: float = 90.0) -> int:
    """Start a bare Desktop instance and wait until its engine is up.

    Opening a file into a running Desktop works on every build; a cold start
    with the file as an argument hangs on 2.158 (see the module docstring).
    """
    before = set(desktop_pids())
    strategies = []
    if _store_alias():
        strategies.append("store-alias")
    if Path(MSI_EXE).exists():
        strategies.append("msi")
    strategies.append("appsfolder")
    pid: int | None = None
    for how in strategies:
        log(f"starting a bare Desktop host via {how} ...")
        _start(None, how)
        until = time.monotonic() + 20
        while time.monotonic() < until and pid is None:
            new = set(desktop_pids()) - before
            if new:
                pid = min(new)
            else:
                time.sleep(1.0)
        if pid is not None:
            break
    if pid is None:
        raise TimeoutError("Power BI Desktop did not start (tried " + ", ".join(strategies) + ").")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        engines = {i["workspace_dir"] for i in engine.discover_instances()}
        if any(pid_for_workspace(w) == pid for w in engines):
            break
        time.sleep(2.0)
    time.sleep(HOST_SETTLE_SECONDS)
    log(f"host pid {pid} is up")
    return pid


# --- refresh ---------------------------------------------------------------------------

def _total_rows(conn: engine.Connection) -> int:
    try:
        tables = engine.table_row_counts(conn)["tables"]
    except engine.EngineError:
        return 0
    return sum(t["rows"] or 0 for t in tables if not t["internal"])


def refresh_data(port: int, database: str, timeout: float = 180.0,
                 poll: float = 12.0, verbose: bool = True, pid: int | None = None) -> dict:
    """Press Desktop's "Refresh now" banner until the model has rows.

    A .pbip opens without data. ``pid`` is the Desktop process holding the
    model (default: found from the engine's workspace, else the first window).
    Returns {"rows": {table: rows}, "clicks": n}; raises TimeoutError, quoting
    the load errors Desktop shows, if no table gets rows within ``timeout`` s.
    """
    conn = engine.local_connection(port, database)
    pid_args = ["-ProcessId", str(pid)] if pid else []
    deadline = time.monotonic() + timeout
    clicks, last_click = 0, 0.0
    while time.monotonic() < deadline:
        if _total_rows(conn) > 0:
            rows = {t["name"]: t["rows"] for t in engine.table_row_counts(conn)["tables"]
                    if not t["internal"]}
            if verbose:
                print(f"data loaded: {rows}", file=sys.stderr, flush=True)
            return {"rows": rows, "clicks": clicks}
        if time.monotonic() - last_click >= poll:
            state = _run_script(_PS_REFRESH, *pid_args).splitlines()
            if state and state[0] == "clicked":
                clicks += 1
                if verbose:
                    print("pressed 'Refresh now' ...", file=sys.stderr, flush=True)
            last_click = time.monotonic()
        time.sleep(2.0)
    notes = [ln for ln in _run_script(_PS_REFRESH, *pid_args, "-DiagnoseOnly").splitlines()
             if ln.startswith("note:")]
    raise TimeoutError(
        f"No table got rows within {int(timeout)} s after {clicks} refresh click(s). "
        f"Desktop says: {notes or 'nothing readable'}. Open the Desktop window and "
        "check the Load pane / Power Query editor for the failing query.")


# --- launch / close --------------------------------------------------------------------

def launch(pbip_path: str | os.PathLike, timeout: float = 300.0,
           poll: float = 3.0, verbose: bool = True, refresh: bool = False,
           host: bool | None = None) -> dict:
    """Open ``pbip_path`` in Desktop; return {port, database, pid, ...}.

    ``pid`` is the Desktop process that holds the model. Raises TimeoutError
    (with diagnostics) if no instance hosting the project's tables appears
    within ``timeout`` seconds. With ``refresh`` the data is loaded too (see
    refresh_data). ``host`` controls the bare host instance started first so
    the file is opened into a running Desktop: None (default) = only when no
    Desktop is running, True = always, False = cold start with the file.
    """
    pbip = Path(pbip_path).resolve()
    if not pbip.is_file():
        raise FileNotFoundError(pbip)
    project = PbipProject(pbip)
    wanted = sorted(t.name for t in project.list_tables())
    if not wanted:
        raise ValueError(f"{pbip} has no tables to match the engine against")

    def log(msg: str) -> None:
        if verbose:
            print(msg, file=sys.stderr, flush=True)

    host_pid: int | None = None
    if host is True or (host is None and not desktop_pids()):
        host_pid = _start_host(log)
    before = set(desktop_pids())
    strategies = ["association"]
    if _store_alias():
        strategies.append("store-alias")
    if Path(MSI_EXE).exists():
        strategies.append("msi")

    started = time.monotonic()
    new_pid: int | None = None
    for how in strategies:
        log(f"launching {pbip.name} via {how} ...")
        _start(pbip, how)
        wait_until = time.monotonic() + 20
        while time.monotonic() < wait_until:
            new = set(desktop_pids()) - before
            if new:
                new_pid = min(new)
                break
            time.sleep(1.0)
        if new_pid is not None:
            break
        log(f"no new {PROCESS_NAME} process after 20 s via {how}")
    if new_pid is None:
        raise TimeoutError(
            f"Power BI Desktop did not start for {pbip} (tried {strategies}). "
            "Is Desktop installed and .pbip associated with it?")
    log(f"{PROCESS_NAME} pid {new_pid}; waiting for tables {wanted} ...")

    last: dict = {}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        instances = engine.discover_instances(include_errors=True)
        live = [i for i in instances if i.get("reachable") and not i.get("error")]
        match = engine.match_instance(project, live)
        last = {"instances": instances, "match": match}
        if match and not match["missing"]:
            elapsed = round(time.monotonic() - started, 1)
            log(f"ready after {elapsed}s: port {match['port']}, database {match['database']}")
            model_pid = pid_for_workspace(match["workspace_dir"]) or new_pid
            info = {
                "port": match["port"],
                "database": match["database"],
                "pid": model_pid,
                "workspace_dir": match["workspace_dir"],
                "tables": wanted,
                "elapsed_s": elapsed,
            }
            if host_pid is not None:
                info["host_pid"] = host_pid
            if refresh:
                info["refresh"] = refresh_data(match["port"], match["database"],
                                               verbose=verbose, pid=model_pid)
            return info
        time.sleep(poll)

    port_files = [i.get("port_file") for i in last.get("instances", [])]
    errors = [i.get("error") for i in last.get("instances", []) if i.get("error")]
    raise TimeoutError(
        f"Desktop (pid {new_pid}) did not expose tables {wanted} within {int(timeout)} s. "
        f"Port files found: {port_files or 'none'}; instance errors: {errors or 'none'}; "
        f"best match: {last.get('match')}. Desktop may be showing a dialog "
        "(refresh prompt, sign-in) that needs a click.")


def close(pid: int | None = None, grace: float = 25.0, verbose: bool = True) -> dict:
    """Close Desktop: normal close + "Don't save", then Stop-Process if it lingers."""
    targets = [int(pid)] if pid is not None else sorted(desktop_pids())
    closed: list[int] = []
    forced: list[int] = []
    for target in targets:
        answer = _run_script(_PS_CLOSE, "-ProcessId", str(target)).splitlines()
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline and target in desktop_pids():
            time.sleep(1.0)
        if target in desktop_pids():
            _ps(f"Stop-Process -Id {target} -Force -ErrorAction SilentlyContinue")
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and target in desktop_pids():
                time.sleep(0.5)
            forced.append(target)
        else:
            closed.append(target)
        if verbose:
            how = "forced" if target in forced else (answer[-1] if answer else "closed")
            print(f"closed pid {target} ({how})", file=sys.stderr, flush=True)
    return {"closed": closed, "forced": forced, "remaining": sorted(desktop_pids())}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("pbip", nargs="?", help="path to the .pbip to open")
    ap.add_argument("--timeout", type=float, default=300.0,
                    help="seconds to wait for the engine (default 300)")
    ap.add_argument("--refresh", action="store_true",
                    help="also press 'Refresh now' and wait until tables have rows")
    ap.add_argument("--host", choices=["auto", "always", "never"], default="auto",
                    help="start a bare Desktop first so the file opens into a running "
                         "instance (auto: only when none is running)")
    ap.add_argument("--close", action="store_true",
                    help="close running Desktop instances instead of launching")
    ap.add_argument("--pid", type=int, default=None, help="with --close: only this pid")
    args = ap.parse_args(argv)
    if args.close:
        print(json.dumps(close(args.pid), indent=2))
        return 0
    if not args.pbip:
        ap.error("a .pbip path is required unless --close is given")
    host = {"auto": None, "always": True, "never": False}[args.host]
    try:
        print(json.dumps(launch(args.pbip, timeout=args.timeout, refresh=args.refresh,
                                host=host), indent=2))
    except (TimeoutError, FileNotFoundError, ValueError, engine.EngineError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
