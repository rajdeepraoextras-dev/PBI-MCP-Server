"""Theme from image -- extract a palette from a logo/photo and build a theme.

Pillow (optional: ``pip install pbi-mcp[render]``) downscales and quantizes
the image to ~16 colors. Near-identical swatches (anti-aliased edges) are
merged into the dominant one, near-white, near-black, low-saturation and
negligible swatches are dropped, and the rest are ordered by saturation then
luminance. The accent is the most saturated mid-luminance swatch; 6-8
distinct swatches become the categorical ``dataColors`` (padded from the
accent's hue-rotated palette when the image is too monochrome to supply six).

The theme itself comes from :func:`core.theme.generate_theme` -- this module
only chooses the brand color and overrides ``dataColors`` afterwards, so the
text classes, structural colors and visual styles stay identical to
``pbi_generate_theme``.
"""

from __future__ import annotations

import colorsys
import re
from pathlib import Path

from core.render import require_pillow
from core.theme import categorical_palette, generate_theme

QUANTIZE_COLORS = 16
THUMBNAIL = (160, 160)

# swatch filters (HLS space, all 0..1)
MIN_SATURATION = 0.20     # below: gray-ish
MIN_LUMINANCE = 0.14      # below: near-black
MAX_LUMINANCE = 0.92      # above: near-white
ACCENT_LUMINANCE = (0.25, 0.70)
MIN_SHARE = 0.01          # a color must cover >= 1% of the pixels to count
MERGE_DISTANCE = 16.0     # RGB distance under which two swatches are one color
MIN_DATA_COLORS = 6
MAX_DATA_COLORS = 8


def safe_theme_name(name: str, fallback: str = "Image Theme") -> str:
    """A theme name that is safe as a file name (it becomes ``<name>.json``)."""
    cleaned = re.sub(r"[^\w .()\-]+", "_", str(name)).strip(" ._")
    return cleaned[:60] or fallback


def _hex(r: int, g: int, b: int) -> str:
    return f"#{r:02X}{g:02X}{b:02X}"


def _rgb(hex_color: str) -> tuple[int, int, int]:
    return tuple(int(hex_color[i:i + 2], 16) for i in (1, 3, 5))  # type: ignore[return-value]


def merge_similar(swatches: list[dict], distance: float = MERGE_DISTANCE) -> list[dict]:
    """Fold swatches closer than `distance` (RGB) into the largest of them.

    Downscaling and quantizing an anti-aliased logo leaves several slightly
    different versions of each real color; the dominant one is kept (its exact
    hex) and the shares are summed.
    """
    clusters: list[dict] = []
    for sw in sorted(swatches, key=lambda s: -s["share"]):
        rgb = _rgb(sw["hex"])
        for c in clusters:
            crgb = _rgb(c["hex"])
            if sum((a - b) ** 2 for a, b in zip(rgb, crgb)) ** 0.5 < distance:
                c["share"] += sw["share"]
                break
        else:
            clusters.append(dict(sw))
    return clusters


def extract_swatches(image_path: str | Path, colors: int = QUANTIZE_COLORS) -> list[dict]:
    """Merged swatches of an image: [{hex, share, hue, saturation, luminance}],
    largest share first.

    Transparent pixels are composited onto white first so a logo with a
    transparent background contributes its real colors, not black.
    """
    Image = require_pillow()[0]
    path = Path(image_path)
    if not path.exists():
        raise FileNotFoundError(f"Image not found: {image_path}")
    with Image.open(path) as im:
        img = im.convert("RGBA")
    white = Image.new("RGBA", img.size, (255, 255, 255, 255))
    img = Image.alpha_composite(white, img).convert("RGB")
    img.thumbnail(THUMBNAIL)
    q = img.quantize(colors=colors)
    counts = q.getcolors(maxcolors=colors * 4) or []
    palette = q.getpalette() or []
    total = sum(c for c, _ in counts) or 1
    raw = []
    for count, idx in counts:
        r, g, b = palette[idx * 3: idx * 3 + 3]
        h, l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
        raw.append({"hex": _hex(r, g, b), "share": count / total,
                    "hue": h, "saturation": s, "luminance": l})
    merged = merge_similar(raw)
    merged.sort(key=lambda sw: -sw["share"])
    return merged


