"""PBIR read/write — the REPORT layer.

Reads `*.Report/definition/`:
  report.json
  pages/pages.json                     # page order + active page
  pages/{pageId}/page.json             # displayName, size, visibility
  pages/{pageId}/visuals/{visualId}/visual.json

`visual.json` anatomy this reader relies on (Part A.4):
  {
    "name": "<visualId>",
    "position": { "x", "y", "z", "width", "height" },
    "visual": {
      "visualType": "...",
      "query": { "queryState": { "<bucket>": { "projections": [ {field, queryRef} ] } } },
      "objects": {...},                 # visual-content formatting
      "visualContainerObjects": {...}   # chrome: title, background, border
    }
  }

The whole dict is kept on `Visual.raw` so a round-trip never drops keys we
don't model. Bucket names differ per visual type — see `visual_specs.py`.
Write builders (build_visual_json, build_page_json, …) live below; PbipProject
persists them.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable

from core.schemas import Page, Position, Visual

_HIDDEN_VISIBILITIES = {"HiddenInViewMode", "Hidden"}

_VC_SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.10.0/schema.json"
_PAGE_SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/page/2.1.0/schema.json"
_PAGES_SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/pagesMetadata/1.1.0/schema.json"


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _pages_dir(report_def_dir: str | Path) -> Path:
    return Path(report_def_dir) / "pages"


def _page_ids_in_order(report_def_dir: str | Path) -> list[str]:
    """Return page ids honouring pages.json order; fall back to folder scan."""
    pages_dir = _pages_dir(report_def_dir)
    meta = pages_dir / "pages.json"
    on_disk = {p.name for p in pages_dir.iterdir() if p.is_dir()} if pages_dir.is_dir() else set()
    if meta.exists():
        order = _load_json(meta).get("pageOrder", [])
        ordered = [pid for pid in order if pid in on_disk]
        # include any folders missing from pageOrder, deterministically
        ordered += sorted(on_disk - set(ordered))
        return ordered
    return sorted(on_disk)


def read_pages(report_def_dir: str | Path) -> list[Page]:
    """Read page list + order from pages/pages.json and each page.json."""
    pages_dir = _pages_dir(report_def_dir)
    pages: list[Page] = []
    for pid in _page_ids_in_order(report_def_dir):
        page_json = pages_dir / pid / "page.json"
        data = _load_json(page_json) if page_json.exists() else {}
        visuals_dir = pages_dir / pid / "visuals"
        visual_count = (
            sum(1 for d in visuals_dir.iterdir() if (d / "visual.json").exists())
            if visuals_dir.is_dir() else 0
        )
        pages.append(Page(
            id=pid,
            name=data.get("displayName") or data.get("name") or pid,
            width=data.get("width"),
            height=data.get("height"),
            visual_count=visual_count,
            is_hidden=data.get("visibility") in _HIDDEN_VISIBILITIES,
        ))
    return pages


def read_visuals(report_def_dir: str | Path, page_id: str) -> list[Visual]:
    """Read all visual.json files for a page."""
    visuals_dir = _pages_dir(report_def_dir) / page_id / "visuals"
    if not visuals_dir.is_dir():
        return []
    visuals: list[Visual] = []
    for vfile in sorted(visuals_dir.glob("*/visual.json")):
        visuals.append(_parse_visual(_load_json(vfile), page_id, vfile.parent.name))
    return visuals


def read_visual(report_def_dir: str | Path, page_id: str, visual_id: str) -> Visual:
    """Read a single visual by id."""
    vfile = _pages_dir(report_def_dir) / page_id / "visuals" / visual_id / "visual.json"
    if not vfile.exists():
        raise FileNotFoundError(f"No visual {visual_id!r} on page {page_id!r}")
    return _parse_visual(_load_json(vfile), page_id, visual_id)


def _parse_visual(data: dict, page_id: str, folder_id: str) -> Visual:
    visual = data.get("visual", {})
    pos = data.get("position", {})
    return Visual(
        id=data.get("name") or folder_id,
        page_id=page_id,
        visual_type=visual.get("visualType"),
        title=_extract_title(visual),
        position=Position(
            x=pos.get("x", 0), y=pos.get("y", 0), z=pos.get("z", 0),
            width=pos.get("width", 0), height=pos.get("height", 0),
        ),
        raw=data,
    )


def _extract_title(visual: dict) -> str | None:
    """Best-effort static title from visualContainerObjects.title[].text literal."""
    try:
        title_obj = visual["visualContainerObjects"]["title"][0]
        raw = title_obj["properties"]["text"]["expr"]["Literal"]["Value"]
    except (KeyError, IndexError, TypeError):
        return None
    raw = raw.strip()
    if len(raw) >= 2 and raw.startswith("'") and raw.endswith("'"):
        return raw[1:-1].replace("''", "'")
    return raw


# --- write builders (pure; PbipProject persists them) ----------------------

def slugify(name: str, *, fallback: str = "item") -> str:
    """Lowercase, hyphenate a display name into a filesystem-safe id stem."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or fallback


