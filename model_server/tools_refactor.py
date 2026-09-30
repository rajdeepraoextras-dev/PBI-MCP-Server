"""Cascading rename tools (model server): measures, columns, tables."""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.refactor import Renamer, find_references

if TYPE_CHECKING:  # pragma: no cover
    from model_server.server import ModelState


def rename_measure(state: "ModelState", table: str, old_name: str, new_name: str) -> dict:
    return Renamer(state.require()).rename_measure(table, old_name, new_name)


def rename_column(state: "ModelState", table: str, old_name: str, new_name: str) -> dict:
    return Renamer(state.require()).rename_column(table, old_name, new_name)


def rename_table(state: "ModelState", old_name: str, new_name: str) -> dict:
    return Renamer(state.require()).rename_table(old_name, new_name)


def references(state: "ModelState", kind: str, name: str, table: str | None = None) -> dict:
    kind = kind.lower()
    if kind not in ("measure", "column", "table"):
        raise ValueError("kind must be 'measure', 'column' or 'table'.")
    if kind == "column" and not table:
        raise ValueError("table is required for kind='column'.")
    return find_references(state.require(), kind, table, name)


def register(mcp, state, tool) -> None:
    @tool(write=True)
    def pbi_rename_measure(table: str, old_name: str, new_name: str) -> dict:
        """Rename a measure everywhere: its TMDL definition, every DAX
        expression that references it (measures, calculated columns, roles),
        perspectives and translations, and every report binding, filter,
        bookmark and conditional-format selector that uses it. Power Query
        sources are never touched. Returns the list of files changed; use
        pbi_undo to revert the whole cascade."""
        return rename_measure(state, table, old_name, new_name)

    @tool(write=True)
    def pbi_rename_column(table: str, old_name: str, new_name: str) -> dict:
        """Rename a column everywhere: its TMDL definition, sortByColumn and
        hierarchy levels, relationships, every DAX expression referencing
        Table[Column] (and bare [Column] inside its own table), role column
        permissions, perspectives, translations, and every report binding,
        filter, sort and bookmark. Power Query sources are never touched.
        Returns the files changed; pbi_undo reverts the cascade."""
        return rename_column(state, table, old_name, new_name)

    @tool(write=True)
    def pbi_rename_table(old_name: str, new_name: str) -> dict:
        """Rename a table everywhere: its TMDL file and header, partitions
        named after it, model.tmdl `ref table`, relationships, every DAX
        qualifier and bare table reference (ALL(Table)...), roles,
        perspectives, translations, and every report Entity reference and
        queryRef. Power Query sources are never touched. Returns the files
        changed; pbi_undo reverts the cascade (including the file rename)."""
        return rename_table(state, old_name, new_name)

    @tool(read=True)
    def pbi_find_references(kind: str, name: str, table: str | None = None) -> dict:
        """Preview where a measure, column (table required) or table is
        referenced: DAX expressions (as table/measure pairs) and report
        fields (Entity.Property) bound anywhere in the report. Use before a
        rename or delete to see the blast radius."""
        return references(state, kind, name, table)
