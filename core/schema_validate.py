"""Pre-flight JSON-schema validation against vendored Fabric schemas (E1).

Every report-layer JSON we write is validated *before* it hits disk, so we
never again produce a shape Desktop rejects. Schemas are the official
Microsoft Fabric PBIR schemas vendored under resources/schemas/ (offline).

Public API:
  validate(kind, obj)         -> list[str] of error messages ("" list = ok)
  assert_valid(kind, obj)     -> raises ValueError on any error
  is_available()              -> False if jsonschema / schemas missing

`kind` is one of: report, page, pagesMetadata, visualContainer,
filterConfiguration, bookmark. Validation degrades gracefully to a no-op if
the optional `jsonschema` dependency isn't installed.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_SCHEMA_DIR = Path(__file__).resolve().parent.parent / "resources" / "schemas"

try:  # optional dependency; validation is a no-op without it
    import jsonschema
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT7
    _HAVE = True
except Exception:  # noqa: BLE001
    _HAVE = False


def is_available() -> bool:
    return _HAVE and (_SCHEMA_DIR / "index.json").exists()


@lru_cache(maxsize=1)
def _registry():
    index = json.loads((_SCHEMA_DIR / "index.json").read_text(encoding="utf-8"))
    resources = []
    for url, fn in index.items():
        schema = json.loads((_SCHEMA_DIR / fn).read_text(encoding="utf-8"))
        # Register under the canonical URL so relative $refs resolve offline.
        resources.append((url, Resource.from_contents(schema, DRAFT7)))
        sid = schema.get("$id")
        if sid and sid != url:
            resources.append((sid, Resource.from_contents(schema, DRAFT7)))
    return Registry().with_resources(resources)


@lru_cache(maxsize=1)
def _roots() -> dict:
    return json.loads((_SCHEMA_DIR / "roots.json").read_text(encoding="utf-8"))


def _validator(kind: str):
    roots = _roots()
    if kind not in roots:
        raise KeyError(f"Unknown schema kind {kind!r}; known: {sorted(roots)}")
    url = roots[kind]
    index = json.loads((_SCHEMA_DIR / "index.json").read_text(encoding="utf-8"))
    schema = json.loads((_SCHEMA_DIR / index[url]).read_text(encoding="utf-8"))
    return jsonschema.Draft7Validator(schema, registry=_registry())


def validate(kind: str, obj: dict) -> list[str]:
    """Return a list of human-readable validation errors (empty = valid).

    The `$schema` version pointer is ignored: real Desktop files declare newer
    format versions (e.g. visualContainer/2.10.0) than the published,
    validatable schemas (1.x), and the structures stay compatible for catching
    real mistakes. We validate structure, not the version string.
    """
    if not is_available():
        return []
    errors = []
    for err in sorted(_validator(kind).iter_errors(obj), key=lambda e: [str(p) for p in e.path]):
        if _is_version_drift(err):
            continue
        loc = "/".join(str(p) for p in err.path) or "<root>"
        errors.append(f"{loc}: {err.message}")
    return errors


def _is_version_drift(err) -> bool:
    """True for errors caused by newer Desktop format vs the 1.x validatable
    schema — confirmed by validating 140 real Desktop files (138 clean; the
    only misses were these). We validate structure, not format version.
    """
    path = list(err.path)
    msg = err.message
    if path == ["$schema"]:
        return True  # version pointer string
    # report.json: 1.0.0 requires layoutOptimization; 3.3.0 files omit it
    if path == [] and "'layoutOptimization' is a required property" in msg:
        return True
    # reportVersionAtImport is a string in 1.0.0 but an object in real files
    if path and path[-1] == "reportVersionAtImport" and "is not of type 'string'" in msg:
        return True
    # VisualTopN condition: valid per Desktop's own validator and present in
    # semanticQuery >=1.3.0, but filterConfiguration pins the 1.2.0 ref which
    # predates it. Only appears in a filter Where/Condition.
    if path and path[-1] == "Condition" and "VisualTopN" in msg:
        return True
    return False


def assert_valid(kind: str, obj: dict, *, context: str = "") -> None:
    """Raise ValueError if `obj` doesn't validate against the `kind` schema."""
    errors = validate(kind, obj)
    if errors:
        where = f" ({context})" if context else ""
        shown = "; ".join(errors[:8])
        more = f" …(+{len(errors) - 8} more)" if len(errors) > 8 else ""
        raise ValueError(
            f"Schema validation failed for {kind}{where}: {shown}{more}")
