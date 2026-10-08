"""bench/locomo_qa.py — the deployment columns (latency
percentiles, stored footprint per conversation, accuracy mean ± sd over
repeated runs) are computed from the harness's own rows, not re-derived by
hand from the CSV."""

from __future__ import annotations

from typing import Any

from bench.locomo_qa import (
    QuestionResult,
    footprint_from_health,
    percentile_ms,
    run_accuracy_spread,
)


def _row(category: int, repeat_labels: str = "", **kw: Any) -> QuestionResult:
    base: dict[str, Any] = dict(
        sample_id="s",
        namespace="ns",
        question_index=0,
        category=category,
        category_name=str(category),
        question="q",
        gold="g",
        generated="a",
        label=(repeat_labels.split("|")[0] if repeat_labels else "CORRECT"),
        judge_reply="",
        abstained=False,
        hits=1,
        search_ms=100,
        embedding_model="",
        search_mode="semantic",
        reader="external",
        reader_model="m",
        judge_model="j",
        top_keys="",
        repeat_labels=repeat_labels,
    )
    base.update(kw)
    return QuestionResult(**base)


def test_percentile_is_nearest_rank_and_none_on_empty() -> None:
    assert percentile_ms([], 50) is None
    sample = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
    assert percentile_ms(sample, 50) == 50
    assert percentile_ms(sample, 95) == 100
    assert percentile_ms([7], 95) == 7


def test_run_accuracy_spread_reads_run_i_from_repeat_labels() -> None:
    rows = [
        _row(1, "CORRECT|WRONG|CORRECT"),
        _row(2, "CORRECT|CORRECT|CORRECT"),
        _row(3, "WRONG|WRONG|CORRECT"),
        _row(5, "CORRECT|CORRECT|CORRECT"),  # adversarial: outside the comparable set
    ]
    spread = run_accuracy_spread(rows, {1, 2, 3, 4})
    assert spread["runs"] == 3
    assert spread["accuracies"] == [66.67, 33.33, 100.0]
    assert spread["mean"] == 66.67
    assert spread["sd"] is not None and 33.0 < spread["sd"] < 34.0
    # An ERROR run drops out of that run's denominator; it never counts as wrong.
    err = run_accuracy_spread([_row(1, "CORRECT|ERROR"), _row(1, "WRONG|WRONG")], {1})
    assert err["accuracies"] == [50.0, 0.0]


def test_run_accuracy_spread_is_empty_without_repeats() -> None:
    spread = run_accuracy_spread([_row(1), _row(2)], {1, 2, 3, 4})
    assert spread == {"runs": 0, "accuracies": [], "mean": None, "sd": None}


def test_footprint_reads_live_rows_and_the_servers_token_estimate() -> None:
    resp = {"live": 148, "footprint": {"value_chars": 22_000, "value_tokens_est": 5_500}}
    assert footprint_from_health(resp) == {"memories": 148, "tokens_est": 5500}


def test_footprint_of_an_older_server_has_no_token_estimate() -> None:
    # A health report without the footprint section (pre-footprint server)
    # keeps the row count and leaves tokens None, never 0.
    assert footprint_from_health({"live": 3}) == {"memories": 3, "tokens_est": None}
    assert footprint_from_health({}) == {"memories": 0, "tokens_est": None}
