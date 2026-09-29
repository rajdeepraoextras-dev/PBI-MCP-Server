"""Page rendering -- a wireframe-with-content picture of a report page.

``render_page`` draws one page of the selected report as SVG (pure string
building, no dependencies) or PNG (the *same* primitives rasterised with
Pillow, an optional extra: ``pip install pbi-mcp[render]``):

* the canvas is the page size from ``page.json`` filled with the page
  background / wallpaper (page objects, else the active theme);
* every visual is a rounded rectangle at its position, in z-order; hidden
  visuals are dashed and groups are outlined and named;
* inside each rectangle: the title (title formatting, else the auto title Power
  BI would derive from the bindings), the visual-type name, a compact bindings
  summary ("Category: Date.Year | Y: Sales.Net Revenue") and a schematic glyph
  for the visual family (bars, line, pie, big number, grid, list, ...).

Colors come from the report's active custom theme (``report.json``
``themeCollection``), falling back to the Power BI defaults, plus the report
accent. The layout is deterministic, so re-rendering an unchanged page yields
byte-identical output.

The drawing code only emits scene primitives (:class:`Rect`, :class:`Line`,
:class:`Poly`, :class:`Circle`, :class:`Text`); :func:`scene_to_svg` and
:func:`scene_to_png` are the two back-ends.
"""

from __future__ import annotations

import io
import json
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

#: Inline the SVG text in the tool result only when it is smaller than this.
INLINE_SVG_LIMIT = 200_000
MAX_SCALE = 8.0
FONT_STACK = "Segoe UI, Helvetica, Arial, sans-serif"

#: Power BI's default palette (the built-in theme) -- used when the report has
#: no custom theme on disk.
DEFAULT_THEME: dict = {
    "name": "Power BI default",
    "dataColors": ["#118DFF", "#12239E", "#E66C37", "#6B007B",
                   "#E044A7", "#744EC2", "#D9B300", "#D64550"],
    "background": "#FFFFFF",
    "foreground": "#252423",
    "foregroundNeutralSecondary": "#605E5C",
    "tableAccent": "#118DFF",
}

_HEX = re.compile(r"^#(?:[0-9A-Fa-f]{3}|[0-9A-Fa-f]{6}|[0-9A-Fa-f]{8})$")


# --- optional dependency ------------------------------------------------------

def require_pillow():
    """Import Pillow lazily; a clear install hint when it is missing."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as e:
        raise ImportError(
            "This feature needs Pillow: pip install pbi-mcp[render]") from e
    return Image, ImageDraw, ImageFont


# --- colors -------------------------------------------------------------------

def parse_color(value: str, default: str = "#000000") -> tuple[int, int, int, float]:
    """'#RGB' / '#RRGGBB' / '#RRGGBBAA' -> (r, g, b, alpha 0..1)."""
    if not isinstance(value, str) or not _HEX.match(value):
        value = default
    h = value.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    a = int(h[6:8], 16) / 255 if len(h) == 8 else 1.0
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), a


def to_hex(r: float, g: float, b: float) -> str:
    return "#" + "".join(f"{max(0, min(255, round(c))):02X}" for c in (r, g, b))


def norm_hex(value: str | None, default: str = "#000000") -> str:
    r, g, b, _ = parse_color(value or default, default)
    return to_hex(r, g, b)


def blend(c1: str, c2: str, t: float) -> str:
    """c1 moved `t` (0..1) of the way to c2."""
    a, b = parse_color(c1), parse_color(c2)
    return to_hex(*(a[i] + (b[i] - a[i]) * t for i in range(3)))


def luminance(c: str) -> float:
    r, g, b, _ = parse_color(c)
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255


# --- scene primitives -----------------------------------------------------------

@dataclass
class Rect:
    x: float
    y: float
    w: float
    h: float
    fill: str | None = None
    fill_opacity: float = 1.0
    stroke: str | None = None
    stroke_width: float = 1.0
    stroke_opacity: float = 1.0
    radius: float = 0.0
    dash: tuple[float, float] | None = None


@dataclass
class Line:
    x1: float
    y1: float
    x2: float
    y2: float
    stroke: str
    width: float = 1.0
    opacity: float = 1.0
    dash: tuple[float, float] | None = None


@dataclass
class Poly:
    points: list[tuple[float, float]]
    fill: str | None = None
    fill_opacity: float = 1.0
    stroke: str | None = None
    width: float = 1.0
    opacity: float = 1.0
    closed: bool = True


@dataclass
class Circle:
    cx: float
    cy: float
    r: float
    fill: str | None = None
    fill_opacity: float = 1.0
    stroke: str | None = None
    width: float = 1.0


@dataclass
class Text:
    x: float
    y: float          # baseline
    s: str
    size: float
    fill: str
    bold: bool = False
    italic: bool = False
    anchor: str = "start"      # start | middle | end
    opacity: float = 1.0


@dataclass
class Open:
    """Start of a logical group (one visual); ignored by the PNG back-end."""
    attrs: dict
    title: str | None = None


@dataclass
class Close:
    pass


@dataclass
class Scene:
    width: float
    height: float
    background: str
    title: str
    items: list = field(default_factory=list)
    visuals: list = field(default_factory=list)   # structured per-visual info


# --- text measuring (shared by both back-ends so truncation matches) -------------

_NARROW = set("iljtfIr.,:;'|!()[]{}/\\ -")
_WIDE = set("mwMW@%&#")


def text_width(s: str, size: float, bold: bool = False) -> float:
    """Approximate rendered width of `s` (Segoe UI / Arial-like metrics)."""
    total = 0.0
    for ch in s:
        if ch in _NARROW:
            total += 0.33
        elif ch in _WIDE:
            total += 0.86
        elif ch.isupper():
            total += 0.63
        elif ch.isdigit():
            total += 0.56
        else:
            total += 0.52
    return total * size * (1.06 if bold else 1.0)


def fit_text(s: str, size: float, max_w: float, bold: bool = False) -> str:
    """Truncate with '...' so the text fits in `max_w`."""
    s = _clean(s)
    if max_w <= 0:
        return ""
    if text_width(s, size, bold) <= max_w:
        return s
    while s and text_width(s + "...", size, bold) > max_w:
        s = s[:-1]
    return (s.rstrip() + "...") if s else ""


_BAD_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")


def _clean(s: str) -> str:
    """Drop characters XML 1.0 cannot carry; flatten newlines."""
    return _BAD_XML.sub("", str(s)).replace("\r", " ").replace("\n", " ")


# --- literal helpers ------------------------------------------------------------

def _decode_literal(v):
    if not isinstance(v, str):
        return v
    s = v.strip()
    if len(s) >= 2 and s[0] == "'" and s[-1] == "'":
        return s[1:-1].replace("''", "'")
    if s == "true":
        return True
    if s == "false":
        return False
    if s == "null":
        return None
    m = re.fullmatch(r"(-?\d+(?:\.\d+)?)[LDMldm]?", s)
    if m:
        f = float(m.group(1))
        return int(f) if f.is_integer() and "." not in m.group(1) else f
    return s


def _literal(node):
    """Value of a PBIR ``{"expr": {"Literal": {"Value": ...}}}`` node, or None."""
    try:
        return _decode_literal(node["expr"]["Literal"]["Value"])
    except (KeyError, TypeError):
        return None


def _color(node) -> str | None:
    """A '#hex' from ``{"solid": {"color": <literal>}}``, a literal, or a str."""
    if isinstance(node, str):
        return node if _HEX.match(node) else None
    if isinstance(node, dict):
        if "solid" in node and isinstance(node["solid"], dict):
            return _color(node["solid"].get("color"))
        v = _literal(node)
        if isinstance(v, str) and _HEX.match(v):
            return v
    return None


def _props(obj_list) -> dict:
    """First entry's ``properties`` of a PBIR object list (else {})."""
    if isinstance(obj_list, list) and obj_list and isinstance(obj_list[0], dict):
        p = obj_list[0].get("properties")
        return p if isinstance(p, dict) else {}
    return {}


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# --- theme ----------------------------------------------------------------------

