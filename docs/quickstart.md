# Quickstart

A first session, start to finish. You normally just ask the assistant in plain
language; the tool calls below are what it runs on your behalf, with abridged
sample responses so you know what to expect. Install the servers first
([Install](install.md)).

## 1. Prepare a project

Save your report from Power BI Desktop as a **Power BI Project** (`.pbip`) with
the PBIR and TMDL options enabled (see [Install](install.md#before-you-install)),
then **close it in Desktop**. You will have a folder like this:

```text
Sales/
  Sales.pbip
  Sales.Report/          # PBIR: definition/pages/..., report.json
  Sales.SemanticModel/   # TMDL: definition/tables/*.tmdl, relationships.tmdl
```

Work on a copy the first time, or make sure the folder is under version
control.

## 2. Point both servers at it

> "Open C:\Reports\Sales\Sales.pbip with both the model and report servers."

`pbi_set_project(path)` on **each** server (they keep separate state, though
they share one undo history per project). It accepts the `.pbip` file, the
project folder, or either layer folder.

```json
{ "ok": true, "path": "C:\\Reports\\Sales\\Sales.pbip", "tables": 2, "measures": 6 }
```

```json
{ "ok": true, "path": "C:\\Reports\\Sales\\Sales.pbip", "pages": 2, "visuals": 4 }
```

The first call in a session also returns a short greeting.

## 3. Check the environment

> "Run pbi_doctor and tell me if anything needs fixing."

`pbi_doctor()` is read-only, never raises, and works even before a project is
selected. With a project it also checks the on-disk formats, schema versions,
counts, backups, the undo journal and the trash. Abridged:

| Severity | Check | Message |
|---|---|---|
| info | python | Python 3.12.4 |
| info | mcp | mcp SDK 1.30.0 (major 1, FastMCP) |
| info | schema_validation | jsonschema + vendored Fabric schemas available; report writes are pre-flight validated. |
| info | desktop | Power BI Desktop installed: Store 2.157.1354.0 |
| info | report_format | Report uses the PBIR (enhanced metadata) format. |
| info | model_format | Semantic model uses the TMDL format. |
| info | counts | 2 tables, 6 columns, 6 measures, 2 pages, 4 visuals |
| info | backups | 0 backup file(s) (*.bak-*), 0 B |

`ok` is `false` only when a finding has severity `error` (for example a legacy
`report.json` or `model.bim` project). Findings that call for action usually
carry a `fix` with the next step.

## 4. Learn what can be built

> "What visual types and buckets can the report server use, and how is my model
> shaped?"

- `pbi_capabilities()` (report server) returns the visual types with their
  required and optional buckets, filter kinds, formatting targets and a worked
  example. The assistant reads it instead of guessing bucket names.
- `pbi_profile_model()` classifies the model into fact, dimension and date
  tables, measure roles and grouping columns. This is the analysis that drives
  the scaffold.

## 5. Scaffold with a dry run

> "Scaffold a report for this model. Show me the plan first."

```json
{ "tool": "pbi_scaffold_report", "args": { "dry_run": true } }
```

`pbi_scaffold_report` implements its own preview: with `dry_run=true` it
returns the **proposal** and writes nothing.

```json
{
  "ok": true,
  "dry_run": true,
  "proposal": {
    "accent": "#1F3A5F",
    "theme_mode": "light",
    "add_nav_bar": false,
    "pages": [ { "name": "Overview", "title": "Executive Overview", "kpis": [ "..." ], "charts": [ "..." ] } ]
  },
  "profile_summary": { "facts": 1, "dimensions": 0, "measures": 6, "time_intel_measures": 0 }
}
```

Adjust the proposal in conversation (page names, brand colour, KPIs), then
build it. Pass `accent="#RRGGBB"` for your brand colour; omit it to inherit
the report's existing theme, which is never overwritten.

## 6. Build it

```json
{ "tool": "pbi_scaffold_report", "args": { "accent": "#0B6E4F" } }
```

```json
{ "ok": true, "pages": [ "overview-2" ], "profile_summary": { "facts": 1, "dimensions": 0, "measures": 6, "time_intel_measures": 0 } }
```

This writes a theme, the pages, their visuals and, for several pages, a
navigation bar, all through the validated primitives. The write is journaled,
so it shows up in `pbi_undo_history()`.

## 7. Lint and validate

For every page the assistant runs `pbi_lint_page(page_id)`, then
`pbi_validate_project()` once:

```json
{ "ok": true, "checked": 21, "errors": [] }
```

`pbi_validate_project` checks every report JSON against the official Fabric
schemas and catches shapes Desktop would reject. `pbi_lint_page` returns
findings such as `overlap`, `off_canvas`, `tiny` and `misalign`, each with a
severity; fix the `warning` ones (`pbi_move_visual` repositions a visual).

## 8. Change the model: preview, apply, undo

> "Add an Average Sale measure to Sales, formatted #,0.00. Show me the diff
> first."

Every write tool accepts `dry_run=true`. The operation runs against a scratch
copy of the project and the response carries the unified diff. Nothing on disk
changes.

```json
{
  "tool": "pbi_create_measure",
  "args": { "table": "Sales", "name": "Average Sale",
            "dax": "DIVIDE([Net Revenue], COUNTROWS(Sales))",
            "format": "#,0.00", "dry_run": true }
}
```

```json
{
  "dry_run": true,
  "result": { "ok": true, "action": "created", "table": "Sales", "name": "Average Sale" },
  "changes": { "added": [], "modified": ["Sales.SemanticModel/definition/tables/Sales.tmdl"], "deleted": [] },
  "diff": "..."
}
```

```diff
--- a/Sales.SemanticModel/definition/tables/Sales.tmdl
+++ b/Sales.SemanticModel/definition/tables/Sales.tmdl
@@ -26,6 +26,9 @@
 	measure 'Hidden Helper' = 1
 		isHidden
 
+	measure 'Average Sale' = DIVIDE([Net Revenue], COUNTROWS(Sales))
+		formatString: #,0.00
+
 	column Amount
 		dataType: double
 		summarizeBy: sum
```

Happy with the diff? Run the same call without `dry_run`. Changed your mind
afterwards?

```json
{ "tool": "pbi_undo_history", "args": { "limit": 5 } }
```

lists the journaled writes, newest first (`id`, `tool`, `time` and the files
each one added, modified or deleted). Then:

```json
{ "tool": "pbi_undo", "args": { "steps": 1 } }
```

```json
{ "undone": [ { "id": "01790691913115651800-218e9b4b", "tool": "pbi_create_measure" } ], "remaining": 0 }
```

`pbi_undo` puts every changed file back **byte for byte** (line endings and
BOM included) and removes files the write added. To group several writes into
one revertible unit, call `pbi_begin_transaction()` first, then `pbi_commit()`
to keep them or `pbi_rollback()` to revert them all. See the
[Safety model](safety.md).

## 9. Reopen in Power BI Desktop

Open the `.pbip` in Desktop. It should load without a repair prompt and show
the new pages and measures. This is the one check that cannot be automated,
and it is worth doing after every session. If something looks wrong, run
`pbi_doctor()` again and see [Recover from trash and backups](cookbook.md#10-recover-from-trash-and-backups).

## Next

- [Cookbook](cookbook.md): ten recipes with the expected tool sequence.
- [Tool reference](tools/model.md): every tool and its parameters.
