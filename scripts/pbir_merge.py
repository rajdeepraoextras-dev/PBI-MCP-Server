#!/usr/bin/env python3
"""Git merge driver: structural three-way merge for PBIR JSON.

Power BI PBIR stores a report as many small JSON files (visual.json,
page.json, pages.json, report.json, bookmarks...). Git's line-based merge
trips over harmless things there (two branches each adding a filter, moving
different visuals...). This driver merges the *parsed* JSON instead:

* objects are merged key by key: a key changed on one side only wins, an
  identical change on both sides collapses, a key deleted on one side and
  untouched on the other is deleted;
* arrays of objects that each carry a unique ``name`` (else ``id``) are matched
  by that key (filters, bookmark items, resource packages, annotations...)
  and merged element by element; new elements from either side are kept and
  the order follows our side;
* every other array (and every scalar) is atomic: if both sides changed it
  differently, that is a conflict.

On a conflict the merged result is still written, with every conflicting
leaf resolved to OUR value; ``<out>.pbir-conflicts.json`` lists each
conflicting path with its ``base`` / ``ours`` / ``theirs`` values (a side
where the value is absent has no such key) and the exit status is 1, so git
reports the file as conflicted. Exit status 0 means a clean merge; 2 means the
inputs could not be merged at all (invalid JSON, bad arguments) and ``<out>``
was left untouched.

The output keeps our file's style: BOM, line endings, indentation width (or
tabs), ``\\uXXXX`` escaping and the trailing newline. A merge that changes
nothing relative to our side leaves the file byte-for-byte as it was.

Setup
-----
1. Scope the driver to the report folder in ``.gitattributes``::

       *.Report/**/*.json merge=pbir
       # projects nested deeper than the repo root:
       **/*.Report/**/*.json merge=pbir

2. Register it (run once per clone; ``git config`` is not versioned)::

       git config merge.pbir.name "PBIR JSON structural merge"
       git config merge.pbir.driver "python scripts/pbir_merge.py %O %A %B %A"

   ``%O`` is the common ancestor, ``%A`` our version, ``%B`` their version.
   Git requires the driver to leave its result in the file named by ``%A``
   (that temporary file is overwritten), which is why ``%A`` appears twice:
   as ``ours`` (input) and as ``out`` (destination). This is safe because the
   script reads all three inputs completely into memory before it writes, and
   it replaces ``out`` atomically (temp file + rename). If you ever pass a
   different ``out`` path, ours is left untouched. Add ``%P`` as a fifth
   argument to have the repo path in messages and in the conflicts file.
   The script path is resolved from the directory git runs the driver in
   (the top of the working tree); use an absolute path when the script lives
   elsewhere, e.g. ``python /path/to/pbi-mcp/scripts/pbir_merge.py ...``.

3. Conflicts files are written next to git's temporary ``%A`` file; add
   ``*.pbir-conflicts.json`` to ``.gitignore``.

Optional flag ``--merge-string-lists`` (put it before the paths) also merges
arrays of unique strings such as ``pageOrder`` as ordered sets instead of
atomically: additions from both sides are kept, an element removed by either
side is removed.

Library use::

    from pbir_merge import merge_json
    merged, conflicts = merge_json(base, ours, theirs)   # base=None: no ancestor

Only the standard library is used, so the file can be copied into any repo.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

__all__ = ["merge_json", "merge_texts", "main", "detect_style"]

_MISSING = object()
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ESCAPE = re.compile(r"\\u[0-9a-fA-F]{4}")
_IDENTITY_KEYS = ("name", "id")


# --- equality ------------------------------------------------------------------

def _eq(a, b) -> bool:
    """JSON equality: key order is irrelevant, true != 1, 1 == 1.0."""
    if a is _MISSING or b is _MISSING:
        return a is b
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, dict):
        return (isinstance(b, dict) and a.keys() == b.keys()
                and all(_eq(a[k], b[k]) for k in a))
    if isinstance(a, list):
        return (isinstance(b, list) and len(a) == len(b)
                and all(_eq(x, y) for x, y in zip(a, b)))
    return type(a) is type(b) and a == b or (
        isinstance(a, (int, float)) and isinstance(b, (int, float)) and a == b)


# --- merge ------------------------------------------------------------------------

def _join(path: str, key: str) -> str:
    return f"{path}.{key}" if _IDENT.fullmatch(key) else f"{path}[{json.dumps(key)}]"


def _conflict(conflicts: list, path: str, base, ours, theirs) -> None:
    entry: dict = {"path": path}
    if base is not _MISSING:
        entry["base"] = base
    if ours is not _MISSING:
        entry["ours"] = ours
    if theirs is not _MISSING:
        entry["theirs"] = theirs
    conflicts.append(entry)


def _identity_key(*arrays: list) -> str | None:
    """The key ('name' or 'id') that identifies every element of every array."""
    if not any(arrays):
        return None
    for key in _IDENTITY_KEYS:
        ok = True
        for arr in arrays:
            seen: set = set()
            for el in arr:
                ident = el.get(key, _MISSING) if isinstance(el, dict) else _MISSING
                if (ident is _MISSING or isinstance(ident, bool)
                        or not isinstance(ident, (str, int)) or ident in seen):
                    ok = False
                    break
                seen.add(ident)
            if not ok:
                break
        if ok:
            return key
    return None


def _merge_order(base_ids: list, ours_ids: list, theirs_ids: list) -> list:
    """Element order for a keyed array: ours' order (or theirs' if only theirs
    reordered the surviving elements) with the other side's additions
    inserted after their predecessor (after the primary side's own additions
    at that spot, so both sides appending at the end gives ours, then theirs).
    """
    bset = set(base_ids)

    def reordered(side: list) -> bool:
        s = set(side)
        return [i for i in side if i in bset] != [i for i in base_ids if i in s]

    if reordered(theirs_ids) and not reordered(ours_ids):
        primary, secondary = theirs_ids, ours_ids
    else:
        primary, secondary = ours_ids, theirs_ids
    in_secondary = set(secondary)
    result = list(primary)
    last = -1
    for ident in secondary:
        if ident in result:
            last = result.index(ident)
            continue
        pos = last + 1
        while pos < len(result) and result[pos] not in in_secondary:
            pos += 1                      # skip the primary side's additions
        result.insert(pos, ident)
        last = pos
    return result


def _merge_keyed(key: str, base: list, ours: list, theirs: list, path: str,
                 conflicts: list, opts: dict) -> list:
    bm = {el[key]: el for el in base}
    om = {el[key]: el for el in ours}
    tm = {el[key]: el for el in theirs}
    out = []
    for ident in _merge_order(list(bm), list(om), list(tm)):
        merged = _merge(bm.get(ident, _MISSING), om.get(ident, _MISSING),
                        tm.get(ident, _MISSING), f"{path}[{key}={ident}]",
                        conflicts, opts)
        if merged is not _MISSING:
            out.append(merged)
    return out


def _merge_string_lists(base: list, ours: list, theirs: list) -> list:
    bset, oset, tset = set(base), set(ours), set(theirs)
    out = []
    for ident in _merge_order(base, ours, theirs):
        if ident in bset:
            keep = ident in oset and ident in tset
        else:
            keep = ident in oset or ident in tset
        if keep:
            out.append(ident)
    return out


def _is_unique_strings(*arrays: list) -> bool:
    return all(all(isinstance(x, str) for x in a) and len(set(a)) == len(a)
               for a in arrays)


def _merge(base, ours, theirs, path: str, conflicts: list, opts: dict):
    if _eq(ours, theirs):
        return ours
    if _eq(ours, base):
        return theirs
    if _eq(theirs, base):
        return ours
    # both sides changed the value, and differently
    if isinstance(ours, dict) and isinstance(theirs, dict) \
            and (isinstance(base, dict) or base is _MISSING):
        b = base if isinstance(base, dict) else {}
        out = {}
        for k in list(ours) + [k for k in theirs if k not in ours]:
            merged = _merge(b.get(k, _MISSING), ours.get(k, _MISSING),
                            theirs.get(k, _MISSING), _join(path, k),
                            conflicts, opts)
            if merged is not _MISSING:
                out[k] = merged
        return out
    if isinstance(ours, list) and isinstance(theirs, list) \
            and (isinstance(base, list) or base is _MISSING):
        b = base if isinstance(base, list) else []
        key = _identity_key(b, ours, theirs)
        if key is not None:
            return _merge_keyed(key, b, ours, theirs, path, conflicts, opts)
        if opts.get("string_lists") and _is_unique_strings(b, ours, theirs):
            return _merge_string_lists(b, ours, theirs)
    _conflict(conflicts, path, base, ours, theirs)
    return ours


def merge_json(base, ours, theirs, *, merge_string_lists: bool = False):
    """Three-way merge of parsed JSON -> ``(merged, conflicts)``.

    ``base=None`` means "no common ancestor" (the file was added on both
    sides). ``conflicts`` is a list of ``{"path", "base"?, "ours"?,
    "theirs"?}``; ``merged`` carries our value at every conflicting path.
    """
    conflicts: list = []
    merged = _merge(_MISSING if base is None else base, ours, theirs, "$",
                    conflicts, {"string_lists": merge_string_lists})
    return merged, conflicts


# --- style ---------------------------------------------------------------------------

def detect_style(raw: bytes) -> tuple[str, str]:
    """(encoding, newline) of a file's bytes; same rule as
    ``core.io_safe.detect_style``: BOM -> utf-8-sig, CRLF when it outnumbers
    bare LF."""
    encoding = "utf-8-sig" if raw[:3] == b"\xef\xbb\xbf" else "utf-8"
    crlf = raw.count(b"\r\n")
    lf_only = raw.count(b"\n") - crlf
    return encoding, "\r\n" if crlf > lf_only else "\n"


def _detect_indent(text: str):
    """Indent unit of the first indented line: int spaces, '\\t', or None."""
    for line in text.splitlines():
        stripped = line.lstrip(" \t")
        if stripped and stripped != line:
            lead = line[:len(line) - len(stripped)]
            return "\t" if lead[0] == "\t" else len(lead)
    return None


def _render(value, model_texts: list[str], encoding: str, newline: str) -> bytes:
    """Serialise `value` in the style of the first `model_texts` entry that
    shows a style (indent width) and the first that shows escaping."""
    indent = next((i for i in map(_detect_indent, model_texts)
                   if i is not None), 2)
    ensure_ascii = bool(_ESCAPE.search(model_texts[0])) if model_texts else False
    text = json.dumps(value, indent=indent, ensure_ascii=ensure_ascii)
    if model_texts and model_texts[0].endswith(("\n", "\r\n")):
        text += "\n"
    if newline != "\n":
        text = text.replace("\n", newline)
    return text.encode(encoding)


# --- file level ------------------------------------------------------------------------

def _load(raw: bytes | None, name: str):
    """(parsed, text) for one input; (None, "") when absent/empty."""
    if raw is None or not raw.strip():
        return None, ""
    text = raw.decode("utf-8-sig")
    try:
        return json.loads(text), text
    except ValueError as exc:
        raise ValueError(f"{name} is not valid JSON: {exc}") from exc


def merge_texts(base: bytes | None, ours: bytes, theirs: bytes, *,
                merge_string_lists: bool = False):
    """Byte-level merge -> ``(merged_bytes, conflicts)`` in our file's style."""
    b_val, b_text = _load(base, "base")
    o_val, o_text = _load(ours, "ours")
    t_val, t_text = _load(theirs, "theirs")
    if o_val is None and not o_text:
        raise ValueError("ours is empty")
    if t_val is None and not t_text:
        raise ValueError("theirs is empty")
    merged, conflicts = merge_json(b_val, o_val, t_val,
                                   merge_string_lists=merge_string_lists)
    if _eq(merged, o_val):
        return ours, conflicts        # nothing to change on our side
    encoding, newline = detect_style(ours)
    models = [t for t in (o_text, t_text, b_text) if t]
    return _render(merged, models, encoding, newline), conflicts


