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


async def _memory_only_row(ch: Any, ns: str, key: str, value: str) -> None:
    """A live memory row with no vector of the served model."""
    from server.memory import store as M

    row = _memory_row(ns, M.next_version(), value, "")
    await M.insert_memory_versions([{**row, "memory_key": key}], ch=ch)


class _PausedEmbedder:
    """The served embedder, with the first call whose text holds `marker` held at
    `gate` until released: an older write paused inside its embedding work."""

    def __init__(self, inner: Any, marker: str) -> None:
        self._inner, self._marker = inner, marker
        self.entered, self.gate = asyncio.Event(), asyncio.Event()
        self.model_name, self.dim = inner.model_name, inner.dim

    async def _hold(self, texts: list[str]) -> None:
        if not self.entered.is_set() and any(self._marker in t for t in texts):
            self.entered.set()
            await self.gate.wait()

    async def embed(self, text: str) -> list[float]:
        await self._hold([text])
        return await self._inner.embed(text)

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        await self._hold(list(texts))
        return await self._inner.embed_many(texts)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


@pytest.mark.parametrize(
    ("older", "prior_vector", "newer"),
    [
        ("memory.set", False, "write"),
        ("memory.set", True, "write"),
        ("memory.update", True, "write"),
        ("bulk_set", False, "write"),
        # the key has a memory row but no vector on the served model (written
        # before a model change, say): only the sweep's fence covers it
        ("memory.update", False, "sensitivity_sweep"),
    ],
)
async def test_an_older_writes_late_vector_stays_under_a_newer_high_write(
    ch: Any, control_plane_url: str, monkeypatch: Any, older: str, prior_vector: bool, newer: str
) -> None:
    """An older plain write draws its version, then pauses in its embed call; a newer
    write labelled 'high' commits; the older one resumes. Its vector carries its own
    memory version, below the high write's fence, so search stays empty, with or
    without a vector for the key before."""
    import httpx

    from server.auth.jwt import mint_token
    from server.embedding import embedder as embedder_mod
    from server.memory.api.models_bulk import MemoryBulkSetInput, _BulkSetEntry
    from server.memory.api.models_crud import MemorySetInput, MemoryUpdateInput
    from server.memory.app import bulk_set, update, write

    ns = f"itest-{uuid.uuid4().hex[:12]}"
    key = f"late_vector_{uuid.uuid4().hex[:6]}"
    marker = f"pauseme{uuid.uuid4().hex[:6]}"
    plain = f"the {key} staging vault {marker} is rotated every friday by the platform team"
    secret = "sk-proj-" + uuid.uuid4().hex + uuid.uuid4().hex[:8]  # labelled 'high'
    ident = {"org": ORG, "namespace": ns, "agent": AGENT}

    paused = _PausedEmbedder(embedder_mod.get_embedder(), marker)
    monkeypatch.setattr(embedder_mod, "_cached", paused)  # what every get_embedder() returns

    async def live_vectors() -> int:
        row = await ch.query_one(
            "SELECT count() AS n FROM loom.memory_embeddings FINAL WHERE org = {o:String}"
            " AND namespace = {n:String} AND memory_key = {k:String}"
            " AND tombstoned_at IS NULL AND embedding_dim > 0",
            params={"o": ORG, "n": ns, "k": key},
        )
        return int(row["n"])

    def older_write() -> Any:
        if older == "memory.set":
            return write.handle_memory_set(MemorySetInput(**ident, memory_key=key, value=plain))
        if older == "memory.update":
            return update.handle_memory_update(
                MemoryUpdateInput(org=ORG, namespace=ns, memory_key=key, value=plain)
            )
        return bulk_set.handle_memory_bulk_set(
            MemoryBulkSetInput(**ident, entries=[_BulkSetEntry(memory_key=key, value=plain)])
        )

    async def sweep_relabels_high() -> None:
        from server.memory.app import sensitivity_sweep

        row = await ch.query_one(
            "SELECT toString(memory_id) AS id FROM loom.memory FINAL WHERE org = {o:String}"
            " AND namespace = {n:String} AND memory_key = {k:String}",
            params={"o": ORG, "n": ns, "k": key},
        )
        cand = sensitivity_sweep.SweepCandidate(
            row["id"], ORG, ns, AGENT, "-", "-", key, "high", ("credential",)
        )
        res = await sensitivity_sweep.apply([cand], ch=ch)
        assert res.errors == [] and res.high == 1

    def newer_high_write() -> Any:
        if newer == "sensitivity_sweep":
            return sweep_relabels_high()
        if older == "bulk_set":
            entry = _BulkSetEntry(memory_key=key, value=f"token {secret}")
            return bulk_set.handle_memory_bulk_set(MemoryBulkSetInput(**ident, entries=[entry]))
        return write.handle_memory_set(
            MemorySetInput(**ident, memory_key=key, value=f"token {secret}")
        )

    token = mint_token(
        subject="late-vector", org=ORG, namespaces=[ns], roles=["writer"], ttl_seconds=600
    )
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(base_url=control_plane_url, headers=headers, timeout=30) as http:
        try:
            first = f"the {key} vault was rotated by hand on mondays"
            if prior_vector:
                await write.handle_memory_set(MemorySetInput(**ident, memory_key=key, value=first))
                assert await live_vectors() == 1
            elif older == "memory.update":
                await _memory_only_row(ch, ns, key, first)

            task = asyncio.create_task(older_write())
            await asyncio.wait_for(paused.entered.wait(), 30)
            await newer_high_write()
            assert await live_vectors() == 0
            paused.gate.set()
            await asyncio.wait_for(task, 30)

            row = await ch.query_one(
                "SELECT sensitivity FROM loom.memory FINAL WHERE org = {o:String}"
                " AND namespace = {n:String} AND memory_key = {k:String}",
                params={"o": ORG, "n": ns, "k": key},
            )
            assert row["sensitivity"] == "high"
            assert await live_vectors() == 0
            r = await http.post(
                "/v1/memory.search",
                json={
                    **ident,
                    "query": "staging vault rotated friday platform team",
                    "top_k": 5,
                    "fresh": True,
                },
            )
            assert r.status_code == 200, r.text
            assert marker not in r.text and secret not in r.text
            assert key not in [h.get("memory_key") for h in r.json().get("results", [])]
        finally:
            paused.gate.set()
            for table in ("memory", "memory_embeddings", "entity_vectors"):
                await ch.execute(
                    f"DELETE FROM loom.{table} WHERE org = {{o:String}} AND namespace = {{n:String}}",
                    params={"o": ORG, "n": ns},
                )


