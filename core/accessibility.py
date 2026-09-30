"""Accessibility helpers: alt text, tab order and a WCAG-flavoured audit.

Where PBIR keeps the accessibility data (confirmed against the vendored
visualContainer schema and real exports):

  * alt text   -> ``visual.visualContainerObjects.general[0].properties.altText``
                  (``VisualContainerGeneralFormattingObjects.altText``), a
                  string literal expression like every other object property;
  * tab order  -> ``position.tabOrder`` (a number; lower tabs first).

The audit (``analyze_page``) reports, per visual: missing alt text, tab-order
duplicates / gaps, text below 9 pt, and text/background contrast below the
WCAG 2.1 AA ratio of 4.5:1. Colours are read from the formatting objects; the
effective background falls back container -> page -> theme -> Power BI
defaults (#FFFFFF page, #252423 text).

Everything here is pure (dicts in, dicts out). Persisting is done by
report_server/tools_accessibility.py through ``PbipProject._write_json``.
"""

from __future__ import annotations

import re
from typing import Iterable

from core.formatting import build_objects_patch, merge_objects

#: Power BI's own defaults, used when a theme does not say otherwise.
DEFAULT_TEXT = "#252423"
DEFAULT_BACKGROUND = "#FFFFFF"
MIN_FONT_PT = 9.0
MIN_CONTRAST = 4.5

#: purely visual furniture: never needs alt text, tabbed last.
DECORATIVE_TYPES = {"shape", "basicShape"}

_ROW_TOLERANCE = 10  # px: visuals whose tops differ less than this share a row

_TYPE_NAMES = {
    "card": "Card", "cardVisual": "Card", "multiRowCard": "Multi-row card",
    "tableEx": "Table", "pivotTable": "Matrix", "slicer": "Slicer",
    "advancedSlicerVisual": "Slicer", "barChart": "Stacked bar chart",
    "columnChart": "Stacked column chart",
    "clusteredBarChart": "Clustered bar chart",
    "clusteredColumnChart": "Clustered column chart",
    "lineChart": "Line chart", "areaChart": "Area chart",
    "stackedAreaChart": "Stacked area chart",
    "waterfallChart": "Waterfall chart", "pieChart": "Pie chart",
    "donutChart": "Donut chart", "scatterChart": "Scatter chart",
    "gauge": "Gauge", "filledMap": "Filled map", "map": "Map",
    "lineClusteredColumnComboChart": "Line and clustered column chart",
    "lineStackedColumnComboChart": "Line and stacked column chart",
    "textbox": "Text box", "image": "Image", "actionButton": "Button",
    "treemap": "Treemap", "funnel": "Funnel chart", "kpi": "KPI",
}
_MEASURE_BUCKETS_FIRST = ("Y", "Y2", "Values", "Data", "X", "Size")
_CATEGORY_BUCKETS = ("Category", "Series", "Rows", "Columns", "Details")


# --- reading order ----------------------------------------------------------

def _pos(v) -> dict:
    return (v.raw or {}).get("position") or {}


def _is_group(v) -> bool:
    return "visualGroup" in (v.raw or {})


def is_decorative(v) -> bool:
    return v.visual_type in DECORATIVE_TYPES


def is_hidden(v) -> bool:
    return bool((v.raw or {}).get("isHidden"))


def reading_order(visuals: Iterable) -> list:
    """Visuals top-to-bottom, left-to-right (rows tolerate small y jitter)."""
    items = sorted(visuals, key=lambda v: (v.position.y, v.position.x, v.id))
    rows: list[list] = []
    for v in items:
        if rows and abs(v.position.y - rows[-1][0].position.y) <= _ROW_TOLERANCE:
            rows[-1].append(v)
        else:
            rows.append([v])
    out: list = []
    for row in rows:
        out.extend(sorted(row, key=lambda v: (v.position.x, v.position.y, v.id)))
    return out


def tab_order_sequence(visuals: Iterable) -> list:
    """Reading order with hidden and decorative visuals moved to the end."""
    vs = list(visuals)
    main = [v for v in vs if not is_hidden(v) and not is_decorative(v)]
    rest = [v for v in vs if is_hidden(v) or is_decorative(v)]
    return reading_order(main) + reading_order(rest)


# --- literals & colours -----------------------------------------------------

