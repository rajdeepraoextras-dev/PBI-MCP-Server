"""Field-usage classification — the deletion fail-safe (Part C.2).

Classifies every model field (column / measure) as:
  * direct   — referenced by the report itself (visual bindings, filters,
               sorts, conditional formatting, page/report filters)
  * indirect — not direct, but reachable from a directly-used measure through
               the DAX dependency graph (so deleting it would break a visual)
  * unused   — neither

Direct refs are found by walking the raw PBIR JSON for the universal field
pattern {"...": {"Expression": {"SourceRef": {"Entity": T}}, "Property": P}}
— that shape appears wherever PBIR references a model field, so this catches
bindings AND filters/sorts/formatting without bucket-specific knowledge.
"""

from __future__ import annotations

import json
from pathlib import Path

from core.lineage import build_lineage


def _walk_field_refs(node) -> set[tuple[str, str]]:
    """Recursively collect (Entity, Property) pairs from a PBIR JSON tree."""
    refs: set[tuple[str, str]] = set()
    if isinstance(node, dict):
        expr = node.get("Expression")
        prop = node.get("Property")
        if isinstance(expr, dict) and isinstance(prop, str):
            entity = (expr.get("SourceRef") or {}).get("Entity")
            if isinstance(entity, str):
                refs.add((entity, prop))
        for v in node.values():
            refs |= _walk_field_refs(v)
    elif isinstance(node, list):
        for v in node:
            refs |= _walk_field_refs(v)
    return refs


def collect_direct_refs(project) -> set[tuple[str, str]]:
    """(Entity, Property) pairs referenced anywhere in the report layer."""
    report_def = project._require_report() / "definition"
    refs: set[tuple[str, str]] = set()

    # report.json + every page.json carry report/page-level filters
    for jf in [report_def / "report.json"]:
        if jf.exists():
            refs |= _walk_field_refs(json.loads(jf.read_text(encoding="utf-8-sig")))
    pages_dir = report_def / "pages"
    if pages_dir.is_dir():
        for jf in pages_dir.rglob("*.json"):
            refs |= _walk_field_refs(json.loads(jf.read_text(encoding="utf-8-sig")))
    return refs


def classify_usage(project) -> dict:
    """Classify every model field as direct / indirect / unused."""
    tables = project.list_tables()
    measures = project.list_measures()
    graph = build_lineage(tables, measures)
    measure_names = {m.name for m in measures}
    measure_table = {m.name: m.table for m in measures}

    direct_refs = collect_direct_refs(project)

    direct_measures: set[str] = set()
    direct_columns: set[str] = set()
    for entity, prop in direct_refs:
        if prop in measure_names and measure_table[prop] == entity:
            direct_measures.add(prop)
        else:
            direct_columns.add(f"{entity}.{prop}")

    # transitive closure from directly-used measures
    indirect_measures: set[str] = set()
    indirect_columns: set[str] = set()
    stack = list(direct_measures)
    seen: set[str] = set()
    while stack:
        cur = stack.pop()
        if cur in seen or cur not in graph:
            continue
        seen.add(cur)
        node = graph[cur]
        for dep in node["measures"]:
            if dep not in direct_measures:
                indirect_measures.add(dep)
            stack.append(dep)
        for col in node["columns"]:
            if col not in direct_columns:
                indirect_columns.add(col)

    # relationships keep join columns alive too — count them as indirect
    for r in project.list_relationships():
        for ref in (f"{r.from_table}.{r.from_column}",
                    f"{r.to_table}.{r.to_column}"):
            if ref not in direct_columns:
                indirect_columns.add(ref)

    all_columns = {
        f"{t.name}.{c.name}" for t in tables for c in t.columns
    }
    unused_measures = sorted(
        measure_names - direct_measures - indirect_measures)
    unused_columns = sorted(
        all_columns - direct_columns - indirect_columns)

    return {
        "direct": {"measures": sorted(direct_measures),
                   "columns": sorted(direct_columns & all_columns)},
        "indirect": {"measures": sorted(indirect_measures),
                     "columns": sorted(indirect_columns & all_columns)},
        "unused": {"measures": unused_measures, "columns": unused_columns},
        "counts": {
            "direct": len(direct_measures) + len(direct_columns & all_columns),
            "indirect": len(indirect_measures) + len(indirect_columns & all_columns),
            "unused": len(unused_measures) + len(unused_columns),
        },
    }
