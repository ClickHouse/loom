"""Digest cap: at most N summary rows in the final block, the
rest of the slots go back to facts; 0 leaves the summaries uncapped; the knob
rides memory.search and memory.ask per request and the deployment setting."""

from __future__ import annotations

from types import SimpleNamespace

from server.memory.api.models_ask import MemoryAskInput
from server.memory.api.models_search import MemorySearchInput
from server.memory.domain.context_block import assemble_context_block


def _hit(key: str, source: str, score: float, text: str = "x") -> SimpleNamespace:
    return SimpleNamespace(
        namespace="n",
        memory_key=key,
        source=source,
        rank_score=score,
        distance=1 - score,
        content_excerpt=f"{text} {key}",
        memory_type="semantic",
        tags=[],
        valid_at=None,
    )


def _pool() -> list[SimpleNamespace]:
    # four digests outrank the facts after band calibration; six facts follow
    digests = [_hit(f"s{i}", "summary", 0.9 - i * 0.01, "digest") for i in range(4)]
    facts = [_hit(f"f{i}", "fact", 0.5 - i * 0.01, "fact") for i in range(6)]
    return digests + facts


def _sources(rows):
    return [str(r.source) for r in rows]


def test_zero_cap_keeps_every_digest_the_band_admits():
    out = assemble_context_block(_pool(), top_k=6, summary_cap=0)
    assert _sources(out).count("summary") == 4


def test_cap_hands_digest_slots_back_to_facts():
    out = assemble_context_block(_pool(), top_k=6, summary_cap=2)
    srcs = _sources(out)
    assert srcs.count("summary") == 2 and srcs.count("fact") == 4
    # the two digests kept are the best-ranked ones
    assert {r.memory_key for r in out if r.source == "summary"} == {"s0", "s1"}
    # facts keep their rank order
    assert [r.memory_key for r in out if r.source == "fact"] == ["f0", "f1", "f2", "f3"]


def test_entity_summaries_count_as_digests():
    pool = [
        _hit("e0", "entity_summary", 0.9),
        _hit("s0", "summary", 0.89),
        _hit("f0", "fact", 0.5),
        _hit("f1", "fact", 0.49),
    ]
    out = assemble_context_block(pool, top_k=3, summary_cap=1)
    srcs = _sources(out)
    assert srcs.count("entity_summary") + srcs.count("summary") == 1
    assert srcs.count("fact") == 2


def test_cap_larger_than_digests_is_a_no_op():
    a = assemble_context_block(_pool(), top_k=6, summary_cap=10)
    b = assemble_context_block(_pool(), top_k=6, summary_cap=0)
    assert [r.memory_key for r in a] == [r.memory_key for r in b]


def test_wire_fields_default_to_deployment_setting():
    assert MemorySearchInput(org="o", namespace="n", query="q").summary_cap is None
    assert MemorySearchInput(org="o", namespace="n", query="q", summary_cap=2).summary_cap == 2
    ask = MemoryAskInput(org="o", namespace="n", query="q")
    assert ask.summary_cap is None and "summary_cap" not in ask.model_fields_set
    assert MemoryAskInput(org="o", namespace="n", query="q", summary_cap=0).summary_cap == 0
