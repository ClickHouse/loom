"""bench/session_ab.py — the customer-side cost-per-session gate."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "bench"))
import session_ab


def _rows(on_costs, off_costs, agent="pr-analyst"):
    rows = []
    for i, (a, b) in enumerate(zip(on_costs, off_costs, strict=True)):
        rows.append(
            {
                "agent": agent,
                "arm": "on",
                "item": f"PR-{i}",
                "cost_usd": a,
                "duration_s": 100 * a,
                "findings": 5,
            }
        )
        rows.append(
            {
                "agent": agent,
                "arm": "off",
                "item": f"PR-{i}",
                "cost_usd": b,
                "duration_s": 100 * b,
                "findings": 6,
            }
        )
    return rows


def test_paired_deltas_are_relative_per_item() -> None:
    d = session_ab.paired_deltas(_rows([1.1, 2.2], [1.0, 2.0]), "cost_usd")
    assert [round(x, 3) for x in d] == [0.1, 0.1]


def test_report_flags_the_analyst_case(capsys) -> None:
    # +63% with the index on, findings unchanged: the gate must fail loudly.
    rows = _rows([1.63, 3.26, 0.815, 2.445], [1.0, 2.0, 0.5, 1.5])
    rc = session_ab.report(rows, agent="pr-analyst", min_pairs=4, max_cost_delta=0.10)
    out = capsys.readouterr().out
    assert rc == 1 and "MORE EXPENSIVE" in out and "GATE FAILED" in out


def test_report_neutral_within_noise(capsys) -> None:
    rows = _rows([1.05, 1.9, 0.52, 1.6], [1.0, 2.0, 0.5, 1.5])
    rc = session_ab.report(rows, min_pairs=4, max_cost_delta=0.10)
    out = capsys.readouterr().out
    assert rc == 0 and "NEUTRAL" in out


def test_report_needs_both_arms() -> None:
    assert session_ab.report([{"arm": "on", "cost_usd": 1.0}]) == 2
