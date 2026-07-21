"""Interactivity & navigation builders (E4).

Sort, column aggregations, navigation buttons, drillthrough/tooltip page
roles, and conditional formatting — all shapes copied from real exports or
built from the vendored semanticQuery schema.
"""

from __future__ import annotations

from core.formatting import encode_property

# QueryAggregateFunction enum (semanticQuery schema)
AGG_FUNCS = {"sum": 0, "average": 1, "avg": 1, "distinctcount": 2,
             "min": 3, "max": 4, "count": 5, "median": 6,
             "stddev": 7, "variance": 8}


# --- sort (D18) -------------------------------------------------------------

def build_sort_definition(field_ref: str, direction: str = "Descending",
                          is_measure: bool = True) -> dict:
    """visual.query.sortDefinition — sort a visual by a field."""
    if direction not in ("Ascending", "Descending"):
        raise ValueError("direction must be Ascending or Descending")
    entity, _, prop = field_ref.partition(".")
    kind = "Measure" if is_measure else "Column"
    return {
        "sort": [{
            "field": {kind: {"Expression": {"SourceRef": {"Entity": entity}},
                             "Property": prop}},
            "direction": direction,
        }],
        "isDefaultSort": True,
    }


# --- aggregation (D19) ------------------------------------------------------

def build_aggregation_projection(query_ref: str, func: str) -> dict:
    """A projection that aggregates a column (Sum/Average/Count/…)."""
    if func.lower() not in AGG_FUNCS:
        raise ValueError(f"Unknown aggregation {func!r}; use {sorted(AGG_FUNCS)}")
    entity, _, prop = query_ref.partition(".")
    return {
        "field": {"Aggregation": {
            "Expression": {"Column": {
                "Expression": {"SourceRef": {"Entity": entity}},
                "Property": prop}},
            "Function": AGG_FUNCS[func.lower()],
        }},
        "queryRef": f"{func.capitalize()}({query_ref})",
    }


# --- navigation button (D21) ------------------------------------------------

def build_nav_button(visual_id: str, label: str, target_page_id: str,
                     position: dict | None = None,
                     fill: str = "#1F3A5F", text_color: str = "#FFFFFF") -> dict:
    """An actionButton that navigates to another page on click."""
    pos = {"x": 0, "y": 0, "z": 6000, "width": 160, "height": 40, "tabOrder": 6000}
    if position:
        pos.update(position)
    visual = {
        "visualType": "actionButton",
        "objects": {
            "text": [{"properties": {
                "show": {"expr": {"Literal": {"Value": "true"}}},
                "text": {"expr": {"Literal": {"Value": f"'{label}'"}}},
                "fontColor": encode_property("fontColor", text_color),
            }, "selector": {"id": "default"}}],
            "fill": [{"properties": {
                "show": {"expr": {"Literal": {"Value": "true"}}},
                "fillColor": encode_property("fillColor", fill),
            }, "selector": {"id": "default"}}],
        },
        "visualContainerObjects": {
            "visualLink": [{"properties": {
                "show": {"expr": {"Literal": {"Value": "true"}}},
                "type": {"expr": {"Literal": {"Value": "'PageNavigation'"}}},
                "navigationSection": {"expr": {"Literal":
                                      {"Value": f"'{target_page_id}'"}}},
            }}],
        },
        "drillFilterOtherVisuals": True,
    }
    return {"$schema": "https://developer.microsoft.com/json-schemas/fabric/"
            "item/report/definition/visualContainer/2.10.0/schema.json",
            "name": visual_id, "position": pos, "visual": visual}


# --- conditional formatting (D23) -------------------------------------------

def build_data_bar(measure_ref: str, positive: str = "#1F8A70",
                   negative: str = "#D13438") -> dict:
    """A dataBars object entry for a table/matrix value column."""
    return {
        "dataBars": [{"properties": {
            "positiveColor": encode_property("positiveColor", positive),
            "negativeColor": encode_property("negativeColor", negative),
            "hideText": {"expr": {"Literal": {"Value": "false"}}},
        }}],
    }
