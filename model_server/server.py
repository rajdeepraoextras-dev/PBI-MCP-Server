"""Model-layer MCP server.

Tools (Part C.1) — prefix every tool `pbi_` so it never collides with a
sibling MCP server in the same session.

Day 6 (this file):  pbi_set_project, pbi_get_model, pbi_list_measures
Day 7:              pbi_model_lineage
Day 9-12 (write):   pbi_create_measure, pbi_update_measure, pbi_delete_measure,
                    pbi_create_column, pbi_create_relationship,
                    pbi_create_calc_group, pbi_bulk_create_measures

Design: the tool bodies are thin wrappers over plain, directly-testable
functions that take an explicit `ModelState`. FastMCP registration and the
process-wide state live at the bottom. This keeps the logic unit-testable
without spinning up an MCP client.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.mcp_compat import Server

from core.pbip import PbipProject
from core.tooling import load_tool_modules, make_tool, register_journal_tools


@dataclass
class ModelState:
    """Holds the project selected via pbi_set_project for the session."""
    project: PbipProject | None = field(default=None)
    greeted: bool = field(default=False)

    def require(self) -> PbipProject:
        if self.project is None:
            raise ValueError("No project set — call pbi_set_project(path) first.")
        return self.project


# --- tool logic (plain functions; unit-tested directly) --------------------

def set_project(state: ModelState, path: str) -> dict:
    """Select the .pbip project all subsequent model tools operate on."""
    project = PbipProject(path)
    project._require_model()  # fail fast if no SemanticModel resolves
    state.project = project
    tables = project.list_tables()
    result = {
        "ok": True,
        "path": str(project.path),
        "tables": len(tables),
        "measures": sum(len(t.measures) for t in tables),
    }
    if not state.greeted:
        from core.capabilities import GREETING
        result["greeting"] = GREETING
        state.greeted = True
    return result


def get_model(state: ModelState) -> dict:
    """Full model snapshot: tables (+columns/+measures) and relationships."""
    return state.require().get_model()


def list_measures(state: ModelState, table: str | None = None) -> list[dict]:
    """All measures (optionally one table) with their DAX + format string."""
    return [m.model_dump() for m in state.require().list_measures(table)]


def model_lineage(state: ModelState, measure: str | None = None) -> dict:
    """Dependency graph from DAX; with `measure`, that node's deps + dependents."""
    return state.require().model_lineage(measure)


def create_measure(state: ModelState, table: str, name: str, dax: str,
                   format: str | None = None,
                   display_folder: str | None = None) -> dict:
    """Create a new measure (name must be unique across the model)."""
    return state.require().create_measure(table, name, dax, format, display_folder)


def update_measure(state: ModelState, table: str, name: str,
                   dax: str | None = None, format: str | None = None,
                   display_folder: str | None = None) -> dict:
    """Update an existing measure; only the fields you pass change."""
    return state.require().update_measure(table, name, dax, format, display_folder)


def delete_measure(state: ModelState, table: str, name: str,
                   force: bool = False, dry_run: bool = False) -> dict:
    """Delete a measure; refuses if other measures reference it (lineage guard)."""
    return state.require().delete_measure(table, name, force, dry_run)


def create_column(state: ModelState, table: str, name: str, data_type: str,
                  summarize_by: str | None = None,
                  source_column: str | None = None,
                  dax: str | None = None) -> dict:
    """Create a data column (or calculated column when dax is given)."""
    return state.require().create_column(
        table, name, data_type, summarize_by, source_column, dax)


def create_relationship(state: ModelState, from_table: str, from_column: str,
                        to_table: str, to_column: str,
                        cardinality: str | None = None,
                        cross_filter: str | None = None,
                        is_active: bool = True) -> dict:
    """Create a relationship between two existing columns."""
    return state.require().create_relationship(
        from_table, from_column, to_table, to_column,
        cardinality, cross_filter, is_active)


# --- MCP server registration --------------------------------------------------

STATE = ModelState()
from core.capabilities import GREETING

