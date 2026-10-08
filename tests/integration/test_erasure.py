"""erasure.request + erasure.status — GDPR/EU AI Act scoped deletion.

Verifies:
  - user_id erasure tombstones memory + memory_embeddings; scrubs agent_events
  - other users' data is untouched
  - audit_log gets erasure.started + erasure.completed rows with chain
  - erasure.status returns the final state
  - namespace erasure tombstones the namespace's memory + chunks
  - other namespaces unaffected
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))


# ch_client fixture comes from tests/conftest.py (env-aware so Cloud runs work)


@pytest.fixture
async def admin_agent(control_plane_url: str, mint_test_token):
    """An agent representing the platform admin who initiates erasures.

    Uses a generous 60s timeout — erasure sagas run multiple
    `mutations_sync=2` ALTER UPDATEs whose latency scales with partition
    size; well past the 10s SDK default in some test orderings.

    Mints an admin-role token: erasure.request / status / export are all
    role=admin endpoints.
    """
    from loom import Agent

    ns = f"adm-{uuid.uuid4().hex[:6]}"
    # user_id erasure requires ORG_ADMIN or higher
    # (per-user scope isn't tied to a single namespace
    # so namespace-grant ADMIN isn't enough). namespace-scope
    # erasure also passes with ORG_ADMIN since it implies the
    # narrower namespace ADMIN.
    tok = mint_test_token(
        org="erasure-test-org", namespaces=["*"], roles=["org_admin", "admin", "writer"]
    )
    async with Agent(
        org="erasure-test-org",
        namespace=ns,
        agent="platform-admin",
        base_url=control_plane_url,
        timeout_s=60.0,
        api_key=tok,
    ) as a:
        yield a


# ============================================================
# user_id erasure
# ============================================================


@pytest.mark.slow
@pytest.mark.asyncio
async def test_user_id_erasure_tombstones_memory(admin_agent, ch_client) -> None:
    """Alice's preferences + semantic memory get tombstoned; Bob's untouched."""
    from loom import Agent

    ns = admin_agent.namespace
    target_user = f"alice-{uuid.uuid4().hex[:6]}"

    async with Agent(
        org="erasure-test-org",
        namespace=ns,
        agent="dwaine",
        base_url=admin_agent.transport._base_url,
    ) as a:
        # Alice writes a few memories
        await a.memory.set("prefers_json", True, memory_type="preference", user_id=target_user)
        await a.memory.set("favorite_table", "events", memory_type="semantic", user_id=target_user)
        # Bob also writes (control). Distinct value from alice's
        # "prefers_json: True" — server/memory/app/conflicts.py canonicalizes
        # values and merges across users when the canonical form
        # matches (scope is (org, ns, customer, memory_type) — no
        # user_id filter). bob's True would merge into alice's row
        # and never reach loom.memory under bob's user_id.
        await a.memory.set("prefers_yaml", "yaml-please", memory_type="preference", user_id="bob")

    # Run erasure for alice
    result = await admin_agent.erasure.request_user_id(target_user, requester="ops@example.com")
    assert result["status"] == "completed"
    assert result["erasure_id"]

    # Find the per-step rows in the response
    steps = {s["step"]: s for s in result["steps"]}
    assert steps["memory.tombstone"]["rows_affected"] >= 2
    assert (
        steps["memory_embeddings.tombstone"]["rows_affected"] >= 2
    )  # both prefs+semantic embedded
    # External systems: real-or-stub depending on config. In CI dev, the URLs
    # are unset so the stub backend reports rows_affected=0 with a "stub:
    # not configured" note.
    assert "langfuse.delete_user" in steps
    assert "clickstack.delete_user" in steps

    # CH check: alice's rows now have tombstoned_at set
    rows = ch_client.query(
        """
        SELECT user_id, tombstoned_at
        FROM loom.memory FINAL
        WHERE org = 'erasure-test-org' AND namespace = {ns:String}
        """,
        parameters={"ns": ns},
    ).result_rows
    alice_rows = [r for r in rows if r[0] == target_user]
    bob_rows = [r for r in rows if r[0] == "bob"]
    assert len(alice_rows) >= 2
    assert all(r[1] is not None for r in alice_rows), "alice's rows must be tombstoned"
    assert len(bob_rows) >= 1
    assert all(r[1] is None for r in bob_rows), "bob's rows must NOT be tombstoned"


@pytest.mark.slow
@pytest.mark.asyncio
async def test_user_id_erasure_scrubs_agent_events(admin_agent, ch_client) -> None:
    """agent_events.user_id replaced with '<erased>' to preserve cost rollups."""
    from loom import Agent

    ns = admin_agent.namespace
    target = f"carol-{uuid.uuid4().hex[:6]}"
    async with Agent(
        org="erasure-test-org",
        namespace=ns,
        agent="dwaine",
        base_url=admin_agent.transport._base_url,
    ) as a:
        await a.memory.set("k", 1, memory_type="semantic", user_id=target)

    # Poll briefly for agent_events flush (100ms emitter cadence)
    import asyncio

    for _ in range(20):
        rows = ch_client.query(
            "SELECT count() FROM loom.agent_events WHERE user_id = {u:String}",
            parameters={"u": target},
        ).result_rows
        if int(rows[0][0]) >= 1:
            break
        await asyncio.sleep(0.1)

    await admin_agent.erasure.request_user_id(target, requester="ops@x.com")

    # After erasure, no agent_events rows should carry the target user_id
    rows = ch_client.query(
        "SELECT count() FROM loom.agent_events WHERE user_id = {u:String}",
        parameters={"u": target},
    ).result_rows
    assert int(rows[0][0]) == 0, "user_id should have been scrubbed from agent_events"


@pytest.mark.asyncio
async def test_erasure_writes_audit_chain(admin_agent, ch_client) -> None:
    """started + completed rows land in audit_log with consistent hashes."""
    target = f"dan-{uuid.uuid4().hex[:6]}"

    result = await admin_agent.erasure.request_user_id(target, requester="legal@x.com")
    erasure_id = result["erasure_id"]

    rows = ch_client.query(
        """
        SELECT event_type, actor
        FROM loom.audit_log
        WHERE request_id = {rid:String}
        ORDER BY ts ASC
        """,
        parameters={"rid": erasure_id},
    ).result_rows
    event_types = [r[0] for r in rows]
    assert "erasure.started" in event_types
    assert "erasure.completed" in event_types
    assert all(r[1] == "legal@x.com" for r in rows)


@pytest.mark.asyncio
async def test_erasure_status_returns_completed(admin_agent) -> None:
    target = f"eve-{uuid.uuid4().hex[:6]}"
    result = await admin_agent.erasure.request_user_id(target, requester="ops@x.com")

    status = await admin_agent.erasure.status(result["erasure_id"])
    assert status is not None
    assert status["status"] == "completed"
    assert status["scope_type"] == "user_id"
    assert status["scope_value"] == target
    assert status["completed_at"] is not None


@pytest.mark.asyncio
async def test_status_for_unknown_id_returns_none(admin_agent) -> None:
    nonexistent = "00000000-0000-0000-0000-000000000000"
    assert await admin_agent.erasure.status(nonexistent) is None


# ============================================================
# namespace erasure
# ============================================================


@pytest.mark.slow
@pytest.mark.asyncio
async def test_namespace_erasure_tombstones_only_target(admin_agent, ch_client) -> None:
    """Erasing namespace X must not affect namespace Y."""
    from loom import Agent

    ns_target = f"erase-me-{uuid.uuid4().hex[:6]}"
    ns_keeper = f"keep-me-{uuid.uuid4().hex[:6]}"

    for ns in (ns_target, ns_keeper):
        async with Agent(
            org="erasure-test-org",
            namespace=ns,
            agent="dwaine",
            base_url=admin_agent.transport._base_url,
        ) as a:
            await a.memory.set("k1", {"data": ns}, memory_type="semantic")
            await a.knowledge.add(
                "Some content for the namespace",
                source_uri=f"src://{ns}",
                tags=["test"],
            )

    # Erase ns_target
    result = await admin_agent.erasure.request_namespace(ns_target, requester="ops@example.com")
    assert result["status"] == "completed"

    steps = {s["step"]: s for s in result["steps"]}
    assert steps["memory.tombstone"]["rows_affected"] >= 1
    assert steps["chunks.supersede"]["rows_affected"] >= 1

    # Target memory rows tombstoned; keeper untouched
    rows = ch_client.query(
        """
        SELECT namespace, tombstoned_at
        FROM loom.memory FINAL
        WHERE org = 'erasure-test-org' AND namespace IN ({t:String}, {k:String})
        """,
        parameters={"t": ns_target, "k": ns_keeper},
    ).result_rows
    target_rows = [r for r in rows if r[0] == ns_target]
    keeper_rows = [r for r in rows if r[0] == ns_keeper]
    assert all(r[1] is not None for r in target_rows)
    assert all(r[1] is None for r in keeper_rows)

    # Target chunks superseded; keeper not
    rows = ch_client.query(
        """
        SELECT namespace, doc_superseded_at
        FROM loom.chunks
        WHERE org = 'erasure-test-org' AND namespace IN ({t:String}, {k:String})
        """,
        parameters={"t": ns_target, "k": ns_keeper},
    ).result_rows
    target_chunks = [r for r in rows if r[0] == ns_target]
    keeper_chunks = [r for r in rows if r[0] == ns_keeper]
    assert all(r[1] is not None for r in target_chunks)
    assert all(r[1] is None for r in keeper_chunks)
