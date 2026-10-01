# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Release notes for each GitHub Release are taken from the matching section below
(`scripts/changelog_section.py`).

## [Unreleased]

### Added

- **Website** (`website/`): a landing page for the GitHub Pages site, with a
  download button that picks the visitor's platform from the latest release,
  a tool explorer, and a preview / apply / undo transcript. Every number and
  tool on it is generated from the servers by `scripts/gen_site_assets.py`
  (checked in CI and by `tests/test_site_assets.py`). The page is set strictly
  in self-hosted Poppins, with no second typeface and no fallback glyphs. The
  docs moved under `/docs/`; the Docs workflow now deploys through the GitHub
  Pages actions.

## [2.1.0] - 2026-09-29

### Added

- **Tool annotations.** Every tool declares the MCP `readOnlyHint`,
  `destructiveHint` and `idempotentHint` annotations, so hosts can auto-approve
  reads and gate deletes.
- **`dry_run` on write tools.** Write tools run against a scratch copy of the
  project and return a unified diff; nothing is written to disk. (Restore,
  cloud and render tools have no preview.)
- **Undo journal and transactions.** Real writes are journaled under
  `<project>/.pbi-mcp/undo/` (outside the `*.Report` / `*.SemanticModel`
  folders). New tools: `pbi_undo`, `pbi_undo_history`,
  `pbi_begin_transaction`, `pbi_commit`, `pbi_rollback`.
- **Tool modules.** Each server auto-discovers `tools_*.py` modules that expose
  `register(mcp, state, tool)` (`core/tooling.py`), so new tool families ship
  without editing the server files.
- **DAX tokenizer and parser** (`core/dax_parser.py`); model lineage is now
  parser-based.
- **`pbi-mcp` command** (`core/cli.py`): `pbi-mcp <model|report|service>` with
  `--transport stdio|streamable-http|sse`, `--host`, `--port` and `--project`
  (or `PBI_MCP_PROJECT`), so `uvx pbi-mcp report` works with no checkout.
  `core.mcp_compat.run_server()` maps host and port onto both mcp 1.x and 2.x.
- **PyPI release** via GitHub Actions trusted publishing (`publish.yml`); no
  API tokens stored.
- **Release automation** (`release.yml`): on a `v*` tag, builds standalone
  executables for Windows, macOS and Linux, the source `.plugin`, the `.mcpb`
  bundles and a portable Claude Desktop config snippet, then creates the GitHub
  Release with `SHA256SUMS.txt` and the notes from this changelog.
- **`.mcpb` bundles** (`pbi-mcp-model.mcpb`, `pbi-mcp-report.mcpb`, plus
  `pbi-mcp-service.mcpb` when the service server is present): one-click Claude
  Desktop extensions with a default-project setting.
- **`server.json`** for the official MCP Registry
  (`io.github.rajdeepraoextras-dev/pbi-mcp`).
- **CI** now runs the suite on Python 3.11 to 3.14 against both mcp 1.x and
  2.x, and a packaging job builds the sdist, wheel and bundles and validates the
  `.mcpb` manifests.
- Project docs and templates: `CONTRIBUTING.md`, `SECURITY.md`, issue forms,
  pull request template.
- **Live engine (optional, Windows).** Connect to the Analysis Services engine
  of a running Power BI Desktop, or to a remote XMLA endpoint:
  `pbi_engine_status`, `pbi_evaluate_dax`, `pbi_validate_dax` (the engine
  compiles a measure before it is written), `pbi_table_row_counts`,
  `pbi_column_stats`, `pbi_preview_table`, `pbi_engine_connect`.
  `scripts/desktop_launch.py` opens and closes Desktop on a fixture project.
- **Cascading rename** of measures, columns and tables across TMDL and PBIR:
  `pbi_rename_measure`, `pbi_rename_column`, `pbi_rename_table`,
  `pbi_find_references`. One `pbi_undo` reverts the whole cascade.
- **Tables, partitions and refresh policy:** `pbi_create_table`,
  `pbi_create_calculated_table`, `pbi_list_partitions`, `pbi_update_partition`,
  `pbi_delete_table`, shared expressions and parameters
  (`pbi_create_expression`, `pbi_list_expressions`, `pbi_update_expression`),
  incremental refresh (`pbi_set_refresh_policy`, `pbi_remove_refresh_policy`).
