# Contributing to pbi-mcp

Thanks for helping. This project edits files that Power BI Desktop has to open
again afterwards, so one rule outranks the rest: **never produce a project
Desktop refuses to open.** Every guideline below serves it.

By contributing you agree that your work is released under the [MIT license](LICENSE).
Please read [SECURITY.md](SECURITY.md) before reporting a vulnerability, and never
commit real `.pbip` data, credentials or personal paths.

## Development setup

You need Python 3.11 or newer. The package supports both major versions of the
`mcp` SDK, and CI runs the suite against each, so develop with **two
environments**.

```bash
git clone https://github.com/rajdeepraoextras-dev/PBI-MCP-Server.git
cd PBI-MCP-Server

# 1) mcp 1.x (FastMCP)
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"      # macOS/Linux: .venv/bin/pip
.venv/Scripts/pip install "mcp<2"

# 2) mcp 2.x (MCPServer)
python -m venv .venv-mcp2
.venv-mcp2/Scripts/pip install "mcp>=2" "pydantic>=2.6" "jsonschema>=4.20" "pytest>=8"
.venv-mcp2/Scripts/pip install -e . --no-deps
echo ".venv-mcp2/" >> .git/info/exclude     # keep the second env out of git status
```

Run the suite in both before every push:

```bash
.venv/Scripts/python -m pytest -q
.venv-mcp2/Scripts/python -m pytest -q
```

Expect a fully green run with a handful of skips: those are the real-fixture
tests (see below) and skip cleanly when the fixtures are absent.

## Repository layout

```
core/            PbipProject, TMDL/PBIR read-write, specs, lineage, usage,
                 safe I/O, undo journal, tool registration, mcp compat, CLI
model_server/    MCP server "pbi-model"   (semantic model / TMDL)
report_server/   MCP server "pbi-report"  (report / PBIR)
resources/       Vendored Microsoft Fabric JSON schemas (offline validation)
skills/          Skills bundled into the plugin
scripts/         Packaging (package.py, build_standalone.py), demos, smoke tests
tests/           Fixtures (synthetic committed, real local-only), goldens, suites
```

## Tests, fixtures and goldens

- `tests/fixtures/synthetic/` is a small generated project and is committed.
- Any real Desktop export dropped under `tests/fixtures/real/` is discovered
  automatically and **never committed** (it is git-ignored). Those tests skip
  when it is missing, which is why CI shows skips.
- `tests/goldens/*.json` are snapshots of emitted `visual.json`. Regenerate them
  deliberately, then review the diff before committing:
  `PBI_MCP_REGEN_GOLDENS=1 python -m pytest tests/test_goldens.py`.
- Write tests must assert what the safety model promises: untouched files stay
  binary-identical, line endings and BOM are preserved, and the project still
  reloads afterwards.
- Tests must pass on mcp 1.x **and** 2.x. Result and annotation field names
  differ between the majors (`isError` vs `is_error`, `readOnlyHint` vs
  `read_only_hint`); `tests/test_tooling.py` has helpers that read both.

## Adding a tool module

Both servers call `load_tool_modules(package, mcp, STATE, tool)` (from
`core/tooling.py`) after their built-in tools. It imports every module in the
package whose name starts with `tools_` (in sorted order) and calls its
`register` function, so a new family of tools is one new file and **no edit to
`server.py`**.

```python
# report_server/tools_example.py
from __future__ import annotations


def register(mcp, state, tool):
    @tool(read=True, idempotent=True)
    def pbi_example_pages() -> list[dict]:
        """One-line summary; this docstring becomes the tool description."""
        project = state.require()
        return [p.model_dump() for p in project.list_pages()]

    @tool(write=True)
    def pbi_example_rename(page_id: str, name: str) -> dict:
        """Rename a page."""
        return state.require().rename_page(page_id, name)
```

The contract, exactly:

- **`register(mcp, state, tool)`** is the only entry point. A `tools_*.py`
  module without a `register` attribute is imported but adds nothing.
- **`mcp`** is the server object (`core.mcp_compat.Server`: `FastMCP` on mcp 1.x,
  `MCPServer` on 2.x). Do not call `mcp.tool()` yourself and only use surface that
  is identical in both majors.
- **`state`** is that server's state object (`ModelState` in `model_server`,
  `ReportState` in `report_server`). `state.project` is the selected
  `PbipProject` or `None`; `state.require()` returns it or raises "No project
  set - call pbi_set_project(path) first."
