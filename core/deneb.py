"""Deneb (Vega / Vega-Lite) custom visual support -- templates + spec editing.

Deneb is an AppSource custom visual that renders a Vega or Vega-Lite spec
against the fields bound to it. This module builds ``visual.json`` containers
for it: the bound fields become the visual's ``dataset`` role projections and
the spec (a JSON string) lives in the ``vega`` formatting object.

The Deneb custom visual itself must be available for these visuals to render:
listed in the report (``report.json`` ``publicCustomVisuals`` -- done by
:func:`ensure_registered`) so Desktop can fetch it from AppSource, or provided
by the organization. Without it Desktop shows a "can't display this visual"
placeholder; the files written here stay valid either way.

Identifiers below were resolved from the Deneb repository (a monorepo; the
visual lives under ``apps/deneb``) on 2026-09-28 -- nothing is fetched at
runtime:

* https://raw.githubusercontent.com/deneb-viz/deneb/main/apps/deneb/pbiviz.json
  ``visual.guid`` = ``deneb7E15AEF80B9E4D4F8E12924291ECE89A`` (visual 2.0.0.0)
* https://raw.githubusercontent.com/deneb-viz/deneb/main/apps/deneb/capabilities.json
  ``dataRoles`` = one ``dataset`` role (GroupingOrMeasure); the ``vega`` object
  carries the spec: ``jsonSpec``, ``jsonConfig``, ``provider``
  (``vegaLite`` | ``vega``), ``version`` (provider version), ``renderMode``
  (``svg`` | ``canvas``), ``logLevel`` and the ``enable*`` interactivity flags
* https://raw.githubusercontent.com/deneb-viz/deneb/main/packages/configuration/src/index.ts
  ``PROJECT_DEFAULTS``: provider ``vegaLite``, renderMode ``svg``
* https://raw.githubusercontent.com/deneb-viz/deneb/main/apps/deneb/package.json
  bundled ``vega-lite`` 6.4.3 / ``vega`` 6.4.0
* https://raw.githubusercontent.com/deneb-viz/deneb/main/packages/data-core/src/lib/field/encoding.ts
  dataset field name = the field's display name with ``\\ " . [ ]`` -> ``_``
* https://raw.githubusercontent.com/deneb-viz/deneb/main/packages/vega-runtime/src/lib/spec-processing/patch-data.ts
  the dataset is injected as ``datasets.dataset`` (Vega-Lite) / the ``dataset``
  data entry (Vega), so specs must read ``{"name": "dataset"}``
* .../spec-processing/patch-vega-lite.ts: Deneb sets ``width``/``height`` to
  ``"container"`` when a spec omits them, so the templates leave them out.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Callable

from core.formatting import _expr_literal

# --- resolved identifiers (see the module docstring for sources) ----------------

DENEB_VISUAL_GUID = "deneb7E15AEF80B9E4D4F8E12924291ECE89A"
DENEB_VISUAL_VERSION = "2.0.0.0"
DATA_ROLE = "dataset"            # queryState bucket
DATASET_NAME = "dataset"         # data name the spec must read
SPEC_OBJECT = "vega"             # objects.vega.*
PROVIDERS = ("vegaLite", "vega")
PROVIDER_VERSIONS = {"vegaLite": "6.4.3", "vega": "6.4.0"}
SCHEMA_URLS = {"vegaLite": "https://vega.github.io/schema/vega-lite/v6.json",
               "vega": "https://vega.github.io/schema/vega/v6.json"}
#: vega-object properties from capabilities.json -> allowed values (None = free)
VEGA_PROPERTIES: dict[str, tuple | None] = {
    "jsonSpec": None, "jsonConfig": None, "provider": PROVIDERS, "version": None,
    "logLevel": ("0", "1", "2", "3", "4"), "renderMode": ("svg", "canvas"),
    "enableTooltips": (True, False), "enableContextMenu": (True, False),
    "enableContextMenuSelector": (True, False), "enableHighlight": (True, False),
    "enableSelection": (True, False), "selectionMode": ("simple", "advanced"),
    "selectionMaxDataPoints": None, "tooltipDelay": None,
}

NEEDS_DENEB = ("The Deneb custom visual must be present for this visual to render: "
               "it is registered in report.json publicCustomVisuals so Desktop can "
               "fetch it from AppSource, or it must be provided by your organization.")

_AGG_RE = re.compile(r"^(Sum|Average|Avg|Count|DistinctCount|Min|Max|Median)"
                     r"\(([^)]+)\)$", re.IGNORECASE)
_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
_RESERVED = re.compile(r'([\\".\[\]])')
_NUMERIC = {"int64", "double", "decimal", "currency", "number"}
_DATES = {"datetime", "date", "time", "datetimezone"}
_BACKPLATE_TYPES = {"shape", "basicShape", "image"}


def is_deneb_type(visual_type: str | None) -> bool:
    return bool(visual_type) and visual_type.lower().startswith("deneb")


def register_visual_spec() -> None:
    """Teach the generic binding tools (pbi_update_bindings, pbi_add_visual,
    pbi_capabilities) that a Deneb visual has one bucket, ``dataset``, so its
    fields can be changed like any other visual's. Never overrides an entry."""
    from core.visual_specs import VISUAL_SPECS

    VISUAL_SPECS.setdefault(DENEB_VISUAL_GUID, {"required": [DATA_ROLE], "optional": []})


register_visual_spec()


def dataset_field_name(display_name: str) -> str:
    """The name Deneb gives a field in the dataset (reserved characters -> '_')."""
    return _RESERVED.sub("_", display_name)


# --- fields ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DField:
    ref: str                  # as passed: "Table.Field" or "Sum(Table.Field)"
    entity: str
    prop: str
    name: str                 # dataset field name
    is_measure: bool
    data_type: str | None = None
    agg: str | None = None

    @property
    def is_numeric(self) -> bool:
        return self.is_measure or self.agg is not None \
            or (self.data_type or "").lower() in _NUMERIC or self.data_type is None

    @property
    def vl_type(self) -> str:
        """Vega-Lite type when the field is used as a dimension."""
        if self.is_measure or self.agg:
            return "quantitative"
        dt = (self.data_type or "").lower()
        if dt in _DATES:
            return "temporal"
        if dt in _NUMERIC:
            return "ordinal"       # numeric key such as Year: discrete
        return "nominal"


