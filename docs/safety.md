# Safety model

The one unforgivable bug for a tool like this is producing a project that
Power BI Desktop refuses to open. So no single mechanism is trusted: every
mutation passes through several independent layers, and each layer gives you a
different way back.

| Layer | What it does | How you use it |
|---|---|---|
| [Atomic writes](#atomic-writes) | a crash never leaves a half-written file | automatic |
| [Backups](#backups) | a `*.bak-<timestamp>` copy before the first edit of a file | `pbi_list_backups`, `pbi_restore_backup` |
| [Style preservation](#style-preservation-and-surgical-edits) | keeps line endings, BOM, partitions, annotations, M source | automatic |
| [Schema validation](#schema-validation) | rejects report JSON that Desktop would reject | `pbi_validate_project` |
| [Deletion guards](#deletion-guards) | refuses deletes that break dependents | `force`, `dry_run` |
| [Dry run](#dry-run) | shows the diff of any write without writing it | `dry_run=true` |
| [Undo journal](#undo-journal) | byte-exact pre-images of every real write | `pbi_undo_history`, `pbi_undo` |
| [Transactions](#transactions) | groups writes into one revertible unit | `pbi_begin_transaction`, `pbi_commit`, `pbi_rollback` |
| [Trash](#trash) | deleted visuals and pages are moved, not erased | `pbi_list_trash`, `pbi_restore_visual` |

## Atomic writes

Files are written to a sibling temporary file (`<name>.tmp-<pid>`) in the same
folder and then renamed over the original with an atomic replace. If the
process dies mid-write, the original is still intact and the half-written temp
file is what is left over, never a truncated project file.

## Backups

The first time a file is modified in a server session, a copy is made beside
it: `Sales.tmdl.bak-20260929-101500` (a `-1`, `-2` suffix is added if several
land in the same second). `pbi_list_backups()` lists them, newest first, and
`pbi_restore_backup(backup)` copies one back over its original, atomically.

Backups accumulate. Once you have verified the project in Desktop it is safe to
delete old `*.bak-*` files; `pbi_doctor()` reports how many there are and how
much space they take. They are excluded from undo snapshots. If the project is
under version control, add `*.bak-*` to its `.gitignore`.

## Style preservation and surgical edits

Desktop mixes line endings across file types (TMDL and `report.json` use CRLF,
`page.json` and `visual.json` use LF) and some files carry a BOM. Every write
detects the existing file's line ending and BOM and reproduces them; new files
default to LF without a BOM.

TMDL is edited with **targeted text edits**. The parsed model is never
re-emitted, so partitions, annotations, lineage tags and M source survive byte
for byte. Tests assert that untouched files are binary-identical after every
write, and a seeded fuzz storm asserts the project always reloads.

## Schema validation

Every report-layer JSON written (`report.json`, `pages.json`, `page.json`,
`visual.json`) is validated against the official Microsoft Fabric PBIR schemas
**before** it is written. The schemas are vendored, so this works offline.
Newer format versions that Desktop writes than the published schemas describe
are tolerated (the structure is checked, not the version string).
`pbi_validate_project()` re-checks every report file on demand, and
`pbi_doctor()` tells you whether validation is active in your install and which
`$schema` versions your project declares.

## Deletion guards

`pbi_delete_measure` refuses to delete a measure when:

- other measures reference it, directly or transitively (DAX lineage), or
- the report uses it, either bound directly in a visual or filter or indirectly
  through a measure the report displays.

`force=true` overrides both, and the response lists what you forced past.
`dry_run=true` returns the would-be dependents without deleting. There is no
tool that deletes a column or a table.

## Dry run

Every write tool that edits project files accepts `dry_run=true`:

1. the project folder is copied to a scratch directory (skipping `.pbi/`, the
   undo store, `.git`, `__pycache__`, `.venv`, `node_modules`, `*.bak-*` and
   `*.tmp-*`);
2. the server's project state is temporarily pointed at the copy and the tool
   runs unchanged against it;
3. the response reports what would have happened.

```json
{
  "dry_run": true,
  "result": { "ok": true, "action": "created", "table": "Sales", "name": "Average Sale" },
  "changes": { "added": [], "modified": ["Sales.SemanticModel/definition/tables/Sales.tmdl"], "deleted": [] },
  "diff": "--- a/Sales.SemanticModel/definition/tables/Sales.tmdl\n+++ b/..."
}
```

The real project is never touched, and the preview is not journaled. The diff
is truncated after 60,000 characters.

Two tools have a preview of their own and keep it instead of the generic one:
`pbi_scaffold_report(dry_run=true)` returns the page **proposal**, and
`pbi_delete_measure(dry_run=true)` returns the **dependents**. Both still
write nothing.

!!! warning "Do not preview the restore tools"
    `pbi_restore_visual` and `pbi_restore_backup` take an absolute path *into*
    the project. A dry run works on a scratch copy that cannot see that path
    (the copy leaves out `.pbi/` and `*.bak-*` files), so their preview is
    unreliable, and for `pbi_restore_visual` it can consume the trashed copy.
    Look at `pbi_list_trash()` or `pbi_list_backups()` first and run the
    restore for real.

## Undo journal

Every real write (a call without `dry_run`) is journaled:

- before the tool runs, the project's files are snapshotted; afterwards, the
  differences are computed;
- if anything changed, the **pre-image** of every modified or deleted file is
  stored under `<project root>/.pbi-mcp/undo/<id>/before/`, with a `meta.json`
  (tool name, time and the added, modified and deleted paths);
- `pbi_undo(steps=1)` restores the pre-images byte for byte (line endings and
  BOM included), deletes files the write added and prunes folders left empty;
- `pbi_undo_history(limit=20)` lists the entries, newest first.

Details worth knowing:

- The store sits **next to** `*.Report` and `*.SemanticModel`, so Desktop never
  sees it. It keeps the newest 50 writes.
- Both servers share one store per project, so the history is project-wide no
  matter which server wrote.
- `pbi_undo` and the transaction tools are not journaled themselves, so an undo
  cannot be undone. Preview with `dry_run` first if you are unsure.
- Snapshots skip `.pbi/`, backups, VCS folders and virtualenvs. Add
  `.pbi-mcp/` to your `.gitignore` if the project is under version control.
- Tools annotated read-only are never journaled. If one of them has an option
  that writes as a side effect (for example a generator with `install=true`),
  prefer its two-step form: generate first, then install with a write tool, so
  the install step can be previewed and undone.

## Transactions

`pbi_begin_transaction()` marks a point in the history. Make as many writes as
you like, then:

- `pbi_commit()` closes the transaction and keeps everything, or
- `pbi_rollback()` reverts every write made since the mark and closes it.

Only one transaction can be open at a time, and `pbi_doctor()` warns about one
that has been left open. A transaction is only as deep as the journal, so keep
transactions short and check `pbi_undo_history()` afterwards.

## Trash

`pbi_delete_visual` and `pbi_delete_page` move the folder to
`<name>.Report/.pbi/mcp-trash/` instead of erasing it. Desktop ignores `.pbi/`,
so trashed items never affect the report. `pbi_list_trash()` lists them,
`pbi_restore_visual(trash_path)` puts a visual back on its page, and
`pbi_delete_page` returns the trashed page's path as `recoverable_at`.

## What is not protected

- **DAX correctness.** The server writes the DAX you give it and its linter
  warns about common rejections, but Power BI Desktop is the real validator.
- **Desktop holding the project open.** Desktop does not lock the files, and
  saving from Desktop can overwrite edits made on disk. Close the project before
  editing; `pbi_doctor()` says whether a Desktop instance appears to be running.
- **Edits made outside the tools**, and anything outside the project folder.
- **The final check.** Reopening the `.pbip` in Desktop cannot be automated.

## Health check

`pbi_doctor()` is the one-call summary of this page for the selected project:
formats (PBIR and TMDL versus legacy), schema versions, page and visual folder
integrity, backup, journal and trash sizes, open transactions, and whether
Desktop is installed or running. It is read-only and never raises.
