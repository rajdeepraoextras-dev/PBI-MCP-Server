"""Day 17: M3 hardening — report read across every real project available.

Read-only sweep: fixtures under tests/fixtures/real/ plus (if still present)
the other real projects found on this machine. Every page and every visual
must parse; usage classification must be complete and disjoint.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.pbip import PbipProject
from core.pbir import visual_bindings
from core.usage import classify_usage

_CANDIDATES = list(
    (Path(__file__).parent / "fixtures" / "real").glob("*/*.pbip")
) + [
    Path(r"C:\Users\resod\OneDrive\Desktop\pbiiiiiiiiii\Corporate Spend.pbip"),
    Path(r"C:\Users\resod\OneDrive\Desktop\finaltest\Zomato Dashboard - Rajdeep Rao.pbip"),
]
PROJECTS = [p for p in _CANDIDATES if p.exists()]


@pytest.fixture(params=PROJECTS, ids=[p.stem[:20] for p in PROJECTS])
def project(request) -> PbipProject:
    return PbipProject(request.param)


def test_every_visual_parses(project):
    pages = project.list_pages()
    assert pages
    total = 0
    for page in pages:
        visuals = project.list_visuals(page.id)
        assert len(visuals) == page.visual_count
        for v in visuals:
            assert v.id and v.raw
            visual_bindings(v)  # must never raise
            got = project.get_visual(page.id, v.id)
            assert got.id == v.id
        total += len(visuals)
    assert total > 0


def test_visual_types_inventory(project):
    """Every visual type string is non-empty (or the visual is a group/box)."""
    for page in project.list_pages():
        for v in project.list_visuals(page.id):
            # some containers (groups) legitimately have no visualType
            if v.visual_type is not None:
                assert isinstance(v.visual_type, str) and v.visual_type


def test_usage_complete_and_disjoint(project):
    u = classify_usage(project)
    all_measures = {m.name for m in project.list_measures()}
    all_columns = {f"{t.name}.{c.name}" for t in project.list_tables()
                   for c in t.columns}
    got_m = [set(u[k]["measures"]) for k in ("direct", "indirect", "unused")]
    got_c = [set(u[k]["columns"]) for k in ("direct", "indirect", "unused")]
    assert got_m[0] | got_m[1] | got_m[2] == all_measures
    assert got_c[0] | got_c[1] | got_c[2] == all_columns
    for i in range(3):
        for j in range(i + 1, 3):
            assert not (got_m[i] & got_m[j])
            assert not (got_c[i] & got_c[j])


def test_positions_sane(project):
    for page in project.list_pages():
        for v in project.list_visuals(page.id):
            assert v.position.width >= 0 and v.position.height >= 0