def resolve_field(project, ref: str) -> DField:
    """Resolve 'Table.Field' (or 'Sum(Table.Field)') against the model."""
    if not isinstance(ref, str) or not ref.strip():
        raise ValueError("Field references must be non-empty strings like 'Table.Field'")
    ref = ref.strip()
    agg = _AGG_RE.match(ref)
    target = agg.group(2).strip() if agg else ref
    entity, _, prop = target.partition(".")
    if not entity or not prop:
        raise ValueError(f"Field {ref!r} must look like 'Table.Column' or 'Table.Measure'")
    try:
        tables = {t.name: t for t in project.list_tables()}
    except FileNotFoundError as e:
        raise ValueError("Deneb visuals bind model fields, but this project has no "
                         "semantic model layer.") from e
    if entity not in tables:
        raise ValueError(f"Unknown table {entity!r} in {ref!r}; tables: {sorted(tables)}")
    table = tables[entity]
    measure = next((m for m in table.measures if m.name == prop), None)
    column = next((c for c in table.columns if c.name == prop), None)
    if measure is None and column is None:
        raise ValueError(f"{ref!r} is neither a column nor a measure of table {entity!r}; "
                         f"columns: {[c.name for c in table.columns]}, "
                         f"measures: {[m.name for m in table.measures]}")
    if agg and measure is not None:
        raise ValueError(f"{ref!r}: measures cannot be aggregated; bind {target!r} directly")
    return DField(ref=ref, entity=entity, prop=prop, name=dataset_field_name(prop),
                  is_measure=measure is not None and column is None,
                  data_type=column.data_type if column is not None else None,
                  agg=agg.group(1) if agg else None)


# --- options ---------------------------------------------------------------------------

def _opt(kind: str, default, description: str, choices: tuple | None = None) -> dict:
    out = {"type": kind, "default": default, "description": description}
    if choices:
        out["choices"] = list(choices)
    return out


_OPT_COLOR = _opt("string", None, "Mark color '#RRGGBB'. Default: the report accent "
                                  "(first data color of the active theme).")
_OPT_PALETTE = _opt("array", None, "Series colors, a list of '#RRGGBB'. Default: the "
                                   "active theme's data colors. Written to the Vega "
                                   "config's range.category.")
_OPT_LABELS = _opt("boolean", False, "Show data labels.")
_OPT_FORMAT = _opt("string", None, "d3-format for axis ticks and labels, e.g. ',.0f', "
                                   "'$,.2f', '.1%', '~s'. Default: automatic (labels: ',.2~f').")
_OPT_ORIENT_H = _opt("string", "horizontal", "Bar direction.", ("horizontal", "vertical"))
_OPT_ORIENT_V = _opt("string", "vertical", "Direction.", ("vertical", "horizontal"))
_OPT_SORT = _opt("string", "auto", "Category order: auto (largest first for text categories, "
                                   "natural order for dates/numbers), natural, ascending "
                                   "or descending (by value).",
                 ("auto", "natural", "ascending", "descending"))
_OPT_INTERP = _opt("string", "linear", "Line interpolation.", ("linear", "monotone", "step"))
_OPT_NORMALIZE = _opt("boolean", False, "100% stacked: each category sums to 100%.")


@dataclass(frozen=True)
class Role:
    name: str
    kind: str          # dimension | measure
    required: bool
    description: str


@dataclass(frozen=True)
class Template:
    name: str
    title: str
    description: str
    roles: tuple
    options: dict
    build: Callable
    example: dict
    aliases: tuple = ()      # ((alias, role), ...) accepted spellings of a role


def _validate_options(tpl: Template, options: dict | None) -> dict:
    """Fill defaults; reject unknown names and wrongly typed values."""
    options = options or {}
    if not isinstance(options, dict):
        raise ValueError("options must be an object like {\"color\": \"#118DFF\"}")
    unknown = sorted(set(options) - set(tpl.options))
    if unknown:
        raise ValueError(f"Unknown option(s) {unknown} for template {tpl.name!r}; "
                         f"allowed: {sorted(tpl.options)}")
    out = {}
    for name, spec in tpl.options.items():
        value = options.get(name, spec["default"])
        kind = spec["type"]
        if value is not None and name in options:
            if kind == "boolean" and not isinstance(value, bool):
                raise ValueError(f"option {name!r} must be true or false")
            if kind == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
                raise ValueError(f"option {name!r} must be an integer")
            if kind == "number" and (not isinstance(value, (int, float)) or isinstance(value, bool)):
                raise ValueError(f"option {name!r} must be a number")
            if kind == "string" and not isinstance(value, str):
                raise ValueError(f"option {name!r} must be a string")
            if kind == "array" and not isinstance(value, list):
                raise ValueError(f"option {name!r} must be a list")
            if "choices" in spec and value not in spec["choices"]:
                raise ValueError(f"option {name!r} must be one of {spec['choices']}")
            if name == "color" and not _HEX.match(value):
                raise ValueError("option 'color' must look like '#RRGGBB'")
            if name == "palette" and (not value or not all(
                    isinstance(c, str) and _HEX.match(c) for c in value)):
                raise ValueError("option 'palette' must be a non-empty list of '#RRGGBB'")
            if name == "bins" and not (2 <= value <= 100):
                raise ValueError("option 'bins' must be between 2 and 100")
            if name == "columns" and not (2 <= value <= 50):
                raise ValueError("option 'columns' must be between 2 and 50")
        out[name] = value
    return out


# --- spec building blocks -----------------------------------------------------------------

def _mix(c1: str, c2: str, t: float) -> str:
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(a[i] + (b[i] - a[i]) * t):02X}" for i in range(3))


def _spec(**body) -> dict:
    out = {"$schema": SCHEMA_URLS["vegaLite"], "data": {"name": DATASET_NAME}}
    out.update(body)
    return out


def _dim(f: DField, *, discrete: bool = False, **extra) -> dict:
    """Encoding for a dimension; dates become discrete when `discrete`."""
    d = {"field": f.name, "type": f.vl_type}
    if discrete and d["type"] == "temporal":
        d["type"] = "ordinal"
        d["timeUnit"] = "yearmonthdate"
        d["title"] = f.name
    d.update(extra)
    return d


def _val(f: DField, fmt: str | None = None, **extra) -> dict:
    d = {"field": f.name, "type": "quantitative"}
    if fmt:
        d["axis"] = {"format": fmt}
    d.update(extra)
    return d


def _label_fmt(o: dict) -> str:
    return o.get("format") or ",.2~f"


def _sort_for(cat: DField, mode: str, value_channel: str):
    """Vega-Lite `sort` for a category axis."""
    if mode == "natural":
        return None
    if mode == "ascending":
        return value_channel
    if mode == "descending" or (mode == "auto" and cat.vl_type == "nominal"):
        return "-" + value_channel
    return None


