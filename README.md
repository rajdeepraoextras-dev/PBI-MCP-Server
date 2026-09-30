# pbi-mcp

[![CI](https://github.com/rajdeepraoextras-dev/PBI-MCP-Server/actions/workflows/ci.yml/badge.svg)](https://github.com/rajdeepraoextras-dev/PBI-MCP-Server/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/rajdeepraoextras-dev/PBI-MCP-Server)](https://github.com/rajdeepraoextras-dev/PBI-MCP-Server/releases/latest)
[![PyPI](https://img.shields.io/pypi/v/pbi-mcp)](https://pypi.org/project/pbi-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/pbi-mcp)](https://pypi.org/project/pbi-mcp/)
[![License: MIT](https://img.shields.io/github/license/rajdeepraoextras-dev/PBI-MCP-Server)](https://github.com/rajdeepraoextras-dev/PBI-MCP-Server/blob/main/LICENSE)

<!-- mcp-name: io.github.rajdeepraoextras-dev/pbi-mcp -->

**MCP servers for local-file Power BI Project (`.pbip`) automation** — model
(TMDL) and report (PBIR) layers. The model and report servers use no Power BI
API, no auth and no cloud: the tools read and write the on-disk project files
Power BI Desktop itself uses. Two optional extras go further: a live engine
connection to a running Desktop, and a `pbi-service` server for the Fabric
REST APIs.

Build a correctly-bound, themed, filtered, multi-page report — or bulk-author
hundreds of measures — from a prompt, in minutes. **154 tools** (73 model +
81 report) across two MCP servers; every report write is pre-flight validated
against the official Fabric schemas.

**Fastest path:** `pbi_set_project(path)` → `pbi_scaffold_report()` profiles
the model and builds a themed, navigable, multi-page designed report in one
call (use `dry_run=true` to review the proposal first). Or compose it yourself
with `pbi_build_designed_page`, the design elements, and the theme generator.

> **Scope honesty.** This builds **structure, speed, consistency**. It ships
> Deneb (Vega-Lite) templates, but it does **not** source AppSource visuals or
> replace design taste. It's a fast report *builder*, not an autonomous report
> *designer*. Shapes the vendored schemas leave open (conditional-format icons,
> analytics lines, field parameters, KPIs) follow Desktop exports and the docs
> but have not all been reopened in Desktop; the reopen check stays a manual
> gate, and each such tool says what is unconfirmed.

## Safety model (the part that matters)

The one unforgivable bug for a tool like this is producing a project Desktop
refuses to open. Every mutation therefore goes through:

- **atomic writes** (temp file + rename — a crash never leaves a half file)
- **backup-once-per-file** snapshots (`*.bak-<timestamp>`), with
  `pbi_list_backups` / `pbi_restore_backup` to recover
- **surgical text edits** for TMDL — the parsed model is never re-emitted, so
  partitions, annotations, and M source are preserved byte-for-byte
- **style preservation** — each file keeps its exact line endings + BOM
  (Desktop mixes CRLF and LF across file types)
- **deletion fail-safes** — deleting a measure that other measures, visuals,
  or filters depend on is refused (transitive DAX lineage + report usage);
  `force=true` overrides, `dry_run=true` previews
- **recoverable visual deletes** — removed visuals move to
  `Report/.pbi/mcp-trash/`, which Desktop ignores
- **`dry_run=true` on write tools** — the call runs against a scratch copy and
  returns a unified diff; nothing is written
- **undo journal and transactions** — real writes keep byte-exact pre-images
  under `<project>/.pbi-mcp/undo/`; `pbi_undo` reverts them, and
  `pbi_begin_transaction` / `pbi_commit` / `pbi_rollback` group several writes
  (restore, cloud and render tools have no preview)
- **tool annotations** — every tool declares read-only / destructive /
  idempotent hints, so hosts can auto-approve reads and gate deletes

## Install

Pick one option. Whichever you choose, your MCP host launches the two servers
(`model` for the semantic model, `report` for the report layer), and every
session starts with `pbi_set_project(path)`.

### A. From PyPI: `uvx` or `pip` (recommended)

Needs Python 3.11+, or [uv](https://docs.astral.sh/uv/), which fetches Python
for you.

```bash
uvx pbi-mcp report                 # run once, nothing to install
pip install pbi-mcp                # or install; gives you the `pbi-mcp` command
pbi-mcp model                      # model | report | service, stdio by default
pbi-mcp report --transport streamable-http --port 8000   # HTTP on 127.0.0.1
```

`--project PATH` (or the `PBI_MCP_PROJECT` environment variable) preselects a
`.pbip` at startup. The HTTP transports have no authentication, so keep the
default `--host 127.0.0.1`.

**Claude Desktop**: Settings > Developer > Edit Config, add the following to
`claude_desktop_config.json`, then restart.

```json
{
  "mcpServers": {
    "pbi-model":  { "command": "uvx", "args": ["pbi-mcp", "model"] },
    "pbi-report": { "command": "uvx", "args": ["pbi-mcp", "report"] }
  }
}
```

**Claude Code**:

```bash
claude mcp add pbi-model -- uvx pbi-mcp model
claude mcp add pbi-report -- uvx pbi-mcp report
```

Add `--scope project` to write a shared `.mcp.json` in your repository instead;
its content is the same `mcpServers` JSON as above.

**Cursor**: put the same `mcpServers` JSON in `~/.cursor/mcp.json` (all
projects) or `.cursor/mcp.json` (one project).

**VS Code**: `.vscode/mcp.json` uses a `servers` key and an explicit `type`:

```json
{
  "servers": {
    "pbi-model":  { "type": "stdio", "command": "uvx", "args": ["pbi-mcp", "model"] },
    "pbi-report": { "type": "stdio", "command": "uvx", "args": ["pbi-mcp", "report"] }
  }
}
```

Installed with `pip` instead of `uvx`? Use `"command": "pbi-mcp"` with
`"args": ["report"]` (or `["model"]`) in any of the snippets above.

### B. Standalone `.plugin` bundle (no Python needed)

Download from the
[latest GitHub Release](https://github.com/rajdeepraoextras-dev/PBI-MCP-Server/releases/latest):

- `pbi-mcp-standalone-win32-amd64.plugin`,
  `pbi-mcp-standalone-darwin-arm64.plugin` or
  `pbi-mcp-standalone-linux-x86_64.plugin`: a self-contained build for that
  platform (Python runtime, dependencies and schemas inside); no local setup.
- `pbi-mcp.plugin`: the source plugin bundle; requires Python 3.11+ plus `mcp`
  (1.x or 2.x), `pydantic` and `jsonschema` on the host.

Drag the `.plugin` file into a plugin-aware MCP host. The standalone builds are
~25 MB, published only as release assets (not tracked in git), and listed with
checksums in `SHA256SUMS.txt`; rebuild one locally with
`scripts/build_standalone.py`.

### C. `.mcpb` for Claude Desktop

Each release also carries one MCP Bundle per server, `pbi-mcp-model.mcpb` and
`pbi-mcp-report.mcpb`. Open one in Claude Desktop (double-click, or Settings >
Extensions) and optionally choose a default project folder. These bundles use
the machine's Python, so it needs Python 3.11+ with
`pip install "mcp<3" pydantic jsonschema`; options A and B avoid that.

### D. From source

```bash
git clone https://github.com/rajdeepraoextras-dev/PBI-MCP-Server.git && cd PBI-MCP-Server
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"     # macOS/Linux: .venv/bin/pip
.venv\Scripts\python -m pytest            # green suite = good to go
.venv\Scripts\python -m core.cli report   # or: python -m report_server.server
```

Build the distributables yourself with `python scripts/package.py` (writes
`dist/pbi-mcp.plugin`, `dist/*.mcpb` and `dist/claude_desktop_config.snippet.json`,
whose paths point at your checkout). See
[CONTRIBUTING.md](https://github.com/rajdeepraoextras-dev/PBI-MCP-Server/blob/main/CONTRIBUTING.md)
to contribute and
[CHANGELOG.md](https://github.com/rajdeepraoextras-dev/PBI-MCP-Server/blob/main/CHANGELOG.md)
for what changed.

**Every session starts with `pbi_set_project(path)`** — point it at a `.pbip`
saved with Desktop's PBIP preview format (enable *Power BI Project (.pbip)
save option* + *PBIR enhanced metadata* in Options → Preview features).

## Tool reference

### `pbi-model` server (semantic model / TMDL)

| Tool | What it does |
|------|--------------|
| `pbi_set_project(path)` | Select the project; returns table/measure counts |
| `pbi_get_model()` | Tables, columns, measures, relationships snapshot |
| `pbi_list_measures(table?)` | Measures with DAX + format string |
| `pbi_model_lineage(measure?)` | DAX dependency graph; per-measure deps + direct/transitive dependents |
| `pbi_create_measure(table, name, dax, format?, display_folder?)` | Create (name must be unique model-wide) |
| `pbi_update_measure(table, name, dax?, format?, display_folder?)` | Partial update; omitted fields kept |
| `pbi_delete_measure(table, name, force?, dry_run?)` | Delete with lineage + report-usage guards |
| `pbi_create_column(table, name, data_type, summarize_by?, source_column?, dax?)` | Data column, or calculated column with `dax` |
| `pbi_create_relationship(from_table, from_column, to_table, to_column, ...)` | Endpoints validated; duplicates detected |
| `pbi_create_calc_group(name, precedence, items)` | Full calc-group table + model.tmdl registration |
| `pbi_bulk_create_measures(measures)` | Batch create; whole batch validated before any write |
| `pbi_list_backups()` / `pbi_restore_backup(backup)` | Recovery |

### `pbi-report` server (report / PBIR)

| Tool | What it does |
|------|--------------|
| `pbi_set_project(path)` | Select the project; returns page/visual counts |
| `pbi_list_pages()` | Pages: id, name, size, visual count, hidden |
| `pbi_list_visuals(page_id)` | Visuals with type, position, title, bindings |
| `pbi_get_visual(page_id, visual_id)` | Full config incl. raw visual.json |
| `pbi_model_usage()` | Classify every field **direct / indirect / unused** — the deletion fail-safe |
| `pbi_create_page(name, width?, height?)` | New page, registered in pages.json |
| `pbi_add_visual(page_id, visuals[])` | Batch add; buckets + model refs validated before any write |
| `pbi_build_page(name, visuals[])` | One call: page + visuals + auto-layout (KPI row, 2-col grid) |
| `pbi_update_bindings(page_id, visual_id, bindings)` | Rebind; formatting/position preserved |
| `pbi_move_visual(page_id, visual_id, x?, y?, width?, height?)` | Partial move/resize |
| `pbi_delete_visual(page_id, visual_id)` | Recoverable delete (→ .pbi/mcp-trash) |
| `pbi_format_visual(page_id, visual_id, target, objects)` | `container` (title/background/border) or `visual` (labels/legend/axes); plain values auto-encoded |
| `pbi_set_report_theme(theme)` | Install + activate a standard PBI theme JSON |
| `pbi_add_filter(scope, field, ...)` | Categorical / Advanced / TopN / RelativeDate at report, page, or visual scope |

### `pbi-report` — design, intelligence & lifecycle (v2)

| Tool | What it does |
|------|--------------|
| `pbi_capabilities()` | Machine-readable spec: visual types, buckets, filters, examples |
| `pbi_scaffold_report(...)` | **Autopilot**: profile the model → themed multi-page designed report (dry_run to review) |
| `pbi_profile_model()` | Classify fact/dimension/date tables, measure roles, grouping columns |
| `pbi_build_designed_page(...)` | Header band + KPI strip on backplates + chart grid, one call |
| `pbi_generate_theme(brand, mode)` | Brand color → coherent theme (palette, text classes, light/dark) |
| `pbi_add_text / pbi_add_image / pbi_add_shape` | Design elements: titles, logos, backplates, dividers |
| `pbi_style_page(page_id, ...)` | Canvas background + wallpaper |
| `pbi_group_visuals / pbi_add_visual_raw` | Group as one block; raw escape hatch for any visual |
| `pbi_sort_visual / pbi_add_nav_button` | Sort; page-navigation buttons |
| `pbi_set_page_role / pbi_set_visual_interactions` | Drillthrough/tooltip pages; cross-filter control |
| `pbi_create_bookmark` | Capture page + filter state |
| `pbi_rename/hide/reorder/delete/duplicate_page` | Page lifecycle (delete is recoverable) |
| `pbi_list_filters / pbi_remove_filter / pbi_list_trash / pbi_restore_visual` | Filter + trash management |
| `pbi_validate_project() / pbi_lint_page(id)` | Schema validation + design lint |
| `pbi_project_diff(other) / pbi_project_summary()` | Diff vs another project/backup; overview |

### More tool families (v2.1)

The full, generated reference for every tool is in `docs/tools/`
(`model.md`, `report.md`, `service.md`).

| Family | Tools |
|--------|-------|
| Safety net | `pbi_undo`, `pbi_undo_history`, `pbi_begin_transaction`, `pbi_commit`, `pbi_rollback`, `pbi_doctor` |
| Cascading rename | `pbi_rename_measure`, `pbi_rename_column`, `pbi_rename_table`, `pbi_find_references` |
| Tables and queries | `pbi_create_table`, `pbi_create_calculated_table`, `pbi_list_partitions`, `pbi_update_partition`, `pbi_delete_table`, `pbi_create_expression`, `pbi_list_expressions`, `pbi_update_expression`, `pbi_set_refresh_policy`, `pbi_remove_refresh_policy` |
| Columns, hierarchies, KPIs | `pbi_list_columns`, `pbi_update_column`, `pbi_update_table`, `pbi_delete_column`, `pbi_set_measure_properties`, `pbi_remove_kpi`, `pbi_create_hierarchy`, `pbi_list_hierarchies`, `pbi_delete_hierarchy` |
| Security and metadata | roles and OLS (`pbi_create_role`, `pbi_update_role`, `pbi_list_roles`, `pbi_delete_role`, `pbi_set_column_permission`, `pbi_set_table_permission_metadata`), perspectives (`pbi_create_perspective`, `pbi_update_perspective`, `pbi_list_perspectives`, `pbi_delete_perspective`), translations (`pbi_add_culture`, `pbi_set_translation`, `pbi_list_translations`, `pbi_delete_culture`), parameters (`pbi_create_field_parameter`, `pbi_list_field_parameters`, `pbi_create_whatif_parameter`, `pbi_list_whatif_parameters`) |
| Quality | `pbi_bpa`, `pbi_bpa_fix`, `pbi_bpa_rules`, `pbi_format_dax`, `pbi_format_measures`, `pbi_dax_references` |
| Live engine | `pbi_engine_status`, `pbi_evaluate_dax`, `pbi_validate_dax`, `pbi_table_row_counts`, `pbi_column_stats`, `pbi_preview_table`, `pbi_engine_connect` |
| Visual formatting | `pbi_set_conditional_format`, `pbi_clear_conditional_format`, `pbi_add_analytics_line`, `pbi_list_analytics_lines`, `pbi_remove_analytics_line`, `pbi_set_tooltip_page`, `pbi_set_slicer`, `pbi_set_data_labels`, `pbi_add_visual_calculation`, `pbi_list_visual_calculations`, `pbi_remove_visual_calculation` |
| Report measures, mobile, accessibility | `pbi_create_report_measure`, `pbi_list_report_measures`, `pbi_update_report_measure`, `pbi_delete_report_measure`, `pbi_set_mobile_layout`, `pbi_get_mobile_layout`, `pbi_set_alt_text`, `pbi_auto_alt_text`, `pbi_set_tab_order`, `pbi_auto_tab_order`, `pbi_accessibility_report` |
| Rendering and Deneb | `pbi_render_page`, `pbi_render_report`, `pbi_theme_from_image`, `pbi_list_deneb_templates`, `pbi_add_deneb_visual`, `pbi_set_deneb_spec` |
| Diff and import | `pbi_semantic_diff`, `pbi_import_pages`, `pbi_import_visuals`, and `scripts/pbir_merge.py` (git merge driver for PBIR JSON) |

MCP resources (`pbip://model`, `pbip://pages`, ...) and prompts (`audit_model`,
`bulk_measures`, `build_dashboard`, `theme_report`, `review_page`) are also
registered.

### Binding format

Bindings are `{bucket: ["Table.Field", ...]}`. Measures vs columns are
resolved automatically. Bucket names are per-visual-type and validated from
`core/visual_specs.py`, whose contents were **surveyed from real Desktop
exports** (150+ visuals) — notably: the Legend bucket is `Series`, combo
charts use `Y` + `Y2`, donut charts have no `Category`.

```jsonc
// a bar chart spec for pbi_add_visual / pbi_build_page
{
  "visual_type": "clusteredBarChart",
  "bindings": {"Category": ["Date.Year"], "Y": ["Sales.Net Revenue"]},
  "title": "Revenue by Year",
  "position": {"x": 40, "y": 40, "width": 600, "height": 360}  // optional
}
```

## Cloud: pbi-service (optional)

A third MCP server, `pbi-service`, talks to the Power BI / Fabric REST APIs so
the project you built locally can be published, refreshed, promoted and
exported without leaving the session. It is strictly additive: `pbi-model`
and `pbi-report` never import it and keep working offline with no auth.

```bash
pip install -e ".[dev,cloud]"          # cloud = msal, only needed for (c)/(d) below
python -m service_server.server        # or: pbi-service-server / scripts/launcher.py service
```

**Auth** is resolved in this order (first configured source wins; check
`pbi_service_status()`):

1. a token passed to `pbi_service_login(token=...)` (session only)
2. env `PBI_ACCESS_TOKEN` (e.g. from `az account get-access-token --resource https://analysis.windows.net/powerbi/api`)
3. a service principal from `AZURE_TENANT_ID` / `AZURE_CLIENT_ID` / `AZURE_CLIENT_SECRET` (msal, cached in memory, refreshed before expiry)
4. interactive device code via `pbi_service_login(device_code=True)` — requires env `PBI_CLIENT_ID`, the id of a public client app you register yourself (no client id is hard-coded); the first call returns `user_code` + `verification_uri`, a later call completes the sign-in

Tokens never touch disk or logs and every tool result is redacted.

| Tool | What it does |
|------|--------------|
| `pbi_service_status()` | Active auth source + identity hints, base URLs, last operation ids |
| `pbi_service_login(token?, device_code?)` | Store a session token or start/poll a device-code sign-in |
| `pbi_set_project(path)` | Select the local `.pbip` to publish (only needed for publishing) |
| `pbi_list_workspaces()` / `pbi_list_items(workspace, type?)` | Browse; workspaces/items accept an id or display name |
| `pbi_get_item_definition(workspace, item, out_dir, type?)` | Download a SemanticModel (TMDL) or Report (PBIR) into a PBIP-shaped folder the local servers can open |
| `pbi_publish_project(workspace, name?, update_if_exists?, publish_model?, publish_report?)` | Upload the model (every file except `.pbi/`) then the report bound to it (`definition.pbir` rewritten to `byConnection` in memory) |
| `pbi_refresh_dataset(workspace, dataset, wait?, timeout?)` / `pbi_refresh_status(...)` | Trigger a refresh, optionally wait; recent history |
| `pbi_list_deployment_pipelines()` / `pbi_deploy_pipeline_stage(pipeline, source_stage, items?, wait?)` | Deploy all or selected items from a stage |
| `pbi_export_report(workspace, report, out_path, format="PDF"\|"PPTX"\|"PNG", page_ids?)` | Export to file and download it |

Cloud writes change nothing on disk, so they have **no `dry_run` preview and
no `pbi_undo`**; the service is the source of truth for them. HTTP goes
through stdlib `urllib` with retries on 429/5xx (honouring `Retry-After`) and
long-running operations polled with a timeout. The REST calls follow the
official Fabric Core Items and Power BI REST references but are exercised in
this repo only against scripted fakes — treat the first run against a real
tenant as a smoke test.

## Layout

```
pbi-mcp/
  core/            # PbipProject + TMDL/PBIR read-write, specs, lineage,
                   # usage classifier, formatting/filter builders, safe I/O;
                   # auth.py + fabric_api.py back the optional cloud server
  model_server/    # MCP server: pbi-model
  report_server/   # MCP server: pbi-report
  service_server/  # MCP server: pbi-service (optional, cloud)
  scripts/         # smoke test, M5 demo, packager
  tests/           # 1700 tests; fixtures/ (synthetic + real, gitignored)
```

## Testing

Fixture-based + golden files + fuzz. The suite runs against a synthetic PBIP
project (committed) and any real exports dropped under `tests/fixtures/real/`
(auto-discovered, never committed). Golden snapshots of emitted `visual.json`
live in `tests/goldens/` (regenerate deliberately with
`PBI_MCP_REGEN_GOLDENS=1`). A seeded 60-op fuzz storm asserts the project
always reloads parseable. The reopen-in-Desktop check stays a manual gate
before each release.

## Live engine (optional)

Everything above works on the project *files*. On Windows with Power BI
Desktop installed, the `pbi-model` server also exposes read-only tools that
talk to the **running** model -- the local Analysis Services instance behind
an open Desktop window -- so DAX can be checked against real data before it
is written to TMDL. They never modify the project.

| Tool | What it does |
|------|--------------|
| `pbi_engine_status()` | ADOMD DLL in use, running Desktop instances (port, databases, tables), the instance matched to the selected project, or why nothing is reachable. Never raises. |
| `pbi_evaluate_dax(query, max_rows?, port?, database?)` | Run `EVALUATE ...` or a DMV (`SELECT * FROM $SYSTEM....`); rows as JSON scalars, dates ISO 8601, `truncated` flag |
| `pbi_validate_dax(dax, table?)` | The engine compiles a measure expression (`EVALUATE ROW("v", <dax>)`, or a query-scoped `DEFINE MEASURE` on `table`); nothing is persisted. Errors point into your expression. |
| `pbi_table_row_counts()` | Row count per table (storage DMV, `COUNTROWS` fallback) |
| `pbi_column_stats(table?)` | VertiPaq cardinality, dictionary / data / hierarchy size and encoding per column, largest first |
| `pbi_preview_table(table, top?)` | `EVALUATE TOPN(top, 'table')` |
| `pbi_engine_connect(connection_string)` | Pin a remote XMLA endpoint (e.g. `Data Source=powerbi://api.powerbi.com/v1.0/myorg/<Workspace>;Initial Catalog=<Dataset>;User ID=;Password=<access token>`) for the session; memory only, `Password=` redacted everywhere. `""` unpins. |

How it works (`core/engine.py`, no pip dependencies): each Desktop instance
writes its port to `AnalysisServicesWorkspace_<guid>/Data/msmdsrv.port.txt`.
Those folders are searched under `%USERPROFILE%\Microsoft\Power BI Desktop
Store App\AnalysisServicesWorkspaces` (Store build), its package-virtualised
`LocalCache` twin, and `%LOCALAPPDATA%\Microsoft\Power BI Desktop\...` (MSI
build); only the port file is ever read. A port must accept a TCP connect
to count, and the instance is matched to the selected project by table
names. Queries run through ADOMD.NET in a spawned `powershell.exe` that
`Add-Type`s the AdomdClient DLL shipped with Desktop (about 2 s per tool
call); query text and connection strings travel in a temp file, never on the
command line. The DLL is looked up as `PBI_ADOMD_DLL` (env), then the MSI
install, then the Store package (`Get-AppxPackage`, newest version). When
nothing is reachable the tools raise a clear `EngineUnavailable` ("open the
project in Power BI Desktop" / "set PBI_ADOMD_DLL"); on non-Windows they
always do.

A `.pbip` opens in Desktop **without data**: row counts are 0 and measures are
blank until the model is refreshed (the banner's *Refresh now*).

`tests/fixtures/engine/Engine.pbip` is a tiny project with inline data (three
`Table.FromRows` partitions, one relationship, three measures, one card) that
opens and refreshes in Desktop without any external source or credentials.

```bash
python scripts/desktop_launch.py tests/fixtures/engine/Engine.pbip --refresh   # open + load data
python -m pytest tests/test_engine.py -q     # the live tests run when an instance is up, else skip
python scripts/desktop_launch.py --close     # normal close, answers "Don't save"; Stop-Process last
```

## License

MIT.
