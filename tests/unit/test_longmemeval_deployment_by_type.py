"""Per-type deployment columns of the LongMemEval bench: latency IQR and
reader-context tokens beside accuracy."""

from __future__ import annotations

import csv
from pathlib import Path

from bench.longmemeval import ItemResult, _pq, _write_csv, deployment_by_type


def _row(
    qt: str, *, judge: str, search: int, ask: int, ctx: int, tin: int = 100, tout: int = 20
) -> ItemResult:
    return ItemResult(
        question_id=f"{qt}-{ask}",
        question_type=qt,
        question="q",
        gold_answer="a",
        top_k_excerpts=[],
        recalled=True,
        answer_in_context=True,
        judge_label=judge,
        judge_rationale="",
        hypothesis="h",
        search_latency_ms=search,
        ask_latency_ms=ask,
        context_tokens_est=ctx,
        qa_tokens_in=tin,
        qa_tokens_out=tout,
    )


def test_percentile_nearest_rank() -> None:
    assert _pq([], 0.5) == 0
    assert _pq([5], 0.25) == 5
    assert (_pq([1, 2, 3, 4, 5], 0.25), _pq([1, 2, 3, 4, 5], 0.5), _pq([1, 2, 3, 4, 5], 0.75)) == (
        2,
        3,
        4,
    )


def test_rows_per_type_with_iqr_context_and_judge() -> None:
    rows = deployment_by_type(
        [
            _row("preference", judge="CORRECT", search=100, ask=1000, ctx=1500),
            _row("preference", judge="WRONG", search=300, ask=3000, ctx=2500),
            _row(
                "preference", judge="CORRECT", search=200, ask=2000, ctx=0
            ),  # no ctx: excluded from mean
            _row("temporal-reasoning", judge="", search=50, ask=0, ctx=0),  # search-only answerer
        ]
    )
    assert [r["question_type"] for r in rows] == ["preference", "temporal-reasoning"]
    p = rows[0]
    assert p["n"] == 3 and p["judge"] == "2/3 (66.7%)"
    assert (p["ask_p25_ms"], p["ask_p50_ms"], p["ask_p75_ms"]) == (1000, 2000, 3000)  # nearest rank
    assert (
        p["search_p50_ms"] == 200
        and p["ctx_tokens_mean"] == 2000
        and p["qa_tokens_per_item"] == 120
    )
    t = rows[1]
    assert t["judge"] == "—" and (t["ask_p25_ms"], t["ask_p50_ms"], t["ask_p75_ms"]) == (0, 0, 0)
    assert t["ctx_tokens_mean"] == 0


def test_csv_carries_context_tokens(tmp_path: Path) -> None:
    out = tmp_path / "r.csv"
    _write_csv(out, [_row("preference", judge="CORRECT", search=1, ask=2, ctx=1234)])
    rows = list(csv.DictReader(out.open()))
    assert rows[0]["context_tokens_est"] == "1234" and rows[0]["ask_latency_ms"] == "2"
