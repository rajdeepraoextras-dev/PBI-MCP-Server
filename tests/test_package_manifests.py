"""Packaging: .mcpb / .plugin bundles, manifests, versions, server.json, workflows.

scripts/package.py is loaded by path (``scripts/`` is not a package) and
``build()`` writes into a pytest tmp dir, never into the repo's ``dist/``.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO = Path(__file__).resolve().parent.parent
SYNTH_DIR = REPO / "tests" / "fixtures" / "synthetic"


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


package = _load("pbi_scripts_package", "scripts/package.py")
standalone = _load("pbi_scripts_build_standalone", "scripts/build_standalone.py")
changelog = _load("pbi_scripts_changelog_section", "scripts/changelog_section.py")

SERVERS = package.available_servers()
PYPROJECT = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
VERSION = PYPROJECT["project"]["version"]

# Fields MANIFEST.md (manifest_version 0.3) requires.
REQUIRED = ("manifest_version", "name", "version", "description", "author", "server")


@pytest.fixture(scope="module")
def dist(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("dist")
    package.build(out)
    return out


def _names(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as zf:
        return zf.namelist()


def _manifest(dist: Path, which: str) -> dict:
    with zipfile.ZipFile(dist / f"pbi-mcp-{which}.mcpb") as zf:
        return json.loads(zf.read("manifest.json"))


# --- build output ------------------------------------------------------------

def test_at_least_model_and_report_are_packaged():
    assert {"model", "report"} <= set(SERVERS)


def test_build_writes_every_artifact(dist):
    expected = {"pbi-mcp.plugin", "claude_desktop_config.snippet.json",
                *(f"pbi-mcp-{s}.mcpb" for s in SERVERS)}
    assert expected <= {p.name for p in dist.iterdir()}


def test_build_returns_the_paths_it_wrote(tmp_path):
    paths = package.build(tmp_path)
    assert paths and all(p.exists() and p.parent == tmp_path for p in paths)


def test_service_bundle_only_when_the_package_exists():
    assert ("service" in SERVERS) == (REPO / "service_server").is_dir()


# --- .mcpb manifests -----------------------------------------------------------

@pytest.mark.parametrize("which", SERVERS)
def test_manifest_has_the_required_fields(dist, which):
    m = _manifest(dist, which)
    for key in REQUIRED:
        assert key in m, key
    assert m["manifest_version"] == "0.3"
    assert m["name"] == f"pbi-mcp-{which}"
    assert m["version"] == VERSION
    assert m["description"].strip()
    assert m["author"]["name"] == "Rajdeep Rao"
    assert m["author"]["url"].startswith("https://")


@pytest.mark.parametrize("which", SERVERS)
def test_manifest_descriptive_fields(dist, which):
    m = _manifest(dist, which)
    assert m["display_name"].strip() and m["long_description"].strip()
    assert m["license"] == "MIT"
    assert m["repository"]["type"] == "git" and m["repository"]["url"].startswith("https://github.com/")
    assert "power-bi" in m["keywords"] and all(isinstance(k, str) for k in m["keywords"])
    assert m["compatibility"]["runtimes"] == {"python": ">=3.11"}
    assert set(m["compatibility"]["platforms"]) == {"darwin", "win32", "linux"}


@pytest.mark.parametrize("which", SERVERS)
def test_manifest_launches_the_cli_for_that_server(dist, which):
    server = _manifest(dist, which)["server"]
    assert server["type"] == "python"
    assert server["entry_point"] == "core/cli.py"
    cfg = server["mcp_config"]
    assert cfg["command"] == "python"
    assert cfg["args"] == ["${__dirname}/core/cli.py", which]
    assert cfg["platform_overrides"]["darwin"]["command"] == "python3"
    assert cfg["platform_overrides"]["linux"]["command"] == "python3"


@pytest.mark.parametrize("which", SERVERS)
def test_user_config_is_defined_and_used_consistently(dist, which):
    m = _manifest(dist, which)
    uc = m["user_config"]
    assert uc["project_path"]["type"] == "directory"
    assert uc["project_path"]["title"] and uc["project_path"]["description"]
    assert uc["project_path"].get("required") is False
    # every ${user_config.X} in mcp_config must be a defined option
    used = set(re.findall(r"\$\{user_config\.(\w+)\}",
                          json.dumps(m["server"]["mcp_config"])))
    assert used and used <= set(uc)
    # ...and it is delivered under the name the CLI reads
    assert m["server"]["mcp_config"]["env"] == {"PBI_MCP_PROJECT": "${user_config.project_path}"}


@pytest.mark.parametrize("which", SERVERS)
def test_manifest_tool_list_is_well_formed(dist, which):
    m = _manifest(dist, which)
    tools = m.get("tools")
    if not tools:                       # server not importable at build time
        assert m["tools_generated"] is True
        return
    names = [t["name"] for t in tools]
    assert len(names) == len(set(names))
    assert all(n.startswith("pbi_") and t["description"] for n, t in zip(names, tools))
    assert "pbi_set_project" in names
    assert m["tools_generated"] is False


# --- .mcpb contents --------------------------------------------------------------

@pytest.mark.parametrize("which", SERVERS)
def test_zip_contains_the_entry_point_and_the_server(dist, which):
    names = set(_names(dist / f"pbi-mcp-{which}.mcpb"))
    pkg = package.SERVER_PACKAGES[which]
    manifest = _manifest(dist, which)
    assert manifest["server"]["entry_point"] in names
    for required in ("manifest.json", "core/cli.py", "core/__init__.py",
                     "core/mcp_compat.py", "core/tooling.py",
                     f"{pkg}/__init__.py", f"{pkg}/server.py",
                     "pyproject.toml", "README.md", "LICENSE", "requirements.txt"):
        assert required in names, required
    schemas = [n for n in names if n.startswith("resources/schemas/") and n.endswith(".json")]
    assert len(schemas) >= 10


@pytest.mark.parametrize("which", SERVERS)
def test_zip_has_every_source_file_and_nothing_else(dist, which):
    names = set(_names(dist / f"pbi-mcp-{which}.mcpb"))
    pkg = package.SERVER_PACKAGES[which]
    for tree in ("core", pkg):
        on_disk = {p.relative_to(REPO).as_posix() for p in (REPO / tree).rglob("*.py")
                   if "__pycache__" not in p.parts}
        assert on_disk <= names, sorted(on_disk - names)
    others = {v for k, v in package.SERVER_PACKAGES.items() if k != which}
    assert not [n for n in names if n.split("/")[0] in others]
    assert not [n for n in names if "__pycache__" in n or n.startswith(("tests/", ".venv", ".git"))]


@pytest.mark.parametrize("which", SERVERS)
def test_requirements_txt_matches_pyproject(dist, which):
    with zipfile.ZipFile(dist / f"pbi-mcp-{which}.mcpb") as zf:
        lines = zf.read("requirements.txt").decode("utf-8").splitlines()
        long_description = json.loads(zf.read("manifest.json"))["long_description"]
    assert lines == PYPROJECT["project"]["dependencies"]
    for requirement in lines:                      # the install hint names each one
        assert f'"{requirement}"' in long_description


def _run_bundle(entry: Path, which: str, project: Path, tool: str):
    async def go():
        params = StdioServerParameters(
            command=sys.executable, args=[str(entry), which],
            env={"PBI_MCP_PROJECT": str(project)}, cwd=str(entry.parent.parent))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                names = {t.name for t in (await session.list_tools()).tools}
                return names, await session.call_tool(tool, {})
    return asyncio.run(go())


@pytest.mark.parametrize("which,tool", [("model", "pbi_list_measures"),
                                        ("report", "pbi_list_pages")])
def test_extracted_bundle_starts_and_honours_the_default_project(dist, tmp_path, which, tool):
    """Unzip like a host would, launch the manifest's command, and call a tool
    that only works once the default-project setting reached the server."""
    bundle_dir = tmp_path / "bundle"
    with zipfile.ZipFile(dist / f"pbi-mcp-{which}.mcpb") as zf:
        zf.extractall(bundle_dir)
    shutil.copytree(SYNTH_DIR, tmp_path / "project")
    entry = bundle_dir / _manifest(dist, which)["server"]["entry_point"]

    names, result = _run_bundle(entry, which, tmp_path / "project" / "Synthetic.pbip", tool)

    assert {"pbi_set_project", tool, "pbi_undo"} <= names
    is_error = getattr(result, "is_error", None)
    if is_error is None:
        is_error = result.isError
    assert not is_error, result.content
    assert result.content


# --- .plugin ------------------------------------------------------------------------

def test_plugin_bundle_registers_every_server(dist):
    with zipfile.ZipFile(dist / "pbi-mcp.plugin") as zf:
        plugin = json.loads(zf.read(".claude-plugin/plugin.json"))
        mcp = json.loads(zf.read(".mcp.json"))
        names = set(zf.namelist())
    assert plugin["version"] == VERSION and plugin["license"] == "MIT"
    assert set(mcp["mcpServers"]) == {f"pbi-{s}" for s in SERVERS}
    for s in SERVERS:
        entry = mcp["mcpServers"][f"pbi-{s}"]
        assert entry["args"] == ["-m", f"{package.SERVER_PACKAGES[s]}.server"]
        assert f"{package.SERVER_PACKAGES[s]}/server.py" in names
    assert {"INSTALL.md", "core/cli.py", "pyproject.toml", "README.md", "LICENSE"} <= names
    assert any(n.startswith("skills/") for n in names)


# --- Claude Desktop snippet -----------------------------------------------------------

def test_portable_snippet_has_no_machine_paths():
    snippet = package.desktop_snippet(["model", "report"], portable=True)
    assert snippet == {"mcpServers": {
        "pbi-model": {"command": "uvx", "args": ["pbi-mcp", "model"]},
        "pbi-report": {"command": "uvx", "args": ["pbi-mcp", "report"]},
    }}


def test_local_snippet_points_at_this_checkout():
    snippet = package.desktop_snippet(["report"])
    entry = snippet["mcpServers"]["pbi-report"]
    assert entry["args"] == ["-m", "report_server.server"]
    assert Path(entry["cwd"]) == REPO and ".venv" in entry["command"]


def test_build_portable_writes_the_uvx_snippet(tmp_path):
    package.build(tmp_path, portable=True)
    data = json.loads((tmp_path / "claude_desktop_config.snippet.json").read_text("utf-8"))
    assert all(v["command"] == "uvx" for v in data["mcpServers"].values())


# --- standalone build -------------------------------------------------------------------

def test_standalone_bundles_the_same_servers():
    assert set(standalone.SERVERS) == set(SERVERS)
    cfg = standalone.mcp_config()["mcpServers"]
    assert set(cfg) == {f"pbi-{s}" for s in SERVERS}
    for key, entry in cfg.items():
        assert entry["args"] == [key.removeprefix("pbi-")]


def test_standalone_collects_every_server_package():
    src = (REPO / "scripts" / "build_standalone.py").read_text(encoding="utf-8")
    # tools_* modules are found with pkgutil at runtime, so each server
    # package must be collected wholesale or the frozen build loses tools.
    assert "--collect-submodules" in src and "SERVERS.values()" in src
    assert {"model_server", "report_server"} <= set(standalone.SERVERS.values())


# --- versions, metadata, changelog ---------------------------------------------------------

def test_versions_in_sync():
    server_json = json.loads((REPO / "server.json").read_text(encoding="utf-8"))
    assert package.VERSION == VERSION
    assert standalone.VERSION == VERSION
    assert server_json["version"] == VERSION
    assert all(p["version"] == VERSION for p in server_json["packages"])
    text = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    assert re.search(rf"^## \[{re.escape(VERSION)}\]", text, re.M), \
        f"CHANGELOG.md has no section for {VERSION}"


def test_pyproject_entry_points_and_urls():
    proj = PYPROJECT["project"]
    assert proj["scripts"]["pbi-mcp"] == "core.cli:main"
    assert proj["scripts"]["pbi-model-server"] == "model_server.server:main"
    assert proj["scripts"]["pbi-report-server"] == "report_server.server:main"
    assert {"Homepage", "Repository", "Issues", "Changelog"} <= set(proj["urls"])
    assert proj["keywords"]
    for minor in ("3.11", "3.12", "3.13", "3.14"):
        assert f"Programming Language :: Python :: {minor}" in proj["classifiers"]
    for target in proj["scripts"].values():
        mod, func = target.split(":")
        assert callable(getattr(importlib.import_module(mod), func))


def test_pyproject_ships_the_vendored_schemas():
    """A wheel once shipped without resources/schemas; keep the tripwire."""
    find = PYPROJECT["tool"]["setuptools"]["packages"]["find"]
    assert "resources.schemas" in find["include"] and find["namespaces"] is True
    assert "*.json" in PYPROJECT["tool"]["setuptools"]["package-data"]["resources.schemas"]
    assert list((REPO / "resources" / "schemas").glob("*.json"))


def test_changelog_section_extraction():
    text = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    body = changelog.section(text, VERSION)
    assert body and not body.startswith("## ") and "### " in body   # subsections, no version heading
    assert changelog.section(text, f"v{VERSION}") == body
    assert changelog.section(text, "0.0.0") is None
    oldest = changelog.section(text, "1.0.0")            # last section: link refs stripped
    assert oldest and not re.search(r"^\[[^\]]+\]: ", oldest, re.M)
    assert "## [" not in body                            # stops at the next heading


def test_changelog_section_boundaries():
    text = "# Changelog\n\n## [2.0.0] - 2026-01-01\n\n### Added\n- x\n\n## [1.0.0]\n- y\n"
    assert changelog.section(text, "2.0.0") == "### Added\n- x\n"
    assert changelog.section(text, "1.0.0") == "- y\n"


def test_changelog_section_cli_exit_codes():
    ok = subprocess.run([sys.executable, str(REPO / "scripts" / "changelog_section.py"), VERSION],
                        capture_output=True, text=True)
    assert ok.returncode == 0 and ok.stdout.strip()
    bad = subprocess.run([sys.executable, str(REPO / "scripts" / "changelog_section.py"), "0.0.0"],
                         capture_output=True, text=True)
    assert bad.returncode == 1 and "no '## [0.0.0]' section" in bad.stderr


# --- MCP Registry server.json --------------------------------------------------------------

def test_server_json_shape():
    from core import cli
    sj = json.loads((REPO / "server.json").read_text(encoding="utf-8"))
    assert sj["$schema"].startswith("https://static.modelcontextprotocol.io/schemas/")
    assert re.fullmatch(r"io\.github\.[A-Za-z0-9-]+/[A-Za-z0-9._-]+", sj["name"])
    assert 0 < len(sj["description"]) <= 100            # registry schema limit
    assert sj["repository"]["source"] == "github"
    assert sj["remotes"] == []
    launched = set()
    for p in sj["packages"]:
        assert p["registryType"] == "pypi" and p["identifier"] == PYPROJECT["project"]["name"]
        assert p["registryBaseUrl"] == "https://pypi.org"
        assert p["runtimeHint"] == "uvx" and p["transport"] == {"type": "stdio"}
        (arg,) = p["packageArguments"]
        assert arg["type"] == "positional" and arg["value"] in cli.SERVERS
        launched.add(arg["value"])
    assert launched == {"model", "report"}


def test_readme_carries_the_registry_ownership_marker():
    """The registry verifies a PyPI package by finding this token in its README."""
    name = json.loads((REPO / "server.json").read_text(encoding="utf-8"))["name"]
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert re.search(rf"mcp-name: {re.escape(name)}(\s|-->|$)", readme)


# --- workflows -----------------------------------------------------------------------------

def _wf(name: str) -> str:
    return (REPO / ".github" / "workflows" / name).read_text(encoding="utf-8")


def test_publish_uses_trusted_publishing_and_no_secrets():
    text = _wf("publish.yml")
    assert "id-token: write" in text and "name: pypi" in text
    assert "pypa/gh-action-pypi-publish" in text
    assert 'tags: ["v*"]' in text
    assert "secrets." not in text and "password:" not in text


def test_release_workflow_covers_the_matrix_and_the_release_step():
    text = _wf("release.yml")
    assert 'tags: ["v*"]' in text and "workflow_dispatch" in text
    for os_name in ("windows-latest", "macos-latest", "ubuntu-latest"):
        assert os_name in text
    for needle in ("scripts/build_standalone.py", "scripts/package.py",
                   "scripts/changelog_section.py", "softprops/action-gh-release",
                   "needs: [standalone, package]", "contents: write"):
        assert needle in text, needle
    assert "secrets." not in text.replace("secrets.GITHUB_TOKEN", "")


def test_ci_tests_both_mcp_majors_and_python_314():
    text = _wf("ci.yml")
    assert 'mcp: ["<2", ">=2"]' in text and 'pip install "mcp${{ matrix.mcp }}"' in text
    assert '"3.14"' in text and '"3.11"' in text
