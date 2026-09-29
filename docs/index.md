# pbi-mcp

**MCP servers that read and write Power BI Project (`.pbip`) files on disk.**
Ask an assistant to scaffold a report, bulk-author DAX measures, re-theme a
dashboard or audit a model, and it edits the same files Power BI Desktop
uses. No Power BI API, no sign-in, no cloud.

There are two servers, and a session normally uses both:

| Server | Edits | Typical work |
|---|---|---|
| `pbi-model` | the semantic model, as **TMDL** | measures, columns, relationships, calculation groups, DAX lineage |
| `pbi-report` | the report, as **PBIR** | pages, visuals, formatting, themes, filters, design elements, validation |

Every session starts the same way: call `pbi_set_project(path)` with a
`.pbip` file or project folder (on each server you use), then let the
assistant work. The [Quickstart](quickstart.md) walks through a first session
and the [Cookbook](cookbook.md) has ten worked recipes.

!!! tip "Fastest path"
    `pbi_scaffold_report()` profiles the model and builds a themed,
    navigable, multi-page designed report in one call. Add `dry_run=true` to
    review the proposal first.

## The safety model

The one unforgivable bug for a tool like this is producing a project Power BI
Desktop refuses to open. Every mutation therefore goes through several
independent layers (details on the [Safety model](safety.md) page):

- **Atomic writes.** Temp file plus rename, so a crash never leaves a half
  written file.
- **Backup once per file.** A `*.bak-<timestamp>` snapshot before the first
  edit, with `pbi_list_backups` and `pbi_restore_backup` to recover.
- **Surgical TMDL edits.** The parsed model is never re-emitted, so
  partitions, annotations and M source are preserved byte for byte.
- **Style preservation.** Each file keeps its exact line endings and BOM
  (Desktop mixes CRLF and LF across file types).
- **Schema validation.** Every report write is checked against the official
  Fabric JSON schemas before it reaches disk.
- **Deletion guards.** Deleting a measure that other measures, visuals or
  filters depend on is refused; `force=true` overrides and `dry_run=true`
  previews.
- **Recoverable deletes.** Removed visuals and pages move to
  `Report/.pbi/mcp-trash/`, which Desktop ignores.
- **Dry run.** Write tools accept `dry_run=true`, which runs the operation on
  a scratch copy and returns the unified diff. Nothing is written.
- **Undo journal and transactions.** Every real write is journaled with
  byte-exact pre-images. `pbi_undo` reverts it, and
  `pbi_begin_transaction` / `pbi_commit` / `pbi_rollback` group several
  writes into one revertible unit.

## What this is and is not

!!! note "Scope"
    This builds **structure, speed and consistency**. It does not create
    custom Deneb/Vega visuals, source AppSource visuals, or replace design
    taste. It is a fast report *builder*, not an autonomous report
    *designer*. It also cannot evaluate DAX: Power BI Desktop remains the
    real validator, so reopening the `.pbip` in Desktop is the final manual
    check after every session.

## Where to next

- [Install](install.md): bundles, `uvx`/`pip`, from source, and config
  snippets for Claude Desktop, Claude Code, Cursor and VS Code.
- [Quickstart](quickstart.md): a first session, from `pbi_set_project` to
  `pbi_undo`.
- [Tool reference](tools/model.md): every tool, its parameters and hints
  (generated from the live servers).
- [Cookbook](cookbook.md): prompt recipes with the expected tool sequence,
  plus the built-in MCP resources and prompts.
- [Safety model](safety.md): what protects your project and how to recover.
- [FAQ](faq.md): troubleshooting, including `pbi_doctor`.
