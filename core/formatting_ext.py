"""Advanced visual formatting builders (feature package: formatting).

Pure functions — no I/O — that emit the PBIR shapes for conditional
formatting, Analytics-pane lines, report-page tooltips, slicer settings, data
labels and visual calculations. `report_server/tools_formatting.py` applies
them to visual.json through PbipProject._write_json (backup + pre-flight
schema validation).

Every shape records where it comes from:

  [real]   copied from Power BI Desktop PBIR exports surveyed on this machine
           (visualContainer 2.10.0 files: Corporate Spend / HR samples) —
           FillRule + linearGradient2 + nullColoringStrategy, Conditional /
           Cases / Comparison, the `dataPoint.fill` (charts) vs
           `values.fontColor` (tables) placement and their selectors,
           visualTooltip.type/section, syncGroup, slicer data.mode /
           selection.selectAllCheckboxEnabled / header.*, labels.* names.
  [schema] constrained by the vendored Fabric schemas: semanticQuery 1.2.0
           (QueryFillRuleExpression, QueryConditionalExpression, QueryCase,
           QueryComparisonExpression/Kind, QueryLiteralExpression,
           QueryNativeVisualCalc), visualContainer 1.0.0 (VisualTooltip,
           VisualSyncGroup, RoleProjection.nativeQueryRef/hidden/format) and
           formattingObjectDefinitions (Selector.metadata / data wildcard).
  [pbix]   property names known from Desktop's PBIX layout JSON and the theme
           visualStyles documentation, NOT verifiable from the vendored
           schemas (object properties are free-form there) and not present in
           the local exports. Each is flagged inline.
"""

from __future__ import annotations

import re

from core.formatting import _expr_literal, encode_literal, encode_property

_HEX_RE = re.compile(r"^#([0-9a-fA-F]{6}|[0-9a-fA-F]{8}|[0-9a-fA-F]{3})$")


# --- expression primitives (semanticQuery) ------------------------------------

def lit(value) -> dict:
    """A bare Literal expression node (no 'expr' wrapper) — [schema]."""
    return {"Literal": {"Value": encode_literal(value)}}


def color_lit(color: str, what: str = "color") -> dict:
    if not isinstance(color, str) or not _HEX_RE.match(color):
        raise ValueError(f"{what} must be a '#RRGGBB' hex string, got {color!r}")
    return lit(color)


def field_expr(entity: str, prop: str, is_measure: bool) -> dict:
    """{"Measure"|"Column": {Expression: {SourceRef: {Entity}}, Property}} — [schema]."""
    kind = "Measure" if is_measure else "Column"
    return {kind: {"Expression": {"SourceRef": {"Entity": entity}},
                   "Property": prop}}


def solid(expr: dict) -> dict:
    """Wrap a color-producing expression as a solid color property — [real]."""
    return {"solid": {"color": {"expr": expr}}}


def decode_literal(node):
    """Best-effort inverse of encode_literal for reporting (None if not a literal)."""
    try:
        raw = node["expr"]["Literal"]["Value"]
    except (KeyError, TypeError):
        return None
    if raw in ("true", "false"):
        return raw == "true"
    if raw == "null":
        return None
    if raw.endswith("L") and raw[:-1].lstrip("-").isdigit():
        return int(raw[:-1])
    if raw.endswith("D"):
        try:
            return float(raw[:-1])
        except ValueError:
            return raw
    if len(raw) >= 2 and raw.startswith("'") and raw.endswith("'"):
        return raw[1:-1].replace("''", "'")
    return raw


def split_ref(field: str) -> tuple[str, str]:
    entity, _, prop = (field or "").partition(".")
    if not entity or not prop:
        raise ValueError(f"field must be 'Table.Field', got {field!r}")
    return entity, prop


def resolve_input(query_state: dict, ref: str, is_measure) -> dict:
    """Expression for a 'Table.Field' (or 'Sum(Table.Col)') input.

    Reuses the visual's own projection when the field is bound (so aggregated
    columns keep their Aggregation expression); otherwise builds a fresh
    Measure/Column reference from the model.
    """
    for spec in query_state.values():
        for proj in spec.get("projections", []):
            if proj.get("queryRef") == ref and isinstance(proj.get("field"), dict):
                return proj["field"]
    entity, prop = split_ref(ref)
    return field_expr(entity, prop, bool(is_measure(entity, prop)))


