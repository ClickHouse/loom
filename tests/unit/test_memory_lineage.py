"""Unit tests for memory.lineage (provenance walk)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest


def _shared_row(
    *,
    promoted_by: str = "auto",
    promoted_from: list[str] | None = None,
    promotion_event: uuid.UUID | None = None,
    promoted_at: datetime | None = None,
    rationale: str = "auto-promoted",
    tombstoned_at: datetime | None = None,
) -> dict:
    return {
        "value": '"sometext"',
        "memory_type": "semantic",
        "confidence": 0.9,
        "promoted_from": promoted_from
        or [
            "clickhouse/finance/agent-a/alice/-/policy_q1",
        ],
        "promotion_event": promotion_event or uuid.uuid4(),
        "promoted_by": promoted_by,
        "rationale": rationale,
        "promoted_at": promoted_at or datetime(2026, 5, 18, 12, 0, 0),
        "reversible_until": datetime(2026, 5, 25, 12, 0, 0),
        "tombstoned_at": tombstoned_at,
    }


def _stub_memory_tag_lookup(monkeypatch: pytest.MonkeyPatch, row: dict | None = None) -> None:
    """Stub the CH client the memory-tag fallback hits when shared is None/tombstoned."""

    class _FakeCh:
        async def query_one(self, *_a, **_kw):
            return row

    import server.core.ch as _ch

    monkeypatch.setattr(_ch, "get_client", lambda: _FakeCh())


def _audit_row(*, ts: datetime, event_type: str, request_id: str):
    from server.insights.store.audit import AuditRow

    return AuditRow(
        ts=ts,
        event_type=event_type,
        org="clickhouse",
        namespace="finance",
        actor="auto",
        subject="{}",
        request_id=request_id,
    )


@pytest.mark.asyncio
async def test_auto_promoted_no_candidate_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """auto-promote bypasses the queue; mechanism='auto'; candidate=None."""
    from server.insights.store import audit as audit_repo
    from server.memory.api.models_graph import MemoryLineageInput
    from server.memory.app.dedup import handle_memory_lineage
    from server.memory.store import shared as shared_repo

    promo_event = uuid.uuid4()
    row = _shared_row(promoted_by="auto", promotion_event=promo_event)

    async def fake_fetch_shared(*_a, **_kw):
        return row

    async def fake_audit(rid, *, ch=None, limit=100):
        return [
            _audit_row(
                ts=datetime(2026, 5, 18, 12, 0, 0),
                event_type="memory.auto_promoted",
                request_id=str(promo_event),
            ),
        ]

    cand_called = {"value": False}

    async def fake_find_candidate(*_a, **_kw):
        cand_called["value"] = True
        return None

    monkeypatch.setattr(shared_repo, "fetch_shared", fake_fetch_shared)
    monkeypatch.setattr(shared_repo, "find_candidate_for_promotion", fake_find_candidate)
    monkeypatch.setattr(audit_repo, "list_by_request_id", fake_audit)

    out = await handle_memory_lineage(
        MemoryLineageInput(
            org="clickhouse",
            namespace="finance",
            agent="reviewer",
            memory_key="policy_q1",
        )
    )
    assert out.found is True
    assert out.mechanism == "auto"
    assert out.candidate is None
    assert cand_called["value"] is False
    assert len(out.audit_chain) == 1
    assert out.audit_chain[0].event_type == "memory.auto_promoted"
    assert len(out.supporters) == 1
    assert out.supporters[0].agent == "agent-a"
    assert out.supporters[0].user_id == "alice"


@pytest.mark.asyncio
async def test_reviewer_promoted_with_exact_candidate_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """promoted_from_candidate_id → direct lookup, exact match, no time-window fallback."""
    from server.insights.store import audit as audit_repo
    from server.memory.api.models_graph import MemoryLineageInput
    from server.memory.app.dedup import handle_memory_lineage
    from server.memory.store import learnings as learnings_repo
    from server.memory.store import shared as shared_repo

    promo_event = uuid.uuid4()
    candidate_id = uuid.uuid4()
    promoted_at = datetime(2026, 5, 18, 12, 0, 0)

    row = _shared_row(
        promoted_by="reviewer@example.com",
        promotion_event=promo_event,
        promoted_at=promoted_at,
    )
    row["promoted_from_candidate_id"] = candidate_id

    async def fake_fetch_shared(*_a, **_kw):
        return row

    async def fake_audit(*_a, **_kw):
        return []

    window_called = {"v": False}

    async def fake_find_by_window(*_a, **_kw):
        window_called["v"] = True
        return None

    find_candidate_calls = []

    async def fake_find_candidate(org, ns, cid, *, ch=None):
        find_candidate_calls.append((org, ns, cid))
        return {
            "candidate_id": candidate_id,
            "memory_key": "policy_q1",
            "memory_type": "semantic",
            "proposed_value": '"value"',
            "supporters": [
                "clickhouse/finance/a-1/u/-/k",
                "clickhouse/finance/a-2/u/-/k",
            ],
            "status": "promoted",
            "rationale": "approved by reviewer",
            "source_kind": "conversation_seeder",
        }

    monkeypatch.setattr(shared_repo, "fetch_shared", fake_fetch_shared)
    monkeypatch.setattr(
        shared_repo,
        "find_candidate_for_promotion",
        fake_find_by_window,
    )
    monkeypatch.setattr(learnings_repo, "find_candidate", fake_find_candidate)
    monkeypatch.setattr(audit_repo, "list_by_request_id", fake_audit)

    out = await handle_memory_lineage(
        MemoryLineageInput(
            org="clickhouse",
            namespace="finance",
            agent="reviewer",
            memory_key="policy_q1",
        )
    )

    assert out.found is True
    assert out.mechanism == "review"
    assert out.candidate is not None
    assert out.candidate.candidate_id == str(candidate_id)
    assert out.candidate.match_confidence == "exact"
    assert out.candidate.supporter_count == 2
    assert out.candidate.source_kind == "conversation_seeder"
    assert window_called["v"] is False
    assert len(find_candidate_calls) == 1
    org_seen, ns_seen, cid_seen = find_candidate_calls[0]
    assert org_seen == "clickhouse"
    assert ns_seen == "finance"
    assert cid_seen == str(candidate_id)


@pytest.mark.asyncio
async def test_reviewer_promoted_with_inferred_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """learnings.review path; mechanism='review'; candidate matched inferred."""
    from server.insights.store import audit as audit_repo
    from server.memory.api.models_graph import MemoryLineageInput
    from server.memory.app.dedup import handle_memory_lineage
    from server.memory.store import shared as shared_repo

    promo_event = uuid.uuid4()
    promoted_at = datetime(2026, 5, 18, 12, 0, 0)
    candidate_id = uuid.uuid4()

    row = _shared_row(
        promoted_by="reviewer@example.com",
        promotion_event=promo_event,
        promoted_at=promoted_at,
        rationale="reviewed-and-promoted by reviewer@example.com",
    )

    async def fake_fetch_shared(*_a, **_kw):
        return row

    async def fake_audit(rid, *, ch=None, limit=100):
        return [
            _audit_row(
                ts=promoted_at,
                event_type="learnings.review",
                request_id=str(promo_event),
            ),
            _audit_row(
                ts=promoted_at + timedelta(milliseconds=50),
                event_type="memory.promote",
                request_id=str(promo_event),
            ),
        ]

    async def fake_find_candidate(org, ns, key, pa, win, *, ch=None):
        assert key == "policy_q1"
        assert win == 300
        return {
            "candidate_id": candidate_id,
            "source_kind": "conversation_seeder",
            "supporter_count": 2,
            "rationale": "conversation-seeder candidate",
            "decided_at": pa - timedelta(seconds=10),
            "decided_by": "reviewer@example.com",
            "status": "promoted",
        }

    monkeypatch.setattr(shared_repo, "fetch_shared", fake_fetch_shared)
    monkeypatch.setattr(shared_repo, "find_candidate_for_promotion", fake_find_candidate)
    monkeypatch.setattr(audit_repo, "list_by_request_id", fake_audit)

    out = await handle_memory_lineage(
        MemoryLineageInput(
            org="clickhouse",
            namespace="finance",
            agent="reviewer",
            memory_key="policy_q1",
        )
    )
    assert out.found is True
    assert out.mechanism == "review"
    assert out.candidate is not None
    assert out.candidate.candidate_id == str(candidate_id)
    assert out.candidate.source_kind == "conversation_seeder"
    assert out.candidate.match_confidence == "inferred"
    assert out.candidate.decided_by == "reviewer@example.com"
    assert [e.event_type for e in out.audit_chain] == [
        "learnings.review",
        "memory.promote",
    ]


@pytest.mark.asyncio
async def test_target_not_found_returns_found_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.api.models_graph import MemoryLineageInput
    from server.memory.app.dedup import handle_memory_lineage
    from server.memory.store import shared as shared_repo

    async def fake_fetch_shared(*_a, **_kw):
        return None

    monkeypatch.setattr(shared_repo, "fetch_shared", fake_fetch_shared)
    _stub_memory_tag_lookup(monkeypatch, row=None)

    out = await handle_memory_lineage(
        MemoryLineageInput(
            org="clickhouse",
            namespace="finance",
            agent="reviewer",
            memory_key="ghost",
        )
    )
    assert out.found is False
    assert out.memory_key == "ghost"
    assert out.mechanism == "unknown"
    assert out.candidate is None
    assert out.audit_chain == []


@pytest.mark.asyncio
async def test_tombstoned_target_returns_found_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A demoted (tombstoned) shared row reads as found=False for lineage."""
    from server.memory.api.models_graph import MemoryLineageInput
    from server.memory.app.dedup import handle_memory_lineage
    from server.memory.store import shared as shared_repo

    row = _shared_row(tombstoned_at=datetime(2026, 5, 18, 12, 0, 0))

    async def fake_fetch_shared(*_a, **_kw):
        return row

    monkeypatch.setattr(shared_repo, "fetch_shared", fake_fetch_shared)
    _stub_memory_tag_lookup(monkeypatch, row=None)

    out = await handle_memory_lineage(
        MemoryLineageInput(
            org="clickhouse",
            namespace="finance",
            agent="reviewer",
            memory_key="demoted",
        )
    )
    assert out.found is False


