"""Unit tests for dual-destination promotion on learnings.review.

Boundaries we stub:
  - learnings_repo.find_candidate + insert_row (queue state)
  - The two side-effect helpers — _do_promote (memory) + _do_promote_to_knowledge
"""

from __future__ import annotations

from typing import Any

import pytest


def _candidate(*, candidate_id="cid-1", status="pending") -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "memory_key": "policy_q1_2026",
        "memory_type": "semantic",
        "proposed_value": '"Q1 2026 incident response playbook supersedes 2025"',
        "supporters": ["clickhouse/finance/agent-a/-/-/policy_q1_2026"],
        "source_kind": "conversation_seeder",
        "status": status,
        "rationale": "conversation-seeder candidate",
    }


@pytest.mark.asyncio
async def test_promote_memory_default_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default destination='memory' fires only the memory path."""
    from server.memory.api.models_learnings import LearningsReviewInput
    from server.memory.app import learnings as h
    from server.memory.store import learnings as learnings_repo

    async def fake_find(*_a, **_kw):
        return _candidate()

    async def fake_insert(*_a, **_kw):
        return None

    memory_calls = []
    knowledge_calls = []

    async def fake_memory_promote(*a, **kw):
        memory_calls.append((a, kw))

    async def fake_knowledge_promote(*a, **kw):
        knowledge_calls.append((a, kw))
        return "learning://cid-1"

    monkeypatch.setattr(learnings_repo, "find_candidate", fake_find)
    monkeypatch.setattr(learnings_repo, "insert_row", fake_insert)
    monkeypatch.setattr(h, "_do_promote", fake_memory_promote)
    monkeypatch.setattr(h, "_do_promote_to_knowledge", fake_knowledge_promote)

    out = await h.handle_learnings_review(
        LearningsReviewInput(
            org="clickhouse",
            namespace="finance",
            candidate_id="cid-1",
            decision="promote",
            # destination omitted → default 'memory'
        )
    )
    assert out.promoted_to_shared is True
    assert out.promoted_to_knowledge is False
    assert out.knowledge_source_uri == ""
    assert len(memory_calls) == 1
    assert len(knowledge_calls) == 0


@pytest.mark.asyncio
async def test_promote_knowledge_fires_only_knowledge_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.api.models_learnings import LearningsReviewInput
    from server.memory.app import learnings as h
    from server.memory.store import learnings as learnings_repo

    async def fake_find(*_a, **_kw):
        return _candidate()

    async def fake_insert(*_a, **_kw):
        return None

    memory_calls = []
    knowledge_calls = []

    async def fake_memory_promote(*a, **kw):
        memory_calls.append((a, kw))

    async def fake_knowledge_promote(*a, **kw):
        knowledge_calls.append((a, kw))
        return "learning://cid-1"

    monkeypatch.setattr(learnings_repo, "find_candidate", fake_find)
    monkeypatch.setattr(learnings_repo, "insert_row", fake_insert)
    monkeypatch.setattr(h, "_do_promote", fake_memory_promote)
    monkeypatch.setattr(h, "_do_promote_to_knowledge", fake_knowledge_promote)

    out = await h.handle_learnings_review(
        LearningsReviewInput(
            org="clickhouse",
            namespace="finance",
            candidate_id="cid-1",
            decision="promote",
            destination="knowledge",
        )
    )
    assert out.promoted_to_shared is False
    assert out.promoted_to_knowledge is True
    assert out.knowledge_source_uri == "learning://cid-1"
    assert len(memory_calls) == 0
    assert len(knowledge_calls) == 1


@pytest.mark.asyncio
async def test_promote_both_fires_both_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.api.models_learnings import LearningsReviewInput
    from server.memory.app import learnings as h
    from server.memory.store import learnings as learnings_repo

    async def fake_find(*_a, **_kw):
        return _candidate()

    async def fake_insert(*_a, **_kw):
        return None

    memory_calls = []
    knowledge_calls = []

    async def fake_memory_promote(*a, **kw):
        memory_calls.append((a, kw))

    async def fake_knowledge_promote(*a, **kw):
        knowledge_calls.append((a, kw))
        return "learning://cid-1"

    monkeypatch.setattr(learnings_repo, "find_candidate", fake_find)
    monkeypatch.setattr(learnings_repo, "insert_row", fake_insert)
    monkeypatch.setattr(h, "_do_promote", fake_memory_promote)
    monkeypatch.setattr(h, "_do_promote_to_knowledge", fake_knowledge_promote)

    out = await h.handle_learnings_review(
        LearningsReviewInput(
            org="clickhouse",
            namespace="finance",
            candidate_id="cid-1",
            decision="promote",
            destination="both",
        )
    )
    assert out.promoted_to_shared is True
    assert out.promoted_to_knowledge is True
    assert out.knowledge_source_uri == "learning://cid-1"
    assert len(memory_calls) == 1
    assert len(knowledge_calls) == 1


@pytest.mark.asyncio
async def test_reject_with_destination_ignores_destination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """destination is only consulted on decision='promote'. Reject
    flips status regardless of what destination was passed."""
    from server.memory.api.models_learnings import LearningsReviewInput
    from server.memory.app import learnings as h
    from server.memory.store import learnings as learnings_repo

    async def fake_find(*_a, **_kw):
        return _candidate()

    async def fake_insert(*_a, **_kw):
        return None

    async def never_called(*a, **kw):
        raise AssertionError("side-effect path should not fire on reject")

    monkeypatch.setattr(learnings_repo, "find_candidate", fake_find)
    monkeypatch.setattr(learnings_repo, "insert_row", fake_insert)
    monkeypatch.setattr(h, "_do_promote", never_called)
    monkeypatch.setattr(h, "_do_promote_to_knowledge", never_called)

    out = await h.handle_learnings_review(
        LearningsReviewInput(
            org="clickhouse",
            namespace="finance",
            candidate_id="cid-1",
            decision="reject",
            destination="both",
        )
    )
    assert out.new_status == "rejected"
    assert out.promoted_to_shared is False
    assert out.promoted_to_knowledge is False


@pytest.mark.asyncio
async def test_do_promote_to_knowledge_builds_correct_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Helper test on the knowledge-side wiring: source_uri,
    title, tags, content unwrapping from JSON-encoded proposed_value."""
    from server.knowledge.app import writes as knowledge_handler
    from server.memory.app import learnings as h

    captured = []

    async def fake_upsert(inp):
        captured.append(inp)

        class _Out:
            doc_id = "doc-1"
            chunks_created = 3
            embedding_model = "fake-ngram-1024"
            embedding_dim = 1024

        return _Out()

    monkeypatch.setattr(knowledge_handler, "handle_knowledge_upsert", fake_upsert)

    candidate = _candidate(candidate_id="cid-abc")
    source_uri = await h._do_promote_to_knowledge(
        "clickhouse",
        "finance",
        candidate,
        "alice@example.com",
        "looks like a runbook",
    )
    assert source_uri == "learning://cid-abc"
    assert len(captured) == 1
    inp = captured[0]
    assert inp.org == "clickhouse"
    assert inp.namespace == "finance"
    assert inp.source_uri == "learning://cid-abc"
    assert inp.title == "policy_q1_2026"
    # proposed_value is JSON-encoded; deserialized for content.
    assert "Q1 2026 incident response playbook" in inp.content
    assert "promoted-from-learning" in inp.tags
    assert "source-kind:conversation_seeder" in inp.tags