def bound_refs(query_state: dict) -> list[str]:
    return [p.get("queryRef") for spec in query_state.values()
            for p in spec.get("projections", []) if p.get("queryRef")]


# --- conditional formatting ---------------------------------------------------

#: visual types whose cells are formatted through the `values` object [real]
TABLE_TYPES = frozenset({"tableEx", "pivotTable"})
#: visual types whose marks are colored through `dataPoint.fill` [real] and
#: whose data-label color is `labels.color` [real]
CHART_TYPES = frozenset({
    "barChart", "columnChart", "clusteredBarChart", "clusteredColumnChart",
    "hundredPercentStackedBarChart", "hundredPercentStackedColumnChart",
    "lineChart", "areaChart", "stackedAreaChart", "waterfallChart",
    "lineClusteredColumnComboChart", "lineStackedColumnComboChart",
    "scatterChart", "pieChart", "donutChart", "funnel", "treemap", "filledMap",
    "map", "ribbonChart",
})
CARD_TYPES = frozenset({"card"})

CF_TARGETS = ("background", "font", "data_bars", "icons", "web_url")

#: Desktop's "Apply to: values only" wildcard (instances, not totals) — [real]
_VALUES_ONLY = [{"dataViewWildcard": {"matchingOption": 1}}]


def cf_placement(visual_type: str, target: str) -> tuple[str, str | None, dict]:
    """(object name, property name or None for whole-object targets, selector
    template without the metadata key) for a conditional-format target.

    Mapping (see module docstring for sources):
      table/matrix  background -> values.backColor   [real: values.fontColor]
                    font       -> values.fontColor   [real]
                    data_bars  -> dataBars object    [pbix]
                    icons      -> icon object        [pbix]
                    web_url    -> webUrl object      [pbix]
      charts        background -> dataPoint.fill     [real]
                    font       -> labels.color       [real]
      card          font       -> labels.color       [real]
    """
    if target not in CF_TARGETS:
        raise ValueError(f"target must be one of {list(CF_TARGETS)}, got {target!r}")
    if visual_type in TABLE_TYPES:
        table_map = {
            "background": ("values", "backColor", {"data": _VALUES_ONLY}),
            "font": ("values", "fontColor", {"data": _VALUES_ONLY}),
            "data_bars": ("dataBars", None, {}),
            "icons": ("icon", None, {}),
            "web_url": ("webUrl", None, {}),
        }
        obj, prop, sel = table_map[target]
        return obj, prop, dict(sel)
    if visual_type in CHART_TYPES:
        if target == "background":
            return "dataPoint", "fill", {"data": _VALUES_ONLY}
        if target == "font":
            return "labels", "color", {}
        raise ValueError(
            f"target {target!r} is only available on table/matrix visuals "
            f"(tableEx, pivotTable); {visual_type!r} supports background "
            f"(mark fill) and font (data-label color).")
    if visual_type in CARD_TYPES:
        if target == "font":
            return "labels", "color", {}
        raise ValueError(f"card visuals only support target='font' "
                         f"(the value's color), not {target!r}")
    raise ValueError(
        f"Conditional formatting is not supported for visual type "
        f"{visual_type!r}. Supported: {sorted(TABLE_TYPES | CHART_TYPES | CARD_TYPES)}")


def build_gradient(input_expr: dict, rule: dict) -> dict:
    """FillRule expression with linearGradient2/3 — shape [real]:

    {"FillRule": {"Input": <expr>, "FillRule": {"linearGradient2": {
        "min": {"color": {"Literal": ..}, "value"?: {"Literal": ..}},
        "max": {...}, "nullColoringStrategy": {"strategy": {"Literal": "'asZero'"}}}}}}

    Custom stop `value` literals and the 'specificColor' null strategy are
    [pbix] (not present in the local exports).
    """
    stops: dict = {}
    for key in ("min", "mid", "max"):
        stop = rule.get(key)
        if stop is None:
            if key == "mid":
                continue
            raise ValueError(f"gradient rule needs '{key}': {{'color': '#hex', 'value'?: n}}")
        if not isinstance(stop, dict) or "color" not in stop:
            raise ValueError(f"gradient '{key}' must be {{'color': '#hex', 'value'?: number}}")
        node = {"color": color_lit(stop["color"], f"gradient {key} color")}
        if stop.get("value") is not None:
            node["value"] = lit(float(stop["value"]))
        stops[key] = node
    kind = "linearGradient3" if "mid" in stops else "linearGradient2"
    null_color = rule.get("null_color")
    if null_color is not None:
        strategy = {"strategy": lit("specificColor"),
                    "color": color_lit(null_color, "null_color")}
    else:
        strategy = {"strategy": lit("asZero")}
    return {"FillRule": {"Input": input_expr,
                         "FillRule": {kind: {**stops, "nullColoringStrategy": strategy}}}}