- **`tool`** is the decorator factory from `make_tool(mcp, state)`:
  `tool(*, read=False, write=False, destructive=False, idempotent=False,
  journaled=True, name=None)`.
  - Exactly one of `read=True` or `write=True` is required (otherwise
    `ValueError`).
  - It attaches the MCP annotations (`readOnlyHint`, `destructiveHint`,
    `idempotentHint`); `destructive` only has effect on write tools.
  - Journaled write tools (the default) get a keyword-only `dry_run: bool = False`
    parameter injected, and real calls are recorded in the undo journal so
    `pbi_undo` can revert them. If your function already declares `dry_run`, it
    owns its preview semantics and only non-dry calls are journaled.
    `journaled=False` is for tools that must not be journaled themselves (the
    undo and transaction tools); use it sparingly.
  - `name=` overrides the tool name, which otherwise is the function name.
- Tool names start with `pbi_` and must be unique within a server.
- Journaled tools have their signature evaluated with
  `inspect.signature(fn, eval_str=True)`, so every type used in an annotation
  must be importable at **module level** of your file, not inside `register`.
- Return JSON-serialisable values (`dict`, `list`, or `model_dump()` output).
- Keep write logic in core (`PbipProject` methods or `core/` helpers) and the
  tool body thin; that is what the unit tests call directly.
- Add tests, and add the tool to the README tool tables and count.

The standalone build passes `--collect-submodules` for each server package, and
the `.plugin` / `.mcpb` packagers include every `*.py`, so new modules ship in
every distribution without further changes.

## Write rules for TMDL and PBIR

These come from the safety model in the README; changes that weaken them will
not be merged.