- **Column, hierarchy and KPI lifecycle:** `pbi_list_columns`,
  `pbi_update_column`, `pbi_update_table`, `pbi_delete_column`,
  `pbi_set_measure_properties` (descriptions, hidden, KPI), `pbi_remove_kpi`,
  `pbi_create_hierarchy`, `pbi_list_hierarchies`, `pbi_delete_hierarchy`.
- **Security and metadata:** row-level security roles, object-level security,
  perspectives, translations and cultures, field parameters and what-if
  parameters (`pbi_create_role`, `pbi_set_column_permission`,
  `pbi_create_perspective`, `pbi_add_culture`, `pbi_set_translation`,
  `pbi_create_field_parameter`, `pbi_create_whatif_parameter` and their
  list/update/delete siblings).
- **Best Practice Analyzer and DAX formatter:** `pbi_bpa`, `pbi_bpa_fix`,
  `pbi_bpa_rules` (ported Tabular Editor rules with safe fixers and custom rules
  from `.pbi-mcp/bpa_rules.json`), `pbi_format_dax`, `pbi_format_measures`,
  `pbi_dax_references`.
- **Advanced visual formatting:** conditional formatting (gradient, rules,
  field value, data bars, icons, web URL), analytics lines, tooltip pages,
  slicer styles and sync groups, data labels, visual calculations.
- **Report-level measures, mobile layout and accessibility:**
  `pbi_create_report_measure` and siblings (`reportExtensions.json`),
  `pbi_set_mobile_layout` / `pbi_get_mobile_layout`, alt text, tab order and
  `pbi_accessibility_report`; `pbi_lint_page` now also returns `a11y_*`
  findings and an `accessibility_ok` flag.
- **Page rendering, theme from image and Deneb:** `pbi_render_page`,
  `pbi_render_report` (SVG or PNG), `pbi_theme_from_image`, and twelve Deneb
  (Vega-Lite) templates via `pbi_add_deneb_visual`, `pbi_set_deneb_spec`,
  `pbi_list_deneb_templates`. Optional extra: `pip install "pbi-mcp[render]"`.
- **Semantic diff, page import and a git merge driver:** `pbi_semantic_diff`,
  `pbi_import_pages`, `pbi_import_visuals`, and `scripts/pbir_merge.py` for
  structural three-way merges of PBIR JSON.
- **`pbi-service` (optional third server):** publish a local project to a
  Fabric workspace, refresh datasets, deploy pipeline stages and export reports
  over the Fabric and Power BI REST APIs. Token, service-principal or
  device-code sign-in; the local servers never import it. Optional extra:
  `pip install "pbi-mcp[cloud]"`. Not exercised against the live service.
- **Docs site, `pbi_doctor`, MCP resources and prompts:** a mkdocs-material
  site with a generated tool reference, a project health check in both
  servers, `pbip://` resources and five guided prompts.

### Changed

- Packaging metadata: PEP 639 `license` expression, project URLs, classifiers
  (Python 3.11 to 3.14) and keywords; package discovery now also picks up the
  optional `service_server` package.
- README: badges and a rewritten Install section (PyPI, standalone `.plugin`,
  `.mcpb`, from source).
- `scripts/build_standalone.py` bundles the `service` server only when
  `service_server/` exists in the checkout.

### Fixed

- `pbi_restore_visual` and `pbi_restore_backup` no longer accept `dry_run`: a
  scratch-copy preview acted on the real trash and backups.
- `pbi_generate_theme` installed a theme by default while marked read-only; it
  is now a write tool with `dry_run` and undo.
- `pbi_rollback` and `pbi_commit` now work past the history cap, `pbi_undo`
  removes the safety copies and empty folders the undone write created, and
  deleted or restored visuals are fully undoable. Cleanup copes with the
  read-only folder attribute OneDrive sets on Windows.
- `relationships.tmdl` `isActive: false` was read as active.
- Built wheels and sdists now include the vendored Fabric schemas
  (`resources/schemas`). Previously an installed wheel had none, so pre-flight
  schema validation could not run outside a source checkout.

## [2.0.1] - 2026-09-28

### Added

- Support for the mcp Python SDK 2.x alongside 1.x through `core/mcp_compat.py`
  (`FastMCP` on 1.x, `MCPServer` on 2.x).
- Self-contained standalone build (PyInstaller): no Python or `pip` needed on
  the host.
