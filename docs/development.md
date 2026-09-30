# Development

How the code is organised, how to add a tool, how to test on both mcp SDK
majors, and how the docs site is built and deployed.

## Repository layout

```text
pbi-mcp/
  core/            # PbipProject + TMDL/PBIR read-write, specs, lineage, usage
                   # classifier, formatting/filter builders, safe I/O, undo
                   # journal, dry-run, doctor
  model_server/    # MCP server pbi-model: server.py + tools_*.py modules
  report_server/   # MCP server pbi-report: server.py + tools_*.py modules
  resources/       # vendored Fabric JSON schemas (offline validation)
  skills/          # bundled assistant skills (build, design, model, audit, ...)
  scripts/         # packagers, demos, docs and README generators
  tests/           # pytest suite; fixtures/ (synthetic committed, real ignored)
  docs/            # this site (mkdocs-material); mkdocs.yml at the root
```

Everything the servers do goes through `core.pbip.PbipProject`, which you can
also import from Python. The server modules are thin wrappers.

## The tool module contract

Both servers load every `tools_*.py` module in their package at start-up
(`core.tooling.load_tool_modules`), in name order, and call its
`register(mcp, state, tool)`. To add tools you create a module; **you do not edit
`model_server/server.py` or `report_server/server.py`.**

```python
# report_server/tools_example.py
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # never import report_server.server at import time
    from report_server.server import ReportState


def count_pages(state: ReportState) -> dict:
    """Plain logic function: explicit state first, JSON-serialisable result."""
    return {"pages": len(state.require().list_pages())}


def rename_all(state: ReportState, prefix: str) -> dict:
    project = state.require()
    for page in project.list_pages():
        project.rename_page(page.id, f"{prefix} {page.name}")
    return {"ok": True}


def register(mcp, state, tool) -> None:
    @tool(read=True, idempotent=True)
    def pbi_count_pages() -> dict:
        """Count the report's pages."""
        return count_pages(state)

    @tool(write=True, idempotent=False)
    def pbi_prefix_page_names(prefix: str) -> dict:
        """Prefix every page's display name with `prefix`."""
        return rename_all(state, prefix)
```

The rules, enforced by `core/tooling.py` and the test suite:

- **Logic first.** Put the behaviour in plain functions that take an explicit
  `state` and call `state.require()` (which returns the selected
  `PbipProject`). They are unit-testable without an MCP client. The `pbi_*`
  wrappers inside `register` stay thin.
- **`@tool(...)` picks the contract.** Exactly one of `read=True` or
  `write=True`. Write tools also take `destructive=` and `idempotent=`; these
  become the MCP annotation hints hosts use to auto-approve reads and gate
  deletes. `journaled=False` is for tools that manage the journal themselves,
  and `name=` overrides the function name.
- **Write tools get `dry_run` and undo for free.** A `dry_run: bool = False`
  parameter is injected and the docstring gains a note about it. `dry_run=true`
  runs the tool on a scratch copy and returns the diff; real calls are
  journaled so `pbi_undo` can revert them. If your function already declares
  its own `dry_run` parameter, it keeps its own preview and only real calls
  are journaled. Read tools get neither.
- **Never import the server modules at import time.** `server.py` imports your
  module while it is still loading, so a top-level import would be circular.
  Use `from __future__ import annotations` and an `if TYPE_CHECKING:` import for
  type hints.
- **Names** start with `pbi_` and must be unique across the whole project: a
  host loads both servers into one session.
- **Docstrings are what the model reads.** Say what the tool does, what each
  argument means, and the pitfalls. Return JSON-serialisable values.
- **Resources and prompts** are registered in the same `register` function with
  `mcp.resource(uri, name=..., description=..., mime_type=...)` and
  `mcp.prompt()`. Stick to those keyword arguments: they mean the same on mcp
  1.x and 2.x. See `model_server/tools_resources.py` for a template with a
  `{table}` parameter.
- Keep source files LF, UTF-8, without a BOM.

## Testing

```bash
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
.venv\Scripts\python -m pytest
```

The suite runs against a synthetic PBIP project (committed under
`tests/fixtures/synthetic`) and any real Desktop exports you drop under
`tests/fixtures/real/` (auto-discovered, never committed). Tests that need a
real export skip cleanly when it is absent, so a fresh checkout reports some
skips; that is expected.

Other kinds of test in the suite:

- **Goldens.** `tests/goldens/*.json` snapshot the exact `visual.json` emitted
  for each visual type. If an emitter change is intentional, regenerate with
  `PBI_MCP_REGEN_GOLDENS=1` (`set PBI_MCP_REGEN_GOLDENS=1` on Windows) and
  review the diff.
