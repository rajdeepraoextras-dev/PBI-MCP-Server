"""scripts/pbir_merge.py: structural three-way merge, styles, exit codes, git."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from core import io_safe

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "pbir_merge.py"

_spec = importlib.util.spec_from_file_location("pbir_merge", SCRIPT)
pm = importlib.util.module_from_spec(_spec)
sys.modules["pbir_merge"] = pm
_spec.loader.exec_module(pm)


def merge(base, ours, theirs, **kw):
    return pm.merge_json(base, ours, theirs, **kw)


# --- objects -------------------------------------------------------------------------

def test_disjoint_key_changes_merge_cleanly():
    base = {"a": 1, "b": 2, "c": {"x": 1, "y": 1}}
    ours = {"a": 10, "b": 2, "c": {"x": 1, "y": 1}}
    theirs = {"a": 1, "b": 2, "c": {"x": 5, "y": 1}, "d": True}
    merged, conflicts = merge(base, ours, theirs)
    assert merged == {"a": 10, "b": 2, "c": {"x": 5, "y": 1}, "d": True}
    assert conflicts == []


def test_identical_changes_collapse():
    base = {"a": 1}
    merged, conflicts = merge(base, {"a": 2, "n": [1]}, {"a": 2, "n": [1]})
    assert merged == {"a": 2, "n": [1]} and conflicts == []


def test_key_order_follows_ours_then_theirs_additions():
    base = {"z": 1}
    ours = {"z": 1, "b": 2, "a": 3}
    theirs = {"z": 1, "c": 4}
    merged, _ = merge(base, ours, theirs)
    assert list(merged) == ["z", "b", "a", "c"]


def test_deletion_on_one_side_wins_when_other_untouched():
    base = {"a": 1, "b": 2}
    merged, conflicts = merge(base, {"a": 1}, {"a": 1, "b": 2, "c": 3})
    assert merged == {"a": 1, "c": 3} and conflicts == []


def test_delete_versus_modify_is_a_conflict_keeping_ours():
    base = {"a": 1, "b": 2}
    merged, conflicts = merge(base, {"a": 1}, {"a": 1, "b": 99})
    assert merged == {"a": 1}
    assert conflicts == [{"path": "$.b", "base": 2, "theirs": 99}]


def test_same_leaf_changed_differently_conflicts_with_ours_kept():
    base = {"p": {"q": {"r": 1}}, "other": 1}
    ours = {"p": {"q": {"r": 2}}, "other": 1}
    theirs = {"p": {"q": {"r": 3}}, "other": 5}
    merged, conflicts = merge(base, ours, theirs)
    assert merged == {"p": {"q": {"r": 2}}, "other": 5}
    assert conflicts == [{"path": "$.p.q.r", "base": 1, "ours": 2, "theirs": 3}]


def test_type_mismatch_and_bool_versus_int_conflict():
    m, c = merge({"a": {"x": 1}}, {"a": {"x": 2}}, {"a": 7})
    assert c[0]["path"] == "$.a" and m == {"a": {"x": 2}}
    m, c = merge({"a": 0}, {"a": True}, {"a": 1})     # true is not 1
    assert [x["path"] for x in c] == ["$.a"]


def test_odd_keys_are_bracketed_in_paths():
    _, c = merge({"a b": 1}, {"a b": 2}, {"a b": 3})
    assert c[0]["path"] == '$["a b"]'


def test_no_common_ancestor_add_add():
    merged, conflicts = merge(None, {"a": 1, "s": {"x": 1}}, {"b": 2, "s": {"y": 2}})
    assert merged == {"a": 1, "s": {"x": 1, "y": 2}, "b": 2} and conflicts == []
    _, conflicts = merge(None, {"a": 1}, {"a": 2})
    assert [c["path"] for c in conflicts] == ["$.a"] and "base" not in conflicts[0]


# --- arrays ---------------------------------------------------------------------------------

def _f(name, **kw):
    return {"name": name, **kw}


def test_keyed_arrays_merge_by_name():
    base = {"filters": [_f("F1", v=1), _f("F2", v=1)]}
    ours = {"filters": [_f("F1", v=2), _f("F2", v=1), _f("Ours")]}
    theirs = {"filters": [_f("F1", v=1), _f("F2", v=5), _f("Theirs")]}
    merged, conflicts = merge(base, ours, theirs)
    assert conflicts == []
    assert merged["filters"] == [_f("F1", v=2), _f("F2", v=5), _f("Ours"),
                                 _f("Theirs")]


def test_keyed_arrays_report_element_paths_in_conflicts():
    base = {"filters": [_f("F1", v=1)]}
    ours = {"filters": [_f("F1", v=2)]}
    theirs = {"filters": [_f("F1", v=3)]}
    merged, conflicts = merge(base, ours, theirs)
    assert merged == ours
    assert conflicts == [{"path": "$.filters[name=F1].v",
                          "base": 1, "ours": 2, "theirs": 3}]


def test_keyed_array_removals_and_modify_delete_conflict():
    base = [_f("a", v=1), _f("b", v=1), _f("c", v=1)]
    ours = [_f("a", v=1), _f("c", v=1)]                     # removed b
    theirs = [_f("a", v=1), _f("b", v=1), _f("c", v=2)]      # edited c
    merged, conflicts = merge(base, ours, theirs)
    assert merged == [_f("a", v=1), _f("c", v=2)] and conflicts == []
    theirs2 = [_f("a", v=1), _f("b", v=9), _f("c", v=1)]     # edited the removed b
    merged, conflicts = merge(base, ours, theirs2)
    assert merged == [_f("a", v=1), _f("c", v=1)]
    assert conflicts == [{"path": "$[name=b]", "base": _f("b", v=1),
                          "theirs": _f("b", v=9)}]


def test_id_key_is_used_when_there_is_no_name():
    base = [{"id": 1, "v": 1}, {"id": 2, "v": 1}]
    ours = [{"id": 1, "v": 2}, {"id": 2, "v": 1}]
    theirs = [{"id": 1, "v": 1}, {"id": 2, "v": 3}]
    merged, conflicts = merge(base, ours, theirs)
    assert merged == [{"id": 1, "v": 2}, {"id": 2, "v": 3}] and conflicts == []


def test_theirs_additions_land_after_their_predecessor():
    base = [_f("a"), _f("b")]
    ours = [_f("a"), _f("b"), _f("o")]
    theirs = [_f("a"), _f("t"), _f("b")]
    merged, conflicts = merge(base, ours, theirs)
    assert [x["name"] for x in merged] == ["a", "t", "b", "o"] and not conflicts


def test_theirs_only_reorder_is_adopted():
    base = [_f("a"), _f("b"), _f("c")]
    ours = [_f("a", v=1), _f("b"), _f("c")]
    theirs = [_f("c"), _f("a"), _f("b")]
    merged, _ = merge(base, ours, theirs)
    assert [x["name"] for x in merged] == ["c", "a", "b"]
    assert merged[1] == _f("a", v=1)


def test_other_arrays_are_atomic():
    for base, ours, theirs in (
            ({"pageOrder": ["a"]}, {"pageOrder": ["a", "b"]}, {"pageOrder": ["a", "c"]}),
            ({"s": [1, 2]}, {"s": [1, 2, 3]}, {"s": [1, 2, 4]}),
            ({"o": [{"x": 1}]}, {"o": [{"x": 2}]}, {"o": [{"x": 3}]})):
        merged, conflicts = merge(base, ours, theirs)
        assert merged == ours
        assert [c["path"] for c in conflicts] == ["$." + next(iter(base))]
    # a one-sided array change is fine
    merged, conflicts = merge({"s": [1]}, {"s": [1, 2]}, {"s": [1]})
    assert merged == {"s": [1, 2]} and conflicts == []


def test_duplicate_or_missing_keys_fall_back_to_atomic():
    base = [{"name": "a"}, {"name": "a"}]
    _, c = merge(base, [{"name": "a", "v": 1}, {"name": "a"}],
                 [{"name": "a", "v": 2}, {"name": "a"}])
    assert c and c[0]["path"] == "$"
    _, c = merge([{"x": 1}], [{"x": 2}], [{"x": 3}])
    assert c[0]["path"] == "$"


def test_string_lists_are_opt_in_ordered_sets():
    base = {"pageOrder": ["a", "b"]}
    ours = {"pageOrder": ["a", "b", "o"]}
    theirs = {"pageOrder": ["a", "t", "b"]}
    _, conflicts = merge(base, ours, theirs)
    assert len(conflicts) == 1
    merged, conflicts = merge(base, ours, theirs, merge_string_lists=True)
    assert merged == {"pageOrder": ["a", "t", "b", "o"]} and conflicts == []
    merged, _ = merge({"p": ["a", "b", "c"]}, {"p": ["a", "c"]},
                      {"p": ["a", "b", "c", "d"]}, merge_string_lists=True)
    assert merged == {"p": ["a", "c", "d"]}


# --- text level: style preservation -------------------------------------------------------------

def _dump(obj, indent=2, newline="\n", bom=False, trailing=False, ascii_=False):
    text = json.dumps(obj, indent=indent, ensure_ascii=ascii_)
    if trailing:
        text += "\n"
    data = text.replace("\n", newline).encode("utf-8")
    return (b"\xef\xbb\xbf" + data) if bom else data


def test_merge_texts_keeps_crlf_bom_indent_and_trailing_newline():
    base = {"a": 1, "b": {"c": 1}}
    ours = _dump({"a": 2, "b": {"c": 1}}, indent=4, newline="\r\n", bom=True,
                 trailing=True)
    theirs = _dump({"a": 1, "b": {"c": 9}, "d": "é"}, indent=2)
    out, conflicts = pm.merge_texts(_dump(base), ours, theirs)
    assert conflicts == []
    assert out.startswith(b"\xef\xbb\xbf")
    text = out.decode("utf-8-sig")
    assert text.endswith("\r\n") and "\r\n" in text and "\n" not in text.replace("\r\n", "")
    assert '\r\n    "a": 2' in text                     # 4-space indent kept
    assert json.loads(text) == {"a": 2, "b": {"c": 9}, "d": "é"}
    assert "é" in text                                 # raw UTF-8 kept


def test_merge_texts_tab_indent_and_ascii_escapes():
    base = {"a": 1}
    ours = _dump({"a": 1, "s": "é"}, indent="\t", ascii_=True)
    theirs = _dump({"a": 2})
    out, _ = pm.merge_texts(_dump(base), ours, theirs)
    text = out.decode("utf-8")
    assert '\n\t"a": 2' in text and "\\u00e9" in text and not text.endswith("\n")


def test_unchanged_relative_to_ours_is_returned_byte_for_byte():
    ours = b'{ "a": 1,\n   "b":   [1,2] }\r\n'          # deliberately odd style
    out, conflicts = pm.merge_texts(b'{"a": 0}', ours, b'{"a": 1}')
    assert out == ours and conflicts == []


def test_detect_style_matches_core_io_safe(tmp_path):
    cases = [b"{}\n", b"{}\r\n", b'\xef\xbb\xbf{\r\n"a": 1\r\n}\r\n',
             b'{\n"a": 1,\r\n"b": 2\n}', b"", b"x\r\ny\nz\r\n"]
    for i, raw in enumerate(cases):
        f = tmp_path / f"c{i}.json"
        f.write_bytes(raw)
        assert pm.detect_style(raw) == io_safe.detect_style(f)


def test_invalid_json_raises_value_error():
    with pytest.raises(ValueError, match="theirs is not valid JSON"):
        pm.merge_texts(b"{}", b"{}", b"{oops")


# --- CLI ---------------------------------------------------------------------------------------------

def run(*args, cwd=None):
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                          capture_output=True, text=True, cwd=cwd)


def write_files(tmp_path, base, ours, theirs):
    paths = {}
    for name, obj in (("base", base), ("ours", ours), ("theirs", theirs)):
        p = tmp_path / f"{name}.json"
        p.write_bytes(obj if isinstance(obj, bytes) else _dump(obj))
        paths[name] = p
    return paths


def test_cli_clean_merge_into_a_separate_out_file(tmp_path):
    f = write_files(tmp_path, {"a": 1, "b": 1}, {"a": 2, "b": 1}, {"a": 1, "b": 3})
    out = tmp_path / "out.json"
    r = run(f["base"], f["ours"], f["theirs"], out)
    assert r.returncode == 0, r.stderr
    assert json.loads(out.read_text(encoding="utf-8")) == {"a": 2, "b": 3}
    assert not (tmp_path / "out.json.pbir-conflicts.json").exists()
    assert json.loads(f["ours"].read_text(encoding="utf-8")) == {"a": 2, "b": 1}


def test_cli_out_may_be_the_ours_file_like_git_passes_it(tmp_path):
    f = write_files(tmp_path, {"a": 1, "b": 1}, {"a": 2, "b": 1}, {"a": 1, "b": 3})
    r = run(f["base"], f["ours"], f["theirs"], f["ours"])
    assert r.returncode == 0, r.stderr
    assert json.loads(f["ours"].read_text(encoding="utf-8")) == {"a": 2, "b": 3}
    assert not list(tmp_path.glob("*.tmp-*"))


def test_cli_conflict_exit_1_writes_conflicts_and_keeps_ours(tmp_path):
    f = write_files(tmp_path, {"a": 1, "b": 1, "c": {"d": 1}},
                    {"a": 2, "b": 1, "c": {"d": 1}},
                    {"a": 3, "b": 9, "c": {"d": 1}})
    r = run(f["base"], f["ours"], f["theirs"], f["ours"], "Rep.Report/x/page.json")
    assert r.returncode == 1
    assert "1 conflict" in r.stderr and "$.a" in r.stderr
    assert json.loads(f["ours"].read_text(encoding="utf-8")) == {
        "a": 2, "b": 9, "c": {"d": 1}}                 # ours at the conflict, theirs elsewhere
    report = json.loads((tmp_path / "ours.json.pbir-conflicts.json")
                        .read_text(encoding="utf-8"))
    assert report == {"file": "Rep.Report/x/page.json", "count": 1,
                      "conflicts": [{"path": "$.a", "base": 1, "ours": 2,
                                     "theirs": 3}]}


def test_cli_clean_merge_removes_a_stale_conflicts_file(tmp_path):
    f = write_files(tmp_path, {"a": 1}, {"a": 2}, {"a": 1})
    stale = tmp_path / "out.json.pbir-conflicts.json"
    stale.write_text("{}", encoding="utf-8")
    assert run(f["base"], f["ours"], f["theirs"], tmp_path / "out.json").returncode == 0
    assert not stale.exists()


def test_cli_missing_or_empty_base_means_add_add(tmp_path):
    f = write_files(tmp_path, b"", {"a": 1}, {"b": 2})
    out = tmp_path / "o.json"
    assert run(f["base"], f["ours"], f["theirs"], out).returncode == 0
    assert json.loads(out.read_text(encoding="utf-8")) == {"a": 1, "b": 2}
    f["base"].unlink()
    assert run(f["base"], f["ours"], f["theirs"], out).returncode == 0


def test_cli_errors_exit_2_and_leave_out_untouched(tmp_path):
    f = write_files(tmp_path, {"a": 1}, {"a": 2}, b"{ broken")
    original = f["ours"].read_bytes()
    r = run(f["base"], f["ours"], f["theirs"], f["ours"])
    assert r.returncode == 2 and "not valid JSON" in r.stderr
    assert f["ours"].read_bytes() == original
    assert run(f["base"]).returncode == 2                       # wrong arg count
    r = run(f["base"], tmp_path / "missing.json", f["theirs"], tmp_path / "o")
    assert r.returncode == 2


def test_cli_survives_a_label_the_console_cannot_encode(tmp_path):
    import os

    f = write_files(tmp_path, {"a": 1}, {"a": 2}, {"a": 3})
    env = {**os.environ, "PYTHONIOENCODING": "ascii"}
    r = subprocess.run([sys.executable, str(SCRIPT), str(f["base"]), str(f["ours"]),
                        str(f["theirs"]), str(f["ours"]), "Übersicht.Report/page.json"],
                       capture_output=True, env=env)
    assert r.returncode == 1 and b"Traceback" not in r.stderr
    assert b"1 conflict" in r.stderr
    report = json.loads((tmp_path / "ours.json.pbir-conflicts.json").read_text(
        encoding="utf-8"))
    assert report["file"] == "Übersicht.Report/page.json"


def test_cli_string_list_flag(tmp_path):
    f = write_files(tmp_path, {"pageOrder": ["a"]}, {"pageOrder": ["a", "o"]},
                    {"pageOrder": ["a", "t"]})
    out = tmp_path / "o.json"
    assert run(f["base"], f["ours"], f["theirs"], out).returncode == 1
    assert run("--merge-string-lists", f["base"], f["ours"], f["theirs"],
               out).returncode == 0
    assert json.loads(out.read_text(encoding="utf-8"))["pageOrder"] == ["a", "o", "t"]


# --- real PBIR files ----------------------------------------------------------------------------------------

SYNTH = Path(__file__).parent / "fixtures" / "synthetic"


def _load(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def test_two_branches_edit_different_parts_of_one_visual(tmp_path):
    vis = SYNTH / "Synthetic.Report/definition/pages/overview/visuals/bar1/visual.json"
    base = _load(vis)
    ours, theirs = json.loads(json.dumps(base)), json.loads(json.dumps(base))
    ours["position"]["x"] = 100                                   # moved on our side
    theirs["visual"]["visualContainerObjects"]["title"][0]["properties"][
        "text"]["expr"]["Literal"]["Value"] = "'Retitled'"        # retitled on theirs
    theirs["filterConfig"] = {"filters": [{"name": "F1", "type": "Categorical"}]}
    merged, conflicts = merge(base, ours, theirs)
    assert conflicts == []
    assert merged["position"]["x"] == 100
    assert merged["visual"]["visualContainerObjects"]["title"][0]["properties"][
        "text"]["expr"]["Literal"]["Value"] == "'Retitled'"
    assert merged["filterConfig"]["filters"][0]["name"] == "F1"


# --- git integration ----------------------------------------------------------------------------------------------

def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com",
         "-c", "core.autocrlf=false", "-c", "commit.gpgsign=false", *args],
        cwd=repo, capture_output=True, text=True, check=check)


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
@pytest.mark.parametrize("pattern, rel", [
    ("*.Report/**/*.json", "R.Report/definition/report.json"),
    ("**/*.Report/**/*.json", "sub/dir/R.Report/definition/report.json"),
])
def test_real_git_merge_uses_the_driver(tmp_path, pattern, rel):
    repo = tmp_path / "repo"
    target = repo / rel
    target.parent.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "checkout", "-q", "-b", "main")
    (repo / ".gitattributes").write_text(f"{pattern} merge=pbir\n",
                                         encoding="utf-8")
    script = SCRIPT.as_posix()
    py = Path(sys.executable).as_posix()
    _git(repo, "config", "merge.pbir.name", "PBIR JSON structural merge")
    _git(repo, "config", "merge.pbir.driver",
         f'"{py}" "{script}" %O %A %B %A %P')

    def save(obj):
        target.write_bytes(_dump(obj, indent=2, trailing=True))

    base = {"settings": {"a": 1, "b": 1},
            "filterConfig": {"filters": [{"name": "F1", "v": 1}]}}
    save(base)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    feat = json.loads(json.dumps(base))
    feat["settings"]["b"] = 2
    feat["filterConfig"]["filters"].append({"name": "F2", "v": 1})
    save(feat)
    _git(repo, "commit", "-q", "-am", "feature")
    _git(repo, "checkout", "-q", "main")
    main = json.loads(json.dumps(base))
    main["settings"]["a"] = 2
    main["filterConfig"]["filters"][0]["v"] = 2
    save(main)
    _git(repo, "commit", "-q", "-am", "main")

    res = _git(repo, "merge", "feature", "-m", "merge", check=False)
    assert res.returncode == 0, res.stdout + res.stderr
    merged = _load(target)
    assert merged == {"settings": {"a": 2, "b": 2},
                      "filterConfig": {"filters": [{"name": "F1", "v": 2},
                                                   {"name": "F2", "v": 1}]}}
    assert target.read_bytes().endswith(b"}\n")              # style kept

    # now a real conflict: both branches change settings.a differently
    _git(repo, "checkout", "-q", "-b", "c1")
    c1 = _load(target)
    c1["settings"]["a"] = 100
    save(c1)
    _git(repo, "commit", "-q", "-am", "c1")
    _git(repo, "checkout", "-q", "main")
    c2 = _load(target)
    c2["settings"]["a"] = 200
    save(c2)
    _git(repo, "commit", "-q", "-am", "c2")
    res = _git(repo, "merge", "c1", "-m", "conflict", check=False)
    assert res.returncode != 0
    status = _git(repo, "status", "--porcelain").stdout
    assert f"UU {rel}" in status
    assert _load(target)["settings"]["a"] == 200             # ours kept
    reports = [p for p in repo.rglob("*.pbir-conflicts.json")
               if ".git" not in p.parts]
    assert len(reports) == 1
    data = json.loads(reports[0].read_text(encoding="utf-8"))
    assert data["file"] == rel
    assert data["conflicts"] == [{"path": "$.settings.a", "base": 2,
                                  "ours": 200, "theirs": 100}]