def _series(f: DField) -> dict:
    return {"field": f.name, "type": "nominal", "legend": {"title": f.name}}


# --- templates ---------------------------------------------------------------------------------

def _t_bar(f: dict, o: dict, stacked: bool = False) -> dict:
    cat, val, ser = f["category"], f["value"], f.get("series")
    horiz = o["orientation"] == "horizontal"
    cch, vch = ("y", "x") if horiz else ("x", "y")
    normalize = stacked and o.get("normalize")
    enc = {cch: _dim(cat, discrete=True), vch: _val(val, o["format"])}
    srt = _sort_for(cat, o["sort"], vch)
    if srt:
        enc[cch]["sort"] = srt
    if normalize:
        enc[vch]["stack"] = "normalize"
        enc[vch]["axis"] = {"format": "%"}
    mark = {"type": "bar", "tooltip": True}
    if ser:
        enc["color"] = _series(ser)
        if not stacked:
            enc[cch + "Offset"] = {"field": ser.name}
        else:                        # one explicit stack order for bars and labels
            enc["order"] = {"field": ser.name, "type": "nominal",
                            "sort": "ascending" if horiz else "descending"}
    else:
        mark["color"] = o["color"]
    if not o["labels"]:
        return _spec(mark=mark, encoding=enc)
    inside = stacked and ser
    text_layer: dict = {}
    text_enc = {"text": {"field": val.name, "type": "quantitative", "format": _label_fmt(o)},
                "color": {"value": "#FFFFFF" if inside else "#252423"}}
    if normalize:                    # label the share, not the raw value
        text_layer["transform"] = [
            {"joinaggregate": [{"op": "sum", "field": val.name, "as": "snTotal"}],
             "groupby": [cat.name]},
            {"calculate": f"datum[{json.dumps(val.name)}] / datum.snTotal", "as": "snShare"}]
        text_enc["text"] = {"field": "snShare", "type": "quantitative", "format": ".0%"}
    if inside:
        text_enc[vch] = {"field": val.name, "type": "quantitative",
                         "stack": "normalize" if normalize else "zero", "bandPosition": 0.5}
        text_enc["detail"] = {"field": ser.name, "type": "nominal"}   # same stack groups as the bars
        tmark = {"type": "text", "align": "center", "baseline": "middle"}
    elif horiz:
        tmark = {"type": "text", "align": "left", "baseline": "middle", "dx": 4}
    else:
        tmark = {"type": "text", "align": "center", "baseline": "bottom", "dy": -3}
    return _spec(encoding=enc, layer=[
        {"mark": mark}, {**text_layer, "mark": tmark, "encoding": text_enc}])


def _t_line(f: dict, o: dict) -> dict:
    cat, val, ser = f["category"], f["value"], f.get("series")
    enc = {"x": _dim(cat), "y": _val(val, o["format"])}
    mark = {"type": "line", "tooltip": True, "strokeWidth": 2.5,
            "interpolate": o["interpolate"], "point": bool(o["points"])}
    if ser:
        enc["color"] = _series(ser)
    else:
        mark["color"] = o["color"]
    if not o["labels"]:
        return _spec(mark=mark, encoding=enc)
    tmark = {"type": "text", "baseline": "bottom", "dy": -8}
    text_enc = {"text": {"field": val.name, "type": "quantitative", "format": _label_fmt(o)},
                "color": {"value": "#252423"}}
    return _spec(encoding=enc, layer=[{"mark": mark}, {"mark": tmark, "encoding": text_enc}])


def _t_area(f: dict, o: dict) -> dict:
    cat, val, ser = f["category"], f["value"], f.get("series")
    enc = {"x": _dim(cat), "y": _val(val, o["format"])}
    if ser and o["normalize"]:
        enc["y"]["stack"] = "normalize"
        enc["y"]["axis"] = {"format": "%"}
    mark = {"type": "area", "tooltip": True, "interpolate": o["interpolate"],
            "line": True, "opacity": 0.8}
    if ser:
        enc["color"] = _series(ser)
    else:
        mark["color"] = o["color"]
    return _spec(mark=mark, encoding=enc)


def _t_scatter(f: dict, o: dict) -> dict:
    det, x, y = f["detail"], f["x"], f["y"]
    size, ser = f.get("size"), f.get("series")
    shared = {"x": _val(x, o["format"], scale={"zero": False}),
              "y": _val(y, scale={"zero": False}),
              "detail": {"field": det.name, "type": "nominal"}}
    mark = {"type": "point", "filled": True, "opacity": 0.8, "tooltip": True}
    point_enc: dict = {}
    if size:
        point_enc["size"] = {"field": size.name, "type": "quantitative",
                             "legend": {"title": size.name}}
    else:
        mark["size"] = 90
    if ser:
        point_enc["color"] = _series(ser)
    else:
        mark["color"] = o["color"]
    if not o["labels"]:
        return _spec(mark=mark, encoding={**shared, **point_enc})
    tmark = {"type": "text", "align": "left", "dx": 8, "fontSize": 10, "color": "#252423"}
    return _spec(encoding=shared, layer=[
        {"mark": mark, "encoding": point_enc},
        {"mark": tmark, "encoding": {"text": {"field": det.name, "type": "nominal"}}}])


def _t_heatmap(f: dict, o: dict) -> dict:
    x, y, val = f["x"], f["y"], f["value"]
    color = o["color"]
    enc = {"x": _dim(x, discrete=True), "y": _dim(y, discrete=True),
           "color": {"field": val.name, "type": "quantitative",
                     "scale": {"range": [_mix("#FFFFFF", color, 0.12), color]},
                     "legend": {"title": val.name}}}
    mark = {"type": "rect", "tooltip": True}
    if not o["labels"]:
        return _spec(mark=mark, encoding=enc)
    ref = json.dumps(val.name)
    text_enc = {
        "text": {"field": val.name, "type": "quantitative", "format": _label_fmt(o)},
        "color": {"condition": {"test": f"datum[{ref}] > 0.55 * datum.hmMax", "value": "#FFFFFF"},
                  "value": "#252423"},
    }
    return _spec(
        transform=[{"joinaggregate": [{"op": "max", "field": val.name, "as": "hmMax"}]}],
        encoding=enc,
        layer=[{"mark": mark},
               {"mark": {"type": "text", "baseline": "middle"}, "encoding": text_enc}])


