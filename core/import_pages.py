"""Import pages / visuals from another Power BI project into the selected one.

``import_pages`` copies whole report pages (page.json, every visual folder,
extra per-visual files such as ``mobile.json``), the registered static
resources they reference (images under ``StaticResources/RegisteredResources``,
re-registered in ``report.json`` ``resourcePackages`` when the source
registered them), and the bookmarks that navigate to them. ``import_visuals``
copies individual visuals onto an existing page.

Rules shared by both:

* Nothing is written until everything has been planned and validated: the
  fields the incoming visuals/filters bind are checked against the target
  model (missing fields refuse the import unless ``allow_missing_fields``),
  and every JSON that will be written is validated against the vendored
  Fabric schema up front. If a write still fails midway, files already
  written are removed / restored before the error propagates.
* Ids are kept when free. A page whose id is taken gets a new id derived like
  ``create_page`` / ``duplicate_page`` do (slug of its display name, ``-n``
  suffix); a visual whose id is taken on the target page gets ``<id>-n``.
  Generated ids are capped at 50 characters (the schema limit). References
  that must follow a renamed id are rewritten: ``page.json`` ``name`` and
  ``pageBinding.name``, page-navigation buttons (``navigationSection``),
  bookmark links, bookmark ``activeSection`` / ``sections``, and
  ``parentGroupName`` for grouped visuals.
* Resources with the same name and identical bytes are reused; the same name
  with different bytes is copied under a fresh name and the references are
  rewritten.

Visual ids are unique per page (PBIR scopes them to the page), so a page
imported into a fresh folder never needs its visuals renamed.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
from pathlib import Path

from core import schema_validate
from core.pbir import _PAGES_SCHEMA, slugify
from core.usage import _walk_field_refs

MAX_ID_LEN = 50
_BOOKMARKS_SCHEMA = ("https://developer.microsoft.com/json-schemas/fabric/item/"
                     "report/definition/bookmarksMetadata/1.0.0/schema.json")


# --- small helpers ---------------------------------------------------------------

def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _read_json_or(path: Path, default):
    try:
        return _read_json(path)
    except FileNotFoundError:
        return default


def _unique_id(base: str, taken: set[str], fallback: str = "item") -> str:
    """`base` (capped at 50 chars) or `base-2`, `base-3`... not in `taken`."""
    base = base or fallback
    cand = base[:MAX_ID_LEN]
    n = 1
    while cand in taken:
        n += 1
        suffix = f"-{n}"
        cand = base[:MAX_ID_LEN - len(suffix)] + suffix
    return cand


def _iter_files(root: Path):
    """(posix relative path, Path) for every non-backup file under `root`."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != ".pbi")
        for name in sorted(filenames):
            if ".bak-" in name or ".tmp-" in name:
                continue
            path = Path(dirpath) / name
            yield path.relative_to(root).as_posix(), path


def _safe_rel(rel: str) -> str:
    p = Path(rel.replace("\\", "/"))
    if p.is_absolute() or ".." in p.parts or not p.parts or ":" in rel:
        raise ValueError(f"Unsafe resource path {rel!r} in the source report")
    return p.as_posix()


def _literal_string(node) -> str | None:
    """'text' from {"expr": {"Literal": {"Value": "'text'"}}}, else None."""
    try:
        raw = node["expr"]["Literal"]["Value"]
    except (KeyError, TypeError):
        return None
    if isinstance(raw, str) and len(raw) >= 2 and raw[0] == raw[-1] == "'":
        return raw[1:-1]
    return None


def _validate_all(project, items: list[tuple[str, str, dict]]) -> None:
    """Schema-validate (kind, label, obj) triples; raise listing all failures."""
    if not (project.preflight and schema_validate.is_available()):
        return
    problems = []
    for kind, label, obj in items:
        errs = schema_validate.validate(kind, obj)
        if errs:
            problems.append(f"{label}: {'; '.join(errs[:3])}")
    if problems:
        shown = "; ".join(problems[:6])
        more = f" (+{len(problems) - 6} more)" if len(problems) > 6 else ""
        raise ValueError(
            f"Imported content fails schema validation, nothing was written: "
            f"{shown}{more}")


