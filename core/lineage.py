"""Model lineage — the who-references-whom dependency graph, parsed from DAX.

Powers `pbi_model_lineage` (Part C.1) and, crucially, the delete fail-safe
later (`pbi_delete_measure` must refuse to remove a referenced object, M6).

How references are read from DAX:
  * `[Name]`            — bare bracket: a measure ref, else a column of the
                          measure's home table.
  * `Table[Column]`     — qualified: a column ref (or a qualified measure).
  * `'Table Name'[Col]` — quoted table qualifier.

We rely on two model facts: measure names are globally unique in a Power BI
model (so measures key by name), and columns disambiguate by their table.
References come from the tokenizer in `core.dax_parser`, so text inside
string literals and comments never looks like a reference, and keywords
(`RETURN [X]`, `NOT [Flag]`) are never mistaken for table qualifiers.
"""

from __future__ import annotations

from core.dax_parser import parse_references


def extract_references(dax: str) -> list[tuple[str | None, str]]:
    """Return (table_qualifier_or_None, bracketed_name) pairs found in `dax`.

    Qualified refs come first, then bare ones; each is listed once, sorted.
    """
    refs = parse_references(dax)
    out: list[tuple[str | None, str]] = sorted(refs.columns)
    out.extend((None, name) for name in sorted(refs.measures))
    return out


def build_lineage(tables, measures) -> dict[str, dict]:
    """Build {measure_name: {table, measures[], columns[], unresolved[]?}}.

    `tables` is a list of Table, `measures` a list of Measure.
    """
    measure_names = {m.name for m in measures}
    cols_by_table: dict[str, set[str]] = {
        t.name: {c.name for c in t.columns} for t in tables
    }

    graph: dict[str, dict] = {}
    for m in measures:
        dep_measures: set[str] = set()
        dep_columns: set[str] = set()
        unresolved: set[str] = set()

        for qualifier, name in extract_references(m.dax):
            if qualifier:
                if name in cols_by_table.get(qualifier, set()):
                    dep_columns.add(f"{qualifier}.{name}")
                elif name in measure_names:
                    dep_measures.add(name)
                else:
                    # qualified but unknown — most likely a column we can't see
                    dep_columns.add(f"{qualifier}.{name}")
            else:
                if name != m.name and name in measure_names:
                    dep_measures.add(name)
                elif name in cols_by_table.get(m.table, set()):
                    dep_columns.add(f"{m.table}.{name}")
                elif name in measure_names:
                    dep_measures.add(name)
                else:
                    unresolved.add(name)

        node = {
            "table": m.table,
            "measures": sorted(dep_measures),
            "columns": sorted(dep_columns),
        }
        if unresolved:
            node["unresolved"] = sorted(unresolved)
        graph[m.name] = node
    return graph


def dependents_of(graph: dict[str, dict]) -> dict[str, list[str]]:
    """Reverse edges: {measure_name: [measures that reference it]}."""
    rev: dict[str, set[str]] = {name: set() for name in graph}
    for name, node in graph.items():
        for dep in node["measures"]:
            if dep in rev:
                rev[dep].add(name)
    return {k: sorted(v) for k, v in rev.items()}


def transitive_dependents(graph: dict[str, dict], measure: str) -> list[str]:
    """All measures that reference `measure` directly or indirectly."""
    rev = dependents_of(graph)
    seen: set[str] = set()
    stack = list(rev.get(measure, []))
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(rev.get(cur, []))
    return sorted(seen)
