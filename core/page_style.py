"""Extract the header / KPI layout style from an existing page (E-extra).

So a newly-built page can match not just the theme color and page size, but
the actual composition of an existing page: header band height + color, title
font, KPI card height, and whether cards sit on backplates. Best-effort — an
arbitrary page may have no recognizable pattern, in which case fields are
simply absent and the caller falls back to defaults.
"""

from __future__ import annotations

_SHAPE_TYPES = {"shape", "basicShape"}
_CARD_TYPES = {"card", "cardVisual", "multiRowCard"}


def _literal(node) -> str | None:
    """Pull a quoted literal value out of an {expr:{Literal:{Value}}} node."""
    try:
        v = node["expr"]["Literal"]["Value"]
    except (KeyError, TypeError):
        return None
    if isinstance(v, str) and len(v) >= 2 and v.startswith("'") and v.endswith("'"):
        return v[1:-1]
    return v


def _shape_fill(visual) -> str | None:
    objs = (visual.raw.get("visual", {}) or {}).get("objects", {})
    for key in ("fill",):
        entries = objs.get(key) or []
        if entries:
            fill = entries[0].get("properties", {}).get("fillColor")
            if isinstance(fill, dict):
                try:
                    return _literal(fill["solid"]["color"])
                except (KeyError, TypeError):
                    return None
    return None


def _shape_rounded(visual) -> bool:
    objs = (visual.raw.get("visual", {}) or {}).get("objects", {})
    entries = objs.get("shape") or []
    if entries:
        tile = _literal(entries[0].get("properties", {}).get("tileShape", {}))
        return tile == "rectangleRounded"
    return False


def _title_style(visual) -> dict | None:
    try:
        para = visual.raw["visual"]["objects"]["general"][0]["properties"] \
            ["paragraphs"][0]
        run = para["textRuns"][0]
        style = run.get("textStyle", {})
    except (KeyError, IndexError, TypeError):
        return None
    out = {}
    size = style.get("fontSize")
    if isinstance(size, str) and size.endswith("pt"):
        try:
            out["size"] = int(float(size[:-2]))
        except ValueError:
            pass
    if style.get("color"):
        out["color"] = style["color"]
    return out or None


def _contains(outer, inner, tol: float = 4) -> bool:
    return (outer.x - tol <= inner.x
            and outer.y - tol <= inner.y
            and outer.x + outer.width + tol >= inner.x + inner.width
            and outer.y + outer.height + tol >= inner.y + inner.height)


def extract_page_style(project, page_id: str) -> dict:
    """Return {header?, title?, kpi?} describing a page's composition."""
    pages = {p.id: p for p in project.list_pages()}
    if page_id not in pages:
        raise KeyError(f"Page {page_id!r} not found")
    pw = pages[page_id].width or 1280
    visuals = project.list_visuals(page_id)
    shapes = [v for v in visuals if v.visual_type in _SHAPE_TYPES]
    cards = [v for v in visuals if v.visual_type in _CARD_TYPES]

    style: dict = {}

    # header band: a wide shape near the top
    for s in sorted(shapes, key=lambda v: v.position.y):
        p = s.position
        if p.y <= 48 and p.width >= 0.7 * pw and 0 < p.height <= 160:
            style["header"] = {"height": round(p.height),
                               "fill": _shape_fill(s)}
            break

    # title textbox in the header region
    header_h = style.get("header", {}).get("height", 80)
    for t in sorted((v for v in visuals if v.visual_type == "textbox"),
                    key=lambda v: v.position.y):
        if t.position.y <= header_h + 8:
            ts = _title_style(t)
            if ts:
                style["title"] = ts
            break

    # KPI cards + backplate detection
    if cards:
        card_h = round(sum(c.position.height for c in cards) / len(cards))
        kpi = {"count": len(cards)}
        backed, fill, rounded, bp_heights = 0, None, False, []
        for c in cards:
            for s in shapes:
                if _contains(s.position, c.position) and \
                        s.position.z < c.position.z:
                    backed += 1
                    fill = fill or _shape_fill(s)
                    rounded = rounded or _shape_rounded(s)
                    bp_heights.append(s.position.height)
                    break
        if backed >= max(1, len(cards) // 2):
            # report the backplate (outer) height so re-matching is stable —
            # the template uses this as the card-slot height then insets.
            kpi["height"] = round(sum(bp_heights) / len(bp_heights))
            kpi.update(backplate=True, backplate_fill=fill,
                       backplate_round=rounded)
        else:
            kpi["height"] = card_h
            kpi["backplate"] = False
        style["kpi"] = kpi

    return style
