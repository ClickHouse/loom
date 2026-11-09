"""Integration tests for every repository against live ClickHouse.

Skip cleanly when LOOM_CH_HOST is unset. Each test isolates itself under a
per-run UUID namespace and cleans up after itself.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from server.identity.domain.read_scope import WHOLE_NAMESPACE

pytestmark = pytest.mark.skipif(
    not os.environ.get("LOOM_CH_HOST"),
    reason="LOOM_CH_HOST not set — integration tests require a live CH",
)


def _wide(head: list[float]) -> list[float]:
    """`head` zero-padded to the configured width (cosines unchanged): a
    coded vector column takes vectors of exactly that length."""
    from server.settings import settings

    return head + [0.0] * (settings.embedding_dim - len(head))


@pytest.fixture(scope="module")
async def ch() -> Any:
    from server.core.ch import get_client

    c = get_client()
    await c.connect()
    yield c
    await c.disconnect()


@pytest.fixture
def isolated_id() -> str:
    return f"itest-{uuid.uuid4().hex[:12]}"


@pytest.fixture
def identity(isolated_id: str) -> Any:
    from server.memory.store import Identity

    return Identity(
        org="itest-org",
        namespace=isolated_id,
        agent="itest-bot",
        user_id="-",
        session_id="-",
    )


async def _seed_memory(
    ch: Any,
    identity: Any,
    memory_key: str,
    memory_type: str,
    value: Any,
    *,
    confidence: float = 1.0,
    pinned: bool = False,
) -> int:
    """Seed one loom.memory row as a fresh RMT version row; return the version."""
    import json
    import uuid

    from server.memory import store as memory_repo

    version = memory_repo.next_version()
    await ch.insert(
        "loom.memory",
        [
            {
                "memory_id": str(uuid.uuid4()),
                "org": identity.org,
                "namespace": identity.namespace,
                "agent": identity.agent,
                "user_id": identity.user_id,
                "session_id": identity.session_id,
                "memory_key": memory_key,
                "memory_type": memory_type,
                "value": json.dumps(value, default=str),
                "confidence": confidence,
                "version": version,
                "pinned": 1 if pinned else 0,
            }
        ],
        settings={"async_insert": 0, "wait_for_async_insert": 1},
    )
    return version


async def _tombstone_memory(ch: Any, identity: Any, memory_key: str) -> None:
    await ch.execute(
        """
        ALTER TABLE loom.memory UPDATE tombstoned_at = now()
        WHERE org = {org:String} AND namespace = {ns:String}
          AND agent = {agent:String} AND user_id = {uid:String}
          AND session_id = {sid:String} AND memory_key = {key:String}
          AND tombstoned_at IS NULL
        SETTINGS mutations_sync = 2
        """,
        params={
            "org": identity.org,
            "ns": identity.namespace,
            "agent": identity.agent,
            "uid": identity.user_id,
            "sid": identity.session_id,
            "key": memory_key,
        },
    )


@pytest.mark.asyncio
async def test_memory_repo_set_get_delete_roundtrip(ch: Any, identity: Any) -> None:
    from server.memory import store as memory_repo

    version = await _seed_memory(
        ch,
        identity,
        "deploy_window",
        "preference",
        "Tue/Thu, never Friday",
        pinned=True,
    )
    assert version > 0

    fetched = await memory_repo.get_by_key(identity, "deploy_window", ch=ch, scope=WHOLE_NAMESPACE)
    assert fetched is not None
    assert fetched.value == "Tue/Thu, never Friday"
    assert fetched.pinned is True
    assert fetched.memory_type == "preference"
    assert fetched.version == version

    await _tombstone_memory(ch, identity, "deploy_window")

    gone = await memory_repo.get_by_key(identity, "deploy_window", ch=ch, scope=WHOLE_NAMESPACE)
    assert gone is None


@pytest.mark.asyncio
async def test_memory_repo_list_filters(ch: Any, identity: Any) -> None:
    from server.memory import store as memory_repo

    for i, mt in enumerate(["semantic", "semantic", "procedural"]):
        await _seed_memory(
            ch,
            identity,
            f"k_{i}",
            mt,
            f"value-{i}",
            confidence=0.9,
        )

    semantic = await memory_repo.list_by_namespace(
        identity,
        memory_type="semantic",
        order_by="memory_key_asc",
        limit=10,
        ch=ch,
        scope=WHOLE_NAMESPACE,
    )
    assert len(semantic) == 2
    assert all(r.memory_type == "semantic" for r in semantic)
    keys = [r.memory_key for r in semantic]
    assert keys == sorted(keys)


@pytest.mark.asyncio
async def test_memory_repo_get_many_by_keys(ch: Any, identity: Any) -> None:
    from server.memory import store as memory_repo

    for k in ("a", "b"):
        await _seed_memory(ch, identity, k, "semantic", f"v-{k}")

    out = await memory_repo.get_many_by_keys(
        identity,
        ["a", "b", "missing"],
        ch=ch,
        scope=WHOLE_NAMESPACE,
    )
    keys = sorted(r.memory_key for r in out)
    assert keys == ["a", "b"]


@pytest.mark.asyncio
async def test_audit_repo_list_by_request_id(ch: Any, isolated_id: str) -> None:
    from server.insights.store import audit as audit_repo

    org = "itest-org"
    ns = isolated_id

    request_id = str(uuid.uuid4())
    await ch.insert(
        "loom.audit_log",
        [
            {
                "ts": datetime.now(UTC).replace(tzinfo=None),
                "event_type": "itest.event",
                "org": org,
                "namespace": ns,
                "actor": "itest",
                "subject": "{}",
                "request_id": request_id,
            }
        ],
    )

    by_id = await audit_repo.list_by_request_id(request_id, ch=ch, limit=10)
    assert len(by_id) >= 1
    assert by_id[0].request_id == request_id


@pytest.mark.asyncio
async def test_documents_and_chunks_roundtrip(
    ch: Any,
    isolated_id: str,
) -> None:
    from server.knowledge.store import chunks as knowledge_repo
    from server.knowledge.store import documents as documents_repo

    org, ns = "itest-org", isolated_id
    uri = "itest://doc1"
    now = datetime.now(UTC).replace(tzinfo=None)

    await documents_repo.insert_one(
        {
            "org": org,
            "namespace": ns,
            "source_uri": uri,
            "doc_id": str(uuid.uuid4()),
            "title": "T",
            "content": "C",
            "tags": ["itest"],
            "metadata": "{}",
            "version": 1,
            "superseded_at": None,
            "ingested_at": now,
        },
        over=0,
        ch=ch,
    )

    await knowledge_repo.insert_chunks(
        [
            {
                "org": org,
                "namespace": ns,
                "source_uri": uri,
                "chunk_idx": i,
                "content": f"c{i}",
                "tags": ["itest"],
                "doc_metadata": "{}",
                "chunk_metadata": "{}",
                "ingested_at": now,
                "doc_superseded_at": None,
                "embedding_model": "itest-model",
                "embedding_dim": len(_wide([])),
                "embedding": _wide([0.1, 0.2, 0.3, 0.4]),
                "created_at": now,
            }
            for i in range(2)
        ],
        ch=ch,
    )

    n = await knowledge_repo.count_active_for_doc(org, ns, uri, ch=ch)
    assert n == 2

    doc = await documents_repo.get_one(org, ns, uri, ch=ch)
    assert doc is not None
    assert doc.title == "T"
    assert doc.tags == ["itest"]

    await knowledge_repo.mark_doc_superseded(org, ns, uri, when=now, ch=ch)
    n_after = await knowledge_repo.count_active_for_doc(org, ns, uri, ch=ch)
    assert n_after == 0


@pytest.mark.asyncio
async def test_retention_apply_emits_multiif_with_per_namespace_overrides(
    ch: Any,
) -> None:
    from server.storage import retention_overrides as _ro

    test_org = "itest-ttl-org"
    test_ns = "itest-ttl-ns"

    @dataclass_compat
    class _LocalSettings:
        retention_audit_log_days: int = 0
        retention_agent_events_days: int = 0
        retention_memory_days: int = 0
        retention_memory_embeddings_days: int = 0
        retention_conversations_days: int = 0
        retention_chunks_days: int = 0
        retention_documents_days: int = 0
        tiering_audit_log_warm_days: int = 0
        tiering_agent_events_warm_days: int = 0
        tiering_memory_warm_days: int = 0
        tiering_memory_embeddings_warm_days: int = 0
        tiering_conversations_warm_days: int = 0
        tiering_chunks_warm_days: int = 0
        tiering_documents_warm_days: int = 0
        tiering_warm_volume_name: str = "warm"

    overrides = [
        {"org": test_org, "namespace": test_ns, "retention_days": 90, "warm_days": 0},
    ]

    managed = [t.table for t in _ro.TARGETS]

    async def _unfinished() -> int:
        row = await ch.query_one(
            "SELECT count() AS n FROM system.mutations "
            "WHERE database = 'loom' AND is_done = 0 "
            "AND concat('loom.', table) IN {tables:Array(String)}",
            {"tables": managed},
        )
        return int((row or {}).get("n") or 0)

    async def _drain(attempts: int = 120) -> None:
        import asyncio

        for _ in range(attempts):  # ~60 s at 0.5 s per poll
            if not await _unfinished():
                return
            await asyncio.sleep(0.5)

    try:
        # apply() leaves a table alone while it has
        # unfinished mutations (an earlier test's DELETE may still be
        # running on this shared service) and the reconcile worker retries
        # next pass. Drain first, then apply until every managed table has
        # been altered once — the change detection skips the tables
        # already done, so the total is exactly one ALTER per table.
        await _drain()
        n = 0
        for _ in range(8):
            n += await _ro.apply(
                _LocalSettings(),
                ch=ch,
                namespace_overrides=overrides,
                leader=True,
            )
            if n >= 8:
                break
            await _drain()
        assert n == 7  # one ALTER per managed table

        row = await ch.query_one(
            "SELECT engine_full FROM system.tables WHERE database='loom' AND name='audit_log'",
        )
        engine_full = (row or {}).get("engine_full", "")
        assert "multiIf" in engine_full
        assert f"'{test_org}'" in engine_full
        assert f"'{test_ns}'" in engine_full
        assert "toIntervalDay(90)" in engine_full

        # far-future fallback (no global = 2099) covers other rows
        assert "2099" in engine_full

    finally:
        try:
            for t in (
                "loom.audit_log",
                "loom.agent_events",
                "loom.memory_embeddings",
                "loom.conversations",
                "loom.chunks",
                "loom.documents",
            ):
                try:
                    await ch.execute(f"ALTER TABLE {t} REMOVE TTL")
                except Exception:  # noqa: BLE001 — teardown: a table may lack a TTL; must not raise
                    pass
            try:
                await ch.execute("ALTER TABLE loom.memory REMOVE TTL")
            except Exception:  # noqa: BLE001 — teardown: memory may lack a TTL; must not raise
                pass
        except Exception:  # noqa: BLE001 — teardown must not raise
            pass


def dataclass_compat(cls):
    from dataclasses import dataclass

    return dataclass(cls)


@pytest.mark.asyncio
async def test_blacklist_repo_insert_list_tombstone(
    ch: Any,
    isolated_id: str,
) -> None:
    from server.memory.store import blacklist as blacklist_repo

    org, ns = "itest-org", isolated_id
    pid = str(uuid.uuid4())
    now = datetime.now(UTC).replace(tzinfo=None)
    await blacklist_repo.insert_row(
        {
            "pattern_id": pid,
            "org": org,
            "namespace": ns,
            "pattern": "test-pattern",
            "pattern_type": "substring",
            "scope": "namespace",
            "reason": "itest",
            "created_by": "itest",
            "hit_count": 0,
            "created_at": now,
            "tombstoned_at": None,
        },
        ch=ch,
    )

    rows = await blacklist_repo.list_for_scope(org, ns, ch=ch)
    assert any(str(r["pattern_id"]) == pid for r in rows)

    found = await blacklist_repo.find_active_pattern(pid, org=org, ch=ch)
    assert found is not None
    assert str(found["pattern_id"]) == pid

    await blacklist_repo.tombstone_pattern(pid, now, ch=ch)
    gone = await blacklist_repo.find_active_pattern(pid, org=org, ch=ch)
    assert gone is None


@pytest.mark.asyncio
async def test_grants_repo_insert_list_find(
    ch: Any,
    isolated_id: str,
) -> None:
    from server.identity.store import grants as grants_repo

    org, ns = "itest-org", isolated_id
    subj = f"itest-user-{isolated_id}"
    now = datetime.now(UTC).replace(tzinfo=None)
    await grants_repo.insert_row(
        {
            "org": org,
            "namespace": ns,
            "subject": subj,
            "role": "reader",
            "granted_at": now,
            "granted_by": "itest",
            "revoked_at": None,
            "version": 1,
        },
        ch=ch,
    )

    active = await grants_repo.list_active(org, ns, ch=ch)
    assert any(r["subject"] == subj for r in active)

    one = await grants_repo.find_active_grant(org, ns, subj, ch=ch)
    assert one is not None
    assert one.role == "reader"


@pytest.mark.asyncio
async def test_shared_repo_insert_fetch_promoted(
    ch: Any,
    isolated_id: str,
) -> None:
    from server.memory.store import shared as shared_repo

    org, ns = "itest-org", isolated_id
    key = "itest-shared-key"
    now = datetime.now(UTC).replace(tzinfo=None)
    promo_id = str(uuid.uuid4())
    await shared_repo.insert_shared_row(
        {
            "org": org,
            "namespace": ns,
            "memory_key": key,
            "value": "v1",
            "memory_type": "semantic",
            "confidence": 0.9,
            "promoted_from": [],
            "promotion_event": promo_id,
            "promoted_from_candidate_id": None,
            "promoted_by": "itest",
            "rationale": "itest",
            "promoted_at": now,
            "reversible_until": now,
            "superseded_by": None,
            "tombstoned_at": None,
            "version": 1,
        },
        ch=ch,
    )

    row = await shared_repo.fetch_shared(org, ns, key, ch=ch)
    assert row is not None
    assert row["value"] == "v1"


@pytest.mark.asyncio
async def test_conflicts_allowlist_insert_exists_remove(
    ch: Any,
    isolated_id: str,
) -> None:
    from server.memory.store import conflicts as conflicts_repo

    org, ns = "itest-org", isolated_id
    key_a, key_b = "a-key", "b-key"  # already (min, max)
    now = datetime.now(UTC).replace(tzinfo=None)
    await conflicts_repo.allowlist_insert(
        [
            {
                "org": org,
                "namespace": ns,
                "memory_key_a": key_a,
                "memory_key_b": key_b,
                "allowed_at": now,
                "allowed_by": "itest",
                "reason": "itest",
                "tombstoned_at": None,
            }
        ],
        ch=ch,
    )

    assert await conflicts_repo.allowlist_exists(org, ns, key_a, key_b, ch=ch) is True

    await conflicts_repo.allowlist_remove(
        org,
        ns,
        key_a,
        key_b,
        "itest-remove",
        now,
        ch=ch,
    )
    assert await conflicts_repo.allowlist_exists(org, ns, key_a, key_b, ch=ch) is False


@pytest.mark.asyncio
async def test_learnings_repo_insert_list_find(
    ch: Any,
    isolated_id: str,
) -> None:
    from server.memory.store import learnings as learnings_repo

    org, ns = "itest-org", isolated_id
    cid = str(uuid.uuid4())
    now = datetime.now(UTC).replace(tzinfo=None)
    await learnings_repo.insert_row(
        {
            "candidate_id": cid,
            "org": org,
            "namespace": ns,
            "memory_key": "itest-learning",
            "memory_type": "semantic",
            "proposed_value": "v",
            "supporter_count": 3,
            "confidence_avg": 0.85,
            "supporters": ["a", "b", "c"],
            "rationale": "itest",
            "status": "pending",
            "proposed_at": now,
            "decided_at": None,
            "decided_by": "",
            "decision_reason": "",
            "version": 1,
        },
        ch=ch,
    )

    pending = await learnings_repo.list_by_status(org, ns, "pending", ch=ch)
    assert any(str(r["candidate_id"]) == cid for r in pending)

    one = await learnings_repo.find_candidate(org, ns, cid, ch=ch)
    assert one is not None
    assert one["memory_key"] == "itest-learning"


@pytest.mark.asyncio
async def test_conversation_repo_append_list_thread(
    ch: Any,
    isolated_id: str,
) -> None:
    from server.knowledge.store import conversations as conversation_repo

    org, ns = "itest-org", isolated_id
    agent = "itest-bot"
    thread_id = f"thread-{isolated_id}"
    msg = conversation_repo.MessageInsert(
        org=org,
        namespace=ns,
        agent=agent,
        thread_id=thread_id,
        msg_id=str(uuid.uuid4()),
        role="user",
        content="hello",
        ts=datetime.now(UTC).replace(tzinfo=None),
    )
    await conversation_repo.append(msg, ch=ch)

    msgs = await conversation_repo.list_thread(
        org,
        ns,
        agent,
        thread_id,
        ch=ch,
        limit=10,
    )
    assert len(msgs) >= 1
    assert msgs[-1].content == "hello"

    n = await conversation_repo.count_thread_msgs(org, ns, agent, thread_id, ch=ch)
    assert n >= 1


@pytest.mark.asyncio
async def test_admin_repo_list_tables_includes_core_set(ch: Any) -> None:
    from server.insights.store import admin as admin_repo

    tables = await admin_repo.list_loom_tables(ch=ch)
    s = set(tables)
    for required in (
        "memory",
        "memory_embeddings",
        "audit_log",
        "documents",
        "chunks",
        "namespaces",
    ):
        assert required in s, f"loom.{required} missing from list_loom_tables"


@pytest.mark.asyncio
async def test_erasure_user_id_cascades_to_shared(
    ch: Any,
    isolated_id: str,
) -> None:
    from server.erasure.store import steps_memory as memory_steps

    org = "itest-org"
    ns = isolated_id
    alice = f"alice-{uuid.uuid4().hex[:6]}"
    bob = f"bob-{uuid.uuid4().hex[:6]}"

    for uid, key in [(alice, "alice-pref"), (bob, "bob-pref")]:
        await ch.insert(
            "loom.shared",
            [
                {
                    "org": org,
                    "namespace": ns,
                    "memory_key": key,
                    "value": '"v"',
                    "memory_type": "preference",
                    "confidence": 0.9,
                    "promoted_from": [f"{org}/{ns}/agent-x/{uid}/sess-y/{key}"],
                    "promotion_event": str(uuid.uuid4()),
                    "promoted_by": "itest",
                    "rationale": "itest",
                    "promoted_at": datetime.now(UTC).replace(tzinfo=None),
                    "reversible_until": datetime.now(UTC).replace(tzinfo=None),
                    "superseded_by": None,
                    "tombstoned_at": None,
                    "version": 1,
                }
            ],
        )

    n = await memory_steps.tombstone_shared_by_user(org, alice, ch=ch)
    assert n == 1, f"expected 1 tombstone for alice, got {n}"

    rows = await ch.query_all(
        """
        SELECT memory_key, tombstoned_at
        FROM loom.shared FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
        ORDER BY memory_key
        """,
        params={"org": org, "ns": ns},
    )
    by_key = {r["memory_key"]: r["tombstoned_at"] for r in rows}
    assert by_key["alice-pref"] is not None, "alice's shared row should be tombstoned"
    assert by_key["bob-pref"] is None, "bob's shared row should be intact"


@pytest.mark.asyncio
async def test_embedding_worker_reconciles_shared_stub(
    ch: Any,
    isolated_id: str,
) -> None:
    from server.embedding import get_embedder
    from server.memory.workers.embedding import EmbeddingWorkerConfig, run_pass

    org = "itest-org"
    ns = isolated_id
    memory_key = f"shared-orphan-{uuid.uuid4().hex[:6]}"
    # tz-aware UTC matches the worker's INSERT path (collapses on FINAL)
    now = datetime.now(UTC)
    # stub must use the worker's model_name or rows never collapse on FINAL
    embedder_model = get_embedder().model_name
    # No other worker touches this table: the CI control plane runs with
    # LOOM_BACKGROUND_WORKERS=off, so the run_pass below is the
    # only pass that can embed the stub and `result.embedded` must name it.

    # stub: embedding_dim=0, populated content_excerpt
    await ch.insert(
        "loom.memory_embeddings",
        [
            {
                "org": org,
                "namespace": ns,
                "agent": "_shared",
                "user_id": "-",
                "session_id": "-",
                "memory_key": memory_key,
                "memory_type": "preference",
                "scope": "shared",
                "content_excerpt": "this is the content the worker will embed",
                "embedding_model": embedder_model,
                "embedding_dim": 0,
                "embedding": _wide([]),
                "memory_key_embedding": _wide([]),
                "tombstoned_at": None,
                "created_at": now,
            }
        ],
    )

    pre = await ch.query_one(
        """
        SELECT embedding_dim FROM loom.memory_embeddings FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
          AND memory_key = {k:String} AND scope = 'shared'
        """,
        params={"org": org, "ns": ns, "k": memory_key},
    )
    assert pre is not None and int(pre["embedding_dim"]) == 0

    result = await run_pass(
        EmbeddingWorkerConfig(
            org_filter=org,
            namespace_filter=ns,
            batch_size=10,
        )
    )
    assert any(e.get("memory_key") == memory_key for e in result.embedded), (
        f"worker did not reconcile {memory_key}; result={result!r}"
    )

    # worker's replacement INSERT is async; poll until FINAL surfaces it
    import asyncio as _asyncio

    post = None
    deadline = _asyncio.get_event_loop().time() + 3.0
    while _asyncio.get_event_loop().time() < deadline:
        post = await ch.query_one(
            """
            SELECT embedding_dim FROM loom.memory_embeddings FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND memory_key = {k:String} AND scope = 'shared'
            """,
            params={"org": org, "ns": ns, "k": memory_key},
        )
        if post is not None and int(post["embedding_dim"]) > 0:
            break
        await _asyncio.sleep(0.1)
    assert post is not None
    assert int(post["embedding_dim"]) > 0, (
        f"expected embedding_dim > 0 after worker pass, got {post}"
    )


@pytest.mark.asyncio
async def test_conflicts_check_against_shared_finds_close_peer(
    ch: Any,
    isolated_id: str,
) -> None:
    from server.memory.app import conflicts

    org = "itest-org"
    ns = isolated_id
    base_vec = _wide([0.1, 0.2, 0.3, 0.4])
    close_vec = _wide([0.11, 0.21, 0.29, 0.39])  # cosine distance ≈ 0.0001

    seeded_key = "existing-pref"
    promotion_event = str(uuid.uuid4())
    now = datetime.now(UTC).replace(tzinfo=None)
    await ch.insert(
        "loom.shared",
        [
            {
                "org": org,
                "namespace": ns,
                "memory_key": seeded_key,
                "value": '"existing preference"',
                "memory_type": "preference",
                "confidence": 0.9,
                "promoted_from": [f"{org}/{ns}/agent-x/-/-/{seeded_key}"],
                "promotion_event": promotion_event,
                "promoted_by": "itest",
                "rationale": "itest",
                "promoted_at": now,
                "reversible_until": now,
                "superseded_by": None,
                "tombstoned_at": None,
                "version": 1,
            }
        ],
    )
    await ch.insert(
        "loom.memory_embeddings",
        [
            {
                "org": org,
                "namespace": ns,
                "agent": "_shared",
                "user_id": "-",
                "session_id": "-",
                "memory_key": seeded_key,
                "memory_type": "preference",
                "scope": "shared",
                "content_excerpt": "existing preference",
                "embedding_model": "itest-model",
                "embedding_dim": len(base_vec),
                "embedding": base_vec,
                "memory_key_embedding": _wide([]),
                "tombstoned_at": None,
                "created_at": now,
            }
        ],
    )

    # different memory_key, close content → conflict
    peers = await conflicts.check_against_shared(
        org=org,
        namespace=ns,
        target_key="new-conflicting-pref",
        memory_type="preference",
        embedding=close_vec,
    )
    assert len(peers) >= 1
    assert peers[0]["memory_key"] == seeded_key

    # same memory_key → supersession not conflict; self-key filtered out
    peers_self = await conflicts.check_against_shared(
        org=org,
        namespace=ns,
        target_key=seeded_key,
        memory_type="preference",
        embedding=close_vec,
    )
    assert peers_self == []


@pytest.mark.asyncio
async def test_conflicts_check_against_shared_far_vec_clears(
    ch: Any,
    isolated_id: str,
) -> None:
    from server.memory.app import conflicts

    org = "itest-org"
    ns = isolated_id
    far_vec = _wide([0.99, -0.99, 0.99, -0.99])  # opposite signs → cosine ≈ 1

    peers = await conflicts.check_against_shared(
        org=org,
        namespace=ns,
        target_key="unrelated",
        memory_type="preference",
        embedding=far_vec,
    )
    assert peers == [], "no shared peers should match a far-vector query"


@pytest.fixture(autouse=True)
async def _cleanup_after_test(ch: Any, isolated_id: str) -> Any:
    """Tombstone everything under the test namespace after each test (best-effort)."""
    yield
    org = "itest-org"
    for sql in (
        f"ALTER TABLE loom.memory UPDATE tombstoned_at = now() "
        f"WHERE org='{org}' AND namespace='{isolated_id}'",
        f"ALTER TABLE loom.memory_embeddings UPDATE tombstoned_at = now() "
        f"WHERE org='{org}' AND namespace='{isolated_id}'",
        f"ALTER TABLE loom.documents UPDATE superseded_at = now() "
        f"WHERE org='{org}' AND namespace='{isolated_id}'",
        f"ALTER TABLE loom.chunks UPDATE doc_superseded_at = now() "
        f"WHERE org='{org}' AND namespace='{isolated_id}'",
        f"ALTER TABLE loom.audit_log DELETE WHERE org='{org}' AND namespace='{isolated_id}'",
        f"ALTER TABLE loom.blacklist UPDATE tombstoned_at = now() "
        f"WHERE org='{org}' AND namespace='{isolated_id}'",
        f"ALTER TABLE loom.namespace_grants UPDATE revoked_at = now(), version = version + 1 "
        f"WHERE org='{org}' AND namespace='{isolated_id}'",
        f"ALTER TABLE loom.shared UPDATE tombstoned_at = now(), version = version + 1 "
        f"WHERE org='{org}' AND namespace='{isolated_id}'",
        f"ALTER TABLE loom.conflict_allowlist UPDATE tombstoned_at = now() "
        f"WHERE org='{org}' AND namespace='{isolated_id}'",
        f"ALTER TABLE loom.learnings_queue UPDATE status = 'rejected', version = version + 1 "
        f"WHERE org='{org}' AND namespace='{isolated_id}'",
        f"ALTER TABLE loom.conversations DELETE WHERE org='{org}' AND namespace='{isolated_id}'",
        f"ALTER TABLE loom.conversation_embeddings DELETE "
        f"WHERE org='{org}' AND namespace='{isolated_id}'",
    ):
        try:
            await ch.execute(sql)
        except Exception:  # noqa: BLE001 — teardown: a missing table must not fail cleanup
            pass