@pytest.mark.asyncio
async def test_malformed_supporter_tuples_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unrecognized supporter tuples are dropped; well-formed ones still render."""
    from server.insights.store import audit as audit_repo
    from server.memory.api.models_graph import MemoryLineageInput
    from server.memory.app.dedup import handle_memory_lineage
    from server.memory.store import shared as shared_repo

    row = _shared_row(
        promoted_from=[
            "clickhouse/finance/agent-a/alice/-/policy_q1",
            "not/enough/slashes",
            "clickhouse/finance/agent-b/bob/-/policy_q1",
        ]
    )

    async def fake_fetch_shared(*_a, **_kw):
        return row

    async def fake_audit(*_a, **_kw):
        return []

    monkeypatch.setattr(shared_repo, "fetch_shared", fake_fetch_shared)
    monkeypatch.setattr(audit_repo, "list_by_request_id", fake_audit)

    out = await handle_memory_lineage(
        MemoryLineageInput(
            org="clickhouse",
            namespace="finance",
            agent="reviewer",
            memory_key="policy_q1",
        )
    )
    assert out.found is True
    assert [s.agent for s in out.supporters] == ["agent-a", "agent-b"]


@pytest.mark.asyncio
async def test_review_path_with_no_candidate_match_still_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reviewer path with no candidate match still returns mechanism='review', candidate=None."""
    from server.insights.store import audit as audit_repo
    from server.memory.api.models_graph import MemoryLineageInput
    from server.memory.app.dedup import handle_memory_lineage
    from server.memory.store import shared as shared_repo

    promo_event = uuid.uuid4()
    row = _shared_row(promoted_by="reviewer@example.com", promotion_event=promo_event)

    async def fake_fetch_shared(*_a, **_kw):
        return row

    async def fake_audit(*_a, **_kw):
        return []

    async def fake_find_candidate(*_a, **_kw):
        return None

    monkeypatch.setattr(shared_repo, "fetch_shared", fake_fetch_shared)
    monkeypatch.setattr(shared_repo, "find_candidate_for_promotion", fake_find_candidate)
    monkeypatch.setattr(audit_repo, "list_by_request_id", fake_audit)

    out = await handle_memory_lineage(
        MemoryLineageInput(
            org="clickhouse",
            namespace="finance",
            agent="reviewer",
            memory_key="policy_q1",
        )
    )
    assert out.found is True
    assert out.mechanism == "review"
    assert out.candidate is None


