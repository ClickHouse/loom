"""bench/pi_llm.py — the update dates order the updates the way the bench
scores them, at every depth it runs."""

from __future__ import annotations

from bench import pi_llm


def test_update_dates_strictly_increase_past_a_month() -> None:
    dates = [pi_llm.update_text("Quorvex0", k, pi_llm._val(0, k))[0] for k in range(30)]
    assert dates == sorted(dates) and len(set(dates)) == 30
    assert dates[0] == "2026-01-02" and dates[-1] == "2026-01-31"
    day, text = pi_llm.update_text("Quorvex0", 40, "azimuthal-e0")
    assert day == "2026-02-11" and "as of 2026-02-11" in text and "azimuthal-e0" in text


def test_row_scores_only_the_episodes_that_answered() -> None:
    res = [
        {"has_latest": True, "has_stale": False, "clean_correct": True, "abstain": False},
        {"has_latest": True, "has_stale": True, "clean_correct": False, "abstain": False},
        {"error": {"_err": 500}},
    ]
    row = pi_llm._row(5, res)
    assert row["n"] == 2 and row["errors"] == 1
    assert row["accuracy"] == 100.0 and row["stale_rate"] == 50.0 and row["clean_correct"] == 50.0
