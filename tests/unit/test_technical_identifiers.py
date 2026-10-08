"""Technical identifiers route, inject and chase."""

from __future__ import annotations

from server.memory.app.ask.identifiers import (
    _injectable_identifiers,
    _technical_identifiers,
    next_identifiers,
)
from server.memory.domain.synth_policy import CHEAP, STRONG, SynthFeatures, decide


def test_symbols_files_and_quoted_errors_are_identifiers() -> None:
    assert "partitionId" in _technical_identifiers(
        "false injectivity claims of toFixedString and partitionId"
    )
    assert "toFixedString" in _technical_identifiers(
        "false injectivity claims of toFixedString and partitionId"
    )
    assert "convertColumnToType.cpp" in _technical_identifiers(
        "changes to convertColumnToType.cpp and the erasure"
    )
    assert _technical_identifiers(
        'the logical error "Join is supported only for pipelines with one output port"'
    ) == ["Join is supported only for pipelines with one output port"]
    assert "ZooKeeperMetadataTransaction" in _injectable_identifiers(
        "the ZooKeeperMetadataTransaction moveOpsTo one-shot consume bug"
    )


def test_product_words_and_prose_are_not_identifiers() -> None:
    assert (
        _technical_identifiers(
            "What do we know about lightweight update patch parts wrong results in ClickHouse?"
        )
        == []
    )
    assert _technical_identifiers("Which platform did I gain the most followers on?") == []


class _Hit:
    def __init__(self, text: str) -> None:
        self.content_excerpt = text
        self.memory_key = "k"


def test_chase_frontier_follows_symbols_and_files() -> None:
    hits = [
        _Hit(
            "The throw sites are QueryPipelineBuilder.cpp:374 joinPipelinesYShaped and :415 joinPipelinesPaired."
        )
    ]
    nxt = next_identifiers(hits, seen=["Join is supported only for pipelines with one output port"])
    assert "QueryPipelineBuilder.cpp:374" in nxt
    assert "joinPipelinesYShaped" in nxt


def _easy(**over):
    base = dict(
        query_chars=80,
        n_memories=25,
        retrieval_confidence=0.8,
        retrieval_risk="medium",
        route_agreement=0.9,
        top_similarity=0.85,
        similarity_margin=0.2,
        query="What do we know about lightweight update patch parts wrong results?",
    )
    base.update(over)
    return SynthFeatures(**base)


def test_rich_evidence_routes_strong_and_thin_evidence_stays_cheap() -> None:
    assert (
        decide(_easy(evidence_tokens=5900), max_cheap_evidence_tokens=2500).reason
        == "rich_evidence"
    )
    assert decide(_easy(evidence_tokens=5900), max_cheap_evidence_tokens=2500).tier == STRONG
    assert decide(_easy(evidence_tokens=900), max_cheap_evidence_tokens=2500).tier == CHEAP
    # rule off
    assert decide(_easy(evidence_tokens=5900), max_cheap_evidence_tokens=0).tier == CHEAP
