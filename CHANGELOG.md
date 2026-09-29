# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Release notes for each GitHub Release are taken from the matching section below
(`scripts/changelog_section.py`).

## [Unreleased]

### Added

- **Tool annotations.** Every tool declares the MCP `readOnlyHint`,
  `destructiveHint` and `idempotentHint` annotations, so hosts can auto-approve
  reads and gate deletes.
- **`dry_run` on every write.** Write tools run against a scratch copy of the
  project and return a unified diff; nothing is written to disk.
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
- In progress (parallel work packages; each entry is expanded as it merges):
  - Live engine
  - Tables, partitions and refresh policy
  - Column lifecycle, hierarchies and KPIs
  - RLS, OLS, perspectives, translations, field parameters and what-if
    parameters
  - Conditional formatting, analytics lines, slicer sync and visual calculations
  - Report-level measures, mobile layout and accessibility
  - Page rendering, theme from image and Deneb
  - Semantic diff, page import and merge driver
  - `pbi-service` cloud server
  - Docs site, `doctor`, MCP resources and prompts

### Changed

- Packaging metadata: PEP 639 `license` expression, project URLs, classifiers
  (Python 3.11 to 3.14) and keywords; package discovery now also picks up the
  optional `service_server` package.
- README: badges and a rewritten Install section (PyPI, standalone `.plugin`,
  `.mcpb`, from source).
- `scripts/build_standalone.py` bundles the `service` server only when
  `service_server/` exists in the checkout.

### Fixed

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
