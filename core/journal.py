"""Write journal: dry-run previews, undo history and transactions.

Every write tool is wrapped (see core/tooling.py) so that:

* ``dry_run=True`` runs the operation against a throw-away copy of the
  project and returns the would-be changes as a unified diff, touching
  nothing on disk;
* real writes are journaled: the pre-image of every changed or deleted file
  is stored under ``<project root>/.pbi-mcp/undo/<id>/`` so ``pbi_undo`` can
  put it back byte-for-byte (line endings and BOMs included);
* ``pbi_begin_transaction`` / ``pbi_commit`` / ``pbi_rollback`` group several
  writes into one revertible unit.

The store lives outside the ``*.Report`` / ``*.SemanticModel`` folders, so
Power BI Desktop never sees it. Snapshots skip ``.pbi/`` caches, ``*.bak-*``
backups and VCS folders. Both servers share one store per project, so the
undo history is project-wide regardless of which server wrote.
"""

from __future__ import annotations

import difflib
import json
import os
import shutil
import stat
import tempfile
import time
import uuid
from pathlib import Path

STORE_DIR = ".pbi-mcp"
SKIP_DIRS = {".pbi", STORE_DIR, ".git", "__pycache__", ".venv", "node_modules"}
MAX_ENTRIES = 50
MAX_DIFF_CHARS = 60_000


# --- project root -----------------------------------------------------------

def project_root(project) -> Path:
    """Folder that holds the .pbip pointer and the layer folders.

    Mirrors PbipProject's own resolution: a file -> its parent; a layer folder
    -> its parent; anything else is already the root.
    """
    p = Path(project.path)
    if p.is_file():
        return p.parent
    if p.name.endswith((".SemanticModel", ".Report")):
        return p.parent
    return p


# --- snapshots --------------------------------------------------------------

def _skip_file(name: str) -> bool:
    return ".bak-" in name or ".tmp-" in name


TRASH_DIR = "mcp-trash"


def _force_unlink(p: Path) -> None:
    try:
        p.unlink()
    except PermissionError:
        os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
        p.unlink()


