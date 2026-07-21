"""Safe file I/O: atomic writes + backup-on-mutate.

The one unforgivable bug is producing a project Power BI Desktop refuses to
open (Part E). Every mutation routes through here so a bad write never
destroys the source file:

  * atomic_write() writes to a sibling temp file then os.replace()s it into
    place — a crash mid-write leaves the original intact, never a half file.
  * backup() snapshots a file before its first mutation this session.

Encoding note: Power BI Desktop is NOT uniform. Real exports show TMDL and
report.json using CRLF, while page.json / pages.json / visual.json use LF —
all UTF-8 without BOM. So we never force a style: we detect and preserve the
existing file's line ending + BOM, and default new files to LF / no-BOM
(matching what Desktop emits for freshly created page/visual JSON).
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

# (encoding, newline) — utf-8-sig round-trips a BOM; newline drives translation.
Style = tuple[str, str]
DEFAULT_STYLE: Style = ("utf-8", "\n")


def detect_style(path: str | Path) -> Style:
    """Detect (encoding, newline) of an existing file so writes preserve it.

    Falls back to DEFAULT_STYLE for a missing/empty file.
    """
    path = Path(path)
    if not path.exists():
        return DEFAULT_STYLE
    b = path.read_bytes()
    encoding = "utf-8-sig" if b[:3] == b"\xef\xbb\xbf" else "utf-8"
    crlf = b.count(b"\r\n")
    lf_only = b.count(b"\n") - crlf
    newline = "\r\n" if crlf > lf_only else "\n"
    return encoding, newline


def atomic_write(
    path: str | Path, data: str, *, encoding: str = "utf-8", newline: str = "\n"
) -> None:
    """Write `data` to `path` atomically (temp file in same dir + os.replace).

    `data` is expected "\n"-joined; `newline` is applied on write (so pass
    "\r\n" to emit CRLF). `encoding="utf-8-sig"` prepends a BOM.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        with open(tmp, "w", encoding=encoding, newline=newline) as fh:
            fh.write(data)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def backup(path: str | Path) -> Path | None:
    """Copy `path` to a timestamped `.bak-<ts>` sibling; return the backup path.

    Returns None if the source doesn't exist yet (nothing to protect).
    """
    path = Path(path)
    if not path.exists():
        return None
    ts = time.strftime("%Y%m%d-%H%M%S")
    bak = path.with_name(f"{path.name}.bak-{ts}")
    n = 0
    while bak.exists():
        n += 1
        bak = path.with_name(f"{path.name}.bak-{ts}-{n}")
    shutil.copy2(path, bak)
    return bak