_KIND = {"eq": 0, "gt": 1, "ge": 2, "lt": 3, "le": 4}   # QueryComparisonKind [schema]


def comparison(kind: str, left: dict, right) -> dict:
    return {"Comparison": {"ComparisonKind": _KIND[kind], "Left": left,
                           "Right": right if isinstance(right, dict) else lit(right)}}


def rule_condition(input_expr: dict, rule: dict) -> dict:
    """min/max bounds -> Comparison (or And of two) on the input — [real]."""
    conds = []
    if rule.get("min") is not None:
        conds.append(comparison("ge" if rule.get("min_inclusive", True) else "gt",
                                input_expr, float(rule["min"])))
    if rule.get("max") is not None:
        conds.append(comparison("le" if rule.get("max_inclusive", False) else "lt",
                                input_expr, float(rule["max"])))
    if not conds:
        raise ValueError("each rule needs a numeric 'min' and/or 'max'")
    if len(conds) == 1:
        return conds[0]
    return {"And": {"Left": conds[0], "Right": conds[1]}}


def build_rules(input_expr: dict, rule: dict) -> dict:
    """Conditional expression whose Cases map value ranges to colors — [real].

    Rules are evaluated in order (first match wins); defaults follow Desktop:
    min inclusive, max exclusive. Optional 'default_color' -> DefaultValue.
    """
    rules = rule.get("rules")
    if not isinstance(rules, list) or not rules:
        raise ValueError("rules rule needs 'rules': [{min?, max?, color}, ...]")
    cases = []
    for r in rules:
        if not isinstance(r, dict) or "color" not in r:
            raise ValueError("each rule needs a 'color': '#hex'")
        cases.append({"Condition": rule_condition(input_expr, r),
                      "Value": color_lit(r["color"], "rule color")})
    out: dict = {"Cases": cases}
    if rule.get("default_color") is not None:
        out["DefaultValue"] = color_lit(rule["default_color"], "default_color")
    return {"Conditional": out}


def color_expression(rule: dict, resolve) -> tuple[str, dict]:
    """(kind, expression) for a background/font color rule.

    `resolve(ref)` turns 'Table.Field' into a Measure/Column expression.
    kind 'field' uses the measure's own value as the color — [pbix].
    """
    kind = rule.get("kind")
    if kind == "gradient":
        return kind, build_gradient(resolve(rule["measure"]), rule)
    if kind == "rules":
        return kind, build_rules(resolve(rule["measure"]), rule)
    if kind == "field":
        return kind, resolve(rule.get("measure") or rule.get("column"))
    raise ValueError("rule.kind must be 'gradient', 'rules' or 'field' for "
                     f"background/font targets, got {kind!r}")


def build_data_bars(rule: dict) -> dict:
    """dataBars object properties — names [pbix] (positiveColor, negativeColor,
    axisColor, hideText, reverseDirection); min/max numeric literals [pbix,
    unconfirmed]. Defaults reuse core.interact.build_data_bar's colors."""
    props = {
        "positiveColor": encode_property("positiveColor",
                                         rule.get("positive_color", "#1F8A70")),
        "negativeColor": encode_property("negativeColor",
                                         rule.get("negative_color", "#D13438")),
        "hideText": _expr_literal(bool(rule.get("show_bar_only", False))),
    }
    for key in ("positive_color", "negative_color", "axis_color"):
        if rule.get(key) is not None:
            color_lit(rule[key], key)
    if rule.get("axis_color") is not None:
        props["axisColor"] = encode_property("axisColor", rule["axis_color"])
    if rule.get("reverse_direction") is not None:
        props["reverseDirection"] = _expr_literal(bool(rule["reverse_direction"]))
    for key in ("min", "max"):
        if rule.get(key) is not None:
            props[key] = _expr_literal(float(rule[key]))
    return props