def _t_histogram(f: dict, o: dict) -> dict:
    val = f["value"]
    horiz = o["orientation"] == "horizontal"
    bin_ch, cnt_ch = ("y", "x") if horiz else ("x", "y")
    enc = {bin_ch: {"field": val.name, "type": "quantitative",
                    "bin": {"maxbins": o["bins"]}, "title": val.name},
           cnt_ch: {"aggregate": "count", "type": "quantitative", "title": "Count",
                    "axis": {"tickMinStep": 1}}}
    return _spec(mark={"type": "bar", "color": o["color"], "tooltip": True, "binSpacing": 1},
                 encoding=enc)


def _t_box(f: dict, o: dict) -> dict:
    cat, val = f.get("category"), f["value"]
    horiz = o["orientation"] == "horizontal"
    cch, vch = ("y", "x") if horiz else ("x", "y")
    enc = {vch: _val(val, o["format"], scale={"zero": False})}
    if cat:
        enc[cch] = _dim(cat, discrete=True)
    mark = {"type": "boxplot", "extent": 1.5, "color": o["color"],
            "median": {"color": "#252423"}}
    return _spec(mark=mark, encoding=enc)


def _t_bullet(f: dict, o: dict) -> dict:
    cat, val, tgt = f["category"], f["value"], f["target"]
    horiz = o["orientation"] == "horizontal"
    cch, vch = ("y", "x") if horiz else ("x", "y")
    thick = "height" if horiz else "width"
    axis_title = None
    enc = {cch: _dim(cat, discrete=True)}
    layers = [
        {"mark": {"type": "bar", "color": "#E1DFDD", thick: {"band": 0.85}},
         "encoding": {vch: _val(tgt, o["format"], title=axis_title)}},
        {"mark": {"type": "bar", "color": o["color"], "tooltip": True, thick: {"band": 0.35}},
         "encoding": {vch: _val(val, o["format"], title=axis_title)}},
        {"mark": {"type": "tick", "color": "#252423", "thickness": 2,
                  "size": 18, "orient": "vertical" if horiz else "horizontal"},
         "encoding": {vch: _val(tgt, o["format"], title=axis_title)}},
    ]
    if o["labels"]:
        layers.append({
            "mark": ({"type": "text", "align": "left", "baseline": "middle", "dx": 6,
                      "color": "#252423"} if horiz else
                     {"type": "text", "align": "center", "baseline": "bottom", "dy": -6,
                      "color": "#252423"}),
            "encoding": {vch: _val(val, o["format"], title=axis_title),
                         "text": {"field": val.name, "type": "quantitative",
                                  "format": _label_fmt(o)}}})
    return _spec(encoding=enc, layer=layers)


def _t_sparkline(f: dict, o: dict) -> dict:
    cat, val = f["category"], f["value"]
    x = _dim(cat, axis=None)
    y = {"field": val.name, "type": "quantitative", "axis": None, "scale": {"zero": False}}
    layers = [{"mark": {"type": "line", "color": o["color"], "strokeWidth": 2,
                        "interpolate": o["interpolate"]}}]
    if o["end_point"]:
        layers.append({
            "transform": [
                {"window": [{"op": "row_number", "as": "spRow"}],
                 "sort": [{"field": cat.name, "order": "ascending"}]},
                {"joinaggregate": [{"op": "max", "field": "spRow", "as": "spLast"}]},
                {"filter": "datum.spRow === datum.spLast"}],
            "mark": {"type": "circle", "color": o["color"], "size": 70, "tooltip": True}})
    return _spec(encoding={"x": x, "y": y}, layer=layers)


def _t_waffle(f: dict, o: dict) -> dict:
    cat, val = f["category"], f["value"]
    cols = o["columns"]
    v = json.dumps(val.name)
    transform = [
        {"joinaggregate": [{"op": "sum", "field": val.name, "as": "wfTotal"}]},
        {"calculate": f"round(datum[{v}] / datum.wfTotal * 100)", "as": "wfCount"},
        {"window": [{"op": "sum", "field": "wfCount", "as": "wfEnd"}],
         "sort": [{"field": cat.name, "order": "ascending"}], "frame": [None, 0]},
        {"calculate": "datum.wfEnd - datum.wfCount", "as": "wfStart"},
        {"calculate": "sequence(datum.wfCount)", "as": "wfSeq"},
        {"flatten": ["wfSeq"]},
        {"calculate": "datum.wfStart + datum.wfSeq", "as": "wfCell"},
        {"calculate": f"datum.wfCell % {cols}", "as": "wfCol"},
        {"calculate": f"floor(datum.wfCell / {cols})", "as": "wfRow"},
    ]
    enc = {"x": {"field": "wfCol", "type": "ordinal", "axis": None,
                 "scale": {"paddingInner": 0.12}},
           "y": {"field": "wfRow", "type": "ordinal", "axis": None, "sort": "descending",
                 "scale": {"paddingInner": 0.12}},
           "color": _series(cat),
           "tooltip": [{"field": cat.name, "type": "nominal"},
                       {"field": val.name, "type": "quantitative", "format": _label_fmt(o)}]}
    return _spec(transform=transform, mark={"type": "rect", "cornerRadius": 2}, encoding=enc)


def _t_dumbbell(f: dict, o: dict) -> dict:
    cat, a, b = f["category"], f["start"], f["end"]
    horiz = o["orientation"] == "horizontal"
    cch, vch = ("y", "x") if horiz else ("x", "y")
    v2 = vch + "2"
    fold = [{"fold": [a.name, b.name], "as": ["dbMeasure", "dbValue"]}]
    point_enc = {vch: _val_named("dbValue", o["format"], "Value"),
                 "color": {"field": "dbMeasure", "type": "nominal",
                           "scale": {"domain": [a.name, b.name]},
                           "legend": {"title": None}}}
    layers = [
        {"mark": {"type": "rule", "color": "#C8C6C4", "strokeWidth": 3},
         "encoding": {vch: _val(a, o["format"], title="Value"), v2: {"field": b.name}}},
        {"transform": fold, "mark": {"type": "circle", "size": 130, "opacity": 1, "tooltip": True},
         "encoding": point_enc},
    ]
    if o["labels"]:
        layers.append({
            "transform": fold,
            "mark": ({"type": "text", "baseline": "bottom", "dy": -10, "color": "#252423"}
                     if horiz else
                     {"type": "text", "align": "left", "dx": 10, "color": "#252423"}),
            "encoding": {vch: {"field": "dbValue", "type": "quantitative"},
                         "text": {"field": "dbValue", "type": "quantitative",
                                  "format": _label_fmt(o)}}})
    return _spec(encoding={cch: _dim(cat, discrete=True)}, layer=layers)