- **Round-trip and loss-free tests** assert untouched files stay
  binary-identical after a write.
- **A seeded fuzz storm** runs random operations and asserts the project always
  reloads.
- **End-to-end client tests** spawn a server over stdio and drive it with a
  real MCP client session.

### Test on both mcp SDK majors

`pyproject.toml` allows `mcp>=1.2.0,<3`, and `core/mcp_compat.py` picks
`FastMCP` (1.x) or `MCPServer` (2.x) at import time. `pip install -e ".[dev]"`
resolves to the newest mcp, so build one environment per major explicitly and
run the whole suite in **both** before committing:

```bash
# mcp 1.x
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
.venv\Scripts\pip install "mcp>=1.2,<2"
.venv\Scripts\python -m pytest

# mcp 2.x
python -m venv .venv-mcp2
.venv-mcp2\Scripts\pip install "mcp>=2" "pydantic>=2.6" "jsonschema>=4.20" "pytest>=8"
.venv-mcp2\Scripts\pip install -e . --no-deps
.venv-mcp2\Scripts\python -m pytest
```

Both directories are git-ignored (add `.venv-mcp2/` to `.git/info/exclude`). When
your tests call tools or inspect annotations, remember the majors differ:
`call_tool` returns a `(content, structured)` tuple on 1.x and a `CallToolResult`
on 2.x, and annotation hints are camelCase (`readOnlyHint`) on 1.x but
snake_case (`read_only_hint`) on 2.x. `tests/test_tooling.py` has helpers that
read both.

### Generated files that must stay current

Two files are derived from the live servers, and tests fail until they are
regenerated after you add, remove or rename a tool:

```bash
python scripts/gen_tool_reference.py     # docs/tools/model.md and report.md
python scripts/update_readme_counts.py   # the tool and test counts in README.md
```

`gen_tool_reference.py --check` and `update_readme_counts.py --check` report
staleness without writing. Do not edit `docs/tools/*.md` by hand.

## Building the docs

The docs are [mkdocs-material](https://squidfunk.github.io/mkdocs-material/).
Install the optional `docs` extra and preview locally:

```bash
pip install -e ".[docs]"
mkdocs serve                  # live preview at http://127.0.0.1:8000
mkdocs build --strict         # what CI runs; writes build/site (git-ignored)
```

`--strict` turns warnings into errors, so a broken link, a broken anchor or a
page missing from the nav fails the build. To add a page, create the Markdown
file under `docs/` and list it under `nav:` in `mkdocs.yml`. Do not hard-code
tool or test totals in prose; the generated pages and the README carry them.

## Deploying the docs

`.github/workflows/docs.yml` builds the site on every pull request (a strict
build plus a check that the tool reference is current) and, on every push to
`main`, regenerates the reference and publishes with:

```bash
mkdocs gh-deploy --force
```

That commits the built site to a `gh-pages` branch. The job asks for
`contents: write` permission, so it needs no personal access token. The site is
served from `https://rajdeepraoextras-dev.github.io/PBI-MCP-Server/`.

!!! important "One-time GitHub Pages setting"
    Enable Pages once, after the first successful run of the workflow has
    created the `gh-pages` branch:

    1. Repository **Settings > Pages**.
    2. Under *Build and deployment*, set **Source** to **Deploy from a branch**.
    3. Choose branch **gh-pages** and folder **/ (root)**, then save.

    If deployment fails with a permissions error, open **Settings > Actions >
    General** and set *Workflow permissions* to **Read and write permissions**
    (an organisation policy can otherwise cap the job's `contents: write`).

Prefer GitHub's artifact-based Pages flow? Replace the deploy job with
`actions/upload-pages-artifact` plus `actions/deploy-pages`, grant the job
`pages: write` and `id-token: write`, and set the Pages source to *GitHub
Actions* instead. Use one flow or the other, not both.

## Releasing

1. Bump the version in `pyproject.toml` and in the `VERSION` constants of
   `scripts/package.py` and `scripts/build_standalone.py`, and add the notes to
   `RELEASE.md`.
2. Run the suite in both mcp environments, then regenerate the docs reference
   and README counts (above).
3. `python scripts/package.py` builds `dist/pbi-mcp.plugin` and the Claude
   Desktop config snippet. `python scripts/build_standalone.py` (needs
   `pip install -e ".[build]"`) freezes the standalone executable bundle for the
   machine it runs on.
4. Attach the bundles to a GitHub Release. The standalone build is about 20 MB
   and is published only as a release asset.
5. The final gate stays manual: open a demo output in Power BI Desktop and
   confirm there is no repair prompt.