ICON_LAYOUTS = ("leftOfData", "rightOfData", "dataOnly")
#: Desktop icon-set style names (Format style dialog) — [pbix]
ICON_STYLES = (
    "threeArrowsColored", "threeArrowsGray", "threeTrafficLights",
    "threeSymbolsCircled", "threeSymbolsUncircled", "threeStarsColored",
    "threeFlagsColored", "threeTriangles", "threeShapes", "fourArrowsColored",
    "fourArrowsGray", "fourRatings", "fourCirclesGray", "fiveArrowsColored",
    "fiveArrowsGray", "fiveRatings", "fiveBoxes", "fiveQuarters",
)


def build_icons(input_expr: dict, rule: dict) -> dict:
    """icon object properties: a Conditional `iconSet` whose case values name
    an icon of the chosen style, plus `layout`/`style` literals.

    [pbix, UNCONFIRMED]: the object name `icon` and the layout values are
    known from Desktop; the exact encoding of the per-case icon literal
    ('<style>_<index>') could not be confirmed from the vendored schemas or
    the local exports — verify against a Desktop export before relying on it.
    """
    style = rule.get("style")
    if not style or not isinstance(style, str):
        raise ValueError(f"icons rule needs 'style' (e.g. one of {ICON_STYLES})")
    rules = rule.get("rules")
    if not isinstance(rules, list) or not rules:
        raise ValueError("icons rule needs 'rules': [{min?, max?, icon: index}, ...]")
    layout = rule.get("layout", "leftOfData")
    if layout not in ICON_LAYOUTS:
        raise ValueError(f"icons layout must be one of {ICON_LAYOUTS}")
    cases = []
    for r in rules:
        if not isinstance(r, dict) or not isinstance(r.get("icon"), int):
            raise ValueError("each icon rule needs an integer 'icon' index")
        cases.append({"Condition": rule_condition(input_expr, r),
                      "Value": lit(f"{style}_{r['icon']}")})
    return {
        "iconSet": {"expr": {"Conditional": {"Cases": cases}}},
        "style": _expr_literal(style),
        "layout": _expr_literal(layout),
    }


def build_web_url(url_expr: dict) -> dict:
    """webUrl object: the field whose value is the link target.

    [pbix, UNCONFIRMED]: object name from Desktop's Web URL format style; the
    property name `url` is not verifiable from the vendored schemas.
    """
    return {"url": {"expr": url_expr}}


# --- objects list helpers ------------------------------------------------------

def _selector_metadata(entry: dict):
    return (entry.get("selector") or {}).get("metadata")


def upsert_entry(objects: dict, obj_name: str, selector: dict,
                 props: dict, *, replace: bool = False) -> None:
    """Merge `props` into the entry of objects[obj_name] whose
    selector.metadata matches (creating it after existing entries otherwise).
    replace=True swaps the entry's properties wholesale."""
    entries = objects.setdefault(obj_name, [])
    meta = selector.get("metadata")
    for entry in entries:
        if _selector_metadata(entry) == meta and (meta is not None
                                                  or not entry.get("selector")):
            if replace:
                entry["properties"] = dict(props)
            else:
                entry.setdefault("properties", {}).update(props)
            if selector:
                entry["selector"] = {**(entry.get("selector") or {}), **selector}
            return
    new = {"properties": dict(props)}
    if selector:
        new["selector"] = dict(selector)
    entries.append(new)


def remove_props(objects: dict, obj_name: str, metadata: str | None,
                 prop_names: list[str] | None) -> int:
    """Remove properties (or whole entries when prop_names is None) scoped to
    `metadata`; drops empty entries/objects. Returns the number removed."""
    entries = objects.get(obj_name) or []
    removed = 0
    kept = []
    for entry in entries:
        if _selector_metadata(entry) != metadata:
            kept.append(entry)
            continue
        if prop_names is None:
            removed += 1
            continue
        props = entry.get("properties", {})
        for name in prop_names:
            if name in props:
                del props[name]
                removed += 1
        if props:
            kept.append(entry)
    if kept:
        objects[obj_name] = kept
    else:
        objects.pop(obj_name, None)
    return removed


# --- analytics lines -------------------------------------------------------------

