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

from server.settings import settings
from tests.integration._eventually import eventually

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


async def _high_candidate(ch: Any, ns: str, key: str) -> Any:
    """The sweep's candidate for AGENT's row `key` as `scan` reads it: its head, unlabelled,
    stated high whatever the detector reads in it."""
    from server.memory.app.sensitivity_sweep import SweepCandidate
    from server.memory.store.sensitivity_sweep_sql import SORT_KEY

    head = await ch.query_one(
        "SELECT toString(memory_id) AS id, version, sensitivity FROM loom.memory FINAL"
        " WHERE org = {o:String} AND namespace = {n:String} AND memory_key = {k:String}",
        params={"o": ORG, "n": ns, "k": key},
    )
    assert head["sensitivity"] == ""
    ident = (ORG, ns, AGENT, USER, SESSION, key)
    return SweepCandidate(
        memory_id=head["id"],
        org=ORG,
        namespace=ns,
        agent=AGENT,
        user_id=USER,
        session_id=SESSION,
        memory_key=key,
        level="high",
        shapes=("credential",),
        sort_key=dict(zip(SORT_KEY, ident, strict=True)),
        version=head["version"],
    )


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


# The configured width: a coded vector column takes vectors of exactly it.
_DIM = settings.embedding_dim


def _wide(head: list[float]) -> list[float]:
    return head + [0.0] * (_DIM - len(head))


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
        embedding_dim=_DIM,
        embedding=_wide([0.1, 0.2, 0.3]),
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
                "entity_embedding": _wide([0.1, 0.2, 0.3]),
                "embedding_model": "fake",
                "embedding_dim": _DIM,
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
    return await M.get_by_id(
        row["id"], org=ORG, namespace=ns, ch=ch, fresh=True, scope=WHOLE_NAMESPACE
    )


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
                    cand = await _high_candidate(ch, ns, key)
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


async def _memory_only_row(ch: Any, ns: str, key: str, value: str) -> dict[str, Any]:
    """A live memory row with no vector of the served model."""
    from server.memory import store as M

    row = {**_memory_row(ns, M.next_version(), value, ""), "memory_key": key}
    await M.insert_memory_versions([row], ch=ch)
    return row


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

        cand = await _high_candidate(ch, ns, key)
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
                cand = await _high_candidate(ch, ns, key)
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
        cand = await _high_candidate(ch, ns, key)
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
        cand = await _high_candidate(ch, ns, key)
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
    # The secret opens the value: the chunks past the first read plain and are embedded
    # (a chunk that reads as high never is), so the paused embed below runs.
    filler = f"The {key} notes go on about the release plan. "
    long_val = f"The {key} root token is {secret} keep it safe. " + filler * (
        EXCERPT_LIMIT // len(filler) + 4
    )

    c_entered, c_gate = asyncio.Event(), asyncio.Event()
    s_entered, s_gate = asyncio.Event(), asyncio.Event()
    real_embed_chunks = set_chunks._embed_chunks
    real_insert = memory_repo.insert_versions_over_heads

    async def paused_embed_chunks(embedder: Any, chunks: list[str]) -> list[list[float]]:
        c_entered.set()
        await c_gate.wait()
        return await real_embed_chunks(embedder, chunks)

    async def paused_insert(*a: Any, **kw: Any) -> int:
        s_entered.set()
        await s_gate.wait()
        return await real_insert(*a, **kw)

    monkeypatch.setattr(set_chunks, "_embed_chunks", paused_embed_chunks)
    monkeypatch.setattr(memory_repo, "insert_versions_over_heads", paused_insert)
    try:
        task = asyncio.create_task(
            write.handle_memory_set(MemorySetInput(**ident, memory_key=key, value=long_val))
        )
        await asyncio.wait_for(c_entered.wait(), 30)
        cand = await _high_candidate(ch, ns, key)
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
    cand = await _high_candidate(ch, ns, key)
    entered, gate = asyncio.Event(), asyncio.Event()
    real_insert = memory_repo.insert_versions_over_heads

    async def paused_insert(*a: Any, **kw: Any) -> int:
        entered.set()
        await gate.wait()
        return await real_insert(*a, **kw)

    monkeypatch.setattr(memory_repo, "insert_versions_over_heads", paused_insert)
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