def _read_optional(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _atomic_write(path: Path, data: bytes) -> None:
    if path.exists() and path.read_bytes() == data:
        return
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    string_lists = "--merge-string-lists" in args
    args = [a for a in args if a != "--merge-string-lists"]
    if len(args) not in (4, 5):
        print("usage: pbir_merge.py [--merge-string-lists] "
              "<base> <ours> <theirs> <out> [<path-label>]", file=sys.stderr)
        return 2
    base_p, ours_p, theirs_p, out_p = (Path(a) for a in args[:4])
    label = args[4] if len(args) == 5 else str(out_p)
    conflicts_p = out_p.with_name(out_p.name + ".pbir-conflicts.json")
    try:
        ours = _read_optional(ours_p)
        theirs = _read_optional(theirs_p)
        if ours is None or theirs is None:
            raise ValueError("ours or theirs file is missing")
        data, conflicts = merge_texts(_read_optional(base_p), ours, theirs,
                                      merge_string_lists=string_lists)
    except ValueError as exc:
        print(f"pbir-merge: cannot merge {label}: {exc}", file=sys.stderr)
        return 2
    _atomic_write(out_p, data)
    if conflicts:
        payload = {"file": label, "count": len(conflicts),
                   "conflicts": conflicts}
        conflicts_p.write_text(json.dumps(payload, indent=2) + "\n",
                               encoding="utf-8")
        paths = ", ".join(c["path"] for c in conflicts[:5])
        more = f" (+{len(conflicts) - 5} more)" if len(conflicts) > 5 else ""
        print(f"pbir-merge: {len(conflicts)} conflict(s) in {label}: "
              f"{paths}{more}; ours kept, details in {conflicts_p}",
              file=sys.stderr)
        return 1
    if conflicts_p.exists():
        conflicts_p.unlink()
    return 0


if __name__ == "__main__":
    sys.exit(main())
