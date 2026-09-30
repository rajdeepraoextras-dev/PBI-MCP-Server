"""Report scaffolding — model profile -> a proposed, designed report (E5 D25-27).

`propose_report` turns a model profile into a page plan the report server
builds through the designed-page template. It's deliberately a *proposal*:
returned as data so the LLM or user can edit it before (or instead of)
building. Smart defaults pick a primary measure, a date trend, and the best
grouping dimensions.
"""

from __future__ import annotations


def _primary_measures(profile: dict, n: int = 4) -> list[str]:
    """Best KPI measures: prefer currency, then base, then ratio."""
    order = {"currency": 0, "base": 1, "ratio": 2, "time_intelligence": 3}
    ranked = sorted(
        profile["measures"],
        key=lambda m: order.get(m["role"], 9))
    seen, out = set(), []
    for m in ranked:
        ref = f"{m['table']}.{m['name']}"
        if ref not in seen:
            seen.add(ref)
            out.append(ref)
        if len(out) >= n:
            break
    return out


def _top_grouping_dims(profile: dict, limit: int = 3) -> list[tuple[str, str]]:
    """(table, column) grouping fields from dimension/fact tables."""
    out = []
    for tname, t in profile["tables"].items():
        if t["kind"] == "date":
            continue
        for col in t["grouping_columns"]:
            out.append((tname, col))
    return out[:limit]


def _dim_labels(dims: list[tuple[str, str]]) -> dict[tuple[str, str], str]:
    """Display name per (table, column): the column name, or "Table Column"
    when several tables share it (two dimensions with a Name column would
    otherwise both produce a page called "Name Detail")."""
    counts: dict[str, int] = {}
    for _, col in dims:
        counts[col] = counts.get(col, 0) + 1
    return {(t, c): (f"{t} {c}" if counts[c] > 1 else c) for t, c in dims}


def _date_field(profile: dict) -> str | None:
    for dt in profile["date_tables"]:
        cols = profile["tables"][dt]["date_columns"]
        if cols:
            return f"{dt}.{cols[0]}"
    return None


def propose_report(profile: dict, *, accent: str = "#1F3A5F",
                   max_detail_pages: int = 3) -> dict:
    """Produce a report proposal: an overview page + per-dimension detail pages."""
    kpis_refs = _primary_measures(profile, 4)
    primary = kpis_refs[0] if kpis_refs else None
    dims = _top_grouping_dims(profile, max_detail_pages)
    date_ref = _date_field(profile)
    labels = _dim_labels(dims)

    pages: list[dict] = []

    # --- Overview -----------------------------------------------------------
    overview_charts = []
    if primary and date_ref:
        overview_charts.append({
            "visual_type": "lineChart",
            "bindings": {"Category": [date_ref], "Y": [primary]},
            "title": f"{primary.split('.')[-1]} over time"})
    if primary and dims:
        dt, dc = dims[0]
        overview_charts.append({
            "visual_type": "clusteredBarChart",
            "bindings": {"Category": [f"{dt}.{dc}"], "Y": [primary]},
            "title": f"{primary.split('.')[-1]} by {labels[(dt, dc)]}"})
    if primary and dims:
        dt, dc = dims[0]
        overview_charts.append({
            "visual_type": "tableEx",
            "bindings": {"Values": [f"{dt}.{dc}"] + kpis_refs[:3]},
            "title": "Detail"})
    pages.append({
        "name": "Overview", "title": "Executive Overview",
        "subtitle": f"{profile['summary']['measures']} measures · "
                    f"{profile['summary']['facts']} fact table(s)",
        "kpis": [{"measure": r, "title": r.split(".")[-1]} for r in kpis_refs],
        "charts": overview_charts,
    })

    # --- per-dimension detail pages ----------------------------------------
    for dt, dc in dims:
        if not primary:
            break
        label = labels[(dt, dc)]
        charts = [
            {"visual_type": "clusteredBarChart",
             "bindings": {"Category": [f"{dt}.{dc}"], "Y": [primary]},
             "title": f"{primary.split('.')[-1]} by {label}"},
            {"visual_type": "tableEx",
             "bindings": {"Values": [f"{dt}.{dc}"] + kpis_refs[:3]},
             "title": f"{label} detail"},
        ]
        pages.append({
            "name": f"{label} Detail", "title": f"{label} Breakdown",
            "subtitle": None,
            "kpis": [{"measure": r, "title": r.split(".")[-1]}
                     for r in kpis_refs[:3]],
            "charts": charts,
        })

    return {
        "accent": accent,
        "theme_mode": "light",
        "add_nav_bar": len(pages) > 1,
        "pages": pages,
        "based_on": profile["summary"],
    }