def _val_named(name: str, fmt: str | None, title: str) -> dict:
    d = {"field": name, "type": "quantitative", "title": title}
    if fmt:
        d["axis"] = {"format": fmt}
    return d


def _role(name, kind, required, text):
    return Role(name, kind, required, text)


_COMMON = {"color": _OPT_COLOR, "palette": _OPT_PALETTE}

TEMPLATES: dict[str, Template] = {}


def _register(tpl: Template) -> None:
    TEMPLATES[tpl.name] = tpl


_register(Template(
    "bar", "Bar / column chart",
    "Bars of a measure per category; optional series makes clustered bars. "
    "Horizontal by default (Power BI's 'bar'), vertical = column chart.",
    (_role("category", "dimension", True, "Column on the category axis."),
     _role("value", "measure", True, "Measure (or numeric column) that sets the bar length."),
     _role("series", "dimension", False, "Column that splits bars into colored, clustered series.")),
    {**_COMMON, "orientation": _OPT_ORIENT_H, "labels": _OPT_LABELS, "format": _OPT_FORMAT,
     "sort": _OPT_SORT},
    lambda f, o: _t_bar(f, o, stacked=False),
    {"bindings": {"category": "Date.Year", "value": "Sales.Net Revenue"}}))

_register(Template(
    "stacked_bar", "Stacked bar / column chart",
    "Bars of a measure per category, stacked by series (optionally 100%).",
    (_role("category", "dimension", True, "Column on the category axis."),
     _role("value", "measure", True, "Measure (or numeric column) that sets the stack size."),
     _role("series", "dimension", True, "Column that splits each bar into stacked segments.")),
    {**_COMMON, "orientation": _OPT_ORIENT_H, "labels": _OPT_LABELS, "format": _OPT_FORMAT,
     "sort": _OPT_SORT, "normalize": _OPT_NORMALIZE},
    lambda f, o: _t_bar(f, o, stacked=True),
    {"bindings": {"category": "Date.Year", "value": "Sales.Net Revenue",
                  "series": "Sales.OrderDate"}}))

_register(Template(
    "line", "Line chart",
    "A measure over an ordered axis (dates or numbers); optional series draws one line each.",
    (_role("category", "dimension", True, "Column on the x axis (date or ordered key)."),
     _role("value", "measure", True, "Measure (or numeric column) on the y axis."),
     _role("series", "dimension", False, "Column that splits the data into one line per member.")),
    {**_COMMON, "points": _opt("boolean", False, "Draw a marker at every point."),
     "interpolate": _OPT_INTERP, "labels": _OPT_LABELS, "format": _OPT_FORMAT},
    _t_line,
    {"bindings": {"category": "Date.Date", "value": "Sales.Net Revenue"}},
    aliases=(("x", "category"), ("y", "value"))))

_register(Template(
    "area", "Area chart",
    "Filled area of a measure over an ordered axis; a series stacks the areas.",
    (_role("category", "dimension", True, "Column on the x axis (date or ordered key)."),
     _role("value", "measure", True, "Measure (or numeric column) on the y axis."),
     _role("series", "dimension", False, "Column that stacks the area by member.")),
    {**_COMMON, "interpolate": _OPT_INTERP, "normalize": _OPT_NORMALIZE, "format": _OPT_FORMAT},
    _t_area,
    {"bindings": {"category": "Date.Date", "value": "Sales.Net Revenue"}},
    aliases=(("x", "category"), ("y", "value"))))

_register(Template(
    "scatter", "Scatter / bubble chart",
    "One point per member of `detail`, positioned by two measures; size adds a "
    "third measure (bubble), series colors the points.",
    (_role("detail", "dimension", True, "Column whose members become the points."),
     _role("x", "measure", True, "Measure (or numeric column) on the x axis."),
     _role("y", "measure", True, "Measure (or numeric column) on the y axis."),
     _role("size", "measure", False, "Measure that sets the point size."),
     _role("series", "dimension", False, "Column that colors the points.")),
    {**_COMMON, "labels": _opt("boolean", False, "Label each point with its `detail` member."),
     "format": _OPT_FORMAT},
    _t_scatter,
    {"bindings": {"detail": "Date.Year", "x": "Sales.Net Revenue", "y": "Sales.Margin %"}}))

_register(Template(
    "heatmap", "Heatmap",
    "A grid of two dimensions with each cell colored by a measure.",
    (_role("x", "dimension", True, "Column across the top."),
     _role("y", "dimension", True, "Column down the side."),
     _role("value", "measure", True, "Measure that sets the cell color.")),
    {"color": _opt("string", None, "Color of the highest cells '#RRGGBB'. Default: the "
                                    "report accent."),
     "palette": _OPT_PALETTE, "labels": _OPT_LABELS, "format": _OPT_FORMAT},
    _t_heatmap,
    {"bindings": {"x": "Date.Year", "y": "Sales.OrderDate", "value": "Sales.Net Revenue"}}))

_register(Template(
    "histogram", "Histogram",
    "Distribution of a measure across the members of `detail`: members are "
    "binned by value and counted.",
    (_role("detail", "dimension", True, "Column whose members are counted (one row each)."),
     _role("value", "measure", True, "Measure (or numeric column) to bin.")),
    {"color": _OPT_COLOR, "palette": _OPT_PALETTE, "orientation": _OPT_ORIENT_V,
     "bins": _opt("integer", 10, "Maximum number of bins (2-100).")},
    _t_histogram,
    {"bindings": {"detail": "Date.Year", "value": "Sales.Net Revenue"}}))

_register(Template(
    "box_plot", "Box plot",
    "Median, quartiles and whiskers of a measure across the members of `detail`, "
    "one box per category (or a single box without a category).",
    (_role("category", "dimension", False, "Column that gives one box per member."),
     _role("detail", "dimension", True, "Column whose members are the observations."),
     _role("value", "measure", True, "Measure (or numeric column) that is summarized.")),
    {"color": _OPT_COLOR, "palette": _OPT_PALETTE, "orientation": _OPT_ORIENT_V,
     "format": _OPT_FORMAT},
    _t_box,
    {"bindings": {"category": "Date.Year", "detail": "Date.Date", "value": "Sales.Net Revenue"}}))