def _force_rmdir(d: Path) -> None:
    """rmdir that also clears the Windows read-only attribute.

    OneDrive marks folders read-only, and a folder moved into the trash (or a
    scratch copy of the project) keeps that flag, which makes rmdir fail.
    """
    try:
        d.rmdir()
    except PermissionError:
        os.chmod(d, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
        d.rmdir()


def _rmtree(path: Path) -> None:
    """Best-effort recursive delete that tolerates read-only attributes."""
    path = Path(path)
    if not path.exists():
        return
    for dirpath, dirnames, filenames in os.walk(path, topdown=False):
        for name in filenames:
            try:
                _force_unlink(Path(dirpath) / name)
            except OSError:
                pass
        for name in dirnames:
            try:
                _force_rmdir(Path(dirpath) / name)
            except OSError:
                pass
    try:
        _force_rmdir(path)
    except OSError:
        pass


def _walk(root: Path):
    """os.walk over the project that skips caches and VCS folders.

    ``.pbi`` folders (Desktop caches) are skipped except their ``mcp-trash``
    subtree: trashed visuals/pages are project state, so deleting and
    restoring them must be journaled.
    """
    for dirpath, dirnames, filenames in os.walk(root):
        if Path(dirpath).name == ".pbi":
            dirnames[:] = [d for d in dirnames if d == TRASH_DIR]
            filenames = []
        else:
            dirnames[:] = sorted(d for d in dirnames
                                 if d not in SKIP_DIRS or d == ".pbi")
        yield Path(dirpath), filenames


def snapshot(root: Path) -> dict[str, bytes]:
    """All project files as {posix relative path: bytes}."""
    out: dict[str, bytes] = {}
    root = Path(root)
    for dirpath, filenames in _walk(root):
        for name in filenames:
            if _skip_file(name):
                continue
            full = dirpath / name
            out[full.relative_to(root).as_posix()] = full.read_bytes()
    return out


def backup_files(root: Path) -> set[str]:
    """Relative paths of the ``*.bak-*`` safety copies under the project."""
    root = Path(root)
    found: set[str] = set()
    for dirpath, filenames in _walk(root):
        for name in filenames:
            if ".bak-" in name:
                found.add((dirpath / name).relative_to(root).as_posix())
    return found


def delta(before: dict[str, bytes], after: dict[str, bytes]) -> dict:
    """Which paths were added / modified / deleted between two snapshots."""
    return {
        "added": sorted(set(after) - set(before)),
        "modified": sorted(k for k in set(before) & set(after)
                           if before[k] != after[k]),
        "deleted": sorted(set(before) - set(after)),
    }


def _lines(data: bytes) -> list[str]:
    return data.decode("utf-8", errors="replace").splitlines(keepends=True)


def unified_diff(before: dict[str, bytes], after: dict[str, bytes],
                 d: dict | None = None) -> str:
    """Unified diff of every changed file, truncated to MAX_DIFF_CHARS."""
    d = d or delta(before, after)
    chunks: list[str] = []
    for path in d["modified"] + d["added"] + d["deleted"]:
        old = before.get(path, b"")
        new = after.get(path, b"")
        if b"\x00" in old or b"\x00" in new:
            chunks.append(f"Binary file {path} changed\n")
            continue
        chunks.extend(difflib.unified_diff(
            _lines(old), _lines(new),
            fromfile=f"a/{path}" if path in before else "/dev/null",
            tofile=f"b/{path}" if path in after else "/dev/null"))
    text = "".join(chunks)
    if len(text) > MAX_DIFF_CHARS:
        text = text[:MAX_DIFF_CHARS] + "\n... (diff truncated)\n"
    return text


# --- undo store -------------------------------------------------------------

class Journal:
    """Undo history + transaction mark for one project root."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.dir = self.root / STORE_DIR / "undo"
        self.tx_file = self.root / STORE_DIR / "transaction.json"

    # entries are directories named "<time_ns>-<uuid8>", so sorting by name
    # is chronological.
    def _entries(self) -> list[Path]:
        if not self.dir.exists():
            return []
        return sorted(p for p in self.dir.iterdir() if p.is_dir())

    def record(self, tool: str, fn):
        """Run `fn()`; if it changed files, persist their pre-images."""
        before = snapshot(self.root)
        baks_before = backup_files(self.root)
        result = fn()
        after = snapshot(self.root)
        d = delta(before, after)
        if d["added"] or d["modified"] or d["deleted"]:
            self._persist(tool, before, d,
                          sorted(backup_files(self.root) - baks_before))
        return result

    def _persist(self, tool: str, before: dict[str, bytes], d: dict,
                 backups: list[str] | None = None) -> Path:
        entry = self.dir / f"{time.time_ns():020d}-{uuid.uuid4().hex[:8]}"
        pre = entry / "before"
        for path in d["modified"] + d["deleted"]:
            target = pre / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(before[path])
        entry.mkdir(parents=True, exist_ok=True)
        meta = {
            "id": entry.name,
            "tool": tool,
            "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "added": d["added"],
            "modified": d["modified"],
            "deleted": d["deleted"],
            "backups": backups or [],
        }
        (entry / "meta.json").write_text(json.dumps(meta, indent=2),
                                         encoding="utf-8")
        self._prune()
        return entry

    def _prune(self) -> None:
        if self.tx_file.exists():      # a rollback needs every entry since begin()
            return
        entries = self._entries()
        for old in entries[:-MAX_ENTRIES] if len(entries) > MAX_ENTRIES else []:
            shutil.rmtree(old, ignore_errors=True)

    def _meta(self, entry: Path) -> dict:
        return json.loads((entry / "meta.json").read_text(encoding="utf-8"))

    def history(self, limit: int = 20) -> list[dict]:
        """Journaled writes, newest first."""
        metas = [self._meta(e) for e in reversed(self._entries())]
        return metas[:max(0, limit)]

    def undo(self, steps: int = 1) -> dict:
        """Revert the latest `steps` journaled writes, newest first."""
        undone: list[dict] = []
        for entry in reversed(self._entries()[-max(0, steps):] if steps else []):
            meta = self._meta(entry)
            pre = entry / "before"
            for path in meta["modified"] + meta["deleted"]:
                src = pre / path
                dst = self.root / path
                dst.parent.mkdir(parents=True, exist_ok=True)
                tmp = dst.with_name(f"{dst.name}.tmp-{os.getpid()}")
                shutil.copyfile(src, tmp)
                os.replace(tmp, dst)
            for path in meta["added"]:
                dst = self.root / path
                if dst.exists():
                    _force_unlink(dst)
            for path in meta.get("backups", []):
                bak = self.root / path
                if bak.exists():
                    _force_unlink(bak)
            for path in meta["added"]:
                self._prune_dirs((self.root / path).parent)
            shutil.rmtree(entry, ignore_errors=True)
            undone.append({"id": meta["id"], "tool": meta["tool"]})
        return {"undone": undone, "remaining": len(self._entries())}

    def _prune_dirs(self, d: Path) -> None:
        """Remove `d` and empty ancestors; leftover safety copies do not count."""
        while d != self.root and d.exists():
            entries = list(d.iterdir())
            if any(not (e.is_file() and _skip_file(e.name)) for e in entries):
                return
            for e in entries:
                _force_unlink(e)
            _force_rmdir(d)
            d = d.parent

    # --- transactions ---------------------------------------------------

    def _since(self, tx: dict) -> list[Path]:
        """Entries written after the transaction began (ids sort by time)."""
        return [e for e in self._entries() if e.name > tx["mark_id"]]

    def in_transaction(self) -> dict | None:
        if not self.tx_file.exists():
            return None
        return json.loads(self.tx_file.read_text(encoding="utf-8"))

    def begin(self) -> dict:
        if self.in_transaction():
            raise ValueError("A transaction is already open; "
                             "call pbi_commit or pbi_rollback first.")
        entries = self._entries()
        tx = {"mark_id": entries[-1].name if entries else "",
              "started": time.strftime("%Y-%m-%dT%H:%M:%S")}
        self.tx_file.parent.mkdir(parents=True, exist_ok=True)
        self.tx_file.write_text(json.dumps(tx), encoding="utf-8")
        return {"ok": True, **tx}

    def commit(self) -> dict:
        tx = self.in_transaction()
        if tx is None:
            raise ValueError("No open transaction.")
        self.tx_file.unlink()
        kept = len(self._since(tx))
        self._prune()
        return {"ok": True, "writes_kept": kept}

    def rollback(self) -> dict:
        tx = self.in_transaction()
        if tx is None:
            raise ValueError("No open transaction.")
        n = len(self._since(tx))
        result = self.undo(n) if n > 0 else {"undone": [], "remaining": len(self._entries())}
        self.tx_file.unlink()
        return {"ok": True, **result}


def for_state(state) -> Journal:
    """The journal for the project currently selected in `state`."""
    return Journal(project_root(state.require()))


# --- dry run ----------------------------------------------------------------

def _copy_ignore(_dir: str, names: list[str]) -> set[str]:
    return {n for n in names if n in SKIP_DIRS or _skip_file(n)}


def dry_run(state, fn) -> dict:
    """Run `fn()` against a scratch copy of the selected project.

    Temporarily rebinds `state.project` to the copy so the tool logic is
    exercised unchanged, then reports what would have changed. Nothing under
    the real project is touched.
    """
    project = state.require()
    root = project_root(project)
    tmp = Path(tempfile.mkdtemp(prefix="pbi-dry-"))
    try:
        dst = tmp / root.name
        shutil.copytree(root, dst, ignore=_copy_ignore)
        try:
            rel = Path(project.path).resolve().relative_to(root.resolve())
        except ValueError:
            rel = Path(".")
        copy = type(project)(dst / rel, backups=False)
        copy.preflight = getattr(project, "preflight", True)
        before = snapshot(dst)
        original = state.project
        state.project = copy
        try:
            result = fn()
        finally:
            state.project = original
        after = snapshot(dst)
        d = delta(before, after)
        return {
            "dry_run": True,
            "result": result,
            "changes": d,
            "diff": unified_diff(before, after, d),
        }
    finally:
        _rmtree(tmp)