@pytest.mark.asyncio
async def test_exact_match_surfaces_conversation_backpointer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Conversation-seeded candidate surfaces originating thread + message id."""
    from server.insights.store import audit as audit_repo
    from server.memory.api.models_graph import MemoryLineageInput
    from server.memory.app.dedup import handle_memory_lineage
    from server.memory.store import learnings as learnings_repo
    from server.memory.store import shared as shared_repo

    promo_event = uuid.uuid4()
    candidate_id = uuid.uuid4()
    promoted_at = datetime(2026, 5, 18, 12, 0, 0)

    row = _shared_row(
        promoted_by="reviewer@example.com",
        promotion_event=promo_event,
        promoted_at=promoted_at,
    )
    row["promoted_from_candidate_id"] = candidate_id

    async def fake_fetch_shared(*_a, **_kw):
        return row

    async def fake_audit(*_a, **_kw):
        return []

    async def fake_find_candidate(org, ns, cid, *, ch=None):
        return {
            "candidate_id": candidate_id,
            "memory_key": "policy_q1",
            "memory_type": "semantic",
            "proposed_value": '"v"',
            "supporters": ["clickhouse/finance/-/u/-/k"],
            "status": "promoted",
            "rationale": "approved",
            "source_kind": "conversation_seeder",
            "seeded_from_thread_id": "thread-42",
            "seeded_from_msg_id": "msg-last-99",
        }

    monkeypatch.setattr(shared_repo, "fetch_shared", fake_fetch_shared)
    monkeypatch.setattr(learnings_repo, "find_candidate", fake_find_candidate)
    monkeypatch.setattr(audit_repo, "list_by_request_id", fake_audit)

    out = await handle_memory_lineage(
        MemoryLineageInput(
            org="clickhouse",
            namespace="finance",
            agent="reviewer",
            memory_key="policy_q1",
        )
    )
    assert out.found is True
    assert out.candidate is not None
    assert out.candidate.match_confidence == "exact"
    assert out.candidate.seeded_from_thread_id == "thread-42"
    assert out.candidate.seeded_from_msg_id == "msg-last-99"


@pytest.mark.asyncio
async def test_memory_supporters_candidate_has_empty_backpointer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """memory_supporters candidates have no single origin turn; seeded_from_* stay empty."""
    from server.insights.store import audit as audit_repo
    from server.memory.api.models_graph import MemoryLineageInput
    from server.memory.app.dedup import handle_memory_lineage
    from server.memory.store import learnings as learnings_repo
    from server.memory.store import shared as shared_repo

    candidate_id = uuid.uuid4()
    promo_event = uuid.uuid4()
    row = _shared_row(
        promoted_by="reviewer@example.com",
        promotion_event=promo_event,
    )
    row["promoted_from_candidate_id"] = candidate_id

    async def fake_fetch_shared(*_a, **_kw):
        return row

    async def fake_audit(*_a, **_kw):
        return []

    async def fake_find_candidate(org, ns, cid, *, ch=None):
        return {
            "candidate_id": candidate_id,
            "memory_key": "policy_q1",
            "supporters": ["clickhouse/finance/-/u/-/k"],
            "status": "promoted",
            "rationale": "memory-supporters approved",
            "source_kind": "memory_supporters",
        }

    monkeypatch.setattr(shared_repo, "fetch_shared", fake_fetch_shared)
    monkeypatch.setattr(learnings_repo, "find_candidate", fake_find_candidate)
    monkeypatch.setattr(audit_repo, "list_by_request_id", fake_audit)

    out = await handle_memory_lineage(
        MemoryLineageInput(
            org="clickhouse",
            namespace="finance",
            agent="reviewer",
            memory_key="policy_q1",
        )
    )
    assert out.candidate is not None
    assert out.candidate.source_kind == "memory_supporters"
    assert out.candidate.seeded_from_thread_id == ""
    assert out.candidate.seeded_from_msg_id == ""


@pytest.mark.asyncio
async def test_operation_registered_with_viewer_role() -> None:
    from server.memory.api.ops_memory import OPERATIONS
    from server.operations.base import Role

    op = next(o for o in OPERATIONS if o.name == "memory.lineage")
    assert op.required_role == Role.VIEWER
    assert op.idempotent is True
    assert op.audit is False