mcp = Server(
    "pbi-model",
    instructions=(
        GREETING + "\n\n"
        "Reads and edits the semantic MODEL layer of a Power BI Project "
        "(.pbip) on disk — measures, columns, relationships, calc groups, DAX "
        "lineage. Call pbi_set_project(path) first. Measure names are unique "
        "model-wide. Deletes are guarded by lineage + report usage; use "
        "dry_run to preview and pbi_list_backups/pbi_restore_backup to recover."
    ),
)
tool = make_tool(mcp, STATE)


@tool(read=True, idempotent=True)
def pbi_set_project(path: str) -> dict:
    """Point the model server at a Power BI Project (.pbip). Call this first."""
    return set_project(STATE, path)


@tool(read=True)
def pbi_get_model() -> dict:
    """Return tables, columns, measures, and relationships for the project."""
    return get_model(STATE)


@tool(read=True)
def pbi_list_measures(table: str | None = None) -> list[dict]:
    """List measures with DAX and format; optionally filter to one table."""
    return list_measures(STATE, table)


@tool(read=True)
def pbi_model_lineage(measure: str | None = None) -> dict:
    """Dependency graph (who references whom). Pass a measure for its deps + dependents."""
    return model_lineage(STATE, measure)


@tool(write=True)
def pbi_create_measure(table: str, name: str, dax: str,
                       format: str | None = None,
                       display_folder: str | None = None) -> dict:
    """Create a measure in `table`. Fails if the measure name already exists."""
    return create_measure(STATE, table, name, dax, format, display_folder)


@tool(write=True, idempotent=True)
def pbi_update_measure(table: str, name: str, dax: str | None = None,
                       format: str | None = None,
                       display_folder: str | None = None) -> dict:
    """Update an existing measure's DAX/format/folder. Omitted fields are kept."""
    return update_measure(STATE, table, name, dax, format, display_folder)


@tool(write=True, destructive=True)
def pbi_delete_measure(table: str, name: str, force: bool = False,
                       dry_run: bool = False) -> dict:
    """Delete a measure. Refuses if other measures OR the report depend on it
    unless force=true. dry_run=true previews without writing."""
    return delete_measure(STATE, table, name, force, dry_run)


@tool(read=True)
def pbi_list_backups() -> list[dict]:
    """List every .bak-* snapshot the server has written, newest first."""
    return STATE.require().list_backups()


@tool(write=True, destructive=True, preview=False)
def pbi_restore_backup(backup: str) -> dict:
    """Restore a backup file (path from pbi_list_backups) over its original."""
    return STATE.require().restore_backup(backup)


@tool(write=True)
def pbi_create_column(table: str, name: str, data_type: str,
                      summarize_by: str | None = None,
                      source_column: str | None = None,
                      dax: str | None = None) -> dict:
    """Create a column in `table`. Pass dax for a calculated column;
    otherwise source_column defaults to the column name."""
    return create_column(STATE, table, name, data_type,
                         summarize_by, source_column, dax)


@tool(write=True)
def pbi_create_relationship(from_table: str, from_column: str,
                            to_table: str, to_column: str,
                            cardinality: str | None = None,
                            cross_filter: str | None = None,
                            is_active: bool = True) -> dict:
    """Create a relationship (many-to-one from -> to by default).
    Both endpoint columns must already exist."""
    return create_relationship(STATE, from_table, from_column,
                               to_table, to_column,
                               cardinality, cross_filter, is_active)


@tool(write=True)
def pbi_create_calc_group(name: str, precedence: int,
                          items: list[dict]) -> dict:
    """Create a calculation group table. items: [{"name","dax"}, ...] using
    SELECTEDMEASURE() patterns; order becomes the display order."""
    return STATE.require().create_calc_group(name, precedence, items)


@tool(write=True)
def pbi_bulk_create_measures(measures: list[dict]) -> dict:
    """Create many measures at once. measures: [{"table","name","dax",
    "format"?,"display_folder"?}, ...]. The whole batch is validated before
    any write happens."""
    return STATE.require().bulk_create_measures(measures)

register_journal_tools(mcp, STATE, tool)
load_tool_modules(__package__ or "model_server", mcp, STATE, tool)


def main() -> None:
    """Entry point (pbi-model-server): run over stdio for an MCP client."""
    mcp.run()


if __name__ == "__main__":
    main()
