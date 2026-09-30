"""MCP resources and prompts for the model server.

Resources expose read-only views of the selected project as JSON text so a
host can attach them to a conversation without a tool call:

    pbip://model                   compact model overview
    pbip://model/measures          every measure with DAX + format
    pbip://model/lineage           the DAX dependency graph
    pbip://model/tables/{table}    one table: columns, measures, relationships

Every resource answers with ``{"error": ...}`` (never an exception) when no
project is selected. Prompts (``audit_model``, ``bulk_measures``) hand the
model a step-by-step recipe that names the exact tools to call.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from urllib.parse import unquote

if TYPE_CHECKING:  # never import model_server.server at import time
    from model_server.server import ModelState

NO_PROJECT = ("No project selected. Call pbi_set_project(path) with a .pbip "
              "file or project folder first, then read this resource again.")


# --- resource logic (plain functions; unit-tested directly) ------------------

def _json(obj) -> str:
    return json.dumps(obj, indent=2, default=str)


def _safe(state: ModelState, build) -> str:
    """JSON text of ``build(project)``, or a JSON error object."""
    if state.project is None:
        return _json({"error": NO_PROJECT})
    try:
        return _json(build(state.project))
    except Exception as exc:  # noqa: BLE001 - a resource read must not raise
        return _json({"error": f"{type(exc).__name__}: {exc}"})


def model_overview(state: ModelState) -> str:
    """Compact overview: names only. Details live in the other resources."""
    def build(project):
        tables = project.list_tables()
        rels = project.list_relationships()
        return {
            "path": str(project.path),
            "counts": {
                "tables": len(tables),
                "columns": sum(len(t.columns) for t in tables),
                "measures": sum(len(t.measures) for t in tables),
                "relationships": len(rels),
            },
            "tables": [{
                "name": t.name,
                "is_hidden": t.is_hidden,
                "is_calc_group": t.is_calc_group,
                "columns": [c.name for c in t.columns],
                "measures": [m.name for m in t.measures],
            } for t in tables],
            "relationships": [r.model_dump() for r in rels],
            "see_also": ["pbip://model/measures", "pbip://model/lineage",
                         "pbip://model/tables/{table}"],
        }
    return _safe(state, build)


def model_measures(state: ModelState) -> str:
    def build(project):
        measures = [m.model_dump() for m in project.list_measures()]
        return {"count": len(measures), "measures": measures}
    return _safe(state, build)


def model_lineage_graph(state: ModelState) -> str:
    return _safe(state, lambda project: project.model_lineage())


def model_table(state: ModelState, table: str) -> str:
    def build(project):
        tables = {t.name: t for t in project.list_tables()}
        name = table if table in tables else unquote(table)
        if name not in tables:
            return {"error": f"Table {table!r} not found.",
                    "available": sorted(tables)}
        t = tables[name]
        return {
            "name": t.name,
            "is_hidden": t.is_hidden,
            "is_calc_group": t.is_calc_group,
            "columns": [c.model_dump() for c in t.columns],
            "measures": [m.model_dump() for m in t.measures],
            "relationships": [r.model_dump()
                              for r in project.list_relationships()
                              if name in (r.from_table, r.to_table)],
        }
    return _safe(state, build)


# --- prompt text ---------------------------------------------------------------

AUDIT_MODEL = """\
Audit the semantic model of the selected Power BI project. Analysis first: \
change nothing until I confirm.

Setup
1. pbi_set_project(path) on this server, and on the pbi-report server too \
(report-usage checks need it), if not already done.
2. pbi_doctor() - stop and fix any finding with severity 'error' (legacy \
model.bim or report.json, missing folders) before going on.

Inspect the model (pbi-model)
3. pbi_get_model() - tables, columns, measures, relationships. Note tables \
with no relationships, measures parked in odd tables, and hidden objects.
4. pbi_list_measures() - read every DAX expression. Look for duplicated \
logic, hard-coded constants, divisions without DIVIDE, missing format \
strings and measures with no display folder.
5. pbi_model_lineage() - the full dependency graph. Collect (a) leaf \
measures that nothing references, (b) any 'unresolved' references (typos or \
broken DAX), (c) unusually long dependency chains.