_AGG_RE = re.compile(r"^(Sum|Average|Avg|Count|DistinctCount|Min|Max|Median)"
                     r"\(([^)]+)\)$", re.IGNORECASE)


def _field_ref(query_ref: str, is_measure: Callable[[str, str], bool]) -> dict:
    """Build a queryState projection for `Entity.Property`.

    Picks Measure vs Column so Power BI binds the field to the right role.
    Also accepts an aggregation form `Sum(Table.Column)` / `Average(...)`.
    """
    agg = _AGG_RE.match(query_ref.strip())
    if agg:
        from core.interact import build_aggregation_projection

        func, inner = agg.group(1), agg.group(2)
        return build_aggregation_projection(inner, func)

    entity, _, prop = query_ref.partition(".")
    kind = "Measure" if is_measure(entity, prop) else "Column"
    return {
        "field": {
            kind: {
                "Expression": {"SourceRef": {"Entity": entity}},
                "Property": prop,
            }
        },
        "queryRef": query_ref,
    }


def build_query_state(bindings: dict[str, list[str]],
                      is_measure: Callable[[str, str], bool]) -> dict:
    """Turn {bucket: [queryRef, ...]} into a PBIR queryState dict."""
    state: dict = {}
    for bucket, refs in bindings.items():
        state[bucket] = {
            "projections": [_field_ref(r, is_measure) for r in refs]
        }
    return state


def build_visual_json(visual_id: str, visual_type: str,
                      bindings: dict[str, list[str]],
                      is_measure: Callable[[str, str], bool],
                      position: dict | None = None,
                      title: str | None = None) -> dict:
    """Assemble a complete visual.json dict."""
    pos = {"x": 0, "y": 0, "z": 0, "width": 320, "height": 240, "tabOrder": 0}
    if position:
        pos.update(position)
    visual: dict = {
        "visualType": visual_type,
        "query": {"queryState": build_query_state(bindings, is_measure)},
    }
    if title is not None:
        visual["visualContainerObjects"] = {
            "title": [
                {"properties": {"text": {"expr": {"Literal": {"Value": f"'{title}'"}}}}}
            ]
        }
    return {"$schema": _VC_SCHEMA, "name": visual_id, "position": pos, "visual": visual}


def build_page_json(page_id: str, name: str,
                    width: float, height: float) -> dict:
    return {
        "$schema": _PAGE_SCHEMA,
        "name": page_id,
        "displayName": name,
        "displayOption": "FitToPage",
        "height": height,
        "width": width,
        "visibility": "AlwaysVisible",
    }


def visual_bindings(visual: Visual) -> dict[str, list[str]]:
    """Map bucket name -> list of queryRefs bound to it (Day 15 uses this)."""
    out: dict[str, list[str]] = {}
    query_state = (
        visual.raw.get("visual", {}).get("query", {}).get("queryState", {})
    )
    for bucket, spec in query_state.items():
        refs = []
        for proj in spec.get("projections", []):
            ref = proj.get("queryRef")
            if ref:
                refs.append(ref)
        out[bucket] = refs
    return out
