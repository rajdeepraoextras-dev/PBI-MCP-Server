"""Build a fully self-contained plugin bundle (E2E "download -> works").

Freezes both MCP servers into ONE standalone executable (Python runtime +
all deps + vendored schemas inside), then packages it with a manifest that
launches the exe directly. The host needs nothing installed — no Python, no
pip.

Output: dist/pbi-mcp-standalone-<os>.plugin

Note: the executable is OS + architecture specific. Run this on each target OS
to produce that platform's bundle (this build == the machine it runs on).

Run:  .venv/Scripts/python.exe scripts/build_standalone.py
"""

from __future__ import annotations

import json
import platform
import subprocess
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DIST = REPO / "dist"
VERSION = "2.0.0"

EXE_NAME = "pbi-mcp.exe" if sys.platform == "win32" else "pbi-mcp"


def freeze() -> Path:
    """Run PyInstaller to produce the standalone executable."""
    cmd = [
        sys.executable, "-m", "PyInstaller", "--noconfirm",
        "--onefile", "--name", "pbi-mcp", "--paths", str(REPO),
        "--add-data", f"resources/schemas{';' if sys.platform=='win32' else ':'}resources/schemas",
        "--collect-submodules", "core",
        "--collect-submodules", "model_server",
        "--collect-submodules", "report_server",
        "--collect-submodules", "mcp.server",
        "--collect-data", "mcp",
        "--collect-all", "jsonschema",
        "--collect-all", "referencing",
        "--collect-all", "pydantic",
        "--exclude-module", "mcp.cli",
        "--exclude-module", "pytest",
        "--exclude-module", "IPython",
        str(REPO / "scripts" / "launcher.py"),
    ]
    subprocess.run(cmd, cwd=REPO, check=True)
    exe = DIST / EXE_NAME
    if not exe.exists():
        raise SystemExit(f"build failed: {exe} not produced")
    return exe


def plugin_manifest() -> dict:
    """`.claude-plugin/plugin.json` — metadata only (matches Claude's format)."""
    return {
        "name": "pbi-mcp",
        "version": VERSION,
        "description": ("Build Power BI reports and models from natural "
                        "language — pages, visuals, themes, layouts, measures, "
                        "lineage. Local-file PBIP format. Self-contained: no "
                        "Python or dependencies needed on the host. "
                        "By Rajdeep Rao — "
                        "https://www.linkedin.com/in/rajdeep-rao-14bab1320/"),
        "author": {"name": "Rajdeep Rao"},
        "keywords": ["power-bi", "powerbi", "report", "pbir", "tmdl",
                     "data-visualization", "dashboard"],
        "license": "MIT",
    }


def mcp_config() -> dict:
    """`.mcp.json` — the MCP servers, launched from the bundled executable."""
    exe = "${CLAUDE_PLUGIN_ROOT}/" + EXE_NAME
    return {
        "mcpServers": {
            "pbi-model": {"command": exe, "args": ["model"]},
            "pbi-report": {"command": exe, "args": ["report"]},
        }
    }


INSTALL = """\
# pbi-mcp (standalone) — zero-setup install

This bundle is fully self-contained: the executable includes the Python
runtime, all dependencies, and the Fabric schemas. The host needs nothing
installed.

1. Add this .plugin to your MCP host (drag-drop).
2. Both servers register: pbi-model and pbi-report.
3. In a session call pbi_set_project(path-to-a-.pbip) first.

Built for: {platform}. The executable is OS + architecture specific — use a
build matching the machine that runs the host.
"""


def main() -> None:
    DIST.mkdir(exist_ok=True)
    exe = freeze()

    tag = f"{sys.platform}-{platform.machine()}".lower()
    bundle = DIST / f"pbi-mcp-standalone-{tag}.plugin"
    if bundle.exists():
        bundle.unlink()

    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(".claude-plugin/plugin.json",
                    json.dumps(plugin_manifest(), indent=2))
        zf.writestr(".mcp.json", json.dumps(mcp_config(), indent=2))
        zf.writestr("INSTALL.md", INSTALL.format(platform=tag))
        zf.write(exe, EXE_NAME)
        zf.write(REPO / "README.md", "README.md")
        # bundled skills (workflow guidance, invokable via /name in chat)
        skills_dir = REPO / "skills"
        for f in sorted(skills_dir.rglob("*.md")):
            zf.write(f, f.relative_to(REPO).as_posix())

    mb = bundle.stat().st_size / 1_048_576
    print(f"built {bundle.name} ({mb:.1f} MB) — self-contained, no host deps")
    print(f"  structure: .claude-plugin/plugin.json + .mcp.json + {EXE_NAME}")


if __name__ == "__main__":
    main()
