"""Project diff — what changed between two .pbip projects (E6 D29).

Compares the model (measures, columns, relationships) and the report (pages,
visuals) of two PbipProjects. Useful for review: diff a working copy against
a backup, or two versions of a report.
"""

from __future__ import annotations


def _measure_map(project) -> dict:
    return {f"{m.table}.{m.name}": (m.dax, m.format_string)
            for m in project.list_measures()}


def _visual_map(project) -> dict:
    out = {}
    for page in project.list_pages():
        for v in project.list_visuals(page.id):
            out[f"{page.id}/{v.id}"] = v.visual_type
    return out


def _diff_keys(a: dict, b: dict) -> dict:
    added = sorted(set(b) - set(a))
    removed = sorted(set(a) - set(b))
    changed = sorted(k for k in set(a) & set(b) if a[k] != b[k])
    return {"added": added, "removed": removed, "changed": changed}


def diff_projects(base, other) -> dict:
    """Structured diff base -> other."""
    measures = _diff_keys(_measure_map(base), _measure_map(other))
    pages = _diff_keys({p.id: p.name for p in base.list_pages()},
                       {p.id: p.name for p in other.list_pages()})
    visuals = _diff_keys(_visual_map(base), _visual_map(other))

    def rel_key(project):
        return {f"{r.from_table}.{r.from_column}->{r.to_table}.{r.to_column}"
                for r in project.list_relationships()}
    rb, ro = rel_key(base), rel_key(other)

    total = sum(len(v) for d in (measures, pages, visuals)
                for v in (d["added"], d["removed"], d["changed"]))
    return {
        "measures": measures,
        "pages": pages,
        "visuals": visuals,
        "relationships": {"added": sorted(ro - rb),
                          "removed": sorted(rb - ro)},
        "total_changes": total + len(ro ^ rb),
        "identical": total == 0 and rb == ro,
    }
