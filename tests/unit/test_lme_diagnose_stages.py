"""bench/lme_diagnose.py — the failure-stage labelling (pure parts plus the
evidence read with ClickHouse faked)."""

from __future__ import annotations

from typing import Any

from bench.lme_diagnose import gold_in, linked_evidence_texts, summarize_stages


def test_gold_in_uses_the_main_benchs_normalisation() -> None:
    assert gold_in("Roscioli", ["the deli near the Vatican was Roscioli!"])
    assert gold_in("blue Snaggletooth", ["bought a Blue-Snaggletooth figure"])
    assert not gold_in("Roscioli", ["a deli near the Vatican"])
    assert not gold_in("", ["anything"])


def test_summarize_stages_is_sorted_by_count_with_shares() -> None:
    rows = summarize_stages(
        ["retrieval_ranking", "compression_loss", "retrieval_ranking", "reader"]
    )
    assert rows[0] == ("retrieval_ranking", 2, 50.0)
    assert {r[0] for r in rows[1:]} == {"compression_loss", "reader"}
    assert summarize_stages([]) == []


class _FakeCH:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.params: dict[str, Any] | None = None

    async def query_all(self, sql: str, params: dict[str, Any] | None = None):
        self.params = params
        return self.rows


class _Boom:
    async def query_all(self, *a: Any, **k: Any):
        raise RuntimeError("no clickhouse")


async def test_linked_evidence_texts_reads_the_turns_behind_the_keys() -> None:
    ch = _FakeCH([{"content": "we ate at Roscioli"}, {"content": "then espresso"}])
    texts = await linked_evidence_texts("dev", "lme-x", ["k1", "k2"], ch=ch)
    assert texts == ["we ate at Roscioli", "then espresso"]
    assert ch.params["keys"] == ["k1", "k2"] and ch.params["ns"] == "lme-x"


async def test_linked_evidence_texts_distinguishes_unreachable_from_empty() -> None:
    assert await linked_evidence_texts("dev", "ns", ["k1"], ch=_Boom()) is None
    assert await linked_evidence_texts("dev", "ns", [], ch=_Boom()) == []
