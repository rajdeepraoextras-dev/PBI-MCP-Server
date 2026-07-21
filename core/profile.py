"""Model profiling — understand a model well enough to design a report (E5 D24).

Classifies, from structure alone (no data access):
  * tables    : fact (many-side of relationships, has measures) vs dimension
                vs date (dataCategory Time / a Date-like key)
  * measures  : ratio (%/decimal format), currency, time-intelligence
                (name/DAX hints), or base
  * columns   : date, geography (dataCategory), category (low-ish cardinality
                text/key), or numeric

Drives pbi_scaffold_report. Everything is a best-effort heuristic surfaced to
the LLM/user for editing — never an irreversible decision.
"""

from __future__ import annotations

import re

_TIME_INTEL = re.compile(
    r"\b(YTD|QTD|MTD|YoY|MoM|QoQ|SPLY|LY|PY|prior year|previous year|"
    r"same period|running total|cumulative|rolling)\b", re.IGNORECASE)
_GEO_CATEGORIES = {"Address", "City", "Continent", "Country", "County",
                   "Latitude", "Longitude", "Place", "PostalCode",
                   "StateOrProvince", "Region", "WebUrl"}
# technical columns that are keys/sequences, not user-facing dimensions
_TECHNICAL_SUFFIX = re.compile(r"(seq|id|key|number|code|guid|sk)$",
                               re.IGNORECASE)


def _is_ratio(fmt: str | None) -> bool:
    return bool(fmt) and ("%" in fmt or fmt.strip() in {"0.0", "0.00", "#,0.0"})


def _is_currency(fmt: str | None) -> bool:
    return bool(fmt) and any(sym in fmt for sym in ("$", "€", "£", "¥", "₹"))


def classify_measure(measure) -> str:
    if _TIME_INTEL.search(measure.name) or _TIME_INTEL.search(measure.dax or ""):
        return "time_intelligence"
    if _is_ratio(measure.format_string):
        return "ratio"
    if _is_currency(measure.format_string):
        return "currency"
    return "base"


def classify_column(col) -> str:
    if col.data_type in ("dateTime", "date"):
        return "date"
    if col.data_category in _GEO_CATEGORIES:
        return "geography"
    if col.data_type in ("int64", "double", "decimal") and not col.is_key:
        return "numeric"
    if col.data_type == "string" or col.is_key:
        return "category"
    return "other"


def profile_model(project) -> dict:
    tables = project.list_tables()
    rels = project.list_relationships()

    # fact/dimension via relationship direction: the "from" side (many) is the
    # fact-ish table; tables referenced as "to" (one) are dimensions.
    from_tables = {r.from_table for r in rels}
    to_tables = {r.to_table for r in rels}

    table_profiles = {}
    key_dimensions = []
    date_tables = []
    fact_tables = []
    for t in tables:
        if t.name.startswith(("LocalDateTable_", "DateTableTemplate_")):
            continue  # auto date tables — skip
        has_measures = bool(t.measures)
        is_date = (any(c.data_category == "Time" for c in t.columns) or
                   any(classify_column(c) == "date" and c.is_key
                       for c in t.columns) or t.name.lower() in ("date", "calendar"))
        if is_date:
            kind = "date"
            date_tables.append(t.name)
        elif t.name in from_tables and has_measures:
            kind = "fact"
            fact_tables.append(t.name)
        elif t.name in to_tables:
            kind = "dimension"
            key_dimensions.append(t.name)
        elif has_measures:
            kind = "fact"
            fact_tables.append(t.name)
        else:
            kind = "dimension"
            key_dimensions.append(t.name)

        cols = [{"name": c.name, "role": classify_column(c),
                 "hidden": c.is_hidden}
                for c in t.columns]
        table_profiles[t.name] = {
            "kind": kind,
            "measure_count": len(t.measures),
            "columns": cols,
            "grouping_columns": [c["name"] for c in cols
                                 if c["role"] in ("category", "geography")
                                 and not c["hidden"]
                                 and not _TECHNICAL_SUFFIX.search(c["name"])],
            "date_columns": [c["name"] for c in cols if c["role"] == "date"],
        }

    measures = []
    for m in project.list_measures():
        measures.append({"table": m.table, "name": m.name,
                         "role": classify_measure(m),
                         "format": m.format_string})

    kpi_candidates = [f"{m['table']}.{m['name']}" for m in measures
                      if m["role"] in ("currency", "base", "ratio")][:8]

    return {
        "tables": table_profiles,
        "measures": measures,
        "fact_tables": fact_tables,
        "dimensions": key_dimensions,
        "date_tables": date_tables,
        "kpi_candidates": kpi_candidates,
        "summary": {
            "facts": len(fact_tables),
            "dimensions": len(key_dimensions),
            "measures": len(measures),
            "time_intel_measures": sum(1 for m in measures
                                       if m["role"] == "time_intelligence"),
        },
    }
