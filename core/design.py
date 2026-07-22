"""Design-layer element builders — text, image, shape (E2).

Epic reports are mostly design objects layered around the charts: title
banners, section headers, logos, KPI backplates, dividers, accent bars. These
are all `visual.json` containers with a `visualType` but no query. Every shape
here was copied from real Desktop exports, not invented.

Z-order convention: backplates/wallpaper shapes get LOW z (behind data
visuals); labels/logos get HIGH z (on top). Callers pass z via position.
"""

from __future__ import annotations

from core.formatting import _expr_literal, encode_property
from core.pbir import _VC_SCHEMA


def _container(visual_id: str, visual: dict, position: dict | None) -> dict:
    pos = {"x": 0, "y": 0, "z": 0, "width": 200, "height": 60, "tabOrder": 0}
    if position:
        pos.update(position)
    visual.setdefault("drillFilterOtherVisuals", True)
    return {"$schema": _VC_SCHEMA, "name": visual_id,
            "position": pos, "visual": visual}


# --- textbox ----------------------------------------------------------------

def build_textbox(visual_id: str, runs, position: dict | None = None) -> dict:
    """A textbox. `runs` is a string or a list of run dicts:

    {"text": ..., "bold": bool, "italic": bool, "size": 14, "color": "#333",
     "font": "Segoe UI", "align": "left|center|right", "url": "https://..."}
    """
    if isinstance(runs, str):
        runs = [{"text": runs}]

    text_runs = []
    align = None
    for r in runs:
        style = {}
        family = r.get("font", "Segoe UI")
        if r.get("bold"):
            family = f"{family} (Bold)" if "(" not in family else family
        style["fontFamily"] = family
        if r.get("size") is not None:
            style["fontSize"] = f"{r['size']}pt"
        if r.get("color"):
            style["color"] = r["color"]
        if r.get("italic"):
            style["fontStyle"] = "italic"
        run = {"value": r.get("text", "")}
        if style:
            run["textStyle"] = style
        if r.get("url"):
            run["url"] = r["url"]
        text_runs.append(run)
        align = r.get("align", align)

    paragraph: dict = {"textRuns": text_runs}
    if align:
        paragraph["horizontalTextAlignment"] = align

    visual = {
        "visualType": "textbox",
        "objects": {"general": [{"properties": {"paragraphs": [paragraph]}}]},
    }
    return _container(visual_id, visual, position)


# --- image ------------------------------------------------------------------

def build_image(visual_id: str, resource_name: str,
                position: dict | None = None,
                scaling: str | None = None) -> dict:
    """An image visual referencing a file already in RegisteredResources.

    `scaling` in {Fit, Fill, Normal} sets imageScalingType.
    """
    general_props = {
        "imageUrl": {"expr": {"ResourcePackageItem": {
            "PackageName": "RegisteredResources",
            "PackageType": 1,
            "ItemName": resource_name,
        }}}
    }
    objects: dict = {"general": [{"properties": general_props}]}
    if scaling:
        objects["imageScaling"] = [{"properties": {
            "imageScalingType": _expr_literal(scaling)}}]
    visual = {"visualType": "image", "objects": objects}
    return _container(visual_id, visual, position)


# --- shape ------------------------------------------------------------------

_TILE_SHAPES = {"rectangle", "rectangleRounded", "oval", "line",
                "arrow", "triangle", "hexagon", "pentagon"}


def build_shape(visual_id: str, shape: str = "rectangle",
                fill: str | None = None, outline: str | None = None,
                outline_weight: float | None = None,
                position: dict | None = None,
                round_corners: bool = False) -> dict:
    """A shape visual (newer 'shape' type): backplates, dividers, accent bars.

    `fill`/`outline` are '#hex' or None. `shape` in the tile-shape set;
    round_corners maps a rectangle to rectangleRounded.
    """
    tile = shape
    if shape == "rectangle" and round_corners:
        tile = "rectangleRounded"
    if tile not in _TILE_SHAPES:
        raise ValueError(f"Unknown shape {shape!r}; use one of {sorted(_TILE_SHAPES)}")

    objects: dict = {
        "shape": [{"properties": {"tileShape": _expr_literal(tile)}}],
    }
    if fill is not None:
        objects["fill"] = [{
            "properties": {"fillColor": encode_property("fillColor", fill)},
            "selector": {"id": "default"},
        }]
    else:
        objects["fill"] = [{
            "properties": {"show": _expr_literal(False)},
            "selector": {"id": "default"},
        }]
    if outline is not None:
        props = {"lineColor": encode_property("lineColor", outline),
                 "show": _expr_literal(True)}
        if outline_weight is not None:
            props["weight"] = _expr_literal(float(outline_weight))
        objects["outline"] = [{"properties": props}]
    else:
        objects["outline"] = [{"properties": {"show": _expr_literal(False)}}]

    visual = {"visualType": "shape", "objects": objects}
    return _container(visual_id, visual, position)
