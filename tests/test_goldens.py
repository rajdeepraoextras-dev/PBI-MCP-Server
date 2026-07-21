"""Day 25: golden-file snapshots of emitted visual.json per visual type.

Each golden is the exact dict `build_visual_json` produces for a fixed input.
If an emitter change alters the output, the diff shows up here first — the
guard against silently breaking the on-disk format.

Regenerate intentionally with:
    .venv/Scripts/python.exe -m pytest tests/test_goldens.py --force-regen
(handled below via the REGEN env var to avoid a plugin dependency):
    set PBI_MCP_REGEN_GOLDENS=1 && pytest tests/test_goldens.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from core.pbir import build_visual_json

GOLDEN_DIR = Path(__file__).parent / "goldens"
GOLDEN_DIR.mkdir(exist_ok=True)

# fixed fake model: measures are (Sales, Net Revenue) / (Sales, Margin %)
_MEASURES = {("Sales", "Net Revenue"), ("Sales", "Margin %")}


def _is_measure(entity: str, prop: str) -> bool:
    return (entity, prop) in _MEASURES


CASES: dict[str, dict] = {
    "card": {
        "bindings": {"Values": ["Sales.Net Revenue"]},
        "title": "Revenue",
    },
    "tableEx": {
        "bindings": {"Values": ["Date.Year", "Sales.Net Revenue"]},
    },
    "clusteredBarChart": {
        "bindings": {"Category": ["Date.Year"], "Y": ["Sales.Net Revenue"]},
        "title": "By Year",
    },
    "lineChart": {
        "bindings": {"Category": ["Date.Year"], "Y": ["Sales.Net Revenue"],
                     "Series": ["Store.Region"]},
    },
    "slicer": {
        "bindings": {"Values": ["Date.Year"]},
    },
    "lineClusteredColumnComboChart": {
        "bindings": {"Category": ["Date.Year"], "Y": ["Sales.Net Revenue"],
                     "Y2": ["Sales.Margin %"]},
    },
    "scatterChart": {
        "bindings": {"Details": ["Date.Year"], "X": ["Sales.Net Revenue"],
                     "Y": ["Sales.Margin %"]},
    },
    "donutChart": {
        "bindings": {"Y": ["Sales.Net Revenue"], "Series": ["Date.Year"]},
    },
}

REGEN = os.environ.get("PBI_MCP_REGEN_GOLDENS") == "1"


@pytest.mark.parametrize("visual_type", sorted(CASES))
def test_golden(visual_type):
    case = CASES[visual_type]
    emitted = build_visual_json(
        f"golden-{visual_type.lower()}", visual_type,
        case["bindings"], _is_measure,
        position={"x": 10, "y": 20, "width": 300, "height": 200},
        title=case.get("title"),
    )
    golden_file = GOLDEN_DIR / f"{visual_type}.json"
    if REGEN or not golden_file.exists():
        golden_file.write_text(json.dumps(emitted, indent=2) + "\n",
                               encoding="utf-8", newline="\n")
    golden = json.loads(golden_file.read_text(encoding="utf-8"))
    assert emitted == golden, (
        f"{visual_type} output changed vs golden. If intentional, "
        f"set PBI_MCP_REGEN_GOLDENS=1 and rerun.")
