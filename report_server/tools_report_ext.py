"""Report-level measures (reportExtensions.json) and phone layout tools.

Logic lives in core/report_ext.py; this module holds the state-taking
functions and the thin ``pbi_*`` MCP wrappers. It never imports
report_server.server (the server loads this module).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core import report_ext

if TYPE_CHECKING:  # pragma: no cover
    from report_server.server import ReportState


# --- logic (explicit state) ---------------------------------------------------

def create_report_measure(state: "ReportState", table: str, name: str, dax: str,
                          format_string: str | None = None,
                          description: str | None = None,
                          data_type: str | None = None) -> dict:
    return report_ext.create_report_measure(
        state.require(), table, name, dax, format_string, description, data_type)


def list_report_measures(state: "ReportState") -> list[dict]:
    return report_ext.list_report_measures(state.require())


def update_report_measure(state: "ReportState", table: str, name: str,
                          dax: str | None = None,
                          format_string: str | None = None,
                          description: str | None = None) -> dict:
    return report_ext.update_report_measure(
        state.require(), table, name, dax, format_string, description)


def delete_report_measure(state: "ReportState", table: str, name: str,
                          force: bool = False) -> dict:
    return report_ext.delete_report_measure(state.require(), table, name, force)


def set_mobile_layout(state: "ReportState", page_id: str, mode: str = "auto",
                      visuals: list[dict] | None = None) -> dict:
    return report_ext.set_mobile_layout(state.require(), page_id, mode, visuals)


def get_mobile_layout(state: "ReportState", page_id: str) -> dict:
    return report_ext.get_mobile_layout(state.require(), page_id)


# --- MCP wrappers ---------------------------------------------------------------

def register(mcp, state, tool) -> None:
    @tool(write=True)
    def pbi_create_report_measure(table: str, name: str, dax: str,
                                  format_string: str | None = None,
                                  description: str | None = None,
                                  data_type: str | None = None) -> dict:
        """Create a report-level measure (stored in the report's
        definition/reportExtensions.json, created if absent, not in the
        semantic model). `table` must be an existing model table; `name` must
        be unique across model and report measures. data_type is one of
        Double (default), Integer, Decimal, Text, Boolean, Date, DateTime.
        Visuals bind it like any measure ("Table.Name"). Returns the created
        measure plus any DAX warnings."""
        return create_report_measure(state, table, name, dax, format_string,
                                     description, data_type)

    @tool(read=True, idempotent=True)
    def pbi_list_report_measures() -> list[dict]:
        """List the report-level measures in reportExtensions.json: table,
        name, dax, data_type, format_string, description. Empty when the
        report has none."""
        return list_report_measures(state)

    @tool(write=True, idempotent=True)
    def pbi_update_report_measure(table: str, name: str, dax: str | None = None,
                                  format_string: str | None = None,
                                  description: str | None = None) -> dict:
        """Update a report-level measure; omitted fields are kept and an empty
        string clears format_string or description. Returns the updated
        measure and any DAX warnings."""
        return update_report_measure(state, table, name, dax, format_string,
                                     description)

    @tool(write=True, destructive=True)
    def pbi_delete_report_measure(table: str, name: str,
                                  force: bool = False) -> dict:
        """Delete a report-level measure. Refused while a visual, filter or
        bookmark binds it, or another report measure's DAX references it,
        unless force=true. Returns what was removed and anything forced
        past."""
        return delete_report_measure(state, table, name, force)

    @tool(write=True, idempotent=True)
    def pbi_set_mobile_layout(page_id: str, mode: str = "auto",
                              visuals: list[dict] | None = None) -> dict:
        """Set a page's phone layout (per-visual mobile.json; report.json
        layoutOptimization is kept in sync). mode='auto' stacks the page's
        visible visuals top-left first on the 320-wide phone canvas keeping
        aspect ratios (min height 80); mode='manual' takes
        visuals=[{"visual_id","x","y","width","height"}] (x+width <= 320);
        mode='off' removes the phone layout. Returns the resulting layout."""
        return set_mobile_layout(state, page_id, mode, visuals)

    @tool(read=True, idempotent=True)
    def pbi_get_mobile_layout(page_id: str) -> dict:
        """A page's phone layout: enabled flag, canvas size, the placed
        visuals with their mobile x/y/width/height (top to bottom), and the
        visuals that have no phone placement."""
        return get_mobile_layout(state, page_id)
