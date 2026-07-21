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
        self._backed_up: set[Path] = set()
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

    def list_tables(self) -> list[Table]:
        from core.tmdl import parse_table_file

        tables_dir = self._require_model() / "definition" / "tables"
        if not tables_dir.is_dir():
            return []
        return [
            parse_table_file(f)
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

    def _write_json(self, path: Path, obj: dict) -> None:
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
        return {"ok": True, "action": "created", "table": table, "name": name}

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
        self.upsert_measure(
            table, name,
            dax if dax is not None else existing.dax,
            fmt if fmt is not None else existing.format_string,
            display_folder if display_folder is not None else existing.display_folder,
        )
        return {"ok": True, "action": "updated", "table": table, "name": name}

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
        return {"ok": True, "action": "created",
                "count": len(measures),
                "tables": sorted(by_table)}

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
        meta = json.loads(meta_path.read_text(encoding="utf-8-sig")) \
            if meta_path.exists() else {"$schema": "", "pageOrder": []}
        order = meta.get("pageOrder", [])
        if page_id not in order:
            order.append(page_id)
        meta["pageOrder"] = order
        meta.setdefault("activePageName", page_id)
        self._write_json(meta_path, meta)
        return page_id

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
        tc["customTheme"] = {"name": file_name, "type": "RegisteredResources"}
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

    def save(self) -> None:
        """No-op checkpoint: mutations are already applied atomically on call.

        Kept for API symmetry (B.2) and as a future hook for batched commits.
        """
        return None
