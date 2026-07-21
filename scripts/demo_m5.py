"""Day 30 / M5 demo: build a themed, filtered, formatted report page-set
through the MCP tool layer, on a copy of the real HR project.

Output: DAY30-DESKTOP-REOPEN-TEST/ at the workspace root. Open the .pbip in
Power BI Desktop; expect two new pages ("Executive Overview", "Workforce
Detail"), a custom theme, and working filters — no repair prompt.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from core.formatting import build_filter
from report_server.server import (
    ReportState, add_visual, build_page, set_project,
)

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "tests" / "fixtures" / "real" / "hr-sample"
OUT = REPO.parent / "DAY30-DESKTOP-REOPEN-TEST"

ACCENT = "#1F3A5F"


def _clear_out(path: Path, attempts: int = 4) -> None:
    """Remove the previous output. OneDrive sync locks fresh dirs, so:
    chmod-and-retry with backoff, then fall back to renaming it aside."""
    import os
    import stat
    import time

    def onexc(func, p, exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            raise exc

    for i in range(attempts):
        try:
            shutil.rmtree(path, onexc=onexc)
            return
        except PermissionError:
            time.sleep(1.0 * (i + 1))
    aside = path.with_name(f"{path.name}.old-{time.strftime('%H%M%S')}")
    os.rename(path, aside)
    print(f"note: previous output was locked by OneDrive; moved to {aside.name}")


def main() -> None:
    if OUT.exists():
        _clear_out(OUT)
    shutil.copytree(SRC, OUT)

    st = ReportState()
    set_project(st, str(next(OUT.glob("*.pbip"))))
    p = st.project

    # ---- theme ------------------------------------------------------------
    p.set_report_theme({
        "name": "MCP Demo Theme",
        "dataColors": [ACCENT, "#5B8DB8", "#8FBCD4", "#C7DCEA",
                       "#F2A104", "#D95D39"],
        "background": "#FFFFFF",
        "foreground": "#252423",
        "tableAccent": ACCENT,
    })

    # ---- page 1: Executive Overview (10 visuals, one call) ------------------
    page1 = build_page(st, "Executive Overview", [
        {"visual_type": "card", "title": "Actives",
         "bindings": {"Values": ["Employee.Actives"]}},
        {"visual_type": "card", "title": "New Hires",
         "bindings": {"Values": ["Employee.New Hires"]}},
        {"visual_type": "card", "title": "Separations",
         "bindings": {"Values": ["Employee.Seps"]}},
        {"visual_type": "card", "title": "Turnover %",
         "bindings": {"Values": ["Employee.TO %"]}},
        {"visual_type": "clusteredBarChart", "title": "Actives by Region",
         "bindings": {"Category": ["BU.Region"],
                      "Y": ["Employee.Actives"]}},
        {"visual_type": "columnChart", "title": "New Hires by Month",
         "bindings": {"Category": ["Date.Month"],
                      "Y": ["Employee.New Hires"]}},
        {"visual_type": "lineChart", "title": "Actives Trend",
         "bindings": {"Category": ["Date.Period"],
                      "Y": ["Employee.Actives"]}},
        {"visual_type": "donutChart", "title": "Separations by Group",
         "bindings": {"Y": ["Employee.Seps"],
                      "Series": ["AgeGroup.AgeGroup"]}},
        {"visual_type": "tableEx", "title": "Region Detail",
         "bindings": {"Values": ["BU.Region", "Employee.Actives",
                                 "Employee.New Hires", "Employee.Seps"]}},
        {"visual_type": "slicer",
         "bindings": {"Values": ["Date.Year"]}},
    ], height=1100)

    # style the KPI cards + charts
    visuals = p.list_visuals(page1["page_id"])
    for v in visuals:
        if v.visual_type == "card":
            p.format_visual(page1["page_id"], v.id, "container", {
                "title": {"fontColor": "#FFFFFF", "background": ACCENT,
                          "fontSize": 12},
                "background": {"color": "#F5F7FA"},
                "border": {"show": True, "color": "#D0D7E2"},
            })
        elif v.visual_type in ("clusteredBarChart", "columnChart",
                               "lineChart"):
            p.format_visual(page1["page_id"], v.id, "visual", {
                "labels": {"show": True, "fontSize": 9},
                "legend": {"show": False},
            })

    # ---- page 2: Workforce Detail + filters ---------------------------------
    page2 = build_page(st, "Workforce Detail", [
        {"visual_type": "clusteredBarChart", "title": "Top Regions by Actives",
         "bindings": {"Category": ["BU.Region"], "Y": ["Employee.Actives"]}},
        {"visual_type": "tableEx", "title": "Full Detail",
         "bindings": {"Values": ["BU.Region", "BU.BU",
                                 "Employee.Actives", "Employee.TO %"]}},
    ])

    # report-scope categorical filter + visual-scope TopN
    p.add_filter("report", build_filter(
        "Employee.FP", filter_type="Categorical", values=["F"]))
    bar_id = next(v.id for v in p.list_visuals(page2["page_id"])
                  if v.visual_type == "clusteredBarChart")
    p.add_filter("visual", build_filter(
        "BU.Region", filter_type="TopN", top_n=5,
        order_by="Employee.Actives"),
        page_id=page2["page_id"], visual_id=bar_id)

    print("M5 demo built.")
    print("  pages:", page1["page_id"], "+", page2["page_id"])
    print("  OPEN THIS ->", next(OUT.glob("*.pbip")))


if __name__ == "__main__":
    main()
