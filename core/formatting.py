"""Formatting, theme, and filter builders for the PBIR report layer.

Everything here emits shapes copied from real Desktop exports:
  * object properties: {"expr": {"Literal": {"Value": <encoded>}}}
  * literals: strings -> 'text' ; ints -> 3L ; floats -> 3.5D ; bools -> true
  * colors: {"solid": {"color": {"expr": {"Literal": {"Value": "'#RRGGBB'"}}}}}
  * filters: Version-2 query trees with a From alias + Where Condition
    (Categorical "In" and Advanced "Comparison" verified against real
    exports; TopN follows the standard query shape).
"""

from __future__ import annotations

import uuid


# --- literals -----------------------------------------------------------------

def encode_literal(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return f"{value}L"
    if isinstance(value, float):
        return f"{value}D"
    s = str(value).replace("'", "''")
    return f"'{s}'"


def _expr_literal(value) -> dict:
    return {"expr": {"Literal": {"Value": encode_literal(value)}}}


_COLOR_KEYS = {"color", "fontColor", "background", "backColor",
               "labelColor", "titleColor"}


def encode_property(key: str, value) -> dict:
    """Encode one object property value the way PBIR stores it.

    Dicts pass through untouched (caller already built the expr tree).
    '#hex' strings on color-ish keys get the solid/color wrapper.
    """
    if isinstance(value, dict):
        return value
    if (key in _COLOR_KEYS or key.lower().endswith("color")) and \
            isinstance(value, str) and value.startswith("#"):
        return {"solid": {"color": _expr_literal(value)}}
    return _expr_literal(value)


def build_objects_patch(objects: dict[str, dict]) -> dict:
    """{objectName: {prop: value}} -> {objectName: [{"properties": {...}}]}"""
    out: dict = {}
    for obj_name, props in objects.items():
        out[obj_name] = [{
            "properties": {
                k: encode_property(k, v) for k, v in props.items()
            }
        }]
    return out


def merge_objects(existing: dict, patch: dict) -> dict:
    """Merge a patch into an existing objects dict, property-level.

    Only the first entry of each object list is merged (the common case);
    additional selector-scoped entries are preserved untouched.
    """
    merged = dict(existing)
    for obj_name, entries in patch.items():
        if obj_name not in merged or not merged[obj_name]:
            merged[obj_name] = entries
            continue
        current = merged[obj_name]
        first = dict(current[0])
        props = dict(first.get("properties", {}))
        props.update(entries[0]["properties"])
        first["properties"] = props
        merged[obj_name] = [first] + list(current[1:])
    return merged


# --- filters --------------------------------------------------------------------

def _source_ref(entity: str, alias: str, prop: str) -> dict:
    return {"Column": {"Expression": {"SourceRef": {"Source": alias}},
                       "Property": prop}}


def _field(entity: str, prop: str, is_measure: bool) -> dict:
    kind = "Measure" if is_measure else "Column"
    return {kind: {"Expression": {"SourceRef": {"Entity": entity}},
                   "Property": prop}}


def build_filter(field_ref: str, *, is_measure: bool = False,
                 filter_type: str = "Categorical",
                 values: list | None = None,
                 comparison: str | None = None,
                 comparison_value=None,
                 top_n: int | None = None,
                 order_by: str | None = None,
                 raw_condition: dict | None = None,
                 name: str | None = None) -> dict:
    """Build one filterConfig entry (shape verified from real exports).

    filter_type:
      Categorical  — `values` kept (In condition)
      Advanced     — `comparison` in {eq,gt,ge,lt,le} vs `comparison_value`
      TopN         — `top_n` count ordered by measure `order_by` (Table.Field)
      Passthrough  — `raw_condition` used verbatim (for shapes not yet
                     modeled, e.g. relative-date)
    """
    entity, _, prop = field_ref.partition(".")
    alias = entity[:1].lower() or "t"
    from_clause = [{"Name": alias, "Entity": entity, "Type": 0}]

    if filter_type == "Categorical":
        if not values:
            raise ValueError("Categorical filter needs values=[...]")
        condition = {"In": {
            "Expressions": [_source_ref(entity, alias, prop)],
            "Values": [[{"Literal": {"Value": encode_literal(v)}}]
                       for v in values],
        }}
    elif filter_type == "Advanced":
        kinds = {"eq": 0, "gt": 1, "ge": 2, "lt": 3, "le": 4}
        if comparison not in kinds:
            raise ValueError(f"comparison must be one of {sorted(kinds)}")
        condition = {"Comparison": {
            "ComparisonKind": kinds[comparison],
            "Left": _source_ref(entity, alias, prop),
            "Right": {"Literal": {"Value": encode_literal(comparison_value)}},
        }}
    elif filter_type == "TopN":
        # Schema truth (semanticquery 1.2.0, confirmed by Desktop's own
        # validation): the condition is VisualTopN with ONLY ItemCount, and
        # FilterDefinition allows only Version/From/Where — no OrderBy.
        # The ranking measure is taken from the visual's own value field, so
        # `order_by` is accepted for API compatibility but not serialized;
        # bind that measure in the visual (e.g. its Y bucket).
        if not top_n:
            raise ValueError("TopN filter needs top_n")
        condition = {"VisualTopN": {"ItemCount": top_n}}
    elif filter_type == "Passthrough":
        if not raw_condition:
            raise ValueError("Passthrough filter needs raw_condition")
        condition = raw_condition
        filter_type = "Advanced"
    else:
        raise ValueError(f"Unknown filter_type {filter_type!r}")

    return {
        "name": name or f"Filter{uuid.uuid4().hex[:8]}",
        "field": _field(entity, prop, is_measure),
        "type": filter_type,
        "filter": {"Version": 2, "From": from_clause,
                   "Where": [{"Condition": condition}]},
        "howCreated": "User",
    }
