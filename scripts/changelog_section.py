"""Print one version's section of CHANGELOG.md (Keep a Changelog format).

    python scripts/changelog_section.py 2.0.1      # or v2.0.1
    python scripts/changelog_section.py 2.0.1 --file CHANGELOG.md

The release workflow feeds the output to the GitHub Release body. The
heading must look like ``## [2.0.1] - 2026-09-29`` (or ``## [2.0.1]``);
everything up to the next ``## `` heading is printed, minus the heading
itself. Exit code 1 with a message on stderr when the version is missing, so
a workflow can fall back to auto-generated notes.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def section(text: str, version: str) -> str | None:
    """Body of the ``## [version]`` section, or None if absent."""
    version = version.lstrip("vV")
    # The whole heading line ("## [2.0.1] - 2026-09-29") is consumed so the
    # date never leaks into the body.
    heading = re.compile(r"^## \[" + re.escape(version) + r"\][^\n]*\n?", re.M)
    m = heading.search(text)
    if m is None:
        return None
    start = m.end()
    nxt = re.compile(r"^## ", re.M).search(text, start)
    body = text[start:nxt.start() if nxt else len(text)]
    # Strip a trailing "[x.y.z]: https://..." link-reference block if present.
    body = re.sub(r"(?:^\[[^\]]+\]:\s*\S+\s*$\n?)+\Z", "", body, flags=re.M)
    return body.strip() + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("version", help="version to extract, e.g. 2.0.1 or v2.0.1")
    p.add_argument("--file", default=str(REPO / "CHANGELOG.md"),
                   help="changelog path (default: CHANGELOG.md in the repo)")
    args = p.parse_args(argv)
    text = Path(args.file).read_text(encoding="utf-8")
    body = section(text, args.version)
    if body is None:
        print(f"changelog_section: no '## [{args.version.lstrip('vV')}]' section "
              f"in {args.file}", file=sys.stderr)
        return 1
    sys.stdout.write(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
