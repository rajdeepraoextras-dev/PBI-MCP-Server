"""Vendor the official Microsoft Fabric PBIR JSON schemas for offline
pre-flight validation (E1). Fetches the transitive closure of $refs so
validation never needs the network.

Schemas are stored in resources/schemas/ keyed by a filesystem-safe slug of
their canonical $id URL, alongside an index.json mapping $id -> file. The
validator (core/schema_validate.py) loads them into a referencing registry.

Run:  .venv/Scripts/python.exe scripts/fetch_schemas.py
"""

from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path
from urllib.parse import urljoin

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "resources" / "schemas"

BASE = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/"

# Entry points we validate against (name -> canonical URL).
ROOTS = {
    "report": BASE + "report/1.0.0/schema.json",
    "page": BASE + "page/1.0.0/schema.json",
    "pagesMetadata": BASE + "pagesMetadata/1.0.0/schema.json",
    "visualContainer": BASE + "visualContainer/1.0.0/schema.json",
    "filterConfiguration": BASE + "filterConfiguration/1.0.0/schema.json",
    "bookmark": BASE + "bookmark/1.0.0/schema.json",
}


def slug(url: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", url.split("definition/")[-1]).strip("_") + ".json"


def external_refs(schema_text: str, base_url: str) -> set[str]:
    urls = set()
    for ref in re.findall(r'"\$ref"\s*:\s*"([^"]+)"', schema_text):
        if ref.startswith("#"):
            continue
        target = urljoin(base_url, ref.split("#")[0])
        urls.add(target)
    return urls


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    seen: dict[str, str] = {}       # url -> filename
    queue = list(ROOTS.values())

    while queue:
        url = queue.pop()
        if url in seen:
            continue
        try:
            text = urllib.request.urlopen(url, timeout=30).read().decode("utf-8")
        except Exception as e:  # noqa: BLE001
            print(f"FAIL {url}: {e}")
            continue
        json.loads(text)  # validate JSON
        fn = slug(url)
        (OUT / fn).write_text(text, encoding="utf-8")
        seen[url] = fn
        print(f"OK   {fn}")
        queue.extend(external_refs(text, url))

    index = {url: fn for url, fn in seen.items()}
    (OUT / "index.json").write_text(json.dumps(index, indent=2), encoding="utf-8")
    (OUT / "roots.json").write_text(
        json.dumps({k: v for k, v in ROOTS.items()}, indent=2), encoding="utf-8")
    print(f"\nvendored {len(seen)} schemas -> {OUT}")


if __name__ == "__main__":
    main()
