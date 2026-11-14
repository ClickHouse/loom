"""memory.promote / memory.demote + shared memory plane.

Covers the explicit promotion path:
  - promote inserts a row into loom.shared and a scope='shared' embedding
  - cross-agent visibility: A promotes; B finds the memory under scope='shared'
  - demote tombstones the shared row + embedding (no longer searchable)
  - demote outside the reversal window still works (was_reversible=False)
  - demote of a non-existent shared key is an idempotent no-op
  - source memory's private layer is unaffected by promote/demote
  - audit_log records both events
  - a definition is promoted from its namespace-wide row with the hash memory.get serves
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))


# ch_client fixture comes from tests/conftest.py (env-aware so Cloud runs work)


@pytest.fixture
async def detector(control_plane_url: str, mint_test_token):
    """agent-pipeline's Detector — writes the original learning.

    memory.promote / memory.demote are ADMIN-role ops (the explicit
    sharing path is privileged); both agents in this module use an
    admin token so they can drive the promote/demote/cross-agent flow.
    """
    from loom import Agent

    ns = f"sh-{uuid.uuid4().hex[:8]}"
    tok = mint_test_token(org="clickhouse", namespaces=["*"], roles=["admin", "writer"])
    async with Agent(
        org="clickhouse",
        namespace=ns,
        agent="detector",
        base_url=control_plane_url,
        api_key=tok,
    ) as a:
        yield a


@pytest.fixture
async def validator(detector, control_plane_url: str, mint_test_token):
    """Different agent in the same namespace — should see the promoted memory."""
    from loom import Agent

    tok = mint_test_token(org="clickhouse", namespaces=["*"], roles=["admin", "writer"])
    async with Agent(
        org="clickhouse",
        namespace=detector.namespace,
        agent="validator",
        base_url=control_plane_url,
        api_key=tok,
    ) as a:
        yield a


# ============================================================
# promote
# ============================================================


@pytest.mark.asyncio
async def test_promote_writes_shared_row(detector, ch_client) -> None:
    """memory.promote inserts into loom.shared with provenance."""
    await detector.memory.set(
        "lesson_x",
        {"pattern": "always guard nullable Maybe<T> with explicit check"},
        memory_type="semantic",
    )
    result = await detector.memory.promote(
        "lesson_x",
        rationale="observed in 3 unrelated PRs",
    )
    assert "shared_memory_key" in result
    assert result["shared_memory_key"] == "lesson_x"
    assert "promotion_event" in result
    assert "reversible_until" in result

    rows = ch_client.query(
        """
        SELECT memory_key, memory_type, promoted_by, rationale
        FROM loom.shared FINAL
        WHERE org = 'clickhouse' AND namespace = {ns:String}
        """,
        parameters={"ns": detector.namespace},
    ).result_rows
    assert len(rows) == 1
    assert rows[0][0] == "lesson_x"
    assert rows[0][1] == "semantic"
    assert "3 unrelated PRs" in rows[0][3]


@pytest.mark.asyncio
async def test_promote_then_cross_agent_search_finds_it(detector, validator) -> None:
    """Agent A promotes; Agent B searches scope='shared' and finds it."""
    await detector.memory.set(
        "shared_finding",
        {"text": "race condition in concurrent MergeTree part removal"},
        memory_type="semantic",
    )
    await detector.memory.promote("shared_finding", rationale="cross-agent learning")

    hits = await validator.memory.search(
        "race condition in concurrent MergeTree part removal",
        memory_type="semantic",
        scope="shared",
        top_k=5,
    )
    keys = {h.memory_key for h in hits}
    assert "shared_finding" in keys


@pytest.mark.asyncio
async def test_promote_does_not_affect_private(detector) -> None:
    """The original private memory still exists after promotion."""
    await detector.memory.set("k1", {"v": 1}, memory_type="semantic")
    await detector.memory.promote("k1", rationale="x")

    private = await detector.memory.get("k1")
    assert private is not None
    assert private["value"] == {"v": 1}


@pytest.mark.asyncio
async def test_promote_nonexistent_source_returns_404(detector) -> None:
    """Promoting a memory that doesn't exist raises NotFound."""
    with pytest.raises(Exception) as ei:
        await detector.memory.promote("never-existed")
    assert "RESOURCE_NOT_FOUND" in str(ei.value)


@pytest.mark.asyncio
async def test_promote_with_target_key_rename(detector, ch_client) -> None:
    """target_key overrides the shared memory_key."""
    await detector.memory.set("private_k", "v1", memory_type="semantic")
    await detector.memory.promote("private_k", target_key="public_k")

    rows = ch_client.query(
        """
        SELECT memory_key FROM loom.shared FINAL
        WHERE org = 'clickhouse' AND namespace = {ns:String}
        """,
        parameters={"ns": detector.namespace},
    ).result_rows
    keys = {r[0] for r in rows}
    assert "public_k" in keys
    assert "private_k" not in keys


