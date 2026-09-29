# FAQ

## Basics

### Do I need Power BI Desktop?

To *edit*, no: the servers change plain files on disk and never call Desktop.
To *check* the result, yes. Desktop is the real validator for DAX and for the
final "does it open cleanly" test, so reopen the `.pbip` in Desktop after every
session.

### Does it work with `.pbix` files or reports already in the Power BI service?

No. It works on the **Power BI Project** format (`.pbip`), a folder of text
files. Open a `.pbix` in Desktop and use *Save as* to create a `.pbip`.

### Which project formats are supported?

The **PBIR** report format (a `definition/` folder with `pages/`) and the
**TMDL** semantic model format (a `definition/` folder with `tables/*.tmdl`).
Legacy single-file `report.json` and `model.bim` projects are detected by
`pbi_doctor()` and not edited. To convert, enable the matching options under
*Options > Preview features* in Desktop (enhanced report metadata format PBIR;
semantic model in TMDL format) and save the project again.

### Do I need both servers?

Use what the job needs. Model work (measures, relationships, calculation
groups) needs only `pbi-model`. Report work needs only `pbi-report`. A few
things use both: `pbi_delete_measure` consults the report's usage when the
project has a report, and audits combine `pbi_model_lineage` with
`pbi_model_usage`. Each server needs its own `pbi_set_project(path)` call.

### Does it make network calls or need an account?

The model and report servers make none, and need no sign-in. Everything happens
on local files. Only the optional [cloud server](cloud.md) talks to the network.

### Which operating systems work?

The servers are pure Python file editing and run anywhere Python 3.11+ runs.
Power BI Desktop itself is Windows-only, so the Desktop detection in
`pbi_doctor()` only runs on Windows. The standalone bundle is a Windows build.

## Safety and recovery

### I made a change I don't want. How do I get back?

In order of convenience: `pbi_undo` (revert the latest write or writes), a
transaction rollback, the trash for deleted visuals and pages, then a
`*.bak-<timestamp>` backup. See
[Recover from trash and backups](cookbook.md#10-recover-from-trash-and-backups)
and the [Safety model](safety.md).

### Power BI Desktop won't open the project after an edit.

1. Run `pbi_doctor()` and `pbi_validate_project()` and read every warning and
   error; page-folder and schema problems show up there.
2. Check `pbi_undo_history()`; undo the last write or two and try again.
3. If needed, restore the affected file from its `*.bak-*` backup
   (`pbi_list_backups`, `pbi_restore_backup`), or from version control.
4. If the cause is a bug in this project, please open an issue with the
   `pbi_doctor()` output and the tool call that preceded it.

### Should I commit the undo journal?

No. The journal lives in `.pbi-mcp/` at the project root; add it (and
`*.bak-*`) to the project's `.gitignore`. Commit the project itself, though:
version control is the best long-term backup.

### Why did Desktop not show my changes?

Desktop reads the files when it opens a project. Close the `.pbip` before the
assistant edits it, and reopen it afterwards. Saving from an open Desktop
window can also overwrite what was written on disk.

### After `pbi_undo` there is an odd extra page or an empty folder.

Run `pbi_doctor()`. A page folder with no `page.json` (typically holding only a
`*.bak-*` file left by an interrupted or undone write) is reported under the
`page_integrity` check with the folder path and a fix. Delete that folder once
you have confirmed it holds nothing you need. Empty `.pbi-mcp` folders are
harmless.

## Using it

### Can it run DAX or refresh my data?

No. It does not connect to Analysis Services, so it cannot evaluate DAX or load
data. It writes the DAX you give it, and a linter warns about the common
patterns Power BI rejects; Desktop is the real validator. Refreshing published
models is what the optional [cloud server](cloud.md) is for.

### Can it make custom visuals such as Deneb or AppSource visuals?

Not natively; those are out of scope. The escape hatch is `pbi_add_visual_raw`:
take a real `visual.json` (export one from Desktop, or read one with
`pbi_get_visual`), tweak it, and add it. The JSON is still validated against the
schema before it is written.

### Which tool should I start with?

`pbi_set_project(path)`, then `pbi_doctor()`. For a whole report,
`pbi_scaffold_report(dry_run=true)`. For a single designed page,
`pbi_build_designed_page`. For a list of measures, `pbi_bulk_create_measures`.
The [Cookbook](cookbook.md) has the full sequences.

### How do I use the built-in prompts and resources?

Hosts that support MCP prompts show `audit_model`, `bulk_measures`,
`build_dashboard`, `theme_report` and `review_page` as ready-made recipes.
Resources such as `pbip://model` and `pbip://pages` can be attached to a
conversation without a tool call. See
[Built-in resources and prompts](cookbook.md#built-in-resources-and-prompts).

### Why are some tools marked "destructive" or "read-only"?

They are MCP annotation hints. Hosts can auto-approve read-only tools and ask
before destructive ones. They describe the tool, not a guarantee: the real
protection is the [safety model](safety.md).

## Troubleshooting with `pbi_doctor`

`pbi_doctor()` is read-only and never raises, so it is always safe to run. It
works before a project is selected (environment checks only) and returns
`{ok, findings, environment, project}`. `ok` is `false` only when a finding has
severity `error`.

| Check | What it tells you |
|---|---|
| `python`, `mcp` | interpreter version, mcp SDK version and major, and the installed pbi-mcp package version |
| `schema_validation` | whether report writes are pre-flight validated in this install |
| `desktop`, `desktop_instance` | Windows: installed Desktop version (MSI and Store), and whether a Desktop session appears to be running |
| `project_layout`, `dataset_reference` | the `.pbip`, `*.Report` and `*.SemanticModel` folders, and what the report is bound to |
| `report_format`, `model_format` | PBIR or legacy report; TMDL or legacy model |
| `page_integrity` | page or visual folders that are incomplete, and `pageOrder` entries with no folder |
| `schema_versions` | `$schema` versions in your project against the vendored ones |
| `counts` | tables, columns, measures, pages, visuals |
| `backups`, `journal`, `trash` | sizes of the recovery stores; open transactions |

### Common findings

- **"Report is in the legacy single report.json format".** Convert the project
  to PBIR in Desktop (see above); the report tools cannot edit it.
- **"Semantic model is a legacy model.bim".** Convert to TMDL the same way.
- **"jsonschema (or resources/schemas) missing".** Report writes are not
  validated. Install `jsonschema`, or use a source install or bundle that
  includes the vendored schemas.
- **"Power BI Desktop appears to be running".** Close the project in Desktop
  before editing, then reopen it.
- **"A transaction has been open since ...".** Call `pbi_commit` to keep its
  writes or `pbi_rollback` to revert them.
- **"pages.json lists page(s) with no folder".** A page folder is missing.
  Restore it with `pbi_undo` or a backup, or remove the id from `pageOrder`.

## Configuration

### My host can't start the server: paths with spaces or backslashes.

In JSON config files, escape backslashes (`C:\\Users\\me\\...`) and keep the
whole path in one string. Give the interpreter path in `command` and the
module in `args`, as in the [Install](install.md) examples.

### Which mcp version does it use?

Whichever is installed, from `mcp>=1.2.0,<3`: `FastMCP` on 1.x and `MCPServer`
on 2.x, chosen automatically. `pbi_doctor()` reports which.
