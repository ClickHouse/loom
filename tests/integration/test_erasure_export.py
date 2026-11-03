"""erasure.export — GDPR Article 20 right-to-portability.

Verifies:
  - user_id scope returns the user's memory + conversation turns and saved
    messages + agent_events + audit_log; other users excluded.
  - namespace scope returns the namespace's memory + knowledge chunks +
    agent_events + audit_log; other namespaces excluded.
  - empty subject → empty sections, every count 0.
  - tombstoned rows included by default; excluded when include_tombstoned=False.
  - The export itself emits an erasure.exported audit row.
  - section_limit truncation flag.
"""

from __future__ import annotations

import asyncio
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
    from loom import Agent

    ns = f"exp-{uuid.uuid4().hex[:6]}"
    # A subject export spans every namespace of the org, so it needs org admin.
    tok = mint_test_token(
        org="export-test-org", namespaces=["*"], roles=["org_admin", "admin", "writer"]
    )
    async with Agent(
        org="export-test-org",
        namespace=ns,
        agent="platform-admin",
        base_url=control_plane_url,
        timeout_s=60.0,
        api_key=tok,
    ) as a:
        yield a


# ============================================================
# user_id scope
# ============================================================


@pytest.mark.asyncio
async def test_export_user_id_returns_user_memory_only(admin_agent) -> None:
    """Alice's rows export; Bob's stay in CH but are not in the archive."""
    from loom import Agent

    ns = admin_agent.namespace
    alice = f"alice-{uuid.uuid4().hex[:6]}"
    bob = f"bob-{uuid.uuid4().hex[:6]}"

    async with Agent(
        org="export-test-org",
        namespace=ns,
        agent="writer",
        base_url=admin_agent.transport._base_url,
    ) as w:
        await w.memory.set("pref_json", True, memory_type="preference", user_id=alice)
        await w.memory.set("fav_table", "events", memory_type="semantic", user_id=alice)
        await w.memory.set("pref_yaml", True, memory_type="preference", user_id=bob)

    archive = await admin_agent.erasure.export_user_id(alice, requester="ops@example.com")

    assert archive["scope_type"] == "user_id"
    assert archive["scope_value"] == alice
    assert archive["counts"]["memory"] >= 2
    alice_keys = {r["memory_key"] for r in archive["memory"]["rows"]}
    assert alice_keys >= {"pref_json", "fav_table"}
    # No Bob
    assert not any(r["user_id"] == bob for r in archive["memory"]["rows"])

    # Knowledge is empty for user_id scope (knowledge is namespace-level)
    assert archive["counts"]["knowledge"] == 0
    assert archive["knowledge"]["rows"] == []


@pytest.mark.asyncio
async def test_export_user_id_empty_subject_returns_empty_sections(admin_agent) -> None:
    """A user who never wrote anything → counts all zero, no errors."""
    nobody = f"ghost-{uuid.uuid4().hex[:8]}"
    archive = await admin_agent.erasure.export_user_id(nobody, requester="ops@x.com")

    sections = (
        "memory",
        "knowledge",
        "conversations",
        "conversation_threads",
        "agent_events",
        "audit_log",
    )
    assert archive["counts"] == dict.fromkeys(sections, 0)
    for section in sections:
        assert archive[section]["rows"] == []
        assert archive[section]["truncated"] is False


@pytest.mark.asyncio
async def test_export_user_id_excludes_tombstoned_when_requested(admin_agent) -> None:
    """include_tombstoned=False filters out soft-deleted memory rows."""
    from loom import Agent

    ns = admin_agent.namespace
    user = f"erin-{uuid.uuid4().hex[:6]}"

    async with Agent(
        org="export-test-org",
        namespace=ns,
        agent="writer",
        base_url=admin_agent.transport._base_url,
    ) as w:
        await w.memory.set("alive", 1, memory_type="semantic", user_id=user)
        await w.memory.set("dying", 2, memory_type="semantic", user_id=user)
        await w.memory.delete("dying", user_id=user)

    full = await admin_agent.erasure.export_user_id(
        user, requester="ops@x.com", include_tombstoned=True
    )
    live_only = await admin_agent.erasure.export_user_id(
        user, requester="ops@x.com", include_tombstoned=False
    )

    full_keys = {r["memory_key"] for r in full["memory"]["rows"]}
    live_keys = {r["memory_key"] for r in live_only["memory"]["rows"]}
    assert "alive" in full_keys and "dying" in full_keys
    assert "alive" in live_keys and "dying" not in live_keys