#: Analytics-pane object names — [pbix]: `y1AxisReferenceLine` (constant line
#: on the value axis; the cartesian capabilities name since the open-source
#: PowerBI-visuals era), `minLine`/`maxLine`/`averageLine`/`medianLine`/
#: `percentileLine` and `trend`, as listed in theme visualStyles references.
LINE_OBJECTS = {
    "constant": "y1AxisReferenceLine",
    "min": "minLine",
    "max": "maxLine",
    "average": "averageLine",
    "median": "medianLine",
    "percentile": "percentileLine",
    "trend": "trend",
}
LINE_KINDS = {v: k for k, v in LINE_OBJECTS.items()}
LINE_STYLES = ("dashed", "solid", "dotted")
LINE_POSITIONS = {"behind": "back", "front": "front"}     # Desktop literals [pbix]
ANALYTICS_TYPES = frozenset({
    "barChart", "columnChart", "clusteredBarChart", "clusteredColumnChart",
    "hundredPercentStackedBarChart", "hundredPercentStackedColumnChart",
    "lineChart", "areaChart", "stackedAreaChart", "scatterChart",
    "lineClusteredColumnComboChart", "lineStackedColumnComboChart",
})
TREND_TYPES = frozenset({"lineChart", "clusteredColumnChart",
                         "clusteredBarChart", "scatterChart", "areaChart"})


def build_analytics_line(kind: str, *, value=None, measure_expr: dict | None = None,
                         color: str | None = None, style: str = "dashed",
                         label: str | None = None, transparency=None,
                         position: str = "behind", percentile=None) -> tuple[str, dict]:
    """(object name, properties) for one Analytics-pane line.

    Property names [pbix]: show, displayName, value, lineColor, transparency
    (0-100), style ('dashed'|'solid'|'dotted'), position ('back'|'front'),
    percentile; `measure` (the series an aggregate line summarises) is
    [pbix, UNCONFIRMED] and only written when a measure is given.
    """
    if kind not in LINE_OBJECTS:
        raise ValueError(f"kind must be one of {sorted(LINE_OBJECTS)}, got {kind!r}")
    if style not in LINE_STYLES:
        raise ValueError(f"style must be one of {LINE_STYLES}")
    if position not in LINE_POSITIONS:
        raise ValueError(f"position must be one of {sorted(LINE_POSITIONS)}")
    props: dict = {"show": _expr_literal(True)}
    if kind == "constant":
        if value is None:
            raise ValueError("a constant line needs value=<number>")
        props["value"] = _expr_literal(float(value))
    elif kind == "percentile":
        if percentile is None:
            raise ValueError("a percentile line needs percentile=<0-100>")
        p = float(percentile)
        if not 0 <= p <= 100:
            raise ValueError("percentile must be between 0 and 100")
        props["percentile"] = _expr_literal(p)
    if kind not in ("constant", "trend") and measure_expr is not None:
        props["measure"] = {"expr": measure_expr}
    if label is not None and kind != "trend":
        props["displayName"] = _expr_literal(str(label))
    if color is not None:
        color_lit(color, "color")
        props["lineColor"] = encode_property("lineColor", color)
    if transparency is not None:
        t = float(transparency)
        if not 0 <= t <= 100:
            raise ValueError("transparency is a percentage 0-100")
        props["transparency"] = _expr_literal(t)
    props["style"] = _expr_literal(style)
    if kind != "trend":
        props["position"] = _expr_literal(LINE_POSITIONS[position])
    return LINE_OBJECTS[kind], props


def list_lines(objects: dict) -> list[dict]:
    """Analytics lines present in `objects`, in a stable index order."""
    out = []
    for obj_name, entries in objects.items():
        kind = LINE_KINDS.get(obj_name)
        if kind is None:
            continue
        for entry in entries:
            props = entry.get("properties", {})
            item = {"index": len(out), "kind": kind, "object": obj_name,
                    "selector": entry.get("selector")}
            for key, name in (("value", "value"), ("percentile", "percentile"),
                              ("label", "displayName"), ("style", "style"),
                              ("position", "position"),
                              ("transparency", "transparency"), ("show", "show")):
                if name in props:
                    item[key] = decode_literal(props[name])
            if "lineColor" in props:
                try:
                    item["color"] = decode_literal(props["lineColor"]["solid"]["color"])
                except (KeyError, TypeError):
                    item["color"] = None
            m = props.get("measure", {}).get("expr", {}).get("Measure")
            if m:
                item["measure"] = f"{m['Expression']['SourceRef']['Entity']}.{m['Property']}"
            out.append(item)
    return out


