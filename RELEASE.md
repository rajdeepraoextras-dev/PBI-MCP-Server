# pbi-mcp — release notes & portfolio write-up

## v2.0.1 — compatibility & packaging

- Runs on both the mcp Python SDK 1.x (`FastMCP`) and 2.x (`MCPServer`) via
  `core/mcp_compat.py`; the 2.x rename had broken fresh installs at import.
- Dependency range is now `mcp>=1.2.0,<3`; the bundle install notes use the
  same range and include the previously missing `jsonschema`.
- The ~20 MB standalone build is no longer tracked in git; download it from
  the GitHub Release. README counts corrected (57 tools).

## v2.0.0 — "Epic Reports"

A major upgrade from a correct report *builder* to a report *designer*. 57 MCP
tools (13 model + 44 report) across the two servers; 297 tests.

**Correctness (E1).** Every report write is now pre-flight validated against
the official Microsoft Fabric JSON schemas (vendored offline, transitive
closure). Calibrated on 140 real Desktop files — 138 clean, 2 documented
version-drift exceptions. This eliminates the class of bug Desktop caught in
v1.0.1. Plus schema-built relativeDate filters, an mtime-keyed parse cache
(2.2× on the real model), and a `pbi_capabilities` self-description so an LLM
can build without trial-and-error.

**Design layer (E2).** The thing that makes reports look designed, not
generated: textboxes, images (with resource upload), shapes/backplates/
dividers, page backgrounds + wallpaper, z-order, visual groups, and a raw
escape hatch for third-party visuals — every shape copied from real exports.

**Design system (E3).** `pbi_generate_theme` (brand color → coherent palette +
text classes, light/dark); a 12-column grid layout engine with a KPI band and
auto page-height; `pbi_build_designed_page` (header band + KPI strip on rounded
backplates + chart grid in one call); and `pbi_lint_page` for overlaps,
off-canvas, and misalignment.

**Interactivity (E4).** Sorting, `Sum()/Average()` column aggregations, page-
navigation buttons, drillthrough/tooltip page roles, cross-filter interactions,
and bookmarks.

**Intelligence (E5).** `pbi_profile_model` classifies the model (fact/dimension/
date tables, measure roles, grouping columns); `pbi_scaffold_report` turns a
raw model into a themed, navigable, multi-page designed report in **one call** —
verified building a 4-page, 105-file, fully schema-valid report from the HR
sample.

**Lifecycle (E6).** Page rename/hide/reorder/delete/duplicate, filter list/
remove, recoverable visual/page trash + restore, `pbi_project_diff`, and CI.

---

# pbi-mcp v1.x — release notes & portfolio write-up

## v1.0.1 (schema-compliance fix)

Desktop's PBIR schema validation of the v1.0.0 demo artifact surfaced two
invalid shapes; both fixed against the published Fabric schemas + real files:

- `themeCollection.customTheme` now includes the required
  `reportVersionAtImport` (an object of layer versions in real Desktop
  files, despite the schema calling it a string — real files win).
- TopN filters now emit the schema's `VisualTopN {ItemCount}` condition;
  the previous `Top {Expressions, OrderBy, Count}` tree was rejected.
  FilterDefinition carries only `Version/From/Where` — the ranking measure
  comes from the visual's own value field.

## v1.0.0

**What it is.** Two MCP servers (25 tools) that let an LLM read and write
Power BI Project files directly on disk — the semantic model (TMDL) and the
report (PBIR) — with no Power BI API, no auth, and no cloud dependency.
Prompt in, working `.pbip` out.

**Why it's hard.** The PBIP JSON/TMDL schemas are undocumented and shift
between Desktop versions; the report layer's `queryState` bucket names differ
per visual type; and one malformed byte can make Desktop refuse to open a
project. This build treats "never corrupt a project" as the primary spec.

## Highlights

- **Ground-truth engineering.** Every format decision was verified against
  real Desktop exports (4 projects, 150+ visuals, 60+ measures) — which
  *corrected* the original design several times: the Legend bucket is really
  `Series`, combo charts bind `Y`+`Y2` (not `ColumnY`/`LineY`), donut charts
  have no `Category`, TMDL is CRLF while page/visual JSON is LF.
- **Loss-free writes.** TMDL mutations are surgical text edits — partitions,
  annotations, lineage tags, and M source survive byte-for-byte. Proven by
  tests that assert untouched files are binary-identical after every write.
- **Safety in depth.** Atomic writes, backup-once snapshots with restore
  tools, transitive-lineage + report-usage delete guards, dry-run previews,
  recoverable visual deletes, and a seeded fuzz storm (3 × 60 random ops)
  that must always leave the project parseable.
- **Real capability.** One tool call builds a 10-visual page with automatic
  layout; the M5 demo produces a themed two-page executive report with
  report- and visual-scope filters on the standard HR sample.

## Numbers

- 25 MCP tools (11 model, 14 report) across 2 servers
- 223 tests, including golden-file snapshots for 8 visual types and
  end-to-end stdio client tests
- 36-day plan executed with per-day definition-of-done
  (see DAYWISE-PLAN.md / TRACKER.md at the workspace root)

## Known limits (v1.0)

- Relative-date filters need a raw condition (Passthrough) until a real
  sample export is available to copy the exact shape from.
- Desktop-reopen verification is a manual gate by design; the automated
  suite covers everything up to that final human check.
- Custom/AppSource visuals are out of scope (spec-driven types only).
