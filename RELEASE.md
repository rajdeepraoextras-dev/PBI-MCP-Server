# pbi-mcp v1.0.0 — release notes & portfolio write-up

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
