"""Pending batches' turns that speak to the question reach
the synthesis window as raw rows; nothing pending, nothing matching, or a
failed read leaves the answer as it was."""

from __future__ import annotations

import json

import pytest

from server.memory.app import pending_evidence as P


def _batch(
    qid: str, sid: str, msgs: list[tuple[str, str]], enq: str = "2026-09-12 15:00:00"
) -> dict:
    return {
        "queue_id": qid,
        "session_id": sid,
        "enqueued_at": enq,
        "payload": json.dumps(
            {"session_id": sid, "messages": [{"role": r, "content": c} for r, c in msgs]}
        ),
    }


def test_rank_picks_the_turns_that_share_words_with_the_question() -> None:
    batches = [
        _batch(
            "q1",
            "ns:s9",
            [
                ("user", "Let us decide: the cache should be an LRU with a 512 MB cap."),
                ("assistant", "Noted, LRU cache with a 512 MB cap."),
                ("user", "Also lunch is at noon."),
            ],
        ),
        _batch("q2", "ns:s10", [("user", "The deploy window moved to Friday.")]),
    ]
    turns = P.rank_pending_turns("What did we decide about the cache cap?", batches, max_turns=3)
    assert [t.index for t in turns] == [0, 1]
    assert (
        turns[0].queue_id == "q1"
        and turns[0].session_id == "ns:s9"
        and turns[0].score > turns[1].score
    )
    # a turn sharing one word is not evidence
    assert P.rank_pending_turns("Is lunch at noon?", batches, min_overlap=2)[0].content.startswith(
        "Also lunch"
    )
    assert P.rank_pending_turns("cache", batches) == [] or all(
        t.score >= 1 for t in P.rank_pending_turns("cache", batches)
    )
    assert P.rank_pending_turns("", batches) == []
    # malformed payloads are skipped, not raised
    assert P.rank_pending_turns("cache cap", [{"queue_id": "x", "payload": "{not json"}]) == []


def test_hits_carry_key_source_session_and_label() -> None:
    turns = P.rank_pending_turns(
        "cache cap", [_batch("q1", "ns:s9", [("user", "the cache cap is 512 MB")])]
    )
    (h,) = P.pending_hits(turns, namespace="ns")
    assert h.memory_key == "pending:q1:0" and h.source == "pending"
    assert h.session_id == "ns:s9" and h.namespace == "ns"
    assert h.content_excerpt.startswith("[user] the cache cap")
    assert h.created_at == "2026-09-12 15:00:00"


@pytest.mark.asyncio
async def test_pending_evidence_for_is_fail_open(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _boom(**kw):
        raise RuntimeError("ch down")

    monkeypatch.setattr(P, "pending_batches", _boom)
    assert await P.pending_evidence_for("q", org="o", namespace="n", agent="a", user_id=None) == []

    async def _none(**kw):
        return []

    monkeypatch.setattr(P, "pending_batches", _none)
    assert await P.pending_evidence_for("q", org="o", namespace="n", agent="a", user_id=None) == []


def test_settings_and_output_field() -> None:
    from server.memory.api.models_ask import MemoryAskOutput
    from server.settings import Settings

    s = Settings()
    assert s.memory_ask_pending_evidence_turns == 5 and s.memory_ask_pending_evidence_batches == 20
    assert MemoryAskOutput(answer="x", pending_evidence=2).pending_evidence == 2
