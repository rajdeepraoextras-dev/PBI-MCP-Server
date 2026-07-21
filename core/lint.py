"""Design lint — catch layout/design problems before they ship (E3 D17).

Checks a page's visuals for issues that make a report look sloppy:
  * overlap      — two data visuals overlap (ignores intentional backplates/
                   group containers, which are meant to sit behind)
  * off_canvas   — a visual extends past the page bounds
  * tiny         — a visual too small to be readable
  * crowding     — visuals almost-but-not-quite aligned (jitter)
Returns a list of findings {severity, code, message, visuals}.
"""

from __future__ import annotations

_BACKPLATE_TYPES = {"shape", "basicShape", "image"}
MIN_W = 80
MIN_H = 40
ALIGN_TOL = 8  # px: closer than this but not equal reads as misalignment


def _box(v):
    p = v.position
    return p.x, p.y, p.x + p.width, p.y + p.height


def _overlap(a, b) -> bool:
    ax1, ay1, ax2, ay2 = _box(a)
    bx1, by1, bx2, by2 = _box(b)
    return not (ax2 <= bx1 or bx2 <= ax1 or ay2 <= by1 or by2 <= ay1)


def lint_page(page, visuals, *, page_width=1280, page_height=720) -> list[dict]:
    findings: list[dict] = []
    data = [v for v in visuals
            if v.visual_type not in _BACKPLATE_TYPES
            and "visualGroup" not in (v.raw or {})]

    # overlaps between data visuals
    for i, a in enumerate(data):
        for b in data[i + 1:]:
            if _overlap(a, b):
                findings.append({
                    "severity": "warning", "code": "overlap",
                    "message": f"visuals {a.id} and {b.id} overlap",
                    "visuals": [a.id, b.id]})

    ph = page.height or page_height
    pw = page.width or page_width
    for v in visuals:
        x1, y1, x2, y2 = _box(v)
        if x2 > pw + 1 or y2 > ph + 1 or x1 < -1 or y1 < -1:
            findings.append({
                "severity": "warning", "code": "off_canvas",
                "message": f"{v.id} extends beyond the page ({pw}x{ph})",
                "visuals": [v.id]})
        if v.visual_type not in _BACKPLATE_TYPES and \
                (v.position.width < MIN_W or v.position.height < MIN_H):
            findings.append({
                "severity": "info", "code": "tiny",
                "message": f"{v.id} may be too small to read "
                           f"({int(v.position.width)}x{int(v.position.height)})",
                "visuals": [v.id]})

    # near-misaligned edges (jitter)
    xs = [round(v.position.x) for v in data]
    for i, a in enumerate(data):
        for b in data[i + 1:]:
            dx = abs(a.position.x - b.position.x)
            if 0 < dx <= ALIGN_TOL:
                findings.append({
                    "severity": "info", "code": "misalign",
                    "message": f"{a.id} and {b.id} left edges are {dx:.0f}px "
                               f"apart — snap to align",
                    "visuals": [a.id, b.id]})
                break
    return findings