class _ProxyAroundReindexReads:
    """The client, running `before_max` once just before reindex's max(version) read
    and `after_mirror` once just after its post-insert mirror read."""

    def __init__(self, inner: Any, before_max: Any, after_mirror: Any) -> None:
        self._inner, self._before_max, self._after_mirror = inner, before_max, after_mirror
        self.fired: list[str] = []

    async def query_all(self, sql: str, *a: Any, **kw: Any) -> Any:
        if "max(version) AS ev" in sql and "max" not in self.fired:
            self.fired.append("max")
            await self._before_max()
        out = await self._inner.query_all(sql, *a, **kw)
        if "max" in self.fired and "mirror" not in self.fired and "length(m.value) > 0" in sql:
            self.fired.append("mirror")
            await self._after_mirror()
        return out

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


async def test_a_high_write_after_the_reindex_mirror_read_fences_the_stub(
    ch: Any, monkeypatch: Any
) -> None:
    """A newer plain write lands between the reindex reads (so the key gets a stub),
    then a 'high' write lands right after the mirror read the stub is built from. The
    stub sits one above what it read, below the high write's fence, so the worker's
    fill is never served."""
    import server.core.ch as ch_mod
    from server.memory.api.models_admin import MemoryReindexInput
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app import ops, write
    from server.memory.workers.embedding import EmbeddingWorkerConfig, run_pass

    ns = f"itest-{uuid.uuid4().hex[:12]}"
    key = f"stub_fence_{uuid.uuid4().hex[:6]}"
    ident = {"org": ORG, "namespace": ns, "agent": AGENT, "memory_key": key}
    secret = "sk-proj-" + uuid.uuid4().hex + uuid.uuid4().hex[:8]
    await _memory_only_row(ch, ns, key, "OLDVALUE the deploy uses blue")

    async def newer_plain() -> None:
        await write.handle_memory_set(MemorySetInput(**ident, value="NEWVALUE deploy uses green"))

    async def high() -> None:
        await write.handle_memory_set(MemorySetInput(**ident, value=f"root token {secret}"))

    try:
        proxy = _ProxyAroundReindexReads(ch, newer_plain, high)
        monkeypatch.setattr(ch_mod, "get_client", lambda: proxy)
        await ops.handle_memory_reindex(MemoryReindexInput(org=ORG, namespace=ns))
        assert proxy.fired == ["max", "mirror"]
        monkeypatch.setattr(ch_mod, "get_client", lambda: ch)
        await run_pass(EmbeddingWorkerConfig(org_filter=ORG, namespace_filter=ns))
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
        for table in ("memory", "memory_embeddings", "entity_vectors", "memory_chunks"):
            await ch.execute(
                f"DELETE FROM loom.{table} WHERE org = {{o:String}} AND namespace = {{n:String}}",
                params={"o": ORG, "n": ns},
            )


async def test_a_touch_during_the_reindex_keeps_the_reindexed_vectors(
    ch: Any, monkeypatch: Any
) -> None:
    """A metadata-only write between the reindex reads changes the head's version, not
    its value: the reindexed vectors (value and key axis) stay live on its columns."""
    import server.core.ch as ch_mod
    from server.memory import store as M
    from server.memory.api.models_admin import MemoryReindexInput
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app import ops, write

    ns = f"itest-{uuid.uuid4().hex[:12]}"
    key = f"reindex_touch_{uuid.uuid4().hex[:6]}"
    await write.handle_memory_set(
        MemorySetInput(org=ORG, namespace=ns, agent=AGENT, memory_key=key, value=f"{key} is blue")
    )

    async def touch() -> None:
        head = await _memory_row_of(ch, ns, key)
        await M.insert_memory_versions(
            [M.build_memory_version_row(head, version=M.next_version(), tags=["touched"])], ch=ch
        )

    async def nothing() -> None:
        return None

    try:
        proxy = _ProxyAroundReindexReads(ch, touch, nothing)
        monkeypatch.setattr(ch_mod, "get_client", lambda: proxy)
        out = await ops.handle_memory_reindex(MemoryReindexInput(org=ORG, namespace=ns, force=True))
        assert out.processed == 1 and proxy.fired == ["max", "mirror"]
        row = await ch.query_one(
            "SELECT embedding_dim, memory_key_embedding_dim, tags FROM loom.memory_embeddings"
            " FINAL WHERE org = {o:String} AND namespace = {n:String}"
            " AND memory_key = {k:String} AND tombstoned_at IS NULL",
            params={"o": ORG, "n": ns, "k": key},
        )
        assert int(row["embedding_dim"]) > 0 and int(row["memory_key_embedding_dim"]) > 0
        assert "touched" in list(row["tags"])
    finally:
        for table in ("memory", "memory_embeddings", "entity_vectors"):
            await ch.execute(
                f"DELETE FROM loom.{table} WHERE org = {{o:String}} AND namespace = {{n:String}}",
                params={"o": ORG, "n": ns},
            )