# ============================================================
# demote
# ============================================================


@pytest.mark.asyncio
async def test_demote_within_window_tombstones(detector, validator, ch_client) -> None:
    """Demote within reversible_until → was_reversible=True, search no longer finds it."""
    await detector.memory.set(
        "tmp_lesson",
        {"text": "memory leak in materialized view background merge"},
        memory_type="semantic",
    )
    await detector.memory.promote("tmp_lesson", reversible_for_days=7)

    # Before demote: Validator can find it via shared search
    pre = await validator.memory.search("memory leak materialized view", scope="shared", top_k=5)
    assert any(h.memory_key == "tmp_lesson" for h in pre)

    # Demote
    result = await detector.memory.demote("tmp_lesson", rationale="false positive")
    assert result["demoted"] is True
    assert result["was_reversible"] is True
    assert result["tombstoned_at"] is not None

    # After demote: shared search no longer finds it
    post = await validator.memory.search("memory leak materialized view", scope="shared", top_k=5)
    assert not any(h.memory_key == "tmp_lesson" for h in post)

    # The shared row is now tombstoned
    rows = ch_client.query(
        """
        SELECT memory_key, tombstoned_at FROM loom.shared FINAL
        WHERE org = 'clickhouse' AND namespace = {ns:String}
          AND memory_key = 'tmp_lesson'
        """,
        parameters={"ns": detector.namespace},
    ).result_rows
    assert len(rows) == 1
    assert rows[0][1] is not None


@pytest.mark.asyncio
async def test_demote_nonexistent_is_noop(detector) -> None:
    """Demoting a memory that was never promoted is a no-op."""
    result = await detector.memory.demote("never-promoted")
    assert result["demoted"] is False
    assert result["was_reversible"] is False


@pytest.mark.asyncio
async def test_demote_then_re_promote_works(detector) -> None:
    """A demoted key can be re-promoted (new version supersedes the tombstone)."""
    await detector.memory.set("renewable", {"v": 1}, memory_type="semantic")
    await detector.memory.promote("renewable")
    await detector.memory.demote("renewable")
    # Re-promote with new content
    await detector.memory.set("renewable", {"v": 2}, memory_type="semantic")
    result = await detector.memory.promote("renewable")
    assert result["shared_memory_key"] == "renewable"


# ============================================================
# audit
# ============================================================


@pytest.mark.asyncio
async def test_promote_and_demote_emit_audit_rows(detector, ch_client) -> None:
    """Both memory.promote and memory.demote are flagged audit=True."""
    await detector.memory.set("audited", {"v": 1}, memory_type="semantic")
    await detector.memory.promote("audited", rationale="for the audit trail")
    await detector.memory.demote("audited", rationale="rolled back")

    rows = ch_client.query(
        """
        SELECT event_type FROM loom.audit_log
        WHERE namespace = {ns:String} AND event_type IN ('memory.promote', 'memory.demote')
        """,
        parameters={"ns": detector.namespace},
    ).result_rows
    event_types = {r[0] for r in rows}
    assert "memory.promote" in event_types
    assert "memory.demote" in event_types