def load_theme(project) -> dict:
    """The report's active custom theme merged over the Power BI defaults.

    Returns the theme dict plus ``source`` ("custom" | "default") and
    ``accent`` (the report accent: first data color, else tableAccent).
    """
    theme = dict(DEFAULT_THEME)
    theme["source"] = "default"
    custom = None
    try:
        report_dir = project._require_report()
        report = _read_json(report_dir / "definition" / "report.json") or {}
        ref = (report.get("themeCollection") or {}).get("customTheme")
        if isinstance(ref, dict) and ref.get("name"):
            custom = _read_json(report_dir / "StaticResources"
                                / "RegisteredResources" / ref["name"])
    except (OSError, FileNotFoundError):
        custom = None
    if custom:
        for key in ("name", "background", "secondaryBackground", "foreground",
                    "foregroundNeutralSecondary", "tableAccent"):
            if isinstance(custom.get(key), str):
                theme[key] = custom[key]
        colors = [c for c in (custom.get("dataColors") or [])
                  if isinstance(c, str) and _HEX.match(c)]
        if colors:
            theme["dataColors"] = colors
        theme["visualStyles"] = custom.get("visualStyles") or {}
        theme["source"] = "custom"
    for key in ("background", "foreground", "foregroundNeutralSecondary",
                "tableAccent"):
        if not _HEX.match(theme.get(key) or ""):
            theme[key] = DEFAULT_THEME[key]
    accent = None
    try:
        accent = project.report_accent()
    except (OSError, FileNotFoundError, KeyError, ValueError):
        accent = None
    theme["accent"] = norm_hex(accent if accent and _HEX.match(accent)
                               else (theme["dataColors"][0]
                                     if theme["source"] == "custom"
                                     else theme["tableAccent"]))
    return theme


# --- friendly type names + families ------------------------------------------------

#: visualType -> (friendly name, glyph family)
TYPE_INFO: dict[str, tuple[str, str]] = {
    "clusteredBarChart": ("Clustered bar chart", "bar"),
    "barChart": ("Stacked bar chart", "bar-stacked"),
    "hundredPercentStackedBarChart": ("100% stacked bar chart", "bar-stacked"),
    "clusteredColumnChart": ("Clustered column chart", "column"),
    "columnChart": ("Stacked column chart", "column-stacked"),
    "hundredPercentStackedColumnChart": ("100% stacked column chart", "column-stacked"),
    "lineChart": ("Line chart", "line"),
    "areaChart": ("Area chart", "area"),
    "stackedAreaChart": ("Stacked area chart", "area"),
    "hundredPercentStackedAreaChart": ("100% stacked area chart", "area"),
    "lineClusteredColumnComboChart": ("Line and clustered column chart", "combo"),
    "lineStackedColumnComboChart": ("Line and stacked column chart", "combo"),
    "ribbonChart": ("Ribbon chart", "ribbon"),
    "waterfallChart": ("Waterfall chart", "waterfall"),
    "funnel": ("Funnel", "funnel"),
    "pieChart": ("Pie chart", "pie"),
    "donutChart": ("Donut chart", "donut"),
    "treemap": ("Treemap", "treemap"),
    "scatterChart": ("Scatter chart", "scatter"),
    "gauge": ("Gauge", "gauge"),
    "card": ("Card", "card"),
    "cardVisual": ("Card (new)", "card"),
    "multiRowCard": ("Multi-row card", "card"),
    "kpi": ("KPI", "kpi"),
    "tableEx": ("Table", "table"),
    "table": ("Table", "table"),
    "pivotTable": ("Matrix", "matrix"),
    "slicer": ("Slicer", "slicer"),
    "advancedSlicerVisual": ("Tile slicer", "slicer"),
    "listSlicer": ("List slicer", "slicer"),
    "textSlicer": ("Text slicer", "slicer"),
    "textbox": ("Text box", "text"),
    "image": ("Image", "image"),
    "shape": ("Shape", "shape"),
    "basicShape": ("Shape", "shape"),
    "actionButton": ("Button", "button"),
    "pageNavigator": ("Page navigator", "button"),
    "bookmarkNavigator": ("Bookmark navigator", "button"),
    "map": ("Map", "map"),
    "filledMap": ("Filled map", "map"),
    "shapeMap": ("Shape map", "map"),
    "azureMap": ("Azure map", "map"),
    "decompositionTreeVisual": ("Decomposition tree", "tree"),
    "keyDriversVisual": ("Key influencers", "tree"),
    "qnaVisual": ("Q&A", "text"),
    "scriptVisual": ("R script visual", "code"),
    "pythonVisual": ("Python visual", "code"),
}

#: Vega-Lite mark -> glyph family (for Deneb visuals).
_MARK_FAMILY = {"bar": "column", "line": "line", "trail": "line", "area": "area",
                "point": "scatter", "circle": "scatter", "square": "scatter",
                "arc": "pie", "rect": "table", "boxplot": "column",
                "errorband": "area", "errorbar": "line", "rule": "line",
                "tick": "line", "text": "text", "geoshape": "map"}


def is_deneb(visual_type: str | None) -> bool:
    return bool(visual_type) and visual_type.lower().startswith("deneb")


def type_label(visual_type: str | None) -> str:
    """Human-readable name of a visualType."""
    if not visual_type:
        return "Visual"
    if visual_type in TYPE_INFO:
        return TYPE_INFO[visual_type][0]
    if is_deneb(visual_type):
        return "Deneb (Vega-Lite)"
    stem = re.sub(r"[0-9A-Fa-f]{16,}$", "", visual_type)
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", stem).replace("_", " ").strip()
    words = (words[:1].upper() + words[1:].lower()) if words else visual_type
    return words + (" (custom)" if stem != visual_type else "")


def family_of(visual_type: str | None) -> str:
    if visual_type in TYPE_INFO:
        return TYPE_INFO[visual_type][1]
    if is_deneb(visual_type):
        return "deneb"
    return "generic"


# --- page model ---------------------------------------------------------------------

@dataclass
class VisualView:
    id: str
    vtype: str | None
    raw: dict
    x: float
    y: float
    w: float
    h: float
    z: float
    order: int
    hidden: bool = False
    is_group: bool = False
    group_name: str | None = None
    parent: str | None = None
    bindings: list = field(default_factory=list)     # [(bucket, [refs])]


def _num(v, default=0.0) -> float:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else default


def _contained(r, outer, tol: float = 2.0) -> bool:
    x, y, w, h = r
    ox, oy, ow, oh = outer
    return (x >= ox - tol and y >= oy - tol
            and x + w <= ox + ow + tol and y + h <= oy + oh + tol)


def _resolve_rects(views: list[VisualView]) -> None:
    """Make every rect absolute. Members of a group are placed inside the
    group's box: absolute coordinates are kept when they already lie inside
    it, else they are treated as offsets from the group's corner."""
    by_id = {v.id: v for v in views}
    done: set[str] = set()

    def resolve(v: VisualView, seen: tuple = ()) -> None:
        if v.id in done or v.id in seen:
            return
        parent = by_id.get(v.parent) if v.parent else None
        if parent is not None and parent.id != v.id:
            resolve(parent, seen + (v.id,))
            box = (parent.x, parent.y, parent.w, parent.h)
            if not _contained((v.x, v.y, v.w, v.h), box):
                shifted = (v.x + parent.x, v.y + parent.y, v.w, v.h)
                if _contained(shifted, box):
                    v.x, v.y = shifted[0], shifted[1]
        done.add(v.id)

    for v in views:
        resolve(v)


def collect_page(project, page_id: str) -> dict:
    """Read a page into {id, name, width, height, objects, views[]}."""
    pages = {p.id: p for p in project.list_pages()}
    if page_id not in pages:
        raise ValueError(f"Page {page_id!r} not found; have {sorted(pages)}")
    page = pages[page_id]
    page_json = _read_json(project._require_report() / "definition" / "pages"
                           / page_id / "page.json") or {}
    from core.pbir import visual_bindings

    views: list[VisualView] = []
    for i, v in enumerate(project.list_visuals(page_id)):
        raw = v.raw or {}
        pos = raw.get("position") or {}
        group = raw.get("visualGroup")
        views.append(VisualView(
            id=v.id, vtype=v.visual_type, raw=raw,
            x=_num(pos.get("x")), y=_num(pos.get("y")),
            w=max(0.0, _num(pos.get("width"))), h=max(0.0, _num(pos.get("height"))),
            z=_num(pos.get("z")), order=i,
            hidden=raw.get("isHidden") is True,
            is_group=isinstance(group, dict),
            group_name=(group or {}).get("displayName") if isinstance(group, dict) else None,
            parent=raw.get("parentGroupName") or None,
            bindings=list(visual_bindings(v).items()),
        ))
    _resolve_rects(views)
    return {
        "id": page_id,
        "name": page.name or page_id,
        "width": float(page.width or page_json.get("width") or 1280),
        "height": float(page.height or page_json.get("height") or 720),
        "objects": page_json.get("objects") or {},
        "views": views,
    }


