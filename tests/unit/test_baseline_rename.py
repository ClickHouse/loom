"""scripts/gates/baseline_rename.py carries ratchet-baseline entries to a
moved file's new path, unchanged, in all three baselines, and refuses (writing
nothing) whenever a move would merge or lose a count."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.gates import baseline_rename as br
from scripts.gates.ratchet import GateError

PER_FILE = {"server/a.py": 2, "server/old/b.py": 1, "server/keep.py": 3}
PER_FUNC = {"server/a.py::f": 250, "server/old/b.py::C.g": 210, "server/keep.py::h": 300}
MYPY = {"server/a.py": 1}


def _repo(tmp_path: Path) -> Path:
    for rel, data in zip(br.BASELINES, (PER_FILE, PER_FUNC, MYPY), strict=True):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(json.dumps(data))
    return tmp_path


def _read(root: Path) -> list[dict[str, int]]:
    return [json.loads((root / rel).read_text()) for rel in br.BASELINES]


def test_a_file_move_carries_every_entry_of_that_file(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    assert br.main(["--root", str(root), "--rename", "server/a.py", "server/pkg/a.py"]) == 0
    per_file, per_func, mypy = _read(root)
    assert per_file == {"server/keep.py": 3, "server/old/b.py": 1, "server/pkg/a.py": 2}
    assert per_func["server/pkg/a.py::f"] == 250 and "server/a.py::f" not in per_func
    assert mypy == {"server/pkg/a.py": 1}
    assert list(per_file) == sorted(per_file)  # written in --update's sorted form


def test_a_directory_move_carries_every_path_under_it(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    assert br.main(["--root", str(root), "--rename", "server/old/", "server/new/"]) == 0
    per_file, per_func, _ = _read(root)
    assert per_file["server/new/b.py"] == 1 and per_func["server/new/b.py::C.g"] == 210
    assert not any(k.startswith("server/old/") for k in per_file | per_func)


def test_a_csv_map_with_a_header_and_a_file_without_debt(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    moves = tmp_path / "moves.csv"
    moves.write_text(
        "old_path,new_path\nserver/a.py,server/x/a.py\n\nserver/clean.py,server/x/clean.py\n"
    )
    assert br.main(["--root", str(root), "--rename-map", str(moves)]) == 0
    assert _read(root)[0]["server/x/a.py"] == 2


def test_a_move_onto_an_existing_entry_is_refused_and_nothing_is_written(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path)
    before = _read(root)
    # the per-file baseline would merge a.py's 2 into keep.py's 3
    assert br.main(["--root", str(root), "--rename", "server/a.py", "server/keep.py"]) == 2
    assert _read(root) == before
    assert "would both become 'server/keep.py'" in capsys.readouterr().err


def test_dry_run_writes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _repo(tmp_path)
    before = _read(root)
    assert (
        br.main(["--root", str(root), "--dry-run", "--rename", "server/a.py", "server/z.py"]) == 0
    )
    assert _read(root) == before
    assert "server/a.py::f -> server/z.py::f (250)" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("pairs", "match"),
    [
        ([("a.py", "a.py")], "moves to itself"),
        ([("a.py", "b.py"), ("a.py", "c.py")], "moves to both"),
        ([("old/", "new.py")], "directory"),
        ([("", "b.py")], "empty path"),
    ],
)
def test_contradictory_maps_are_refused(pairs: list[tuple[str, str]], match: str) -> None:
    with pytest.raises(GateError, match=match):
        br.parse_map(pairs)


def test_a_repeated_identical_move_is_one_move() -> None:
    assert br.parse_map([("a.py", "b.py"), ("a.py", "b.py")]) == {"a.py": "b.py"}


def test_a_malformed_map_row_is_refused(tmp_path: Path) -> None:
    moves = tmp_path / "m.csv"
    moves.write_text("server/a.py\n")
    with pytest.raises(GateError, match="expected old_path,new_path"):
        br.read_map(moves)


def test_the_real_baselines_re_key_and_come_back(tmp_path: Path) -> None:
    # a round trip over the committed files: every entry comes back unchanged
    for rel in br.BASELINES:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text((br.ROOT / rel).read_text())
    before = _read(tmp_path)
    assert br.main(["--root", str(tmp_path), "--rename", "server/", "srv/"]) == 0
    assert all(not k.startswith("server/") for b in _read(tmp_path) for k in b)
    assert br.main(["--root", str(tmp_path), "--rename", "srv/", "server/"]) == 0
    assert _read(tmp_path) == before
