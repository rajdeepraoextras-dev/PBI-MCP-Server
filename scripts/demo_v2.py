"""v2 demo: one call turns the raw HR model into an epic, designed, multi-page
report — theme, KPI strips on backplates, header bands, nav bar, filters —
then validates it against the Fabric schemas.

Output: DAYV2-DESKTOP-REOPEN-TEST/ at the workspace root. Open the .pbip in
Power BI Desktop; expect a themed Overview + per-dimension detail pages, all
navigable, with no repair prompt.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from core.formatting import build_filter
from report_server.server import ReportState, scaffold_report, set_project

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "tests" / "fixtures" / "real" / "hr-sample"
OUT = REPO.parent / "DAYV2-DESKTOP-REOPEN-TEST"


def _clear(path: Path):
    import os
    import stat
    import time

    def onexc(func, p, exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            raise exc
    for i in range(4):
        try:
            shutil.rmtree(path, onexc=onexc)
            return
        except PermissionError:
            time.sleep(1.0 * (i + 1))
    os.rename(path, path.with_name(f"{path.name}.old-{time.strftime('%H%M%S')}"))


def main() -> None:
    if OUT.exists():
        _clear(OUT)
    shutil.copytree(SRC, OUT)

    st = ReportState()
    set_project(st, str(next(OUT.glob("*.pbip"))))

    # ONE call: profile the model and build a full designed report.
    res = scaffold_report(st, accent="#1F3A5F", max_detail_pages=3)

    # add a relative-date report filter for polish
    p = st.project
    st.project.add_filter("report", build_filter(
        "Date.Date", filter_type="RelativeDate", last_n=3, relative_unit="year"))

    report = p.validate_project()
    print("v2 demo built from a raw model in one scaffold call.")
    print("  pages:", res["pages"])
    print("  profile:", res["profile_summary"])
    print("  schema-valid:", report["ok"],
          f"({report['checked']} JSON files checked)")
    print("  OPEN THIS ->", next(OUT.glob("*.pbip")))


if __name__ == "__main__":
    main()
