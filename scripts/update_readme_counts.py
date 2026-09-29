"""Keep the numbers in README.md honest.

Computes, from the live code:

* the number of tools each server registers (``list_tools``), and their total;
* the number of collected tests (``python -m pytest --collect-only -q``),

and rewrites two places in README.md in place:

    **N tools** (a model + b report)
    tests/           # N tests; ...

Whitespace, line wrapping and everything else in the README are preserved.

    python scripts/update_readme_counts.py                # rewrite README.md
    python scripts/update_readme_counts.py --check        # exit 1 if stale
    python scripts/update_readme_counts.py --skip-tests   # keep the test count

Run it after adding or removing tools (tests/test_readme_counts.py fails
until you do).
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
README = REPO / "README.md"

#: ``**57 tools** (13 model +\n44 report)`` - the gap after "+" may wrap.
TOOLS_RE = re.compile(
    r"\*\*(?P<total>\d+) tools\*\* \((?P<model>\d+) model \+(?P<gap>\s+)"
    r"(?P<report>\d+) report\)")
#: ``tests/           # 299 tests; fixtures/ ...`` in the layout block.
TESTS_RE = re.compile(r"(?P<pre>tests/\s+# )(?P<n>\d+)(?P<post> tests;)")


def _tool_count(module: str) -> int:
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    mod = importlib.import_module(module)
    return len(asyncio.run(mod.mcp.list_tools()))


def live_tool_counts() -> tuple[int, int]:
    """(model tools, report tools) registered right now."""
    return (_tool_count("model_server.server"),
            _tool_count("report_server.server"))


def collected_test_count() -> int | None:
    """Tests pytest collects, or None if the count cannot be determined."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q",
         "-p", "no:cacheprovider"],
        cwd=REPO, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    # the summary is the last line; test ids above it may contain any text
    found = re.findall(r"^(\d+)(?:/\d+)? tests? collected\b", proc.stdout,
                       re.M)
    return int(found[-1]) if found else None


def readme_tool_counts(text: str) -> tuple[int, int, int] | None:
    """(total, model, report) as currently written in README text."""
    m = TOOLS_RE.search(text)
    if not m:
        return None
    return int(m["total"]), int(m["model"]), int(m["report"])


def readme_test_count(text: str) -> int | None:
    m = TESTS_RE.search(text)
    return int(m["n"]) if m else None


def apply_counts(text: str, model: int, report: int,
                 tests: int | None) -> str:
    """README text with the tool sentence (and test line) rewritten."""
    if not TOOLS_RE.search(text):
        raise ValueError("README.md has no '**N tools** (a model + b report)' "
                         "sentence to update")
    text = TOOLS_RE.sub(
        lambda m: f"**{model + report} tools** ({model} model +{m['gap']}"
                  f"{report} report)", text, count=1)
    if tests is not None:
        if not TESTS_RE.search(text):
            raise ValueError("README.md has no 'tests/ # N tests;' line to "
                             "update")
        text = TESTS_RE.sub(
            lambda m: f"{m['pre']}{tests}{m['post']}", text, count=1)
    return text


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true",
                    help="do not write; exit 1 if the README is out of date")
    ap.add_argument("--skip-tests", action="store_true",
                    help="do not collect tests; leave the test count as is")
    args = ap.parse_args(argv)

    model, report = live_tool_counts()
    tests = None if args.skip_tests else collected_test_count()
    if not args.skip_tests and tests is None:
        print("warning: could not determine the collected test count; "
              "leaving it unchanged", file=sys.stderr)

    old = README.read_bytes().decode("utf-8")
    new = apply_counts(old, model, report, tests)
    print(f"tools: {model} model + {report} report = {model + report}")
    if tests is not None:
        print(f"tests: {tests} collected")

    if new == old:
        print("README.md is up to date")
        return 0
    if args.check:
        print("README.md is out of date; run "
              "python scripts/update_readme_counts.py", file=sys.stderr)
        return 1
    README.write_bytes(new.encode("utf-8"))
    print("README.md updated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
