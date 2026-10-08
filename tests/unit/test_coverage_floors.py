"""The branch-coverage floor check fails a critical module under its floor,
counts a vanished module as failing, and reads the committed floors."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.inventory import coverage_floors as cf


def _report(files: dict[str, tuple[int, int]]) -> dict:
    return {
        "meta": {"branch_coverage": True},
        "files": {
            path: {"summary": {"covered_branches": c, "num_branches": n}}
            for path, (c, n) in files.items()
        },
    }


FLOORS = [cf.Floor("server/auth/", 80.0, "r"), cf.Floor("server/core/ch.py", 90.0, "r")]


def test_a_module_under_its_floor_fails_and_one_above_passes() -> None:
    report = _report(
        {
            "server/auth/jwt.py": (7, 10),
            "server/auth/grants.py": (1, 10),
            "server/core/ch.py": (95, 100),
        }
    )
    auth, ch = cf.measure(report, FLOORS)
    assert (auth.covered, auth.total, auth.below) == (8, 20, True)
    assert not ch.below and ch.can_raise


def test_a_floor_that_matches_no_file_fails() -> None:
    [auth, _] = cf.measure(_report({"server/other.py": (1, 1)}), FLOORS)
    assert auth.below


def test_a_report_without_branch_data_is_refused() -> None:
    with pytest.raises(ValueError, match="--cov-branch"):
        cf.measure({"meta": {"branch_coverage": False}, "files": {}}, FLOORS)


def test_the_cli_exits_one_below_a_floor(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = tmp_path / "coverage.json"
    report.write_text(json.dumps(_report({"server/auth/jwt.py": (0, 10)})))
    assert cf.main([str(report)]) == 1
    assert "BELOW" in capsys.readouterr().out


def test_every_committed_floor_has_a_reason_and_a_real_module() -> None:
    root = Path(__file__).resolve().parents[2]
    floors = cf.load_floors()
    assert floors
    for floor in floors:
        assert 0 < floor.branch <= 100 and floor.reason
        assert list(root.glob(floor.prefix + "*")), f"{floor.prefix} names no file"