# --- field check --------------------------------------------------------------------

def _extension_fields(report_dir: Path) -> set[tuple[str, str]]:
    """(entity, measure) pairs defined in reportExtensions.json."""
    ext = _read_json_or(report_dir / "definition" / "reportExtensions.json", {})
    out: set[tuple[str, str]] = set()
    if isinstance(ext, dict):
        for entity in ext.get("entities") or []:
            if isinstance(entity, dict):
                for m in entity.get("measures") or []:
                    if isinstance(m, dict) and m.get("name"):
                        out.add((str(entity.get("name")), str(m["name"])))
    return out


def find_missing_fields(target, source, objs: list[tuple[str, dict]]
                        ) -> list[dict] | None:
    """Fields bound by `objs` that the target model lacks.

    `objs` is [(label, json)]; every (Entity, Property) reference anywhere in
    the JSON (bindings, filters, sorts, conditional formatting...) is checked.
    Returns None when the target has no semantic model to check against.
    """
    try:
        target._require_model()
    except FileNotFoundError:
        return None
    known: set[tuple[str, str]] = set()
    for t in target.list_tables():
        known.update((t.name, c.name) for c in t.columns)
        known.update((m.table, m.name) for m in t.measures)
    try:
        known |= _extension_fields(target._require_report())
    except FileNotFoundError:
        pass
    try:
        source_ext = _extension_fields(source._require_report())
    except FileNotFoundError:
        source_ext = set()
    used: dict[tuple[str, str], set[str]] = {}
    for label, obj in objs:
        for ref in _walk_field_refs(obj):
            if ref not in known:
                used.setdefault(ref, set()).add(label)
    out = []
    for (entity, prop), labels in sorted(used.items()):
        entry = {"field": f"{entity}.{prop}", "used_in": sorted(labels)[:5]}
        if (entity, prop) in source_ext:
            entry["note"] = ("report-level measure defined in the source "
                             "report's reportExtensions.json (not imported)")
        out.append(entry)
    return out


def _check_fields(target, source, objs, allow_missing: bool,
                  warnings: list[str]) -> list[dict]:
    missing = find_missing_fields(target, source, objs)
    if missing is None:
        warnings.append("field check skipped: no semantic model found next to "
                        "the target report")
        return []
    if missing and not allow_missing:
        names = ", ".join(m["field"] for m in missing[:15])
        more = f" (+{len(missing) - 15} more)" if len(missing) > 15 else ""
        raise ValueError(
            f"Refusing to import: {len(missing)} field(s) used by the "
            f"imported content do not exist in the target model: {names}{more}. "
            f"Add them to the model, or pass allow_missing_fields=true to "
            f"import anyway.")
    return missing


# --- static resources ---------------------------------------------------------------

def _resource_refs(node, out: list[dict] | None = None) -> list[dict]:
    """Every ResourcePackageItem dict inside `node` (by reference)."""
    out = [] if out is None else out
    if isinstance(node, dict):
        rpi = node.get("ResourcePackageItem")
        if isinstance(rpi, dict):
            out.append(rpi)
        for v in node.values():
            _resource_refs(v, out)
    elif isinstance(node, list):
        for v in node:
            _resource_refs(v, out)
    return out


def _registered_items(report_json: dict) -> dict[str, dict]:
    items: dict[str, dict] = {}
    for pkg in report_json.get("resourcePackages") or []:
        if isinstance(pkg, dict) and (pkg.get("name") == "RegisteredResources"
                                      or pkg.get("type") == "RegisteredResources"):
            for item in pkg.get("items") or []:
                if isinstance(item, dict) and item.get("name"):
                    items[item["name"]] = item
    return items


def _variant(rel: str, taken: set[str]) -> str:
    """`dir/stem-2.ext` style unused relative path."""
    p = Path(rel)
    n = 1
    cand = rel
    while cand.lower() in taken:
        n += 1
        cand = (p.parent / f"{p.stem}-{n}{p.suffix}").as_posix()
    return cand