async def test_a_rollback_off_a_high_version_is_embedded_again(ch: Any) -> None:
    """set plain, set high (its fence), roll back to the plain version: the reinstated
    stub sits above the fence and the embed worker drains it, so the key is searchable
    again (the stub queue tests IS NULL inside argMax, which skips NULLs)."""
    from server.identity.api.models import NamespaceRollbackInput
    from server.identity.app.namespace_rollback import handle_namespace_rollback
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app import write
    from server.memory.workers.embedding import EmbeddingWorkerConfig, run_pass

    ns = f"itest-{uuid.uuid4().hex[:12]}"
    key = f"rollback_drain_{uuid.uuid4().hex[:6]}"
    ident = {"org": ORG, "namespace": ns, "agent": AGENT}
    secret = "sk-proj-" + uuid.uuid4().hex + uuid.uuid4().hex[:8]

    async def live_vectors() -> int:
        row = await ch.query_one(
            "SELECT count() AS n FROM loom.memory_embeddings FINAL WHERE org = {o:String}"
            " AND namespace = {n:String} AND memory_key = {k:String}"
            " AND tombstoned_at IS NULL AND embedding_dim > 0",
            params={"o": ORG, "n": ns, "k": key},
        )
        return int(row["n"])

    try:
        plain = f"the {key} vault is audited each quarter by the security office"
        await write.handle_memory_set(MemorySetInput(**ident, memory_key=key, value=plain))
        await asyncio.sleep(1.1)
        as_of = datetime.now(UTC)
        await asyncio.sleep(1.1)
        await write.handle_memory_set(
            MemorySetInput(**ident, memory_key=key, value=f"root token {secret}")
        )
        assert await live_vectors() == 0
        from server.auth.context import set_current_auth
        from server.auth.jwt import AuthInfo
        from server.operations.base import Role

        set_current_auth(  # the rollback names its caller in the audit row
            AuthInfo(
                subject="rollback-drain",
                org=ORG,
                namespaces=(ns,),
                roles=(Role.ADMIN,),
                is_service_account=False,
            )
        )
        try:
            await handle_namespace_rollback(
                NamespaceRollbackInput(
                    org=ORG, namespace=ns, as_of=as_of, dry_run=False, memory_keys=[key]
                )
            )
        finally:
            set_current_auth(None)
        await run_pass(EmbeddingWorkerConfig(org_filter=ORG, namespace_filter=ns))
        assert await live_vectors() == 1
        row = await ch.query_one(
            "SELECT sensitivity FROM loom.memory FINAL WHERE org = {o:String}"
            " AND namespace = {n:String} AND memory_key = {k:String}",
            params={"o": ORG, "n": ns, "k": key},
        )
        assert row["sensitivity"] != "high"
    finally:
        for table in ("memory", "memory_embeddings", "entity_vectors"):
            await ch.execute(
                f"DELETE FROM loom.{table} WHERE org = {{o:String}} AND namespace = {{n:String}}",
                params={"o": ORG, "n": ns},
            )