- **All writes go through `core/io_safe.py`**: `atomic_write` (temp file plus
  rename, so a crash never leaves a half file) and `backup` (a
  `*.bak-<timestamp>` snapshot before a file's first mutation).
- **TMDL edits are surgical text edits.** Never re-emit the parsed model:
  partitions, annotations, lineage tags and M source must survive
  byte-for-byte.
- **Preserve each file's style.** Desktop mixes CRLF (TMDL, `report.json`) and
  LF (`page.json`, `visual.json`); detect with `io_safe.detect_style`, keep the
  existing line endings and BOM, and default new files to LF without BOM.
- **Validate PBIR before writing.** Emitted JSON is checked against the vendored
  Fabric schemas (`core/schema_validate.py`). Bucket names and shapes come from
  real Desktop exports (`core/visual_specs.py`); do not invent them.
- **Guard destructive operations.** Deleting something other things depend on
  is refused unless `force=true` (lineage plus report usage). Removed visuals
  move to `Report/.pbi/mcp-trash/` so they are recoverable.
- **Stay inside the project.** The dry-run and undo machinery snapshots the
  project folder (`core/journal.py`), so a write that touches files elsewhere
  cannot be previewed or reverted. No network access from `model` or `report`.
- **Reopen in Power BI Desktop** after changing any writer. That check is a
  manual release gate; the automated suite covers everything up to it.

## Code conventions

- Python 3.11+, type hints, `from __future__ import annotations`.
- Source files are **LF, UTF-8, no BOM.** `.gitattributes` disables line-ending
  normalisation (`* -text`) because fixtures deliberately mix styles, so make sure
  your editor does not convert source files to CRLF.
- Keep changes focused; update the README and `CHANGELOG.md` (`[Unreleased]`)
  in the same pull request. The PR template lists the checks.

## How the distributions are built

| Artifact | Built by | Notes |
|---|---|---|
| `pbi-mcp` on PyPI (sdist, wheel) | `python -m build` (`publish.yml`) | `pbi-mcp` command from `core/cli.py` |
| `pbi-mcp.plugin` | `scripts/package.py` | source bundle, needs host Python |
| `pbi-mcp-<server>.mcpb` | `scripts/package.py` | Claude Desktop extension per server |
| `pbi-mcp-standalone-<platform>.plugin` | `scripts/build_standalone.py` | PyInstaller, one per OS (`release.yml`) |
| `claude_desktop_config.snippet.json` | `scripts/package.py` | `--portable` writes `uvx` entries |

Build the bundles locally with `python scripts/package.py` (output in `dist/`;
add `--dist DIR` to write elsewhere). The standalone build needs
`pip install -e ".[build]"`. Validate a `.mcpb` manifest with
`npx --yes @anthropic-ai/mcpb validate path/to/manifest.json`.

## Releasing

Releases are cut by pushing a tag; the workflows do the rest.

1. **Update `CHANGELOG.md`.** Move the `[Unreleased]` entries under a new
   `## [X.Y.Z] - YYYY-MM-DD` heading, leave an empty `[Unreleased]` above it,
   and fix the compare links at the bottom. The GitHub Release body is this
   section.
2. **Bump the version** in every place it lives:
   - `pyproject.toml` (`[project].version`)
   - `scripts/package.py` (`VERSION`)
   - `scripts/build_standalone.py` (`VERSION`)
   - `server.json` (`version` and each `packages[].version`)

   `tests/test_package_manifests.py::test_versions_in_sync` fails when they drift.
3. Merge to `main` with CI green, then tag and push:
   `git tag vX.Y.Z && git push origin vX.Y.Z`.
4. Two workflows start from the tag:
   - **`publish.yml`** builds the sdist and wheel, smoke-tests the wheel in a
     clean environment and uploads to PyPI.
   - **`release.yml`** builds the standalone executables (Windows, macOS,
     Linux), the source `.plugin`, the `.mcpb` bundles and a portable config
     snippet, then creates or updates the GitHub Release with all of them, a
     `SHA256SUMS.txt`, and the changelog section as the notes.
5. Once the PyPI upload has finished, publish to the MCP Registry (below).

Both workflows check that the tag matches `pyproject.toml`; a mismatch fails the
run before anything is uploaded. Tags containing a hyphen (`v2.1.0-rc1`) are
marked as pre-releases on GitHub.

### One-time setup

**PyPI trusted publisher** (no API token or secret is ever stored):

1. On <https://pypi.org/manage/account/publishing/> add a *pending publisher*
   (the project does not exist yet): project name `pbi-mcp`, owner
   `rajdeepraoextras-dev`, repository `PBI-MCP-Server`, workflow `publish.yml`,
   environment `pypi`.
2. After the first successful upload it becomes a normal publisher of the
   project and can be managed under the project's *Publishing* settings.

**GitHub environment `pypi`:** repository *Settings > Environments > New
environment*, named exactly `pypi`. Optionally add yourself as a required
reviewer so every upload waits for an approval click, and restrict deployments
to `v*` tags.

**Optional:** enable *Settings > Code security > Private vulnerability
reporting* so [SECURITY.md](SECURITY.md) works as written.

## Publishing to the MCP Registry

`server.json` at the repository root describes the server for the
[official MCP Registry](https://registry.modelcontextprotocol.io). It lists the
PyPI package `pbi-mcp`, launched with `uvx`, once for `report` and once for
`model`.

The registry verifies that you own the PyPI package by looking for
`mcp-name: io.github.rajdeepraoextras-dev/pbi-mcp` in the package README. That
line is in `README.md` (as an HTML comment); do not remove it.

Publish after the release is on PyPI:

```bash
# install mcp-publisher (macOS/Linux: brew install mcp-publisher, or download a
# release binary from https://github.com/modelcontextprotocol/registry/releases)
mcp-publisher login github      # device flow; sign in as rajdeepraoextras-dev
mcp-publisher validate          # optional: checks server.json without publishing
mcp-publisher publish           # reads ./server.json
```

Check the listing with
`curl "https://registry.modelcontextprotocol.io/v0.1/servers?search=io.github.rajdeepraoextras-dev/pbi-mcp"`.
The `io.github.rajdeepraoextras-dev/` prefix is what GitHub login authorises, so
the name in `server.json` must not change. For each new version, bump
`server.json` first (step 2 above) and republish.

## Listing on Smithery and Glama

**Smithery** hosts local servers from an `.mcpb` bundle. Take
`pbi-mcp-report.mcpb` (and `pbi-mcp-model.mcpb`) from the GitHub Release, then
either upload it at <https://smithery.ai/new> or publish from the Smithery CLI
(after authenticating it as described in their docs):

```bash
smithery mcp publish ./pbi-mcp-report.mcpb -n rajdeepraoextras-dev/pbi-mcp-report
```

**Glama** indexes servers from GitHub. Open <https://glama.ai/mcp/servers>,
choose *Add Server* and submit the repository URL. To claim it as maintainer,
add a `glama.json` at the repository root:

```json
{
  "$schema": "https://glama.ai/mcp/schemas/server.json",
  "maintainers": ["rajdeepraoextras-dev"]
}
```

Both directories change their submission flows now and then; if a step above no
longer matches, follow the current instructions on their sites.
