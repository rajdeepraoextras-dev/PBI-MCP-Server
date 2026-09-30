"""core/cli (the `pbi-mcp` command) and core.mcp_compat.run_server."""

from __future__ import annotations

import http.client
import json
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import cli, mcp_compat

REPO = Path(__file__).resolve().parent.parent
SYNTH_DIR = REPO / "tests" / "fixtures" / "synthetic"


# --- argument parsing --------------------------------------------------------

def test_help_lists_servers_transports_and_options(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith("usage: pbi-mcp")
    for word in ("model", "report", "service", "stdio", "streamable-http", "sse",
                 "--transport", "--host", "--port", "--project", "127.0.0.1", "8000"):
        assert word in out, word


def test_module_invocation_help():
    r = subprocess.run([sys.executable, "-m", "core.cli", "--help"], cwd=REPO,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert "usage: pbi-mcp" in r.stdout


def test_runs_as_a_file_from_any_directory(tmp_path):
    """The .mcpb manifests launch `python <bundle>/core/cli.py <server>`."""
    r = subprocess.run([sys.executable, str(REPO / "core" / "cli.py"), "--version"],
                       cwd=tmp_path, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("pbi-mcp ")


@pytest.mark.parametrize("argv", [
    [],                                          # server is required
    ["bogus"],                                   # unknown server
    ["report", "--transport", "carrier-pigeon"],
    ["report", "--port", "eighty"],
])
def test_usage_errors_exit_2(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2
    assert "usage: pbi-mcp" in capsys.readouterr().err


# --- server loading -----------------------------------------------------------

def test_load_server_returns_the_real_modules():
    from model_server import server as model
    from report_server import server as report
    assert cli.load_server("model").mcp is model.mcp
    assert cli.load_server("report").mcp is report.mcp


def test_service_server_absent_is_a_clean_error(monkeypatch):
    # A None entry makes `import service_server` raise ModuleNotFoundError,
    # exactly what an install without that package does.
    monkeypatch.setitem(sys.modules, "service_server", None)
    monkeypatch.delitem(sys.modules, "service_server.server", raising=False)
    with pytest.raises(SystemExit) as exc:
        cli.main(["service"])
    msg = str(exc.value.code)
    assert "service_server" in msg and "not available" in msg
    assert msg.endswith("Installed servers: model, report.")   # what *is* installed


def test_missing_third_party_dependency_is_not_masked(tmp_path, monkeypatch):
    """Only the server's own package being absent gets the friendly message."""
    pkg = tmp_path / "broken_server_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "server.py").write_text("import definitely_not_an_installed_dep_xyz\n",
                                   encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setitem(cli.SERVERS, "model", "broken_server_pkg.server")
    with pytest.raises(ModuleNotFoundError) as exc:
        cli.load_server("model")
    assert exc.value.name == "definitely_not_an_installed_dep_xyz"


# --- dispatch ------------------------------------------------------------------

@pytest.fixture
def fake_run(monkeypatch):
    """Replace server loading and run_server; record what main() asks for."""
    calls: list[tuple[object, dict]] = []
    mcp_obj = object()
    module = SimpleNamespace(mcp=mcp_obj)
    monkeypatch.setattr(cli, "load_server", lambda which: module)
    monkeypatch.setattr(mcp_compat, "run_server",
                        lambda mcp, **kw: calls.append((mcp, kw)))
    monkeypatch.delenv(cli.PROJECT_ENV, raising=False)
    return SimpleNamespace(calls=calls, mcp=mcp_obj, module=module)


def test_defaults_are_stdio_on_loopback(fake_run, capsys):
    assert cli.main(["report"]) == 0
    assert fake_run.calls == [(fake_run.mcp, {"transport": "stdio",
                                              "host": "127.0.0.1", "port": 8000})]
    assert capsys.readouterr().err == ""


def test_transport_host_and_port_are_forwarded(fake_run):
    assert cli.main(["model", "--transport", "streamable-http",
                     "--host", "127.0.0.1", "--port", "9123"]) == 0
    assert fake_run.calls == [(fake_run.mcp, {"transport": "streamable-http",
                                              "host": "127.0.0.1", "port": 9123})]


def test_dispatch_loads_the_requested_server(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "load_server",
                        lambda which: seen.append(which) or SimpleNamespace(mcp=object()))
    monkeypatch.setattr(mcp_compat, "run_server", lambda mcp, **kw: None)
    for which in ("model", "report", "service"):
        assert cli.main([which]) == 0
    assert seen == ["model", "report", "service"]


def test_non_loopback_http_bind_warns(fake_run, capsys):
    cli.main(["report", "--transport", "sse", "--host", "0.0.0.0"])
    err = capsys.readouterr().err
    assert "warning" in err and "0.0.0.0:8000" in err and "no authentication" in err
    assert fake_run.calls[0][1]["host"] == "0.0.0.0"          # still honoured


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_http_bind_is_silent(fake_run, capsys, host):
    cli.main(["report", "--transport", "streamable-http", "--host", host])
    assert capsys.readouterr().err == ""


def test_stdio_ignores_host_for_the_warning(fake_run, capsys):
    cli.main(["report", "--host", "0.0.0.0"])           # stdio: nothing is exposed
    assert capsys.readouterr().err == ""


def test_keyboard_interrupt_returns_130(monkeypatch):
    monkeypatch.setattr(cli, "load_server", lambda which: SimpleNamespace(mcp=object()))

    def interrupted(mcp, **kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(mcp_compat, "run_server", interrupted)
    assert cli.main(["report"]) == 130


# --- --project / PBI_MCP_PROJECT ----------------------------------------------

@pytest.fixture
def project_module(monkeypatch):
    seen: list[tuple[object, str]] = []
    module = SimpleNamespace(mcp=object(), STATE="STATE-OBJECT",
                             set_project=lambda state, path: seen.append((state, path)))
    monkeypatch.setattr(cli, "load_server", lambda which: module)
    monkeypatch.setattr(mcp_compat, "run_server", lambda mcp, **kw: None)
    monkeypatch.delenv(cli.PROJECT_ENV, raising=False)
    return seen


def test_project_flag_preselects(project_module):
    cli.main(["report", "--project", "C:/x/Demo.pbip"])
    assert project_module == [("STATE-OBJECT", "C:/x/Demo.pbip")]


def test_project_env_var_preselects_and_flag_wins(project_module, monkeypatch):
    monkeypatch.setenv(cli.PROJECT_ENV, "D:/env/Demo.pbip")
    cli.main(["report"])
    cli.main(["report", "--project", "E:/flag/Demo.pbip"])
    assert [p for _, p in project_module] == ["D:/env/Demo.pbip", "E:/flag/Demo.pbip"]


@pytest.mark.parametrize("value", ["", "${user_config.project_path}"])
def test_unset_or_unsubstituted_project_is_ignored(project_module, monkeypatch, value):
    # An optional .mcpb user_config left blank arrives empty or as the raw token.
    monkeypatch.setenv(cli.PROJECT_ENV, value)
    cli.main(["report"])
    assert project_module == []


def test_bad_project_warns_but_still_serves(monkeypatch, capsys):
    ran = []

    def boom(state, path):
        raise ValueError("no such project")

    monkeypatch.setattr(cli, "load_server", lambda which: SimpleNamespace(
        mcp="M", STATE="S", set_project=boom))
    monkeypatch.setattr(mcp_compat, "run_server", lambda mcp, **kw: ran.append(mcp))
    monkeypatch.delenv(cli.PROJECT_ENV, raising=False)
    assert cli.main(["report", "--project", "nowhere.pbip"]) == 0
    assert ran == ["M"]
    assert "could not preselect project" in capsys.readouterr().err


@pytest.mark.parametrize("which", ["model", "report"])
def test_project_preselect_on_the_real_servers(which, tmp_path):
    """--project reaches each real server's own set_project + STATE."""
    shutil.copytree(SYNTH_DIR, tmp_path / "s")
    mod = cli.load_server(which)
    before = (mod.STATE.project, mod.STATE.greeted)
    try:
        cli._preselect_project(mod, str(tmp_path / "s" / "Synthetic.pbip"))
        assert mod.STATE.project is not None
        assert Path(mod.STATE.project.path).exists()
    finally:
        mod.STATE.project, mod.STATE.greeted = before


# --- run_server: argument mapping with fake servers ---------------------------

class FakeMcp:
    """Records run() calls; `settings` exists like FastMCP's (mcp 1.x)."""

    def __init__(self, with_settings: bool = True):
        if with_settings:
            self.settings = SimpleNamespace(host="127.0.0.1", port=8000)
        self.calls: list[tuple[tuple, dict]] = []

    def run(self, *args, **kwargs):
        self.calls.append((args, kwargs))


@pytest.mark.parametrize("transport", ["streamable-http", "sse"])
def test_run_server_mcp1_sets_settings_then_runs(monkeypatch, transport):
    monkeypatch.setattr(mcp_compat, "MCP_MAJOR", 1)
    fake = FakeMcp()
    mcp_compat.run_server(fake, transport, "0.0.0.0", 9001)
    assert (fake.settings.host, fake.settings.port) == ("0.0.0.0", 9001)
    # mcp 1.x run() takes only the transport; host/port live in settings.
    assert fake.calls == [((), {"transport": transport})]


def test_run_server_mcp1_none_keeps_sdk_defaults(monkeypatch):
    monkeypatch.setattr(mcp_compat, "MCP_MAJOR", 1)
    fake = FakeMcp()
    mcp_compat.run_server(fake, "streamable-http", None, None)
    assert (fake.settings.host, fake.settings.port) == ("127.0.0.1", 8000)
    mcp_compat.run_server(fake, "streamable-http", port=7000)
    assert (fake.settings.host, fake.settings.port) == ("127.0.0.1", 7000)


def test_run_server_mcp1_without_settings_does_not_crash(monkeypatch):
    monkeypatch.setattr(mcp_compat, "MCP_MAJOR", 1)
    fake = FakeMcp(with_settings=False)
    mcp_compat.run_server(fake, "sse", "127.0.0.1", 8001)
    assert fake.calls == [((), {"transport": "sse"})]


@pytest.mark.parametrize("transport", ["streamable-http", "sse"])
def test_run_server_mcp2_forwards_host_and_port_as_kwargs(monkeypatch, transport):
    monkeypatch.setattr(mcp_compat, "MCP_MAJOR", 2)
    fake = FakeMcp(with_settings=False)
    mcp_compat.run_server(fake, transport, "0.0.0.0", 9001)
    assert fake.calls == [((), {"transport": transport, "host": "0.0.0.0", "port": 9001})]


def test_run_server_mcp2_omits_unset_values(monkeypatch):
    monkeypatch.setattr(mcp_compat, "MCP_MAJOR", 2)
    fake = FakeMcp(with_settings=False)
    mcp_compat.run_server(fake, "streamable-http")
    mcp_compat.run_server(fake, "streamable-http", port=7000)
    assert fake.calls == [((), {"transport": "streamable-http"}),
                          ((), {"transport": "streamable-http", "port": 7000})]


@pytest.mark.parametrize("major", [1, 2])
def test_run_server_stdio_ignores_host_and_port(monkeypatch, major):
    monkeypatch.setattr(mcp_compat, "MCP_MAJOR", major)
    fake = FakeMcp()
    mcp_compat.run_server(fake, "stdio", "0.0.0.0", 9999)
    assert fake.calls == [((), {"transport": "stdio"})]
    assert (fake.settings.host, fake.settings.port) == ("127.0.0.1", 8000)


def test_run_server_default_transport_is_stdio(monkeypatch):
    monkeypatch.setattr(mcp_compat, "MCP_MAJOR", 1)
    fake = FakeMcp()
    mcp_compat.run_server(fake)
    assert fake.calls == [((), {"transport": "stdio"})]


# --- run_server on the installed SDK: really open a port ----------------------

def _free_port(family=socket.AF_INET, addr="127.0.0.1") -> int:
    with socket.socket(family) as s:
        s.bind((addr, 0))
        return s.getsockname()[1]


def _wait_for_port(proc, host: str, port: int, timeout: float = 60.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline and proc.poll() is None:
        try:
            socket.create_connection((host, port), timeout=0.5).close()
            return True
        except OSError:
            time.sleep(0.25)
    return False


def _serve(args: list[str], host: str, port: int):
    proc = subprocess.Popen([sys.executable, "-m", "core.cli", *args], cwd=REPO,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    return proc, _wait_for_port(proc, host, port)


def _stop(proc) -> str:
    proc.kill()
    try:
        return proc.communicate(timeout=20)[1] or ""
    except subprocess.TimeoutExpired:      # pragma: no cover
        return ""


@pytest.mark.parametrize("transport", ["streamable-http", "sse"])
def test_http_transports_open_the_requested_port(transport):
    """End to end on whichever mcp major is installed: the port opens."""
    port = _free_port()
    proc, opened = _serve(["model", "--transport", transport, "--port", str(port)],
                          "127.0.0.1", port)
    try:
        assert opened, f"{transport} never opened port {port}:\n{_stop(proc)}"
        if transport == "streamable-http":
            body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                               "params": {"protocolVersion": "2025-03-26",
                                          "capabilities": {},
                                          "clientInfo": {"name": "pytest", "version": "0"}}})
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
            conn.request("POST", "/mcp", body=body, headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream"})
            resp = conn.getresponse()
            data = resp.read().decode("utf-8", "replace")
            assert resp.status == 200, data
            assert "pbi-model" in data and "serverInfo" in data
    finally:
        _stop(proc)


def _ipv6_loopback_available() -> bool:
    try:
        with socket.socket(socket.AF_INET6) as s:
            s.bind(("::1", 0))
        return True
    except OSError:
        return False


@pytest.mark.skipif(not _ipv6_loopback_available(), reason="no IPv6 loopback")
def test_host_option_reaches_the_server():
    """Bind ::1 only: reachable there, not on 127.0.0.1 -> --host is honoured."""
    port = _free_port(socket.AF_INET6, "::1")
    proc, opened = _serve(["model", "--transport", "streamable-http",
                           "--host", "::1", "--port", str(port)], "::1", port)
    try:
        assert opened, f"never opened [::1]:{port}:\n{_stop(proc)}"
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
    finally:
        _stop(proc)
