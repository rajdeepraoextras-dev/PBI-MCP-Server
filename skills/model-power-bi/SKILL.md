---
name: model-power-bi
description: Use when working with the Power BI semantic MODEL — DAX measures, columns, relationships, calculation groups, and dependency lineage. Triggers on "add a measure", "create these DAX measures", "bulk-create measures", "add a relationship", "what depends on this measure", "safe to delete this measure?", "build a time-intelligence calc group", "audit my model". Uses the pbi-model MCP server, which edits TMDL directly and loss-free.
---

# Work with the Power BI Model (TMDL)

The pbi-model server edits the semantic model in a `.pbip` as **surgical,
loss-free text edits** — partitions, annotations, and M source are never
touched. Call `pbi_set_project(path)` first.

## Read before you write

- **`pbi_get_model()`** — tables, columns, measures, relationships.
- **`pbi_list_measures(table?)`** — measures with DAX + format.
- **`pbi_model_lineage(measure?)`** — the dependency graph: what a measure
  depends on AND what depends on it (direct + transitive). Use this before any
  delete or refactor.

## Measures

- **`pbi_create_measure(table, name, dax, format?, display_folder?)`** — name
  must be unique model-wide (enforced).
- **`pbi_update_measure(table, name, dax?, format?, display_folder?)`** —
  partial; omitted fields are preserved.
- **`pbi_bulk_create_measures(measures[])`** — create many at once; the whole
  batch is validated before any write. Use this when the user pastes a list of
  measures — one call, not N.
- **`pbi_delete_measure(table, name, force?, dry_run?)`** — refuses if other
  measures OR report visuals/filters depend on it. Use `dry_run=true` to
  preview, `force=true` only when the user confirms.

## Columns, relationships, calc groups

- **`pbi_create_column(table, name, data_type, summarize_by?, source_column?,
  dax?)`** — pass `dax` for a calculated column.
- **`pbi_create_relationship(from_table, from_column, to_table, to_column,
  ...)`** — both endpoint columns must exist; duplicates are rejected.
- **`pbi_create_calc_group(name, precedence, items)`** — items are
  `[{"name": "YTD", "dax": "..."}]` using `SELECTEDMEASURE()`. Great for
  time-intelligence patterns applied across all measures.

## Auditing

- **`pbi_model_usage()`** (report server) classifies every field as
  **direct / indirect / unused** — the safe basis for cleanup.
- **`pbi_profile_model()`** classifies fact/dimension/date tables and measure
  roles (ratio/currency/time-intelligence) — useful context before building.

## Rules of thumb

- Measure names are globally unique; columns disambiguate by table.
- Reference measures bare (`[Revenue]`), columns qualified (`Sales[Amount]`).
- Never hand-delete; the guards exist to protect the report — respect them,
  and use `dry_run` to show consequences.
- Recover anything with `pbi_list_backups` + `pbi_restore_backup`.
