"""B.3 round-trip test — the M0 gate (Day 5).

Load -> read -> mutate -> save -> reload -> assert, run against REAL exported
`.pbip` projects discovered under tests/fixtures/real/. Each test works on a
throwaway copy, so the source fixtures are never mutated.

Step 6 of B.3 — reopen in Power BI Desktop with no repair prompt — cannot be
automated; it stays a manual release gate. `make_qa_copy()` produces a mutated
project on disk for that manual check.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from core import io_safe
from core.pbip import PbipProject

REAL_DIR = Path(__file__).parent / "fixtures" / "real"


def _discover() -> list[Path]:
    if not REAL_DIR.is_dir():
        return []
    return sorted(REAL_DIR.glob("*/*.pbip"))


REAL_PROJECTS = _discover()

pytestmark = pytest.mark.skipif(
    not REAL_PROJECTS,
    reason="No real .pbip under tests/fixtures/real/ (copy an export there)",
)


def _ids(paths):
    return [p.parent.name for p in paths]


@pytest.fixture(params=REAL_PROJECTS, ids=_ids(REAL_PROJECTS))
def real_copy(request, tmp_path) -> PbipProject:
    """A fresh, isolated copy of one real project."""
    src = request.param.parent
    dst = tmp_path / src.name
    shutil.copytree(src, dst)
    return PbipProject(dst / request.param.name)


def test_smoke_import():
    """Day 1: the package imports and PbipProject is constructible."""
    from core.pbip import PbipProject

    project = PbipProject("some/path.pbip")
    assert project.path == Path("some/path.pbip")


# --- read: the fixture is understood ---------------------------------------

def test_reads_model(real_copy):
    assert real_copy.list_tables(), "expected tables"
    assert real_copy.list_measures(), "expected measures"


def test_reads_report(real_copy):
    pages = real_copy.list_pages()
    assert pages, "expected pages"
    # every visual on every page parses without error
    for page in pages:
        for v in real_copy.list_visuals(page.id):
            assert v.id


# --- B.3 steps 1-5: mutate -> save -> reload -> assert ----------------------

def test_b3_roundtrip(real_copy):
    a_table = real_copy.list_tables()[0].name

    # 3. upsert a measure
    real_copy.upsert_measure(a_table, "PBI MCP Test M", "BLANK()", fmt="#,0")

    # 4. create a page + add a card bound to a real measure
    target_measure = real_copy.list_measures()[0]
    ref = f"{target_measure.table}.{target_measure.name}"
    page_id = real_copy.create_page("PBI MCP QA")
    visual_id = real_copy.add_visual(page_id, {
        "visual_type": "card",
        "bindings": {"Values": [ref]},
        "title": "QA Card",
    })

    # 5. save
    real_copy.save()

    # reload from disk and assert everything landed
    reopened = PbipProject(real_copy.path)
    assert "PBI MCP Test M" in {m.name for m in reopened.list_measures()}
    pages = {p.id: p for p in reopened.list_pages()}
    assert page_id in pages
    assert visual_id in {v.id for v in reopened.list_visuals(page_id)}


def test_mutation_preserves_style(real_copy):
    """A mutated TMDL file keeps its original line ending + BOM."""
    table = real_copy.list_tables()[0].name
    tfile = real_copy._table_file(table)
    before = io_safe.detect_style(tfile)
    real_copy.upsert_measure(table, "Style Probe", "1")
    after = io_safe.detect_style(tfile)
    assert before == after


def test_untouched_files_are_byte_identical(real_copy):
    """Mutating one table must not rewrite any other file in the project."""
    root = real_copy.path.parent
    before = {
        p: p.read_bytes()
        for p in root.rglob("*.tmdl")
    }
    table = real_copy.list_tables()[0].name
    tfile = real_copy._table_file(table)
    real_copy.upsert_measure(table, "Isolation Probe", "1")
    for p, data in before.items():
        if p == tfile:
            continue
        assert p.read_bytes() == data, f"unexpected change in {p.name}"
