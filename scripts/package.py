"""Day 34: build the distributable plugin bundle.

Produces dist/:
  pbi-mcp.plugin        — zip bundle: plugin.json manifest (both MCP servers)
                          + source + pyproject + README + INSTALL notes.
                          Drag-drop into a plugin-aware host (e.g. Cowork);
                          the manifest tells the host how to launch each
                          server (python -m ..., cwd=bundle root).
  claude_desktop_config.snippet.json — copy-paste block for Claude Desktop.

Run:  .venv/Scripts/python.exe scripts/package.py
"""

from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DIST = REPO / "dist"

VERSION = "2.0.1"

# Claude plugin metadata (goes in .claude-plugin/plugin.json — same format as
# the standalone bundle, minus the frozen exe: this variant needs host Python).
PLUGIN_JSON = {
    "name": "power-bi-mcp-server",
    "version": VERSION,
    "description": (
        "Build Power BI reports and models from natural language — local-file "
        "PBIP (TMDL + PBIR). Source bundle: needs Python 3.11+ with "
        "mcp/pydantic/jsonschema on the host (use the standalone build for "
        "zero-setup)."
    ),
    "author": {"name": "Rajdeep Rao"},
    "keywords": ["power-bi", "powerbi", "report", "pbir", "tmdl", "dashboard"],
    "license": "MIT",
}

MCP_JSON = {
    "mcpServers": {
        "pbi-model": {
            "command": "python",
            "args": ["-m", "model_server.server"],
            "cwd": "${CLAUDE_PLUGIN_ROOT}",
        },
        "pbi-report": {
            "command": "python",
            "args": ["-m", "report_server.server"],
            "cwd": "${CLAUDE_PLUGIN_ROOT}",
        },
    }
}

INSTALL_MD = """\
# Installing pbi-mcp

## Option A — plugin bundle (drag-drop)
Drop `pbi-mcp.plugin` onto a plugin-aware MCP host (e.g. Cowork).
The manifest registers two servers: `pbi-model` and `pbi-report`.
The host machine needs Python 3.11+ with:
`pip install "mcp<3" pydantic jsonschema`

## Option B — Claude Desktop (manual)
1. Unzip the bundle (or clone the repo) somewhere permanent.
2. `python -m venv .venv && .venv\\Scripts\\pip install "mcp<3" pydantic jsonschema`
3. Merge `claude_desktop_config.snippet.json` into your
   `claude_desktop_config.json` (fix the two absolute paths).
4. Restart Claude Desktop; call `pbi_set_project` first in each server.

## First call
Every session starts with `pbi_set_project(path)` pointing at a `.pbip`
file or project folder saved in Power BI Desktop's PBIP format
(Preview features: "Power BI Project (.pbip) save option" + PBIR).
"""

INCLUDE = [
    "core", "model_server", "report_server",
    "pyproject.toml", "README.md",
]


def main() -> None:
    DIST.mkdir(exist_ok=True)
    bundle = DIST / "pbi-mcp.plugin"
    if bundle.exists():
        bundle.unlink()

    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(".claude-plugin/plugin.json",
                    json.dumps(PLUGIN_JSON, indent=2))
        zf.writestr(".mcp.json", json.dumps(MCP_JSON, indent=2))
        zf.writestr("INSTALL.md", INSTALL_MD)
        for item in INCLUDE:
            src = REPO / item
            if src.is_file():
                zf.write(src, item)
            else:
                for f in sorted(src.rglob("*.py")):
                    if "__pycache__" in f.parts:
                        continue
                    zf.write(f, f.relative_to(REPO).as_posix())
        # vendored Fabric schemas (needed for offline pre-flight validation)
        for f in sorted((REPO / "resources" / "schemas").glob("*.json")):
            zf.write(f, f.relative_to(REPO).as_posix())
        # bundled skills
        for f in sorted((REPO / "skills").rglob("*.md")):
            zf.write(f, f.relative_to(REPO).as_posix())

    snippet = {
        "mcpServers": {
            "pbi-model": {
                "command": str(REPO / ".venv" / "Scripts" / "python.exe"),
                "args": ["-m", "model_server.server"],
                "cwd": str(REPO),
            },
            "pbi-report": {
                "command": str(REPO / ".venv" / "Scripts" / "python.exe"),
                "args": ["-m", "report_server.server"],
                "cwd": str(REPO),
            },
        }
    }
    (DIST / "claude_desktop_config.snippet.json").write_text(
        json.dumps(snippet, indent=2), encoding="utf-8")

    size = bundle.stat().st_size // 1024
    names = zipfile.ZipFile(bundle).namelist()
    print(f"built {bundle.name} ({size} KB, {len(names)} files)")
    print(f"built claude_desktop_config.snippet.json")


if __name__ == "__main__":
    main()