@pytest.mark.asyncio
async def test_a_promotion_whose_write_awaits_publication_is_made(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PUBLICATION_PENDING says the document committed: the promotion answers its
    source URI, so the review records its decision, rather than failing a write the
    caller is told not to repeat."""
    from server.core.pg_lease import PublicationPending
    from server.knowledge.app import writes as knowledge_handler
    from server.memory.app import learnings as h

    async def pending_upsert(inp):
        raise PublicationPending()

    monkeypatch.setattr(knowledge_handler, "handle_knowledge_upsert", pending_upsert)
    source_uri = await h._do_promote_to_knowledge(
        "clickhouse", "finance", _candidate(candidate_id="cid-abc"), "alice@example.com", ""
    )
    assert source_uri == "learning://cid-abc"


@pytest.mark.asyncio
async def test_a_memory_promotion_whose_write_awaits_publication_is_made(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The memory half as the knowledge half: a `memory.set` answering
    PUBLICATION_PENDING is a made write."""
    from server.core.pg_lease import PublicationPending
    from server.memory.app import learnings as h

    async def pending(*a, **kw):
        raise PublicationPending()

    monkeypatch.setattr(h, "_promote_via_memory_set", pending)
    candidate = {**_candidate(candidate_id="cid-abc"), "source_kind": "conversation_seeder"}
    await h._do_promote("clickhouse", "finance", candidate, "alice@example.com", "")


@pytest.mark.asyncio
async def test_promote_idempotent_already_promoted_returns_existing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-reviewing a candidate that's already promoted is a no-op."""
    from server.memory.api.models_learnings import LearningsReviewInput
    from server.memory.app import learnings as h
    from server.memory.store import learnings as learnings_repo

    async def fake_find(*_a, **_kw):
        return _candidate(status="promoted")

    async def never(*a, **kw):
        raise AssertionError("should not fire on idempotent path")

    monkeypatch.setattr(learnings_repo, "find_candidate", fake_find)
    monkeypatch.setattr(learnings_repo, "insert_row", never)
    monkeypatch.setattr(h, "_do_promote", never)
    monkeypatch.setattr(h, "_do_promote_to_knowledge", never)

    out = await h.handle_learnings_review(
        LearningsReviewInput(
            org="clickhouse",
            namespace="finance",
            candidate_id="cid-1",
            decision="promote",
            destination="both",
        )
    )
    assert out.new_status == "promoted"
    assert out.promoted_to_shared is False
    assert out.promoted_to_knowledge is False


@pytest.mark.asyncio
async def test_a_review_carries_the_candidates_support_and_proposal_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """learnings_queue is ReplacingMergeTree(version): the review's status row is what
    FINAL reads afterwards, so it carries supporter_count, confidence_avg and
    proposed_at instead of zeroing them (learnings.list and memory.lineage read them)."""
    from datetime import datetime

    from server.memory.api.models_learnings import LearningsReviewInput
    from server.memory.app import learnings as h
    from server.memory.store import learnings as learnings_repo

    proposed = datetime(2026, 1, 2, 3, 4, 5)

    async def fake_find(*_a, **_kw):
        return {
            **_candidate(),
            "supporter_count": 3,
            "confidence_avg": 0.75,
            "proposed_at": proposed,
        }

    written: list[dict] = []

    async def fake_insert(row, *_a, **_kw):
        written.append(row)

    monkeypatch.setattr(learnings_repo, "find_candidate", fake_find)
    monkeypatch.setattr(learnings_repo, "insert_row", fake_insert)
    await h.handle_learnings_review(
        LearningsReviewInput(
            org="clickhouse", namespace="finance", candidate_id="cid-1", decision="reject"
        )
    )
    [row] = written
    assert row["supporter_count"] == 3 and row["confidence_avg"] == 0.75
    assert row["proposed_at"] == proposed and row["status"] == "rejected"