_register(Template(
    "bullet", "Bullet chart",
    "Actual vs target per category: a thin bar for the value over a wide track "
    "up to the target, with a tick at the target.",
    (_role("category", "dimension", True, "Column that gives one bullet per member."),
     _role("value", "measure", True, "Measure for the actual value (thin bar)."),
     _role("target", "measure", True, "Measure for the target (track and tick).")),
    {"color": _OPT_COLOR, "palette": _OPT_PALETTE, "orientation": _OPT_ORIENT_H,
     "labels": _OPT_LABELS, "format": _OPT_FORMAT},
    _t_bullet,
    {"bindings": {"category": "Date.Year", "value": "Sales.Net Revenue",
                  "target": "Sales.Complex Measure"}}))

_register(Template(
    "sparkline", "Sparkline",
    "A minimal axis-free line of a measure with a marker on the last point; "
    "sized for small tiles and table cells.",
    (_role("category", "dimension", True, "Column that orders the line (date or key)."),
     _role("value", "measure", True, "Measure (or numeric column) that is plotted.")),
    {"color": _OPT_COLOR, "palette": _OPT_PALETTE, "interpolate": _opt(
        "string", "monotone", "Line interpolation.", ("linear", "monotone", "step")),
     "end_point": _opt("boolean", True, "Mark the last point.")},
    _t_sparkline,
    {"bindings": {"category": "Date.Date", "value": "Sales.Net Revenue"}},
    aliases=(("x", "category"), ("y", "value"))))

_register(Template(
    "waffle", "Waffle chart",
    "A grid of 100 squares split between categories in proportion to a measure "
    "(counts are rounded, so the grid can hold 99-101 cells).",
    (_role("category", "dimension", True, "Column whose members share the grid."),
     _role("value", "measure", True, "Measure that sets each member's share.")),
    {"palette": _OPT_PALETTE, "format": _OPT_FORMAT,
     "columns": _opt("integer", 10, "Squares per row (2-50).")},
    _t_waffle,
    {"bindings": {"category": "Date.Year", "value": "Sales.Net Revenue"}}))

_register(Template(
    "dumbbell", "Dumbbell chart",
    "Two measures per category joined by a line: compare a start and an end "
    "value (before/after, plan/actual).",
    (_role("category", "dimension", True, "Column that gives one dumbbell per member."),
     _role("start", "measure", True, "Measure for the first dot."),
     _role("end", "measure", True, "Measure for the second dot.")),
    {"palette": _OPT_PALETTE, "orientation": _OPT_ORIENT_H,
     "labels": _OPT_LABELS, "format": _OPT_FORMAT},
    _t_dumbbell,
    {"bindings": {"category": "Date.Year", "start": "Sales.Net Revenue",
                  "end": "Sales.Margin %"}}))


def list_templates() -> list[dict]:
    """Template catalogue: roles (bindings), options and an example each."""
    out = []
    for tpl in TEMPLATES.values():
        out.append({
            "name": tpl.name,
            "title": tpl.title,
            "description": tpl.description,
            "bindings": [{"role": r.name, "required": r.required, "kind": r.kind,
                          "description": r.description} for r in tpl.roles],
            "options": tpl.options,
            "example": {"template": tpl.name, **tpl.example},
            **({"aliases": dict(tpl.aliases)} if tpl.aliases else {}),
        })
    return out


# --- config + theme -------------------------------------------------------------------------------

def default_config(palette: list[str], foreground: str = "#252423",
                   muted: str = "#605E5C", font: str = "Segoe UI") -> dict:
    """Vega config (Deneb's ``jsonConfig``) that matches the report look."""
    text = {"labelFont": font, "titleFont": font, "labelColor": muted, "titleColor": foreground,
            "labelFontSize": 11, "titleFontSize": 11, "titleFontWeight": "normal"}
    return {
        "view": {"stroke": "transparent"},
        "font": font,
        "axis": {**text, "gridColor": "#E1DFDD", "domainColor": "#C8C6C4",
                 "tickColor": "#C8C6C4"},
        "legend": dict(text),
        "range": {"category": list(palette)},
        "bar": {"cornerRadiusEnd": 3},
        "area": {"line": True},
        "line": {"strokeCap": "round", "strokeJoin": "round"},
        "text": {"font": font},
    }


def project_theme(project) -> dict:
    """{palette, foreground, muted} from the report's active theme (or defaults)."""
    from core.render import load_theme

    theme = load_theme(project)
    return {"palette": list(theme["dataColors"]), "foreground": theme["foreground"],
            "muted": theme.get("foregroundNeutralSecondary") or "#605E5C",
            "accent": theme["accent"]}


# --- fields -> spec ---------------------------------------------------------------------------------

def _normalise_bindings(tpl: Template, bindings: dict) -> dict[str, str]:
    if not isinstance(bindings, dict) or not bindings:
        raise ValueError(f"bindings must be an object mapping roles to 'Table.Field', "
                         f"e.g. {tpl.example['bindings']}")
    known = {r.name for r in tpl.roles}
    aliases = dict(tpl.aliases)
    out: dict[str, str] = {}
    for key, ref in bindings.items():
        role = str(key).strip().lower()
        if role not in known:
            role = aliases.get(role, role)
        if role not in known:
            raise ValueError(f"Unknown binding role {key!r} for template {tpl.name!r}; "
                             f"roles: {[r.name for r in tpl.roles]}"
                             + (f" (aliases: {dict(tpl.aliases)})" if aliases else ""))
        if role in out:
            raise ValueError(f"Role {role!r} of template {tpl.name!r} was given twice "
                             f"(also as an alias); pass it once.")
        if isinstance(ref, (list, tuple)):
            if len(ref) != 1:
                raise ValueError(f"role {role!r} takes exactly one field, got {list(ref)}")
            ref = ref[0]
        out[role] = ref
    missing = [r.name for r in tpl.roles if r.required and r.name not in out]
    if missing:
        raise ValueError(f"Template {tpl.name!r} needs binding(s) {missing}: "
                         + "; ".join(f"{r.name} = {r.description}" for r in tpl.roles
                                     if r.name in missing))
    return out


def resolve_bindings(project, tpl: Template, bindings: dict) -> dict[str, DField]:
    """Resolve + type-check the role bindings of a template."""
    refs = _normalise_bindings(tpl, bindings)
    fields: dict[str, DField] = {}
    for role in tpl.roles:
        if role.name not in refs:
            continue
        f = resolve_field(project, refs[role.name])
        if role.kind == "dimension" and (f.is_measure or f.agg):
            raise ValueError(f"role {role.name!r} needs a column (a dimension), but "
                             f"{f.ref!r} is a {'measure' if f.is_measure else 'aggregation'}")
        if role.kind == "measure" and not f.is_numeric:
            raise ValueError(f"role {role.name!r} needs a measure or a numeric column, but "
                             f"{f.ref!r} is a {f.data_type} column")
        fields[role.name] = f
    seen: dict[str, str] = {}
    for f in fields.values():
        if f.name in seen and seen[f.name] != f.ref:
            raise ValueError(f"{f.ref!r} and {seen[f.name]!r} both appear as {f.name!r} in "
                             f"Deneb's dataset (its field names are the display names); "
                             f"bind fields with distinct names.")
        seen[f.name] = f.ref
    return fields


