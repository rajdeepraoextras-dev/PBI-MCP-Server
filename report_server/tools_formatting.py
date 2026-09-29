"""Advanced visual formatting tools (feature package: formatting).

Conditional formatting, Analytics-pane lines, report-page tooltips, slicer
settings, data labels and visual calculations. Loaded by
core.tooling.load_tool_modules — never import report_server.server here.

Every write reads visual.json, edits the dict and hands it back to
PbipProject._write_json (style-preserving, backed up, schema-validated).
Shape provenance ([real] / [schema] / [pbix]) is documented in
core/formatting_ext.py.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from core import formatting_ext as fx
from core.formatting import build_objects_patch, merge_objects

if TYPE_CHECKING:  # pragma: no cover
    from report_server.server import ReportState


# --- helpers ------------------------------------------------------------------------

def _load(project, page_id: str, visual_id: str):
    vfile = project._visual_file(page_id, visual_id)
    return vfile, json.loads(vfile.read_text(encoding="utf-8-sig"))


def _visual(data: dict) -> dict:
    visual = data.get("visual")
    if not isinstance(visual, dict) or "visualType" not in visual:
        raise ValueError("this container is a visual group, not a visual")
    return visual


def _query_state(visual: dict) -> dict:
    return visual.setdefault("query", {}).setdefault("queryState", {})


def _check_model_ref(project, ref: str) -> None:
    """A 'Table.Field' (or 'Agg(Table.Col)') must exist in the semantic model."""
    inner = ref
    if ref.endswith(")") and "(" in ref:
        inner = ref[ref.index("(") + 1:-1]
    entity, prop = fx.split_ref(inner)
    if any(m.table == entity and m.name == prop for m in project.list_measures()):
        return
    if any(t.name == entity and any(c.name == prop for c in t.columns)
           for t in project.list_tables()):
        return
    raise KeyError(f"Unknown model field {ref!r} — use a Table.Field that exists "
                   f"in the semantic model.")


def _bound_field(query_state: dict, field: str) -> str:
    refs = fx.bound_refs(query_state)
    if field not in refs:
        raise ValueError(f"field {field!r} is not bound in this visual; bound "
                         f"fields: {refs}")
    return field


# --- 1. conditional formatting --------------------------------------------------------

def set_conditional_format(state: ReportState, page_id: str, visual_id: str,
                           field: str, target: str, rule: dict) -> dict:
    """Apply a conditional-format rule to a bound field of a visual."""
    if not isinstance(rule, dict):
        raise ValueError("rule must be an object")
    project = state.require()
    vfile, data = _load(project, page_id, visual_id)
    visual = _visual(data)
    query_state = _query_state(visual)
    ref = _bound_field(query_state, field)
    obj_name, prop_name, selector = fx.cf_placement(visual["visualType"], target)
    selector["metadata"] = ref

    def resolve(r):
        if not r:
            raise ValueError("rule needs 'measure' (Table.Measure or Table.Column)")
        if r not in fx.bound_refs(query_state):
            _check_model_ref(project, r)
        return fx.resolve_input(query_state, r, project._is_measure)

    objects = visual.setdefault("objects", {})
    if target in ("background", "font"):
        kind, expr = fx.color_expression(rule, resolve)
        fx.upsert_entry(objects, obj_name, selector, {prop_name: fx.solid(expr)})
    elif target == "data_bars":
        kind = "data_bars"
        fx.upsert_entry(objects, obj_name, selector, fx.build_data_bars(rule),
                        replace=True)
    elif target == "icons":
        kind = "icons"
        input_expr = resolve(rule.get("measure") or ref)
        fx.upsert_entry(objects, obj_name, selector, fx.build_icons(input_expr, rule),
                        replace=True)
    else:  # web_url
        kind = "field"
        if rule.get("kind", "field") != "field":
            raise ValueError("web_url takes {'kind': 'field', 'measure'|'column': 'Table.Field'}")
        url_ref = rule.get("measure") or rule.get("column")
        fx.upsert_entry(objects, obj_name, selector, fx.build_web_url(resolve(url_ref)),
                        replace=True)
    project._write_json(vfile, data)
    return {"ok": True, "page_id": page_id, "visual_id": visual_id, "field": ref,
            "target": target, "kind": kind, "object": obj_name,
            "property": prop_name, "selector": selector}


def clear_conditional_format(state: ReportState, page_id: str, visual_id: str,
                             field: str, target: str) -> dict:
    """Remove a conditional format previously set on a field."""
    project = state.require()
    vfile, data = _load(project, page_id, visual_id)
    visual = _visual(data)
    obj_name, prop_name, _ = fx.cf_placement(visual["visualType"], target)
    objects = visual.get("objects") or {}
    removed = fx.remove_props(objects, obj_name, field,
                              [prop_name] if prop_name else None)
    if removed:
        if objects:
            visual["objects"] = objects
        else:
            visual.pop("objects", None)
        project._write_json(vfile, data)
    return {"ok": True, "page_id": page_id, "visual_id": visual_id, "field": field,
            "target": target, "removed": removed}


# --- 2. analytics lines -----------------------------------------------------------------

def add_analytics_line(state: ReportState, page_id: str, visual_id: str, kind: str,
                       value=None, measure: str | None = None,
                       color: str | None = None, style: str = "dashed",
                       label: str | None = None, transparency=None,
                       position: str = "behind", percentile=None) -> dict:
    project = state.require()
    vfile, data = _load(project, page_id, visual_id)
    visual = _visual(data)
    vt = visual["visualType"]
    if vt not in fx.ANALYTICS_TYPES:
        raise ValueError(f"Analytics lines need a cartesian visual "
                         f"({sorted(fx.ANALYTICS_TYPES)}), not {vt!r}")
    if kind == "trend" and vt not in fx.TREND_TYPES:
        raise ValueError(f"Trend lines are only available on {sorted(fx.TREND_TYPES)}")
    measure_expr = None
    if measure is not None:
        query_state = _query_state(visual)
        if measure not in fx.bound_refs(query_state):
            _check_model_ref(project, measure)
        measure_expr = fx.resolve_input(query_state, measure, project._is_measure)
    obj_name, props = fx.build_analytics_line(
        kind, value=value, measure_expr=measure_expr, color=color, style=style,
        label=label, transparency=transparency, position=position,
        percentile=percentile)
    objects = visual.setdefault("objects", {})
    entries = objects.setdefault(obj_name, [])
    # instance selector ids "0", "1", ... — how Desktop keeps several lines apart
    used = {(e.get("selector") or {}).get("id") for e in entries}
    n = 0
    while str(n) in used:
        n += 1
    entries.append({"properties": props, "selector": {"id": str(n)}})
    project._write_json(vfile, data)
    lines = fx.list_lines(objects)
    return {"ok": True, "page_id": page_id, "visual_id": visual_id, "kind": kind,
            "object": obj_name, "index": next(
                (l["index"] for l in lines
                 if l["object"] == obj_name and (l.get("selector") or {}).get("id") == str(n)),
                len(lines) - 1),
            "lines": len(lines)}


def list_analytics_lines(state: ReportState, page_id: str, visual_id: str) -> list[dict]:
    _, data = _load(state.require(), page_id, visual_id)
    return fx.list_lines(_visual(data).get("objects") or {})


def remove_analytics_line(state: ReportState, page_id: str, visual_id: str,
                          index: int) -> dict:
    project = state.require()
    vfile, data = _load(project, page_id, visual_id)
    visual = _visual(data)
    objects = visual.get("objects") or {}
    removed = fx.remove_line(objects, index)
    if not objects:
        visual.pop("objects", None)
    project._write_json(vfile, data)
    return {"ok": True, "page_id": page_id, "visual_id": visual_id,
            "removed": removed, "remaining": len(fx.list_lines(objects))}


# --- 3. tooltip page -----------------------------------------------------------------------

def set_tooltip_page(state: ReportState, page_id: str, visual_id: str,
                     tooltip_page_id: str | None = None) -> dict:
    project = state.require()
    if tooltip_page_id is not None:
        if tooltip_page_id == page_id:
            raise ValueError("a visual cannot use its own page as its tooltip")
        pj = (project._require_report() / "definition" / "pages" / tooltip_page_id
              / "page.json")
        if not pj.exists():
            raise KeyError(f"Page {tooltip_page_id!r} not found")
        page = json.loads(pj.read_text(encoding="utf-8-sig"))
        if (page.get("pageBinding") or {}).get("type") != "Tooltip":
            raise ValueError(
                f"Page {tooltip_page_id!r} is not a tooltip page — run "
                f"pbi_set_page_role({tooltip_page_id!r}, 'tooltip') first.")
    vfile, data = _load(project, page_id, visual_id)
    visual = _visual(data)
    vco = visual.setdefault("visualContainerObjects", {})
    fx.set_tooltip(vco, tooltip_page_id)
    project._write_json(vfile, data)
    return {"ok": True, "page_id": page_id, "visual_id": visual_id,
            "tooltip_page": tooltip_page_id}


# --- 4. slicer ------------------------------------------------------------------------------

def set_slicer(state: ReportState, page_id: str, visual_id: str,
               style: str | None = None, single_select: bool | None = None,
               select_all: bool | None = None, sync_group: str | None = None,
               sync_field_changes: bool | None = None,
               sync_filter_changes: bool | None = None,
               search: bool | None = None, header=None,
               orientation: str | None = None) -> dict:
    project = state.require()
    vfile, data = _load(project, page_id, visual_id)
    visual = _visual(data)
    if visual["visualType"] != "slicer":
        raise ValueError(f"pbi_set_slicer needs a 'slicer' visual, not "
                         f"{visual['visualType']!r}")
    patch = fx.slicer_patch(style=style, single_select=single_select,
                            select_all=select_all, search=search, header=header,
                            orientation=orientation)
    changed = sorted(patch)
    if patch:
        visual["objects"] = merge_objects(visual.get("objects", {}),
                                          build_objects_patch(patch))
    if sync_group is not None:
        if sync_group == "":
            visual.pop("syncGroup", None)
        else:
            visual["syncGroup"] = fx.sync_group(sync_group, sync_field_changes,
                                                sync_filter_changes)
        changed.append("syncGroup")
    elif sync_field_changes is not None or sync_filter_changes is not None:
        raise ValueError("sync_field_changes/sync_filter_changes need sync_group")
    if not changed:
        raise ValueError("nothing to change — pass at least one setting")
    project._write_json(vfile, data)
    return {"ok": True, "page_id": page_id, "visual_id": visual_id, "changed": changed,
            "sync_group": visual.get("syncGroup")}


# --- 5. data labels -----------------------------------------------------------------------------

def set_data_labels(state: ReportState, page_id: str, visual_id: str, show: bool = True,
                    position: str | None = None, display_units=None,
                    decimals: int | None = None, color: str | None = None,
                    font_size=None, font_family: str | None = None,
                    background=None) -> dict:
    project = state.require()
    vfile, data = _load(project, page_id, visual_id)
    visual = _visual(data)
    obj_name, props = fx.data_labels(
        visual["visualType"], show=show, position=position,
        display_units=display_units, decimals=decimals, color=color,
        font_size=font_size, font_family=font_family, background=background)
    visual["objects"] = merge_objects(visual.get("objects", {}),
                                      build_objects_patch({obj_name: props}))
    project._write_json(vfile, data)
    return {"ok": True, "page_id": page_id, "visual_id": visual_id,
            "object": obj_name, "properties": sorted(props)}


# --- 7. visual calculations ------------------------------------------------------------------------

def add_visual_calculation(state: ReportState, page_id: str, visual_id: str,
                           name: str, expression: str,
                           format_string: str | None = None, hidden: bool = False,
                           bucket: str | None = None) -> dict:
    project = state.require()
    vfile, data = _load(project, page_id, visual_id)
    visual = _visual(data)
    query_state = _query_state(visual)
    if not query_state:
        raise ValueError("bind at least one field before adding a visual calculation")
    bucket = bucket or fx.default_calc_bucket(query_state)
    if bucket not in query_state:
        raise ValueError(f"bucket {bucket!r} is not on this visual; have "
                         f"{sorted(query_state)}")
    proj = fx.build_visual_calc_projection(name, expression, format_string, hidden)
    taken = set(fx.bound_refs(query_state)) | set(fx.ensure_native_query_refs(query_state))
    if name in taken:
        raise ValueError(f"{name!r} is already a field or calculation name on this visual")
    query_state[bucket].setdefault("projections", []).append(proj)
    known = fx.ensure_native_query_refs(query_state)
    schema_bumped = fx.ensure_schema_at_least(data)
    project._write_json(vfile, data)
    result = {"ok": True, "page_id": page_id, "visual_id": visual_id, "name": name,
              "bucket": bucket, "schema_upgraded": schema_bumped,
              "field_names": [k for k in known if k != name]}
    unknown = fx.unknown_calc_refs(expression, known)
    if unknown:
        result["warnings"] = [
            f"[{u}] does not match any field on this visual; fields are "
            f"referenced as [nativeQueryRef]: {result['field_names']}" for u in unknown]
    return result


def list_visual_calculations(state: ReportState, page_id: str,
                             visual_id: str) -> list[dict]:
    _, data = _load(state.require(), page_id, visual_id)
    return fx.list_calcs((_visual(data).get("query") or {}).get("queryState", {}))


def remove_visual_calculation(state: ReportState, page_id: str, visual_id: str,
                              name: str) -> dict:
    project = state.require()
    vfile, data = _load(project, page_id, visual_id)
    visual = _visual(data)
    query_state = _query_state(visual)
    for bucket, spec in query_state.items():
        projs = spec.get("projections", [])
        for i, proj in enumerate(projs):
            calc = proj.get("field", {}).get("NativeVisualCalculation")
            if calc and calc.get("Name") == name:
                del projs[i]
                if not projs:
                    del query_state[bucket]
                project._write_json(vfile, data)
                return {"ok": True, "page_id": page_id, "visual_id": visual_id,
                        "removed": name, "bucket": bucket,
                        "remaining": [c["name"] for c in fx.list_calcs(query_state)]}
    raise KeyError(f"No visual calculation named {name!r} on {visual_id!r} "
                   f"(see pbi_list_visual_calculations)")


# --- MCP registration ----------------------------------------------------------------------------------

def register(mcp, state, tool) -> None:

    @tool(write=True, idempotent=True)
    def pbi_set_conditional_format(page_id: str, visual_id: str, field: str,
                                   target: str, rule: dict) -> dict:
        """Apply conditional formatting to a field bound in a visual. field is
        the bound 'Table.Column'/'Table.Measure' (its queryRef). target:
        background|font (table/matrix cells via `values`, chart marks via
        `dataPoint.fill`, chart/card data-label color via `labels.color`),
        data_bars|icons|web_url (table/matrix only). rule shapes — gradient:
        {"kind":"gradient","measure":"T.M","min":{"color":"#hex","value"?:n},
        "mid"?:{...},"max":{...},"null_color"?:"#hex"}; rules: {"kind":"rules",
        "measure":"T.M","rules":[{"min"?:n,"max"?:n,"min_inclusive"?:true,
        "max_inclusive"?:false,"color":"#hex"}],"default_color"?:"#hex"};
        field value: {"kind":"field","measure":"T.M"}; data_bars:
        {"positive_color","negative_color","axis_color"?,"show_bar_only"?,
        "min"?,"max"?}; icons: {"style":"threeArrowsColored","rules":[{"min"?,
        "max"?,"icon":0}],"layout"?:"leftOfData|rightOfData|dataOnly",
        "measure"?}; web_url: {"kind":"field","measure"|"column":"T.F"}.
        Returns the object/property/selector written. Re-running replaces the
        same rule."""
        return set_conditional_format(state, page_id, visual_id, field, target, rule)

    @tool(write=True, destructive=True, idempotent=True)
    def pbi_clear_conditional_format(page_id: str, visual_id: str, field: str,
                                     target: str) -> dict:
        """Remove the conditional format of one target (background|font|
        data_bars|icons|web_url) from a bound field; returns how many
        properties/entries were removed (0 = nothing was set)."""
        return clear_conditional_format(state, page_id, visual_id, field, target)

    @tool(write=True)
    def pbi_add_analytics_line(page_id: str, visual_id: str, kind: str,
                               value: float | None = None,
                               measure: str | None = None,
                               color: str | None = None, style: str = "dashed",
                               label: str | None = None,
                               transparency: float | None = None,
                               position: str = "behind",
                               percentile: float | None = None) -> dict:
        """Add an Analytics-pane line to a cartesian visual. kind: constant
        (needs value) | min | max | average | median | percentile (needs
        percentile 0-100) | trend (line/clustered/scatter/area only). measure
        = 'Table.Measure' the aggregate line summarises (optional); color '#hex';
        style dashed|solid|dotted; transparency 0-100; position behind|front;
        label = legend text. Returns the line's index for
        pbi_remove_analytics_line."""
        return add_analytics_line(state, page_id, visual_id, kind, value, measure,
                                  color, style, label, transparency, position,
                                  percentile)

    @tool(read=True)
    def pbi_list_analytics_lines(page_id: str, visual_id: str) -> list:
        """List a visual's Analytics-pane lines: index, kind, object, value/
        percentile, label, color, style, position, measure."""
        return list_analytics_lines(state, page_id, visual_id)

    @tool(write=True, destructive=True)
    def pbi_remove_analytics_line(page_id: str, visual_id: str, index: int) -> dict:
        """Remove the analytics line at `index` (from pbi_list_analytics_lines)."""
        return remove_analytics_line(state, page_id, visual_id, index)

    @tool(write=True, idempotent=True)
    def pbi_set_tooltip_page(page_id: str, visual_id: str,
                             tooltip_page_id: str | None = None) -> dict:
        """Show a report tooltip page when hovering a visual (visualTooltip type
        'ReportPage' + section). tooltip_page_id must already be a tooltip-role
        page (pbi_set_page_role). Pass tooltip_page_id=None to revert to the
        default tooltip."""
        return set_tooltip_page(state, page_id, visual_id, tooltip_page_id)

    @tool(write=True, idempotent=True)
    def pbi_set_slicer(page_id: str, visual_id: str, style: str | None = None,
                       single_select: bool | None = None,
                       select_all: bool | None = None,
                       sync_group: str | None = None,
                       sync_field_changes: bool | None = None,
                       sync_filter_changes: bool | None = None,
                       search: bool | None = None, header=None,
                       orientation: str | None = None) -> dict:
        """Configure a slicer. style: list|dropdown|tile|between|relative_date|
        before|after (data.mode; tile = list + horizontal). single_select,
        select_all (selection), search (search box), header (true/false or the
        header text), orientation vertical|horizontal. sync_group='Name'
        joins a sync group across pages (syncGroup.groupName with
        fieldChanges/filterChanges, default true); sync_group='' leaves it.
        Only settings you pass change."""
        return set_slicer(state, page_id, visual_id, style, single_select, select_all,
                          sync_group, sync_field_changes, sync_filter_changes,
                          search, header, orientation)

    @tool(write=True, idempotent=True)
    def pbi_set_data_labels(page_id: str, visual_id: str, show: bool = True,
                            position: str | None = None,
                            display_units: str | None = None,
                            decimals: int | None = None,
                            color: str | None = None,
                            font_size: float | None = None,
                            font_family: str | None = None,
                            background: str | None = None) -> dict:
        """Data labels. Charts/pie/donut -> `labels` (show, position e.g.
        OutsideEnd|InsideEnd|InsideCenter|InsideBase for columns, Above|Below|
        Center for lines, Outside|Inside for pie; display_units auto|none|
        thousands|millions|billions|trillions or a number; decimals; color
        '#hex'; font_size; font_family; background '#hex' or false). Card ->
        `labels` (color/size/family/units/decimals). Table/matrix -> `values`
        (color, font_size, font_family, background only). Other visual types
        are rejected."""
        return set_data_labels(state, page_id, visual_id, show, position,
                               display_units, decimals, color, font_size,
                               font_family, background)

    @tool(write=True)
    def pbi_add_visual_calculation(page_id: str, visual_id: str, name: str,
                                   expression: str,
                                   format_string: str | None = None,
                                   hidden: bool = False,
                                   bucket: str | None = None) -> dict:
        """Add a visual calculation (visual-level DAX such as
        'RUNNINGSUM([Net Revenue])' or '[Net Revenue] / COLLAPSE([Net Revenue],
        ROWS)') as a NativeVisualCalculation projection. Fields are referenced
        as [nativeQueryRef] — every existing field gets one (its field name);
        the response lists them and warns about unknown references. bucket
        defaults to Values/Y/Data. hidden=true keeps a helper calculation out
        of the visual. Note: pbi_update_bindings rewrites the query and drops
        visual calculations."""
        return add_visual_calculation(state, page_id, visual_id, name, expression,
                                      format_string, hidden, bucket)

    @tool(read=True)
    def pbi_list_visual_calculations(page_id: str, visual_id: str) -> list:
        """List a visual's visual calculations: bucket, name, expression,
        format, hidden."""
        return list_visual_calculations(state, page_id, visual_id)

    @tool(write=True, destructive=True)
    def pbi_remove_visual_calculation(page_id: str, visual_id: str, name: str) -> dict:
        """Remove a visual calculation by name."""
        return remove_visual_calculation(state, page_id, visual_id, name)
