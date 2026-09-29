"""Environment + project health report behind ``pbi_doctor`` (both servers).

The report answers "why doesn't this work?" without a debugger: which Python
and mcp SDK are running, whether the selected project is in the formats the
servers can edit (PBIR + TMDL, not legacy ``report.json`` / ``model.bim``),
which Fabric schema versions the project declares versus the ones vendored
for pre-flight validation, how much backup / undo / trash state has
accumulated, and whether Power BI Desktop is installed or currently running
(detection only -- nothing connects to it).

Every check is isolated: an exception inside one becomes a ``warning``
finding, never a raised error, so the tool always returns a report.

Output shape::

    {"ok": bool,                      # no "error" findings
     "findings": [{"severity": "info"|"warning"|"error",
                   "check": str, "message": str, "fix"?: str}],
     "environment": {...},
     "project": {...}}                # {"selected": false} when none
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path

#: Per-machine (MSI) install of Power BI Desktop.
DESKTOP_MSI_EXE = Path(r"C:\Program Files\Microsoft Power BI Desktop\bin"
                       r"\PBIDesktop.exe")
#: Microsoft Store package name.
DESKTOP_STORE_PACKAGE = "Microsoft.MicrosoftPowerBIDesktop"
#: Where a running Desktop's embedded Analysis Services instance writes its
#: port file, relative to %LOCALAPPDATA% (MSI install, then Store install).
AS_PORT_FILE_GLOBS = (
    "Microsoft/Power BI Desktop/AnalysisServicesWorkspaces/*/Data/"
    "msmdsrv.port.txt",
    "Packages/Microsoft.MicrosoftPowerBIDesktop_8wekyb3d8bbwe/LocalCache/"
    "Local/Microsoft/Power BI Desktop/AnalysisServicesWorkspaces/*/Data/"
    "msmdsrv.port.txt",
)
POWERSHELL_TIMEOUT = 20.0
#: Oldest Python the servers support (pyproject requires-python).
MIN_PYTHON = (3, 11)
#: Above these the backup pile is worth mentioning.
BACKUP_WARN_COUNT = 200
BACKUP_WARN_BYTES = 100 * 1024 * 1024
#: Folders never searched for backups (mirrors core.journal.SKIP_DIRS).
_SKIP_DIRS = {".pbi", ".pbi-mcp", ".git", "__pycache__", ".venv",
              "node_modules"}

_SCHEMA_URL = re.compile(
    r"/fabric/item/report/definition/(?P<kind>[A-Za-z]+)/"
    r"(?P<version>\d+\.\d+\.\d+)/schema\.json$")


def _desktop_ps_script() -> str:
    """One PowerShell round-trip that reports both install kinds as JSON."""
    return (
        "$o = @{}; "
        f"$msi = '{DESKTOP_MSI_EXE}'; "
        "if (Test-Path $msi) { $o.msi_version = "
        "(Get-Item $msi).VersionInfo.ProductVersion }; "
        f"$p = Get-AppxPackage -Name {DESKTOP_STORE_PACKAGE} "
        "-ErrorAction SilentlyContinue; "
        "if ($p) { $o.store_version = [string]$p.Version; "
        "$o.store_location = [string]$p.InstallLocation }; "
        "$o | ConvertTo-Json -Compress"
    )


class _Report:
    """Accumulates findings; ``run`` fences each check."""

    def __init__(self) -> None:
        self.findings: list[dict] = []
        self.environment: dict = {}
        self.project: dict = {"selected": False}

    def add(self, severity: str, check: str, message: str,
            fix: str | None = None) -> None:
        finding = {"severity": severity, "check": check, "message": message}
        if fix:
            finding["fix"] = fix
        self.findings.append(finding)

    def run(self, check: str, fn) -> None:
        try:
            fn(self)
        except Exception as exc:  # noqa: BLE001 - isolation is the contract
            self.add("warning", check,
                     f"check could not run: {type(exc).__name__}: {exc}")

    def result(self) -> dict:
        return {
            "ok": not any(f["severity"] == "error" for f in self.findings),
            "findings": self.findings,
            "environment": self.environment,
            "project": self.project,
        }


# --- public entry point -----------------------------------------------------

def run(project=None, *, server: str | None = None) -> dict:
    """Build the doctor report for an optional selected ``PbipProject``.

    ``server`` ("pbi-model" / "pbi-report") only tunes severity: a legacy
    layout of the layer *this* server edits is an error; the other layer is a
    warning, because this server's tools keep working.
    """
    rep = _Report()
    if server:
        rep.environment["server"] = server
    rep.run("python", _check_python)
    rep.run("mcp", _check_mcp)
    rep.run("pbi_mcp", _check_pbi_mcp)
    rep.run("schema_validation", _check_schema_validation)
    rep.run("desktop", _check_desktop)
    rep.run("desktop_instance", _check_desktop_instance)
    if project is not None:
        rep.project = {"selected": True, "path": str(project.path)}
        rep.run("project_layout", lambda r: _check_layout(r, project))
        rep.run("report_format",
                lambda r: _check_report_format(r, project, server))
        rep.run("model_format",
                lambda r: _check_model_format(r, project, server))
        rep.run("dataset_reference", lambda r: _check_dataset_ref(r, project))
        rep.run("page_integrity", lambda r: _check_page_integrity(r, project))
        rep.run("schema_versions", lambda r: _check_schema_versions(r, project))
        rep.run("counts", lambda r: _check_counts(r, project))
        rep.run("backups", lambda r: _check_backups(r, project))
        rep.run("journal", lambda r: _check_journal(r, project))
        rep.run("trash", lambda r: _check_trash(r, project))
    else:
        rep.add("info", "project",
                "No project selected; project checks skipped.",
                fix="Call pbi_set_project(path) then pbi_doctor() again.")
    return rep.result()


# --- environment ------------------------------------------------------------

def _check_python(rep: _Report) -> None:
    rep.environment["python"] = {
        "version": platform.python_version(),
        "executable": sys.executable,
        "platform": platform.platform(),
    }
    if sys.version_info < MIN_PYTHON:
        need = ".".join(map(str, MIN_PYTHON))
        rep.add("error", "python",
                f"Python {platform.python_version()} is below the {need} "
                "minimum.",
                fix=f"Run the servers with Python {need} or newer.")
    else:
        rep.add("info", "python", f"Python {platform.python_version()}")


def _check_mcp(rep: _Report) -> None:
    from core.mcp_compat import MCP_MAJOR, Server

    version = _dist_version("mcp")
    rep.environment["mcp"] = {"version": version, "major": MCP_MAJOR,
                              "server_class": Server.__name__}
    rep.add("info", "mcp", f"mcp SDK {version} (major {MCP_MAJOR}, "
                           f"{Server.__name__})")


def _check_pbi_mcp(rep: _Report) -> None:
    version = _dist_version("pbi-mcp")
    rep.environment["pbi_mcp"] = {"version": version}
    if version == "unknown":
        rep.add("info", "pbi_mcp",
                "pbi-mcp is not installed as a package (running from source "
                "or a frozen bundle).")
    else:
        rep.add("info", "pbi_mcp", f"pbi-mcp {version}")


def _check_schema_validation(rep: _Report) -> None:
    from core import schema_validate

    available = schema_validate.is_available()
    rep.environment["schema_validation"] = {"available": available}
    if available:
        rep.add("info", "schema_validation",
                "jsonschema + vendored Fabric schemas available; report "
                "writes are pre-flight validated.")
    else:
        rep.add("warning", "schema_validation",
                "jsonschema (or resources/schemas) missing; report writes "
                "are NOT pre-flight validated.",
                fix="pip install jsonschema")


def _dist_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


# --- Power BI Desktop (Windows only; detection, never a connection) ---------

def _is_windows() -> bool:
    return sys.platform == "win32"


def _powershell(script: str) -> str | None:
    """Run one PowerShell command; stdout, or None when it cannot run.

    stdin is detached: these servers speak MCP over stdio, and a child that
    inherited stdin could swallow protocol bytes.
    """
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True, text=True, timeout=POWERSHELL_TIMEOUT,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return None
    return (proc.stdout or "").strip()


def detect_desktop() -> dict:
    """Installed Power BI Desktop: MSI exe version and/or Store package."""
    info: dict = {"platform_supported": _is_windows(), "msi": None,
                  "store": None, "powershell": None}
    if not _is_windows():
        return info
    if DESKTOP_MSI_EXE.exists():
        info["msi"] = {"path": str(DESKTOP_MSI_EXE), "version": None}
    out = _powershell(_desktop_ps_script())
    info["powershell"] = out is not None
    try:
        data = json.loads(out) if out else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    if data.get("msi_version"):
        info["msi"] = {"path": str(DESKTOP_MSI_EXE),
                       "version": str(data["msi_version"])}
    if data.get("store_version"):
        info["store"] = {"version": str(data["store_version"]),
                         "location": data.get("store_location")}
    return info


def _check_desktop(rep: _Report) -> None:
    info = detect_desktop()
    rep.environment["desktop"] = info
    if not info["platform_supported"]:
        rep.add("info", "desktop",
                "Power BI Desktop detection is Windows-only; skipped.")
        return
    found = []
    if info["msi"]:
        found.append(f"MSI {info['msi']['version'] or '(version unknown)'}")
    if info["store"]:
        found.append(f"Store {info['store']['version']}")
    if found:
        rep.add("info", "desktop",
                "Power BI Desktop installed: " + ", ".join(found))
    elif info["powershell"] is False:
        rep.add("warning", "desktop",
                "Could not query installed apps (PowerShell unavailable or "
                "timed out); Desktop presence is unknown.")
    else:
        rep.add("warning", "desktop",
                "Power BI Desktop not found (neither MSI nor Store install).",
                fix="Install Desktop to open and verify the edited .pbip; the "
                    "servers themselves do not need it.")


def detect_desktop_instance() -> dict:
    """Is a Desktop-embedded Analysis Services instance running right now?

    Desktop writes ``msmdsrv.port.txt`` into a per-session workspace folder
    while a file is open and removes it on close. Existence is the signal;
    nothing is connected to.
    """
    local = os.environ.get("LOCALAPPDATA")
    if not _is_windows() or not local:
        return {"detectable": False, "running": False, "port_files": []}
    base = Path(local)
    files: list[str] = []
    for pattern in AS_PORT_FILE_GLOBS:
        files.extend(str(p) for p in base.glob(pattern))
    return {"detectable": True, "running": bool(files),
            "port_files": sorted(files)}


def _check_desktop_instance(rep: _Report) -> None:
    info = detect_desktop_instance()
    rep.environment["desktop_instance"] = info
    if not info["detectable"]:
        return
    if info["running"]:
        n = len(info["port_files"])
        rep.add("info", "desktop_instance",
                f"Power BI Desktop appears to be running ({n} open "
                f"workspace{'s' if n != 1 else ''}). Edits made on disk are "
                "not visible in that window until the project is reopened.",
                fix="Close the .pbip in Desktop before editing, or reopen it "
                    "afterwards to load the changes.")
    else:
        rep.add("info", "desktop_instance",
                "No running Power BI Desktop instance detected.")


# --- project ----------------------------------------------------------------

def _root(project) -> Path:
    from core.journal import project_root

    return project_root(project)


def _check_layout(rep: _Report, project) -> None:
    root = _root(project)
    rep.project["root"] = str(root)
    rep.project["semantic_model_dir"] = (
        str(project.semantic_model_dir) if project.semantic_model_dir else None)
    rep.project["report_dir"] = (
        str(project.report_dir) if project.report_dir else None)
    pbip = sorted(root.glob("*.pbip"))
    rep.project["pbip_file"] = str(pbip[0]) if pbip else None
    if not pbip:
        rep.add("warning", "project_layout",
                f"No .pbip pointer file found in {root}.",
                fix="Save the project from Power BI Desktop as a Power BI "
                    "Project (.pbip).")
    if project.semantic_model_dir is None:
        rep.add("warning", "project_layout",
                "No *.SemanticModel folder: model tools will fail.")
    if project.report_dir is None:
        rep.add("warning", "project_layout",
                "No *.Report folder: report tools will fail.")


def _legacy_severity(layer: str, server: str | None) -> str:
    """error when this server edits `layer`, else warning."""
    mine = {"pbi-model": "model", "pbi-report": "report"}.get(server or "")
    return "error" if mine in (None, layer) else "warning"


def _check_report_format(rep: _Report, project, server: str | None) -> None:
    rdir = project.report_dir
    if rdir is None:
        rep.project["report_format"] = None
        return
    definition = rdir / "definition"
    if (definition / "pages").is_dir() or (definition / "report.json").exists():
        rep.project["report_format"] = "PBIR"
        rep.add("info", "report_format",
                "Report uses the PBIR (enhanced metadata) format.")
    elif (rdir / "report.json").exists():
        rep.project["report_format"] = "legacy"
        rep.add(_legacy_severity("report", server), "report_format",
                "Report is in the legacy single report.json format; the "
                "report tools only edit PBIR (a definition/ folder with "
                "pages).",
                fix="In Desktop enable Options > Preview features > 'Store "
                    "reports using enhanced metadata format (PBIR)', then "
                    "save the project again.")
    else:
        rep.project["report_format"] = "unknown"
        rep.add("warning", "report_format",
                f"Could not recognise the report layout under {rdir}.")


def _check_model_format(rep: _Report, project, server: str | None) -> None:
    mdir = project.semantic_model_dir
    if mdir is None:
        rep.project["model_format"] = None
        return
    definition = mdir / "definition"
    if (definition / "model.tmdl").exists() or (definition / "tables").is_dir():
        rep.project["model_format"] = "TMDL"
        rep.add("info", "model_format", "Semantic model uses the TMDL format.")
    elif (mdir / "model.bim").exists():
        rep.project["model_format"] = "legacy"
        rep.add(_legacy_severity("model", server), "model_format",
                "Semantic model is a legacy model.bim; the model tools only "
                "edit TMDL (a definition/ folder).",
                fix="In Desktop enable Options > Preview features > 'Store "
                    "semantic model using TMDL format', then save again.")
    else:
        rep.project["model_format"] = "unknown"
        rep.add("warning", "model_format",
                f"Could not recognise the model layout under {mdir}.")


def _check_dataset_ref(rep: _Report, project) -> None:
    """Does the report's definition.pbir point at a model we can reach?"""
    rdir = project.report_dir
    if rdir is None or not (rdir / "definition.pbir").exists():
        return
    data = json.loads((rdir / "definition.pbir").read_text(encoding="utf-8-sig"))
    ref = data.get("datasetReference") or {}
    if "byConnection" in ref:
        rep.project["dataset_reference"] = "byConnection"
        rep.add("info", "dataset_reference",
                "The report is bound to a remote semantic model "
                "(byConnection); there is no local model for the model "
                "server to edit.")
    elif "byPath" in ref:
        rep.project["dataset_reference"] = "byPath"
        rel = (ref["byPath"] or {}).get("path")
        target = (rdir / rel).resolve() if rel else None
        if target is None or not target.exists():
            rep.add("warning", "dataset_reference",
                    f"definition.pbir points at {rel!r}, which does not "
                    "exist.",
                    fix="Fix datasetReference.byPath.path in definition.pbir "
                        "so it names the *.SemanticModel folder.")
        else:
            rep.add("info", "dataset_reference",
                    f"The report is bound to the local model {rel}.")