Check the report (pbi-report)
6. pbi_model_usage() - classifies every measure and column as direct, \
indirect or unused in the report.
7. pbi_validate_project() - confirms the report JSON is schema-valid.

Find safe cleanup candidates
8. A measure is a removal candidate only if it is unused in the report AND \
is a leaf in the lineage graph. 'Unused in this report' is not 'unused \
everywhere': other reports may be bound to this model.
9. For each candidate run pbi_delete_measure(table, name, dry_run=true) to \
see its dependents and report usage without changing anything.

Report back
10. Give a short, prioritised summary: schema validity, unused fields, risky \
measures (bound in visuals), broken references and suggested fixes. Then ask \
which items to act on.

If I approve deletions
- pbi_begin_transaction(), then pbi_delete_measure(table, name) for each \
one (never force=true unless I explicitly accept the dependents it lists), \
then pbi_validate_project(). Keep everything with pbi_commit() or revert the \
whole batch with pbi_rollback().
- pbi_undo() reverts the latest single write and pbi_undo_history() lists \
what can be undone. pbi_list_backups() / pbi_restore_backup(backup) is the \
file-level safety net.
"""

BULK_MEASURES = """\
Create many measures from the specification below in one safe batch.

Specification:
{spec}

Steps
1. pbi_set_project(path) if not already done, then pbi_doctor() once.
2. pbi_get_model() and pbi_list_measures() - learn the real table and column \
names and which measure names are already taken. Measure names are unique \
across the whole model.
3. Turn the specification into a list of {{"table", "name", "dax", \
"format"?, "display_folder"?}} objects. Resolve every table against \
pbi_get_model(); write measure references as [Measure] and columns as \
Table[Column]. Ask me about anything ambiguous instead of guessing.
4. Check the DAX against what Power BI rejects: no measure inside a \
CALCULATE boolean filter (wrap it in FILTER), no empty arguments (",,"), and \
time intelligence needs a marked Date table.
5. pbi_bulk_create_measures(measures=[...], dry_run=true) - read the \
returned diff. The batch is validated as a whole (duplicate names, unknown \
tables) before anything is written.
6. Run pbi_bulk_create_measures(measures=[...]) for real. If the response \
carries 'warnings', fix those measures with pbi_update_measure(table, name, \
dax=...) and check again.
7. Verify with pbi_model_lineage(): the new measures must show no \
'unresolved' references.
8. Summarise what was created per table and which warnings you fixed.

Recovery: one bulk call is one undo step. pbi_undo() takes the whole batch \
back and pbi_undo_history() confirms it. Nothing is written by a dry_run.
"""


def audit_model_text() -> str:
    return AUDIT_MODEL


def bulk_measures_text(spec: str) -> str:
    return BULK_MEASURES.format(spec=spec.strip() or "(none given - ask me)")


# --- registration ---------------------------------------------------------------

def register(mcp, state, tool) -> None:
    @mcp.resource("pbip://model", name="model_overview",
                  description="Compact overview of the selected semantic "
                              "model: tables with column and measure names, "
                              "relationships and counts.",
                  mime_type="application/json")
    def model_overview_resource() -> str:
        return model_overview(state)

    @mcp.resource("pbip://model/measures", name="model_measures",
                  description="Every measure with its DAX, format string and "
                              "display folder.",
                  mime_type="application/json")
    def model_measures_resource() -> str:
        return model_measures(state)

    @mcp.resource("pbip://model/lineage", name="model_lineage",
                  description="DAX dependency graph: what each measure "
                              "depends on and who references it.",
                  mime_type="application/json")
    def model_lineage_resource() -> str:
        return model_lineage_graph(state)

    @mcp.resource("pbip://model/tables/{table}", name="model_table",
                  description="One table: full column and measure "
                              "definitions plus its relationships.",
                  mime_type="application/json")
    def model_table_resource(table: str) -> str:
        return model_table(state, table)

    @mcp.prompt()
    def audit_model() -> str:
        """Audit the semantic model: lineage, unused fields, risky measures
        and safe removal candidates. Read-only until you approve changes."""
        return audit_model_text()

    @mcp.prompt()
    def bulk_measures(spec: str) -> str:
        """Create many DAX measures from a written specification in one
        validated, previewable, undoable batch."""
        return bulk_measures_text(spec)