# --- titles -------------------------------------------------------------------------

def _short(ref: str) -> str:
    """'Sales.Net Revenue' -> 'Net Revenue'; 'Sum(Sales.Amount)' -> 'Sum of Amount'."""
    m = re.fullmatch(r"(\w+)\(([^)]*)\)", ref.strip())
    if m:
        return f"{m.group(1)} of {_short(m.group(2))}"
    return ref.partition(".")[2] or ref


def _join(names: list[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def auto_title(vtype: str | None, bindings: list) -> str | None:
    """The title Power BI derives from the bindings when none is set."""
    b = {k: [_short(r) for r in v] for k, v in bindings}
    fam = family_of(vtype)
    if fam in ("card", "kpi", "gauge"):
        names = b.get("Values") or b.get("Data") or b.get("Y") or []
        return _join(names) or None
    if fam == "slicer":
        return (b.get("Values") or [None])[0]
    if fam in ("table", "matrix", "text", "image", "shape", "button", "code"):
        return None
    if fam == "scatter":
        y, x = b.get("Y") or [], b.get("X") or []
        if y and x:
            return f"{_join(y)} by {_join(x)}"
    y = b.get("Y") or b.get("Values") or []
    dims = (b.get("Category") or []) + (b.get("Series") or [])
    if y and dims:
        return f"{_join(y)} by {_join(dims)}"
    every = [n for names in b.values() for n in names]
    return _join(every) or None


def container_title(raw: dict) -> tuple[str | None, bool]:
    """(static title text, shown?) from visualContainerObjects.title."""
    vis = raw.get("visual") or {}
    p = _props((vis.get("visualContainerObjects") or {}).get("title"))
    text = _literal(p.get("text"))
    shown = _literal(p.get("show"))
    return (text if isinstance(text, str) and text.strip() else None,
            shown is not False)


def textbox_paragraphs(raw: dict) -> list[dict]:
    """A text box's non-empty paragraphs: {text, size (px), color, bold, align}."""
    p = _props(((raw.get("visual") or {}).get("objects") or {}).get("general"))
    out = []
    for para in p.get("paragraphs") or []:
        if not isinstance(para, dict):
            continue
        runs = [r for r in para.get("textRuns") or [] if isinstance(r, dict)]
        text = "".join(str(r.get("value", "")) for r in runs).strip()
        if not text:
            continue
        style = runs[0].get("textStyle") if runs else None
        style = style if isinstance(style, dict) else {}
        pt = re.match(r"\s*([\d.]+)\s*pt", str(style.get("fontSize", "")))
        family = f"{style.get('fontFamily', '')} {style.get('fontWeight', '')}".lower()
        color = style.get("color")
        out.append({
            "text": text,
            "size": float(pt.group(1)) * 4 / 3 if pt else 16.0,   # 1pt = 4/3 px
            "color": color if isinstance(color, str) and _HEX.match(color) else None,
            "bold": "bold" in family,
            "align": para.get("horizontalTextAlignment") or "left",
        })
    return out


def textbox_lines(raw: dict) -> list[str]:
    """The paragraphs of a text box as plain lines."""
    return [p["text"] for p in textbox_paragraphs(raw)]


def _object_props(raw: dict, name: str) -> dict:
    return _props(((raw.get("visual") or {}).get("objects") or {}).get(name))


def _container_props(raw: dict, name: str) -> dict:
    return _props(((raw.get("visual") or {}).get("visualContainerObjects") or {}).get(name))


def deneb_mark(raw: dict) -> str | None:
    """The Vega-Lite ``mark`` type of a Deneb visual's spec, if it parses."""
    spec = _literal(_object_props(raw, "vega").get("jsonSpec"))
    if not isinstance(spec, str):
        return None
    try:
        doc = json.loads(spec)
    except ValueError:
        return None
    for _ in range(3):
        if not isinstance(doc, dict):
            return None
        if "mark" in doc:
            m = doc["mark"]
            return m.get("type") if isinstance(m, dict) else m
        layers = doc.get("layer")
        if isinstance(layers, list) and layers:
            doc = layers[0]
            continue
        return None
    return None


# --- painter ----------------------------------------------------------------------------

class Painter:
    """Collects primitives; thin helpers with sensible defaults."""

    def __init__(self, items: list):
        self.items = items

    def rect(self, x, y, w, h, **kw):
        self.items.append(Rect(x, y, max(0.0, w), max(0.0, h), **kw))

    def line(self, x1, y1, x2, y2, stroke, width=1.0, **kw):
        self.items.append(Line(x1, y1, x2, y2, stroke, width, **kw))

    def poly(self, points, **kw):
        self.items.append(Poly(list(points), **kw))

    def circle(self, cx, cy, r, **kw):
        self.items.append(Circle(cx, cy, r, **kw))

    def text(self, x, y, s, size, fill, **kw):
        if s:
            self.items.append(Text(x, y, _clean(s), size, fill, **kw))

    def sector(self, cx, cy, r, a0, a1, fill, hole=0.0, opacity=1.0):
        """Pie/donut slice; angles in degrees clockwise from 12 o'clock."""
        a1 = min(a1, a0 + 359.9)
        steps = max(2, int(abs(a1 - a0) / 4) + 1)
        outer = [self._pt(cx, cy, r, a0 + (a1 - a0) * i / steps) for i in range(steps + 1)]
        if hole > 0:
            inner = [self._pt(cx, cy, hole, a0 + (a1 - a0) * i / steps)
                     for i in range(steps, -1, -1)]
            pts = outer + inner
        else:
            pts = [(cx, cy)] + outer
        self.poly(pts, fill=fill, fill_opacity=opacity)

    @staticmethod
    def _pt(cx, cy, r, deg):
        a = math.radians(deg)
        return cx + r * math.sin(a), cy - r * math.cos(a)

    def ellipse(self, cx, cy, rx, ry, **kw):
        pts = [(cx + rx * math.cos(2 * math.pi * i / 48),
                cy + ry * math.sin(2 * math.pi * i / 48)) for i in range(48)]
        self.poly(pts, **kw)


# --- glyphs -------------------------------------------------------------------------------

@dataclass
class Style:
    """Colors shared by the glyph painters."""
    palette: list
    accent: str
    ink: str          # foreground text color
    muted: str        # secondary text color
    card: str         # container fill
    canvas: str       # page fill


def _g_bars(p: Painter, b, st: Style, horizontal: bool, stacked: bool, multi: bool):
    x, y, w, h = b
    along, span = (h, w) if horizontal else (w, h)
    n = 5 if along >= 110 else 4
    vals = [0.62, 0.95, 0.45, 0.8, 0.55][:n]
    slot = along / n
    thick = slot * 0.66
    c0, c1 = st.palette[0], st.palette[1 % len(st.palette)]

    def bar(a0, t, s0, s1, color):
        if horizontal:
            p.rect(x + s0 * span, y + a0, (s1 - s0) * span, t, fill=color, radius=1.5)
        else:
            p.rect(x + a0, y + span - s1 * span, t, (s1 - s0) * span, fill=color, radius=1.5)

    for i, v in enumerate(vals):
        a0 = i * slot + (slot - thick) / 2
        if multi and stacked:
            bar(a0, thick, 0, v * 0.6, c0)
            bar(a0, thick, v * 0.6, v, c1)
        elif multi:
            bar(a0, thick / 2 - 0.5, 0, v, c0)
            bar(a0 + thick / 2 + 0.5, thick / 2 - 0.5, 0, v * 0.72, c1)
        else:
            bar(a0, thick, 0, v, c0)
    if horizontal:
        p.line(x, y, x, y + h, st.muted, 1, opacity=0.6)
    else:
        p.line(x, y + h, x + w, y + h, st.muted, 1, opacity=0.6)


_LINE_PTS = [(0, .62), (.18, .40), (.36, .55), (.55, .22), (.75, .38), (1, .08)]


def _g_line(p: Painter, b, st: Style, area: bool, multi: bool):
    x, y, w, h = b
    pts = [(x + px * w, y + py * h) for px, py in _LINE_PTS]
    if area:
        p.poly(pts + [(x + w, y + h), (x, y + h)], fill=st.palette[0], fill_opacity=0.28)
    p.line(x, y + h, x + w, y + h, st.muted, 1, opacity=0.6)
    if multi:
        pts2 = [(x + px * w, y + min(h, (py * 0.7 + 0.28) * h)) for px, py in _LINE_PTS]
        p.poly(pts2, stroke=st.palette[1 % len(st.palette)], width=2, closed=False)
    p.poly(pts, stroke=st.palette[0], width=2, closed=False)
    for px, py in pts[::2]:
        p.circle(px, py, 2.4, fill=st.palette[0])


def _g_combo(p: Painter, b, st: Style):
    x, y, w, h = b
    n = 5
    vals = [.5, .7, .4, .8, .6]
    slot = w / n
    for i, v in enumerate(vals):
        p.rect(x + i * slot + slot * .18, y + h - v * h, slot * .64, v * h,
               fill=st.palette[0], fill_opacity=0.85, radius=1.5)
    pts = [(x + (i + .5) * slot, y + h * (.55 - .1 * math.sin(i * 1.3) - .06 * i))
           for i in range(n)]
    p.poly(pts, stroke=st.palette[1 % len(st.palette)], width=2, closed=False)
    for px, py in pts:
        p.circle(px, py, 2.6, fill=st.palette[1 % len(st.palette)])
    p.line(x, y + h, x + w, y + h, st.muted, 1, opacity=0.6)


def _g_waterfall(p: Painter, b, st: Style):
    x, y, w, h = b
    steps = [(0, .5, 0), (.5, .78, 0), (.78, .6, 1), (.6, .92, 0), (0, .92, 2)]
    slot = w / len(steps)
    cols = [st.palette[0], st.palette[1 % len(st.palette)], st.muted]
    for i, (a, c, kind) in enumerate(steps):
        lo, hi = min(a, c), max(a, c)
        p.rect(x + i * slot + slot * .18, y + h - hi * h, slot * .64, (hi - lo) * h,
               fill=cols[kind], radius=1.5)
    p.line(x, y + h, x + w, y + h, st.muted, 1, opacity=0.6)


def _g_pie(p: Painter, b, st: Style, donut: bool):
    x, y, w, h = b
    r = min(w, h) / 2 * 0.95
    cx, cy = x + w / 2, y + h / 2
    a = 0.0
    for i, frac in enumerate([0.42, 0.28, 0.18, 0.12]):
        p.sector(cx, cy, r, a, a + 360 * frac, st.palette[i % len(st.palette)],
                 hole=r * 0.55 if donut else 0.0)
        a += 360 * frac


def _g_scatter(p: Painter, b, st: Style):
    x, y, w, h = b
    r0 = max(2.0, min(w, h) / 30)
    pts = [(.15, .7, 1), (.3, .45, 1.5), (.42, .62, .9), (.55, .3, 1.7), (.68, .5, 1),
           (.8, .2, 1.3), (.9, .4, .9), (.25, .85, .9), (.6, .75, 1.3)]
    p.line(x, y, x, y + h, st.muted, 1, opacity=0.6)
    p.line(x, y + h, x + w, y + h, st.muted, 1, opacity=0.6)
    for i, (px, py, s) in enumerate(pts):
        p.circle(x + 6 + px * (w - 12), y + py * (h - 6), r0 * s,
                 fill=st.palette[i % 3 % len(st.palette)], fill_opacity=0.8)


def _g_gauge(p: Painter, b, st: Style):
    x, y, w, h = b
    r = min(w / 2, h * 0.9) * 0.95
    cx, cy = x + w / 2, y + h * 0.5 + r / 2
    p.sector(cx, cy, r, -90, 90, st.muted, hole=r * 0.62, opacity=0.25)
    p.sector(cx, cy, r, -90, 30, st.palette[0], hole=r * 0.62)
    p.line(cx, cy, *Painter._pt(cx, cy, r * 0.9, 30), st.ink, 2)
    p.circle(cx, cy, 3, fill=st.ink)


def _g_number(p: Painter, b, st: Style, arrow: bool = False):
    x, y, w, h = b
    size = max(12.0, min(h * 0.78, w / 3.1, 44))
    text = "123"
    tw = text_width(text, size, True)
    cx = x + w / 2 + (size * 0.3 if arrow else 0)
    p.text(cx, y + h / 2 + size * 0.35, text, size, st.accent, bold=True, anchor="middle")
    if arrow:
        ax, ay, s = cx - tw / 2 - size * 0.55, y + h / 2, size * 0.28
        p.poly([(ax, ay - s), (ax + s, ay + s), (ax - s, ay + s)], fill="#107C10")


def _g_table(p: Painter, b, st: Style, cols: int, matrix: bool):
    x, y, w, h = b
    cols = max(2, min(6, cols))
    row_h = 15.0
    rows = int(max(3, min(8, h // row_h)))
    row_h = h / rows
    p.rect(x, y, w, row_h, fill=st.accent, fill_opacity=0.28, radius=2)
    if matrix:
        p.rect(x, y + row_h, w / cols, h - row_h, fill=st.accent, fill_opacity=0.10)
    col_w = w / cols
    for r in range(1, rows):
        p.line(x, y + r * row_h, x + w, y + r * row_h, st.muted, 1, opacity=0.35)
    for c in range(1, cols):
        p.line(x + c * col_w, y, x + c * col_w, y + h, st.muted, 1, opacity=0.25)
    for r in range(rows):
        for c in range(cols):
            frac = 0.35 + 0.4 * (((r * 7 + c * 3) % 5) / 4)
            p.rect(x + c * col_w + 4, y + r * row_h + row_h / 2 - 1.5,
                   max(3.0, (col_w - 8) * frac), 3,
                   fill=st.ink if r == 0 else st.muted, fill_opacity=0.55, radius=1.5)
    p.rect(x, y, w, h, stroke=st.muted, stroke_opacity=0.5, radius=2)


def _g_slicer(p: Painter, b, st: Style):
    x, y, w, h = b
    n = int(max(2, min(5, h // 18)))
    gap = h / n
    for i in range(n):
        cy = y + gap * i + gap / 2
        p.rect(x + 2, cy - 5, 10, 10, stroke=st.ink, stroke_opacity=0.7,
               fill=st.accent if i == 0 else None, radius=2)
        p.rect(x + 20, cy - 2, max(10.0, (w - 26) * (0.85 - 0.15 * (i % 3))), 4,
               fill=st.muted, fill_opacity=0.45, radius=2)


def _g_image(p: Painter, b, st: Style):
    x, y, w, h = b
    s = min(w, h, 70)
    ix, iy = x + (w - s * 1.3) / 2, y + (h - s) / 2
    iw = s * 1.3
    p.rect(ix, iy, iw, s, stroke=st.ink, stroke_opacity=0.7, width=1, radius=3)
    p.circle(ix + iw * .28, iy + s * .3, s * .1, fill=st.accent)
    p.poly([(ix + 2, iy + s - 2), (ix + iw * .38, iy + s * .48),
            (ix + iw * .58, iy + s * .72), (ix + iw * .72, iy + s * .55),
            (ix + iw - 2, iy + s - 2)], fill=st.accent, fill_opacity=0.55)


def _g_text(p: Painter, b, st: Style):
    x, y, w, h = b
    n = int(max(2, min(5, h // 12)))
    for i in range(n):
        frac = 1.0 if i < n - 1 else 0.55
        p.rect(x, y + i * 12 + 3, min(w, 260) * frac, 4, fill=st.muted,
               fill_opacity=0.5, radius=2)


def _g_map(p: Painter, b, st: Style):
    x, y, w, h = b
    r = min(w, h) / 2 * 0.9
    cx, cy = x + w / 2, y + h / 2
    p.circle(cx, cy, r, stroke=st.muted, width=1.4)
    p.ellipse(cx, cy, r * .45, r, stroke=st.muted, width=1)
    p.line(cx - r, cy, cx + r, cy, st.muted, 1)
    p.poly([(cx + r * .3, cy - r * .1), (cx + r * .42, cy - r * .5), (cx + r * .54, cy - r * .1)],
           fill=st.accent)
    p.circle(cx + r * .42, cy - r * .5, r * .11, fill=st.accent)


def _g_funnel(p: Painter, b, st: Style):
    x, y, w, h = b
    n = 4
    slot = h / n
    for i, frac in enumerate([1.0, 0.72, 0.5, 0.3]):
        bw = w * frac
        p.rect(x + (w - bw) / 2, y + i * slot + slot * .12, bw, slot * .76,
               fill=st.palette[i % len(st.palette)], fill_opacity=0.9, radius=2)


def _g_treemap(p: Painter, b, st: Style):
    x, y, w, h = b
    g = 2
    cells = [(0, 0, .55, 1), (.55, 0, .45, .58), (.55, .58, .25, .42), (.8, .58, .2, .42)]
    for i, (a, c, cw, ch) in enumerate(cells):
        p.rect(x + a * w + g / 2, y + c * h + g / 2, cw * w - g, ch * h - g,
               fill=st.palette[i % len(st.palette)], fill_opacity=0.85, radius=2)


def _g_ribbon(p: Painter, b, st: Style):
    x, y, w, h = b
    n = 4
    slot = w / n
    cw = slot * .42
    splits = [(.0, .5, 1), (.0, .35, 1), (.0, .45, 1), (.0, .6, 1)]
    tops = [(.55, 1.0), (.4, 1.0), (.62, 1.0), (.5, 1.0)]
    for i in range(n):
        lo, hi = tops[i]
        p.rect(x + i * slot, y + h * (1 - hi + lo * 0.0), cw, h * hi * 0.55,
               fill=st.palette[0], radius=1.5)
        p.rect(x + i * slot, y + h * (1 - hi + hi * 0.55), cw, h * hi * 0.45,
               fill=st.palette[1 % len(st.palette)], radius=1.5)
    for i in range(n - 1):
        x0, x1 = x + i * slot + cw, x + (i + 1) * slot
        p.poly([(x0, y + h * .45), (x1, y + h * .45), (x1, y + h * .55), (x0, y + h * .55)],
               fill=st.palette[0], fill_opacity=0.35)


def _g_tree(p: Painter, b, st: Style):
    x, y, w, h = b
    nw, nh = min(w * .22, 60), min(h * .2, 22)
    root = (x, y + h / 2 - nh / 2)
    kids = [(x + w * .38, y + h * .18), (x + w * .38, y + h * .62)]
    leaves = [(x + w * .72, y + h * .04), (x + w * .72, y + h * .34)]
    p.rect(*root, nw, nh, fill=st.accent, radius=3)
    for kx, ky in kids:
        p.poly([(root[0] + nw, root[1] + nh / 2), (kx - 6, root[1] + nh / 2),
                (kx - 6, ky + nh / 2), (kx, ky + nh / 2)],
               stroke=st.muted, width=1, closed=False)
        p.rect(kx, ky, nw, nh, fill=st.palette[1 % len(st.palette)], fill_opacity=0.85, radius=3)
    for lx, ly in leaves:
        p.poly([(kids[0][0] + nw, kids[0][1] + nh / 2), (lx - 6, kids[0][1] + nh / 2),
                (lx - 6, ly + nh / 2), (lx, ly + nh / 2)],
               stroke=st.muted, width=1, closed=False)
        p.rect(lx, ly, nw, nh, fill=st.palette[2 % len(st.palette)], fill_opacity=0.85, radius=3)


def _g_code(p: Painter, b, st: Style):
    x, y, w, h = b
    size = max(14.0, min(h * .6, w / 4, 36))
    p.text(x + w / 2, y + h / 2 + size * .35, "{ }", size, st.accent, bold=True, anchor="middle")


def _g_generic(p: Painter, b, st: Style):
    x, y, w, h = b
    s = min(w, h, 46)
    p.rect(x + (w - s) / 2, y + (h - s) / 2, s, s, stroke=st.muted, dash=(3, 3), radius=4)
    p.rect(x + (w - s) / 2 + s * .3, y + (h - s) / 2 + s * .3, s * .4, s * .4,
           fill=st.accent, fill_opacity=0.6, radius=2)


def draw_glyph(p: Painter, family: str, box, st: Style, *, multi: bool, cols: int) -> None:
    """Draw the schematic glyph of `family` inside `box` = (x, y, w, h)."""
    x, y, w, h = box
    if w < 24 or h < 18:
        return
    if family in ("card",):
        _g_number(p, box, st)
    elif family == "kpi":
        _g_number(p, box, st, arrow=True)
    elif family == "bar":
        _g_bars(p, box, st, True, False, multi)
    elif family == "bar-stacked":
        _g_bars(p, box, st, True, True, True)
    elif family == "column":
        _g_bars(p, box, st, False, False, multi)
    elif family == "column-stacked":
        _g_bars(p, box, st, False, True, True)
    elif family == "line":
        _g_line(p, box, st, False, multi)
    elif family == "area":
        _g_line(p, box, st, True, multi)
    elif family == "combo":
        _g_combo(p, box, st)
    elif family == "waterfall":
        _g_waterfall(p, box, st)
    elif family in ("pie", "donut"):
        _g_pie(p, box, st, family == "donut")
    elif family == "scatter":
        _g_scatter(p, box, st)
    elif family == "gauge":
        _g_gauge(p, box, st)
    elif family in ("table", "matrix"):
        _g_table(p, box, st, cols, family == "matrix")
    elif family == "slicer":
        _g_slicer(p, box, st)
    elif family == "image":
        _g_image(p, box, st)
    elif family == "text":
        _g_text(p, box, st)
    elif family == "map":
        _g_map(p, box, st)
    elif family == "funnel":
        _g_funnel(p, box, st)
    elif family == "treemap":
        _g_treemap(p, box, st)
    elif family == "ribbon":
        _g_ribbon(p, box, st)
    elif family == "tree":
        _g_tree(p, box, st)
    elif family in ("code", "deneb"):
        _g_code(p, box, st)
    else:
        _g_generic(p, box, st)


# --- scene building ---------------------------------------------------------------------------

def _wrap_bindings(bindings: list, size: float, max_w: float, max_lines: int) -> list[str]:
    """Pack "Bucket: a, b" parts into lines joined by ' | '."""
    parts = [f"{bucket}: {', '.join(refs)}" for bucket, refs in bindings if refs]
    lines: list[str] = []
    cur = ""
    for part in parts:
        trial = f"{cur} | {part}" if cur else part
        if cur and text_width(trial, size) > max_w:
            lines.append(cur)
            cur = part
        else:
            cur = trial
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1] + " ..."
    return [fit_text(ln, size, max_w) for ln in lines]


def _shape_scene(p: Painter, v: VisualView, st: Style, theme: dict) -> None:
    """Shapes (backplates, header bands, dividers) are drawn as themselves."""
    fillp = _object_props(v.raw, "fill")
    outp = _object_props(v.raw, "outline")
    tile = _literal(_object_props(v.raw, "shape").get("tileShape")) or "rectangle"
    fill = _color(fillp.get("fillColor")) if _literal(fillp.get("show")) is not False else None
    out_shown = _literal(outp.get("show"))
    outline = _color(outp.get("lineColor")) if out_shown is not False else None
    weight = _num(_literal(outp.get("weight")), 1.0) if outline else 1.0
    weight = max(1.0, min(weight, 8.0))
    dash = (4, 3) if v.hidden else None
    x, y, w, h = v.x, v.y, v.w, v.h
    if fill is None and outline is None:
        outline, weight, dash = st.muted, 1.0, dash or (3, 3)   # keep it visible
    kw = dict(fill=fill, stroke=outline, stroke_width=weight)
    if tile in ("rectangle", "rectangleRounded"):
        p.rect(x, y, w, h, radius=min(10.0, w / 4, h / 4) if tile == "rectangleRounded" else 0.0,
               dash=dash, **kw)
    elif tile == "oval":
        p.ellipse(x + w / 2, y + h / 2, w / 2, h / 2, fill=fill, stroke=outline, width=weight)
    elif tile == "line":
        p.line(x, y + h / 2 if h < w else y, x + w if h < w else x,
               y + h / 2 if h < w else y + h, outline or fill or st.muted, weight or 1, dash=dash)
    elif tile == "triangle":
        p.poly([(x + w / 2, y), (x + w, y + h), (x, y + h)], fill=fill, stroke=outline, width=weight)
    elif tile == "hexagon":
        p.poly([(x + w * .25, y), (x + w * .75, y), (x + w, y + h / 2),
                (x + w * .75, y + h), (x + w * .25, y + h), (x, y + h / 2)],
               fill=fill, stroke=outline, width=weight)
    elif tile == "pentagon":
        p.poly([(x + w / 2, y), (x + w, y + h * .38), (x + w * .81, y + h),
                (x + w * .19, y + h), (x, y + h * .38)], fill=fill, stroke=outline, width=weight)
    elif tile == "arrow":
        p.poly([(x, y + h * .3), (x + w * .6, y + h * .3), (x + w * .6, y),
                (x + w, y + h / 2), (x + w * .6, y + h), (x + w * .6, y + h * .7),
                (x, y + h * .7)], fill=fill, stroke=outline, width=weight)
    else:
        p.rect(x, y, w, h, dash=dash, **kw)


_BUTTON_TYPES = ("actionButton", "pageNavigator", "bookmarkNavigator")


def _textbox_scene(p: Painter, v: VisualView, st: Style) -> str | None:
    """A text box draws its real text (size / color / alignment) on a
    transparent, faintly dotted box, so header bands behind it stay visible."""
    x, y, w, h = v.x, v.y, v.w, v.h
    op = 0.6 if v.hidden else 1.0
    p.rect(x, y, w, h, stroke=st.muted, stroke_width=1.0, stroke_opacity=0.35 * op,
           radius=max(0.0, min(4.0, w / 8, h / 8)), dash=(4, 3) if v.hidden else (1.5, 3))
    paras = textbox_paragraphs(v.raw)
    if not paras:
        p.text(x + 6, y + min(h - 4, 16), "(empty text box)", 10, st.muted,
               italic=True, opacity=op)
        return None
    pad = 6.0 if min(w, h) >= 24 else 2.0
    cursor = y + pad
    for para in paras:
        size = max(7.0, min(para["size"], 48.0))
        base = cursor + size * 0.95
        if base > y + h - 1 and cursor > y + pad:
            break
        text = fit_text(para["text"], size, w - 2 * pad, para["bold"])
        anchor = {"center": "middle", "right": "end"}.get(para["align"], "start")
        tx = {"middle": x + w / 2, "end": x + w - pad}.get(anchor, x + pad)
        p.text(tx, base, text, size, para["color"] or st.ink, bold=para["bold"],
               anchor=anchor, opacity=op)
        cursor = base + size * 0.3
    return paras[0]["text"]


def _button_scene(p: Painter, v: VisualView, st: Style, label: str) -> str:
    """Buttons and navigators draw as filled pills carrying their label."""
    x, y, w, h = v.x, v.y, v.w, v.h
    fill = _color(_object_props(v.raw, "fill").get("fillColor")) or st.accent
    tp = _object_props(v.raw, "text")
    text = _literal(tp.get("text"))
    text = text if isinstance(text, str) and text.strip() else label
    fcol = _color(tp.get("fontColor")) or ("#FFFFFF" if luminance(fill) < 0.6 else "#252423")
    op = 0.6 if v.hidden else 1.0
    p.rect(x, y, w, h, fill=fill, fill_opacity=op, radius=min(h / 2, 16.0),
           stroke=blend(fill, "#000000", 0.25) if v.hidden else None,
           dash=(4, 3) if v.hidden else None)
    size = max(8.0, min(13.0, h * 0.42))
    p.text(x + w / 2, y + h / 2 + size * 0.36, fit_text(text, size, w - 12, True), size,
           fcol, bold=True, anchor="middle", opacity=op)
    return text


def _visual_scene(items: list, v: VisualView, st: Style, theme: dict,
                  show_ids: bool) -> dict:
    """Append one visual's primitives; return its structured info."""
    p = Painter(items)
    fam = family_of(v.vtype)
    if fam == "deneb":
        fam = _MARK_FAMILY.get(deneb_mark(v.raw) or "", "deneb")
    label = type_label(v.vtype)
    static, shown = container_title(v.raw)
    auto = auto_title(v.vtype, v.bindings)
    title = static if (static and shown) else auto
    info = {"id": v.id, "type": v.vtype, "type_label": label, "title": title,
            "x": v.x, "y": v.y, "width": v.w, "height": v.h, "z": v.z,
            "hidden": v.hidden}
    attrs = {"data-visual-id": v.id, "data-visual-type": v.vtype or "",
             "data-z": f"{v.z:g}"}
    if v.hidden:
        attrs["data-hidden"] = "true"
    open_at = len(items)
    items.append(Open(attrs, None))
    x, y, w, h = v.x, v.y, v.w, v.h

    def finish() -> dict:
        info["title"] = title
        items[open_at].title = (f"{label} - {v.id}" + (f" - {title}" if title else "")
                                + (" (hidden)" if v.hidden else ""))
        items.append(Close())
        return info

    if fam == "shape":
        _shape_scene(p, v, st, theme)
        return finish()
    if v.vtype == "textbox":
        first = _textbox_scene(p, v, st)
        title = static or first
        if show_ids:
            p.text(x + w - 4, y + h - 4, v.id, 8, st.muted, anchor="end", opacity=0.8)
        return finish()
    if fam == "button":
        title = _button_scene(p, v, st, label)
        return finish()

    # container: own background color when the visual sets one
    bgp = _container_props(v.raw, "background")
    own = _color(bgp.get("color")) if _literal(bgp.get("show")) is not False else None
    fill = own or st.card
    radius = max(0.0, min(8.0, w / 6, h / 6))
    p.rect(x, y, w, h, fill=fill, fill_opacity=0.55 if v.hidden else 1.0,
           stroke=blend(st.canvas, st.ink, 0.28), stroke_width=1.0, radius=radius,
           dash=(4, 3) if v.hidden else None)
    ink = st.ink if not own else (st.ink if luminance(own) > 0.5 else "#FFFFFF")
    muted = st.muted if not own else (st.muted if luminance(own) > 0.5 else "#D0D0D0")
    lst = Style(st.palette, st.accent, ink, muted, fill, st.canvas)
    op = 0.6 if v.hidden else 1.0

    pad = 8.0 if min(w, h) >= 40 else 4.0
    small = h < 80
    tsize = 10.5 if small else 12.0
    ysize = 8.5 if small else 9.0
    bsize = 8.0 if small else 8.5
    wide = w >= 2.4 * h and h < 130
    text_w = (w * 0.58 if wide else w) - 2 * pad
    ty = y + pad + tsize * 0.85

    heading = title or label
    tag = "hidden" if v.hidden else None
    tag_w = text_width(tag, 8.5) + 6 if tag and w >= 90 else 0.0
    p.text(x + pad, ty, fit_text(heading, tsize, text_w - tag_w, True), tsize, ink,
           bold=title is not None, italic=title is None, opacity=op)
    if tag_w:
        p.text(x + w - pad, ty, tag, 8.5, lst.muted, italic=True, anchor="end")
    cursor = ty
    if h >= 46 and heading != label:
        cursor += ysize + 3
        tl = label + (f" - {v.id}" if show_ids else "")
        p.text(x + pad, cursor, fit_text(tl, ysize, text_w), ysize, lst.muted, opacity=op)
    elif show_ids and h >= 30:
        cursor += ysize + 3
        p.text(x + pad, cursor, fit_text(v.id, ysize, text_w), ysize, lst.muted, opacity=op)

    # body text: image resource / bindings summary
    extra: list[str] = []
    if v.vtype == "image":
        res = _object_props(v.raw, "general").get("imageUrl") or {}
        try:
            extra = [f"Image: {res['expr']['ResourcePackageItem']['ItemName']}"]
        except (KeyError, TypeError):
            extra = []
    room = (y + h - pad - cursor) if not wide else (h - (cursor - y) - pad)
    max_lines = int(max(0, min(3, (room - (0 if wide else 30)) // (bsize + 2))))
    if extra:
        lines = [fit_text(t, bsize, text_w) for t in extra][:max_lines]
    elif h >= 70 or wide:
        lines = _wrap_bindings(v.bindings, bsize, text_w, max_lines)
    else:
        lines = []
    for ln in lines:
        cursor += bsize + 2.5
        p.text(x + pad, cursor, ln, bsize, lst.muted, opacity=op)

    # schematic glyph in the remaining space
    if wide:
        box = (x + w * 0.62, y + pad, w * 0.34, h - 2 * pad)
    else:
        top = cursor + 8
        box = (x + pad, top, w - 2 * pad, y + h - pad - top)
    if box[2] >= 40 and box[3] >= 26:
        gx, gy, gw, gh = box
        cap_w, cap_h = min(gw, 460), min(gh, 300)
        box = (gx + (gw - cap_w) / 2, gy + (gh - cap_h) / 2, cap_w, cap_h)
        multi = any(k == "Series" for k, _ in v.bindings)
        n_fields = sum(len(r) for _, r in v.bindings)
        mark = len(items)
        draw_glyph(p, fam, box, lst, multi=multi, cols=max(2, n_fields))
        if v.hidden:                     # fade hidden visuals' glyph
            for it in items[mark:]:
                _fade(it, 0.5)
    return finish()


def _fade(it, k: float) -> None:
    for attr in ("opacity", "fill_opacity", "stroke_opacity"):
        if hasattr(it, attr):
            setattr(it, attr, getattr(it, attr) * k)


def _page_colors(page: dict, theme: dict) -> tuple[str, str, float]:
    """(wallpaper, page background, page background alpha)."""
    base = theme["background"]
    styles = ((theme.get("visualStyles") or {}).get("page") or {}).get("*") or {}
    bg_theme = _props(styles.get("background"))
    wall_theme = _props(styles.get("outspace"))
    bgp = _props(page["objects"].get("background")) or bg_theme
    wallp = _props(page["objects"].get("outspace")) or wall_theme
    bg = norm_hex(_color(bgp.get("color")), base) if bgp.get("color") else base
    transparency = _literal(bgp.get("transparency")) if "transparency" in bgp else None
    if transparency is None and isinstance(bgp.get("transparency"), (int, float)):
        transparency = bgp["transparency"]
    alpha = 1.0
    if isinstance(transparency, (int, float)) and not isinstance(transparency, bool):
        alpha = 1.0 - max(0.0, min(100.0, float(transparency))) / 100.0
    wall = norm_hex(_color(wallp.get("color")), bg) if wallp.get("color") else base
    return wall, bg, alpha


def build_scene(project, page_id: str, *, show_ids: bool = False) -> Scene:
    """The wireframe of one page as backend-neutral primitives."""
    page = collect_page(project, page_id)
    theme = load_theme(project)
    wall, bg, alpha = _page_colors(page, theme)
    canvas = bg if alpha >= 0.5 else wall
    fg = theme["foreground"]
    muted = theme.get("foregroundNeutralSecondary") or blend(canvas, fg, 0.6)
    if abs(luminance(muted) - luminance(canvas)) < 0.25:      # keep secondary text legible
        muted = blend(canvas, fg, 0.65)
    st = Style(palette=theme["dataColors"], accent=theme["accent"], ink=fg, muted=muted,
               card=blend(canvas, fg, 0.04), canvas=canvas)
    w, h = page["width"], page["height"]
    scene = Scene(width=w, height=h, background=wall, title=page["name"])
    items = scene.items
    p = Painter(items)
    p.rect(0, 0, w, h, fill=wall)
    p.rect(0, 0, w, h, fill=bg, fill_opacity=alpha)

    views = page["views"]
    drawn = sorted((v for v in views if not v.is_group),
                   key=lambda v: (v.z, v.order))
    for v in drawn:
        scene.visuals.append(_visual_scene(items, v, st, theme, show_ids))

    # group outlines on top, named
    for g in sorted((v for v in views if v.is_group), key=lambda v: (v.z, v.order)):
        name = g.group_name or g.id
        items.append(Open({"data-group-id": g.id, "data-group-name": name},
                          f"Group - {name}"))
        gc = st.accent
        p.rect(g.x, g.y, g.w, g.h, stroke=gc, stroke_width=1.4, dash=(6, 4), radius=4)
        label = fit_text(f"Group: {name}", 9, max(20.0, g.w - 8))
        lw = text_width(label, 9) + 8
        ly = g.y - 1 if g.y >= 13 else g.y + 12
        p.rect(g.x + 2, ly - 10, lw, 13, fill=gc, radius=3)
        p.text(g.x + 6, ly, label, 9, "#FFFFFF" if luminance(gc) < 0.6 else "#252423", bold=True)
        items.append(Close())
        scene.visuals.append({"id": g.id, "type": "visualGroup", "type_label": "Group",
                              "title": name, "x": g.x, "y": g.y, "width": g.w,
                              "height": g.h, "z": g.z, "hidden": g.hidden})
    if not views:
        p.text(w / 2, h / 2, "No visuals on this page", 14, muted, anchor="middle", italic=True)
    return scene


# --- SVG back-end -------------------------------------------------------------------------------

def _n(v: float) -> str:
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _esc(s: str) -> str:
    return (_clean(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _paint(fill: str | None, opacity: float, prefix: str = "fill") -> str:
    if fill is None:
        return f' {prefix}="none"'
    out = f' {prefix}="{norm_hex(fill)}"'
    if opacity < 0.999:
        out += f' {prefix}-opacity="{_n(opacity)}"'
    return out


def _stroke(stroke: str | None, width: float, opacity: float = 1.0,
            dash: tuple | None = None) -> str:
    if not stroke:
        return ""
    out = f' stroke="{norm_hex(stroke)}" stroke-width="{_n(width)}"'
    if opacity < 0.999:
        out += f' stroke-opacity="{_n(opacity)}"'
    if dash:
        out += f' stroke-dasharray="{_n(dash[0])} {_n(dash[1])}"'
    return out


def scene_to_svg(scene: Scene, scale: float = 1.0) -> str:
    """Serialise a scene to an SVG document (well-formed XML, UTF-8 safe)."""
    w, h = scene.width, scene.height
    out: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_n(w * scale)}" '
        f'height="{_n(h * scale)}" viewBox="0 0 {_n(w)} {_n(h)}" role="img" '
        f'aria-label="{_esc("Wireframe of page " + scene.title)}" '
        f'font-family="{FONT_STACK}">',
        f"<title>{_esc(scene.title)}</title>",
        f"<desc>{_esc('Wireframe render of report page ' + scene.title + ' (pbi-mcp)')}</desc>",
    ]
    for it in scene.items:
        if isinstance(it, Open):
            attrs = "".join(f' {k}="{_esc(v)}"' for k, v in it.attrs.items())
            out.append(f"<g{attrs}>")
            if it.title:
                out.append(f"<title>{_esc(it.title)}</title>")
        elif isinstance(it, Close):
            out.append("</g>")
        elif isinstance(it, Rect):
            rx = f' rx="{_n(it.radius)}"' if it.radius else ""
            out.append(
                f'<rect x="{_n(it.x)}" y="{_n(it.y)}" width="{_n(it.w)}" '
                f'height="{_n(it.h)}"{rx}{_paint(it.fill, it.fill_opacity)}'
                f"{_stroke(it.stroke, it.stroke_width, it.stroke_opacity, it.dash)}/>")
        elif isinstance(it, Line):
            out.append(
                f'<line x1="{_n(it.x1)}" y1="{_n(it.y1)}" x2="{_n(it.x2)}" y2="{_n(it.y2)}"'
                f'{_stroke(it.stroke, it.width, it.opacity, it.dash)}/>')
        elif isinstance(it, Poly):
            pts = " ".join(f"{_n(px)},{_n(py)}" for px, py in it.points)
            tag = "polygon" if it.closed else "polyline"
            fill = _paint(it.fill, it.fill_opacity)
            join = ' stroke-linejoin="round" stroke-linecap="round"' if it.stroke else ""
            out.append(f'<{tag} points="{pts}"{fill}'
                       f'{_stroke(it.stroke, it.width, it.opacity)}{join}/>')
        elif isinstance(it, Circle):
            out.append(
                f'<circle cx="{_n(it.cx)}" cy="{_n(it.cy)}" r="{_n(it.r)}"'
                f"{_paint(it.fill, it.fill_opacity)}{_stroke(it.stroke, it.width)}/>")
        elif isinstance(it, Text):
            weight = ' font-weight="bold"' if it.bold else ""
            style = ' font-style="italic"' if it.italic else ""
            anchor = f' text-anchor="{it.anchor}"' if it.anchor != "start" else ""
            op = f' opacity="{_n(it.opacity)}"' if it.opacity < 0.999 else ""
            out.append(
                f'<text x="{_n(it.x)}" y="{_n(it.y)}" font-size="{_n(it.size)}"'
                f'{weight}{style}{anchor} fill="{norm_hex(it.fill)}"{op}>{_esc(it.s)}</text>')
    out.append("</svg>")
    return "\n".join(out) + "\n"


# --- PNG back-end (Pillow) -----------------------------------------------------------------------

def _rgba(color: str | None, opacity: float = 1.0):
    if color is None:
        return None
    r, g, b, a = parse_color(color)
    return r, g, b, int(round(255 * max(0.0, min(1.0, opacity * a))))


def _dashes(x1, y1, x2, y2, dash):
    """Split a segment into dash pieces."""
    length = math.hypot(x2 - x1, y2 - y1)
    if length == 0:
        return
    on, off = dash
    ux, uy = (x2 - x1) / length, (y2 - y1) / length
    pos = 0.0
    while pos < length:
        end = min(pos + on, length)
        yield (x1 + ux * pos, y1 + uy * pos, x1 + ux * end, y1 + uy * end)
        pos += on + off


def scene_to_png(scene: Scene, scale: float = 1.0) -> bytes:
    """Rasterise a scene with Pillow (default font, 2x supersampling)."""
    Image, ImageDraw, ImageFont = require_pillow()
    width, height = max(1, round(scene.width * scale)), max(1, round(scene.height * scale))
    ss = 2 if width * height <= 4_000_000 else 1
    k = scale * ss
    img = Image.new("RGB", (width * ss, height * ss), norm_hex(scene.background, "#FFFFFF"))
    draw = ImageDraw.Draw(img, "RGBA")
    fonts: dict[int, object] = {}

    def font(size: float):
        px = max(6, int(round(size * k)))
        if px not in fonts:
            try:
                fonts[px] = ImageFont.load_default(size=px)     # Pillow >= 10.1
            except TypeError:
                fonts[px] = ImageFont.load_default()
        return fonts[px]

    def seg(x1, y1, x2, y2, color, wpx, dash=None):
        if dash:
            for a, b, c, d in _dashes(x1, y1, x2, y2, (dash[0] * k, dash[1] * k)):
                draw.line([(a, b), (c, d)], fill=color, width=wpx)
        else:
            draw.line([(x1, y1), (x2, y2)], fill=color, width=wpx)

    for it in scene.items:
        if isinstance(it, Rect):
            x0, y0, x1, y1 = it.x * k, it.y * k, (it.x + it.w) * k, (it.y + it.h) * k
            r = min(it.radius * k, (x1 - x0) / 2, (y1 - y0) / 2)
            fill = _rgba(it.fill, it.fill_opacity)
            stroke = _rgba(it.stroke, it.stroke_opacity)
            wpx = max(1, int(round(it.stroke_width * k)))
            if fill:
                if r >= 1:
                    draw.rounded_rectangle([x0, y0, x1, y1], radius=r, fill=fill)
                else:
                    draw.rectangle([x0, y0, x1, y1], fill=fill)
            if stroke and it.dash is None:
                if r >= 1:
                    draw.rounded_rectangle([x0, y0, x1, y1], radius=r, outline=stroke, width=wpx)
                else:
                    draw.rectangle([x0, y0, x1, y1], outline=stroke, width=wpx)
            elif stroke:
                d = (it.dash[0] * k, it.dash[1] * k)
                for a, b, c, e in ((x0 + r, y0, x1 - r, y0), (x1, y0 + r, x1, y1 - r),
                                   (x1 - r, y1, x0 + r, y1), (x0, y1 - r, x0, y0 + r)):
                    for s in _dashes(a, b, c, e, d):
                        draw.line([(s[0], s[1]), (s[2], s[3])], fill=stroke, width=wpx)
                if r >= 1:
                    for box, a0 in (((x0, y0, x0 + 2 * r, y0 + 2 * r), 180),
                                    ((x1 - 2 * r, y0, x1, y0 + 2 * r), 270),
                                    ((x1 - 2 * r, y1 - 2 * r, x1, y1), 0),
                                    ((x0, y1 - 2 * r, x0 + 2 * r, y1), 90)):
                        draw.arc(box, a0, a0 + 90, fill=stroke, width=wpx)
        elif isinstance(it, Line):
            seg(it.x1 * k, it.y1 * k, it.x2 * k, it.y2 * k,
                _rgba(it.stroke, it.opacity), max(1, int(round(it.width * k))), it.dash)
        elif isinstance(it, Poly):
            pts = [(px * k, py * k) for px, py in it.points]
            fill = _rgba(it.fill, it.fill_opacity)
            if fill and len(pts) >= 3:
                draw.polygon(pts, fill=fill)
            stroke = _rgba(it.stroke, it.opacity)
            if stroke and len(pts) >= 2:
                wpx = max(1, int(round(it.width * k)))
                path = pts + ([pts[0]] if it.closed else [])
                draw.line(path, fill=stroke, width=wpx, joint="curve")
        elif isinstance(it, Circle):
            box = [(it.cx - it.r) * k, (it.cy - it.r) * k, (it.cx + it.r) * k, (it.cy + it.r) * k]
            fill = _rgba(it.fill, it.fill_opacity)
            stroke = _rgba(it.stroke)
            draw.ellipse(box, fill=fill, outline=stroke,
                         width=max(1, int(round(it.width * k))) if stroke else 0)
        elif isinstance(it, Text):
            f = font(it.size)
            color = _rgba(it.fill, it.opacity)
            anchor = {"start": "ls", "middle": "ms", "end": "rs"}[it.anchor]
            x, y = it.x * k, it.y * k
            reps = (0.0, max(1.0, it.size * k * 0.04)) if it.bold else (0.0,)
            for dx in reps:
                try:
                    draw.text((x + dx, y), it.s, font=f, fill=color, anchor=anchor)
                except (ValueError, TypeError):      # bitmap fallback font: no anchors
                    tw = draw.textlength(it.s, font=f)
                    ox = {"ls": 0, "ms": tw / 2, "rs": tw}[anchor]
                    draw.text((x + dx - ox, y - it.size * k * 0.8), it.s, font=f, fill=color)
    if ss > 1:
        resample = getattr(Image, "Resampling", Image).LANCZOS
        img = img.resize((width, height), resample)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


# --- files + public API ---------------------------------------------------------------------------

def default_render_dir(project) -> Path:
    """``<Report>/.pbi/mcp-renders`` -- Desktop ignores ``.pbi/``."""
    return project._require_report() / ".pbi" / "mcp-renders"


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _check_args(fmt: str, scale: float) -> None:
    if fmt not in ("svg", "png"):
        raise ValueError("format must be 'svg' or 'png'")
    if not isinstance(scale, (int, float)) or isinstance(scale, bool) \
            or not (0 < scale <= MAX_SCALE):
        raise ValueError(f"scale must be a number in (0, {MAX_SCALE:g}]")


def _resolve_out(project, page_id: str, fmt: str, out_path) -> Path:
    if out_path is None:
        return default_render_dir(project) / f"{page_id}.{fmt}"
    p = Path(os.path.expanduser(str(out_path)))
    if p.is_dir() or str(out_path).endswith(("/", "\\")):
        return p / f"{page_id}.{fmt}"
    if p.suffix.lower() != f".{fmt}":
        raise ValueError(f"out_path {str(p)!r} must end in .{fmt} for format {fmt!r} "
                         f"(or be a directory)")
    return p


def render_page(project, page_id: str, fmt: str = "svg", out_path=None,
                scale: float = 1.0, inline: bool = True,
                show_ids: bool = False) -> dict:
    """Render `page_id` to a file; returns {path, width, height, bytes, ...}."""
    _check_args(fmt, scale)
    if fmt == "png":
        require_pillow()                     # fail before doing any work
    scene = build_scene(project, page_id, show_ids=show_ids)
    target = _resolve_out(project, page_id, fmt, out_path)
    if fmt == "svg":
        text = scene_to_svg(scene, scale)
        data = text.encode("utf-8")
    else:
        text = None
        data = scene_to_png(scene, scale)
    _atomic_write_bytes(target, data)
    result = {
        "ok": True,
        "page_id": page_id,
        "page_name": scene.title,
        "format": fmt,
        "path": str(target),
        "width": round(scene.width * scale),
        "height": round(scene.height * scale),
        "scale": scale,
        "bytes": len(data),
        "visual_count": len(scene.visuals),
        "visuals": scene.visuals,
    }
    if fmt == "svg" and inline:
        if len(data) < INLINE_SVG_LIMIT:
            result["svg"] = text
        else:
            result["inline_skipped"] = (f"SVG is {len(data)} bytes (limit "
                                        f"{INLINE_SVG_LIMIT}); open {target} instead")
    return result


def render_report(project, fmt: str = "svg", out_dir=None, scale: float = 1.0) -> list[dict]:
    """Render every page of the report; one result dict per page (no inline SVG)."""
    _check_args(fmt, scale)
    if fmt == "png":
        require_pillow()
    base = Path(os.path.expanduser(str(out_dir))) if out_dir else default_render_dir(project)
    results = []
    for page in project.list_pages():
        r = render_page(project, page.id, fmt, base / f"{page.id}.{fmt}", scale, inline=False)
        r.pop("visuals", None)
        results.append(r)
    return results
