"""Day 3: PBIR reader tests against the synthetic fixture.

Fixture has two pages: 'overview' (card + bar + table) and 'details'
(hidden, one slicer).
"""

from __future__ import annotations

from pathlib import Path

from core.pbip import PbipProject
from core.pbir import visual_bindings

FIXTURE = Path(__file__).parent / "fixtures" / "synthetic" / "Synthetic.pbip"


def _project() -> PbipProject:
    return PbipProject(FIXTURE)


# --- pages ------------------------------------------------------------------

def test_pages_order_and_names():
    pages = _project().list_pages()
    assert [p.id for p in pages] == ["overview", "details"]  # pages.json order
    assert [p.name for p in pages] == ["Overview", "Details"]


def test_page_visual_counts():
    by_id = {p.id: p for p in _project().list_pages()}
    assert by_id["overview"].visual_count == 3
    assert by_id["details"].visual_count == 1


def test_hidden_page_flag():
    by_id = {p.id: p for p in _project().list_pages()}
    assert by_id["overview"].is_hidden is False
    assert by_id["details"].is_hidden is True


def test_page_size():
    overview = {p.id: p for p in _project().list_pages()}["overview"]
    assert overview.width == 1280
    assert overview.height == 720


# --- visuals ----------------------------------------------------------------

def test_list_visuals_types():
    visuals = _project().list_visuals("overview")
    types = {v.id: v.visual_type for v in visuals}
    assert types == {
        "card1": "card",
        "bar1": "clusteredBarChart",
        "table1": "tableEx",
    }


def test_visual_position_and_title():
    card = _project().get_visual("overview", "card1")
    assert card.title == "Total Revenue"
    assert card.position.x == 16
    assert card.position.width == 220


def test_get_visual_keeps_raw():
    bar = _project().get_visual("overview", "bar1")
    assert bar.raw["visual"]["visualType"] == "clusteredBarChart"
    assert "queryState" in bar.raw["visual"]["query"]


def test_missing_visual_raises():
    import pytest

    with pytest.raises(FileNotFoundError):
        _project().get_visual("overview", "nope")


# --- bindings helper (feeds Day 15 field-usage) -----------------------------

def test_visual_bindings_bar():
    bar = _project().get_visual("overview", "bar1")
    binds = visual_bindings(bar)
    assert binds["Category"] == ["Date.Year"]
    assert binds["Y"] == ["Sales.Net Revenue"]


def test_visual_bindings_table_multi():
    table = _project().get_visual("overview", "table1")
    binds = visual_bindings(table)
    assert binds["Values"] == ["Sales.Net Revenue", "Sales.Margin %"]