class _ProxyBeforeReindexHeadRead:
    """The client, running `before_max` before reindex's max(version) read and
    `before_head` before its post-insert head read."""

    def __init__(self, inner: Any, before_max: Any, before_head: Any) -> None:
        self._inner, self._before_max, self._before_head = inner, before_max, before_head
        self.fired: list[str] = []

    async def query_all(self, sql: str, *a: Any, **kw: Any) -> Any:
        if "max(version) AS ev" in sql and "max" not in self.fired:
            self.fired.append("max")
            await self._before_max()
        elif (
            "max" in self.fired
            and "head" not in self.fired
            and "PREWHERE (org, namespace, agent, user_id, session_id, memory_key)" in sql
            and "FROM loom.memory FINAL" in sql
        ):
            self.fired.append("head")
            await self._before_head()
        return await self._inner.query_all(sql, *a, **kw)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


async def test_a_touch_copy_of_a_reindexed_row_under_a_high_head_is_retired(
    ch: Any, monkeypatch: Any
) -> None:
    """A high write lands between the reindex reads (the reindexed row sits above its
    fence), then memory.touch copies that row at its version + 1 before the re-check:
    the re-check's retire sits above the copy, so nothing is live under the high head."""
    import server.core.ch as ch_mod
    from server.memory.api.models_admin import MemoryReindexInput
    from server.memory.api.models_crud import MemorySetInput, MemoryTouchInput
    from server.memory.app import handlers, ops, write

    ns = f"itest-{uuid.uuid4().hex[:12]}"
    key = f"reindex_touch_high_{uuid.uuid4().hex[:6]}"
    ident = {"org": ORG, "namespace": ns, "agent": AGENT, "memory_key": key}
    secret = "sk-proj-" + uuid.uuid4().hex + uuid.uuid4().hex[:8]
    await _memory_only_row(ch, ns, key, "OLDVALUE the deploy uses blue")

    async def high() -> None:
        await write.handle_memory_set(MemorySetInput(**ident, value=f"root token {secret}"))

    async def touch() -> None:
        await handlers.handle_memory_touch(MemoryTouchInput(**ident, ttl_seconds=3600))

    try:
        proxy = _ProxyBeforeReindexHeadRead(ch, high, touch)
        monkeypatch.setattr(ch_mod, "get_client", lambda: proxy)
        await ops.handle_memory_reindex(MemoryReindexInput(org=ORG, namespace=ns))
        monkeypatch.setattr(ch_mod, "get_client", lambda: ch)
        assert proxy.fired == ["max", "head"]
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
        for table in ("memory", "memory_embeddings", "entity_vectors", "memory_chunks"):
            await ch.execute(
                f"DELETE FROM loom.{table} WHERE org = {{o:String}} AND namespace = {{n:String}}",
                params={"o": ORG, "n": ns},
            )


class _ProxyBeforeRetireRead:
    """The client, running `before_max` before reindex's max(version) read and
    `before_retire` just before its re-check's retire reads the key's live rows."""

    def __init__(self, inner: Any, before_max: Any, before_retire: Any) -> None:
        self._inner, self._before_max, self._before_retire = inner, before_max, before_retire
        self.fired: list[str] = []

    async def query_all(self, sql: str, *a: Any, **kw: Any) -> Any:
        if "max(version) AS ev" in sql and "max" not in self.fired:
            self.fired.append("max")
            await self._before_max()
        elif (
            "max" in self.fired
            and "head" not in self.fired
            and "PREWHERE (org, namespace, agent, user_id, session_id, memory_key)" in sql
            and "FROM loom.memory FINAL" in sql
        ):
            self.fired.append("head")
        elif (
            "head" in self.fired
            and "retire" not in self.fired
            and "SELECT * FROM loom.memory_embeddings FINAL" in sql
            and "tombstoned_at IS NULL" in sql
        ):
            self.fired.append("retire")
            await self._before_retire()
        return await self._inner.query_all(sql, *a, **kw)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


