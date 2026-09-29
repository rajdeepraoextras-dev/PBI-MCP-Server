# Cookbook

Ten prompt recipes, each with the tool sequence an assistant should follow.
Every recipe assumes `pbi_set_project(path)` has been called on the servers it
uses. Tool names appear in code style; the [model](tools/model.md) and
[report](tools/report.md) tool references list every parameter.

Two habits make every recipe safer:

- **Preview first.** Write tools accept `dry_run=true` and return the diff
  without writing. (`pbi_scaffold_report` and `pbi_delete_measure` have their
  own preview: a proposal, and the dependents, respectively.)
- **Know your way back.** `pbi_undo` reverts the latest write;
  `pbi_begin_transaction` / `pbi_rollback` revert a group of writes.

## 1. Build a sales dashboard from a fresh model

> "Build a sales dashboard from Sales.pbip in our brand colour #0B6E4F. Show me
> the plan before you build anything."

1. `pbi_set_project(path)` on `pbi-model` and `pbi-report`.
2. `pbi_doctor()`. Fix any `error` finding (a legacy project, a missing folder)
   before going on.
3. `pbi_capabilities()`. Read the exact visual types and buckets instead of
   guessing (the Legend role is `Series`; combo charts use `Y` and `Y2`).
4. `pbi_profile_model()`. Fact, dimension and date tables, measure roles,
   grouping columns.
5. `pbi_scaffold_report(accent="#0B6E4F", dry_run=true)`. Returns the proposed
   pages, KPIs and charts; edit them with the user.
6. `pbi_scaffold_report(accent="#0B6E4F")`. Builds the theme, an overview page
   (KPI strip, trend, breakdown, table) and per-dimension detail pages with a
   navigation bar.
7. `pbi_list_pages()`, then `pbi_lint_page(page_id)` for each page and
   `pbi_validate_project()`. It must return `ok: true`.
8. Ask the user to reopen the `.pbip` in Power BI Desktop.

To compose pages yourself instead of the autopilot, replace steps 5 and 6 with
`pbi_generate_theme(brand="#0B6E4F", install=false)`, then
`pbi_set_report_theme(theme=<returned theme>)`, then one call per page:

```json
{
  "tool": "pbi_build_designed_page",
  "args": {
    "name": "Overview",
    "title": "Sales overview",
    "subtitle": "Net revenue and margin",
    "kpis": [
      { "measure": "Sales.Net Revenue", "title": "Revenue" },
      { "measure": "Sales.Margin %", "title": "Margin" }
    ],
    "charts": [
      { "visual_type": "clusteredBarChart",
        "bindings": { "Category": ["Date.Year"], "Y": ["Sales.Net Revenue"] },
        "title": "Revenue by year" }
    ]
  }
}
```

## 2. Bulk-author 30 measures from a spec

> "Here is a table of 30 measures (name, DAX, format, display folder). Create
> them all in Sales. Check for name clashes first."

1. `pbi_list_measures()`. Measure names are unique across the whole model, so
   this is your collision check.
2. `pbi_get_model()`. Confirm the real table and column names the DAX uses.
3. Convert the spec to the batch shape and call
   `pbi_bulk_create_measures(measures=[...], dry_run=true)`. The whole batch is
   validated first (duplicate names, unknown tables), and the response carries
   the diff.
4. `pbi_bulk_create_measures(measures=[...])`. If the result has `warnings`, the
   linter spotted DAX Power BI will reject: fix each with
   `pbi_update_measure(table, name, dax=...)`.
5. `pbi_model_lineage()`. The new measures must show no `unresolved`
   references.
6. Wrong batch? `pbi_undo()`: one bulk call is one undo step.

```json
{
  "tool": "pbi_bulk_create_measures",
  "args": {
    "measures": [
      { "table": "Sales", "name": "Total Sales", "dax": "SUM(Sales[Amount])",
        "format": "#,0", "display_folder": "Revenue" },
      { "table": "Sales", "name": "Margin %",
        "dax": "DIVIDE([Total Sales] - [Total Cost], [Total Sales])",
        "format": "0.0%", "display_folder": "Margin" }
    ],
    "dry_run": true
  }
}
```

## 3. Add a time-intelligence calculation group

> "Add a Time Intelligence calculation group with Current, YTD, prior year and
> YoY %."

1. `pbi_get_model()`. Confirm there is a proper Date table with a `Date` column
   and a relationship from your fact table. If not, add the relationship with
   `pbi_create_relationship(from_table="Sales", from_column="OrderDate",
   to_table="Date", to_column="Date")`.
2. `pbi_create_calc_group(name="Time Intelligence", precedence=10, items=[...], dry_run=true)`
   and read the diff.
3. Run it again without `dry_run`. The table is created and registered in
   `model.tmdl`; items keep the order you gave and the selector column is named
   `Name`.
4. Put it on a slicer with the report server:
   `pbi_add_visual(page_id, [{"visual_type": "slicer", "bindings": {"Values": ["Time Intelligence.Name"]}, "title": "Period"}])`.
