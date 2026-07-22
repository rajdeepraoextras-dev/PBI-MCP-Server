---
name: audit-power-bi-report
description: Use when reviewing, cleaning up, or health-checking a Power BI project — finding unused measures/columns, broken or risky deletes, schema problems, and design issues. Triggers on "audit my report", "what's unused", "clean up the model", "is this safe to delete", "health check", "review my dashboard", "find problems", "what can I remove". Read-only analysis first; only change things on explicit request.
---

# Audit a Power BI Project

Analysis before action. Run the read-only checks, summarize findings, and only
mutate when the user asks. Call `pbi_set_project(path)` on both servers as
needed.

## 1. Schema + design validity (report server)

- **`pbi_validate_project()`** — is every page/visual/filter schema-valid?
  Report any errors verbatim; these are shapes Desktop would reject.
- **`pbi_lint_page(page_id)`** for each page — overlaps, off-canvas visuals,
  too-small visuals, misaligned edges.

## 2. Field usage — what's actually used (report server)

- **`pbi_model_usage()`** classifies every measure and column as
  **direct** (bound in a visual/filter), **indirect** (needed by a displayed
  measure or a relationship join), or **unused**.
- The `unused` bucket is your cleanup candidate list — but confirm with the
  user before removing anything; "unused in this report" ≠ "safe to delete
  everywhere."

## 3. Model structure + lineage (model server)

- **`pbi_profile_model()`** — fact/dimension/date tables, measure roles
  (ratio/currency/time-intelligence), grouping columns. Flags oddities like a
  fact table with no measures or a dimension with no key.
- **`pbi_model_lineage()`** — the full dependency graph. Look for:
  - measures nothing references (leaf measures) that are also `unused` in the
    report → strong removal candidates;
  - `unresolved` references in any measure → a likely typo or broken DAX.

## 4. Before any delete

- **`pbi_delete_measure(table, name, dry_run=true)`** shows what would break
  (measure dependents + report usage) without changing anything. Present that,
  get confirmation, then run with `dry_run=false` (or `force=true` only if the
  user accepts breaking the listed dependents).

## Reporting the audit

Give the user a short, prioritized summary, e.g.:

> **Schema:** ✅ valid (105 files).
> **Design:** 1 overlap on "Detail" page (visuals X, Y).
> **Unused:** 4 measures, 12 columns (list). None are referenced by other
> measures — safe removal candidates.
> **Risk:** measure "Old KPI" is bound in 2 visuals; deleting it breaks them.

Then ask what they want to act on. Everything you change is atomic + backed up
(`pbi_list_backups` / `pbi_restore_backup`) and deletes are recoverable from
`.pbi/mcp-trash`, so cleanup is safe to do incrementally.