@pytest.mark.asyncio
async def test_export_user_id_includes_agent_events(admin_agent, ch_client) -> None:
    """agent_events firehose rows for this user_id appear in the export."""
    from loom import Agent

    ns = admin_agent.namespace
    user = f"frank-{uuid.uuid4().hex[:6]}"

    async with Agent(
        org="export-test-org",
        namespace=ns,
        agent="writer",
        base_url=admin_agent.transport._base_url,
    ) as w:
        await w.memory.set("k", 1, memory_type="semantic", user_id=user)

    # Wait for events flush (~100ms emitter cadence)
    for _ in range(30):
        rows = ch_client.query(
            "SELECT count() FROM loom.agent_events WHERE user_id = {u:String}",
            parameters={"u": user},
        ).result_rows
        if int(rows[0][0]) >= 1:
            break
        await asyncio.sleep(0.1)

    archive = await admin_agent.erasure.export_user_id(user, requester="ops@x.com")
    assert archive["counts"]["agent_events"] >= 1
    assert all(r["user_id"] == user for r in archive["agent_events"]["rows"])


@pytest.mark.asyncio
async def test_export_user_id_includes_their_saved_messages_and_turns(
    admin_agent, ch_client
) -> None:
    """A user's saved message, their transcript turns and their thread export with the
    columns stored; another user's do not, and a turn holding a key is labelled high."""
    ns = admin_agent.namespace
    org = admin_agent.org
    grace = f"grace-{uuid.uuid4().hex[:6]}"
    heidi = f"heidi-{uuid.uuid4().hex[:6]}"
    thread = f"t-{uuid.uuid4().hex[:6]}"
    base = {"org": org, "namespace": ns, "agent": "chat"}
    key = "AKIA" + "ABCDEFGHIJKLMNOP"

    post = admin_agent.transport.post
    await post("/v1/conversation.set_logging", {"org": org, "namespace": ns, "enabled": True})
    await post(
        "/v1/conversation.create",
        {**base, "thread_id": thread, "title": "Renewal terms", "user_id": grace},
    )
    await post(
        "/v1/conversation.append",
        {
            **base,
            "thread_id": thread,
            "msg_id": "m-turn",
            "role": "user",
            "content": f"my key is {key}",
            "user_id": grace,
        },
    )
    saved = await post(
        "/v1/conversation.save_message",
        {
            **base,
            "conversation_id": thread,
            "message_id": "m-saved",
            "role": "assistant",
            "content": "the renewal clause caps the increase at four percent",
            "user_id": grace,
        },
    )
    assert saved["indexed"] is True
    await post(
        "/v1/conversation.save_message",
        {
            **base,
            "conversation_id": f"t-{heidi}",
            "message_id": "m-heidi",
            "role": "user",
            "content": "heidi's own saved message",
            "user_id": heidi,
        },
    )

    archive = await admin_agent.erasure.export_user_id(grace, requester="ops@x.com")

    rows = {r["msg_id"]: r for r in archive["conversations"]["rows"]}
    assert set(rows) == {"m-turn", "m-saved"}
    assert archive["counts"]["conversations"] == 2
    msg = rows["m-saved"]
    assert (msg["namespace"], msg["agent"], msg["thread_id"], msg["role"]) == (
        ns,
        "chat",
        thread,
        "assistant",
    )
    assert msg["content"] == "the renewal clause caps the increase at four percent"
    assert msg["user_id"] == grace and msg["is_saved"] is True
    assert msg["saved_at"] is not None and msg["tombstoned_at"] is None
    assert msg["sensitivity"] == ""
    turn = rows["m-turn"]
    assert turn["is_saved"] is False and turn["content"] == f"my key is {key}"
    assert turn["sensitivity"] == "high"
    threads = archive["conversation_threads"]["rows"]
    assert [(t["thread_id"], t["title"], t["user_id"]) for t in threads] == [
        (thread, "Renewal terms", grace)
    ]

    # The high turn it served is audited, with its agent, thread and message.
    for _ in range(30):
        audited = ch_client.query(
            """
            SELECT subject FROM loom.audit_log
            WHERE event_type = 'memory.sensitive_read' AND org = {o:String}
              AND namespace = {ns:String}
            """,
            parameters={"o": org, "ns": ns},
        ).result_rows
        if audited:
            break
        await asyncio.sleep(0.1)
    assert len(audited) == 1
    assert all(part in audited[0][0] for part in ('"chat"', thread, '"m-turn"'))