5. Reopen in Desktop to confirm the group loads and behaves.

```json
{
  "tool": "pbi_create_calc_group",
  "args": {
    "name": "Time Intelligence",
    "precedence": 10,
    "items": [
      { "name": "Current", "dax": "SELECTEDMEASURE()" },
      { "name": "YTD", "dax": "CALCULATE(SELECTEDMEASURE(), DATESYTD('Date'[Date]))" },
      { "name": "PY", "dax": "CALCULATE(SELECTEDMEASURE(), SAMEPERIODLASTYEAR('Date'[Date]))" },
      { "name": "YoY %",
        "dax": "VAR _cy = SELECTEDMEASURE()\nVAR _py = CALCULATE(SELECTEDMEASURE(), SAMEPERIODLASTYEAR('Date'[Date]))\nRETURN\n    DIVIDE(_cy - _py, _py)" }
    ]
  }
}
```

## 4. Audit and remove unused fields safely

> "Find measures and columns nothing uses and remove the safe ones."

1. `pbi_model_usage()` (report server) classifies every field as **direct**
   (bound in a visual or filter), **indirect** (needed by a displayed measure
   or a relationship) or **unused**.
2. `pbi_model_lineage()` (model server). Measures that are unused *and* not
   referenced by any other measure are the safe candidates. Note any
   `unresolved` references: they are typos or broken DAX.
3. For each candidate, `pbi_delete_measure(table, name, dry_run=true)` shows
   `dependents` without touching anything.
4. `pbi_begin_transaction()`.
5. `pbi_delete_measure(table, name)` for each approved measure. A measure with
   dependents or report usage is refused; only pass `force=true` if the user
   accepts the breakage it lists.
6. `pbi_validate_project()`, then `pbi_model_usage()` again to confirm the
   counts moved as expected.
7. `pbi_commit()` to keep the cleanup, or `pbi_rollback()` to revert all of it.

"Unused in this report" is not "unused everywhere": other reports may be bound
to the same model. There is deliberately no tool that deletes columns; list
the unused ones for manual cleanup in Desktop.

## 5. Re-theme to a brand colour

> "Re-theme the whole report to #0B6E4F."

1. `pbi_list_pages()`, then `pbi_page_style(page_id)` on one page to see its
   header band, KPI card and backplate.
2. `pbi_generate_theme(brand="#0B6E4F", name="Brand", mode="light", install=false)`
   previews the palette, text classes and visual styles.
3. Install it through a write tool so it can be previewed and undone:
   `pbi_set_report_theme(theme=<returned theme>, dry_run=true)`, then the same
   call without `dry_run`.
4. Pages with an explicit canvas colour:
   `pbi_style_page(page_id, background_color="#FFFFFF", wallpaper_color="#EEF2F5")`.
5. Visuals with hard-coded colours ignore the theme. Find them with
   `pbi_list_visuals(page_id)` and `pbi_get_visual(page_id, visual_id)`, then
   recolour with `pbi_format_visual(page_id, visual_id, target="container",
   objects={"title": {"fontColor": "#0B6E4F"}})`.
6. `pbi_lint_page(page_id)` for each page and `pbi_validate_project()`.

`pbi_generate_theme` with `install=true` also installs, but it is a read-only
tool: that route has no `dry_run` and is not in the undo journal.

## 6. Add a drill-through page

> "Add a Product Detail page that we can drill through to from Overview."

1. `pbi_list_pages()` to pick the source page and its id.
2. Build the page in the same look as the source:
   `pbi_build_designed_page(name="Product Detail", title="Product detail", kpis=[...], charts=[...], match_page="overview")`.
   Page size and accent are inherited; `match_page` also copies the header and
   KPI composition. Note the `page_id` in the response.
3. `pbi_set_page_role(page_id, role="drillthrough")` marks it as a
   drill-through page (`tooltip` and `default` are the other roles). Choose the
   drill-through field afterwards in Desktop's *Drill through* well.
4. `pbi_add_nav_button(page_id, label="Back", target_page_id="overview")` for a
   way back.
5. Optionally `pbi_hide_page(page_id, hidden=true)` so it only appears through
   drill-through.
6. `pbi_lint_page(page_id)` and `pbi_validate_project()`.

## 7. Wire navigation buttons and bookmarks

> "Add a nav bar to every page, and bookmarks for FY2024 and FY2023."

1. `pbi_list_pages()` for the page ids. (`pbi_scaffold_report` already builds a
   nav bar for the pages it creates.)
2. For each page and each target:
   `pbi_add_nav_button(page_id, label, target_page_id, position={"x": 16, "y": 8, "width": 140, "height": 32}, fill="#1F3A5F")`.
   Step the `x` position by 150 per button.
3. Set up the state a bookmark should capture:
   `pbi_add_filter(scope="page", field="Date.Year", filter_type="Categorical", values=[2024], page_id=page_id)`.
4. `pbi_create_bookmark(name="FY2024", display_name="FY 2024", page_id=page_id)`
   captures the active page and each page's saved filter state. Remove the
   filter (`pbi_list_filters`, `pbi_remove_filter`), add the next one, and
   repeat for FY2023.
