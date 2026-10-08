"""The two per-file ratchet gates: a committed `{path: count}`
baseline that only shrinks. mypy: a touched file cannot gain errors and debt
cannot move between files; stub-only environment lines are reported, not
counted. Function length: ruff PLR0915 per file, a fixture repo gaining an
oversized function fails, a shrinking one lowers the baseline on --update.
A gate that cannot read its baseline or its tool's output fails (exit 2)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from scripts.gates import function_length_gate as fl_gate
from scripts.gates import mypy_gate, ratchet

ROOT = Path(__file__).resolve().parents[2]
GATES = ROOT / "scripts" / "gates"


# ---- shared compare --------------------------------------------------------


def test_compare_flags_growth_per_file_not_in_total() -> None:
    base = {"a.py": 3, "b.py": 2}
    # Total is unchanged (5) but debt moved from a to b: that is a regression.
    rep = ratchet.compare(base, {"a.py": 2, "b.py": 3})
    assert rep.regressions == {"b.py": (3, 2)}
    assert rep.decreases == {"a.py": (2, 3)}
    assert not rep.ok


def test_compare_new_file_has_zero_baseline_and_removed_file_is_a_decrease() -> None:
    rep = ratchet.compare({"gone.py": 4}, {"new.py": 1})
    assert rep.regressions == {"new.py": (1, 0)}
    assert rep.decreases == {"gone.py": (0, 4)}


def test_lowered_never_raises_and_drops_zeroes() -> None:
    assert ratchet.lowered({"a.py": 3, "b.py": 2}, {"a.py": 1, "b.py": 5, "c.py": 7}) == {
        "a.py": 1,
        "b.py": 2,
    }


def test_write_baseline_is_sorted_and_one_entry_per_line(tmp_path: Path) -> None:
    p = tmp_path / "b.json"
    ratchet.write_baseline(p, {"z.py": 1, "a.py": 2, "m.py": 0})
    assert json.loads(p.read_text()) == {"a.py": 2, "z.py": 1}
    assert p.read_text().splitlines()[1].strip().startswith('"a.py"')


def test_write_then_load_round_trips(tmp_path: Path) -> None:
    p = tmp_path / "b.json"
    ratchet.write_baseline(p, {"b.py": 2, "a.py::f": 300})
    assert ratchet.load_baseline(p) == {"a.py::f": 300, "b.py": 2}


@pytest.mark.parametrize(
    "text,why",
    [
        ("", "not valid JSON"),
        ("[1, 2]", "JSON object"),
        ('{"a.py": 1, "a.py": 2}', "duplicate key 'a.py'"),
        ('{"a/b.py": 1, "a\\\\b.py": 2}', "after normalising"),
        ('{"a.py": "3"}', "non-negative integer"),
        ('{"a.py": 1.5}', "non-negative integer"),
        ('{"a.py": true}', "non-negative integer"),
        ('{"a.py": -1}', "non-negative integer"),
    ],
)
def test_malformed_baseline_fails_the_gate(tmp_path: Path, text: str, why: str) -> None:
    p = tmp_path / "b.json"
    p.write_text(text)
    with pytest.raises(ratchet.GateError, match=why):
        ratchet.load_baseline(p)


def test_windows_separators_in_a_baseline_are_normalised(tmp_path: Path) -> None:
    p = tmp_path / "b.json"
    p.write_text(json.dumps({"server\\x.py": 2}))
    assert ratchet.load_baseline(p) == {"server/x.py": 2}


class _Fixed(ratchet.Ratchet):
    name, unit, command = "fixed-gate", "error(s)", "fixed"

    def __init__(self, path: Path, counts: dict[str, int]) -> None:
        super().__init__(path)
        self.counts = counts

    def measure(self) -> dict[str, int]:
        return self.counts

    def headline(self, current, baseline) -> list[str]:
        return []

    def failure(self, report) -> str:
        return "regressed"


def test_update_never_writes_while_any_key_regressed(tmp_path: Path) -> None:
    p = tmp_path / "b.json"
    p.write_text(json.dumps({"a.py": 3, "b.py": 1}))
    before = p.read_text()
    # a fell, b grew: the win on a must not be locked in alongside b's growth
    assert _Fixed(p, {"a.py": 1, "b.py": 2}).run(update=True) == 1
    assert p.read_text() == before


def test_update_prunes_keys_the_tree_no_longer_has(tmp_path: Path, capsys) -> None:
    p = tmp_path / "b.json"
    p.write_text(json.dumps({"gone.py": 2, "kept.py": 1}))
    assert _Fixed(p, {"kept.py": 1}).run(update=False) == 0
    assert "cleared    gone.py" in capsys.readouterr().out
    assert _Fixed(p, {"kept.py": 1}).run(update=True) == 0
    assert json.loads(p.read_text()) == {"kept.py": 1}


def test_missing_baseline_fails_and_update_does_not_create_one(tmp_path: Path, capsys) -> None:
    p = tmp_path / "absent.json"
    assert _Fixed(p, {}).run(update=False) == 2
    assert _Fixed(p, {"a.py": 1}).run(update=True) == 2
    assert not p.exists()
    assert "cannot read baseline" in capsys.readouterr().out


# ---- mypy gate: parsing ----------------------------------------------------

MYPY_OUT = """\
server/x.py:10: error: Incompatible return value type (got "int", expected "str")  [return-value]
server/x.py:12: error: Missing type parameters for generic type "list"  [type-arg]
server/y.py:3: error: Library stubs not installed for "yaml"  [import-untyped]
server/y.py:3: note: Hint: "python3 -m pip install types-PyYAML"
server/y.py:3: note: (or run "mypy --install-types" to install all missing stub packages)
server/z.py:8: error: Library stubs not installed for "yaml"  [import-untyped]
server/w.py:8: error: Library stubs not installed for "toml"  [import-untyped]
server/w.py:8: note: Hint: "python3 -m pip install types-toml"
sdk/python/loom/k.py:1: error: Returning Any from function declared to return "str"  [no-any-return]
Found 6 errors in 5 files (checked 3 source files)
"""


def test_parse_counts_per_file_and_excludes_only_listed_stub_packages() -> None:
    counts, env_only = mypy_gate.parse(MYPY_OUT, listed_stubs={"types-pyyaml"})
    assert counts == {"server/x.py": 2, "server/w.py": 1, "sdk/python/loom/k.py": 1}
    # The hint is printed once per module; the second yaml line is still excluded.
    assert [ln.split(":")[0] for ln in env_only] == ["server/y.py", "server/z.py"]


def test_parse_counts_stubs_the_project_does_not_list() -> None:
    counts, env_only = mypy_gate.parse(MYPY_OUT, listed_stubs=set())
    assert counts["server/y.py"] == 1 and counts["server/z.py"] == 1 and counts["server/w.py"] == 1
    assert env_only == []


def test_parse_reads_column_numbers_and_windows_paths() -> None:
    out = (
        'server/a.py:3:5: error: Name "x" is not defined  [name-defined]\n'
        "C:\\repo\\server\\b.py:4: error: Bad  [misc]\n"
    )
    counts, _ = mypy_gate.parse(out, listed_stubs=set())
    assert counts == {"server/a.py": 1, "C:/repo/server/b.py": 1}


def test_reported_total_reads_the_summary_or_fails() -> None:
    assert mypy_gate.reported_total(MYPY_OUT) == 6
    assert mypy_gate.reported_total("Found 1 error in 1 file (checked 2 source files)") == 1
    assert mypy_gate.reported_total("Success: no issues found in 3 source files") == 0
    with pytest.raises(ratchet.GateError):
        mypy_gate.reported_total("")


def test_listed_stub_packages_reads_pyproject() -> None:
    listed = mypy_gate._listed_stub_packages(ROOT / "pyproject.toml")
    assert "types-pyyaml" in listed


# ---- mypy gate: main against a fake mypy run --------------------------------


def _run_mypy_gate(
    monkeypatch, tmp_path: Path, output: str, baseline: dict[str, int] | None, *args
):
    monkeypatch.setattr(mypy_gate, "run_mypy", lambda: output)
    bl = tmp_path / "mypy_baseline.json"
    if baseline is not None:
        bl.write_text(json.dumps(baseline))
    return mypy_gate.main(["--baseline", str(bl), *args]), bl


def test_mypy_gate_fails_when_a_file_grows_even_if_total_falls(monkeypatch, tmp_path, capsys):
    rc, _ = _run_mypy_gate(
        monkeypatch,
        tmp_path,
        MYPY_OUT,
        {"server/x.py": 1, "server/w.py": 5, "sdk/python/loom/k.py": 1},
    )
    out = capsys.readouterr().out
    assert rc == 1
    assert "REGRESSION server/x.py: 2 error(s) (baseline 1)" in out
    assert "server/x.py:10: error" in out  # the offending lines are printed


def test_mypy_gate_reports_stub_only_divergence_instead_of_failing(monkeypatch, tmp_path, capsys):
    rc, _ = _run_mypy_gate(
        monkeypatch,
        tmp_path,
        MYPY_OUT,
        {"server/x.py": 2, "server/w.py": 1, "sdk/python/loom/k.py": 1},
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "2 environment-only line(s) excluded" in out
    assert "types-PyYAML" in out or 'stubs not installed for "yaml"' in out


def test_mypy_gate_update_lowers_but_never_raises(monkeypatch, tmp_path, capsys):
    rc, bl = _run_mypy_gate(
        monkeypatch,
        tmp_path,
        MYPY_OUT,
        {"server/x.py": 4, "server/w.py": 1, "sdk/python/loom/k.py": 1, "server/old.py": 2},
        "--update",
    )
    assert rc == 0
    assert json.loads(bl.read_text()) == {
        "server/x.py": 2,
        "server/w.py": 1,
        "sdk/python/loom/k.py": 1,
    }
    assert "baseline lowered" in capsys.readouterr().out


def test_mypy_gate_without_a_baseline_fails_even_on_update(monkeypatch, tmp_path):
    rc, bl = _run_mypy_gate(monkeypatch, tmp_path, MYPY_OUT, None, "--update")
    assert rc == 2
    assert not bl.exists()


def test_mypy_gate_fails_when_lines_go_unparsed(monkeypatch, tmp_path, capsys):
    # A format the parser does not read would otherwise count as zero errors.
    out = "server/x.py: error: something new\nFound 1 error in 1 file (checked 1 source file)\n"
    rc, _ = _run_mypy_gate(monkeypatch, tmp_path, out, {})
    assert rc == 2
    assert "parsed 0 error line(s) but mypy reported 1" in capsys.readouterr().out


def test_run_mypy_fails_on_a_crash_exit(monkeypatch):
    class _Proc:
        returncode, stdout, stderr = 2, "", "mypy: error: bad config"

    monkeypatch.setattr(mypy_gate.subprocess, "run", lambda *a, **k: _Proc())
    with pytest.raises(ratchet.GateError, match="exited 2"):
        mypy_gate.run_mypy()


def test_mypy_gate_unparseable_output_fails(monkeypatch, tmp_path):
    rc, _ = _run_mypy_gate(monkeypatch, tmp_path, "Traceback: mypy exploded", {})
    assert rc == 2


def test_committed_mypy_baseline_matches_shape() -> None:
    data = json.loads((GATES / "mypy_baseline.json").read_text())
    # empty once every package is paid down; a listed file carries a positive count
    assert all(isinstance(v, int) and v > 0 for v in data.values())
    assert all(k.startswith(("server/", "sdk/python/loom/")) for k in data)
    # The sdk is paid down to zero and must stay there.
    assert not [k for k in data if k.startswith("sdk/")]


# ---- function-length gate: fixture repo ------------------------------------


def _big_function(name: str, statements: int) -> str:
    body = "\n".join(f"    x{i} = {i}" for i in range(statements))
    return f"def {name}():\n{body}\n    return x0\n\n"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "server").mkdir()
    (tmp_path / "sdk" / "python").mkdir(parents=True)
    shutil.copy(ROOT / "pyproject.toml", tmp_path / "pyproject.toml")
    (tmp_path / "server" / "big.py").write_text(
        _big_function("huge", 60) + _big_function("small", 5)
    )
    (tmp_path / "sdk" / "python" / "ok.py").write_text(_big_function("fine", 10))
    gates = tmp_path / "scripts" / "gates"
    gates.mkdir(parents=True)
    (gates / "function_length_baseline.json").write_text("{}")
    (gates / "function_length_lines_baseline.json").write_text("{}")
    return tmp_path


def test_collector_counts_oversized_functions_per_file(repo: Path) -> None:
    findings = fl_gate.ruff_findings(repo, fl_gate.TARGETS)
    counts = fl_gate.counts_from_findings(repo, findings)
    assert counts == {"server/big.py": 1}
    inv = fl_gate.inventory(repo, findings)
    assert inv[0] == fl_gate.Oversized("server/big.py", 1, "huge", statements=60, lines=62)


def test_gate_passes_at_baseline_and_fails_when_a_file_gains_one(repo: Path, capsys) -> None:
    bl = repo / "baseline.json"
    bl.write_text(json.dumps({"server/big.py": 1}))
    assert fl_gate.main(["--root", str(repo), "--baseline", str(bl)]) == 0
    with (repo / "server" / "big.py").open("a") as f:
        f.write(_big_function("second", 70))
    assert fl_gate.main(["--root", str(repo), "--baseline", str(bl)]) == 1
    out = capsys.readouterr().out
    assert "REGRESSION server/big.py: 2 oversized function(s) (baseline 1)" in out
    assert "server/big.py:1: huge has 60 statements" in out
    assert "server/big.py:72: second has 70 statements" in out


def test_gate_blocks_a_new_file_with_an_oversized_function(repo: Path) -> None:
    bl = repo / "baseline.json"
    bl.write_text(json.dumps({"server/big.py": 1}))
    (repo / "server" / "fresh.py").write_text(_big_function("brand_new", 55))
    assert fl_gate.main(["--root", str(repo), "--baseline", str(bl)]) == 1


def test_gate_update_lowers_baseline_when_a_function_shrinks(repo: Path, capsys) -> None:
    bl = repo / "baseline.json"
    bl.write_text(json.dumps({"server/big.py": 1, "server/removed.py": 2}))
    (repo / "server" / "big.py").write_text(_big_function("huge", 20))
    assert fl_gate.main(["--root", str(repo), "--baseline", str(bl)]) == 0
    assert "--update" in capsys.readouterr().out
    assert fl_gate.main(["--root", str(repo), "--baseline", str(bl), "--update"]) == 0
    assert json.loads(bl.read_text()) == {}


def test_inventory_lists_long_functions_by_lines(repo: Path, capsys) -> None:
    (repo / "server" / "long.py").write_text(
        _big_function("tall", 30).replace("\n    x", "\n\n    x")
    )
    assert fl_gate.main(["--root", str(repo), "--inventory"]) == 0
    out = capsys.readouterr().out
    assert "function(s) over 50 statements" in out and "server/big.py::huge" in out
    long = fl_gate.long_functions(repo, fl_gate.TARGETS, 50)
    assert {fn.function for fn in long} == {"tall", "huge"}  # by lines, not statements
    assert fl_gate.long_functions(repo, fl_gate.TARGETS, 200) == []


def test_committed_function_length_baseline_matches_shape() -> None:
    data = json.loads((GATES / "function_length_baseline.json").read_text())
    assert data and all(isinstance(v, int) and v > 0 for v in data.values())
    assert all(k.startswith(("server/", "sdk/python/")) for k in data)


# ---- function-length gate: per-function line ratchet --------


def _tall_function(name: str, lines: int) -> str:
    """A function of exactly `lines` lines with few statements: it is long,
    not statement-heavy, so only the line ratchet sees it."""
    body = "\n".join(f"    # filler {i}" for i in range(lines - 2))
    return f"def {name}():\n{body}\n    return 1\n\n"


def test_line_counts_keys_are_file_and_qualified_function(repo: Path) -> None:
    (repo / "server" / "tall.py").write_text(
        "class K:\n" + "\n".join("    " + ln for ln in _tall_function("m", 30).splitlines()) + "\n"
    )
    counts = {fn.key: fn.lines for fn in fl_gate.long_functions(repo, fl_gate.TARGETS, 20)}
    assert counts == {"server/tall.py::K.m": 30, "server/big.py::huge": 62}


def test_lines_gate_blocks_growth_and_new_crossings_only(repo: Path, capsys) -> None:
    (repo / "server" / "tall.py").write_text(_tall_function("tall", 260))
    bl = repo / "lines.json"
    bl.write_text(json.dumps({"server/tall.py::tall": 260}))
    assert fl_gate.LineRatchet(repo, bl).run(update=False) == 0
    # growing a listed function past its entry fails
    (repo / "server" / "tall.py").write_text(_tall_function("tall", 261))
    assert fl_gate.LineRatchet(repo, bl).run(update=False) == 1
    out = capsys.readouterr().out
    assert "REGRESSION server/tall.py::tall: 261 lines (baseline 260)" in out
    assert "server/tall.py:1: tall is 261 lines" in out
    # a new function crossing 200 lines fails; one under it is invisible
    (repo / "server" / "tall.py").write_text(
        _tall_function("tall", 260) + _tall_function("fresh", 201)
    )
    assert fl_gate.LineRatchet(repo, bl).run(update=False) == 1
    (repo / "server" / "tall.py").write_text(
        _tall_function("tall", 260) + _tall_function("fresh", 200)
    )
    assert fl_gate.LineRatchet(repo, bl).run(update=False) == 0


def test_lines_gate_update_lowers_and_drops_functions_under_the_line(repo: Path, capsys) -> None:
    (repo / "server" / "tall.py").write_text(
        _tall_function("tall", 230) + _tall_function("gone", 150)
    )
    bl = repo / "lines.json"
    bl.write_text(json.dumps({"server/tall.py::tall": 260, "server/tall.py::gone": 250}))
    assert fl_gate.LineRatchet(repo, bl).run(update=False) == 0
    assert "--update" in capsys.readouterr().out
    assert fl_gate.LineRatchet(repo, bl).run(update=True) == 0
    assert json.loads(bl.read_text()) == {"server/tall.py::tall": 230}


def test_main_runs_both_ratchets(repo: Path, capsys) -> None:
    bl = repo / "baseline.json"
    bl.write_text(json.dumps({"server/big.py": 1}))
    lines_bl = repo / "lines.json"
    lines_bl.write_text(json.dumps({}))
    (repo / "server" / "tall.py").write_text(_tall_function("tall", 230))
    # the per-file count passes (no new PLR0915 finding) but the line ratchet fails
    assert (
        fl_gate.main(
            ["--root", str(repo), "--baseline", str(bl), "--lines-baseline", str(lines_bl)]
        )
        == 1
    )
    assert "server/tall.py::tall: 230 lines (baseline 0)" in capsys.readouterr().out


def test_main_reports_both_ratchets_when_both_regress(repo: Path, capsys) -> None:
    (repo / "server" / "tall.py").write_text(_tall_function("tall", 230))
    (repo / "server" / "fresh.py").write_text(_big_function("brand_new", 55))
    assert fl_gate.main(["--root", str(repo)]) == 1
    out = capsys.readouterr().out
    assert "REGRESSION server/fresh.py: 1 oversized" in out
    assert "REGRESSION server/tall.py::tall: 230 lines" in out


def test_gate_fails_on_an_unparsable_file(repo: Path, capsys) -> None:
    (repo / "server" / "broken.py").write_text("def f(:\n")
    assert fl_gate.LineRatchet(repo, repo / fl_gate.LINES_BASELINE).run() == 2
    assert "cannot parse" in capsys.readouterr().out


def test_gate_fails_on_a_missing_target(repo: Path, capsys) -> None:
    shutil.rmtree(repo / "sdk")
    assert fl_gate.main(["--root", str(repo)]) == 2
    assert "missing" in capsys.readouterr().out


def test_gate_fails_when_ruff_prints_nothing(repo: Path, monkeypatch, capsys) -> None:
    class _Proc:
        returncode, stdout, stderr = 0, "", ""

    monkeypatch.setattr(fl_gate.subprocess, "run", lambda *a, **k: _Proc())
    assert fl_gate.main(["--root", str(repo)]) == 2
    assert "no JSON findings list" in capsys.readouterr().out


def test_committed_lines_baseline_matches_shape_and_is_the_over_200_set() -> None:
    data = json.loads((GATES / "function_length_lines_baseline.json").read_text())
    assert all(isinstance(v, int) and v > fl_gate.INVENTORY_LINES for v in data.values())
    assert all("::" in k and k.startswith(("server/", "sdk/python/")) for k in data)
    assert len(data) <= 56, "the per-function baseline only shrinks"


@pytest.mark.parametrize(
    "name",
    ["mypy_baseline.json", "function_length_baseline.json", "function_length_lines_baseline.json"],
)
def test_committed_baseline_is_in_the_form_update_writes(name: str, tmp_path: Path) -> None:
    """Sorted keys, one per line: a hand edit or a merge cannot reorder it, so
    `--update` diffs show only the counts that moved."""
    path = GATES / name
    canonical = tmp_path / name
    ratchet.write_baseline(canonical, ratchet.load_baseline(path))
    assert path.read_text() == canonical.read_text(), f"regenerate {name} with --update"
