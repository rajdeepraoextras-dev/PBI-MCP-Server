"""Page templates — compose design elements + visuals into a designed page
(E3 D15-16). A template returns an ordered build plan the report server
executes through the existing primitives (style_page, add_shape, add_text,
add_visual). Positions come from the layout engine.

Templates:
  exec-summary : header band + KPI strip (cards on backplates) + chart grid
  kpi-detail   : compact header + wide KPI strip + full-width detail table
  story        : large title header + single hero chart + supporting row

The plan is a list of steps: {"op": ..., ...}. Executing it is the server's
job so all writes stay validated + atomic.
"""

from __future__ import annotations

from core.layout import GUTTER, MARGIN, Grid

# z-order lanes: backplates behind, data mid, labels/chrome on top
Z_BACKPLATE = 100
Z_HEADER_BG = 200
Z_DATA = 1000
Z_LABEL = 5000


def _header_band(title: str, subtitle: str | None, width: float,
                 accent: str, fg: str,
                 style: dict | None = None) -> list[dict]:
    steps: list[dict] = []
    hstyle = (style or {}).get("header", {})
    tstyle = (style or {}).get("title", {})
    band_h = hstyle.get("height", 72)
    band_fill = hstyle.get("fill") or accent
    title_size = tstyle.get("size", 20)
    title_color = tstyle.get("color", "#FFFFFF")
    steps.append({"op": "shape", "shape": "rectangle", "fill": band_fill,
                  "position": {"x": 0, "y": 0, "width": width, "height": band_h},
                  "z": Z_HEADER_BG})
    runs = [{"text": title, "bold": True, "size": title_size,
             "color": title_color}]
    steps.append({"op": "text", "runs": runs,
                  "position": {"x": MARGIN, "y": max(8, (band_h - 32) // 2),
                               "width": width - 2 * MARGIN,
                               "height": 32}, "z": Z_LABEL})
    if subtitle:
        steps.append({"op": "text",
                      "runs": [{"text": subtitle, "size": 10, "color": "#E8EEF5"}],
                      "position": {"x": MARGIN, "y": band_h - 26,
                                   "width": width - 2 * MARGIN, "height": 20},
                      "z": Z_LABEL})
    return steps, band_h


def _kpi_strip(kpis: list[dict], width: float, top: float,
               backplate: str, style: dict | None = None
               ) -> tuple[list[dict], float]:
    """kpis: [{"measure": "Table.M", "title": ...}]. Card on a backplate."""
    steps: list[dict] = []
    kstyle = (style or {}).get("kpi", {})
    use_backplate = kstyle.get("backplate", True)
    backplate = kstyle.get("backplate_fill") or backplate
    round_bp = kstyle.get("backplate_round", True)
    grid = Grid(width=width)
    n = min(len(kpis), 6)
    span = max(1, 12 // n)
    card_h = kstyle.get("height", 104)
    for i, kpi in enumerate(kpis):
        col = (i % n) * span
        row = i // n
        y = top + row * (card_h + GUTTER)
        pos = grid.span(col, span, y, card_h)
        if use_backplate:
            steps.append({"op": "shape", "shape": "rectangle",
                          "fill": backplate, "round_corners": round_bp,
                          "position": dict(pos), "z": Z_BACKPLATE})
            inner = {"x": pos["x"] + 12, "y": pos["y"] + 10,
                     "width": pos["width"] - 24, "height": pos["height"] - 20,
                     "z": Z_DATA}  # card sits IN FRONT of its backplate
        else:
            inner = {**pos, "z": Z_DATA}
        steps.append({"op": "visual",
                      "spec": {"visual_type": "card",
                               "bindings": {"Values": [kpi["measure"]]},
                               "title": kpi.get("title"),
                               "position": inner}})
    rows = -(-len(kpis) // n)
    return steps, top + rows * (card_h + GUTTER) + GUTTER


def exec_summary_plan(title: str, subtitle: str | None,
                      kpis: list[dict], charts: list[dict], *,
                      width: float = 1280, accent: str = "#1F3A5F",
                      backplate: str = "#F5F7FA",
                      page_bg: str = "#FFFFFF",
                      style: dict | None = None) -> list[dict]:
    """Build an executive-summary page plan.

    `style` (from core.page_style.extract_page_style) overrides the header
    band height/fill, title font, and KPI card height / backplate so the page
    matches an existing one.
    """
    from core.layout import layout_page

    steps: list[dict] = [{"op": "style_page", "background_color": page_bg,
                          "wallpaper_color": backplate}]
    header, band_h = _header_band(title, subtitle, width, accent, "#252423",
                                  style)
    steps += header

    strip, body_top = _kpi_strip(kpis, width, band_h + GUTTER, backplate, style) \
        if kpis else ([], band_h + GUTTER)
    steps += strip

    # charts laid out below the KPI strip
    chart_specs = [dict(c) for c in charts]
    laid, page_height = layout_page(
        chart_specs, width=width, height=max(720, body_top + 320),
        header_height=body_top - MARGIN)
    for c in chart_specs:
        steps.append({"op": "visual", "spec": c})
    return steps, page_height


TEMPLATES = {"exec-summary": exec_summary_plan}