@pytest.mark.parametrize("second", ["touch", "verify"])
async def test_two_copies_of_a_reindexed_row_under_a_high_head_are_never_made(
    ch: Any, monkeypatch: Any, second: str
) -> None:
    """A high write lands between the reindex reads, then two copies (touch + touch,
    touch + verify) run between the re-check's head read and its retire. The copy
    paths never copy a vector under a head that is not embedded, so nothing is live
    under the high head whatever the retire's margin."""
    import server.core.ch as ch_mod
    from server.memory.api.models_admin import MemoryReindexInput
    from server.memory.api.models_crud import MemorySetInput, MemoryTouchInput, MemoryVerifyInput
    from server.memory.app import handlers, ops, verify, write

    ns = f"itest-{uuid.uuid4().hex[:12]}"
    key = f"reindex_touch3_{uuid.uuid4().hex[:6]}"
    ident = {"org": ORG, "namespace": ns, "agent": AGENT, "memory_key": key}
    secret = "sk-proj-" + uuid.uuid4().hex + uuid.uuid4().hex[:8]
    await _memory_only_row(ch, ns, key, "OLDVALUE the deploy uses blue")

    async def high() -> None:
        await write.handle_memory_set(MemorySetInput(**ident, value=f"root token {secret}"))

    async def copies() -> None:
        monkeypatch.setattr(ch_mod, "get_client", lambda: ch)
        await handlers.handle_memory_touch(MemoryTouchInput(**ident, ttl_seconds=3600))
        if second == "touch":
            await handlers.handle_memory_touch(MemoryTouchInput(**ident, ttl_seconds=7200))
        else:
            await verify.handle_memory_verify(
                MemoryVerifyInput(
                    org=ORG, namespace=ns, agent=AGENT, memory_key=key, actor="human:probe"
                )
            )

    try:
        proxy = _ProxyBeforeRetireRead(ch, high, copies)
        monkeypatch.setattr(ch_mod, "get_client", lambda: proxy)
        await ops.handle_memory_reindex(MemoryReindexInput(org=ORG, namespace=ns))
        monkeypatch.setattr(ch_mod, "get_client", lambda: ch)
        assert proxy.fired == ["max", "head", "retire"]
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
        for table in ("memory", "memory_embeddings", "entity_vectors", "memory_chunks"):
            await ch.execute(
                f"DELETE FROM loom.{table} WHERE org = {{o:String}} AND namespace = {{n:String}}",
                params={"o": ORG, "n": ns},
            )


