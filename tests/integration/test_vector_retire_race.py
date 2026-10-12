"""An older 'high' write that retires vectors after a newer embedded write landed
must not retire the newer one's. Drives the store functions against live
ClickHouse in the interleaving: A draws its version and inserts its high row,
B lands a newer embedded version (vector + entity vector), then A retires.

Skips cleanly when LOOM_CH_HOST is unset.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("LOOM_CH_HOST"),
    reason="LOOM_CH_HOST not set — integration tests require a live CH",
)

ORG = "itest-org"
AGENT, USER, SESSION, KEY = "itest-bot", "-", "-", "race/fact/k"


@pytest.fixture
async def ch() -> Any:
    from server.core.ch import get_client

    c = get_client()
    await c.connect()
    yield c
    await c.disconnect()


def _memory_row(ns: str, version: int, value: str, sensitivity: str) -> dict[str, Any]:
    from server.memory import store as M

    now = datetime.now(UTC).replace(tzinfo=None)
    return M.build_first_version_row(
        {
            "org": ORG,
            "namespace": ns,
            "agent": AGENT,
            "user_id": USER,
            "session_id": SESSION,
            "memory_key": KEY,
            "memory_type": "semantic",
            "value": json.dumps(value),
            "value_json": json.dumps({"v": value}),
            "content_sha256": hashlib.sha256(value.encode()).hexdigest(),
            "confidence": 1.0,
            "actor": "process:itest",
            "source_class": "asserted",
            "sensitivity": sensitivity,
            "version": version,
        },
        now=now,
    )


async def _embedded_write(ch: Any, ns: str, value: str, entity: str) -> None:
    """One embedded generation: its memory row, then its vector and entity vector,
    stamped the way memory.set / bulk_set stamp them."""
    from server.memory import store as M

    await M.insert_memory_versions([_memory_row(ns, M.next_version(), value, "")], ch=ch)
    emb = M.build_embedding_row(
        org=ORG,
        namespace=ns,
        agent=AGENT,
        user_id=USER,
        session_id=SESSION,
        memory_key=KEY,
        memory_type="semantic",
        scope="private",
        content_excerpt=value,
        embedding_model="fake",
        embedding_dim=3,
        embedding=[0.1, 0.2, 0.3],
        created_at=datetime.now(UTC).replace(tzinfo=None),
    )
    await M.insert_embedding_rows([emb], ch=ch)
    await ch.insert(
        "loom.entity_vectors",
        [
            {
                "org": ORG,
                "namespace": ns,
                "agent": AGENT,
                "user_id": USER,
                "entity_text": entity,
                "entity_embedding": [0.1, 0.2, 0.3],
                "embedding_model": "fake",
                "embedding_dim": 3,
                "memory_key": KEY,
                "tombstoned_at": None,
                "created_at": datetime.now(UTC).replace(tzinfo=None),
            }
        ],
        settings={"async_insert": 0, "wait_for_async_insert": 1},
    )


async def test_a_paused_older_high_write_keeps_the_newer_writes_vectors(ch: Any) -> None:
    from server.memory import store as M

    ns = f"itest-{uuid.uuid4().hex[:12]}"
    try:
        await _embedded_write(ch, ns, "old value", "old-entity")
        previous = SimpleNamespace(
            org=ORG,
            namespace=ns,
            agent=AGENT,
            user_id=USER,
            session_id=SESSION,
            memory_key=KEY,
            memory_type="semantic",
            sensitivity="",
        )

        # A: draws its version, inserts its high row, then pauses.
        v_a = M.next_version()
        await M.insert_memory_versions([_memory_row(ns, v_a, "sk-high", "high")], ch=ch)
        # entity_vectors.created_at is whole seconds: B lands in a later one.
        await asyncio.sleep(1.1)
        await _embedded_write(ch, ns, "newer value", "new-entity")
        # A resumes and retires what it replaced.
        await M.tombstone_vectors_of(previous, version=v_a, ch=ch)

        live = await ch.query_all(
            "SELECT value, sensitivity FROM loom.memory FINAL"
            " WHERE org = {o:String} AND namespace = {n:String} AND tombstoned_at IS NULL",
            params={"o": ORG, "n": ns},
        )
        assert [(json.loads(r["value"]), r["sensitivity"]) for r in live] == [("newer value", "")]
        vectors = await ch.query_all(
            "SELECT content_excerpt FROM loom.memory_embeddings FINAL"
            " WHERE org = {o:String} AND namespace = {n:String} AND tombstoned_at IS NULL",
            params={"o": ORG, "n": ns},
        )
        assert [r["content_excerpt"] for r in vectors] == ["newer value"]
        entities = await ch.query_all(
            "SELECT entity_text FROM loom.entity_vectors FINAL"
            " WHERE org = {o:String} AND namespace = {n:String} AND tombstoned_at IS NULL",
            params={"o": ORG, "n": ns},
        )
        assert [r["entity_text"] for r in entities] == ["new-entity"]
    finally:
        for table in ("memory", "memory_embeddings", "entity_vectors"):
            await ch.execute(
                f"DELETE FROM loom.{table} WHERE org = {{o:String}} AND namespace = {{n:String}}",
                params={"o": ORG, "n": ns},
            )


class _PauseAfterRead:
    """The store's client, with `on_read` run once after the first read: a delayed
    metadata re-emit that read the live row, then sat while another write landed."""

    def __init__(self, ch: Any, on_read: Any) -> None:
        self._ch, self._on_read = ch, on_read

    def __getattr__(self, name: str) -> Any:
        return getattr(self._ch, name)

    async def query_all(self, sql: str, params: Any = None) -> Any:
        rows = await self._ch.query_all(sql, params=params)
        if self._on_read is not None:
            hook, self._on_read = self._on_read, None
            await hook()
        return rows


async def test_a_metadata_reemit_paused_across_a_high_set_does_not_restore_the_vector(
    ch: Any, control_plane_url: str
) -> None:
    import httpx

    from server.auth.jwt import mint_token
    from server.memory import store as M

    ns = f"itest-{uuid.uuid4().hex[:12]}"
    key = f"vault_reemit_{uuid.uuid4().hex[:6]}"
    secret = "sk-proj-" + uuid.uuid4().hex + uuid.uuid4().hex[:8]  # labelled 'high'
    plain = f"the {key} staging vault is rotated every friday by the platform team"
    token = mint_token(
        subject="reemit-race", org=ORG, namespaces=[ns], roles=["writer"], ttl_seconds=600
    )
    ident = {"org": ORG, "namespace": ns, "agent": AGENT}

    async def post(http: httpx.AsyncClient, op: str, body: dict[str, Any]) -> Any:
        r = await http.post(f"/v1/{op}", json={**ident, **body})
        assert r.status_code == 200, r.text
        return r.json()

    async def live_vectors() -> int:
        row = await ch.query_one(
            "SELECT count() AS n FROM loom.memory_embeddings FINAL WHERE org = {o:String}"
            " AND namespace = {n:String} AND memory_key = {k:String} AND tombstoned_at IS NULL",
            params={"o": ORG, "n": ns, "k": key},
        )
        return int(row["n"])

    async def wait_for(n: int) -> None:
        for _ in range(60):
            if await live_vectors() == n:
                return
            await asyncio.sleep(0.5)
        assert await live_vectors() == n

    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(base_url=control_plane_url, headers=headers, timeout=30) as http:
        try:
            await post(http, "memory.set", {"memory_key": key, "value": plain})
            await wait_for(1)

            async def high_set_lands() -> None:
                await post(http, "memory.set", {"memory_key": key, "value": f"token {secret}"})
                await wait_for(0)

            # The verification mirror reads the live vector, the high set retires it,
            # then the mirror's copy is written.
            await M.reemit_embedding_rows_for_key(
                ORG, ns, AGENT, "-", "-", key, ch=_PauseAfterRead(ch, high_set_lands), verified=1
            )
            assert await live_vectors() == 0
            out = await post(
                http,
                "memory.search",
                {"query": "staging vault rotated friday platform team", "top_k": 5, "fresh": True},
            )
            assert secret not in json.dumps(out)
            assert key not in [h.get("memory_key") for h in out.get("results", [])]
        finally:
            for table in ("memory", "memory_embeddings", "entity_vectors"):
                await ch.execute(
                    f"DELETE FROM loom.{table} WHERE org = {{o:String}} AND namespace = {{n:String}}",
                    params={"o": ORG, "n": ns},
                )


async def _memory_row_of(ch: Any, ns: str, key: str) -> Any:
    from server.identity.domain.read_scope import WHOLE_NAMESPACE
    from server.memory import store as M

    row = await ch.query_one(
        "SELECT toString(memory_id) AS id FROM loom.memory FINAL"
        " WHERE org = {o:String} AND namespace = {n:String} AND memory_key = {k:String}",
        params={"o": ORG, "n": ns, "k": key},
    )
    return await M.get_by_id(row["id"], org=ORG, ch=ch, fresh=True, scope=WHOLE_NAMESPACE)


async def _expire_and_anonymize(ch: Any, ns: str, key: str) -> None:
    from server.memory import store as M
    from server.memory.app import expiry

    existing = await _memory_row_of(ch, ns, key)
    past = datetime.now(UTC).replace(tzinfo=None, microsecond=0).replace(year=2025)
    await M.insert_memory_versions(
        [
            M.build_memory_version_row(
                existing, version=M.next_version(), expires_at=past, on_expire="anonymize"
            )
        ],
        ch=ch,
    )
    res = await expiry.run_pass(expiry.ExpiryConfig(org_filter=ORG, namespace_filter=ns))
    assert res.anonymized == 1 and res.failures == [], res.as_dict()


async def _orphan_and_reconcile(ch: Any, ns: str, key: str, monkeypatch: Any) -> None:
    """The memory row is tombstoned without its mirror; the reconcile heals it."""
    from server.memory import store as M
    from server.memory.workers import orphan_vector_reconcile
    from server.settings import settings

    existing = await _memory_row_of(ch, ns, key)
    now = datetime.now(UTC).replace(tzinfo=None)
    await M.insert_memory_versions(
        [M.build_memory_version_row(existing, version=M.next_version(), tombstoned_at=now)],
        ch=ch,
    )
    monkeypatch.setattr(settings, "orphan_vector_reconcile_grace_seconds", 0)
    await asyncio.sleep(1.1)  # the scan takes vectors created before now()
    res = await orphan_vector_reconcile.run_pass(ch=ch)
    assert res.errors == [] and res.embeddings_healed >= 1


@pytest.mark.parametrize(
    "retire", ["memory.delete", "sensitivity_sweep", "expiry_anonymize", "orphan_reconcile"]
)
async def test_a_metadata_reemit_paused_across_a_retire_does_not_restore_the_vector(
    ch: Any, control_plane_url: str, retire: str, monkeypatch: Any
) -> None:
    """memory.delete's mirror, the sensitivity sweep's high retire, the expiry
    anonymize mirror and the orphan reconcile land between a verify re-emit's
    read and its write: each retire is stamped above read + 1 (its memory row's
    version, or one drawn before its scan), so the late copy does not win."""
    import httpx

    from server.auth.jwt import mint_token
    from server.memory import store as M
    from server.memory.app import sensitivity_sweep

    ns = f"itest-{uuid.uuid4().hex[:12]}"
    key = f"retire_reemit_{uuid.uuid4().hex[:6]}"
    token = mint_token(
        subject="reemit-race", org=ORG, namespaces=[ns], roles=["writer"], ttl_seconds=600
    )
    ident = {"org": ORG, "namespace": ns, "agent": AGENT}

    async def live_vectors() -> int:
        row = await ch.query_one(
            "SELECT count() AS n FROM loom.memory_embeddings FINAL WHERE org = {o:String}"
            " AND namespace = {n:String} AND memory_key = {k:String} AND tombstoned_at IS NULL",
            params={"o": ORG, "n": ns, "k": key},
        )
        return int(row["n"])

    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(base_url=control_plane_url, headers=headers, timeout=30) as http:
        try:
            r = await http.post(
                "/v1/memory.set",
                json={**ident, "memory_key": key, "value": f"the {key} vault rotates on fridays"},
            )
            assert r.status_code == 200, r.text
            for _ in range(60):
                if await live_vectors() == 1:
                    break
                await asyncio.sleep(0.5)
            assert await live_vectors() == 1

            async def retire_lands() -> None:
                if retire == "memory.delete":
                    r = await http.post("/v1/memory.delete", json={**ident, "memory_key": key})
                    assert r.status_code == 200 and r.json()["tombstoned"], r.text
                elif retire == "expiry_anonymize":
                    await _expire_and_anonymize(ch, ns, key)
                elif retire == "orphan_reconcile":
                    await _orphan_and_reconcile(ch, ns, key, monkeypatch)
                else:
                    row = await ch.query_one(
                        "SELECT toString(memory_id) AS id FROM loom.memory FINAL"
                        " WHERE org = {o:String} AND namespace = {n:String}"
                        " AND memory_key = {k:String}",
                        params={"o": ORG, "n": ns, "k": key},
                    )
                    cand = sensitivity_sweep.SweepCandidate(
                        row["id"], ORG, ns, AGENT, "-", "-", key, "high", ("credential",)
                    )
                    res = await sensitivity_sweep.apply([cand], ch=ch)
                    assert res.errors == [] and res.embeddings_tombstoned == 1
                assert await live_vectors() == 0

            await M.reemit_embedding_rows_for_key(
                ORG, ns, AGENT, "-", "-", key, ch=_PauseAfterRead(ch, retire_lands), verified=1
            )
            assert await live_vectors() == 0
        finally:
            for table in ("memory", "memory_embeddings", "entity_vectors"):
                await ch.execute(
                    f"DELETE FROM loom.{table} WHERE org = {{o:String}} AND namespace = {{n:String}}",
                    params={"o": ORG, "n": ns},
                )
