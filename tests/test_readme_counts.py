"""scripts/update_readme_counts.py keeps README.md's numbers in step with the code."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
README = REPO / "README.md"


def _load():
    spec = importlib.util.spec_from_file_location(
        "update_readme_counts", REPO / "scripts" / "update_readme_counts.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def script():
    return _load()


def test_readme_tool_counts_match_the_live_servers(script):
    model, report = script.live_tool_counts()
    written = script.readme_tool_counts(README.read_text(encoding="utf-8"))
    assert written is not None, "README lost its '**N tools** (a model + b report)' sentence"
    assert written == (model + report, model, report), (
        f"README says {written[0]} tools ({written[1]} model + {written[2]} "
        f"report) but the servers register {model + report} ({model} + "
        f"{report}). Run: python scripts/update_readme_counts.py")


def test_readme_has_a_test_count_line_to_maintain(script):
    # informational only: the number changes with every test that is added
    assert script.readme_test_count(README.read_text(encoding="utf-8")) is not None


def test_check_mode_agrees_with_the_readme(script, capsys):
    assert script.main(["--check", "--skip-tests"]) == 0
    assert "up to date" in capsys.readouterr().out


SAMPLE = """\
hundreds of measures - from a prompt, in minutes. **57 tools** (13 model +
44 report) across two MCP servers; every report write is pre-flight
validated.

```
  tests/           # 299 tests; fixtures/ (synthetic + real, gitignored)
```
"""


def test_apply_counts_rewrites_both_numbers_in_place(script):
    out = script.apply_counts(SAMPLE, 19, 50, 412)
    assert "**69 tools** (19 model +\n50 report) across two MCP servers" in out
    assert "tests/           # 412 tests; fixtures/" in out
    expected = SAMPLE.replace("**57 tools** (13 model +\n44 report)",
                              "**69 tools** (19 model +\n50 report)") \
        .replace("# 299 tests;", "# 412 tests;")
    assert out == expected                      # nothing else moved


def test_apply_counts_is_idempotent(script):
    once = script.apply_counts(SAMPLE, 19, 50, 412)
    assert script.apply_counts(once, 19, 50, 412) == once


def test_apply_counts_can_leave_the_test_count_alone(script):
    out = script.apply_counts(SAMPLE, 13, 44, None)
    assert out == SAMPLE
    assert "299 tests;" in script.apply_counts(SAMPLE, 20, 44, None)


def test_apply_counts_keeps_crlf_and_single_line_layout(script):
    text = SAMPLE.replace("\n", "\r\n").replace("+\r\n44", "+ 44")
    out = script.apply_counts(text, 100, 250, 1000)
    assert "**350 tools** (100 model + 250 report)" in out
    assert out.count("\r\n") == text.count("\r\n") and "\n" not in out.replace("\r\n", "")
    assert "# 1000 tests;" in out


def test_apply_counts_reads_back_what_it_wrote(script):
    out = script.apply_counts(SAMPLE, 7, 8, 9)
    assert script.readme_tool_counts(out) == (15, 7, 8)
    assert script.readme_test_count(out) == 9


def test_apply_counts_refuses_a_readme_without_the_sentence(script):
    with pytest.raises(ValueError, match="tools"):
        script.apply_counts("no counts here", 1, 2, None)
    with pytest.raises(ValueError, match="tests"):
        script.apply_counts("**3 tools** (1 model + 2 report)", 1, 2, 5)


class _Done:
    def __init__(self, stdout):
        self.stdout = stdout


@pytest.mark.parametrize("stdout, expected", [
    ("346 tests collected in 1.20s\n", 346),
    ("tests/a.py::t[9 tests collected in 1s]\n\n1 test collected in 0.01s", 1),
    ("12/14 tests collected (2 deselected) in 0.10s", 12),
    ("no tests ran in 0.01s", None),
    ("", None),
], ids=["plural", "test-id-mentions-the-phrase", "deselected", "none", "empty"])
def test_collected_test_count_parses_pytest_output(script, monkeypatch, stdout, expected):
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: _Done(stdout))
    assert script.collected_test_count() == expected
