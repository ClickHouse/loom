"""Offline lexical re-score of a hypotheses file (bench/lme_rescore.py) reuses
bench/answer_scoring.py and reports both the simplified and the raw pair."""

from __future__ import annotations

import pytest

from bench.lme_rescore import score_row, summarize


def _item(qid: str = "q1", qtype: str = "temporal-reasoning", gold: str = "May 7, 2023"):
    return {"question_id": qid, "question_type": qtype, "answer": gold}


def test_score_row_carries_raw_and_simplified_pairs():
    r = score_row(
        _item(),
        "Caroline went to the LGBTQ support group on May 7, 2023.",
        "May 7, 2023",
        "model:gpt-4o-mini",
    )
    # the sentence is punished for its length, the span is not
    assert 0.0 < r["f1_raw"] < 0.6 and 0.0 < r["bleu1_raw"] < 0.5
    assert r["f1"] == 1.0 and r["bleu1"] == 1.0
    assert r["simplified_by"] == "model:gpt-4o-mini"


def test_summary_is_percent_overall_and_per_type():
    rows = [
        {"question_type": "ku", "f1": 1.0, "bleu1": 1.0, "f1_raw": 0.1, "bleu1_raw": 0.1},
        {"question_type": "ku", "f1": 0.0, "bleu1": 0.0, "f1_raw": 0.0, "bleu1_raw": 0.0},
        {"question_type": "ms", "f1": 0.5, "bleu1": 0.25, "f1_raw": 0.05, "bleu1_raw": 0.05},
    ]
    s = summarize(rows)
    assert s["n"] == 3 and s["f1"] == 50.0 and s["bleu1"] == pytest.approx(41.67)
    assert s["f1_raw"] == 5.0
    assert s["by_type"]["ku"] == {"n": 2, "f1": 50.0, "bleu1": 50.0}
