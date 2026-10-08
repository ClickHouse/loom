"""The lesson attribution bench: Wilson bounds, lift
against the rest of the graded runs, and an INSUFFICIENT verdict that names
what is missing."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "lesson_grade_attribution", Path("bench/lesson_grade_attribution.py")
)
assert _spec and _spec.loader
bench = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bench)


def test_wilson_bounds() -> None:
    assert bench.wilson(0, 0) == (0.0, 1.0)
    lo, hi = bench.wilson(8, 10)
    assert 0.44 < lo < 0.5 and 0.94 < hi < 0.97
    assert bench.wilson(10, 10)[1] == 1.0


def test_lift_is_against_the_other_graded_runs() -> None:
    cov = {"closed": 20, "with_used": 12, "pass_runs": 10, "fail_runs": 10, "graded_with_used": 12}
    out = bench.summarise(
        cov,
        {"runs": 20, "passes": 10},
        [{"key": "lesson_a", "runs": 8, "passes": 7}],
        {"lesson_a": "proposed"},
        min_graded=10,
    )
    (row,) = out["per_memory"]
    assert row["pass_rate"] == 0.875 and row["rest_pass_rate"] == 0.25 and row["lift"] == 0.625
    assert row["status"] == "proposed" and out["verdict"] == "OK" and out["missing"] == []


def test_no_grades_is_insufficient_and_says_why() -> None:
    cov = {"closed": 12232, "with_used": 899, "pass_runs": 0, "fail_runs": 0, "graded_with_used": 0}
    out = bench.summarise(cov, {"runs": 0, "passes": 0}, [], {}, min_graded=30)
    assert out["verdict"] == "INSUFFICIENT" and out["baseline"]["pass_rate"] is None
    assert any("run.grade" in m for m in out["missing"])
