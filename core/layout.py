"""Layout engine — a 12-column grid with horizontal bands (E3 D13-14).

Turns a page spec into non-overlapping, aligned, spacing-consistent visual
positions. Replaces the ad-hoc auto_layout with a real grid so output reads
as designed. Bands stack vertically: header, kpi (cards), body (charts),
footer. The page auto-extends in height if the body doesn't fit.

All positions are dicts {x, y, width, height} in report units.
"""

from __future__ import annotations

from dataclasses import dataclass

GUTTER = 12
MARGIN = 16
COLS = 12

CARD_TYPES = {"card", "cardVisual", "multiRowCard", "gauge"}
FULL_WIDTH_HINT = {"tableEx", "pivotTable", "lineChart", "stackedAreaChart"}


@dataclass
class Grid:
    width: float = 1280
    margin: float = MARGIN
    gutter: float = GUTTER
    cols: int = COLS

    def col_width(self) -> float:
        usable = self.width - 2 * self.margin - (self.cols - 1) * self.gutter
        return usable / self.cols

    def span(self, col_start: int, col_count: int, y: float,
             height: float) -> dict:
        cw = self.col_width()
        x = self.margin + col_start * (cw + self.gutter)
        w = col_count * cw + (col_count - 1) * self.gutter
        return {"x": round(x), "y": round(y),
                "width": round(w), "height": round(height)}


def _cols_for(n: int) -> int:
    """How many grid columns each of n equal tiles should span."""
    if n <= 0:
        return COLS
    per = max(1, COLS // n)
    return per


def layout_page(visuals: list[dict], *, width: float = 1280,
                height: float = 720, header_height: float = 0,
                footer_height: float = 0) -> tuple[list[dict], float]:
    """Assign a grid position to every visual lacking one.

    Returns (visuals, page_height). Cards go in a top KPI band (up to 6 across),
    charts flow on a 2-column body grid, full-width types take all 12 columns.
    Explicit positions are left untouched. Page height extends to fit.
    """
    grid = Grid(width=width)
    y = MARGIN + header_height

    auto = [v for v in visuals if "position" not in v]
    cards = [v for v in auto if v["visual_type"] in CARD_TYPES]
    charts = [v for v in auto if v["visual_type"] not in CARD_TYPES]

    # KPI band: cards share one row (max 6), each an equal column span
    if cards:
        n = min(len(cards), 6)
        rows = -(-len(cards) // n)
        span = max(1, COLS // n)
        card_h = 110
        for i, v in enumerate(cards):
            r, c = divmod(i, n)
            v["position"] = grid.span(c * span, span,
                                      y + r * (card_h + GUTTER), card_h)
        y += rows * (card_h + GUTTER) + GUTTER

    # Body: charts flow on a 2-column grid; tables/matrices span full width.
    row_h = 300
    col_cursor = 0  # 0 = left half, 6 = right half
    max_y = y
    lone = None     # left-half chart still waiting for a right-half partner

    def _widen(chart: dict) -> None:
        pos = chart["position"]
        chart["position"] = grid.span(0, COLS, pos["y"], pos["height"])

    for v in charts:
        full_width = v["visual_type"] in ("tableEx", "pivotTable")
        if full_width:
            if col_cursor == 6:      # close the half-open row first ...
                if lone is not None:
                    _widen(lone)     # ... and don't leave its right half empty
                    lone = None
                y += row_h + GUTTER
                col_cursor = 0
            v["position"] = grid.span(0, COLS, y, row_h)
            y += row_h + GUTTER
        else:
            v["position"] = grid.span(col_cursor, 6, y, row_h)
            if col_cursor == 0:
                col_cursor = 6
                lone = v
            else:
                col_cursor = 0
                lone = None
                y += row_h + GUTTER
        max_y = max(max_y, v["position"]["y"] + v["position"]["height"])
    if col_cursor == 6 and lone is not None:   # odd chart count: last one spans the row
        _widen(lone)

    page_height = max(height, max_y + footer_height + MARGIN)
    return visuals, page_height
