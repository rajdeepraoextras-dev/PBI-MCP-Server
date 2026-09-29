"""``pbi-mcp`` — one command line for every server in this package.

    pbi-mcp <model|report|service> [--transport stdio|streamable-http|sse]
            [--host 127.0.0.1] [--port 8000] [--project PATH]

This is the PyPI console script (``uvx pbi-mcp report``), the file the
``.mcpb`` bundles launch (``python core/cli.py report``) and a plain
``python -m core.cli``. Server modules are imported lazily, so ``--help`` and
argument errors never pay for (or fail on) a server import, and ``service``
reports a clean error when the optional ``service_server`` package is not
part of the install.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import os
import sys
from pathlib import Path

if __package__ in (None, ""):
    # Executed as a file (python core/cli.py ...): sys.path[0] is core/, so
    # put the checkout / bundle root first to make `core.*` importable.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SERVERS: dict[str, str] = {
    "model": "model_server.server",
    "report": "report_server.server",
    "service": "service_server.server",
}
TRANSPORTS = ("stdio", "streamable-http", "sse")
PROJECT_ENV = "PBI_MCP_PROJECT"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _version() -> str:
    try:
        from importlib.metadata import version
        return version("pbi-mcp")
    except Exception:  # not installed (running from a bundle or checkout)
        return "unknown"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pbi-mcp",
        description="Run one of the pbi-mcp MCP servers (Power BI Project "
                    "automation on local .pbip files).",
        epilog="Every session starts with pbi_set_project(path); --project or "
               f"${PROJECT_ENV} preselects it.",
    )
    p.add_argument("server", choices=sorted(SERVERS),
                   help="which server to run: model (TMDL), report (PBIR) or "
                        "service (Power BI Service / Fabric; optional package)")
    p.add_argument("--transport", choices=TRANSPORTS, default="stdio",
                   help="MCP transport (default: stdio)")
    p.add_argument("--host", default="127.0.0.1",
                   help="bind address for the HTTP transports (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=8000,
                   help="port for the HTTP transports (default: 8000)")
    p.add_argument("--project", metavar="PATH",
                   help="preselect a .pbip file or project folder (same as "
                        f"setting {PROJECT_ENV})")
    p.add_argument("--version", action="version", version=f"pbi-mcp {_version()}")
    return p


def load_server(which: str):
    """Import and return the server module for ``which``.

    Raises ``SystemExit`` with a one-line message (printed to stderr, exit
    status 1) when the server's own package is not part of this install (the
    ``service`` server ships separately and may be absent). An import error
    raised from *inside* an installed server, such as a missing third-party
    dependency, propagates unchanged so the real cause stays visible.
    """
    modname = SERVERS[which]
    package = modname.split(".")[0]
    try:
        return importlib.import_module(modname)
    except ModuleNotFoundError as exc:
        if exc.name in (package, modname):
            installed = [s for s, m in sorted(SERVERS.items())
                         if importlib.util.find_spec(m.split(".")[0]) is not None]
            raise SystemExit(
                f"pbi-mcp: the '{which}' server is not available in this install "
                f"(package '{package}' not found). Installed servers: "
                + (", ".join(installed) or "none") + ".") from None
        raise


def _preselect_project(mod, path: str | None) -> None:
    """Call the module's ``set_project(STATE, path)`` if it has one.

    ``path`` comes from ``--project`` or ``$PBI_MCP_PROJECT``; the ``.mcpb``
    manifests route an optional user_config value through the env var, so an
    empty or unsubstituted ``${...}`` placeholder means "not set". A bad path
    is a warning, not a startup failure — the tools still work after an
    explicit pbi_set_project.
    """
    if not path or path.startswith("${"):
        return
    set_project = getattr(mod, "set_project", None)
    state = getattr(mod, "STATE", None)
    if set_project is None or state is None:
        return
    try:
        set_project(state, path)
    except Exception as exc:  # noqa: BLE001 — report and keep serving
        print(f"pbi-mcp: warning: could not preselect project {path!r}: {exc}",
              file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.transport != "stdio" and args.host not in LOOPBACK_HOSTS:
        print(f"pbi-mcp: warning: {args.host}:{args.port} is reachable beyond this "
              "machine, and these servers read and write local project files with "
              "no authentication of their own. Bind to 127.0.0.1 unless something "
              "in front of it authenticates callers.", file=sys.stderr)
    mod = load_server(args.server)
    _preselect_project(mod, args.project or os.environ.get(PROJECT_ENV))
    from core import mcp_compat
    try:
        mcp_compat.run_server(mod.mcp, transport=args.transport,
                              host=args.host, port=args.port)
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