def build_spec(template: str, fields: dict[str, DField], options: dict | None = None,
               theme: dict | None = None) -> tuple[dict, dict]:
    """(vega-lite spec, vega config) for a template + resolved fields."""
    tpl = _template(template)
    o = _validate_options(tpl, options)
    theme = theme or {"palette": ["#118DFF", "#12239E", "#E66C37", "#6B007B",
                                  "#E044A7", "#744EC2", "#D9B300", "#D64550"],
                      "foreground": "#252423", "muted": "#605E5C"}
    palette = o.get("palette") or theme["palette"]
    if "color" in tpl.options:
        o["color"] = o.get("color") or palette[0]
    spec = tpl.build(fields, o)
    config = default_config(palette, theme["foreground"], theme["muted"])
    return spec, config


def _template(name: str) -> Template:
    key = str(name).strip().lower().replace("-", "_").replace(" ", "_")
    if key not in TEMPLATES:
        raise ValueError(f"Unknown Deneb template {name!r}; templates: {sorted(TEMPLATES)}")
    return TEMPLATES[key]


# --- visual.json ---------------------------------------------------------------------------------------

def _lit(value) -> dict:
    return _expr_literal(value)


def vega_properties(spec_json: str, config_json: str, provider: str = "vegaLite",
                    **extra) -> dict:
    """The ``properties`` of objects.vega: every value a Literal expression."""
    props = {
        "provider": _lit(provider),
        "version": _lit(PROVIDER_VERSIONS[provider]),
        "jsonSpec": _lit(spec_json),
        "jsonConfig": _lit(config_json),
        "renderMode": _lit("svg"),
        "enableTooltips": _lit(True),
    }
    for key, value in extra.items():
        allowed = VEGA_PROPERTIES.get(key, ())
        if key not in VEGA_PROPERTIES or (allowed and value not in allowed):
            raise ValueError(f"Unsupported Deneb property {key}={value!r}")
        props[key] = _lit(value)
    return props


def dump_json(doc) -> str:
    """Stable, editor-friendly JSON text for jsonSpec / jsonConfig."""
    return json.dumps(doc, indent=2, ensure_ascii=False)


def projections_for(project, fields: dict[str, DField]) -> list[dict]:
    """The ``dataset`` role projections for the bound fields (deduplicated)."""
    from core.pbir import _field_ref

    seen: set[str] = set()
    out = []
    for f in fields.values():
        if f.ref in seen:
            continue
        seen.add(f.ref)
        proj = _field_ref(f.ref, project._is_measure)
        if f.agg:
            proj["displayName"] = f.prop        # keep the dataset field name = column name
        out.append(proj)
    return out


def build_visual_json(project, visual_id: str, template: str, fields: dict[str, DField],
                      options: dict | None, position: dict, title: str | None) -> dict:
    """Complete visual.json for a template visual (schema-valid)."""
    from core.pbir import _VC_SCHEMA

    spec, config = build_spec(template, fields, options, project_theme(project))
    visual: dict = {
        "visualType": DENEB_VISUAL_GUID,
        "query": {"queryState": {DATA_ROLE: {"projections": projections_for(project, fields)}}},
        "objects": {SPEC_OBJECT: [{"properties": vega_properties(
            dump_json(spec), dump_json(config))}]},
        "drillFilterOtherVisuals": True,
    }
    if title:
        visual["visualContainerObjects"] = {"title": [{"properties": {
            "show": _lit(True), "text": _lit(title)}}]}
    pos = {"x": 0, "y": 0, "z": 0, "width": 480, "height": 320, "tabOrder": 0}
    pos.update(position)
    return {"$schema": _VC_SCHEMA, "name": visual_id, "position": pos, "visual": visual}


def free_position(project, page_id: str, width: float = 480.0,
                  height: float = 320.0) -> tuple[dict, str | None]:
    """First free rectangle (reading order) on the page for a new visual.

    Returns ({x, y, width, height, z, tabOrder}, warning). Shapes / images are
    treated as backplates and ignored; falls back to below the content.
    """
    pages = {p.id: p for p in project.list_pages()}
    page = pages[page_id]
    pw, ph = float(page.width or 1280), float(page.height or 720)
    width, height = min(width, pw - 32), min(height, ph - 32)
    visuals = project.list_visuals(page_id)
    boxes = [(v.position.x, v.position.y, v.position.x + v.position.width,
              v.position.y + v.position.height)
             for v in visuals
             if v.visual_type not in _BACKPLATE_TYPES and "visualGroup" not in (v.raw or {})]
    z = int(max((v.position.z for v in visuals), default=-1)) + 1

    def free(x, y):
        return all(x + width <= bx or bx2 <= x or y + height <= by or by2 <= y
                   for bx, by, bx2, by2 in boxes)

    step = 8
    y = 16
    while y + height <= ph - 8:
        x = 16
        while x + width <= pw - 8:
            if free(x, y):
                return {"x": x, "y": y, "width": width, "height": height,
                        "z": z, "tabOrder": z}, None
            x += step
        y += step
    bottom = max((b[3] for b in boxes), default=0)
    top = bottom + 16
    room = ph - top - 8
    if room >= 120:                       # use the space left below the content
        fit = min(height, room)
        return ({"x": 16, "y": top, "width": width, "height": fit, "z": z, "tabOrder": z},
                f"No free {int(width)}x{int(height)} area on the page; the visual was "
                f"placed below the existing content at {int(width)}x{int(fit)}. "
                f"Pass position to place it elsewhere.")
    return ({"x": 16, "y": top, "width": width, "height": height, "z": z, "tabOrder": z},
            "No free space on the page; the visual was placed below the existing content "
            "and extends past the page. Pass position to place it.")


def is_registered(report: dict) -> bool:
    """Whether report.json already makes the Deneb visual available."""
    if DENEB_VISUAL_GUID in (report.get("publicCustomVisuals") or []):
        return True
    for org in report.get("organizationCustomVisuals") or []:
        if isinstance(org, dict) and is_deneb_type(org.get("name")):
            return True
    for pkg in report.get("resourcePackages") or []:
        if isinstance(pkg, dict) and pkg.get("type") in (
                "CustomVisual", "OrganizationalStoreCustomVisual") \
                and is_deneb_type(pkg.get("name")):
            return True
    return False


