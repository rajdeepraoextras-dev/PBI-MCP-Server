"""Build the distributable bundles into dist/ (or any directory via build()).

Produces:
  pbi-mcp.plugin        — Claude plugin bundle (zip): .claude-plugin/plugin.json
                          + .mcp.json registering every server + source +
                          pyproject + README + INSTALL notes + skills. Drag-drop
                          into a plugin-aware host; needs host Python 3.11+ with
                          mcp/pydantic/jsonschema.
  pbi-mcp-<server>.mcpb — one MCP Bundle per server (Claude Desktop extension,
                          spec: https://github.com/anthropics/mcpb). manifest.json
                          + the source tree the server needs. Same host
                          requirements as the .plugin.
  claude_desktop_config.snippet.json — copy-paste block for Claude Desktop.
                          Default: points at this checkout's .venv (local
                          paths). --portable: `uvx pbi-mcp <server>` entries
                          with no machine-specific paths (what releases ship).

Run:  .venv/Scripts/python.exe scripts/package.py [--dist DIR] [--portable]
Import: ``from scripts.package import build; build(Path("out"))`` (tests do).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tomllib
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DIST = REPO / "dist"

VERSION = "2.1.0"
REPO_URL = "https://github.com/rajdeepraoextras-dev/PBI-MCP-Server"
AUTHOR = {"name": "Rajdeep Rao",
          "url": "https://www.linkedin.com/in/rajdeep-rao-14bab1320/"}
KEYWORDS = ["power-bi", "powerbi", "pbip", "pbir", "tmdl", "dax", "report",
            "dashboard", "semantic-model", "data-visualization"]
# Single source of truth: the same list pip resolves for the wheel.
RUNTIME_DEPS: list[str] = tomllib.loads(
    (REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]

# Every server this repo can ship. service_server is built on a sibling branch
# and may be absent from a checkout; everything below tolerates that.
SERVER_PACKAGES = {"model": "model_server", "report": "report_server",
                   "service": "service_server"}
SERVER_TITLES = {
    "model": ("Power BI Model", "semantic model layer (TMDL): measures, columns, "
              "relationships, calculation groups, DAX lineage"),
    "report": ("Power BI Report", "report layer (PBIR): pages, visuals, formatting, "
               "themes, filters, design elements, one-call scaffolding"),
    "service": ("Power BI Service", "Power BI Service / Fabric workspaces: deploy, "
                "refresh and inspect published items"),
}


def available_servers() -> list[str]:
    """Servers whose package exists in this checkout, in canonical order."""
    return [k for k, pkg in SERVER_PACKAGES.items() if (REPO / pkg).is_dir()]


# --- Claude plugin bundle ----------------------------------------------------

PLUGIN_JSON = {
    "name": "power-bi-mcp-server",
    "version": VERSION,
    "description": (
        "Build Power BI reports and models from natural language — local-file "
        "PBIP (TMDL + PBIR). Source bundle: needs Python 3.11+ with "
        "mcp/pydantic/jsonschema on the host (use the standalone build for "
        "zero-setup)."
    ),
    "author": {"name": AUTHOR["name"]},
    "keywords": ["power-bi", "powerbi", "report", "pbir", "tmdl", "dashboard"],
    "license": "MIT",
}


def mcp_json(servers: list[str]) -> dict:
    return {"mcpServers": {
        f"pbi-{s}": {"command": "python",
                     "args": ["-m", f"{SERVER_PACKAGES[s]}.server"],
                     "cwd": "${CLAUDE_PLUGIN_ROOT}"}
        for s in servers}}


INSTALL_MD = """\
# Installing pbi-mcp

## Option A — plugin bundle (drag-drop)
Drop `pbi-mcp.plugin` onto a plugin-aware MCP host (e.g. Cowork).
The manifest registers the servers: {servers}.
The host machine needs Python 3.11+ with:
`pip install "mcp<3" pydantic jsonschema`

