"""Conservative static DAX linter (best-effort, non-blocking).

There is no offline DAX engine, so this only catches a few HIGH-CONFIDENCE
mistakes that Power BI Desktop rejects — the kind that show up constantly in
generated DAX. It never blocks a write; it returns warnings so the caller (an
LLM) gets an immediate correction signal.

Precision over recall: only flag patterns with a near-zero false-positive rate.
"""

from __future__ import annotations

import re

_CMP = r"(?:=|<>|<=|>=|<|>)"
# a whole argument that is `Table[Col] <op> [name]`  or  `[name] <op> Table[Col]`
_COL = r"'?[\w ]+'?\[[^\]]+\]"
_MEAS = r"\[([^\]]+)\]"
_BARE_CMP_RHS = re.compile(rf"^\s*{_COL}\s*{_CMP}\s*{_MEAS}\s*$")
_BARE_CMP_LHS = re.compile(rf"^\s*{_MEAS}\s*{_CMP}\s*{_COL}\s*$")


def _matching_paren(s: str, open_idx: int) -> int:
    """Index of the ')' matching the '(' at open_idx (respects strings)."""
    depth = 0
    in_str = False
    for i in range(open_idx, len(s)):
        ch = s[i]
        if ch == '"':
            in_str = not in_str
        elif not in_str:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return i
    return -1


def _split_top_level(s: str) -> list[str]:
    """Split on top-level commas (respects (), [], and "" strings)."""
    args, cur, depth, in_str = [], "", 0, False
    for ch in s:
        if ch == '"':
            in_str = not in_str
        if not in_str:
            if ch in "([":
                depth += 1
            elif ch in ")]":
                depth -= 1
            elif ch == "," and depth == 0:
                args.append(cur)
                cur = ""
                continue
        cur += ch
    if cur.strip():
        args.append(cur)
    return args


def _calc_filter_args(dax: str):
    """Yield each filter argument (args after the first) of every CALCULATE /
    CALCULATETABLE call in `dax`."""
    for m in re.finditer(r"\bCALCULATE(?:TABLE)?\s*\(", dax, re.IGNORECASE):
        open_idx = m.end() - 1
        close_idx = _matching_paren(dax, open_idx)
        if close_idx == -1:
            continue
        inner = dax[open_idx + 1:close_idx]
        args = _split_top_level(inner)
        for a in args[1:]:
            yield a


def lint_dax(dax: str, measure_names: set[str] | None = None) -> list[str]:
    """Return a list of high-confidence DAX warnings (empty = looks fine)."""
    warnings: list[str] = []
    measure_names = measure_names or set()

    # 1. A measure used as a CALCULATE boolean filter predicate. Only flag when
    #    the WHOLE filter argument is a bare comparison (the FILTER(...) form
    #    won't match, so no false positive on the correct pattern).
    for arg in _calc_filter_args(dax):
        for rx in (_BARE_CMP_RHS, _BARE_CMP_LHS):
            mm = rx.match(arg)
            if mm:
                name = mm.group(1)
                if not measure_names or name in measure_names:
                    warnings.append(
                        f"CALCULATE boolean filter '{arg.strip()}' references "
                        f"measure [{name}] — Power BI rejects this ('PLACEHOLDER "
                        f"in True/False table filter'). Wrap it: "
                        f"FILTER(ALL(Table[Col]), Table[Col] {'='} [{name}]).")
                break

    # 2. Empty argument (consecutive / leading / trailing comma). DAX has no
    #    omitted positional args — e.g. TOPN(.., .., , DESC) is invalid.
    if re.search(r"\(\s*,|,\s*,|,\s*\)", dax):
        warnings.append(
            "Empty argument detected (two commas with nothing between, or a "
            "trailing comma). Remove it — e.g. TOPN(N, table, orderBy, DESC), "
            "not TOPN(N, table, orderBy, , DESC).")

    return warnings
