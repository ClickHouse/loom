"""memory.ask names the sessions its cited memories came from."""

from __future__ import annotations

from server.memory.api.models_ask import MemoryAskOutput
from server.memory.api.models_search import _MemorySearchHit


def _hit(key: str, sid: str) -> _MemorySearchHit:
    return _MemorySearchHit(
        memory_key=key,
        memory_type="semantic",
        content_excerpt="x",
        distance=0.2,
        similarity=0.8,
        session_id=sid,
    )


def test_cited_sessions_follow_the_cited_keys_in_order_without_repeats() -> None:
    out = MemoryAskOutput(
        answer="a",
        memory_keys=["k3", "k1"],
        results=[
            _hit("k1", "s1"),
            _hit("k2", "s2"),
            _hit("k3", "s3"),
            _hit("k4", "s1"),
            _hit("k5", ""),
        ],
    )
    # results order, cited keys only, "" skipped, s1 once even though k4 (uncited) shares it
    assert out.cited_sessions == ["s1", "s3"]


def test_no_citations_or_no_sessions_gives_an_empty_list() -> None:
    assert (
        MemoryAskOutput(answer="a", memory_keys=[], results=[_hit("k1", "s1")]).cited_sessions == []
    )
    assert (
        MemoryAskOutput(answer="a", memory_keys=["k1"], results=[_hit("k1", "")]).cited_sessions
        == []
    )
    # an explicit value is kept as sent
    out = MemoryAskOutput(
        answer="a", memory_keys=["k1"], results=[_hit("k1", "s1")], cited_sessions=["pinned"]
    )
    assert out.cited_sessions == ["pinned"]
