"""A test run's split into parts (`tests/_shards.py`)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests import _shards


@pytest.mark.parametrize(("spec", "want"), [("1/1", (1, 1)), ("2/4", (2, 4)), ("4/4", (4, 4))])
def test_a_shard_spec_reads_as_index_and_count(spec: str, want: tuple[int, int]) -> None:
    assert _shards.parse(spec) == want


@pytest.mark.parametrize("spec", ["0/4", "5/4", "4", "a/b", "1/", "/4", "-1/4", "1/0", "²/4"])
def test_a_malformed_or_out_of_range_spec_is_refused(spec: str) -> None:
    with pytest.raises(ValueError, match="LOOM_TEST_SHARD"):
        _shards.parse(spec)


def test_every_file_lands_in_exactly_one_part_and_the_parts_are_within_one_file() -> None:
    weights = {f"tests/integration/test_{i:03d}.py": (i * 37) % 50 + 1 for i in range(200)}
    part = _shards.assign(weights, 4)
    assert part.keys() == weights.keys()
    assert set(part.values()) == {1, 2, 3, 4}
    load = [sum(w for f, w in weights.items() if part[f] == k) for k in (1, 2, 3, 4)]
    assert max(load) - min(load) <= max(weights.values())


def test_the_split_depends_only_on_the_files_and_their_weights() -> None:
    weights = {"b.py": 5, "a.py": 5, "c.py": 9, "d.py": 1}
    reordered = dict(reversed(list(weights.items())))
    assert (
        _shards.assign(weights, 2)
        == _shards.assign(reordered, 2)
        == {
            "c.py": 1,
            "a.py": 2,
            "b.py": 2,
            "d.py": 1,
        }
    )


def test_a_file_weighs_its_recorded_seconds_else_its_tests_at_the_recorded_rate() -> None:
    files = {
        "a.py": "def test_one(): ...\nasync def test_two(): ...\n",
        "b.py": "def test_one(): ...\n",
        "c.py": "def test_one(): ...\ndef test_two(): ...\ndef test_three(): ...\n",
        "d.py": "def helper(): ...\n",
    }
    # 12 s over a.py's and b.py's three tests: 4 s a test for the files with no record.
    weights = _shards.weigh(files, {"a.py": 10.0, "b.py": 2.0})
    assert weights == {"a.py": 10.0, "b.py": 2.0, "c.py": 12.0, "d.py": 4.0}


def test_with_nothing_recorded_a_file_weighs_its_test_count() -> None:
    files = {"a.py": "def test_one(): ...\ndef test_two(): ...\n", "b.py": "x = 1\n"}
    assert _shards.weigh(files, {}) == {"a.py": 2.0, "b.py": 1.0}


def _tree(root: Path) -> list[Path]:
    names = [
        "test_a.py",
        "test_b.py",
        "sub/test_c.py",
        "sub/c_test.py",
        "helper.py",
        "__pycache__/test_old.py",
        ".hidden/test_h.py",
    ]
    for name in names:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text("def test_one(): ...\n")
    return [root / n for n in names]


def test_the_walk_finds_the_test_modules_pytest_collects(tmp_path: Path) -> None:
    _tree(tmp_path)
    found = {p.relative_to(tmp_path).as_posix() for p in _shards.test_files([tmp_path])}
    assert found == {"test_a.py", "test_b.py", "sub/test_c.py", "sub/c_test.py"}


@pytest.mark.parametrize("n", [1, 2, 3, 4])
def test_the_parts_collect_every_test_file_exactly_once(tmp_path: Path, n: int) -> None:
    files = [p.resolve() for p in _tree(tmp_path) if p.resolve() in _shards.test_files([tmp_path])]
    assert len(files) == 4
    kept: list[Path] = []
    for k in range(1, n + 1):
        split = _shards.Split(k, n, [tmp_path])
        kept += [f for f in files if not split.ignores(f)]
    assert sorted(kept) == sorted(files)


def test_a_test_file_the_walk_missed_runs_in_part_one(tmp_path: Path) -> None:
    _tree(tmp_path / "tree")
    stray = tmp_path / "elsewhere" / "test_stray.py"
    stray.parent.mkdir()
    stray.write_text("def test_one(): ...\n")
    assert [_shards.Split(k, 3, [tmp_path / "tree"]).ignores(stray) for k in (1, 2, 3)] == [
        False,
        True,
        True,
    ]


def test_a_module_that_is_not_a_test_file_is_never_ignored(tmp_path: Path) -> None:
    _tree(tmp_path)
    split = _shards.Split(2, 2, [tmp_path])
    assert not split.ignores(tmp_path / "helper.py")
    assert not split.ignores(tmp_path / "sub")


def test_a_bad_shard_spec_is_a_usage_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOOM_TEST_SHARD", "5/4")
    with pytest.raises(pytest.UsageError, match="LOOM_TEST_SHARD"):
        _shards.ignored(Path("tests/unit/test_x.py"), None)  # type: ignore[arg-type]


def test_without_a_shard_nothing_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOOM_TEST_SHARD", raising=False)
    assert _shards.ignored(Path("tests/unit/test_x.py"), None) is None  # type: ignore[arg-type]


def test_the_weights_are_seconds_per_file_from_junit_reports(tmp_path: Path) -> None:
    report = tmp_path / "unit.xml"
    report.write_text(
        '<testsuites><testsuite name="pytest">'
        '<testcase classname="tests.unit.test_test_shards" name="a" time="1.5"/>'
        '<testcase classname="tests.unit.test_test_shards" name="b" time="0.25"/>'
        '<testcase classname="tests.unit.test_test_shards.TestK" name="c" time="2"/>'
        '<testcase classname="tests.unit.no_such_file" name="d" time="9"/>'
        "</testsuite></testsuites>"
    )
    assert _shards.record([report]) == {"tests/unit/test_test_shards.py": 3.75}


def _collect(*args: str, shard: str | None, deselect_free: bool = False) -> list[str]:
    env = {k: v for k, v in os.environ.items() if k != "LOOM_TEST_SHARD"}
    if shard:
        env["LOOM_TEST_SHARD"] = shard
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--co",
            "-q",
            "-p",
            "no:randomly",
            "-p",
            "no:cacheprovider",
            *args,
        ],
        cwd=_shards.ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    # 5: the part holds no test of these paths.
    assert done.returncode in (0, 5), done.stdout[-2000:] + done.stderr[-2000:]
    assert "deselected" not in done.stdout or not deselect_free, done.stdout[-500:]
    return [line for line in done.stdout.splitlines() if "::" in line]


def test_files_and_node_ids_named_on_the_command_line_run_in_one_part_each() -> None:
    """pytest asks `pytest_ignore_collect` only about paths it walks to, so the
    conftest also deselects what an argument brought in from another part."""
    args = (
        "tests/unit/test_request_ids.py",
        "tests/unit/test_lemmatize.py",
        "tests/unit/test_test_shards.py::test_a_shard_spec_reads_as_index_and_count",
    )
    whole = _collect(*args, shard=None)
    parts = [_collect(*args, shard=f"{k}/2") for k in (1, 2)]
    assert sorted(parts[0] + parts[1]) == sorted(whole)
    assert parts[0] and parts[1]


def test_a_directory_argument_skips_the_other_parts_files_before_collecting_them() -> None:
    """Under a directory, `pytest_ignore_collect` keeps another part's files from being
    imported at all: nothing is collected only to be deselected."""
    whole = _collect("tests/analyzer", shard=None)
    parts = [_collect("tests/analyzer", shard=f"{k}/2", deselect_free=True) for k in (1, 2)]
    assert sorted(parts[0] + parts[1]) == sorted(whole)


def test_the_committed_weights_are_seconds_per_test_file() -> None:
    weights = json.loads(_shards.WEIGHTS.read_text())
    assert weights, "regenerate with python -m tests._shards <junit xml>..."
    assert all(name.startswith("tests/") and name.endswith(".py") for name in weights)
    assert all(isinstance(s, int | float) and s >= 0 for s in weights.values())