def _only_leftovers(folder: Path) -> bool:
    """True when a folder holds nothing but backup / temp files."""
    files = [p.name for p in folder.rglob("*") if p.is_file()]
    return all(".bak-" in n or ".tmp-" in n for n in files)


def _check_page_integrity(rep: _Report, project) -> None:
    """Page and visual folders must be complete, and pages.json must agree.

    A folder without page.json (for example the leftover of an undone or
    interrupted write) is listed as a page by the report tools and can make
    Desktop reject the report, so it deserves a loud finding.
    """
    rdir = project.report_dir
    pages_dir = rdir / "definition" / "pages" if rdir else None
    if pages_dir is None or not pages_dir.is_dir():
        return
    folders = sorted(p for p in pages_dir.iterdir() if p.is_dir())
    names = {p.name for p in folders}
    orphan_pages = [p for p in folders if not (p / "page.json").exists()]
    orphan_visuals = [
        v for p in folders if (p / "visuals").is_dir()
        for v in sorted((p / "visuals").iterdir())
        if v.is_dir() and not (v / "visual.json").exists()]
    order: list = []
    meta = pages_dir / "pages.json"
    if meta.exists():
        data = json.loads(meta.read_text(encoding="utf-8-sig"))
        order = data.get("pageOrder", []) if isinstance(data, dict) else []
    dangling = [pid for pid in order if pid not in names]

    rep.project["page_integrity"] = {
        "orphan_page_folders": [p.name for p in orphan_pages],
        "orphan_visual_folders": [
            f"{v.parent.parent.name}/{v.name}" for v in orphan_visuals],
        "page_order_without_folder": dangling,
    }
    for p in orphan_pages:
        leftover = ("only backup/temp files remain in it, the leftover of an "
                    "undone or interrupted write" if _only_leftovers(p)
                    else "it holds other files")
        rep.add("warning", "page_integrity",
                f"Page folder '{p.name}' has no page.json ({leftover}); the "
                "report tools list it as a page and Desktop may refuse to "
                "open the report.",
                fix=f"Delete the folder {p} (after checking it holds nothing "
                    "you need), or restore its page.json from a backup.")
    for v in orphan_visuals:
        rep.add("warning", "page_integrity",
                f"Visual folder '{v.parent.parent.name}/{v.name}' has no "
                "visual.json.",
                fix=f"Delete the folder {v}, or restore its visual.json.")
    if dangling:
        rep.add("warning", "page_integrity",
                f"pages.json lists page(s) with no folder: {dangling}.",
                fix="Remove them from pageOrder in pages.json, or restore the "
                    "page folders (pbi_undo_history / pbi_list_backups).")
    if not (orphan_pages or orphan_visuals or dangling):
        rep.add("info", "page_integrity",
                "Every page and visual folder is complete and consistent "
                "with pages.json.")