@pytest.mark.asyncio
async def test_a_definition_is_promoted_with_the_hash_get_serves(
    control_plane_url: str, mint_test_token, ch_client
) -> None:
    """Through the SDK: the namespace-wide row written as agent '-', refused without a
    hash, promoted with the `content_sha256` memory.get serves, by the token's actor."""
    from loom import Agent
    from loom.errors import ValidationError

    from tests.integration._resources import unique_org

    org, ns = unique_org("definition"), f"def-{uuid.uuid4().hex[:8]}"
    tok = mint_test_token(org=org, namespaces=["*"], roles=["admin", "writer"], subject="definer")
    value = {"term": "ARR", "aliases": ["annual recurring revenue"], "body": "Yearly revenue."}
    async with Agent(
        org=org, namespace=ns, agent="detector", base_url=control_plane_url, api_key=tok
    ) as a:
        await a.memory.set("definition/arr", value, "definition", agent_id="-")
        got = await a.memory.get("definition/arr", agent_id="-")
        with pytest.raises(ValidationError) as e:  # no approved hash
            await a.memory.promote("definition/arr", agent_id="-")
        assert e.value.code == "VALIDATION_FAILED"
        shared_rows = "SELECT count() FROM loom.shared WHERE org = {org:String}"
        assert ch_client.query(shared_rows, parameters={"org": org}).result_rows == [(0,)]
        out = await a.memory.share(
            "definition/arr",
            agent_id="-",
            approved_sha256=got["content_sha256"],
            approver="lead",
            rationale="the finance glossary",
        )
    assert out["shared_memory_key"] == "definition/arr" and out["ignored_args"] == ["approver"]
    rows = ch_client.query(
        """
        SELECT memory_type, value, promoted_by, promoted_from, promoted_from_candidate_id,
               promotion_event, rationale, confidence
        FROM loom.shared FINAL
        WHERE org = {org:String} AND namespace = {ns:String} AND memory_key = 'definition/arr'
        """,
        parameters={"org": org, "ns": ns},
    ).result_rows
    (
        (memory_type, stored, promoted_by, promoted_from, candidate, event, rationale, confidence),
    ) = rows
    assert memory_type == "definition" and json.loads(stored) == value
    assert promoted_by == "process:definer"  # the token's actor, not the body's approver
    assert promoted_from == [f"{org}/{ns}/-/-/-/definition/arr"]
    assert candidate is None and str(event) == out["promotion_event"]
    assert rationale == "the finance glossary" and confidence == 1.0


@pytest.mark.asyncio
async def test_a_name_is_held_until_its_definition_is_demoted(
    control_plane_url: str, mint_test_token
) -> None:
    """Through the server: a name another definition holds is refused, the definition's
    own revision is not, and demoting it gives the name back."""
    from loom import Agent
    from loom.errors import LoomError

    from tests.integration._resources import unique_org

    org, ns = unique_org("definition-names"), f"def-{uuid.uuid4().hex[:8]}"
    tok = mint_test_token(org=org, namespaces=["*"], roles=["admin", "writer"], subject="definer")
    async with Agent(
        org=org, namespace=ns, agent="detector", base_url=control_plane_url, api_key=tok
    ) as a:

        async def promote(key: str, value: dict) -> dict:
            await a.memory.set(key, value, "definition", agent_id="-")
            got = await a.memory.get(key, agent_id="-")
            return await a.memory.promote(key, agent_id="-", approved_sha256=got["content_sha256"])

        await promote("definition/arr", {"term": "ARR", "body": "Annual recurring revenue."})
        revenue = {"term": "Revenue", "aliases": ["arr"], "body": "Yearly income."}
        with pytest.raises(LoomError) as e:
            await promote("definition/revenue", revenue)
        assert e.value.code == "DEFINITION_NAME_TAKEN"
        assert e.value.detail["conflicting_keys"] == ["definition/arr"]
        assert e.value.detail["names"] == ["arr"]
        # Its own revision holds the name already.
        await promote("definition/arr", {"term": "ARR", "body": "Yearly recurring revenue."})
        await a.memory.demote("definition/arr")
        out = await promote("definition/revenue", revenue)
    assert out["shared_memory_key"] == "definition/revenue"


@pytest.mark.asyncio
async def test_promotions_racing_for_one_name_leave_one_holder(
    control_plane_url: str, mint_test_token, ch_client
) -> None:
    """Through the server: three definitions sharing a term, promoted at once, leave one
    shared row; the others are refused by name or by the namespace's claim."""
    from loom import Agent
    from loom.errors import LoomError

    from tests.integration._resources import unique_org

    org, ns = unique_org("definition-race"), f"def-{uuid.uuid4().hex[:8]}"
    tok = mint_test_token(org=org, namespaces=["*"], roles=["admin", "writer"], subject="definer")
    keys = [f"definition/arr-{i}" for i in range(3)]
    async with Agent(
        org=org, namespace=ns, agent="detector", base_url=control_plane_url, api_key=tok
    ) as a:
        hashes = {}
        for key in keys:
            await a.memory.set(key, {"term": "ARR", "body": key}, "definition", agent_id="-")
            hashes[key] = (await a.memory.get(key, agent_id="-"))["content_sha256"]
        results = await asyncio.gather(
            *(a.memory.promote(k, agent_id="-", approved_sha256=hashes[k]) for k in keys),
            return_exceptions=True,
        )
    refused = [r.code for r in results if isinstance(r, LoomError)]
    assert len(refused) == 2, results
    assert set(refused) <= {"DEFINITION_NAME_TAKEN", "OVERLOADED"}
    count = ch_client.query(
        "SELECT count() FROM loom.shared FINAL WHERE org = {org:String} AND namespace = {ns:String}",
        parameters={"org": org, "ns": ns},
    ).result_rows[0][0]
    assert count == 1
