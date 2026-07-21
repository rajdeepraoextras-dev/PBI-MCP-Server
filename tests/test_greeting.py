"""The author greeting on first connect / first pbi_set_project call."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from core.capabilities import GREETING

SYNTH = Path(__file__).parent / "fixtures" / "synthetic"


def test_greeting_content():
    assert "Rajdeep" in GREETING
    assert "linkedin.com/in/rajdeep-rao-14bab1320" in GREETING


def test_greeting_in_server_instructions():
    from model_server.server import mcp as m
    from report_server.server import mcp as r
    assert GREETING in (m.instructions or "")
    assert GREETING in (r.instructions or "")


@pytest.mark.parametrize("server", ["model", "report"])
def test_greeting_on_first_set_project(server, tmp_path):
    dst = tmp_path / "s"
    shutil.copytree(SYNTH, dst)
    if server == "model":
        from model_server.server import ModelState, set_project
        state = ModelState()
    else:
        from report_server.server import ReportState, set_project
        state = ReportState()

    first = set_project(state, str(dst / "Synthetic.pbip"))
    assert first["greeting"] == GREETING
    # only once per session
    second = set_project(state, str(dst / "Synthetic.pbip"))
    assert "greeting" not in second