def _plan_resources(source, target, objs: list[dict],
                    warnings: list[str]) -> dict:
    """Decide which registered resources to copy/reuse/rename for `objs`.

    Rewrites ItemName in `objs` (already private deep copies) when a resource
    must be renamed. Returns {"copies": [(src, dst)], "register": [items],
    "copied": [...], "reused": [...], "renamed": {old: new}}.
    """
    src_dir, dst_dir = source._require_report(), target._require_report()
    src_reg = src_dir / "StaticResources" / "RegisteredResources"
    dst_reg = dst_dir / "StaticResources" / "RegisteredResources"
    src_items = _registered_items(_read_json_or(
        src_dir / "definition" / "report.json", {}))
    dst_items = _registered_items(_read_json_or(
        dst_dir / "definition" / "report.json", {}))

    refs = [rpi for obj in objs for rpi in _resource_refs(obj)
            if rpi.get("PackageName") == "RegisteredResources"
            and isinstance(rpi.get("ItemName"), str)]
    names = sorted({r["ItemName"] for r in refs})

    taken_files = {rel.lower() for rel, _ in _iter_files(dst_reg)} \
        if dst_reg.is_dir() else set()
    taken_names = {n.lower() for n in dst_items} | taken_files
    plan = {"copies": [], "register": [], "copied": [], "reused": [],
            "renamed": {}}
    rename: dict[str, str] = {}
    for name in names:
        item = src_items.get(name)
        rel = _safe_rel((item or {}).get("path") or name)
        src_file = src_reg / rel
        if not src_file.is_file():
            warnings.append(f"resource {name!r} is referenced but "
                            f"{src_file} does not exist; reference left as is")
            continue
        dst_file = dst_reg / rel
        new_rel, new_name = rel, name
        if dst_file.exists():
            if dst_file.read_bytes() == src_file.read_bytes():
                plan["reused"].append(name)
            else:
                new_rel = _variant(rel, taken_files)
                new_name = new_rel if name == rel else \
                    _variant(name, taken_names)
                if new_name != name:
                    plan["renamed"][name] = new_name
                    rename[name] = new_name
        if new_rel != rel or not dst_file.exists():
            plan["copies"].append((src_file, dst_reg / new_rel))
            plan["copied"].append(new_name)
            taken_files.add(new_rel.lower())
        taken_names.add(new_name.lower())
        if item is not None and new_name not in dst_items:
            plan["register"].append({**item, "name": new_name, "path": new_rel})
    for r in refs:
        if r["ItemName"] in rename:
            r["ItemName"] = rename[r["ItemName"]]
    return plan


def _apply_report_registration(target, source, plan: dict,
                               visual_types: set[str],
                               warnings: list[str]) -> dict | None:
    """The updated target report.json (resource items + custom visuals), or
    None when nothing needs registering."""
    src_report = _read_json_or(
        source._require_report() / "definition" / "report.json", {})
    report_path = target._require_report() / "definition" / "report.json"
    report = copy.deepcopy(_read_json_or(report_path, None))
    changed = False

    if plan["register"]:
        if report is None:
            warnings.append("target has no report.json; imported resources "
                            "were copied but not registered")
        else:
            packages = report.setdefault("resourcePackages", [])
            pkg = next((p for p in packages if isinstance(p, dict)
                        and (p.get("name") == "RegisteredResources"
                             or p.get("type") == "RegisteredResources")), None)
            if pkg is None:
                pkg = {"name": "RegisteredResources",
                       "type": "RegisteredResources", "items": []}
                packages.append(pkg)
            have = {i.get("name") for i in pkg.setdefault("items", [])
                    if isinstance(i, dict)}
            for item in plan["register"]:
                if item["name"] not in have:
                    pkg["items"].append(item)
                    changed = True

    custom = sorted(t for t in visual_types if t in
                    set(src_report.get("publicCustomVisuals") or []))
    if custom and report is not None:
        public = report.setdefault("publicCustomVisuals", [])
        for t in custom:
            if t not in public:
                public.append(t)
                changed = True
    org = {o.get("name"): o for o in src_report.get("organizationCustomVisuals")
           or [] if isinstance(o, dict)}
    for t in sorted(visual_types & set(org)):
        if report is not None:
            lst = report.setdefault("organizationCustomVisuals", [])
            if all(o.get("name") != t for o in lst if isinstance(o, dict)):
                lst.append(copy.deepcopy(org[t]))
                changed = True
    private = {p.get("name") for p in src_report.get("resourcePackages") or []
               if isinstance(p, dict) and p.get("type") == "CustomVisual"}
    tgt_private = {p.get("name") for p in (report or {}).get("resourcePackages")
                   or [] if isinstance(p, dict)}
    for t in sorted(visual_types & (private - tgt_private)):
        warnings.append(f"visual type {t!r} is a private custom visual whose "
                        f"package is not in the target report; copy its "
                        f"resource package or the visual will not render")
    return report if changed else None


