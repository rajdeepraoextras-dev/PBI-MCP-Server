"""Pydantic models for the PBIP domain.

These are the shapes that flow between `PbipProject`, the parsers, and the
MCP tool boundary. Keep them thin — they mirror the on-disk format, not a
richer internal model.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


# --- Model layer (TMDL) ---------------------------------------------------

class Column(BaseModel):
    name: str
    data_type: str | None = None
    summarize_by: str | None = None
    is_hidden: bool = False


class Measure(BaseModel):
    table: str
    name: str
    dax: str
    format_string: str | None = None
    display_folder: str | None = None
    is_hidden: bool = False


class Relationship(BaseModel):
    name: str | None = None
    from_table: str
    from_column: str
    to_table: str
    to_column: str
    cardinality: str | None = None          # e.g. "manyToOne"
    cross_filter: str | None = None
    is_active: bool = True


class Table(BaseModel):
    name: str
    columns: list[Column] = Field(default_factory=list)
    measures: list[Measure] = Field(default_factory=list)
    is_hidden: bool = False
    is_calc_group: bool = False


# --- Report layer (PBIR) --------------------------------------------------

class Position(BaseModel):
    x: float = 0
    y: float = 0
    z: float = 0
    width: float = 0
    height: float = 0


class Visual(BaseModel):
    id: str
    page_id: str
    visual_type: str | None = None
    title: str | None = None
    position: Position = Field(default_factory=Position)
    # Raw config kept verbatim so round-trips never lose keys we don't model.
    raw: dict = Field(default_factory=dict)


class Page(BaseModel):
    id: str
    name: str
    width: float | None = None
    height: float | None = None
    visual_count: int = 0
    is_hidden: bool = False