def _literal(node):
    """The raw Literal value of ``{"expr": {"Literal": {"Value": ...}}}``."""
    try:
        return node["expr"]["Literal"]["Value"]
    except (KeyError, TypeError):
        return None


def _unquote(value):
    if isinstance(value, str) and len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    return value


def literal_string(node) -> str | None:
    v = _unquote(_literal(node))
    return v if isinstance(v, str) else None


def literal_number(node) -> float | None:
    v = _literal(node)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    if isinstance(v, str):
        m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)[DLMdlm]?\s*", v)
        if m:
            return float(m.group(1))
    return None


def literal_bool(node) -> bool | None:
    v = _literal(node)
    if v in ("true", True):
        return True
    if v in ("false", False):
        return False
    return None


def parse_hex(color) -> tuple[int, int, int] | None:
    """``#RGB`` / ``#RRGGBB`` (or ``#RRGGBBAA``, alpha dropped) -> (r, g, b)."""
    if not isinstance(color, str):
        return None
    m = re.fullmatch(r"#([0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})", color.strip())
    if not m:
        return None
    h = m.group(1)
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def to_hex(rgb: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{max(0, min(255, round(c))):02X}" for c in rgb)


def color_of(prop) -> str | None:
    """A ``#hex`` colour from a colour property, or None when not a literal
    (theme colours, conditional formats, measures are not resolvable here)."""
    if not isinstance(prop, dict):
        return None
    try:
        node = prop["solid"]["color"]
    except (KeyError, TypeError):
        node = prop
    s = literal_string(node)
    return s if parse_hex(s) else None


def relative_luminance(rgb: tuple[float, float, float]) -> float:
    def chan(c: float) -> float:
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (chan(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(fg: str, bg: str) -> float:
    """WCAG 2.1 contrast ratio between two ``#hex`` colours (1.0 - 21.0)."""
    a, b = parse_hex(fg), parse_hex(bg)
    if a is None or b is None:
        raise ValueError(f"unparseable colour: {fg!r} / {bg!r}")
    la, lb = relative_luminance(a), relative_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def blend(top: str, alpha: float, under: str) -> str:
    """``top`` at opacity ``alpha`` (0-1) composited over ``under``."""
    t, u = parse_hex(top), parse_hex(under)
    return to_hex(tuple(alpha * a + (1 - alpha) * b for a, b in zip(t, u)))  # type: ignore[arg-type]


def best_text_color(bg: str) -> str:
    """Black or white, whichever reads better on ``bg``."""
    return "#000000" if contrast_ratio("#000000", bg) >= contrast_ratio("#FFFFFF", bg) \
        else "#FFFFFF"


# --- alt text ---------------------------------------------------------------

def get_alt_text(raw: dict) -> str | None:
    try:
        entry = raw["visual"]["visualContainerObjects"]["general"][0]
        text = literal_string(entry["properties"]["altText"])
    except (KeyError, IndexError, TypeError):
        return None
    return text if text and text.strip() else None


def set_alt_text_raw(raw: dict, text: str) -> None:
    """Set (or, with an empty string, clear) alt text on a visual.json dict."""
    visual = raw.setdefault("visual", {})
    vco = visual.setdefault("visualContainerObjects", {})
    if not text:
        for entry in vco.get("general", []) or []:
            (entry.get("properties") or {}).pop("altText", None)
        vco["general"] = [e for e in vco.get("general", []) or []
                          if e.get("properties")]
        if not vco["general"]:
            vco.pop("general")
        return
    visual["visualContainerObjects"] = merge_objects(
        vco, build_objects_patch({"general": {"altText": text}}))


def _friendly_type(visual_type: str | None) -> str:
    if not visual_type:
        return "Visual"
    if visual_type in _TYPE_NAMES:
        return _TYPE_NAMES[visual_type]
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", visual_type).lower()
    return spaced[:1].upper() + spaced[1:]


def _projection_name(proj: dict) -> tuple[str | None, bool]:
    """(display name, is_measure) of one queryState projection."""
    field = proj.get("field") or {}
    for kind in ("Measure", "Column", "Aggregation", "HierarchyLevel"):
        node = field.get(kind)
        if node is None:
            continue
        if kind == "Aggregation":
            inner = (node.get("Expression") or {}).get("Column") or {}
            return inner.get("Property"), True
        if kind == "HierarchyLevel":
            return node.get("Level"), False
        return node.get("Property"), kind == "Measure"
    ref = proj.get("queryRef")
    return (ref.split(".", 1)[-1] if ref else None), False


def bound_fields(raw: dict) -> tuple[list[str], list[str]]:
    """(measure names, category/column names) bound in a visual's queryState."""
    state = ((raw.get("visual") or {}).get("query") or {}).get("queryState") or {}
    measures: list[str] = []
    columns: list[str] = []
    for bucket, spec in state.items():
        for proj in (spec or {}).get("projections", []) or []:
            name, is_measure = _projection_name(proj)
            if not name:
                continue
            target = measures if is_measure else columns
            if name not in target:
                target.append(name)
    return measures, columns


def _join(names: list[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def describe_visual(v) -> str:
    """'<visual type> of <measures> by <category>', prefixed by the title."""
    raw = v.raw or {}
    kind = _friendly_type(v.visual_type)
    measures, columns = bound_fields(raw)
    if measures and columns:
        desc = f"{kind} of {_join(measures)} by {_join(columns)}"
    elif measures:
        desc = f"{kind} of {_join(measures)}"
    elif columns:
        desc = f"{kind} of {_join(columns)}"
    elif v.visual_type == "image":
        item = ""
        try:
            item = raw["visual"]["objects"]["general"][0]["properties"][
                "imageUrl"]["expr"]["ResourcePackageItem"]["ItemName"]
        except (KeyError, IndexError, TypeError):
            pass
        stem = re.sub(r"\.[A-Za-z0-9]+$", "", item).replace("_", " ").replace("-", " ")
        desc = f"Image {stem}".strip() if stem else "Image"
    elif v.visual_type == "actionButton":
        label = None
        try:
            label = literal_string(raw["visual"]["objects"]["text"][0][
                "properties"]["text"])
        except (KeyError, IndexError, TypeError):
            pass
        desc = f"Button {label}" if label else "Button"
    else:
        desc = kind
    title = v.title
    if title and title.strip().lower() not in desc.lower():
        return f"{title.strip()}: {desc}"
    return desc


def needs_alt_text(v) -> bool:
    """Content visuals do; groups, decorative shapes and text boxes (read
    aloud from their own content) do not."""
    return not (_is_group(v) or is_decorative(v) or is_hidden(v)
                or v.visual_type == "textbox")


# --- text size & colours ----------------------------------------------------

#: object property names that carry a text colour
_TEXT_COLOR_KEYS = {"fontColor", "labelColor", "color"}
_TEXT_COLOR_OBJECTS_FOR_PLAIN_COLOR = {"labels", "dataLabels", "categoryLabels",
                                       "subTitle", "values", "columnHeaders",
                                       "rowHeaders", "grid"}


def _objects(raw: dict, key: str) -> dict:
    return ((raw.get("visual") or {}).get(key)) or {}


def font_sizes(raw: dict) -> list[tuple[str, float]]:
    """(where, size in pt) for every explicit font size on a visual."""
    found: list[tuple[str, float]] = []
    for group in ("objects", "visualContainerObjects"):
        for obj, entries in _objects(raw, group).items():
            for entry in entries or []:
                for key, val in (entry.get("properties") or {}).items():
                    if key.lower().endswith("fontsize"):
                        n = literal_number(val)
                        if n is not None:
                            found.append((f"{obj}.{key}", n))
    # textbox runs: textStyle.fontSize == "8pt"
    for entry in _objects(raw, "objects").get("general", []) or []:
        for para in (entry.get("properties") or {}).get("paragraphs", []) or []:
            for run in para.get("textRuns", []) or []:
                size = (run.get("textStyle") or {}).get("fontSize")
                if isinstance(size, str):
                    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(pt|px)?\s*", size)
                    if m:
                        n = float(m.group(1))
                        if m.group(2) == "px":
                            n = n * 0.75
                        found.append(("textbox.fontSize", n))
    return found


def _shown(props: dict) -> bool:
    return literal_bool(props.get("show")) is not False


def text_colors(raw: dict) -> list[tuple[str, str, str | None]]:
    """(where, colour, own background or None) for each explicit text colour.

    A title may carry its own background; a button's text sits on its fill.
    """
    found: list[tuple[str, str, str | None]] = []
    vtype = (raw.get("visual") or {}).get("visualType")
    for group in ("objects", "visualContainerObjects"):
        for obj, entries in _objects(raw, group).items():
            for entry in entries or []:
                props = entry.get("properties") or {}
                if not _shown(props):
                    continue
                own_bg = color_of(props.get("background")) if obj == "title" else None
                if vtype == "actionButton" and obj == "text":
                    fill = (_objects(raw, "objects").get("fill") or [{}])[0]
                    own_bg = color_of((fill.get("properties") or {}).get("fillColor"))
                for key, val in props.items():
                    is_text = key in ("fontColor", "labelColor") or (
                        key == "color" and obj in _TEXT_COLOR_OBJECTS_FOR_PLAIN_COLOR)
                    if not is_text:
                        continue
                    c = color_of(val)
                    if c:
                        found.append((f"{obj}.{key}", c, own_bg))
    for entry in _objects(raw, "objects").get("general", []) or []:
        for para in (entry.get("properties") or {}).get("paragraphs", []) or []:
            for run in para.get("textRuns", []) or []:
                c = (run.get("textStyle") or {}).get("color")
                if parse_hex(c):
                    found.append(("textbox.color", c, None))
    return found


def container_background(raw: dict) -> tuple[str, float] | None:
    """(colour, opacity 0-1) of the visual container's own background."""
    entries = _objects(raw, "visualContainerObjects").get("background") or []
    if not entries:
        return None
    props = entries[0].get("properties") or {}
    if literal_bool(props.get("show")) is False:
        return None
    color = color_of(props.get("color"))
    if not color:
        return None
    transparency = literal_number(props.get("transparency"))
    opacity = 1.0 if transparency is None else max(0.0, min(1.0, 1 - transparency / 100))
    return (color, opacity) if opacity > 0 else None


# --- theme / page context ---------------------------------------------------

def page_background(page_json: dict | None, under: str) -> str | None:
    """Explicit page canvas colour (blended over ``under``) or None."""
    if not page_json:
        return None
    entries = ((page_json.get("objects") or {}).get("background")) or []
    if not entries:
        return None
    props = entries[0].get("properties") or {}
    color = color_of(props.get("color"))
    if not color:
        return None
    transparency = literal_number(props.get("transparency"))
    opacity = 1.0 if transparency is None else max(0.0, min(1.0, 1 - transparency / 100))
    if opacity <= 0:
        return None
    return blend(color, opacity, under)


def theme_colors(project) -> dict:
    """{"foreground", "background", "source"} from the report's custom theme
    (report.json themeCollection), else Power BI's defaults."""
    import json

    out = {"foreground": DEFAULT_TEXT, "background": DEFAULT_BACKGROUND,
           "source": "default"}
    try:
        report_dir = project._require_report()
        data = json.loads((report_dir / "definition" / "report.json")
                          .read_text(encoding="utf-8-sig"))
        ct = (data.get("themeCollection") or {}).get("customTheme")
        if not ct:
            return out
        theme = json.loads((report_dir / "StaticResources" / "RegisteredResources"
                            / ct["name"]).read_text(encoding="utf-8-sig"))
    except (KeyError, ValueError, OSError, FileNotFoundError, TypeError):
        return out
    fg, bg = theme.get("foreground"), theme.get("background")
    if parse_hex(fg):
        out["foreground"] = fg
        out["source"] = "theme"
    if parse_hex(bg):
        out["background"] = bg
        out["source"] = "theme"
    return out


# --- the audit --------------------------------------------------------------

def _finding(severity, code, v, message, fix) -> dict:
    return {"severity": severity, "code": code, "visual_id": v.id if v else None,
            "message": message, "fix": fix}


def analyze_page(visuals: list, *, page_json: dict | None = None,
                 theme: dict | None = None) -> list[dict]:
    """Accessibility findings for one page.

    With ``theme`` (from ``theme_colors``) the contrast check knows the page
    and theme colours. Without it (the context-free mode used by the design
    lint) contrast is only judged where a visual states its own opaque
    background, so an unknown dark page can never cause a false positive.
    """
    findings: list[dict] = []
    live = [v for v in visuals if not is_hidden(v)]

    # 1. alt text
    for v in live:
        if needs_alt_text(v) and not get_alt_text(v.raw or {}):
            findings.append(_finding(
                "warning", "missing_alt_text", v,
                f"{_friendly_type(v.visual_type)} {v.id} has no alt text",
                f"pbi_set_alt_text(page, '{v.id}', '...') or pbi_auto_alt_text(page)"))

    # 2. tab order
    orders = []
    for v in live:
        t = _pos(v).get("tabOrder")
        orders.append((v, t if isinstance(t, (int, float)) else None))
    by_value: dict[float, list] = {}
    for v, t in orders:
        if t is not None:
            by_value.setdefault(t, []).append(v)
    for t, vs in sorted(by_value.items()):
        if len(vs) > 1:
            ids = [x.id for x in vs]
            findings.append(_finding(
                "warning", "duplicate_tab_order", vs[0],
                f"visuals {ids} share tab order {t:g}, so keyboard order is ambiguous",
                "pbi_auto_tab_order(page) or pbi_set_tab_order(page, [...])"))
    missing = [v for v, t in orders if t is None]
    for v in missing:
        findings.append(_finding(
            "info", "missing_tab_order", v,
            f"{v.id} has no tab order", "pbi_auto_tab_order(page)"))
    values = sorted(by_value)
    if values and (values[0] != 0 or any(
            b - a != 1 for a, b in zip(values, values[1:]))) \
            and all(len(vs) == 1 for vs in by_value.values()):
        findings.append(_finding(
            "info", "tab_order_gaps", None,
            f"tab order values {[int(x) if float(x).is_integer() else x for x in values]}"
            " are not contiguous from 0",
            "pbi_auto_tab_order(page) renumbers them 0..n-1"))

    # 3. text size
    for v in live:
        if is_decorative(v):
            continue
        for where, size in font_sizes(v.raw or {}):
            if size < MIN_FONT_PT:
                findings.append(_finding(
                    "warning", "small_text", v,
                    f"{v.id} {where} is {size:g} pt (minimum {MIN_FONT_PT:g} pt)",
                    f"raise {where} to at least {MIN_FONT_PT:g} pt "
                    "with pbi_format_visual"))

    # 4. contrast
    theme = theme or None
    base_bg = theme["background"] if theme else None
    page_bg = page_background(page_json, base_bg or DEFAULT_BACKGROUND) \
        if theme else None
    for v in live:
        if is_decorative(v) or _is_group(v):
            continue
        raw = v.raw or {}
        cont = container_background(raw)
        parent = page_bg or base_bg          # None when unknown
        if cont is not None:
            color, opacity = cont
            if opacity >= 1:
                bg = color
            elif parent:
                bg = blend(color, opacity, parent)
            else:
                bg = None
        else:
            bg = parent
        explicit_bg = cont is not None or page_bg is not None
        colors = text_colors(raw)
        for where, fg, own_bg in colors:
            eff = own_bg or bg
            if not eff:
                continue
            ratio = contrast_ratio(fg, eff)
            if ratio < MIN_CONTRAST:
                findings.append(_finding(
                    "warning", "low_contrast", v,
                    f"{v.id} {where} {fg} on {eff} has contrast {ratio:.2f}:1 "
                    f"(WCAG AA needs {MIN_CONTRAST}:1)",
                    f"use {best_text_color(eff)} or another colour with >= "
                    f"{MIN_CONTRAST}:1 against {eff}"))
        if theme and not colors and explicit_bg and bg:
            fg = theme["foreground"]
            ratio = contrast_ratio(fg, bg)
            if ratio < MIN_CONTRAST:
                findings.append(_finding(
                    "warning", "low_contrast", v,
                    f"{v.id} default text {fg} on {bg} has contrast {ratio:.2f}:1 "
                    f"(WCAG AA needs {MIN_CONTRAST}:1)",
                    f"set an explicit text colour such as {best_text_color(bg)} "
                    "or change the background"))
    return findings


def as_lint_findings(findings: list[dict]) -> list[dict]:
    """Accessibility findings in the design-lint shape, always 'warning'."""
    out = []
    for f in findings:
        out.append({
            "severity": "warning",
            "code": f"a11y_{f['code']}",
            "message": f["message"],
            "visuals": [f["visual_id"]] if f.get("visual_id") else [],
            "fix": f["fix"],
        })
    return out
