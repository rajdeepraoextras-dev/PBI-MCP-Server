"""Per-visual-type bucket + format maps — the crux of the report layer.

`queryState` bucket names differ per visual type; binding a field into the
wrong bucket silently breaks the visual. This module is the single source of
truth mapping visual type -> its buckets (Part C.3).

Bucket names below were SURVEYED FROM REAL DESKTOP EXPORTS (4 projects, 150+
visuals) — which corrected the initial plan's guesses:
  * "Legend" in the UI is the "Series" bucket in queryState
  * combo charts use "Y" (columns) + "Y2" (line) — not ColumnY/LineY
  * donutChart has NO Category bucket (Series slices it)
  * the new card visual is "cardVisual" and uses "Data" (classic card: "Values")
Unverified-by-survey types (scatterChart, gauge) follow the PBIR schema and
are marked below; verify on first real use.
"""

from __future__ import annotations

VISUAL_SPECS: dict[str, dict[str, list[str]]] = {
    # --- verified against real exports -------------------------------------
    "card":                    {"required": ["Values"], "optional": []},
    "cardVisual":              {"required": ["Data"], "optional": []},   # new card
    "multiRowCard":            {"required": ["Values"], "optional": []},
    "tableEx":                 {"required": ["Values"], "optional": []},
    "pivotTable":              {"required": ["Rows", "Values"], "optional": ["Columns"]},
    "slicer":                  {"required": ["Values"], "optional": []},
    "advancedSlicerVisual":    {"required": ["Values"], "optional": []},
    "barChart":                {"required": ["Category", "Y"], "optional": ["Series", "Tooltips"]},  # stacked
    "columnChart":             {"required": ["Category", "Y"], "optional": ["Series", "Tooltips"]},  # stacked
    "clusteredBarChart":       {"required": ["Category", "Y"], "optional": ["Series", "Tooltips"]},
    "clusteredColumnChart":    {"required": ["Category", "Y"], "optional": ["Series", "Tooltips"]},
    "lineChart":               {"required": ["Category", "Y"], "optional": ["Series", "Tooltips"]},
    "stackedAreaChart":        {"required": ["Category", "Y"], "optional": ["Series", "Tooltips"]},
    "areaChart":               {"required": ["Category", "Y"], "optional": ["Series", "Tooltips"]},
    "waterfallChart":          {"required": ["Category", "Y"], "optional": ["Tooltips"]},
    "pieChart":                {"required": ["Y"], "optional": ["Category", "Series", "Tooltips"]},
    "donutChart":              {"required": ["Y"], "optional": ["Series", "Tooltips"]},  # no Category!
    "lineClusteredColumnComboChart": {"required": ["Category", "Y"], "optional": ["Y2", "Series"]},
    "lineStackedColumnComboChart":   {"required": ["Category", "Y"], "optional": ["Y2", "Series"]},
    "filledMap":               {"required": ["Category"], "optional": ["Tooltips", "Y"]},
    # --- from PBIR schema, not yet seen in a local export -------------------
    "scatterChart":            {"required": ["Details"], "optional": ["X", "Y", "Size", "Series"]},
    "gauge":                   {"required": ["Y"], "optional": ["MinValue", "MaxValue", "TargetValue"]},
}


def buckets_for(visual_type: str) -> dict[str, list[str]]:
    """Return {"required": [...], "optional": [...]} for a visual type."""
    if visual_type not in VISUAL_SPECS:
        raise KeyError(
            f"No spec for visual type {visual_type!r}. Known: "
            f"{sorted(VISUAL_SPECS)}"
        )
    return VISUAL_SPECS[visual_type]


def validate_bindings(visual_type: str, bindings: dict[str, list[str]]) -> None:
    """Raise ValueError if `bindings` don't fit the visual type's buckets.

    Checks: every required bucket present and non-empty; no unknown buckets.
    """
    spec = buckets_for(visual_type)
    allowed = set(spec["required"]) | set(spec["optional"])
    missing = [b for b in spec["required"] if not bindings.get(b)]
    unknown = [b for b in bindings if b not in allowed]
    problems = []
    if missing:
        problems.append(f"missing required bucket(s) {missing}")
    if unknown:
        problems.append(
            f"unknown bucket(s) {unknown} — allowed for {visual_type!r}: "
            f"{sorted(allowed)}"
        )
    if problems:
        raise ValueError(f"Invalid bindings for {visual_type!r}: "
                         + "; ".join(problems))
