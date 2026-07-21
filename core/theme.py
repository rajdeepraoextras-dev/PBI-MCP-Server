"""Theme generator — brand color -> a full, coherent Power BI theme (E3 D12).

Produces a standard Power BI theme JSON: a categorical dataColors palette
derived by hue rotation from the brand color, sequential tints/shades for
backgrounds and structure, and text classes. Light and dark modes.

Colour math is HSL, stdlib only. The output is the documented PBI theme
shape (name + dataColors + structural colors + textClasses + visualStyles),
which Desktop imports without a schema (themes aren't schema-validated by us).
"""

from __future__ import annotations

import colorsys


def _hex_to_rgb(h: str) -> tuple[float, float, float]:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore


def _rgb_to_hex(rgb) -> str:
    return "#" + "".join(f"{max(0, min(255, round(c * 255))):02X}" for c in rgb)


def _hls(h: str):
    r, g, b = _hex_to_rgb(h)
    return colorsys.rgb_to_hls(r, g, b)


def _from_hls(hue, light, sat) -> str:
    return _rgb_to_hex(colorsys.hls_to_rgb(hue % 1.0, max(0, min(1, light)),
                                           max(0, min(1, sat))))


def categorical_palette(brand: str, n: int = 8) -> list[str]:
    """A harmonious categorical palette anchored on the brand color.

    Rotates hue in golden-angle-ish steps and alternates lightness so adjacent
    series stay distinguishable — a design-system-agnostic default.
    """
    h, l, s = _hls(brand)
    sat = max(s, 0.45)
    colors = [brand]
    # analogous + complementary spread, then fill by hue rotation
    offsets = [0.52, 0.30, 0.72, 0.14, 0.86, 0.42, 0.62, 0.22, 0.78]
    lights = [l, min(l + 0.08, 0.62), max(l - 0.08, 0.32)]
    i = 0
    while len(colors) < n:
        hue = (h + offsets[i % len(offsets)]) % 1.0
        light = lights[i % len(lights)]
        colors.append(_from_hls(hue, light, sat))
        i += 1
    return colors[:n]


def generate_theme(brand: str = "#1F3A5F", name: str = "MCP Brand Theme",
                   mode: str = "light") -> dict:
    """Full Power BI theme JSON from a brand color."""
    if mode not in ("light", "dark"):
        raise ValueError("mode must be 'light' or 'dark'")
    h, l, s = _hls(brand)
    data_colors = categorical_palette(brand, 8)

    if mode == "light":
        bg = "#FFFFFF"
        bg_alt = _from_hls(h, 0.97, min(s, 0.25))     # faint tinted panel
        fg = "#252423"
        fg_muted = "#605E5C"
        accent = brand
    else:
        bg = _from_hls(h, 0.10, min(s, 0.35))
        bg_alt = _from_hls(h, 0.15, min(s, 0.35))
        fg = "#F3F2F1"
        fg_muted = "#C8C6C4"
        accent = _from_hls(h, 0.65, max(s, 0.5))

    def text_class(size, color, weight="Normal"):
        return {"fontSize": size, "color": color,
                "fontFace": "Segoe UI", "fontWeight": weight}

    return {
        "name": name,
        "dataColors": data_colors,
        "background": bg,
        "secondaryBackground": bg_alt,
        "foreground": fg,
        "foregroundNeutralSecondary": fg_muted,
        "tableAccent": accent,
        "good": "#107C10", "neutral": "#F2A104", "bad": "#D13438",
        "maximum": data_colors[0], "minimum": bg_alt,
        "textClasses": {
            "title": text_class(14, fg, "Bold"),
            "header": text_class(12, fg, "Semibold"),
            "label": text_class(10, fg_muted),
            "callout": text_class(28, accent, "Bold"),
        },
        "visualStyles": {
            "*": {
                "*": {
                    "background": [{"show": True, "color": {"solid": {"color": bg}}}],
                    "border": [{"show": False}],
                    "title": [{"show": True, "fontColor": {"solid": {"color": fg}},
                               "fontSize": 12, "fontFamily": "Segoe UI Semibold",
                               "alignment": "left"}],
                }
            },
        },
    }