def _usable(swatches: list[dict], min_sat: float, min_share: float) -> list[dict]:
    return [sw for sw in swatches
            if sw["saturation"] >= min_sat
            and MIN_LUMINANCE <= sw["luminance"] <= MAX_LUMINANCE
            and sw["share"] >= min_share]


def _distinct(a: dict, b: dict) -> bool:
    dh = abs(a["hue"] - b["hue"])
    dh = min(dh, 1 - dh)
    return dh > 0.03 or abs(a["luminance"] - b["luminance"]) > 0.15 \
        or abs(a["saturation"] - b["saturation"]) > 0.25


def choose_palette(swatches: list[dict]) -> tuple[str, list[str], list[dict]]:
    """(accent, data_colors, ordered candidate swatches) from merged swatches."""
    candidates: list[dict] = []
    for min_sat, min_share in ((MIN_SATURATION, MIN_SHARE), (MIN_SATURATION, 0.001),
                               (0.05, 0.001)):
        candidates = _usable(swatches, min_sat, min_share)
        if candidates:
            break
    if not candidates:                      # black-and-white artwork
        candidates = [sw for sw in swatches if sw["luminance"] <= MAX_LUMINANCE]
    if not candidates:
        raise ValueError("The image has no usable colors (all near-white); "
                         "pick a brand color and use pbi_generate_theme.")
    # saturation first, then luminance (darker before lighter)
    candidates.sort(key=lambda sw: (-sw["saturation"], sw["luminance"]))

    lo, hi = ACCENT_LUMINANCE
    mid = [sw for sw in candidates if lo <= sw["luminance"] <= hi]
    accent_sw = max(mid or candidates,
                    key=lambda sw: (round(sw["saturation"], 1), sw["share"]))
    accent = accent_sw["hex"]

    chosen: list[dict] = [accent_sw]
    for sw in candidates:
        if len(chosen) >= MAX_DATA_COLORS:
            break
        if sw["hex"] != accent_sw["hex"] and all(_distinct(sw, c) for c in chosen):
            chosen.append(sw)
    data_colors = [sw["hex"] for sw in chosen]
    if len(data_colors) < MIN_DATA_COLORS:
        taken = {d.upper() for d in data_colors}
        for c in categorical_palette(accent, MAX_DATA_COLORS):
            if c.upper() not in taken:
                data_colors.append(c)
                taken.add(c.upper())
            if len(data_colors) >= MIN_DATA_COLORS:
                break
    return accent, data_colors[:MAX_DATA_COLORS], candidates


def theme_from_image(image_path: str | Path, name: str | None = None,
                     mode: str = "light") -> dict:
    """Build a Power BI theme whose accent + dataColors come from an image.

    Returns {theme, accent, data_colors, swatches, image}. ``theme`` is the
    standard theme JSON from core.theme.generate_theme with ``dataColors``
    replaced by the extracted palette.
    """
    if mode not in ("light", "dark"):
        raise ValueError("mode must be 'light' or 'dark'")
    path = Path(image_path)
    swatches = extract_swatches(path)
    accent, data_colors, ordered = choose_palette(swatches)
    theme = generate_theme(accent, safe_theme_name(name or f"{path.stem} Theme"), mode)
    theme["dataColors"] = data_colors
    theme["maximum"] = data_colors[0]
    return {
        "theme": theme,
        "accent": accent,
        "data_colors": data_colors,
        "swatches": [
            {"hex": sw["hex"], "share": round(sw["share"], 3),
             "saturation": round(sw["saturation"], 2),
             "luminance": round(sw["luminance"], 2)}
            for sw in ordered
        ],
        "image": str(path),
    }