def remove_line(objects: dict, index: int) -> dict:
    """Remove the analytics line at `index` (from list_lines); returns it."""
    lines = list_lines(objects)
    if not lines:
        raise KeyError("this visual has no analytics lines")
    if not isinstance(index, int) or not 0 <= index < len(lines):
        raise KeyError(f"index must be 0..{len(lines) - 1} (see pbi_list_analytics_lines)")
    target = lines[index]
    entries = objects[target["object"]]
    pos = sum(1 for l in lines[:index] if l["object"] == target["object"])
    del entries[pos]
    if not entries:
        del objects[target["object"]]
    return target


# --- report-page tooltip ---------------------------------------------------------

def tooltip_binding(page_id: str) -> dict:
    """visualContainerObjects.visualTooltip properties binding a tooltip page:
    type 'ReportPage' + section <pageId> [real: type/section; schema:
    VisualTooltip.type/section/show]."""
    return {"show": _expr_literal(True), "type": _expr_literal("ReportPage"),
            "section": _expr_literal(page_id)}


def set_tooltip(vc_objects: dict, page_id: str | None) -> None:
    entries = vc_objects.get("visualTooltip") or [{"properties": {}}]
    props = entries[0].setdefault("properties", {})
    if page_id is None:
        props.pop("section", None)
        props.pop("show", None)
        props["type"] = _expr_literal("Default")      # Desktop's own revert [real]
    else:
        props.update(tooltip_binding(page_id))
    vc_objects["visualTooltip"] = entries


def current_tooltip_page(vc_objects: dict) -> str | None:
    try:
        props = vc_objects["visualTooltip"][0]["properties"]
    except (KeyError, IndexError, TypeError):
        return None
    if decode_literal(props.get("type", {})) == "Default":
        return None
    return decode_literal(props.get("section", {})) if "section" in props else None


# --- slicer -------------------------------------------------------------------------

#: style -> (data.mode literal [real: 'Dropdown'; others pbix], orientation)
SLICER_STYLES = {
    "list": ("Basic", 0),
    "dropdown": ("Dropdown", None),
    "tile": ("Basic", 1),
    "between": ("Between", None),
    "relative_date": ("RelativeDate", None),
    "before": ("Before", None),
    "after": ("After", None),
}
SLICER_ORIENTATIONS = {"vertical": 0, "horizontal": 1}     # general.orientation [pbix]


def slicer_patch(style=None, single_select=None, select_all=None, search=None,
                 header=None, orientation=None) -> dict:
    """{objectName: {prop: plain value}} for core.formatting.build_objects_patch.

    data.mode [real], selection.selectAllCheckboxEnabled [real],
    selection.singleSelect [pbix], general.selfFilterEnabled (search box)
    [pbix], general.orientation 0|1 [pbix], header.show/text [real].
    """
    patch: dict = {}
    if style is not None:
        if style not in SLICER_STYLES:
            raise ValueError(f"style must be one of {sorted(SLICER_STYLES)}")
        mode, orient = SLICER_STYLES[style]
        patch.setdefault("data", {})["mode"] = mode
        if orient is not None:
            patch.setdefault("general", {})["orientation"] = orient
    if orientation is not None:
        if orientation not in SLICER_ORIENTATIONS:
            raise ValueError(f"orientation must be one of {sorted(SLICER_ORIENTATIONS)}")
        patch.setdefault("general", {})["orientation"] = SLICER_ORIENTATIONS[orientation]
    if single_select is not None:
        patch.setdefault("selection", {})["singleSelect"] = bool(single_select)
    if select_all is not None:
        patch.setdefault("selection", {})["selectAllCheckboxEnabled"] = bool(select_all)
    if search is not None:
        patch.setdefault("general", {})["selfFilterEnabled"] = bool(search)
    if header is not None:
        if isinstance(header, bool):
            patch.setdefault("header", {})["show"] = header
        else:
            patch.setdefault("header", {}).update({"show": True, "text": str(header)})
    return patch


