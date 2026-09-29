"""Report-server tools: Deneb (Vega / Vega-Lite) custom visuals.

Template catalogue, add-a-template-visual and set-an-arbitrary-spec. Logic
lives in core/deneb.py (which also documents where the Deneb identifiers come
from); this module is the thin tool layer.

Every visual built here needs the Deneb custom visual to be present in the
report or organization to render.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core import deneb  # registers the Deneb visual type with the generic binding tools

if TYPE_CHECKING:  # pragma: no cover - typing only (the server loads this module)
    from report_server.server import ReportState


# --- tool logic ---------------------------------------------------------------------------

def list_deneb_templates(state: ReportState) -> list[dict]:
    """The Deneb template catalogue (needs no project)."""
    return deneb.list_templates()


def add_deneb_visual(state: ReportState, page_id: str, template: str, bindings: dict,
                     position: dict | None = None, title: str | None = None,
                     options: dict | None = None) -> dict:
    """Add a template-built Deneb visual bound to model fields."""
    return deneb.add_deneb_visual(state.require(), page_id, template, bindings,
                                  position, title, options)


def set_deneb_spec(state: ReportState, page_id: str, visual_id: str, spec,
                   config=None) -> dict:
    """Replace the spec (and optionally the config) of an existing Deneb visual."""
    return deneb.set_deneb_spec(state.require(), page_id, visual_id, spec, config)


# --- MCP registration ---------------------------------------------------------------------------

def register(mcp, state, tool) -> None:
    @tool(read=True, idempotent=True)
    def pbi_list_deneb_templates() -> list[dict]:
        """List the Deneb chart templates pbi_add_deneb_visual can build: bar,
        stacked_bar, line, area, scatter, heatmap, histogram, box_plot, bullet,
        sparkline, waffle and dumbbell. Each entry gives its name, a description,
        the bindings it takes (role -> 'Table.Field', required or optional,
        dimension or measure), its options (color, palette, orientation, labels,
        format, ...) with defaults, and an example call (field names are
        illustrative; use your model's). The Deneb custom visual must be present
        in the report or organization for these visuals to render."""
        return list_deneb_templates(state)

    @tool(write=True)
    def pbi_add_deneb_visual(page_id: str, template: str, bindings: dict,
                             position: dict | None = None, title: str | None = None,
                             options: dict | None = None) -> dict:
        """Add a Deneb (Vega-Lite) chart to a page from a template (see
        pbi_list_deneb_templates). bindings maps template roles to model fields,
        e.g. {"category": "Date.Year", "value": "Sales.Net Revenue", "series":
        "Store.Region"}; measures and columns are resolved from the model, must
        exist and fit the role (dimension roles take columns, measure roles take
        measures or numeric columns; 'Sum(Table.Col)' aggregates a column). The
        fields are bound to the visual's dataset so the data flows, and the spec
        reads the dataset by field name. options tunes the template (color,
        palette, orientation, labels, format, ...). position is {x, y, width,
        height} (default: the first free 480x320 slot on the page); title sets
        the container title. The Deneb custom visual must be present in the
        report or organization to render: this lists it in report.json
        publicCustomVisuals when it is not already available. Returns the new
        visual id and the dataset field names the spec uses."""
        return add_deneb_visual(state, page_id, template, bindings, position, title,
                                options)

    @tool(write=True, destructive=True, idempotent=True)
    def pbi_set_deneb_spec(page_id: str, visual_id: str, spec: dict | str,
                           config: dict | str | None = None) -> dict:
        """Replace the Vega-Lite or Vega spec of an existing Deneb visual with an
        arbitrary one (an object, or JSON text); the provider is detected from
        $schema / the spec's keys. config (optional) replaces the Vega config
        (jsonConfig); omitted, the current config is kept. The spec must read the
        bound fields from the data named "dataset" (Vega-Lite: "data": {"name":
        "dataset"}); a warning is returned when it does not. Field names in the
        dataset are the bound fields' display names with \\ " . [ ] replaced by _.
        Bindings are not changed: to change the fields, call pbi_update_bindings
        with the single bucket "dataset", e.g. {"dataset": ["Date.Year",
        "Sales.Net Revenue"]}. The Deneb custom visual must be present in the
        report or organization to render."""
        return set_deneb_spec(state, page_id, visual_id, spec, config)
