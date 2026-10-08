"""The exploration readout's arithmetic."""

from __future__ import annotations

import pytest

from bench.procedure_explore_readout import SQL, readout, wilson


def test_wilson_interval_shape() -> None:
    assert wilson(0, 0) == (0.0, 0.0, 0.0)
    p, lo, hi = wilson(9, 10)
    assert p == pytest.approx(0.9) and 0.55 < lo < 0.9 < hi <= 1.0
    p, lo, hi = wilson(50, 100)
    assert lo == pytest.approx(0.404, abs=0.005) and hi == pytest.approx(0.596, abs=0.005)


def test_readout_reports_the_gap_and_separation() -> None:
    rows = [
        {
            "namespace": "ns",
            "key": "p_good",
            "shown_n": 40,
            "shown_ok": 36,
            "withheld_n": 30,
            "withheld_ok": 12,
        },
        {
            "namespace": "ns",
            "key": "p_thin",
            "shown_n": 3,
            "shown_ok": 3,
            "withheld_n": 0,
            "withheld_ok": 0,
        },
    ]
    out = readout(rows)
    good, thin = out
    assert good["gap"] == pytest.approx(0.9 - 0.4) and good["separated"] is True
    assert thin["gap"] is None and thin["separated"] is False
    assert thin["withheld_ci"] == [0.0, 0.0]


def test_retention_rows_compute_success_and_srr() -> None:
    from bench.procedure_explore_readout import retention_rows

    rows = [
        {"namespace": "ns", "memory_key": "p", "runs": 9, "ok": 6, "pairs": 6, "ok_after_ok": 5}
    ]
    (t,) = retention_rows(rows)
    assert t["runs"] == 10 and t["success"] == pytest.approx(0.667, abs=1e-3)
    assert t["retention"] == pytest.approx(5 / 6, abs=1e-3)
    (u,) = retention_rows(
        [{"namespace": "ns", "memory_key": "q", "runs": 1, "ok": 0, "pairs": 0, "ok_after_ok": 0}]
    )
    assert u["retention"] is None


def test_outcomes_are_read_from_the_table_the_distiller_writes() -> None:
    from server.memory.store.session_outcomes import OUTCOMES_TABLE

    assert f"FROM {OUTCOMES_TABLE}\n" in SQL