## Option B — Claude Desktop (manual)
1. Unzip the bundle (or clone the repo) somewhere permanent.
2. `python -m venv .venv && .venv\\Scripts\\pip install "mcp<3" pydantic jsonschema`
3. Merge `claude_desktop_config.snippet.json` into your
   `claude_desktop_config.json` (fix the two absolute paths).
4. Restart Claude Desktop; call `pbi_set_project` first in each server.

## Option C — PyPI
`pip install pbi-mcp` then `pbi-mcp report` / `pbi-mcp model` (stdio), or
`uvx pbi-mcp report` with no install at all.

## First call
Every session starts with `pbi_set_project(path)` pointing at a `.pbip`
file or project folder saved in Power BI Desktop's PBIP format
(Preview features: "Power BI Project (.pbip) save option" + PBIR).
"""


def _add_tree(zf: zipfile.ZipFile, rel: str, pattern: str = "*.py") -> None:
    src = REPO / rel
    if src.is_file():
        zf.write(src, rel)
        return
    for f in sorted(src.rglob(pattern)):
        if "__pycache__" in f.parts:
            continue
        zf.write(f, f.relative_to(REPO).as_posix())


def _add_common(zf: zipfile.ZipFile, servers: list[str]) -> None:
    """Source tree shared by every bundle flavour."""
    for item in ["core", *(SERVER_PACKAGES[s] for s in servers),
                 "pyproject.toml", "README.md", "LICENSE"]:
        _add_tree(zf, item)
    # vendored Fabric schemas (needed for offline pre-flight validation)
    for f in sorted((REPO / "resources" / "schemas").glob("*.json")):
        zf.write(f, f.relative_to(REPO).as_posix())
    # Best Practice Analyzer rule catalog
    zf.write(REPO / "resources" / "bpa_rules.json", "resources/bpa_rules.json")


def build_plugin(dist: Path, servers: list[str]) -> Path:
    bundle = dist / "pbi-mcp.plugin"
    if bundle.exists():
        bundle.unlink()
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(".claude-plugin/plugin.json", json.dumps(PLUGIN_JSON, indent=2))
        zf.writestr(".mcp.json", json.dumps(mcp_json(servers), indent=2))
        zf.writestr("INSTALL.md", INSTALL_MD.format(
            servers=", ".join(f"`pbi-{s}`" for s in servers)))
        _add_common(zf, servers)
        _add_tree(zf, "skills", "*.md")  # bundled skills
    return bundle


# --- MCP Bundles (.mcpb) -----------------------------------------------------

def _tool_list(which: str) -> list[dict] | None:
    """[{name, description}] for the manifest, by asking the server module.

    Best effort: the manifest is still valid without it (tools_generated
    covers the gap), and the service server may need extras we don't have.
    """
    try:
        sys.path.insert(0, str(REPO))
        import importlib
        mod = importlib.import_module(f"{SERVER_PACKAGES[which]}.server")
        tools = asyncio.run(mod.mcp.list_tools())
        return [{"name": t.name,
                 "description": " ".join((t.description or "").split())[:200]}
                for t in tools]
    except Exception:  # noqa: BLE001 — manifest stays valid without the list
        return None


def mcpb_manifest(which: str) -> dict:
    """manifest.json for one server, per MANIFEST.md (manifest_version 0.3)."""
    title, blurb = SERVER_TITLES[which]
    manifest = {
        "manifest_version": "0.3",
        "name": f"pbi-mcp-{which}",
        "display_name": f"{title} (pbi-mcp)",
        "version": VERSION,
        "description": f"Power BI Project (.pbip) automation — {blurb}.",
        "long_description": (
            f"The `pbi-{which}` MCP server from pbi-mcp: reads and writes the "
            f"{blurb} of a Power BI Project saved in Desktop's PBIP format, "
            "directly on disk — no Power BI API, no auth, no cloud. Every write "
            "is atomic, backed up, journaled (`pbi_undo`) and, for the report "
            "layer, pre-flight validated against the official Fabric schemas.\n\n"
            "Requires Python 3.11+ on this machine with "
            "`pip install " + " ".join(f'"{d}"' for d in RUNTIME_DEPS) + "` "
            "(also listed in requirements.txt inside this bundle). "
            "Start every session with `pbi_set_project(path)`, or set the "
            "default project below."
        ),
        "author": AUTHOR,
        "repository": {"type": "git", "url": REPO_URL + ".git"},
        "homepage": REPO_URL,
        "documentation": REPO_URL + "#readme",
        "support": REPO_URL + "/issues",
        "license": "MIT",
        "keywords": KEYWORDS,
        "server": {
            "type": "python",
            "entry_point": "core/cli.py",
            "mcp_config": {
                "command": "python",
                "args": ["${__dirname}/core/cli.py", which],
                "env": {"PBI_MCP_PROJECT": "${user_config.project_path}"},
                "platform_overrides": {
                    "darwin": {"command": "python3"},
                    "linux": {"command": "python3"},
                },
            },
        },
        "compatibility": {
            "platforms": ["darwin", "win32", "linux"],
            "runtimes": {"python": ">=3.11"},
        },
        "user_config": {
            "project_path": {
                "type": "directory",
                "title": "Default Power BI project",
                "description": ("Folder of a .pbip project to select at startup "
                                "(optional — pbi_set_project can switch later)."),
                "required": False,
            },
        },
    }
    tools = _tool_list(which)
    if tools:
        manifest["tools"] = tools
        manifest["tools_generated"] = False
    else:
        manifest["tools_generated"] = True
    return manifest


def build_mcpb(dist: Path, which: str) -> Path:
    bundle = dist / f"pbi-mcp-{which}.mcpb"
    if bundle.exists():
        bundle.unlink()
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(mcpb_manifest(which), indent=2))
        zf.writestr("requirements.txt", "\n".join(RUNTIME_DEPS) + "\n")
        _add_common(zf, [which])
    return bundle


# --- Claude Desktop snippet ---------------------------------------------------

def desktop_snippet(servers: list[str], portable: bool = False) -> dict:
    """Claude Desktop ``mcpServers`` block.

    ``portable=False`` launches from this checkout's ``.venv`` (the absolute
    paths are specific to the machine that ran the build). ``portable=True``
    uses ``uvx pbi-mcp <server>``: nothing machine-specific, works anywhere
    uv is installed once the package is on PyPI.
    """
    if portable:
        return {"mcpServers": {f"pbi-{s}": {"command": "uvx", "args": ["pbi-mcp", s]}
                               for s in servers}}
    exe = REPO / ".venv" / ("Scripts/python.exe" if sys.platform == "win32"
                            else "bin/python")
    return {"mcpServers": {
        f"pbi-{s}": {"command": str(exe),
                     "args": ["-m", f"{SERVER_PACKAGES[s]}.server"],
                     "cwd": str(REPO)}
        for s in servers}}


# --- driver ------------------------------------------------------------------

def build(dist_dir: Path | str = DIST, portable: bool = False) -> list[Path]:
    """Build every artifact into ``dist_dir``; returns the paths written."""
    dist = Path(dist_dir)
    dist.mkdir(parents=True, exist_ok=True)
    servers = available_servers()
    out = [build_plugin(dist, servers)]
    out += [build_mcpb(dist, s) for s in servers]
    snippet = dist / "claude_desktop_config.snippet.json"
    snippet.write_text(json.dumps(desktop_snippet(servers, portable), indent=2),
                       encoding="utf-8")
    out.append(snippet)
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Build pbi-mcp distribution bundles.")
    p.add_argument("--dist", default=str(DIST), help="output directory (default: dist/)")
    p.add_argument("--portable", action="store_true",
                   help="write the Claude Desktop snippet as `uvx pbi-mcp <server>` "
                        "instead of paths into this checkout's .venv")
    args = p.parse_args(argv)
    for path in build(args.dist, portable=args.portable):
        if path.suffix in (".plugin", ".mcpb"):
            n = len(zipfile.ZipFile(path).namelist())
            print(f"built {path.name} ({path.stat().st_size // 1024} KB, {n} files)")
        else:
            print(f"built {path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
