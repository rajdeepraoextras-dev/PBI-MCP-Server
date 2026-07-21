"""Single entry point for the frozen (standalone) build.

Usage: pbi-mcp <model|report>
The plugin manifest launches this with one arg, so one bundled executable
serves both MCP servers. When frozen by PyInstaller, the Python runtime,
dependencies, and vendored schemas are all inside the binary — the host needs
nothing installed.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _bootstrap_paths() -> None:
    """Make the packages importable both frozen and from source."""
    if getattr(sys, "frozen", False):
        base = Path(sys._MEIPASS)  # type: ignore[attr-defined]
    else:
        base = Path(__file__).resolve().parent.parent
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))


def main() -> None:
    _bootstrap_paths()
    which = (sys.argv[1] if len(sys.argv) > 1 else "report").lower()
    if which in ("model", "pbi-model"):
        from model_server.server import main as run
    elif which in ("report", "pbi-report"):
        from report_server.server import main as run
    else:
        sys.stderr.write(f"unknown server {which!r}; use 'model' or 'report'\n")
        sys.exit(2)
    run()


if __name__ == "__main__":
    main()