@pytest.mark.parametrize("made_high_by", ["memory.update", "sensitivity_sweep"])
async def test_a_long_value_made_high_keeps_no_live_chunks(
    ch: Any, control_plane_url: str, made_high_by: str
) -> None:
    """A long value's verbatim chunks are a search leg of their own (read with no
    join back to loom.memory): a key made 'high' tombstones them with its vectors."""
    import httpx

    from server.auth.jwt import mint_token
    from server.memory.api.models_crud import MemorySetInput, MemoryUpdateInput
    from server.memory.app import sensitivity_sweep, update, write
    from server.memory.domain.excerpt import EXCERPT_LIMIT

    ns = f"itest-{uuid.uuid4().hex[:12]}"
    key = f"long_high_{uuid.uuid4().hex[:6]}"
    ident = {"org": ORG, "namespace": ns, "agent": AGENT}
    secret = "sk-proj-" + uuid.uuid4().hex + uuid.uuid4().hex[:8]
    sentence = f"The {key} runbook says the staging vault is rotated every friday. "
    long_plain = sentence * (EXCERPT_LIMIT // len(sentence) + 4)

    async def live_chunks() -> int:
        row = await ch.query_one(
            "SELECT count() AS n FROM loom.memory_chunks FINAL WHERE org = {o:String}"
            " AND namespace = {n:String} AND parent_memory_key = {k:String}"
            " AND tombstoned_at IS NULL",
            params={"o": ORG, "n": ns, "k": key},
        )
        return int(row["n"])

    token = mint_token(
        subject="long-high", org=ORG, namespaces=[ns], roles=["writer"], ttl_seconds=600
    )
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(base_url=control_plane_url, headers=headers, timeout=30) as http:
        try:
            await write.handle_memory_set(MemorySetInput(**ident, memory_key=key, value=long_plain))
            assert await live_chunks() > 0
            if made_high_by == "memory.update":
                await update.handle_memory_update(
                    MemoryUpdateInput(
                        org=ORG, namespace=ns, memory_key=key, value=f"{long_plain} {secret}"
                    )
                )
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
                assert res.errors == [] and res.high == 1
            assert await live_chunks() == 0
            r = await http.post(
                "/v1/memory.search",
                json={**ident, "query": "staging vault rotated every friday", "top_k": 5},
            )
            assert r.status_code == 200, r.text
            assert key not in [h.get("memory_key") for h in r.json().get("results", [])]
        finally:
            for table in ("memory", "memory_embeddings", "entity_vectors", "memory_chunks"):
                await ch.execute(
                    f"DELETE FROM loom.{table} WHERE org = {{o:String}} AND namespace = {{n:String}}",
                    params={"o": ORG, "n": ns},
                )


async def test_a_reindex_paused_across_a_high_relabel_leaves_no_vector(
    ch: Any, monkeypatch: Any
) -> None:
    """memory.reindex reads a plain row, the sweep relabels it high (retire and
    fence), then the reindex writes: its row carries the memory row's version + 1,
    below the fence, so no vector is live under the high row."""
    from server.embedding import embedder as embedder_mod
    from server.memory.api.models_admin import MemoryReindexInput
    from server.memory.app import ops, sensitivity_sweep

    ns = f"itest-{uuid.uuid4().hex[:12]}"
    key = f"reindex_high_{uuid.uuid4().hex[:6]}"
    marker = f"pauseme{uuid.uuid4().hex[:6]}"
    value = f"the {key} staging vault {marker} is rotated every friday"

    async def live_vectors() -> int:
        row = await ch.query_one(
            "SELECT count() AS n FROM loom.memory_embeddings FINAL WHERE org = {o:String}"
            " AND namespace = {n:String} AND memory_key = {k:String}"
            " AND tombstoned_at IS NULL AND embedding_dim > 0",
            params={"o": ORG, "n": ns, "k": key},
        )
        return int(row["n"])

    try:
        await _memory_only_row(ch, ns, key, value)  # no vector yet: reindex picks it up
        paused = _PausedEmbedder(embedder_mod.get_embedder(), marker)
        monkeypatch.setattr(embedder_mod, "_cached", paused)
        task = asyncio.create_task(
            ops.handle_memory_reindex(MemoryReindexInput(org=ORG, namespace=ns))
        )
        await asyncio.wait_for(paused.entered.wait(), 30)
        row = await ch.query_one(
            "SELECT toString(memory_id) AS id FROM loom.memory FINAL WHERE org = {o:String}"
            " AND namespace = {n:String} AND memory_key = {k:String}",
            params={"o": ORG, "n": ns, "k": key},
        )
        cand = sensitivity_sweep.SweepCandidate(
            row["id"], ORG, ns, AGENT, "-", "-", key, "high", ("credential",)
        )
        res = await sensitivity_sweep.apply([cand], ch=ch)
        assert res.errors == [] and res.high == 1
        paused.gate.set()
        out = await asyncio.wait_for(task, 30)
        assert out.processed == 1
        assert await live_vectors() == 0
    finally:
        for table in ("memory", "memory_embeddings", "entity_vectors"):
            await ch.execute(
                f"DELETE FROM loom.{table} WHERE org = {{o:String}} AND namespace = {{n:String}}",
                params={"o": ORG, "n": ns},
            )


async def test_a_reindex_lands_above_a_newer_row_of_its_model(ch: Any) -> None:
    """A same-model row above the memory row's version (the embed worker's stub
    tombstone on a model change, an embedding-only re-emit) must not outrank the
    reindexed vector."""
    from server.embedding import get_embedder
    from server.memory import store as M
    from server.memory.api.models_admin import MemoryReindexInput
    from server.memory.app import ops

    ns = f"itest-{uuid.uuid4().hex[:12]}"
    key = f"reindex_above_{uuid.uuid4().hex[:6]}"
    try:
        await _memory_only_row(ch, ns, key, f"the {key} vault is rotated every friday")
        mem = await ch.query_one(
            "SELECT max(version) AS v FROM loom.memory WHERE org = {o:String}"
            " AND namespace = {n:String} AND memory_key = {k:String}",
            params={"o": ORG, "n": ns, "k": key},
        )
        stale = M.build_embedding_row(
            org=ORG,
            namespace=ns,
            agent=AGENT,
            user_id=USER,
            session_id=SESSION,
            memory_key=key,
            scope="private",
            embedding_model=get_embedder().model_name,
            embedding_dim=0,
            tombstoned_at=datetime.now(UTC).replace(tzinfo=None),
            created_at=datetime.now(UTC).replace(tzinfo=None),
            version=int(mem["v"]) + 5,
        )
        await M.insert_embedding_rows([stale], ch=ch)
        out = await ops.handle_memory_reindex(MemoryReindexInput(org=ORG, namespace=ns))
        assert out.processed == 1
        row = await ch.query_one(
            "SELECT count() AS n FROM loom.memory_embeddings FINAL WHERE org = {o:String}"
            " AND namespace = {n:String} AND memory_key = {k:String}"
            " AND tombstoned_at IS NULL AND embedding_dim > 0",
            params={"o": ORG, "n": ns, "k": key},
        )
        assert int(row["n"]) == 1
    finally:
        for table in ("memory", "memory_embeddings"):
            await ch.execute(
                f"DELETE FROM loom.{table} WHERE org = {{o:String}} AND namespace = {{n:String}}",
                params={"o": ORG, "n": ns},
            )


async def test_chunks_finishing_after_a_newer_high_write_are_not_served(
    ch: Any, control_plane_url: str, monkeypatch: Any
) -> None:
    """A long memory.set pauses while its chunks embed; a newer write labels the key
    'high' and retires the chunks there are; the older write's chunks then land. It
    re-reads the head after its insert and retires them, so search serves nothing."""
    import httpx

    from server.auth.jwt import mint_token
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app import set_chunks, write
    from server.memory.domain.excerpt import EXCERPT_LIMIT

    ns = f"itest-{uuid.uuid4().hex[:12]}"
    key = f"late_chunks_{uuid.uuid4().hex[:6]}"
    ident = {"org": ORG, "namespace": ns, "agent": AGENT}
    secret = "sk-proj-" + uuid.uuid4().hex + uuid.uuid4().hex[:8]
    sentence = f"The {key} runbook says the staging vault is rotated every friday. "
    long_plain = sentence * (EXCERPT_LIMIT // len(sentence) + 4)

    entered, gate = asyncio.Event(), asyncio.Event()
    real_embed_chunks = set_chunks._embed_chunks

    async def paused_embed_chunks(embedder: Any, chunks: list[str]) -> list[list[float]]:
        entered.set()
        await gate.wait()
        return await real_embed_chunks(embedder, chunks)

    monkeypatch.setattr(set_chunks, "_embed_chunks", paused_embed_chunks)

    async def live_chunks() -> int:
        row = await ch.query_one(
            "SELECT count() AS n FROM loom.memory_chunks FINAL WHERE org = {o:String}"
            " AND namespace = {n:String} AND parent_memory_key = {k:String}"
            " AND tombstoned_at IS NULL",
            params={"o": ORG, "n": ns, "k": key},
        )
        return int(row["n"])

    token = mint_token(
        subject="late-chunks", org=ORG, namespaces=[ns], roles=["writer"], ttl_seconds=600
    )
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(base_url=control_plane_url, headers=headers, timeout=30) as http:
        try:
            task = asyncio.create_task(
                write.handle_memory_set(MemorySetInput(**ident, memory_key=key, value=long_plain))
            )
            await asyncio.wait_for(entered.wait(), 30)
            await write.handle_memory_set(
                MemorySetInput(**ident, memory_key=key, value=f"root token {secret}")
            )
            gate.set()
            await asyncio.wait_for(task, 30)
            head = await ch.query_one(
                "SELECT sensitivity FROM loom.memory FINAL WHERE org = {o:String}"
                " AND namespace = {n:String} AND memory_key = {k:String}",
                params={"o": ORG, "n": ns, "k": key},
            )
            assert head["sensitivity"] == "high"
            assert await live_chunks() == 0
            r = await http.post(
                "/v1/memory.search",
                json={**ident, "query": "staging vault rotated every friday", "top_k": 5},
            )
            assert r.status_code == 200, r.text
            assert "runbook" not in r.text
            assert key not in [h.get("memory_key") for h in r.json().get("results", [])]
        finally:
            gate.set()
            for table in ("memory", "memory_embeddings", "entity_vectors", "memory_chunks"):
                await ch.execute(
                    f"DELETE FROM loom.{table} WHERE org = {{o:String}} AND namespace = {{n:String}}",
                    params={"o": ORG, "n": ns},
                )


async def test_a_deleted_memory_restores_searchable(ch: Any) -> None:
    """set, delete, restore: the restored key has a live vector again (the restore's
    embeddings read once failed on every call with ILLEGAL_AGGREGATION)."""
    from server.memory.api.models_crud import MemoryDeleteInput, MemoryRestoreInput, MemorySetInput
    from server.memory.app import handlers, restore, write

    ns = f"itest-{uuid.uuid4().hex[:12]}"
    key = f"restore_live_{uuid.uuid4().hex[:6]}"
    ident = {"org": ORG, "namespace": ns, "agent": AGENT}

    async def live_vectors() -> int:
        row = await ch.query_one(
            "SELECT count() AS n FROM loom.memory_embeddings FINAL WHERE org = {o:String}"
            " AND namespace = {n:String} AND memory_key = {k:String}"
            " AND tombstoned_at IS NULL AND embedding_dim > 0",
            params={"o": ORG, "n": ns, "k": key},
        )
        return int(row["n"])

    try:
        value = f"the {key} vault is audited each quarter"
        await write.handle_memory_set(MemorySetInput(**ident, memory_key=key, value=value))
        assert await live_vectors() == 1
        out = await handlers.handle_memory_delete(MemoryDeleteInput(**ident, memory_key=key))
        assert out.tombstoned and await live_vectors() == 0
        res = await restore.handle_memory_restore(MemoryRestoreInput(**ident, memory_key=key))
        assert res.restored
        assert await live_vectors() == 1
    finally:
        for table in ("memory", "memory_embeddings", "entity_vectors"):
            await ch.execute(
                f"DELETE FROM loom.{table} WHERE org = {{o:String}} AND namespace = {{n:String}}",
                params={"o": ORG, "n": ns},
            )


class _ProxyBeforeMaxRead:
    """The client, running `hook` once just before reindex's max(version) read."""

    def __init__(self, inner: Any, hook: Any) -> None:
        self._inner, self._hook, self.fired = inner, hook, False

    async def query_all(self, sql: str, *a: Any, **kw: Any) -> Any:
        if "max(version) AS ev" in sql and not self.fired:
            self.fired = True
            await self._hook()
        return await self._inner.query_all(sql, *a, **kw)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


async def test_a_high_relabel_between_the_reindex_reads_leaves_no_vector(
    ch: Any, monkeypatch: Any
) -> None:
    """The sweep relabels the key high between reindex's row read and its max(version)
    read: the max sees the fence, so the reindexed row lands above it; the
    post-insert head re-check retires it."""
    import server.core.ch as ch_mod
    from server.memory.api.models_admin import MemoryReindexInput
    from server.memory.app import ops, sensitivity_sweep

    ns = f"itest-{uuid.uuid4().hex[:12]}"
    key = f"r36_{uuid.uuid4().hex[:6]}"
    value = f"the {key} vault password is hunter2"

    async def relabel() -> None:
        row = await ch.query_one(
            "SELECT toString(memory_id) AS id FROM loom.memory FINAL WHERE org = {o:String}"
            " AND namespace = {n:String} AND memory_key = {k:String}",
            params={"o": ORG, "n": ns, "k": key},
        )
        cand = sensitivity_sweep.SweepCandidate(
            row["id"], ORG, ns, AGENT, "-", "-", key, "high", ("credential",)
        )
        res = await sensitivity_sweep.apply([cand], ch=ch)
        assert res.errors == [] and res.high == 1

    try:
        await _memory_only_row(ch, ns, key, value)
        proxy = _ProxyBeforeMaxRead(ch, relabel)
        monkeypatch.setattr(ch_mod, "get_client", lambda: proxy)
        out = await ops.handle_memory_reindex(MemoryReindexInput(org=ORG, namespace=ns))
        assert proxy.fired
        live = await ch.query_one(
            "SELECT count() AS n FROM loom.memory_embeddings FINAL WHERE org = {o:String}"
            " AND namespace = {n:String} AND memory_key = {k:String}"
            " AND tombstoned_at IS NULL AND embedding_dim > 0",
            params={"o": ORG, "n": ns, "k": key},
        )
        head = await ch.query_one(
            "SELECT sensitivity FROM loom.memory FINAL WHERE org = {o:String}"
            " AND namespace = {n:String} AND memory_key = {k:String}",
            params={"o": ORG, "n": ns, "k": key},
        )
        assert out.processed == 1
        assert head["sensitivity"] == "high"
        assert int(live["n"]) == 0, "live vector under a high head"
    finally:
        for table in ("memory", "memory_embeddings"):
            await ch.execute(
                f"DELETE FROM loom.{table} WHERE org = {{o:String}} AND namespace = {{n:String}}",
                params={"o": ORG, "n": ns},
            )


async def test_chunks_landing_between_the_sweeps_fence_and_its_relabel_are_retired(
    ch: Any, monkeypatch: Any
) -> None:
    """The sweep fences (and retires chunks) before it relabels; a long set's chunks
    land in between and see an unlabelled head. The sweep retires the key's chunks
    again once the label is in."""
    import server.memory.store as memory_repo
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app import sensitivity_sweep, set_chunks, write
    from server.memory.domain.excerpt import EXCERPT_LIMIT
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_sensitivity_autodetect", False)
    ns = f"itest-{uuid.uuid4().hex[:12]}"
    key = f"r36c_{uuid.uuid4().hex[:6]}"
    ident = {"org": ORG, "namespace": ns, "agent": AGENT}
    secret = "sk-proj-" + uuid.uuid4().hex + uuid.uuid4().hex[:8]
    sentence = f"The {key} root token is {secret} keep it safe. "
    long_val = sentence * (EXCERPT_LIMIT // len(sentence) + 4)

    c_entered, c_gate = asyncio.Event(), asyncio.Event()
    s_entered, s_gate = asyncio.Event(), asyncio.Event()
    real_embed_chunks = set_chunks._embed_chunks
    real_wv = memory_repo.write_versions_by_ids

    async def paused_embed_chunks(embedder: Any, chunks: list[str]) -> list[list[float]]:
        c_entered.set()
        await c_gate.wait()
        return await real_embed_chunks(embedder, chunks)

    async def paused_wv(*a: Any, **kw: Any) -> int:
        s_entered.set()
        await s_gate.wait()
        return await real_wv(*a, **kw)

    monkeypatch.setattr(set_chunks, "_embed_chunks", paused_embed_chunks)
    monkeypatch.setattr(memory_repo, "write_versions_by_ids", paused_wv)
    try:
        task = asyncio.create_task(
            write.handle_memory_set(MemorySetInput(**ident, memory_key=key, value=long_val))
        )
        await asyncio.wait_for(c_entered.wait(), 30)
        row = await ch.query_one(
            "SELECT toString(memory_id) AS id, sensitivity FROM loom.memory FINAL"
            " WHERE org = {o:String} AND namespace = {n:String} AND memory_key = {k:String}",
            params={"o": ORG, "n": ns, "k": key},
        )
        assert row["sensitivity"] == ""
        cand = sensitivity_sweep.SweepCandidate(
            row["id"], ORG, ns, AGENT, "-", "-", key, "high", ("credential",)
        )
        sweep = asyncio.create_task(sensitivity_sweep.apply([cand], ch=ch))
        await asyncio.wait_for(s_entered.wait(), 30)  # fenced, not yet relabelled
        c_gate.set()
        await asyncio.wait_for(task, 30)
        s_gate.set()
        res = await asyncio.wait_for(sweep, 30)
        assert res.errors == [] and res.high == 1
        live = await ch.query_one(
            "SELECT count() AS n FROM loom.memory_chunks FINAL WHERE org = {o:String}"
            " AND namespace = {n:String} AND parent_memory_key = {k:String}"
            " AND tombstoned_at IS NULL",
            params={"o": ORG, "n": ns, "k": key},
        )
        assert int(live["n"]) == 0
    finally:
        c_gate.set()
        s_gate.set()
        for table in ("memory", "memory_embeddings", "entity_vectors", "memory_chunks"):
            await ch.execute(
                f"DELETE FROM loom.{table} WHERE org = {{o:String}} AND namespace = {{n:String}}",
                params={"o": ORG, "n": ns},
            )


async def test_a_reindex_between_the_sweeps_fence_and_its_relabel_leaves_no_vector(
    ch: Any, monkeypatch: Any
) -> None:
    """A whole reindex runs while the sweep is between its fence and its relabel: it
    reads an unlabelled head and lands above the fence. The sweep retires the key's
    vectors again once its relabel is the head."""
    import server.memory.store as memory_repo
    from server.memory.api.models_admin import MemoryReindexInput
    from server.memory.app import ops, sensitivity_sweep

    ns = f"itest-{uuid.uuid4().hex[:12]}"
    key = f"sweep_reindex_{uuid.uuid4().hex[:6]}"
    await _memory_only_row(ch, ns, key, f"the {key} vault password is hunter2")
    row = await ch.query_one(
        "SELECT toString(memory_id) AS id FROM loom.memory FINAL WHERE org = {o:String}"
        " AND namespace = {n:String} AND memory_key = {k:String}",
        params={"o": ORG, "n": ns, "k": key},
    )
    entered, gate = asyncio.Event(), asyncio.Event()
    real_wv = memory_repo.write_versions_by_ids

    async def paused_wv(*a: Any, **kw: Any) -> int:
        entered.set()
        await gate.wait()
        return await real_wv(*a, **kw)

    monkeypatch.setattr(memory_repo, "write_versions_by_ids", paused_wv)
    cand = sensitivity_sweep.SweepCandidate(
        row["id"], ORG, ns, AGENT, "-", "-", key, "high", ("credential",)
    )
    try:
        sweep = asyncio.create_task(sensitivity_sweep.apply([cand], ch=ch))
        await asyncio.wait_for(entered.wait(), 30)
        await ops.handle_memory_reindex(MemoryReindexInput(org=ORG, namespace=ns))
        gate.set()
        res = await asyncio.wait_for(sweep, 30)
        assert res.errors == [] and res.high == 1
        head = await ch.query_one(
            "SELECT sensitivity FROM loom.memory FINAL WHERE org = {o:String}"
            " AND namespace = {n:String} AND memory_key = {k:String}",
            params={"o": ORG, "n": ns, "k": key},
        )
        live = await ch.query_one(
            "SELECT count() AS n FROM loom.memory_embeddings FINAL WHERE org = {o:String}"
            " AND namespace = {n:String} AND memory_key = {k:String}"
            " AND tombstoned_at IS NULL AND embedding_dim > 0",
            params={"o": ORG, "n": ns, "k": key},
        )
        assert head["sensitivity"] == "high"
        assert int(live["n"]) == 0
    finally:
        gate.set()
        for table in ("memory", "memory_embeddings", "entity_vectors", "memory_chunks"):
            await ch.execute(
                f"DELETE FROM loom.{table} WHERE org = {{o:String}} AND namespace = {{n:String}}",
                params={"o": ORG, "n": ns},
            )


async def test_a_newer_plain_write_between_the_reindex_reads_keeps_its_value(
    ch: Any, monkeypatch: Any
) -> None:
    """A plain memory.set lands between reindex's row read and its max(version) read:
    the reindexed (older) vector would sit above the new one. The re-check hands the
    key back to the embed worker, which serves the newer value."""
    import server.core.ch as ch_mod
    from server.memory.api.models_admin import MemoryReindexInput
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app import ops, write
    from server.memory.workers.embedding import EmbeddingWorkerConfig, run_pass

    ns = f"itest-{uuid.uuid4().hex[:12]}"
    key = f"reindex_newer_{uuid.uuid4().hex[:6]}"
    await _memory_only_row(ch, ns, key, "OLDVALUE the deploy uses blue")

    async def newer() -> None:
        await write.handle_memory_set(
            MemorySetInput(
                org=ORG,
                namespace=ns,
                agent=AGENT,
                memory_key=key,
                value="NEWVALUE the deploy uses green",
            )
        )

    try:
        proxy = _ProxyBeforeMaxRead(ch, newer)
        monkeypatch.setattr(ch_mod, "get_client", lambda: proxy)
        await ops.handle_memory_reindex(MemoryReindexInput(org=ORG, namespace=ns))
        assert proxy.fired
        monkeypatch.setattr(ch_mod, "get_client", lambda: ch)
        await run_pass(EmbeddingWorkerConfig(org_filter=ORG, namespace_filter=ns))
        rows = await ch.query_all(
            "SELECT content_excerpt FROM loom.memory_embeddings FINAL WHERE org = {o:String}"
            " AND namespace = {n:String} AND memory_key = {k:String}"
            " AND tombstoned_at IS NULL AND embedding_dim > 0",
            params={"o": ORG, "n": ns, "k": key},
        )
        assert rows and all("NEWVALUE" in str(r["content_excerpt"]) for r in rows), rows
    finally:
        for table in ("memory", "memory_embeddings", "entity_vectors", "memory_chunks"):
            await ch.execute(
                f"DELETE FROM loom.{table} WHERE org = {{o:String}} AND namespace = {{n:String}}",
                params={"o": ORG, "n": ns},
            )
