# pbi-mcp

**MCP servers for local-file Power BI Project (`.pbip`) automation** — model
(TMDL) and report (PBIR) layers. No Power BI API, no auth, no cloud: the
tools read and write the on-disk project files Power BI Desktop itself uses.

Build a correctly-bound, themed, filtered, multi-page report — or bulk-author
hundreds of measures — from a prompt, in minutes. **57 tools** (13 model +
44 report) across two MCP servers; every report write is pre-flight validated
against the official Fabric schemas.

**Fastest path:** `pbi_set_project(path)` → `pbi_scaffold_report()` profiles
the model and builds a themed, navigable, multi-page designed report in one
call (use `dry_run=true` to review the proposal first). Or compose it yourself
with `pbi_build_designed_page`, the design elements, and the theme generator.

> **Scope honesty.** This builds **structure, speed, consistency**. It does
> **not** do custom Deneb/Vega visuals, AppSource visual sourcing, or replace
> design taste. It's a fast report *builder*, not an autonomous report
> *designer*.

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

## Download the plugin

For the easiest install, grab a bundle from the
[latest GitHub Release](https://github.com/rajdeepraoextras-dev/PBI-MCP-Server/releases/latest):

- `pbi-mcp-standalone-win32-amd64.plugin` - self-contained Windows build; no
  local Python setup required.
- `pbi-mcp.plugin` - source plugin bundle; requires Python 3.11+ plus `mcp`
  (1.x or 2.x), `pydantic` and `jsonschema` on the host.

The standalone build is ~20 MB and is published only as a release asset (it
is not tracked in git); rebuild it locally with `scripts/build_standalone.py`.

Drag the `.plugin` file into a plugin-aware MCP host, then call
`pbi_set_project(path)` first in both `pbi-model` and `pbi-report` sessions.

## Quickstart

```bash
git clone https://github.com/rajdeepraoextras-dev/PBI-MCP-Server.git && cd PBI-MCP-Server
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
.venv\Scripts\python -m pytest          # green suite = good to go
```

Register in Claude Desktop: run `scripts/package.py`, then merge
`dist/claude_desktop_config.snippet.json` into your
`claude_desktop_config.json` and restart. Or drag-drop `dist/pbi-mcp.plugin`
into a plugin-aware host. Details: `INSTALL.md` inside the bundle.

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

## Layout

```
pbi-mcp/
  core/            # PbipProject + TMDL/PBIR read-write, specs, lineage,
                   # usage classifier, formatting/filter builders, safe I/O
  model_server/    # MCP server: pbi-model
  report_server/   # MCP server: pbi-report
  scripts/         # smoke test, M5 demo, packager
  tests/           # 299 tests; fixtures/ (synthetic + real, gitignored)
```

## Testing

Fixture-based + golden files + fuzz. The suite runs against a synthetic PBIP
project (committed) and any real exports dropped under `tests/fixtures/real/`
(auto-discovered, never committed). Golden snapshots of emitted `visual.json`
live in `tests/goldens/` (regenerate deliberately with
`PBI_MCP_REGEN_GOLDENS=1`). A seeded 60-op fuzz storm asserts the project
always reloads parseable. The reopen-in-Desktop check stays a manual gate
before each release.

## License

MIT.