def vendored_schema_versions() -> dict[str, list[str]]:
    """{kind: [versions]} of the Fabric schemas vendored for validation."""
    from core.schema_validate import _SCHEMA_DIR

    index = _SCHEMA_DIR / "index.json"
    if not index.exists():
        return {}
    out: dict[str, list[str]] = {}
    for url in json.loads(index.read_text(encoding="utf-8")):
        m = _SCHEMA_URL.search(url)
        if m:
            out.setdefault(m["kind"], []).append(m["version"])
    return {k: sorted(v, key=_vkey) for k, v in out.items()}


def project_schema_versions(report_dir: Path) -> dict[str, dict[str, int]]:
    """{kind: {version: file count}} declared by ``$schema`` in the report."""
    out: dict[str, dict[str, int]] = {}
    definition = Path(report_dir) / "definition"
    if not definition.is_dir():
        return out
    for path in definition.rglob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (ValueError, OSError):
            continue
        url = data.get("$schema") if isinstance(data, dict) else None
        m = _SCHEMA_URL.search(url) if isinstance(url, str) else None
        if not m:
            continue
        kind = out.setdefault(m["kind"], {})
        kind[m["version"]] = kind.get(m["version"], 0) + 1
    return out


def _vkey(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


def _check_schema_versions(rep: _Report, project) -> None:
    if project.report_dir is None:
        return
    vendored = vendored_schema_versions()
    found = project_schema_versions(project.report_dir)
    rep.project["schema_versions"] = {"project": found, "vendored": vendored}
    if not found:
        rep.add("info", "schema_versions",
                "No $schema declarations found in the report JSON.")
        return
    for kind, versions in sorted(found.items()):
        have = vendored.get(kind)
        for version, n in sorted(versions.items(), key=lambda kv: _vkey(kv[0])):
            where = f"{kind}/{version} ({n} file{'s' if n != 1 else ''})"
            if not have:
                rep.add("info", "schema_versions",
                        f"{where}: no vendored schema for this kind, so "
                        "these files are not pre-flight validated.")
            elif version in have:
                rep.add("info", "schema_versions",
                        f"{where}: matches a vendored schema.")
            elif _vkey(version) > _vkey(have[-1]):
                rep.add("info", "schema_versions",
                        f"{where}: newer than the vendored {have[-1]}; "
                        "structure is still validated and version drift is "
                        "tolerated.")
            else:
                rep.add("warning", "schema_versions",
                        f"{where}: older than the vendored {have}; Desktop "
                        "upgrades it on the next save.")


def _check_counts(rep: _Report, project) -> None:
    counts: dict = {}
    if project.semantic_model_dir is not None:
        try:
            tables = project.list_tables()
            counts["tables"] = len(tables)
            counts["columns"] = sum(len(t.columns) for t in tables)
            counts["measures"] = sum(len(t.measures) for t in tables)
        except Exception as exc:  # noqa: BLE001
            rep.add("warning", "counts",
                    f"model could not be read: {type(exc).__name__}: {exc}")
    if project.report_dir is not None:
        try:
            pages = project.list_pages()
            counts["pages"] = len(pages)
            counts["visuals"] = sum(p.visual_count for p in pages)
        except Exception as exc:  # noqa: BLE001
            rep.add("warning", "counts",
                    f"report could not be read: {type(exc).__name__}: {exc}")
    rep.project["counts"] = counts
    rep.add("info", "counts",
            ", ".join(f"{v} {k}" for k, v in counts.items())
            or "nothing readable")


def _walk_files(root: Path):
    """Every file under `root`, pruning caches, VCS folders and the undo store."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            yield Path(dirpath) / name


def _check_backups(rep: _Report, project) -> None:
    files = [p for p in _walk_files(_root(project)) if ".bak-" in p.name]
    size = sum(p.stat().st_size for p in files)
    rep.project["backups"] = {"count": len(files), "bytes": size}
    msg = f"{len(files)} backup file(s) (*.bak-*), {_human(size)}"
    if len(files) > BACKUP_WARN_COUNT or size > BACKUP_WARN_BYTES:
        rep.add("warning", "backups", msg + " - a large pile.",
                fix="Once the project is verified in Desktop, delete old "
                    "*.bak-* files (pbi_list_backups lists them).")
    else:
        rep.add("info", "backups", msg)


def _check_journal(rep: _Report, project) -> None:
    from core.journal import STORE_DIR

    store = _root(project) / STORE_DIR
    undo = store / "undo"
    entries = ([p for p in undo.iterdir() if p.is_dir()]
               if undo.is_dir() else [])
    size = (sum(p.stat().st_size for p in store.rglob("*") if p.is_file())
            if store.is_dir() else 0)
    tx = None
    tx_file = store / "transaction.json"
    if tx_file.exists():
        try:
            tx = json.loads(tx_file.read_text(encoding="utf-8"))
        except ValueError:
            tx = {"started": "unknown"}
    rep.project["journal"] = {"entries": len(entries), "bytes": size,
                              "dir": str(store), "open_transaction": tx}
    rep.add("info", "journal",
            f"{len(entries)} undo entr{'y' if len(entries) == 1 else 'ies'}, "
            f"{_human(size)} in {STORE_DIR}/ (pbi_undo_history / pbi_undo)")
    if tx:
        rep.add("warning", "journal",
                f"A transaction has been open since {tx.get('started')}.",
                fix="Call pbi_commit to keep its writes or pbi_rollback to "
                    "revert them.")


def _check_trash(rep: _Report, project) -> None:
    if project.report_dir is None:
        return
    items = project.list_trash()
    rep.project["trash"] = {"count": len(items)}
    rep.add("info", "trash",
            f"{len(items)} item(s) in .pbi/mcp-trash "
            "(pbi_list_trash / pbi_restore_visual)")


def _human(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    value = float(n)
    for unit in ("KB", "MB", "GB"):
        value /= 1024
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}"
    return f"{value:.1f} GB"  # pragma: no cover