5. `pbi_validate_project()`.

A button that *applies* a bookmark has no dedicated tool. Copy a Desktop-made
button with `pbi_get_visual` and add it with `pbi_add_visual_raw`.

## 8. Preview any change with dry_run and revert with undo

> "Add a Margin % measure, but show me exactly what will change first."

1. `pbi_create_measure(table="Sales", name="Margin %", dax="DIVIDE([Profit], [Net Revenue])", format="0.0%", dry_run=true)`.
   The response has `result`, `changes` (added, modified, deleted paths) and
   the unified `diff`. Nothing is written.
2. Apply the same call without `dry_run`.
3. `pbi_undo_history(limit=5)` lists what can be reverted.
4. `pbi_undo(steps=1)` reverts the latest write byte for byte.

To experiment across several writes: `pbi_begin_transaction()`, make the
changes, then `pbi_commit()` or `pbi_rollback()`.

## 9. Diff two versions of a project

> "Compare this project with the copy in C:\Reports\Sales_v1 and summarise what
> changed."

1. `pbi_project_diff(other_path="C:\\Reports\\Sales_v1\\Sales.pbip")` compares
   the selected project (the base) with the other one: measures (added,
   removed, changed DAX or format), pages, visuals, relationships,
   `total_changes` and `identical`. "Added" means present only in the *other*
   project.
2. `pbi_project_summary()` gives a compact overview of the selected project
   (tables, measures, relationships, pages, visuals and field-usage counts) to
   put the diff in context.
3. Drill into a difference with `pbi_list_visuals(page_id)`,
   `pbi_get_visual(page_id, visual_id)` or `pbi_list_measures()`.

The other path can be any `.pbip`, or a copy of an earlier state: a folder
copy, a checkout of an older Git commit, or a restored backup.

## 10. Recover from trash and backups

> "I deleted the wrong visual, and an earlier edit overwrote a file. Get both
> back."

Try these in order, from most to least convenient:

1. **Undo.** `pbi_undo_history()` shows the journaled writes; `pbi_undo(steps=n)`
   reverts the latest `n` byte for byte, including deletes.
2. **Trash.** Deleted visuals and pages are moved, not erased, into
   `<name>.Report/.pbi/mcp-trash/`. `pbi_list_trash()` lists them and
   `pbi_restore_visual(trash_path)` puts a visual back on its page. A deleted
   page is in `.pbi/mcp-trash/pages/<id>-<timestamp>/`; `pbi_delete_page` returned
   that path as `recoverable_at`.
3. **Backups.** The first edit to any file leaves a `*.bak-<timestamp>` copy
   beside it. `pbi_list_backups()` lists them and
   `pbi_restore_backup(backup=<path>)` restores one atomically.
4. **Your own version control.** The project folder is plain text; commit it.

!!! warning "Run the restore tools for real"
    Do not use `dry_run` with `pbi_restore_visual` or `pbi_restore_backup`.
    They take a path *into* the project, which the preview's scratch copy
    cannot see, so the preview is unreliable and can consume the trashed
    copy. Check `pbi_list_trash()` or `pbi_list_backups()` first, then restore
    for real. See [Dry run](safety.md#dry-run).

After any recovery, run `pbi_doctor()` and `pbi_validate_project()`, then
reopen the project in Desktop.

## Built-in resources and prompts

Besides tools, each server publishes read-only **resources** (JSON a host can
attach without a tool call) and ready-made **prompts** (the recipes above, in
short form, naming the exact tools to call).

| Server | Resource | Content |
|---|---|---|
| `pbi-model` | `pbip://model` | compact overview: tables with column and measure names, relationships, counts |
| `pbi-model` | `pbip://model/measures` | every measure with DAX, format string and display folder |
| `pbi-model` | `pbip://model/lineage` | the DAX dependency graph |
| `pbi-model` | `pbip://model/tables/{table}` | one table's full columns, measures and relationships |
| `pbi-report` | `pbip://pages` | pages in order with size, visual count and hidden flag |
| `pbi-report` | `pbip://pages/{page_id}` | one page with its visuals and bindings |
| `pbi-report` | `pbip://capabilities` | visual types, buckets, filters and workflow (needs no project) |
| `pbi-report` | `pbip://theme` | the active theme: base, custom theme JSON and accent |

Resources answer `{"error": "..."}` (JSON) when no project is selected, telling
you to call `pbi_set_project` first.

| Server | Prompt | Arguments | Purpose |
|---|---|---|---|
| `pbi-model` | `audit_model` | none | read-only model audit and safe cleanup candidates |
| `pbi-model` | `bulk_measures` | `spec` | create many measures from a written spec, previewed and undoable |
| `pbi-report` | `build_dashboard` | `goal` | scaffold a themed, validated report for a goal |
| `pbi-report` | `theme_report` | `brand_color` | re-theme to a brand colour |
| `pbi-report` | `review_page` | `page_id` | review one page and propose fixes |
