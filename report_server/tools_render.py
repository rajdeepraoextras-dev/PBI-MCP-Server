"""Report-server tools: page rendering and theme-from-image.

``pbi_render_page`` / ``pbi_render_report`` draw a wireframe-with-content of
the page(s) (SVG needs nothing; PNG needs Pillow: ``pip install
pbi-mcp[render]``). ``pbi_theme_from_image`` builds a report theme from the
colors of a logo or photo. Logic lives in core/render.py and
core/theme_image.py; this module is the thin tool layer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:  # pragma: no cover - typing only (the server loads this module)
    from report_server.server import ReportState


# --- tool logic ---------------------------------------------------------------------------

def render_page(state: ReportState, page_id: str, format: str = "svg",
                out_path: str | None = None, scale: float = 1.0,
                inline: bool = True) -> dict:
    """Render one page to SVG/PNG under .pbi/mcp-renders (or `out_path`)."""
    from core import render

    return render.render_page(state.require(), page_id, format, out_path, scale, inline)


def render_report(state: ReportState, format: str = "svg",
                  out_dir: str | None = None) -> list[dict]:
    """Render every page; one result per page."""
    from core import render

    return render.render_report(state.require(), format, out_dir)


def theme_from_image(state: ReportState, image_path: str, name: str | None = None,
                     mode: str = "light", install: bool = False) -> dict:
    """Extract a palette from an image and build (optionally install) a theme."""
    from core import theme_image

    project = state.require()
    result = theme_image.theme_from_image(image_path, name, mode)
    result["installed"] = False
    if install:
        result.update(project.set_report_theme(result["theme"]))
        result["installed"] = True
    return result


# --- MCP registration ---------------------------------------------------------------------------

def register(mcp, state, tool) -> None:
    # These write a derived image file (default: <Report>/.pbi/mcp-renders, which
    # Desktop ignores and the undo journal skips), not project content, so they
    # are annotated as writes but are not journaled / dry-run capable.
    @tool(write=True, idempotent=True, journaled=False)
    def pbi_render_page(page_id: str, format: Literal["svg", "png"] = "svg",
                        out_path: str | None = None, scale: float = 1.0,
                        inline: bool = True) -> dict:
        """Draw a page as a wireframe-with-content picture: the page canvas at
        its real size with its background, and every visual as a rounded box at
        its position (z-order kept, hidden visuals dashed, groups outlined and
        named) showing its title, visual type, a bindings summary
        ("Category: Date.Year | Y: Sales.Net Revenue") and a schematic glyph
        (bars, line, pie, big number, grid, list, ...), colored from the
        report's theme and accent. format is 'svg' (default) or 'png' (needs
        Pillow: pip install pbi-mcp[render]); scale multiplies the size (0-8].
        The file goes to out_path (a file ending in .svg/.png, or a directory)
        or, by default, <Report>/.pbi/mcp-renders/<page_id>.<ext>. Returns the
        path, size, bytes, a per-visual list (id, type, title, geometry) and,
        for SVG when inline is true and the file is under 200 KB, the SVG text
        so hosts can show it."""
        return render_page(state, page_id, format, out_path, scale, inline)

    @tool(write=True, idempotent=True, journaled=False)
    def pbi_render_report(format: Literal["svg", "png"] = "svg",
                          out_dir: str | None = None) -> list[dict]:
        """Render every page of the report with the same wireframe drawing as
        pbi_render_page, one file per page (<page_id>.svg or .png) in out_dir or,
        by default, <Report>/.pbi/mcp-renders/. PNG needs Pillow (pip install
        pbi-mcp[render]). Returns one entry per page: page_id, page_name, path,
        width, height, bytes, visual_count (no inline SVG; open the files)."""
        return render_report(state, format, out_dir)

    @tool(write=True, destructive=True, idempotent=True)
    def pbi_theme_from_image(image_path: str, name: str | None = None,
                             mode: Literal["light", "dark"] = "light",
                             install: bool = False) -> dict:
        """Build a Power BI theme from the colors of an image (logo, photo,
        brand artwork; needs Pillow: pip install pbi-mcp[render]). The image is
        downscaled and quantized to ~16 colors, near-white / near-black / gray
        swatches are dropped, the rest are ordered by saturation then luminance,
        the most saturated mid-tone becomes the accent and 6-8 distinct swatches
        become the theme's dataColors; the theme itself comes from the same
        generator as pbi_generate_theme (mode 'light' or 'dark'). Returns the
        theme JSON, the accent, the dataColors and the extracted swatches. Nothing
        is written unless install=true, which installs and activates the theme in
        the report (replacing the current custom theme); name defaults to
        '<image name> Theme'."""
        return theme_from_image(state, image_path, name, mode, install)