def ensure_registered(project) -> bool:
    """List Deneb in report.json publicCustomVisuals unless already available.

    Returns True when report.json was changed.
    """
    path = project._require_report() / "definition" / "report.json"
    data = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    if is_registered(data):
        return False
    data.setdefault("publicCustomVisuals", []).append(DENEB_VISUAL_GUID)
    project._write_json(path, data)
    return True


def add_deneb_visual(project, page_id: str, template: str, bindings: dict,
                     position: dict | None = None, title: str | None = None,
                     options: dict | None = None) -> dict:
    """Bind fields to a template and add the Deneb visual to a page."""
    if page_id not in {p.id for p in project.list_pages()}:
        raise ValueError(f"Page {page_id!r} not found; have "
                         f"{sorted(p.id for p in project.list_pages())}")
    tpl = _template(template)
    fields = resolve_bindings(project, tpl, bindings)
    _validate_options(tpl, options)          # fail before anything is written
    if position is not None and not isinstance(position, dict):
        raise ValueError("position must be an object like {x, y, width, height}")
    user = dict(position or {})
    unknown = sorted(set(user) - {"x", "y", "z", "width", "height", "tabOrder"})
    if unknown:
        raise ValueError(f"Unknown position key(s) {unknown}; use x, y, width, height "
                         f"(and optionally z, tabOrder)")
    for k, v in user.items():
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            raise ValueError(f"position.{k} must be a number")
    if title is not None and not isinstance(title, str):
        raise ValueError("title must be a string")
    auto, warning = free_position(project, page_id, user.get("width", 480.0),
                                  user.get("height", 320.0))
    pos = {**auto, **user}
    if {"x", "y"} & set(user):
        warning = None
    obj = build_visual_json(project, "deneb", tpl.name, fields, options, pos, title)
    vid = project.add_visual_raw(page_id, obj, base="deneb")
    registered = ensure_registered(project)
    result = {
        "ok": True, "page_id": page_id, "visual_id": vid, "template": tpl.name,
        "position": {k: pos[k] for k in ("x", "y", "width", "height")},
        "dataset_fields": {role: f.name for role, f in fields.items()},
        "custom_visual_registered": registered,
        "note": NEEDS_DENEB,
    }
    if warning:
        result["warnings"] = [warning]
    return result


# --- reading + replacing a spec ---------------------------------------------------------------------------

def _decode(node):
    """Literal string value of an objects property, or None."""
    try:
        v = node["expr"]["Literal"]["Value"]
    except (KeyError, TypeError):
        return None
    if isinstance(v, str) and len(v) >= 2 and v[0] == "'" and v[-1] == "'":
        return v[1:-1].replace("''", "'")
    return v


def read_deneb_visual(visual_json: dict) -> dict:
    """{provider, version, spec, config, render_mode} of a Deneb visual.json.

    spec/config are parsed JSON when valid, else the raw text.
    """
    visual = visual_json.get("visual") or {}
    if not is_deneb_type(visual.get("visualType")):
        raise ValueError(f"Not a Deneb visual (visualType {visual.get('visualType')!r})")
    entries = (visual.get("objects") or {}).get(SPEC_OBJECT) or []
    props = entries[0].get("properties", {}) if entries else {}

    def parsed(key):
        raw = _decode(props.get(key))
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return raw

    return {"provider": _decode(props.get("provider")) or "vegaLite",
            "version": _decode(props.get("version")),
            "render_mode": _decode(props.get("renderMode")),
            "spec": parsed("jsonSpec"), "config": parsed("jsonConfig")}


def detect_provider(spec: dict, default: str = "vegaLite") -> str:
    """'vega' or 'vegaLite' from $schema, else from characteristic keys."""
    schema = str(spec.get("$schema", "")).lower()
    if "vega-lite" in schema:
        return "vegaLite"
    if "/vega/" in schema:
        return "vega"
    if {"mark", "encoding", "layer", "hconcat", "vconcat", "concat", "facet", "repeat"} & set(spec):
        return "vegaLite"
    if {"marks", "scales", "signals", "axes"} & set(spec):
        return "vega"
    return default


def _as_json_object(value, what: str):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as e:
            raise ValueError(f"{what} is not valid JSON: {e}") from e
    if not isinstance(value, dict):
        raise ValueError(f"{what} must be a JSON object")
    return value


def set_deneb_spec(project, page_id: str, visual_id: str, spec, config=None) -> dict:
    """Replace the Vega / Vega-Lite spec (and optionally config) of a Deneb visual."""
    spec = _as_json_object(spec, "spec")
    config = None if config is None else _as_json_object(config, "config")
    vfile = project._visual_file(page_id, visual_id)
    data = json.loads(vfile.read_text(encoding="utf-8-sig"))
    visual = data.get("visual") or {}
    if not is_deneb_type(visual.get("visualType")):
        raise ValueError(f"Visual {visual_id!r} is a {visual.get('visualType')!r}, not a "
                         f"Deneb visual; add one with pbi_add_deneb_visual first.")
    entries = visual.setdefault("objects", {}).setdefault(SPEC_OBJECT, [{"properties": {}}])
    if not entries:
        entries.append({"properties": {}})
    props = entries[0].setdefault("properties", {})
    current = read_deneb_visual(data)
    provider = detect_provider(spec, current["provider"])
    props["provider"] = _lit(provider)
    props["version"] = _lit(PROVIDER_VERSIONS[provider])
    props["jsonSpec"] = _lit(dump_json(spec))
    if config is not None or "jsonConfig" not in props:
        props["jsonConfig"] = _lit(dump_json(config if config is not None else {}))
    props.setdefault("renderMode", _lit("svg"))
    props.setdefault("enableTooltips", _lit(True))
    project._write_json(vfile, data)
    warnings = []
    compact = json.dumps(spec, separators=(",", ":"))
    if '"name":"dataset"' not in compact:
        warnings.append(
            "The spec never reads the data named 'dataset' (Vega-Lite: "
            "\"data\": {\"name\": \"dataset\"}; Vega: a data entry named 'dataset'); "
            "Deneb injects the bound fields there, so the visual will show no data.")
    result = {"ok": True, "page_id": page_id, "visual_id": visual_id, "provider": provider,
              "version": PROVIDER_VERSIONS[provider], "spec_bytes": len(dump_json(spec)),
              "config_updated": config is not None, "note": NEEDS_DENEB}
    if warnings:
        result["warnings"] = warnings
    return result