def sync_group(group_name: str, field_changes=None, filter_changes=None) -> dict:
    """visual.syncGroup — [schema: VisualSyncGroup; real: all three keys]."""
    block = {"groupName": group_name,
             "fieldChanges": True if field_changes is None else bool(field_changes),
             "filterChanges": True if filter_changes is None else bool(filter_changes)}
    return block


# --- data labels -----------------------------------------------------------------------

DISPLAY_UNITS = {"auto": 0, "none": 1, "thousands": 1000, "millions": 1_000_000,
                 "billions": 1_000_000_000, "trillions": 1_000_000_000_000}
LABEL_TYPES = CHART_TYPES | CARD_TYPES | TABLE_TYPES


def data_labels(visual_type: str, *, show=True, position=None, display_units=None,
                decimals=None, color=None, font_size=None, font_family=None,
                background=None) -> tuple[str, dict]:
    """(object name, {prop: plain value}) for pbi_set_data_labels.

    charts -> `labels`: show, labelPosition, labelDisplayUnits, labelPrecision,
      color, fontSize, fontFamily [real], enableBackground [real] +
      backgroundColor [pbix]
    card   -> `labels`: color, fontSize, fontFamily [real], labelDisplayUnits,
      labelPrecision [pbix]
    table/matrix -> `values`: fontColor, fontSize, fontFamily, backColor [real]
      (show/position/units/decimals are per-column there — unsupported)
    """
    if visual_type not in LABEL_TYPES:
        raise ValueError(
            f"Data labels are not supported for {visual_type!r}. Supported: "
            f"{sorted(LABEL_TYPES)}")
    units = None
    if display_units is not None:
        if isinstance(display_units, str):
            if display_units not in DISPLAY_UNITS:
                raise ValueError(f"display_units must be a number or one of {sorted(DISPLAY_UNITS)}")
            units = DISPLAY_UNITS[display_units]
        else:
            units = int(display_units)
    if color is not None:
        color_lit(color, "color")
    if visual_type in TABLE_TYPES:
        bad = [n for n, v in (("show", show is False), ("position", position is not None),
                              ("display_units", display_units is not None),
                              ("decimals", decimals is not None)) if v]
        if bad:
            raise ValueError(
                f"{bad} are per-column settings on table/matrix visuals and are "
                f"not supported here; use color/font_size/font_family/background")
        props: dict = {}
        if color is not None:
            props["fontColor"] = color
        if font_size is not None:
            props["fontSize"] = font_size
        if font_family is not None:
            props["fontFamily"] = font_family
        if background is not None:
            color_lit(background, "background")
            props["backColor"] = background
        if not props:
            raise ValueError("nothing to set — pass color/font_size/font_family/background")
        return "values", props
    props = {}
    if visual_type in CARD_TYPES:
        bad = [n for n, v in (("show", show is False), ("position", position is not None),
                              ("background", background is not None)) if v]
        if bad:
            raise ValueError(f"{bad} do not apply to card labels")
    else:
        props["show"] = bool(show)
        if position is not None:
            props["labelPosition"] = str(position)
        if background is not None:
            if background is False:
                props["enableBackground"] = False
            else:
                color_lit(background, "background")
                props["enableBackground"] = True
                props["backgroundColor"] = background
    if units is not None:
        props["labelDisplayUnits"] = units
    if decimals is not None:
        props["labelPrecision"] = int(decimals)
    if color is not None:
        props["color"] = color
    if font_size is not None:
        props["fontSize"] = font_size
    if font_family is not None:
        props["fontFamily"] = str(font_family)
    return "labels", props


# --- visual calculations -------------------------------------------------------------------

_VC_VERSION_RE = re.compile(r"/visualContainer/(\d+)\.(\d+)\.(\d+)/schema\.json$")
#: below this visualContainer version the $schema pointer is upgraded when a
#: visual calculation is added (see ensure_schema_at_least)
MIN_VISUAL_CALC_SCHEMA = (1, 2, 0)
_CALC_BUCKET_PREFERENCE = ("Values", "Y", "Data", "Y2", "X")


def schema_version(url) -> tuple[int, int, int] | None:
    m = _VC_VERSION_RE.search(url or "")
    return tuple(int(g) for g in m.groups()) if m else None  # type: ignore[return-value]


