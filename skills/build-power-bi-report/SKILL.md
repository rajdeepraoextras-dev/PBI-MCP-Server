---
name: build-power-bi-report
description: Use when the user wants to build, create, or scaffold a Power BI report, dashboard, or page. Triggers on "build a Power BI report", "create a dashboard", "make a sales report", "add a page", "scaffold a report from my model", "I need a report for X". Covers the fastest path from a .pbip to a finished, designed, schema-valid report using the pbi-model and pbi-report MCP servers.
---

# Build a Power BI Report

This plugin ships two MCP servers — **pbi-model** (semantic model / TMDL) and
**pbi-report** (report / PBIR) — that edit a `.pbip` project directly on disk.
Every write is atomic, backed up, and pre-flight validated against the
official Fabric schemas, so you cannot corrupt the project.

## Always do this first

Call **`pbi_set_project(path)`** on the server you're using (each server tracks
its own project). Point it at a `.pbip` file or the project folder. Then call
**`pbi_capabilities()`** once to load the exact visual types, buckets, and
filter kinds — don't guess bucket names, read them.

## The default is BUILD, and the fastest path is scaffold

Prefer building over interrogating the user. Scale questions to scope:

| Request | What to do |
|---|---|
| "Build me a report" / broad | `pbi_scaffold_report(dry_run=true)` → show the proposed pages → build |
| A specific page (KPIs + charts) | `pbi_build_designed_page(...)` in one call |
| One or a few visuals on a page | `pbi_add_visual` / `pbi_build_page` |
| One visual, one property | Just do it |

### `pbi_scaffold_report` — the autopilot

One call profiles the model (fact/dimension/date tables, measure roles,
grouping columns) and builds a **themed, navigable, multi-page designed
report**: an overview page (KPI strip on backplates + trend + breakdown +
table) plus per-dimension detail pages with a nav bar. Use
`dry_run=true` first to return the proposal so the user can edit it, then run
it for real. Pass `accent="#RRGGBB"` for their brand color.

### `pbi_build_designed_page` — one designed page

`pbi_build_designed_page(name, title, subtitle?, kpis, charts, accent?)`.
- `kpis`: `[{"measure": "Table.Measure", "title": "..."}]`
- `charts`: normal visual specs (positions auto-laid-out on a 12-col grid)

It composes a header band, a KPI strip on rounded backplates, and a chart grid.

## Binding grammar (for pbi_add_visual / pbi_build_page)

```json
{ "visual_type": "clusteredBarChart",
  "bindings": { "Category": ["Date.Year"], "Y": ["Sales.Revenue"] },
  "title": "Revenue by Year" }
```
- Buckets are per-visual-type — get them from `pbi_capabilities()`. The Legend
  role is the **`Series`** bucket; combo charts use **`Y`** + **`Y2`**; donut
  has no `Category`.
- Aggregate a column inline: `"Sum(Sales.Amount)"`, `"Average(...)"`.
- Measures vs columns are resolved automatically.

## Finish every build with verification

1. **`pbi_validate_project()`** — must be `ok: true` (schema-valid).
2. **`pbi_lint_page(page_id)`** — fix any `overlap` / `off_canvas` warnings.
3. Tell the user to reopen the `.pbip` in Power BI Desktop (the one check that
   can't be automated).

## Safety you can rely on

Deletes are guarded by lineage + report usage (`force`/`dry_run` available);
deleted visuals/pages go to `.pbi/mcp-trash` and restore with
`pbi_restore_visual` / trash tools; `pbi_list_backups` + `pbi_restore_backup`
recover any file.
