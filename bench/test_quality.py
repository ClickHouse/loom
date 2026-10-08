"""CH-free unit tests for bench/quality.py's pure core (summary + instrument).

Lives under bench/ like test_temporal_assertions.py: `pytest bench/test_quality.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench.quality import QuestionResult, gate, run_metadata, summarise


def _r(rank: int, hits: int, returned: int = 5) -> QuestionResult:
    return QuestionResult(
        conversation_id="c",
        question="q",
        gold_keys=["g"],
        top_k_keys=[f"k{i}" for i in range(returned)],
        top_k_excerpts=[""] * returned,
        recalled=rank > 0,
        first_gold_rank=rank,
        gold_hits=hits,
    )


def test_summary_separates_recall_from_rank_and_noise() -> None:
    s = summarise([_r(1, 1), _r(5, 1), _r(0, 0), _r(2, 2)])
    assert s.total == 4 and s.recalled == 3
    assert abs(s.recall - 0.75) < 1e-9
    assert abs(s.mrr - (1 + 0.2 + 0 + 0.5) / 4) < 1e-9
    assert abs(s.precision_at_k - (0.2 + 0.2 + 0.0 + 0.4) / 4) < 1e-9
    assert abs(s.rank1 - 0.25) < 1e-9


def test_dumping_the_store_shows_up_as_noise_not_recall() -> None:
    """Every gold in the top k but buried: recall 1.0, MRR and precision low."""
    s = summarise([_r(10, 1, returned=10) for _ in range(3)])
    assert s.recall == 1.0 and abs(s.mrr - 0.1) < 1e-9 and abs(s.precision_at_k - 0.1) < 1e-9


def test_empty_run_is_all_zero() -> None:
    assert summarise([]).total == 0 and summarise([]).mrr == 0.0


def test_run_metadata_reads_the_instrument_from_health() -> None:
    m = run_metadata(
        {
            "embedding_model": "text-embedding-3-large",
            "embedding_dim_probed": 3072,
            "llm_model": "gpt-4o-mini",
            "llm_provider": "openai",
            "build_commit": "abc123",
        }
    )
    assert m == {
        "embedding_model": "text-embedding-3-large",
        "embedding_dim": 3072,
        "llm_model": "gpt-4o-mini",
        "llm_provider": "openai",
        "build_commit": "abc123",
    }
    assert run_metadata({})["embedding_model"] == ""


def test_gate_still_pure() -> None:
    assert gate(3, 4, 0.5)[0] == 0
    assert gate(1, 4, 0.5)[0] == 1


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