def ensure_schema_at_least(data: dict, minimum=MIN_VISUAL_CALC_SCHEMA) -> bool:
    """Upgrade visual.json's $schema to the version this repo emits when the
    declared visualContainer version is older than `minimum` (or missing).

    The vendored visualContainer 1.0.0 -> semanticQuery 1.0.0 pair already
    defines NativeVisualCalculation and RoleProjection.nativeQueryRef, so
    validation does not need this; it keeps the file on the format Desktop
    writes for visual calculations. Returns True when the pointer changed.
    """
    from core.pbir import _VC_SCHEMA

    current = schema_version(data.get("$schema"))
    if current is not None and current >= minimum:
        return False
    if data.get("$schema") == _VC_SCHEMA:
        return False
    data["$schema"] = _VC_SCHEMA
    return True


def native_name(proj: dict) -> str:
    field = proj.get("field", {})
    for kind in ("Measure", "Column", "HierarchyLevel"):
        if kind in field and isinstance(field[kind], dict) and "Property" in field[kind]:
            return str(field[kind]["Property"])
    if "Aggregation" in field:
        inner = field["Aggregation"].get("Expression", {})
        for kind in ("Column", "Measure"):
            if kind in inner and "Property" in inner[kind]:
                return str(inner[kind]["Property"])
    if "NativeVisualCalculation" in field:
        return str(field["NativeVisualCalculation"].get("Name", ""))
    ref = str(proj.get("queryRef", ""))
    return ref.rsplit(".", 1)[-1] or ref


def ensure_native_query_refs(query_state: dict) -> list[str]:
    """Give every projection a nativeQueryRef (the name DAX uses in a visual
    calculation as [name]) — Desktop does this when the first calculation is
    added [schema: RoleProjection.nativeQueryRef]. Returns the names."""
    taken = {p["nativeQueryRef"] for spec in query_state.values()
             for p in spec.get("projections", []) if p.get("nativeQueryRef")}
    for spec in query_state.values():
        for proj in spec.get("projections", []):
            if proj.get("nativeQueryRef"):
                continue
            base = native_name(proj) or "Field"
            name, n = base, 1
            while name in taken:
                n += 1
                name = f"{base} {n}"
            proj["nativeQueryRef"] = name
            taken.add(name)
    return sorted(taken)


def default_calc_bucket(query_state: dict) -> str:
    for bucket in _CALC_BUCKET_PREFERENCE:
        if bucket in query_state:
            return bucket
    if query_state:
        return next(iter(query_state))
    raise ValueError("the visual has no field buckets to add a calculation to")


def build_visual_calc_projection(name: str, expression: str,
                                 format_string: str | None = None,
                                 hidden: bool = False) -> dict:
    """A queryState projection holding a visual calculation — [schema:
    QueryNativeVisualCalc (Language 'dax', Expression, Name) + RoleProjection
    queryRef/nativeQueryRef/format/hidden]. DataType is omitted: the
    semanticQuery 1.0.0 definition the vendored visualContainer schema refs
    does not allow it."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name must be a non-empty string")
    if not isinstance(expression, str) or not expression.strip():
        raise ValueError("expression must be a non-empty DAX string")
    proj = {
        "field": {"NativeVisualCalculation": {
            "Language": "dax", "Expression": expression.strip(), "Name": name}},
        "queryRef": name,
        "nativeQueryRef": name,
    }
    if format_string:
        proj["format"] = format_string
    if hidden:
        proj["hidden"] = True
    return proj


_BRACKET_RE = re.compile(r"\[([^\[\]]+)\]")


def unknown_calc_refs(expression: str, known: list[str]) -> list[str]:
    """[name] references in a visual-calculation expression that match no
    nativeQueryRef on the visual (case-insensitive) — a lint hint."""
    lower = {k.lower() for k in known}
    return sorted({m for m in _BRACKET_RE.findall(expression)
                   if m.strip().lower() not in lower})


def list_calcs(query_state: dict) -> list[dict]:
    out = []
    for bucket, spec in query_state.items():
        for i, proj in enumerate(spec.get("projections", [])):
            calc = proj.get("field", {}).get("NativeVisualCalculation")
            if not calc:
                continue
            out.append({"bucket": bucket, "position": i, "name": calc.get("Name"),
                        "expression": calc.get("Expression"),
                        "format": proj.get("format"),
                        "hidden": bool(proj.get("hidden", False))})
    return out
