"""pbi_doctor for the model server: environment + project health report.

The logic lives in core/doctor.py (shared with the report server); this
module only binds it to the model server's project state.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core import doctor

if TYPE_CHECKING:  # never import model_server.server at import time
    from model_server.server import ModelState


def doctor_report(state: ModelState) -> dict:
    """Health report for the currently selected project (or none)."""
    return doctor.run(state.project, server="pbi-model")


def register(mcp, state, tool) -> None:
    @tool(read=True, idempotent=True)
    def pbi_doctor() -> dict:
        """Diagnose the environment and the selected project (read-only,
        never raises). Reports Python, mcp SDK and pbi-mcp versions; whether
        the project is PBIR + TMDL (editable) or legacy report.json /
        model.bim; declared $schema versions vs the vendored ones; table,
        measure, page and visual counts; backup, undo-journal and trash
        sizes; and (Windows) the installed Power BI Desktop version and
        whether a Desktop instance is running. Works before pbi_set_project
        for the environment checks. Returns {ok, findings[{severity, check,
        message, fix?}], environment, project}; ok is false only if a finding
        has severity 'error'."""
        return doctor_report(state)