# --- writer with rollback --------------------------------------------------------------

class _Writer:
    """Writes through PbipProject helpers and can undo a failed import."""

    def __init__(self, project, stop_dir: Path):
        self.p = project
        self.stop = stop_dir
        self.created: list[Path] = []
        self.saved: dict[Path, bytes] = {}

    def _track(self, path: Path) -> None:
        if path in self.saved or path in self.created:
            return
        if path.exists():
            self.saved[path] = path.read_bytes()
        else:
            self.created.append(path)

    def json(self, path: Path, obj: dict, *, validate: bool = True) -> None:
        self._track(path)
        self.p._write_json(path, obj, validate=validate)

    def copy(self, src: Path, dst: Path) -> None:
        self._track(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(f"{dst.name}.tmp-{os.getpid()}")
        try:
            shutil.copyfile(src, tmp)
            os.replace(tmp, dst)
        finally:
            if tmp.exists():
                tmp.unlink()

    def rollback(self) -> None:
        for path in reversed(self.created):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            parent = path.parent
            while parent != self.stop and parent.exists() \
                    and self.stop in parent.parents and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent
        for path, data in self.saved.items():
            tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
            tmp.write_bytes(data)
            os.replace(tmp, path)


# --- link rewriting ------------------------------------------------------------------

def _remap_links(node, page_map: dict[str, str], bookmark_map: dict[str, str],
                 nav_targets: set[str]) -> None:
    """Rewrite page-navigation / bookmark links inside a visual JSON."""
    if isinstance(node, dict):
        for key, val in node.items():
            if key in ("navigationSection", "bookmark") and isinstance(val, dict):
                ident = _literal_string(val)
                if ident is None:
                    continue
                mapping = page_map if key == "navigationSection" else bookmark_map
                if ident in mapping and mapping[ident] != ident:
                    val["expr"]["Literal"]["Value"] = f"'{mapping[ident]}'"
                    ident = mapping[ident]
                if key == "navigationSection":
                    nav_targets.add(ident)
            else:
                _remap_links(val, page_map, bookmark_map, nav_targets)
    elif isinstance(node, list):
        for v in node:
            _remap_links(v, page_map, bookmark_map, nav_targets)


# --- source loading ----------------------------------------------------------------

def _load_source_page(defn: Path, pid: str) -> dict:
    pdir = defn / "pages" / pid
    page_json = _read_json(pdir / "page.json")
    visuals: dict[str, dict] = {}
    extras: dict[str, Path] = {}
    vextras: dict[str, dict[str, Path]] = {}
    for rel, path in _iter_files(pdir):
        if rel == "page.json":
            continue
        parts = rel.split("/")
        if len(parts) == 3 and parts[0] == "visuals" and parts[2] == "visual.json":
            visuals[parts[1]] = _read_json(path)
        elif len(parts) >= 3 and parts[0] == "visuals":
            vextras.setdefault(parts[1], {})["/".join(parts[2:])] = path
        else:
            extras[rel] = path
    return {"json": page_json, "visuals": visuals, "extras": extras,
            "vextras": vextras}


def _source_bookmarks(defn: Path) -> list[dict]:
    """[{"path", "stem", "suffix", "data"}] for every source bookmark file."""
    out = []
    bdir = defn / "bookmarks"
    if not bdir.is_dir():
        return out
    for path in sorted(bdir.glob("*.json")):
        if path.name == "bookmarks.json":
            continue
        data = _read_json(path)
        if not isinstance(data, dict):
            continue
        suffix = ".bookmark.json" if path.name.endswith(".bookmark.json") \
            else ".json"
        out.append({"path": path, "stem": path.name[:-len(suffix)],
                    "suffix": suffix, "data": data})
    return out


def _target_bookmark_ids(defn: Path) -> tuple[set[str], set[str]]:
    """(names, file stems) already used by the target's bookmarks."""
    names: set[str] = set()
    stems: set[str] = set()
    for b in _source_bookmarks(defn):
        stems.add(b["stem"])
        if b["data"].get("name"):
            names.add(str(b["data"]["name"]))
    meta = _read_json_or(defn / "bookmarks" / "bookmarks.json", {})
    for item in (meta.get("items") or []) if isinstance(meta, dict) else []:
        if isinstance(item, dict) and item.get("name"):
            stems.add(str(item["name"]))
    return names, stems


# --- import_pages -------------------------------------------------------------------------

def import_pages(target, source, page_ids: list[str],
                 rename_map: dict[str, str] | None = None,
                 include_bookmarks: bool = True,
                 allow_missing_fields: bool = False,
                 position: int | None = None) -> dict:
    """Copy pages from `source` into `target` (both ``PbipProject``).

    See the module docstring for the rules. Returns ``{"ok", "id_map"
    (old page id -> new), "pages": [...], "bookmarks", "resources",
    "order", "missing_fields", "warnings"}``.
    """
    if not page_ids:
        raise ValueError("page_ids must list at least one page to import")
    if len(set(page_ids)) != len(page_ids):
        raise ValueError("page_ids contains duplicates")
    rename_map = dict(rename_map or {})
    if position is not None and (isinstance(position, bool)
                                 or not isinstance(position, int)
                                 or position < 0):
        raise ValueError("position must be an integer >= 0 (0-based index in "
                         "the target page order) or omitted to append")
    stray = sorted(set(rename_map) - set(page_ids))
    if stray:
        raise ValueError(f"rename_map has keys that are not in page_ids: {stray}")
    for k, v in rename_map.items():
        if not isinstance(v, str) or not v.strip():
            raise ValueError(f"rename_map[{k!r}] must be a non-empty string")

    src_defn = source._require_report() / "definition"
    tgt_report = target._require_report()
    tgt_defn = tgt_report / "definition"
    src_order = [p.id for p in source.list_pages()]
    unknown = [p for p in page_ids if p not in src_order]
    if unknown:
        raise ValueError(f"Page(s) {unknown} not found in the source report "
                         f"(available: {src_order})")
    selected = [p for p in src_order if p in set(page_ids)]   # source order

    warnings: list[str] = []
    src = {pid: _load_source_page(src_defn, pid) for pid in selected}
    display = {pid: rename_map.get(pid)
               or src[pid]["json"].get("displayName")
               or src[pid]["json"].get("name") or pid for pid in selected}

    # --- ids -----------------------------------------------------------------
    tgt_pages_dir = tgt_defn / "pages"
    existing = {p.name for p in tgt_pages_dir.iterdir() if p.is_dir()} \
        if tgt_pages_dir.is_dir() else set()
    taken = existing | {p for p in selected if p not in existing}
    page_map: dict[str, str] = {}
    for pid in selected:
        if pid in existing:
            new = _unique_id(slugify(display[pid], fallback="page"), taken, "page")
            taken.add(new)
        else:
            new = pid
        page_map[pid] = new
    current_order = [p.id for p in target.list_pages()]
    existing_names = {p.name for p in target.list_pages()}
    for pid in selected:
        if display[pid] in existing_names:
            warnings.append(f"display name {display[pid]!r} already exists in "
                            f"the target report; pass rename_map to rename it")

    # --- bookmarks (planned before link rewriting so links can follow) --------
    bookmark_plan: list[dict] = []
    bookmark_map: dict[str, str] = {}
    skipped_bookmarks: list[dict] = []
    if include_bookmarks:
        used_names, used_stems = _target_bookmark_ids(tgt_defn)
        for b in _source_bookmarks(src_defn):
            es = b["data"].get("explorationState")
            active = es.get("activeSection") if isinstance(es, dict) else None
            if active not in page_map:
                continue
            old_name = str(b["data"].get("name") or b["stem"])
            if old_name == b["stem"]:
                new_name = _unique_id(old_name, used_names | used_stems)
                new_stem = new_name
            else:
                new_name = _unique_id(old_name, used_names)
                new_stem = _unique_id(b["stem"], used_stems)
            used_names.add(new_name)
            used_stems.add(new_stem)
            bookmark_map[old_name] = new_name
            bookmark_plan.append({"src": b, "old_name": old_name,
                                  "name": new_name, "stem": new_stem})

    # bookmark JSON: validated here so a skipped bookmark never leaves a
    # dangling bookmark link in the imported visuals.
    bookmark_files: list[tuple[Path, dict, dict]] = []
    for entry in bookmark_plan:
        data = copy.deepcopy(entry["src"]["data"])
        es = data["explorationState"]
        es["activeSection"] = page_map[es["activeSection"]]
        if isinstance(es.get("sections"), dict):
            es["sections"] = {page_map[k]: v for k, v in es["sections"].items()
                              if k in page_map}
        data["name"] = entry["name"]
        errs = schema_validate.validate("bookmark", data) \
            if target.preflight else []
        if errs:
            skipped_bookmarks.append({"id": entry["old_name"],
                                      "reason": "; ".join(errs[:3])})
            bookmark_map.pop(entry["old_name"], None)
            continue
        path = tgt_defn / "bookmarks" / f"{entry['stem']}{entry['src']['suffix']}"
        bookmark_files.append((path, data, entry))
    if skipped_bookmarks:
        warnings.append(f"{len(skipped_bookmarks)} bookmark(s) skipped: they "
                        f"fail schema validation")

    # --- rewrite page + visual JSON ------------------------------------------------
    nav_targets: set[str] = set()
    pages_out: list[dict] = []
    all_objs: list[tuple[str, dict]] = []
    for pid in selected:
        new_id = page_map[pid]
        pj = copy.deepcopy(src[pid]["json"])
        pj["name"] = new_id
        pj["displayName"] = display[pid]
        binding = pj.get("pageBinding")
        if isinstance(binding, dict) and binding.get("name") == pid:
            binding["name"] = new_id
        visuals = {}
        for vid, vj in src[pid]["visuals"].items():
            vj = copy.deepcopy(vj)
            vj["name"] = vid
            _remap_links(vj, page_map, bookmark_map, nav_targets)
            visuals[vid] = vj
            all_objs.append((f"{pid}/{vid}", vj))
        all_objs.append((pid, pj))
        pages_out.append({"old": pid, "new": new_id, "json": pj,
                          "visuals": visuals})

    missing = _check_fields(target, source, all_objs, allow_missing_fields,
                            warnings)
    plan = _plan_resources(source, target, [o for _, o in all_objs], warnings)
    visual_types = {vj["visual"]["visualType"]
                    for p in pages_out for vj in p["visuals"].values()
                    if isinstance(vj.get("visual"), dict)
                    and vj["visual"].get("visualType")}
    report_update = _apply_report_registration(target, source, plan,
                                               visual_types, warnings)

    final_pages = set(current_order) | set(page_map.values())
    dangling = sorted(t for t in nav_targets if t not in final_pages)
    if dangling:
        warnings.append(f"imported navigation buttons point at page(s) "
                        f"{dangling} that do not exist in the target report")

    # --- validation before any write -----------------------------------------------------
    checks: list[tuple[str, str, dict]] = []
    for p in pages_out:
        checks.append(("page", f"{p['new']}/page.json", p["json"]))
        for vid, vj in p["visuals"].items():
            checks.append(("visualContainer",
                           f"{p['new']}/visuals/{vid}/visual.json", vj))
    if report_update is not None:
        checks.append(("report", "report.json", report_update))

    # pages.json
    meta_path, meta = target._pages_meta()
    pos = len(current_order) if position is None else min(position,
                                                          len(current_order))
    new_ids = [page_map[pid] for pid in selected]
    new_meta = copy.deepcopy(meta)
    new_meta.setdefault("$schema", _PAGES_SCHEMA)
    new_meta["pageOrder"] = current_order[:pos] + new_ids + current_order[pos:]
    if not current_order:
        new_meta.setdefault("activePageName", new_ids[0])
    checks.append(("pagesMetadata", "pages.json", new_meta))
    _validate_all(target, checks)

    # --- write ----------------------------------------------------------------------------
    w = _Writer(target, tgt_defn)
    try:
        for src_file, dst_file in plan["copies"]:
            w.copy(src_file, dst_file)
        for p in pages_out:
            pdir = tgt_pages_dir / p["new"]
            w.json(pdir / "page.json", p["json"])
            for vid, vj in p["visuals"].items():
                w.json(pdir / "visuals" / vid / "visual.json", vj)
                for rel, path in src[p["old"]]["vextras"].get(vid, {}).items():
                    w.copy(path, pdir / "visuals" / vid / rel)
            for rel, path in src[p["old"]]["extras"].items():
                w.copy(path, pdir / rel)
        if report_update is not None:
            w.json(tgt_defn / "report.json", report_update)
        if bookmark_files:
            bdir = tgt_defn / "bookmarks"
            for path, data, _ in bookmark_files:
                w.json(path, data, validate=False)
            bmeta_path = bdir / "bookmarks.json"
            bmeta = _read_json_or(bmeta_path, None) or {
                "$schema": _BOOKMARKS_SCHEMA, "items": []}
            items = bmeta.setdefault("items", [])
            have = {i.get("name") for i in items if isinstance(i, dict)}
            for _, _, entry in bookmark_files:
                if entry["stem"] not in have:
                    items.append({"name": entry["stem"]})
            w.json(bmeta_path, bmeta, validate=False)
        w.json(meta_path, new_meta)
    except BaseException:
        w.rollback()
        raise

    return {
        "ok": True,
        "id_map": dict(page_map),
        "pages": [{"from": p["old"], "to": p["new"],
                   "name": display[p["old"]],
                   "visual_ids": {v: v for v in sorted(p["visuals"])},
                   "visual_count": len(p["visuals"])} for p in pages_out],
        "bookmarks": {
            "imported": [{"from": e["old_name"], "to": e["name"]}
                         for _, _, e in bookmark_files],
            "skipped": skipped_bookmarks},
        "resources": {"copied": plan["copied"], "reused": plan["reused"],
                      "renamed": plan["renamed"]},
        "order": new_meta["pageOrder"],
        "missing_fields": missing,
        "warnings": warnings,
    }


# --- import_visuals ---------------------------------------------------------------------------

def _check_offset(offset) -> tuple[float, float]:
    if offset is None:
        return 0, 0
    if not isinstance(offset, dict) or set(offset) - {"x", "y"}:
        raise ValueError('offset must be an object like {"x": 20, "y": 10}')
    out = []
    for axis in ("x", "y"):
        v = offset.get(axis, 0)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError(f"offset[{axis!r}] must be a number")
        out.append(v)
    return out[0], out[1]


def import_visuals(target, source, page_id: str, visual_ids: list[str],
                   target_page_id: str, offset: dict | None = None,
                   allow_missing_fields: bool = False) -> dict:
    """Copy visuals from `source` page `page_id` onto `target_page_id`.

    Ids that are taken on the target page become ``<id>-n``; the optional
    ``offset`` ``{"x", "y"}`` is added to every imported visual's position
    (group members included). Selecting a visual group also imports its
    members; a member whose group is not imported is ungrouped. Returns
    ``{"ok", "page_id", "id_map" (old visual id -> new), "count",
    "missing_fields", "resources", "warnings"}``.
    """
    if not visual_ids:
        raise ValueError("visual_ids must list at least one visual to import")
    dx, dy = _check_offset(offset)

    src_defn = source._require_report() / "definition"
    tgt_report = target._require_report()
    tgt_defn = tgt_report / "definition"
    if page_id not in {p.id for p in source.list_pages()}:
        raise ValueError(f"Page {page_id!r} not found in the source report "
                         f"(available: {[p.id for p in source.list_pages()]})")
    tgt_pages = {p.id for p in target.list_pages()}
    if target_page_id not in tgt_pages:
        raise ValueError(f"Target page {target_page_id!r} not found "
                         f"(available: {sorted(tgt_pages)})")

    page = _load_source_page(src_defn, page_id)
    available = sorted(page["visuals"])
    unknown = [v for v in visual_ids if v not in page["visuals"]]
    if unknown:
        raise ValueError(f"Visual(s) {unknown} not found on source page "
                         f"{page_id!r} (available: {available})")

    warnings: list[str] = []
    chosen: list[str] = list(dict.fromkeys(visual_ids))
    groups = {v for v in chosen if "visualGroup" in page["visuals"][v]}
    while groups:
        members = sorted(v for v, obj in page["visuals"].items()
                         if obj.get("parentGroupName") in groups
                         and v not in chosen)
        if not members:
            break
        chosen.extend(members)
        warnings.append(f"also importing group member(s) {members}")
        groups = {v for v in members if "visualGroup" in page["visuals"][v]}

    vdir = tgt_defn / "pages" / target_page_id / "visuals"
    existing = {p.name for p in vdir.iterdir() if p.is_dir()} \
        if vdir.is_dir() else set()
    taken = existing | {v for v in chosen if v not in existing}
    id_map: dict[str, str] = {}
    for vid in chosen:
        if vid in existing:
            new = _unique_id(vid, taken, "visual")
            taken.add(new)
        else:
            new = vid
        id_map[vid] = new

    objs: dict[str, dict] = {}
    for vid in chosen:
        vj = copy.deepcopy(page["visuals"][vid])
        vj["name"] = id_map[vid]
        parent = vj.get("parentGroupName")
        if parent is not None:
            if parent in id_map:
                vj["parentGroupName"] = id_map[parent]
            else:
                del vj["parentGroupName"]
                warnings.append(f"visual {vid!r} was in group {parent!r}, "
                                f"which is not imported; it is ungrouped")
        if dx or dy:
            pos = vj.setdefault("position", {})
            pos["x"] = pos.get("x", 0) + dx
            pos["y"] = pos.get("y", 0) + dy
        objs[vid] = vj

    missing = _check_fields(target, source,
                            [(f"{page_id}/{v}", o) for v, o in objs.items()],
                            allow_missing_fields, warnings)
    plan = _plan_resources(source, target, list(objs.values()), warnings)
    visual_types = {o["visual"]["visualType"] for o in objs.values()
                    if isinstance(o.get("visual"), dict)
                    and o["visual"].get("visualType")}
    report_update = _apply_report_registration(target, source, plan,
                                               visual_types, warnings)
    checks = [("visualContainer",
               f"{target_page_id}/visuals/{id_map[v]}/visual.json", o)
              for v, o in objs.items()]
    if report_update is not None:
        checks.append(("report", "report.json", report_update))
    _validate_all(target, checks)

    w = _Writer(target, tgt_defn)
    try:
        for src_file, dst_file in plan["copies"]:
            w.copy(src_file, dst_file)
        for vid, obj in objs.items():
            folder = vdir / id_map[vid]
            w.json(folder / "visual.json", obj)
            for rel, path in page["vextras"].get(vid, {}).items():
                w.copy(path, folder / rel)
        if report_update is not None:
            w.json(tgt_defn / "report.json", report_update)
    except BaseException:
        w.rollback()
        raise

    return {
        "ok": True,
        "page_id": target_page_id,
        "id_map": dict(id_map),
        "count": len(id_map),
        "missing_fields": missing,
        "resources": {"copied": plan["copied"], "reused": plan["reused"],
                      "renamed": plan["renamed"]},
        "warnings": warnings,
    }