- Five bundled skills: build, design, model, wireframe and audit.
- Conservative DAX linter and skill guidance for common Power BI rejections.
- New pages inherit the report's page size and theme accent; header and KPI
  style matching (`match_page`).
- Author greeting on first connect.

### Changed

- Dependency range is `mcp>=1.2.0,<3`; the bundle install notes use the same
  range and now include the previously missing `jsonschema`.
- The ~20 MB standalone build is no longer tracked in git; it is published as a
  GitHub Release asset.
- Plugin bundle follows Claude's plugin format (`.claude-plugin/plugin.json` and
  `.mcp.json`).
- README tool counts corrected (57 tools).

### Fixed

- Fresh installs no longer fail at import when pip resolves mcp 2.x, which
  renamed `FastMCP` to `MCPServer`.

## [2.0.0] - 2026-07-21

"Epic Reports": from a correct report builder to a report designer. 57 MCP
tools (13 model, 44 report) across the two servers.

### Added

- **Correctness.** Every report write is pre-flight validated against the
  official Microsoft Fabric JSON schemas (vendored offline). Calibrated on 140
  real Desktop files: 138 clean, 2 documented version-drift exceptions.
  Schema-built `relativeDate` filters, an mtime-keyed parse cache (2.2x on the
  real model), and `pbi_capabilities` so a model can build without trial and
  error.
- **Design layer.** Textboxes, images (with resource upload), shapes,
  backplates and dividers, page backgrounds and wallpaper, z-order, visual
  groups, and a raw escape hatch (`pbi_add_visual_raw`) for third-party visuals;
  every shape copied from real exports.
- **Design system.** `pbi_generate_theme` (brand colour to palette and text
  classes, light or dark), a 12-column grid layout engine with a KPI band and
  automatic page height, `pbi_build_designed_page`, and `pbi_lint_page` for
  overlaps, off-canvas and misalignment.
- **Interactivity.** Sorting, `Sum()` and `Average()` column aggregations,
  page-navigation buttons, drillthrough and tooltip page roles, cross-filter
  interactions, and bookmarks.
- **Intelligence.** `pbi_profile_model` classifies fact, dimension and date
  tables, measure roles and grouping columns; `pbi_scaffold_report` turns a raw
  model into a themed, navigable, multi-page designed report in one call.
- **Lifecycle.** Page rename, hide, reorder, delete and duplicate; filter list
  and remove; recoverable visual and page trash with restore; `pbi_project_diff`;
  continuous integration.

## [1.0.1] - 2026-07-21

### Fixed

- `themeCollection.customTheme` now includes the required
  `reportVersionAtImport` (an object of layer versions in real Desktop files,
  despite the schema calling it a string).
- TopN filters emit the schema's `VisualTopN { ItemCount }` condition; the
  previous `Top { Expressions, OrderBy, Count }` tree was rejected by Desktop.
  The filter definition carries only `Version`, `From` and `Where`.

## [1.0.0] - 2026-07-21

### Added

- Two MCP servers (25 tools: 11 model, 14 report) that read and write the
  semantic model (TMDL) and the report (PBIR) of a Power BI Project directly on
  disk. No Power BI API, no auth, no cloud dependency.
- Loss-free TMDL writes: surgical text edits, so partitions, annotations,
  lineage tags and M source survive byte-for-byte.
- Safety in depth: atomic writes, backup-once snapshots with restore tools,
  transitive-lineage and report-usage delete guards, dry-run previews,
  recoverable visual deletes, and a seeded fuzz storm that must always leave the
  project parseable.
- Visual specs (bucket names per visual type) surveyed from real Desktop
  exports, golden snapshots for 8 visual types, and end-to-end stdio client
  tests.

### Known limits

- Desktop-reopen verification is a manual gate by design.
- Custom and AppSource visuals are out of scope (spec-driven types only).

[Unreleased]: https://github.com/rajdeepraoextras-dev/PBI-MCP-Server/compare/v2.0.1...HEAD
[2.0.1]: https://github.com/rajdeepraoextras-dev/PBI-MCP-Server/compare/v2.0.0...v2.0.1
[2.0.0]: https://github.com/rajdeepraoextras-dev/PBI-MCP-Server/compare/v1.0.1...v2.0.0
[1.0.1]: https://github.com/rajdeepraoextras-dev/PBI-MCP-Server/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/rajdeepraoextras-dev/PBI-MCP-Server/releases/tag/v1.0.0
