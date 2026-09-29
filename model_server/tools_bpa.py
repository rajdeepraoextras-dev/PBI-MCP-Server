"""Best Practice Analyzer tools: pbi_bpa, pbi_bpa_fix, pbi_bpa_rules.

The logic lives in ``core.bpa``; the functions here take an explicit ``state``
(the server's ``ModelState``) so they are unit-testable without an MCP client.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core import bpa

if TYPE_CHECKING:  # pragma: no cover
    from model_server.server import ModelState


# --- tool logic (plain functions) --------------------------------------------

def run_bpa(state: ModelState, categories: list[str] | None = None,
            severity_min: int = 1, table: str | None = None,
            rules: list[str] | None = None,
            exclude_rules: list[str] | None = None,
            max_findings: int | None = 500) -> dict:
    """Analyze the selected project; see ``core.bpa.analyze``."""
    return bpa.analyze(state.require(), categories=categories,
                       severity_min=severity_min, table=table, rules=rules,
                       exclude_rules=exclude_rules, max_findings=max_findings)


def run_bpa_fix(state: ModelState, rules: list[str] | None = None,
                table: str | None = None) -> dict:
    """Apply the safe fixers; see ``core.bpa.apply_fixes``."""
    return bpa.apply_fixes(state.require(), rules=rules, table=table)


def list_bpa_rules(state: ModelState) -> dict:
    """The rule catalog (plus the project's custom rules when one is selected)."""
    project = getattr(state, "project", None)
    catalog, warnings = bpa.all_rules(project)
    rules = [r.listing() for r in catalog]
    return {
        "count": len(rules),
        "categories": list(bpa.CATEGORIES),
        "rules": rules,
        "custom_rules_file": str(bpa.CUSTOM_RULES_FILE.as_posix()),
        "warnings": warnings,
    }


# --- MCP registration ---------------------------------------------------------

def register(mcp, state, tool) -> None:
    @tool(read=True)
    def pbi_bpa(categories: list[str] | None = None, severity_min: int = 1,
                table: str | None = None, rules: list[str] | None = None,
                exclude_rules: list[str] | None = None,
                max_findings: int = 500) -> dict:
        """Best Practice Analyzer: check the semantic model against ~60 rules
        ported from Tabular Editor / Microsoft's standard BPA (performance, DAX,
        error prevention, maintenance, naming, formatting) and any custom rules
        in <project>/.pbi-mcp/bpa_rules.json. Read-only.

        Each finding has rule_id, category, severity (3 = most severe), the
        object it is about (object_type, table, name), a fix-oriented message and
        `fixable` (pbi_bpa_fix can repair it). The response also carries a
        summary per category / severity / rule and `fixable_count`.

        Filters: `categories` (Performance, DAX Expressions, Error Prevention,
        Maintenance, Naming Conventions, Formatting), `severity_min` (1-3),
        `table`, `rules` / `exclude_rules` (rule IDs or aliases; see
        pbi_bpa_rules). Usage-based rules (unused columns / measures) need a
        report layer. At most `max_findings` findings are returned; the summary
        always counts everything."""
        return run_bpa(state, categories, severity_min, table, rules,
                       exclude_rules, max_findings)

    @tool(write=True, destructive=False, idempotent=True)
    def pbi_bpa_fix(rules: list[str] | None = None, table: str | None = None) -> dict:
        """Apply the Best Practice Analyzer's safe automatic fixes and report
        what changed. Each fix inserts a single property line into the table's
        TMDL file and nothing else: a default format string on numeric measures
        that lack one (#,0, or 0.0% for percent-like names), isHidden on
        foreign-key columns, and dataCategory on columns named Latitude,
        Longitude, WebUrl or ImageUrl. Re-running is a no-op. `rules` limits the
        fixes to those rule IDs; `table` to one table."""
        return run_bpa_fix(state, rules, table)

    @tool(read=True)
    def pbi_bpa_rules() -> dict:
        """List every Best Practice Analyzer rule: id, aliases, category,
        severity, description, scopes, whether pbi_bpa_fix can repair it and
        whether it needs a report layer. Includes the project's custom rules
        when a project is selected."""
        return list_bpa_rules(state)