# ============================================================
# namespace scope
# ============================================================


@pytest.mark.asyncio
async def test_export_namespace_returns_chunks_and_memory(admin_agent) -> None:
    """Whole-namespace export includes knowledge chunks (user_id scope doesn't)."""
    from loom import Agent

    ns = admin_agent.namespace
    async with Agent(
        org="export-test-org",
        namespace=ns,
        agent="writer",
        base_url=admin_agent.transport._base_url,
    ) as w:
        await w.memory.set("ns_key", 1, memory_type="semantic")
        await w.knowledge.add(
            content="Loom does memory + knowledge + conversations.",
            source_uri=f"doc://export-{uuid.uuid4().hex[:6]}",
            title="export test",
            tags=["export-test"],
        )

    archive = await admin_agent.erasure.export_namespace(ns, requester="ops@x.com")

    assert archive["scope_type"] == "namespace"
    assert archive["scope_value"] == ns
    assert archive["counts"]["memory"] >= 1
    assert archive["counts"]["knowledge"] >= 1
    assert all(r["namespace"] == ns for r in archive["memory"]["rows"])
    assert all(r["namespace"] == ns for r in archive["knowledge"]["rows"])


@pytest.mark.asyncio
async def test_export_namespace_isolated_from_other_namespaces(admin_agent) -> None:
    """Writes in other namespaces don't leak into a namespace-scoped export."""
    from loom import Agent

    target_ns = admin_agent.namespace
    other_ns = f"other-{uuid.uuid4().hex[:6]}"

    async with Agent(
        org="export-test-org",
        namespace=target_ns,
        agent="writer",
        base_url=admin_agent.transport._base_url,
    ) as w:
        await w.memory.set("in_target", 1, memory_type="semantic")

    async with Agent(
        org="export-test-org",
        namespace=other_ns,
        agent="writer",
        base_url=admin_agent.transport._base_url,
    ) as w:
        await w.memory.set("in_other", 1, memory_type="semantic")

    archive = await admin_agent.erasure.export_namespace(target_ns, requester="ops@x.com")
    keys = {r["memory_key"] for r in archive["memory"]["rows"]}
    assert "in_target" in keys
    assert "in_other" not in keys


# ============================================================
# Audit + truncation
# ============================================================


@pytest.mark.asyncio
async def test_export_emits_audit_row(admin_agent, ch_client) -> None:
    """Every export run leaves an erasure.exported audit_log row."""
    user = f"audited-{uuid.uuid4().hex[:6]}"
    await admin_agent.erasure.export_user_id(user, requester="compliance@example.com")

    # Wait for audit emitter flush
    for _ in range(30):
        rows = ch_client.query(
            """
            SELECT count() FROM loom.audit_log
            WHERE event_type = 'erasure.exported' AND actor = 'process:test-suite'
              AND JSONExtractString(subject, 'requester') = 'compliance@example.com'
              AND position(subject, {u:String}) > 0
            """,
            parameters={"u": user},
        ).result_rows
        if int(rows[0][0]) >= 1:
            break
        await asyncio.sleep(0.1)
    assert int(rows[0][0]) >= 1


@pytest.mark.asyncio
async def test_export_section_limit_marks_truncated(admin_agent) -> None:
    """If a section exceeds section_limit, the response carries truncated=True."""
    from loom import Agent

    ns = admin_agent.namespace
    user = f"chatty-{uuid.uuid4().hex[:6]}"

    async with Agent(
        org="export-test-org",
        namespace=ns,
        agent="writer",
        base_url=admin_agent.transport._base_url,
    ) as w:
        for i in range(5):
            await w.memory.set(f"k{i}", i, memory_type="semantic", user_id=user)

    archive = await admin_agent.erasure.export(
        scope_type="user_id",
        scope_value=user,
        requester="ops@x.com",
        section_limit=2,
    )
    assert archive["memory"]["truncated"] is True
    assert archive["memory"]["count"] == 2
