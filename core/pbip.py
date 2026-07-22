"""PbipProject — the one class every MCP tool uses.

Build this before any MCP tool (Part B.2). It owns loading a `.pbip`
project, reading the model (TMDL) and report (PBIR) layers, and writing
mutations back through the safe-I/O layer.

Definition of done for the foundation (B.3) — the Day-5 gate:
  1. Load fixtures/Sample.pbip
  2. list_measures() finds the known measures
  3. upsert_measure("Sales", "Test M", "BLANK()")
  4. create_page("QA"); add_visual(page, one card bound to a real measure)
  5. save()
  6. Reopen in Power BI Desktop -> no repair prompt, page + measure present
"""

from __future__ import annotations

import json
from pathlib import Path

from core.schemas import Measure, Page, Relationship, Table, Visual


class PbipProject:
    """Load, read, and mutate a Power BI Project (.pbip) on disk.

    Accepts the project root, the `.pbip` pointer file, or a `.Report` /
    `.SemanticModel` folder — resolves the sibling layer from there.
    """

    def __init__(self, path: str | Path, *, backups: bool = True):
        self.path = Path(path)
        self.semantic_model_dir: Path | None = None
        self.report_dir: Path | None = None
        self.backups = backups
        self.preflight = True   # pre-flight schema validation on writes (E1)
        self._backed_up: set[Path] = set()
        #: str(path) -> ((path, mtime_ns, size), Table) — mtime-keyed cache
        self._table_cache: dict = {}
        # Lazily resolve so constructing with a non-existent path (e.g. in a
        # unit test) doesn't raise — resolution happens on first real access.
        if self.path.exists():
            self._resolve()

    def _resolve(self) -> None:
        """Locate the `*.SemanticModel` and `*.Report` folders from `self.path`.

        Accepts the project root, the `.pbip` pointer file, or either layer
        folder — and finds the sibling layer from there.
        """
        p = self.path
        root: Path
        if p.is_file():                       # a .pbip pointer (or a file within)
            root = p.parent
        elif p.name.endswith(".SemanticModel"):
            self.semantic_model_dir = p
            root = p.parent
        elif p.name.endswith(".Report"):
            self.report_dir = p
            root = p.parent
        else:                                 # a project-root directory
            root = p

        if self.semantic_model_dir is None:
            self.semantic_model_dir = next(root.glob("*.SemanticModel"), None)
        if self.report_dir is None:
            self.report_dir = next(root.glob("*.Report"), None)

    def _require_model(self) -> Path:
        if self.semantic_model_dir is None:
            self._resolve()
        if self.semantic_model_dir is None:
            raise FileNotFoundError(
                f"No *.SemanticModel folder found from {self.path}"
            )
        return self.semantic_model_dir

    # --- read -------------------------------------------------------------

    def _parse_table_cached(self, path: Path) -> Table:
        """Parse a table file, memoized by (path, mtime, size).

        A write changes mtime, so the cache self-invalidates — never stale.
        Keeps repeated tool calls from reparsing the whole model each time.
        """
        from core.tmdl import parse_table_file

        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
        cached = self._table_cache.get(str(path))
        if cached is not None and cached[0] == key:
            return cached[1]
        table = parse_table_file(path)
        self._table_cache[str(path)] = (key, table)
        return table

    def list_tables(self) -> list[Table]:
        tables_dir = self._require_model() / "definition" / "tables"
        if not tables_dir.is_dir():
            return []
        return [
            self._parse_table_cached(f)
            for f in sorted(tables_dir.glob("*.tmdl"))
        ]

    def list_measures(self, table: str | None = None) -> list[Measure]:
        measures: list[Measure] = []
        for t in self.list_tables():
            if table is not None and t.name != table:
                continue
            measures.extend(t.measures)
        return measures

    def list_relationships(self) -> list[Relationship]:
        from core.tmdl import parse_relationships_file

        rel_file = self._require_model() / "definition" / "relationships.tmdl"
        if not rel_file.exists():
            return []
        return parse_relationships_file(rel_file)

    def model_lineage(self, measure: str | None = None) -> dict:
        """Dependency graph parsed from DAX (who references whom).

        With `measure`: that measure's direct deps + who references it
        (directly and transitively). Without: the full graph + reverse edges.
        """
        from core.lineage import (
            build_lineage, dependents_of, transitive_dependents,
        )

        tables = self.list_tables()
        measures = self.list_measures()
        graph = build_lineage(tables, measures)

        if measure is not None:
            if measure not in graph:
                raise KeyError(f"Measure {measure!r} not found")
            node = graph[measure]
            return {
                "measure": measure,
                "table": node["table"],
                "depends_on_measures": node["measures"],
                "depends_on_columns": node["columns"],
                "unresolved": node.get("unresolved", []),
                "referenced_by": dependents_of(graph).get(measure, []),
                "referenced_by_transitive": transitive_dependents(graph, measure),
            }

        return {"measures": graph, "referenced_by": dependents_of(graph)}

    def get_model(self) -> dict:
        """A JSON-serializable snapshot: tables (+columns/+measures) & relationships."""
        tables = self.list_tables()
        return {
            "path": str(self.path),
            "tables": [t.model_dump() for t in tables],
            "relationships": [r.model_dump() for r in self.list_relationships()],
            "counts": {
                "tables": len(tables),
                "columns": sum(len(t.columns) for t in tables),
                "measures": sum(len(t.measures) for t in tables),
            },
        }

    def _require_report(self) -> Path:
        if self.report_dir is None:
            self._resolve()
        if self.report_dir is None:
            raise FileNotFoundError(f"No *.Report folder found from {self.path}")
        return self.report_dir

    def list_pages(self) -> list[Page]:
        from core.pbir import read_pages

        return read_pages(self._require_report() / "definition")

    def list_visuals(self, page_id: str) -> list[Visual]:
        from core.pbir import read_visuals

        return read_visuals(self._require_report() / "definition", page_id)

    def get_visual(self, page_id: str, visual_id: str) -> Visual:
        from core.pbir import read_visual

        return read_visual(self._require_report() / "definition", page_id, visual_id)

    # --- write (each does atomic write + optional backup) -----------------

    def _write_text(self, path: Path, text: str) -> None:
        """Backup-once then atomically write text, preserving the file's style.

        Existing files keep their exact line ending + BOM (Desktop mixes CRLF
        and LF across file types); new files default to LF / no-BOM.
        """
        from core import io_safe

        encoding, newline = io_safe.detect_style(path)
        if self.backups and path not in self._backed_up and path.exists():
            io_safe.backup(path)
            self._backed_up.add(path)
        io_safe.atomic_write(path, text, encoding=encoding, newline=newline)

    #: report-layer filenames -> schema kind for pre-flight validation
    _SCHEMA_KIND = {
        "visual.json": "visualContainer",
        "page.json": "page",
        "pages.json": "pagesMetadata",
        "report.json": "report",
    }

    def _write_json(self, path: Path, obj: dict, *,
                    validate: bool = True) -> None:
        """Write JSON, pre-flight validated against the vendored Fabric schema.

        A malformed report shape is rejected here — before it can reach disk
        and before Desktop ever sees it. Set validate=False for non-schema
        files (theme resources, bookmarks handled separately).
        """
        if validate and self.preflight:
            kind = self._SCHEMA_KIND.get(path.name)
            if kind is not None:
                from core import schema_validate

                schema_validate.assert_valid(kind, obj, context=str(path.name))
        self._write_text(path, json.dumps(obj, indent=2))

    def _table_file(self, table: str) -> Path:
        """Locate the .tmdl file whose declared table name is `table`."""
        from core.tmdl import parse_table_file

        tables_dir = self._require_model() / "definition" / "tables"
        for f in sorted(tables_dir.glob("*.tmdl")):
            if parse_table_file(f).name == table:
                return f
        raise KeyError(f"Table {table!r} not found under {tables_dir}")

    def _is_measure(self, entity: str, prop: str) -> bool:
        return any(
            m.table == entity and m.name == prop for m in self.list_measures()
        )

    def upsert_measure(
        self, table: str, name: str, dax: str, fmt: str | None = None,
        display_folder: str | None = None,
    ) -> None:
        """Insert or replace a measure via a loss-free targeted text edit."""
        from core.tmdl import upsert_measure_text

        path = self._table_file(table)
        new_text = upsert_measure_text(
            path.read_text(encoding="utf-8-sig"), name, dax, fmt, display_folder
        )
        self._write_text(path, new_text)

    def _find_measure(self, name: str) -> Measure | None:
        for m in self.list_measures():
            if m.name == name:
                return m
        return None

    def _lint_dax(self, dax: str, extra_names: set[str] | None = None) -> list[str]:
        """Best-effort DAX warnings (non-blocking)."""
        from core.dax_lint import lint_dax

        names = {m.name for m in self.list_measures()}
        if extra_names:
            names |= extra_names
        return lint_dax(dax, names)

    def create_measure(
        self, table: str, name: str, dax: str, fmt: str | None = None,
        display_folder: str | None = None,
    ) -> dict:
        """Create a new measure; error if the name already exists.

        Measure names are unique across a Power BI model, so we check the whole
        model, not just `table`.
        """
        existing = self._find_measure(name)
        if existing is not None:
            raise ValueError(
                f"Measure {name!r} already exists in table {existing.table!r}"
            )
        self._table_file(table)  # validate the table exists before writing
        self.upsert_measure(table, name, dax, fmt, display_folder)
        result = {"ok": True, "action": "created", "table": table, "name": name}
        warnings = self._lint_dax(dax)
        if warnings:
            result["warnings"] = warnings
        return result

    def update_measure(
        self, table: str, name: str, dax: str | None = None,
        fmt: str | None = None, display_folder: str | None = None,
    ) -> dict:
        """Update an existing measure; only provided fields change.

        Unspecified fields (None) are preserved from the current definition.
        Errors if the measure doesn't exist, or exists in a different table.
        """
        existing = self._find_measure(name)
        if existing is None:
            raise KeyError(f"Measure {name!r} not found")
        if existing.table != table:
            raise ValueError(
                f"Measure {name!r} lives in table {existing.table!r}, not {table!r}"
            )
        new_dax = dax if dax is not None else existing.dax
        self.upsert_measure(
            table, name, new_dax,
            fmt if fmt is not None else existing.format_string,
            display_folder if display_folder is not None else existing.display_folder,
        )
        result = {"ok": True, "action": "updated", "table": table, "name": name}
        warnings = self._lint_dax(new_dax)
        if warnings:
            result["warnings"] = warnings
        return result

    def list_backups(self) -> list[dict]:
        """All .bak-* snapshots in the project, newest first."""
        import re

        out = []
        for root in filter(None, [self.semantic_model_dir, self.report_dir]):
            for bak in root.rglob("*.bak-*"):
                m = re.match(r"(?s)(.+)\.bak-(\d{8}-\d{6})(?:-\d+)?$", bak.name)
                if not m:
                    continue
                out.append({
                    "backup": str(bak),
                    "original": str(bak.with_name(m.group(1))),
                    "timestamp": m.group(2),
                })
        return sorted(out, key=lambda b: b["timestamp"], reverse=True)

    def restore_backup(self, backup: str) -> dict:
        """Copy a .bak-* snapshot back over its original file (atomically)."""
        from core import io_safe

        bak = Path(backup)
        if not bak.exists():
            raise FileNotFoundError(f"Backup not found: {backup}")
        match = [b for b in self.list_backups() if b["backup"] == str(bak)]
        if not match:
            raise ValueError(f"{backup} is not a backup of this project")
        original = Path(match[0]["original"])
        encoding, newline = io_safe.detect_style(bak)
        text = bak.read_text(encoding="utf-8-sig")
        io_safe.atomic_write(original, text,
                             encoding=encoding, newline=newline)
        return {"ok": True, "restored": str(original), "from": str(bak)}

    def delete_measure(self, table: str, name: str, force: bool = False,
                       dry_run: bool = False) -> dict:
        """Delete a measure — refuses if other measures reference it.

        The guard uses the transitive dependency graph (Part C.1: "must check
        lineage first"). `force=True` overrides, for when the caller has
        already handled the dependents.
        """
        from core.tmdl import delete_measure_text

        existing = self._find_measure(name)
        if existing is None:
            raise KeyError(f"Measure {name!r} not found")
        if existing.table != table:
            raise ValueError(
                f"Measure {name!r} lives in table {existing.table!r}, not {table!r}"
            )

        lineage = self.model_lineage(name)
        dependents = lineage["referenced_by"]
        if dependents and not force:
            raise ValueError(
                f"Refusing to delete {name!r}: referenced by "
                f"{dependents} (transitively: "
                f"{lineage['referenced_by_transitive']}). "
                f"Update those measures first, or pass force=true."
            )

        # report-aware fail-safe (M6): a measure the report uses — bound in a
        # visual/filter (direct) or feeding one that is (indirect) — is
        # load-bearing even with no measure-to-measure dependents.
        if not force:
            usage = self._report_usage_or_none()
            if usage is not None:
                if name in usage["direct"]["measures"]:
                    raise ValueError(
                        f"Refusing to delete {name!r}: it is bound directly "
                        f"in the report (visual or filter). Remove those "
                        f"usages first, or pass force=true.")
                if name in usage["indirect"]["measures"]:
                    raise ValueError(
                        f"Refusing to delete {name!r}: a measure the report "
                        f"displays depends on it. Pass force=true to "
                        f"override.")

        if dry_run:
            return {"ok": True, "action": "dry_run", "would_delete": name,
                    "table": table, "dependents": dependents}

        path = self._table_file(table)
        new_text = delete_measure_text(
            path.read_text(encoding="utf-8-sig"), name)
        self._write_text(path, new_text)
        return {"ok": True, "action": "deleted", "table": table, "name": name,
                "forced_past_dependents": dependents if force else []}

    def _report_usage_or_none(self) -> dict | None:
        """classify_usage if a report layer exists; None for model-only work."""
        from core.usage import classify_usage

        try:
            self._require_report()
        except FileNotFoundError:
            return None
        return classify_usage(self)

    def _find_table(self, table: str) -> Table:
        for t in self.list_tables():
            if t.name == table:
                return t
        raise KeyError(f"Table {table!r} not found")

    def create_column(
        self, table: str, name: str, data_type: str,
        summarize_by: str | None = None, source_column: str | None = None,
        dax: str | None = None,
    ) -> dict:
        """Create a column (data or, with `dax`, calculated) in `table`."""
        from core.tmdl import insert_column_text

        t = self._find_table(table)
        if any(c.name == name for c in t.columns):
            raise ValueError(f"Column {name!r} already exists in {table!r}")
        path = self._table_file(table)
        new_text = insert_column_text(
            path.read_text(encoding="utf-8-sig"),
            name, data_type, summarize_by, source_column, dax,
        )
        self._write_text(path, new_text)
        return {"ok": True, "action": "created", "table": table, "column": name}

    def _column_exists(self, table: str, column: str) -> bool:
        try:
            t = self._find_table(table)
        except KeyError:
            return False
        return any(c.name == column for c in t.columns)

    def create_relationship(
        self, from_table: str, from_column: str,
        to_table: str, to_column: str,
        cardinality: str | None = None, cross_filter: str | None = None,
        is_active: bool = True,
    ) -> dict:
        """Create a relationship; endpoints must be existing columns."""
        import uuid

        from core.tmdl import append_relationship_text

        for tbl, col in [(from_table, from_column), (to_table, to_column)]:
            if not self._column_exists(tbl, col):
                raise KeyError(f"Column {tbl}.{col} not found")
        for r in self.list_relationships():
            if (r.from_table == from_table and r.from_column == from_column
                    and r.to_table == to_table and r.to_column == to_column):
                raise ValueError(
                    f"Relationship {from_table}.{from_column} -> "
                    f"{to_table}.{to_column} already exists ({r.name})"
                )

        rel_file = self._require_model() / "definition" / "relationships.tmdl"
        text = rel_file.read_text(encoding="utf-8-sig") if rel_file.exists() else ""
        name = str(uuid.uuid4())
        new_text = append_relationship_text(
            text, name, from_table, from_column, to_table, to_column,
            cardinality, cross_filter, is_active,
        )
        self._write_text(rel_file, new_text)
        return {"ok": True, "action": "created", "name": name,
                "from": f"{from_table}.{from_column}",
                "to": f"{to_table}.{to_column}"}

    def create_calc_group(self, name: str, precedence: int,
                          items: list[dict],
                          column_name: str = "Name") -> dict:
        """Create a calculation-group table and register it in model.tmdl.

        items: [{"name": ..., "dax": ...}, ...] — SELECTEDMEASURE() patterns.
        """
        from core.tmdl import add_table_ref_text, emit_calc_group_table

        if any(t.name == name for t in self.list_tables()):
            raise ValueError(f"Table {name!r} already exists")
        if not items:
            raise ValueError("A calculation group needs at least one item")

        tables_dir = self._require_model() / "definition" / "tables"
        table_file = tables_dir / f"{name}.tmdl"
        self._write_text(table_file, emit_calc_group_table(
            name, precedence, items, column_name))

        model_file = self._require_model() / "definition" / "model.tmdl"
        if model_file.exists():
            self._write_text(model_file, add_table_ref_text(
                model_file.read_text(encoding="utf-8-sig"), name))
        return {"ok": True, "action": "created", "table": name,
                "items": [i["name"] for i in items]}

    def bulk_create_measures(self, measures: list[dict]) -> dict:
        """Create many measures in one call (validated up front, then applied).

        measures: [{"table","name","dax","format"?,"display_folder"?}, ...]
        All-or-nothing validation: any duplicate/unknown-table rejects the
        whole batch before a single write happens.
        """
        from core.tmdl import upsert_measure_text

        if not measures:
            raise ValueError("Empty measures batch")
        existing = {m.name for m in self.list_measures()}
        tables = {t.name for t in self.list_tables()}
        seen: set[str] = set()
        for spec in measures:
            name, table = spec["name"], spec["table"]
            if table not in tables:
                raise KeyError(f"Table {table!r} not found (measure {name!r})")
            if name in existing:
                raise ValueError(f"Measure {name!r} already exists")
            if name in seen:
                raise ValueError(f"Duplicate name {name!r} within the batch")
            seen.add(name)

        # group by table -> one read/write per file
        by_table: dict[str, list[dict]] = {}
        for spec in measures:
            by_table.setdefault(spec["table"], []).append(spec)
        for table, specs in by_table.items():
            path = self._table_file(table)
            text = path.read_text(encoding="utf-8-sig")
            for s in specs:
                text = upsert_measure_text(
                    text, s["name"], s["dax"],
                    s.get("format"), s.get("display_folder"))
            self._write_text(path, text)

        # DAX lint across the batch (names include the whole batch)
        batch_names = existing | seen
        flagged = {}
        for spec in measures:
            w = self._lint_dax(spec["dax"], batch_names)
            if w:
                flagged[spec["name"]] = w
        result = {"ok": True, "action": "created",
                  "count": len(measures), "tables": sorted(by_table)}
        if flagged:
            result["warnings"] = flagged
            result["warning_note"] = (
                f"{len(flagged)} of {len(measures)} measures have likely DAX "
                f"errors Power BI will reject — review and fix these.")
        return result

    def create_page(self, name: str, width: float = 1280,
                    height: float = 720) -> str:
        """Create a page; return its pageId."""
        from core.pbir import build_page_json, slugify

        pages_dir = self._require_report() / "definition" / "pages"
        existing = {p.name for p in pages_dir.iterdir() if p.is_dir()} \
            if pages_dir.is_dir() else set()
        base = slugify(name, fallback="page")
        page_id, n = base, 1
        while page_id in existing:
            n += 1
            page_id = f"{base}-{n}"

        self._write_json(pages_dir / page_id / "page.json",
                         build_page_json(page_id, name, width, height))

        # Register the page in pages.json (order + keep/assign active page).
        meta_path = pages_dir / "pages.json"
        from core.pbir import _PAGES_SCHEMA

        meta = json.loads(meta_path.read_text(encoding="utf-8-sig")) \
            if meta_path.exists() else {"$schema": _PAGES_SCHEMA, "pageOrder": []}
        order = meta.get("pageOrder", [])
        if page_id not in order:
            order.append(page_id)
        meta["pageOrder"] = order
        meta.setdefault("activePageName", page_id)
        self._write_json(meta_path, meta)
        return page_id

    def _new_visual_id(self, page_id: str, base: str) -> tuple[Path, str]:
        visuals_dir = (self._require_report() / "definition" / "pages"
                       / page_id / "visuals")
        existing = {p.name for p in visuals_dir.iterdir() if p.is_dir()} \
            if visuals_dir.is_dir() else set()
        vid, n = base, 1
        while vid in existing:
            n += 1
            vid = f"{base}-{n}"
        return visuals_dir, vid

    def _add_raw_visual(self, page_id: str, base: str, obj: dict) -> str:
        """Write a prebuilt visual.json dict; return the visualId."""
        if page_id not in {p.id for p in self.list_pages()}:
            raise KeyError(f"Page {page_id!r} not found")
        visuals_dir, vid = self._new_visual_id(page_id, base)
        obj["name"] = vid
        self._write_json(visuals_dir / vid / "visual.json", obj)
        return vid

    def add_text(self, page_id: str, runs, position: dict | None = None,
                 z: int | None = None) -> str:
        from core.design import build_textbox

        if z is not None:
            position = {**(position or {}), "z": z, "tabOrder": z}
        return self._add_raw_visual(page_id, "textbox",
                                    build_textbox("textbox", runs, position))

    def add_image(self, page_id: str, image_path: str,
                  position: dict | None = None, scaling: str | None = None,
                  z: int | None = None) -> str:
        """Upload an image into RegisteredResources and place it on the page."""
        import shutil

        from core.design import build_image

        src = Path(image_path)
        if not src.exists():
            raise FileNotFoundError(f"Image not found: {image_path}")
        res_dir = (self._require_report() / "StaticResources"
                   / "RegisteredResources")
        res_dir.mkdir(parents=True, exist_ok=True)
        # unique resource name in the package
        resource_name = src.name
        n = 1
        while (res_dir / resource_name).exists():
            n += 1
            resource_name = f"{src.stem}-{n}{src.suffix}"
        shutil.copy2(src, res_dir / resource_name)
        if z is not None:
            position = {**(position or {}), "z": z, "tabOrder": z}
        obj = build_image("image", resource_name, position, scaling)
        return self._add_raw_visual(page_id, "image", obj)

    def add_shape(self, page_id: str, shape: str = "rectangle",
                  fill: str | None = None, outline: str | None = None,
                  outline_weight: float | None = None,
                  position: dict | None = None, round_corners: bool = False,
                  z: int | None = None) -> str:
        from core.design import build_shape

        if z is not None:
            position = {**(position or {}), "z": z, "tabOrder": z}
        obj = build_shape("shape", shape, fill, outline, outline_weight,
                          position, round_corners)
        return self._add_raw_visual(page_id, "shape", obj)

    def add_visual_raw(self, page_id: str, visual_json: dict,
                       base: str = "visual") -> str:
        """Add a prebuilt, schema-validated visual.json (escape hatch).

        For third-party visuals or shapes this server doesn't model. The dict
        is validated against the visualContainer schema before writing.
        """
        return self._add_raw_visual(page_id, base, dict(visual_json))

    def style_page(self, page_id: str, background_color: str | None = None,
                   background_transparency: float | None = None,
                   wallpaper_color: str | None = None) -> dict:
        """Set page canvas background + wallpaper (outspace) styling."""
        from core.formatting import build_objects_patch, merge_objects

        page_json = (self._require_report() / "definition" / "pages"
                     / page_id / "page.json")
        if not page_json.exists():
            raise FileNotFoundError(f"Page {page_id!r} not found")
        data = json.loads(page_json.read_text(encoding="utf-8-sig"))

        patch: dict = {}
        bg: dict = {}
        if background_color is not None:
            bg["color"] = background_color
        if background_transparency is not None:
            bg["transparency"] = background_transparency
        if bg:
            patch["background"] = bg
        if wallpaper_color is not None:
            patch["outspace"] = {"color": wallpaper_color}
        if not patch:
            raise ValueError("Nothing to style — pass a color/transparency")

        data["objects"] = merge_objects(data.get("objects", {}),
                                        build_objects_patch(patch))
        self._write_json(page_json, data)
        return {"ok": True, "page_id": page_id, "styled": sorted(patch)}

    def group_visuals(self, page_id: str, visual_ids: list[str],
                      name: str = "Group") -> str:
        """Group visuals so they move/style as one. Returns the group id."""
        if len(visual_ids) < 2:
            raise ValueError("Grouping needs at least two visuals")
        pages_dir = self._require_report() / "definition" / "pages"
        members = []
        for vid in visual_ids:
            vf = pages_dir / page_id / "visuals" / vid / "visual.json"
            if not vf.exists():
                raise FileNotFoundError(f"Visual {vid!r} not found")
            members.append((vf, json.loads(vf.read_text(encoding="utf-8-sig"))))

        # bounding box of members
        xs = [m[1].get("position", {}).get("x", 0) for m in members]
        ys = [m[1].get("position", {}).get("y", 0) for m in members]
        x2 = [m[1].get("position", {}).get("x", 0)
              + m[1].get("position", {}).get("width", 0) for m in members]
        y2 = [m[1].get("position", {}).get("y", 0)
              + m[1].get("position", {}).get("height", 0) for m in members]
        z_min = min(m[1].get("position", {}).get("z", 0) for m in members)

        visuals_dir, gid = self._new_visual_id(page_id, "group")
        group = {
            "$schema": self._VC_SCHEMA_URL,
            "name": gid,
            "position": {"x": min(xs), "y": min(ys), "z": z_min,
                         "width": max(x2) - min(xs),
                         "height": max(y2) - min(ys), "tabOrder": z_min},
            "visualGroup": {"displayName": name, "groupMode": "ScaleMode"},
        }
        self._write_json(visuals_dir / gid / "visual.json", group,
                         validate=False)  # group container has no 'visual'
        for vf, data in members:
            data["parentGroupName"] = gid
            self._write_json(vf, data)
        return gid

    @property
    def _VC_SCHEMA_URL(self) -> str:
        from core.pbir import _VC_SCHEMA
        return _VC_SCHEMA

    # --- page lifecycle -----------------------------------------------------

    def _pages_meta(self) -> tuple[Path, dict]:
        meta_path = (self._require_report() / "definition" / "pages"
                     / "pages.json")
        meta = json.loads(meta_path.read_text(encoding="utf-8-sig")) \
            if meta_path.exists() else {"pageOrder": []}
        return meta_path, meta

    def rename_page(self, page_id: str, new_name: str) -> dict:
        page_json = (self._require_report() / "definition" / "pages"
                     / page_id / "page.json")
        if not page_json.exists():
            raise FileNotFoundError(f"Page {page_id!r} not found")
        data = json.loads(page_json.read_text(encoding="utf-8-sig"))
        data["displayName"] = new_name
        self._write_json(page_json, data)
        return {"ok": True, "page_id": page_id, "name": new_name}

    def hide_page(self, page_id: str, hidden: bool = True) -> dict:
        page_json = (self._require_report() / "definition" / "pages"
                     / page_id / "page.json")
        if not page_json.exists():
            raise FileNotFoundError(f"Page {page_id!r} not found")
        data = json.loads(page_json.read_text(encoding="utf-8-sig"))
        data["visibility"] = "HiddenInViewMode" if hidden else "AlwaysVisible"
        self._write_json(page_json, data)
        return {"ok": True, "page_id": page_id, "hidden": hidden}

    def reorder_pages(self, order: list[str]) -> dict:
        meta_path, meta = self._pages_meta()
        on_disk = {p.id for p in self.list_pages()}
        unknown = [p for p in order if p not in on_disk]
        if unknown:
            raise KeyError(f"Unknown page(s): {unknown}")
        # keep any pages the caller omitted, appended in their current order
        meta["pageOrder"] = order + [p for p in meta.get("pageOrder", [])
                                     if p not in order and p in on_disk]
        self._write_json(meta_path, meta)
        return {"ok": True, "order": meta["pageOrder"]}

    def delete_page(self, page_id: str) -> dict:
        """Delete a page (recoverable: moved to Report/.pbi/mcp-trash/pages)."""
        import shutil
        import time

        pages_dir = self._require_report() / "definition" / "pages"
        pdir = pages_dir / page_id
        if not pdir.is_dir():
            raise FileNotFoundError(f"Page {page_id!r} not found")
        trash = (self._require_report() / ".pbi" / "mcp-trash" / "pages"
                 / f"{page_id}-{time.strftime('%Y%m%d-%H%M%S')}")
        trash.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(pdir), str(trash))
        meta_path, meta = self._pages_meta()
        meta["pageOrder"] = [p for p in meta.get("pageOrder", []) if p != page_id]
        if meta.get("activePageName") == page_id:
            meta["activePageName"] = meta["pageOrder"][0] if meta["pageOrder"] else None
        self._write_json(meta_path, meta)
        return {"ok": True, "deleted": page_id, "recoverable_at": str(trash)}

    def duplicate_page(self, page_id: str, new_name: str | None = None) -> str:
        """Copy a page (and its visuals) to a new page; returns new page id."""
        import shutil

        from core.pbir import slugify

        pages_dir = self._require_report() / "definition" / "pages"
        src = pages_dir / page_id
        if not src.is_dir():
            raise FileNotFoundError(f"Page {page_id!r} not found")
        src_data = json.loads((src / "page.json").read_text(encoding="utf-8-sig"))
        name = new_name or f"{src_data.get('displayName', page_id)} copy"
        existing = {p.name for p in pages_dir.iterdir() if p.is_dir()}
        new_id, n = slugify(name, fallback="page"), 1
        while new_id in existing:
            n += 1
            new_id = f"{slugify(name)}-{n}"
        shutil.copytree(src, pages_dir / new_id)
        # fix the copied page.json name/displayName
        pj = pages_dir / new_id / "page.json"
        data = json.loads(pj.read_text(encoding="utf-8-sig"))
        data["name"] = new_id
        data["displayName"] = name
        self._write_json(pj, data)
        meta_path, meta = self._pages_meta()
        meta.setdefault("pageOrder", []).append(new_id)
        self._write_json(meta_path, meta)
        return new_id

    # --- filter + visual lifecycle -----------------------------------------

    def list_filters(self, scope: str, page_id: str | None = None,
                     visual_id: str | None = None) -> list[dict]:
        report_def = self._require_report() / "definition"
        if scope == "report":
            target = report_def / "report.json"
        elif scope == "page":
            target = report_def / "pages" / page_id / "page.json"
        elif scope == "visual":
            target = self._visual_file(page_id, visual_id)
        else:
            raise ValueError("scope must be report | page | visual")
        if not target.exists():
            return []
        data = json.loads(target.read_text(encoding="utf-8-sig"))
        return [{"name": f.get("name"), "type": f.get("type"),
                 "field": f.get("field")}
                for f in data.get("filterConfig", {}).get("filters", [])]

    def remove_filter(self, scope: str, filter_name: str,
                      page_id: str | None = None,
                      visual_id: str | None = None) -> dict:
        report_def = self._require_report() / "definition"
        if scope == "report":
            target = report_def / "report.json"
        elif scope == "page":
            target = report_def / "pages" / page_id / "page.json"
        elif scope == "visual":
            target = self._visual_file(page_id, visual_id)
        else:
            raise ValueError("scope must be report | page | visual")
        data = json.loads(target.read_text(encoding="utf-8-sig"))
        filters = data.get("filterConfig", {}).get("filters", [])
        kept = [f for f in filters if f.get("name") != filter_name]
        if len(kept) == len(filters):
            raise KeyError(f"Filter {filter_name!r} not found")
        data["filterConfig"]["filters"] = kept
        self._write_json(target, data)
        return {"ok": True, "removed": filter_name, "remaining": len(kept)}

    def list_trash(self) -> list[dict]:
        trash = self._require_report() / ".pbi" / "mcp-trash"
        if not trash.is_dir():
            return []
        out = []
        for kind in ("pages",):
            for d in (trash / kind).glob("*") if (trash / kind).is_dir() else []:
                out.append({"kind": "page", "path": str(d), "name": d.name})
        # trashed visuals live under mcp-trash/<pageId>/<visualId-ts>
        for pdir in trash.iterdir():
            if pdir.name == "pages" or not pdir.is_dir():
                continue
            for vd in pdir.glob("*"):
                out.append({"kind": "visual", "page": pdir.name,
                            "path": str(vd), "name": vd.name})
        return out

    def restore_visual(self, trash_path: str) -> dict:
        """Restore a visual folder from mcp-trash back onto its page."""
        import shutil

        src = Path(trash_path)
        if not src.is_dir() or not (src / "visual.json").exists():
            raise FileNotFoundError(f"No trashed visual at {trash_path}")
        page_id = src.parent.name
        # strip the -timestamp suffix to recover the original id
        original = src.name.rsplit("-", 2)[0]
        dest = (self._require_report() / "definition" / "pages" / page_id
                / "visuals" / original)
        if dest.exists():
            raise ValueError(f"{original} already exists on page {page_id}")
        shutil.move(str(src), str(dest))
        return {"ok": True, "restored": original, "page_id": page_id}

    def add_visual(self, page_id: str, spec: dict) -> str:
        """Add a visual from a spec; return its visualId.

        spec = {
          "visual_type": "card",
          "bindings": {"Values": ["Sales.Net Revenue"]},   # bucket -> queryRefs
          "position": {"x":..,"y":..,"width":..,"height":..},  # optional
          "title": "Total Revenue",                            # optional
          "id": "card1",                                       # optional
        }
        """
        from core.pbir import build_visual_json, slugify

        visual_type = spec["visual_type"]
        bindings = spec.get("bindings", {})
        visuals_dir = (
            self._require_report() / "definition" / "pages" / page_id / "visuals"
        )
        existing = {p.name for p in visuals_dir.iterdir() if p.is_dir()} \
            if visuals_dir.is_dir() else set()
        base = spec.get("id") or slugify(visual_type, fallback="visual")
        visual_id, n = base, 1
        while visual_id in existing:
            n += 1
            visual_id = f"{base}-{n}"

        obj = build_visual_json(
            visual_id, visual_type, bindings, self._is_measure,
            position=spec.get("position"), title=spec.get("title"),
        )
        self._write_json(visuals_dir / visual_id / "visual.json", obj)
        return visual_id

    def _visual_file(self, page_id: str, visual_id: str) -> Path:
        f = (self._require_report() / "definition" / "pages" / page_id
             / "visuals" / visual_id / "visual.json")
        if not f.exists():
            raise FileNotFoundError(
                f"No visual {visual_id!r} on page {page_id!r}")
        return f

    def update_bindings(self, page_id: str, visual_id: str,
                        bindings: dict) -> dict:
        """Replace a visual's queryState from {bucket: [queryRef,...]}.

        Everything else in visual.json (position, formatting, title) is kept.
        """
        from core.pbir import build_query_state

        vfile = self._visual_file(page_id, visual_id)
        data = json.loads(vfile.read_text(encoding="utf-8-sig"))
        visual = data.setdefault("visual", {})
        visual.setdefault("query", {})["queryState"] = build_query_state(
            bindings, self._is_measure)
        self._write_json(vfile, data)
        return {"ok": True, "page_id": page_id, "visual_id": visual_id,
                "bindings": bindings}

    def move_visual(self, page_id: str, visual_id: str,
                    x: float | None = None, y: float | None = None,
                    width: float | None = None,
                    height: float | None = None) -> dict:
        """Reposition/resize a visual; only the fields you pass change."""
        vfile = self._visual_file(page_id, visual_id)
        data = json.loads(vfile.read_text(encoding="utf-8-sig"))
        pos = data.setdefault("position", {})
        for key, val in [("x", x), ("y", y),
                         ("width", width), ("height", height)]:
            if val is not None:
                pos[key] = val
        self._write_json(vfile, data)
        return {"ok": True, "page_id": page_id, "visual_id": visual_id,
                "position": pos}

    def delete_visual(self, page_id: str, visual_id: str) -> dict:
        """Delete a visual — recoverably: its folder moves to .pbi/mcp-trash/.

        Desktop ignores the .pbi directory, so the trashed copy never affects
        the report but can be restored by moving it back.
        """
        import shutil
        import time

        vfile = self._visual_file(page_id, visual_id)
        vdir = vfile.parent
        trash = (self._require_report() / ".pbi" / "mcp-trash" / page_id
                 / f"{visual_id}-{time.strftime('%Y%m%d-%H%M%S')}")
        trash.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(vdir), str(trash))
        return {"ok": True, "action": "deleted", "page_id": page_id,
                "visual_id": visual_id, "recoverable_at": str(trash)}

    def sort_visual(self, page_id: str, visual_id: str, field: str,
                    direction: str = "Descending", is_measure: bool = True) -> dict:
        """Set a visual's sort field + direction."""
        from core.interact import build_sort_definition

        vfile = self._visual_file(page_id, visual_id)
        data = json.loads(vfile.read_text(encoding="utf-8-sig"))
        data.setdefault("visual", {}).setdefault("query", {})[
            "sortDefinition"] = build_sort_definition(field, direction, is_measure)
        self._write_json(vfile, data)
        return {"ok": True, "visual_id": visual_id, "sort": field,
                "direction": direction}

    def add_nav_button(self, page_id: str, label: str, target_page_id: str,
                       position: dict | None = None, fill: str = "#1F3A5F",
                       text_color: str = "#FFFFFF") -> str:
        """Add a page-navigation button; returns its visualId."""
        from core.interact import build_nav_button

        if target_page_id not in {p.id for p in self.list_pages()}:
            raise KeyError(f"Target page {target_page_id!r} not found")
        obj = build_nav_button("navbutton", label, target_page_id,
                               position, fill, text_color)
        return self._add_raw_visual(page_id, "navbutton", obj)

    def set_page_role(self, page_id: str, role: str,
                      tooltip_size: tuple[int, int] | None = None) -> dict:
        """Mark a page as a drillthrough or tooltip page (pageBinding)."""
        roles = {"drillthrough": "Drillthrough", "tooltip": "Tooltip",
                 "default": "Default"}
        if role not in roles:
            raise ValueError(f"role must be one of {sorted(roles)}")
        page_json = (self._require_report() / "definition" / "pages"
                     / page_id / "page.json")
        if not page_json.exists():
            raise FileNotFoundError(f"Page {page_id!r} not found")
        data = json.loads(page_json.read_text(encoding="utf-8-sig"))
        data["pageBinding"] = {"name": page_id, "type": roles[role],
                               "parameters": []}
        if role == "tooltip":
            w, h = tooltip_size or (320, 240)
            data["width"], data["height"] = w, h
            data["displayOption"] = "ActualSize"
        self._write_json(page_json, data)
        return {"ok": True, "page_id": page_id, "role": roles[role]}

    def set_visual_interactions(self, page_id: str, source_visual: str,
                                interactions: dict) -> dict:
        """Set how a source visual cross-filters others.

        interactions: {target_visual_id: "Filter"|"Highlight"|"NoFilter"}
        stored on page.json visualInteractions[] (schema string enum).
        """
        types = {"Filter": "DataFilter", "Highlight": "HighlightFilter",
                 "NoFilter": "NoFilter", "Default": "Default"}
        page_json = (self._require_report() / "definition" / "pages"
                     / page_id / "page.json")
        if not page_json.exists():
            raise FileNotFoundError(f"Page {page_id!r} not found")
        data = json.loads(page_json.read_text(encoding="utf-8-sig"))
        existing = {(i.get("source"), i.get("target")): i
                    for i in data.get("visualInteractions", [])}
        for target, mode in interactions.items():
            if mode not in types:
                raise ValueError(f"interaction must be one of {sorted(types)}")
            existing[(source_visual, target)] = {
                "source": source_visual, "target": target,
                "type": types[mode]}
        data["visualInteractions"] = list(existing.values())
        self._write_json(page_json, data)
        return {"ok": True, "page_id": page_id, "source": source_visual,
                "interactions": interactions}

    def create_bookmark(self, name: str, display_name: str | None = None,
                        page_id: str | None = None) -> dict:
        """Capture the current report state as a bookmark.

        Captures active page + each page's filter state (page/visual filters
        already on disk). Registers it in bookmarks/bookmarks.json.
        """
        import re as _re
        import uuid

        report_def = self._require_report() / "definition"
        bookmarks_dir = report_def / "bookmarks"
        bookmarks_dir.mkdir(exist_ok=True)

        pages = self.list_pages()
        active = page_id or (pages[0].id if pages else None)
        sections = {}
        for pg in pages:
            page_json = report_def / "pages" / pg.id / "page.json"
            pdata = json.loads(page_json.read_text(encoding="utf-8-sig"))
            sections[pg.id] = {"visualContainers": {}}
            if pdata.get("filterConfig"):
                sections[pg.id]["filters"] = pdata["filterConfig"]

        bm_id = "Bookmark" + uuid.uuid4().hex[:20]
        slug = _re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_") or bm_id
        bookmark = {
            "$schema": "https://developer.microsoft.com/json-schemas/fabric/"
                       "item/report/definition/bookmark/1.0.0/schema.json",
            "displayName": display_name or name,
            "name": bm_id,
            "options": {"targetVisualNames": []},
            "explorationState": {"version": "1.3", "activeSection": active,
                                 "sections": sections},
        }
        self._write_json(bookmarks_dir / f"{slug}.json", bookmark,
                         validate=False)

        # register in bookmarks metadata
        meta_path = bookmarks_dir / "bookmarks.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8-sig")) \
            if meta_path.exists() else {
                "$schema": "https://developer.microsoft.com/json-schemas/fabric/"
                           "item/report/definition/bookmarksMetadata/1.0.0/schema.json",
                "items": []}
        meta.setdefault("items", []).append({"name": slug})
        self._write_json(meta_path, meta, validate=False)
        return {"ok": True, "bookmark": bm_id, "name": name, "file": f"{slug}.json"}

    def format_visual(self, page_id: str, visual_id: str, target: str,
                      objects: dict) -> dict:
        """Merge formatting properties into a visual.

        target="container" -> visualContainerObjects (title, background,
        border, chrome); target="visual" -> objects (axes, legend, labels).
        `objects` = {objectName: {prop: plain value or prebuilt expr dict}}.
        """
        from core.formatting import build_objects_patch, merge_objects

        if target not in ("container", "visual"):
            raise ValueError("target must be 'container' or 'visual'")
        key = "visualContainerObjects" if target == "container" else "objects"
        vfile = self._visual_file(page_id, visual_id)
        data = json.loads(vfile.read_text(encoding="utf-8-sig"))
        visual = data.setdefault("visual", {})
        visual[key] = merge_objects(visual.get(key, {}),
                                    build_objects_patch(objects))
        self._write_json(vfile, data)
        return {"ok": True, "visual_id": visual_id, "target": target,
                "objects": sorted(objects)}

    def set_report_theme(self, theme: dict) -> dict:
        """Install a custom theme JSON and reference it in report.json."""
        theme_name = theme.get("name")
        if not theme_name:
            raise ValueError("Theme JSON needs a 'name' field")
        report_dir = self._require_report()
        res_dir = report_dir / "StaticResources" / "RegisteredResources"
        file_name = f"{theme_name}.json"
        self._write_json(res_dir / file_name, theme)

        report_json = report_dir / "definition" / "report.json"
        data = json.loads(report_json.read_text(encoding="utf-8-sig")) \
            if report_json.exists() else {}
        tc = data.setdefault("themeCollection", {})
        # reportVersionAtImport is REQUIRED (Desktop schema). Real files show
        # it's an object of layer versions — reuse the project's own if any
        # theme already records one, else a current-era default.
        version_at_import = None
        for existing in tc.values():
            if isinstance(existing, dict) and \
                    existing.get("reportVersionAtImport"):
                version_at_import = existing["reportVersionAtImport"]
                break
        if version_at_import is None:
            version_at_import = {"visual": "2.9.0", "report": "3.3.0",
                                 "page": "2.3.1"}
        tc["customTheme"] = {
            "name": file_name,
            "reportVersionAtImport": version_at_import,
            "type": "RegisteredResources",
        }
        self._write_json(report_json, data)
        return {"ok": True, "theme": theme_name, "resource": file_name}

    def add_filter(self, scope: str, filter_entry: dict,
                   page_id: str | None = None,
                   visual_id: str | None = None) -> dict:
        """Append a filter to report.json / page.json / visual.json
        filterConfig. `filter_entry` comes from core.formatting.build_filter.
        """
        report_def = self._require_report() / "definition"
        if scope == "report":
            target = report_def / "report.json"
        elif scope == "page":
            if not page_id:
                raise ValueError("page scope needs page_id")
            target = report_def / "pages" / page_id / "page.json"
            if not target.exists():
                raise FileNotFoundError(f"Page {page_id!r} not found")
        elif scope == "visual":
            if not (page_id and visual_id):
                raise ValueError("visual scope needs page_id and visual_id")
            target = self._visual_file(page_id, visual_id)
        else:
            raise ValueError("scope must be report | page | visual")

        data = json.loads(target.read_text(encoding="utf-8-sig")) \
            if target.exists() else {}
        fc = data.setdefault("filterConfig", {})
        fc.setdefault("filters", []).append(filter_entry)
        self._write_json(target, data)
        return {"ok": True, "scope": scope,
                "filter": filter_entry["name"], "file": target.name}

    def validate_project(self) -> dict:
        """Validate every report-layer JSON against the Fabric schemas.

        Returns {ok, checked, errors: [{file, messages}]}. Read-only.
        """
        from core import schema_validate

        if not schema_validate.is_available():
            return {"ok": True, "checked": 0, "errors": [],
                    "note": "jsonschema/schemas unavailable — validation skipped"}
        report_def = self._require_report() / "definition"
        problems = []
        checked = 0
        targets = [(report_def / "report.json", "report")]
        pages_dir = report_def / "pages"
        if (pages_dir / "pages.json").exists():
            targets.append((pages_dir / "pages.json", "pagesMetadata"))
        if pages_dir.is_dir():
            for pj in pages_dir.glob("*/page.json"):
                targets.append((pj, "page"))
            for vj in pages_dir.glob("*/visuals/*/visual.json"):
                targets.append((vj, "visualContainer"))
        for path, kind in targets:
            if not path.exists():
                continue
            checked += 1
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            errs = schema_validate.validate(kind, data)
            if errs:
                problems.append({
                    "file": str(path.relative_to(report_def)),
                    "kind": kind, "messages": errs[:10]})
        return {"ok": not problems, "checked": checked, "errors": problems}

    def save(self) -> None:
        """No-op checkpoint: mutations are already applied atomically on call.

        Kept for API symmetry (B.2) and as a future hook for batched commits.
        """
        return None