async def test_copies_that_read_the_head_before_the_high_write_retire_themselves(
    ch: Any, monkeypatch: Any
) -> None:
    """Two touches read the (embedded) head, then a 'high' write lands, then reindex
    writes above its fence and the touches copy that row up one at a time, past the
    re-check's retire. Each copy re-reads the head after its insert and retires one
    above itself, so nothing is live under the high head."""
    import server.core.ch as ch_mod
    import server.memory.store as M
    from server.memory.api.models_admin import MemoryReindexInput
    from server.memory.api.models_crud import MemorySetInput, MemoryTouchInput
    from server.memory.app import handlers, ops, write

    ns = f"itest-{uuid.uuid4().hex[:12]}"
    key = f"r41_{uuid.uuid4().hex[:6]}"
    ident = {"org": ORG, "namespace": ns, "agent": AGENT, "memory_key": key}
    secret = "sk-proj-" + uuid.uuid4().hex + uuid.uuid4().hex[:8]
    await _memory_only_row(ch, ns, key, "OLDVALUE the deploy uses blue")

    orig = M.reemit_embedding_rows_for_key
    entered = {"A": asyncio.Event(), "B": asyncio.Event()}
    go_a, go_b_read, b_read_done, go_b_insert = (asyncio.Event() for _ in range(4))
    calls: list[str] = []

    async def wrapped(o, n, a, u, s, k, *, ch=None, **ov):  # type: ignore[no-untyped-def]
        who = "A" if not calls else "B"
        calls.append(who)
        entered[who].set()
        if who == "A":
            await go_a.wait()
            return await orig(o, n, a, u, s, k, ch=ch, **ov)
        await go_b_read.wait()
        rows = await M.live_embedding_rows_for_key(
            ch, org=o, namespace=n, agent=a, user_id=u, session_id=s, memory_key=k
        )
        b_read_done.set()
        await go_b_insert.wait()
        from server.memory.store.embeddings import reemit_rows

        return await reemit_rows(rows, ov, ch=ch)

    monkeypatch.setattr(M, "reemit_embedding_rows_for_key", wrapped)
    monkeypatch.setattr(handlers, "get_client", lambda: ch)
    tasks: list[asyncio.Task[Any]] = []

    async def before_max() -> None:
        monkeypatch.setattr(ch_mod, "get_client", lambda: ch)
        # both touches read the (embedded) head and insert their memory rows first
        tasks.append(
            asyncio.create_task(
                handlers.handle_memory_touch(MemoryTouchInput(**ident, ttl_seconds=3600))
            )
        )
        await asyncio.wait_for(entered["A"].wait(), 30)
        tasks.append(
            asyncio.create_task(
                handlers.handle_memory_touch(MemoryTouchInput(**ident, ttl_seconds=7200))
            )
        )
        await asyncio.wait_for(entered["B"].wait(), 30)
        await write.handle_memory_set(MemorySetInput(**ident, value=f"root token {secret}"))

    async def before_retire() -> None:
        go_a.set()
        await asyncio.wait_for(tasks[0], 30)  # A copies the reindexed row at +1
        go_b_read.set()
        await asyncio.wait_for(b_read_done.wait(), 30)  # B read A's copy

    proxy = _ProxyBeforeRetireRead(ch, before_max, before_retire)
    monkeypatch.setattr(ch_mod, "get_client", lambda: proxy)
    try:
        await ops.handle_memory_reindex(MemoryReindexInput(org=ORG, namespace=ns))
        go_b_insert.set()
        await asyncio.wait_for(tasks[1], 30)
        monkeypatch.setattr(ch_mod, "get_client", lambda: ch)
        assert proxy.fired == ["max", "head", "retire"]
        head = await ch.query_one(
            "SELECT sensitivity, version FROM loom.memory FINAL WHERE org = {o:String}"
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
        for table in ("memory", "memory_embeddings", "entity_vectors", "memory_chunks"):
            await ch.execute(
                f"DELETE FROM loom.{table} WHERE org = {{o:String}} AND namespace = {{n:String}}",
                params={"o": ORG, "n": ns},
            )


async def test_a_high_profile_section_never_gets_a_vector(ch: Any) -> None:
    """profile.set with a credential is labelled 'high': no re-embed stub, nothing for
    the embed worker to fill, and the profile leg returns no hit."""
    from server.identity.domain.read_scope import WHOLE_NAMESPACE
    from server.memory.api.models_profile import ProfileSetInput
    from server.memory.app import profile
    from server.memory.app.profile_search import search_profile
    from server.memory.workers.embedding import EmbeddingWorkerConfig, run_pass

    ns = f"itest-{uuid.uuid4().hex[:12]}"
    secret = "sk-proj-" + uuid.uuid4().hex + uuid.uuid4().hex[:8]
    try:
        await profile.handle_profile_set(
            ProfileSetInput(
                org=ORG, namespace=ns, agent=AGENT, label="persona", value=f"my key is {secret}"
            )
        )
        await run_pass(EmbeddingWorkerConfig(org_filter=ORG, namespace_filter=ns))
        head = await ch.query_one(
            "SELECT sensitivity FROM loom.memory FINAL WHERE org = {o:String}"
            " AND namespace = {n:String} AND memory_type = 'profile'",
            params={"o": ORG, "n": ns},
        )
        assert head is not None and head["sensitivity"] == "high"
        live = await ch.query_one(
            "SELECT count() AS n FROM loom.memory_embeddings FINAL WHERE org = {o:String}"
            " AND namespace = {n:String} AND tombstoned_at IS NULL AND embedding_dim > 0",
            params={"o": ORG, "n": ns},
        )
        assert int(live["n"]) == 0
        hits = await search_profile(
            ORG, [ns], "my key", top_k=5, lexical=lambda *_a: True, scope=WHOLE_NAMESPACE, ch=ch
        )
        assert secret not in json.dumps(hits, default=str)
    finally:
        for table in ("memory", "memory_embeddings", "entity_vectors", "memory_chunks"):
            await ch.execute(
                f"DELETE FROM loom.{table} WHERE org = {{o:String}} AND namespace = {{n:String}}",
                params={"o": ORG, "n": ns},
            )


async def _high_key_with_row_above_its_fence(
    ch: Any, ns: str, key: str, *, dim: int
) -> tuple[str, int]:
    """A key made 'high' through memory.set, then a vector row (or a dim-0 stub) of
    its old text written above the high row's fence, as a pass that drew its version
    before the high write but inserted after it would leave."""
    from server.embedding import get_embedder
    from server.memory import store as M
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app import write

    secret = "sk-proj-" + uuid.uuid4().hex + uuid.uuid4().hex[:8]
    ident = {"org": ORG, "namespace": ns, "agent": AGENT, "memory_key": key}
    await write.handle_memory_set(MemorySetInput(**ident, value=f"root token {secret}"))
    head = await ch.query_one(
        "SELECT version FROM loom.memory FINAL WHERE org = {o:String}"
        " AND namespace = {n:String} AND memory_key = {k:String}",
        params={"o": ORG, "n": ns, "k": key},
    )
    embedder = get_embedder()
    row = M.build_embedding_row(
        org=ORG,
        namespace=ns,
        agent=AGENT,
        user_id=USER,
        session_id=SESSION,
        memory_key=key,
        memory_type="semantic",
        scope="private",
        content_excerpt=f"the old text and {secret}",
        embedding_model=embedder.model_name,
        embedding_dim=dim,
        embedding=[0.1] * dim,
        created_at=datetime.now(UTC).replace(tzinfo=None),
        version=int(head["version"]) + 10,
    )
    await M.insert_embedding_rows([row], ch=ch)
    return secret, int(head["version"])


async def _live_rows(ch: Any, ns: str, key: str) -> int:
    row = await ch.query_one(
        "SELECT count() AS n FROM loom.memory_embeddings FINAL WHERE org = {o:String}"
        " AND namespace = {n:String} AND memory_key = {k:String} AND tombstoned_at IS NULL",
        params={"o": ORG, "n": ns, "k": key},
    )
    return int(row["n"])


async def test_a_high_candidate_whose_head_moved_retires_nothing(ch: Any) -> None:
    """The key is written again after the scan, with text the detector did not read: the
    sweep leaves that head unlabelled and its vector live."""
    from server.embedding import get_embedder
    from server.memory import store as M
    from server.memory.app import sensitivity_sweep

    ns = f"itest-{uuid.uuid4().hex[:12]}"
    key = f"moved_{uuid.uuid4().hex[:6]}"
    try:
        scanned = await _memory_only_row(ch, ns, key, f"the {key} vault password is hunter2")
        cand = await _high_candidate(ch, ns, key)
        text = "the door is blue"
        newer = scanned | {
            "version": M.next_version(),
            "value": json.dumps(text),
            "value_json": json.dumps({"v": text}),
            "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
        }
        await M.insert_memory_versions([newer], ch=ch)
        embedder = get_embedder()
        vector = M.build_embedding_row(
            org=ORG,
            namespace=ns,
            agent=AGENT,
            user_id=USER,
            session_id=SESSION,
            memory_key=key,
            memory_type="semantic",
            scope="private",
            content_excerpt=text,
            embedding_model=embedder.model_name,
            embedding_dim=embedder.dim,
            embedding=[0.1] * embedder.dim,
            created_at=datetime.now(UTC).replace(tzinfo=None),
            version=newer["version"],
        )
        await M.insert_embedding_rows([vector], ch=ch)
        res = await sensitivity_sweep.apply([cand], ch=ch)
        assert (res.relabelled, len(res.left), res.errors) == (0, 1, [])
        assert await _live_rows(ch, ns, key) == 1
        head = await ch.query_one(
            "SELECT version, sensitivity FROM loom.memory FINAL WHERE org = {o:String}"
            " AND namespace = {n:String} AND memory_key = {k:String}"
            " SETTINGS select_sequential_consistency = 1",
            params={"o": ORG, "n": ns, "k": key},
        )
        assert (head["version"], head["sensitivity"]) == (newer["version"], "")
    finally:
        for table in ("memory", "memory_embeddings", "entity_vectors", "memory_chunks"):
            await ch.execute(
                f"DELETE FROM loom.{table} WHERE org = {{o:String}} AND namespace = {{n:String}}",
                params={"o": ORG, "n": ns},
            )


async def test_a_high_candidate_is_labelled_whatever_other_key_holds_its_id(ch: Any) -> None:
    """A namespace restored into itself under another agent holds the id under two keys,
    the copy's head newer: the sweep reads the candidate by its own key, labels it and
    retires its vector, and leaves the copy as it is."""
    from server.embedding import get_embedder
    from server.memory import store as M
    from server.memory.app import sensitivity_sweep

    ns = f"itest-{uuid.uuid4().hex[:12]}"
    key = f"twin_{uuid.uuid4().hex[:6]}"
    try:
        scanned = await _memory_only_row(ch, ns, key, f"the {key} vault password is hunter2")
        cand = await _high_candidate(ch, ns, key)
        copy = scanned | {"agent": "restored", "version": M.next_version()}
        await M.insert_memory_versions([copy], ch=ch)
        embedder = get_embedder()
        vector = M.build_embedding_row(
            org=ORG,
            namespace=ns,
            agent=AGENT,
            user_id=USER,
            session_id=SESSION,
            memory_key=key,
            memory_type="semantic",
            scope="private",
            content_excerpt="the vault password",
            embedding_model=embedder.model_name,
            embedding_dim=embedder.dim,
            embedding=[0.1] * embedder.dim,
            created_at=datetime.now(UTC).replace(tzinfo=None),
            version=scanned["version"],
        )
        await M.insert_embedding_rows([vector], ch=ch)
        res = await sensitivity_sweep.apply([cand], ch=ch)
        assert (res.high, res.left, res.errors) == (1, [], [])
        assert await _live_rows(ch, ns, key) == 0
        labels = await ch.query_all(
            "SELECT agent, version, sensitivity FROM loom.memory FINAL WHERE org = {o:String}"
            " AND namespace = {n:String} AND memory_key = {k:String} ORDER BY agent"
            " SETTINGS select_sequential_consistency = 1",
            params={"o": ORG, "n": ns, "k": key},
        )
        by_agent = {r["agent"]: (r["version"], r["sensitivity"]) for r in labels}
        assert by_agent[AGENT][0] > scanned["version"] and by_agent[AGENT][1] == "high"
        assert by_agent["restored"] == (copy["version"], "")
    finally:
        for table in ("memory", "memory_embeddings", "entity_vectors", "memory_chunks"):
            await ch.execute(
                f"DELETE FROM loom.{table} WHERE org = {{o:String}} AND namespace = {{n:String}}",
                params={"o": ORG, "n": ns},
            )


async def test_the_sweep_labels_a_row_stored_with_no_user_or_session(ch: Any) -> None:
    """A user or session sent empty is stored as given: the sweep reads, labels and
    retires the row under that stored key."""
    from server.embedding import get_embedder
    from server.memory import store as M
    from server.memory.app import sensitivity_sweep

    ns = f"itest-{uuid.uuid4().hex[:12]}"
    key = f"blank_{uuid.uuid4().hex[:6]}"
    try:
        row = {
            **_memory_row(ns, M.next_version(), "the badge PIN is 4471", ""),
            "memory_key": key,
            "user_id": "",
            "session_id": "",
        }
        await M.insert_memory_versions([row], ch=ch)
        embedder = get_embedder()
        vector = M.build_embedding_row(
            org=ORG,
            namespace=ns,
            agent=AGENT,
            user_id="",
            session_id="",
            memory_key=key,
            memory_type="semantic",
            scope="private",
            content_excerpt="the badge PIN",
            embedding_model=embedder.model_name,
            embedding_dim=embedder.dim,
            embedding=[0.1] * embedder.dim,
            created_at=datetime.now(UTC).replace(tzinfo=None),
            version=row["version"],
        )
        await M.insert_embedding_rows([vector], ch=ch)
        report = await sensitivity_sweep.scan(
            org=ORG, namespace=ns, limit=10, page_size=100, pages=1, ch=ch
        )
        assert [(c.memory_key, c.user_id, c.session_id) for c in report.candidates] == [
            (key, "", "")
        ]
        res = await sensitivity_sweep.apply(report.candidates, ch=ch)
        # The retire matched the stored key: the fence alone would hide the vector too.
        assert (res.high, res.embeddings_tombstoned, res.left, res.errors) == (1, 1, [], [])
        head = await ch.query_one(
            "SELECT sensitivity FROM loom.memory FINAL WHERE org = {o:String}"
            " AND namespace = {n:String} AND memory_key = {k:String}"
            " SETTINGS select_sequential_consistency = 1",
            params={"o": ORG, "n": ns, "k": key},
        )
        assert head["sensitivity"] == "high"
        assert await _live_rows(ch, ns, key) == 0
    finally:
        for table in ("memory", "memory_embeddings", "entity_vectors", "memory_chunks"):
            await ch.execute(
                f"DELETE FROM loom.{table} WHERE org = {{o:String}} AND namespace = {{n:String}}",
                params={"o": ORG, "n": ns},
            )


async def test_fence_high_retires_a_vector_written_above_its_fence(ch: Any) -> None:
    from server.embedding import get_embedder
    from server.memory.app.high_fence import fence_high

    ns = f"itest-{uuid.uuid4().hex[:12]}"
    key = f"above_fence_{uuid.uuid4().hex[:6]}"
    try:
        _secret, version = await _high_key_with_row_above_its_fence(
            ch, ns, key, dim=get_embedder().dim
        )
        assert await _live_rows(ch, ns, key) == 1
        await fence_high([((ORG, ns, AGENT, USER, SESSION, key), version)], ch=ch)
        assert await _live_rows(ch, ns, key) == 0
    finally:
        for table in ("memory", "memory_embeddings", "entity_vectors", "memory_chunks"):
            await ch.execute(
                f"DELETE FROM loom.{table} WHERE org = {{o:String}} AND namespace = {{n:String}}",
                params={"o": ORG, "n": ns},
            )


async def test_a_stub_above_a_high_fence_is_never_sent_to_the_embedder(
    ch: Any, monkeypatch: Any
) -> None:
    from server.embedding import embedder as embedder_mod
    from server.memory.workers.embedding import EmbeddingWorkerConfig, run_pass

    ns = f"itest-{uuid.uuid4().hex[:12]}"
    key = f"stub_above_fence_{uuid.uuid4().hex[:6]}"
    sent: list[str] = []

    class _Recording:
        def __init__(self, inner: Any) -> None:
            self._inner, self.model_name, self.dim = inner, inner.model_name, inner.dim

        async def embed(self, text: str) -> list[float]:
            sent.append(text)
            return await self._inner.embed(text)

        async def embed_many(self, texts: list[str]) -> list[list[float]]:
            sent.extend(texts)
            return await self._inner.embed_many(texts)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

    try:
        secret, _version = await _high_key_with_row_above_its_fence(ch, ns, key, dim=0)
        monkeypatch.setattr(embedder_mod, "_cached", _Recording(embedder_mod.get_embedder()))
        await run_pass(EmbeddingWorkerConfig(org_filter=ORG, namespace_filter=ns))
        assert not any(secret in t for t in sent)
        assert await _live_rows(ch, ns, key) == 0
    finally:
        for table in ("memory", "memory_embeddings", "entity_vectors", "memory_chunks"):
            await ch.execute(
                f"DELETE FROM loom.{table} WHERE org = {{o:String}} AND namespace = {{n:String}}",
                params={"o": ORG, "n": ns},
            )


async def test_a_slow_writes_embedding_is_still_counted_in_the_bm25_stats(ch: Any) -> None:
    """An embedding row carries its memory row's version, drawn when the write began.
    When it lands after the refresh views' watermark moved past that version, no pass
    counts it; the write indexes its own row (`index_late_rows`). A late row that does
    not is the control: absent from both tables after the next refresh."""
    import time

    from server.embedding import get_embedder
    from server.memory import store as M

    async def refresh() -> None:
        for view in ("memory_doc_stats_refresh", "memory_term_freq_refresh"):
            await ch.execute(f"SYSTEM REFRESH VIEW loom.{view}")
            await ch.execute(f"SYSTEM WAIT VIEW loom.{view}")

    ns = f"itest-{uuid.uuid4().hex[:12]}"
    word = f"zq{uuid.uuid4().hex[:8]}"
    embedder = get_embedder()

    def late_row(key: str, *, ago_s: int = 30) -> dict[str, Any]:
        return M.build_embedding_row(
            org=ORG,
            namespace=ns,
            agent=AGENT,
            user_id=USER,
            session_id=SESSION,
            memory_key=key,
            memory_type="semantic",
            scope="private",
            content_excerpt=f"the {word} runbook",
            embedding_model=embedder.model_name,
            embedding_dim=embedder.dim,
            embedding=[0.1] * embedder.dim,
            created_at=datetime.now(UTC).replace(tzinfo=None),
            version=time.time_ns() - ago_s * 1_000_000_000,  # drawn ago_s seconds ago
        )

    async def marker_indexed() -> bool:
        await refresh()
        row = await ch.query_one(
            "SELECT count() AS n FROM loom.memory_doc_stats FINAL WHERE org = {o:String}"
            " AND namespace = {n:String} AND memory_key = 'marker'",
            params={"o": ORG, "n": ns},
        )
        return bool(row and int(row["n"]))

    try:
        # A row drawn now: once a pass has indexed it, the watermarks are past it and so
        # past the late rows below, drawn 30 s ago, whatever other namespaces wrote.
        await M.insert_embedding_rows([late_row("marker", ago_s=0)], ch=ch)
        await eventually(marker_indexed, max_wait=60.0, interval=1.0, description="the marker")
        indexed, control = late_row("slow_write"), late_row("slow_write_unindexed")
        await M.insert_embedding_rows([indexed, control], ch=ch)
        assert await M.index_late_rows([indexed], ch=ch) == 1
        await refresh()
        stats = await ch.query_all(
            "SELECT memory_key FROM loom.memory_doc_stats FINAL WHERE org = {o:String}"
            " AND namespace = {n:String}",
            params={"o": ORG, "n": ns},
        )
        assert sorted(r["memory_key"] for r in stats) == ["marker", "slow_write"]
        df = await ch.query_one(
            "SELECT uniqExactMerge(df_state) AS n FROM loom.memory_term_freq"
            " WHERE org = {o:String} AND namespace = {n:String} AND term = {t:String}",
            params={"o": ORG, "n": ns, "t": word},
        )
        assert int(df["n"]) == 2  # the marker and the indexed late row, not the control
    finally:
        for table in ("memory_embeddings", "memory_doc_stats", "memory_term_freq"):
            await ch.execute(
                f"DELETE FROM loom.{table} WHERE org = {{o:String}} AND namespace = {{n:String}}",
                params={"o": ORG, "n": ns},
            )
