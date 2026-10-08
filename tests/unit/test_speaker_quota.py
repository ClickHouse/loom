"""Per-speaker retrieval quota: the `speaker:<name>` tag reaches
the hit as `speaker`, and the context block can guarantee each voice its
best rows before the remainder fills by rank. Default off; pure."""

from __future__ import annotations

from types import SimpleNamespace

from server.memory.api.models_search import MemorySearchInput, _MemorySearchHit
from server.memory.app.retrieval.search import speaker_from_tags
from server.memory.domain.context_block import apply_speaker_quota, assemble_context_block
from server.memory.domain.search_types import SearchHit


def _hit(key: str, score: float, speaker: str = "") -> SearchHit:
    return SearchHit(
        memory_key=key,
        memory_type="semantic",
        content_excerpt=f"fact {key}",
        distance=1.0 - score,
        rank_score=score,
        speaker=speaker,
    )


def test_speaker_from_tags() -> None:
    assert speaker_from_tags(["bench", "speaker:Caroline", "x"]) == "Caroline"
    assert speaker_from_tags(["speaker:"]) == ""
    assert speaker_from_tags(None) == "" and speaker_from_tags([]) == ""
    assert speaker_from_tags(["speaker:A", "speaker:B"]) == "A"  # first wins


def test_apply_speaker_quota_balances_two_voices_and_keeps_rank_within() -> None:
    ranked = [
        _hit("c1", 0.9, "Caroline"),
        _hit("c2", 0.8, "Caroline"),
        _hit("c3", 0.7, "Caroline"),
        _hit("n1", 0.6, "Melanie"),
        _hit("u1", 0.5),
        _hit("n2", 0.4, "Melanie"),
    ]
    out = apply_speaker_quota(ranked, 2)
    assert [h.memory_key for h in out] == ["c1", "c2", "n1", "n2", "c3", "u1"]
    # Quota larger than any voice: every attributed row leads in rank order,
    # unattributed rows follow — the quota head is "the speakers first".
    assert [h.memory_key for h in apply_speaker_quota(ranked, 10)] == [
        "c1",
        "c2",
        "c3",
        "n1",
        "n2",
        "u1",
    ]


def test_apply_speaker_quota_noop_cases() -> None:
    one = [_hit("a", 0.9, "X"), _hit("b", 0.8, "X"), _hit("c", 0.7)]
    assert apply_speaker_quota(one, 1) == one  # one voice: nothing to balance
    assert apply_speaker_quota(one, 0) == one
    assert apply_speaker_quota([], 3) == []
    unattributed = [_hit("a", 0.9), _hit("b", 0.8)]
    assert apply_speaker_quota(unattributed, 2) == unattributed


def test_assemble_context_block_applies_quota_inside_top_k() -> None:
    pool = [
        _hit("c1", 0.9, "Caroline"),
        _hit("c2", 0.8, "Caroline"),
        _hit("c3", 0.7, "Caroline"),
        _hit("c4", 0.6, "Caroline"),
        _hit("n1", 0.5, "Melanie"),
        _hit("n2", 0.4, "Melanie"),
    ]
    plain = assemble_context_block(pool, top_k=4)
    assert [h.memory_key for h in plain] == ["c1", "c2", "c3", "c4"]
    quota = assemble_context_block(pool, top_k=4, per_speaker_quota=2)
    assert [h.memory_key for h in quota] == ["c1", "c2", "n1", "n2"]


def test_wire_models_default_off() -> None:
    assert MemorySearchInput(org="o", namespace="n", agent="*", query="q").per_speaker_quota is None
    h = _MemorySearchHit(
        memory_key="k", memory_type="semantic", content_excerpt="x", distance=0.1, similarity=0.9
    )
    assert h.speaker == ""
    assert SimpleNamespace(**{"speaker": "Z"}).speaker == "Z"
