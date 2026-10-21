"""A job whose call for one identity is held skips that identity's work, it does not
stall on it: the next selections leave the held work out (or read more and drop
it), so the batch goes to other identities' work while the hold lasts, and the
held work comes back once it is over. A group whose identity cannot be read is
held alone: the other groups' vectors are kept."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import server.memory.store as memory_repo
from server.core.errors import ModelCallRefused
from server.llm import customer, identity
from server.llm.identity import IdentityUnread
from server.settings import settings
from server.workers import holds
from tests.unit.clock_fake import ManualClock
from tests.unit.switch_state import switch_off

_REFUSED = {"gateway_status": 429, "gateway_code": "budget_exceeded"}


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> ManualClock:
    c = ManualClock(t=1000.0)
    monkeypatch.setattr(holds.time, "monotonic", c.monotonic)
    return c


@pytest.fixture
def agents(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Identity headers naming the agent, at gateway.example; no customer header."""
    monkeypatch.setattr(settings, "inference_identity_headers_json", '{"X-Agent": "{agent}"}')
    monkeypatch.setattr(settings, "inference_gateway_hosts", "gateway.example")
    monkeypatch.setattr(settings, "inference_customer_header", "")
    identity._reset_for_tests()
    switch_off()
    yield
    identity._reset_for_tests()


def _stub(key: str, *, created: int, version: int) -> dict[str, Any]:
    row = memory_repo.build_embedding_row(
        org="acme",
        namespace="shared",
        memory_key=key,
        content_excerpt=f"text of {key}",
        embedding_model="m",
        embedding_dim=0,
        embed_attempts=0,
    )
    base = datetime(2026, 10, 1, tzinfo=UTC)
    return {**row, "created_at": base + timedelta(minutes=created), "version": version}


class _Stubs:
    """`find_embedding_stubs` over `rows` as ClickHouse answers it: the newest four
    times the batch by version, minus the keys left out, then the oldest first, at
    most the batch."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.embedded: set[str] = set()
        self.limits: list[int] = []

    async def find(self, *, batch_size: int, exclude_keys: Any = None, **_: Any) -> Any:
        self.limits.append(batch_size)
        left_out = set(exclude_keys or [])
        live = [
            r for r in self.rows if r["memory_key"] not in self.embedded and _key(r) not in left_out
        ]
        candidates = sorted(live, key=lambda r: -r["version"])[: batch_size * 4]
        return [dict(r) for r in sorted(candidates, key=lambda r: r["created_at"])[:batch_size]]

    async def insert(self, rows: list[dict[str, Any]], **_: Any) -> None:
        self.embedded.update(r["memory_key"] for r in rows if r["embedding_dim"] > 0)

    async def writers(self, org: str, keys: Any, **_: Any) -> dict[Any, Any]:
        # a* were written by agent-a, b* by agent-b, into the shared namespace
        return {
            (ns, key): [
                {
                    "agent": "",
                    "user_id": "-",
                    "session_id": "-",
                    "actor": f"process:sa:t:agent-{key[0]}",
                }
            ]
            for ns, key in keys
        }


def _key(r: dict[str, Any]) -> tuple[str, ...]:
    cols = (
        "org",
        "namespace",
        "agent",
        "user_id",
        "session_id",
        "scope",
        "embedding_model",
        "memory_key",
    )
    return tuple(str(r.get(c) or "") for c in cols)


class _Embedder:
    """The gateway's answer by the agent a call names: agent-a is over its budget."""

    model_name, dim = "m", 2

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        agent = customer.headers(await customer.require()).get("X-Agent", "")
        self.calls.append((agent, len(texts)))
        if agent == "agent-a":
            raise ModelCallRefused("refused", hold_s=600.0, **_REFUSED)
        return [[1.0, 0.0] for _ in texts]


def test_the_embedding_worker_takes_other_identities_stubs_while_one_is_held(
    agents, clock, monkeypatch
) -> None:
    from server.memory.workers import embedding as w

    # agent-a's three stubs are the oldest; the batch holds two.
    store = _Stubs(
        [
            _stub("a1", created=1, version=10),
            _stub("a2", created=2, version=11),
            _stub("a3", created=3, version=12),
            _stub("b1", created=4, version=13),
            _stub("b2", created=5, version=14),
        ]
    )
    embedder = _Embedder()

    async def nothing(*a: Any, **k: Any) -> Any:
        return None

    async def none(*a: Any, **k: Any) -> list[Any]:
        return []

    async def served(rows: list[dict[str, Any]], *a: Any) -> tuple[list[Any], list[Any]]:
        return rows, []

    monkeypatch.setattr(w, "get_embedder", lambda: embedder)
    monkeypatch.setattr(memory_repo, "find_memories_without_embedding_rows", none)
    monkeypatch.setattr(memory_repo, "find_embedding_stubs", store.find)
    monkeypatch.setattr(memory_repo, "insert_embedding_rows", store.insert)
    monkeypatch.setattr(memory_repo, "actors_by_key", store.writers)
    monkeypatch.setattr(memory_repo, "retire_copies_under_unembedded_heads", nothing)
    monkeypatch.setattr(memory_repo, "index_late_rows", nothing)
    monkeypatch.setattr(w, "_stubs_still_served", served)
    monkeypatch.setattr(w, "_refresh_entities", nothing)

    def run() -> Any:
        switch_off()
        return asyncio.run(w.run_pass(w.EmbeddingWorkerConfig(batch_size=2)))

    first = run()
    assert embedder.calls == [("agent-a", 2)] and first.embedded_count == 0
    second = run()  # a1, a2 left out; a3 read and dropped, never sent
    assert embedder.calls[1:] == [("agent-b", 2)]
    assert sorted(store.embedded) == ["b1", "b2"] and second.failed_count == 0
    assert store.limits == [2, 8]  # while an identity is held, a larger read
    assert run().embedded_count == 0 and embedder.calls[2:] == []  # all left: no call
    clock.advance(600)
    run()  # the hold is over: agent-a's oldest are asked for again
    assert embedder.calls[2:] == [("agent-a", 2)] and store.limits[-1] == 2


class _Identities:
    """The profile fold's selection as ClickHouse answers it: the identities not
    left out (switched off, held), the most facts first, at most `lim`."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.sent: list[dict[str, Any]] = []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None, **_: Any) -> Any:
        params = params or {}
        self.sent.append(params)
        held = {tuple(h) for h in params.get("held", [])}
        assert ("held" in params) == ("NOT IN {held:Array(Tuple(String, String, String))}" in sql)
        live = [
            r
            for r in self.rows
            if r["org"] not in set(params["off"])
            and (r["org"], r["namespace"], r["user_id"]) not in held
        ]
        return sorted(live, key=lambda r: -r["n"])[: params["lim"]]


def test_the_profile_fold_gives_a_held_identitys_slot_to_the_next(clock, monkeypatch) -> None:
    from server.memory.workers import profile_consolidation as w

    ch = _Identities(
        [
            {"org": "acme", "namespace": "n", "user_id": "u-big", "n": 9, "writer": ""},
            {"org": "acme", "namespace": "n", "user_id": "u-small", "n": 3, "writer": ""},
        ]
    )
    folded: list[str] = []

    async def fold(ch: Any, row: dict[str, Any], cfg: Any) -> bool:
        folded.append(row["user_id"])
        if row["user_id"] == "u-big":
            raise ModelCallRefused("refused", hold_s=600.0, **_REFUSED)
        return True

    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    monkeypatch.setattr(w, "_consolidate_identity", fold)

    def run() -> Any:
        switch_off()
        return asyncio.run(w.run_pass(w.ProfileConsolidationConfig(max_identities_per_pass=1)))

    assert run().errors == 0 and folded == ["u-big"]  # a held fold is no failure
    assert run().profiles_written == 1 and folded == ["u-big", "u-small"]
    assert ch.sent[1]["held"] == [("acme", "n", "u-big")]
    clock.advance(600)
    run()
    assert folded[-1] == "u-big" and "held" not in ch.sent[-1]  # the hold is over


class _ByOrg:
    """An embedder whose call for acme cannot name its identity (`IdentityUnread`)."""

    model_name, dim = "m", 2

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        if customer.current() == "acme":
            raise IdentityUnread("the identity headers could not read namespace acme/n")
        return [[0.5, 0.5] for _ in texts]

    async def embed(self, text: str) -> list[float]:
        return (await self.embed_many([text]))[0]


@pytest.fixture
def by_org(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """An identity header naming the org: each org's rows are their own call."""
    monkeypatch.setattr(settings, "inference_identity_headers_json", '{"X-Org": "{org}"}')
    monkeypatch.setattr(settings, "inference_gateway_hosts", "gateway.example")
    monkeypatch.setattr(settings, "inference_customer_header", "")
    switch_off()
    yield


async def test_the_embedding_worker_holds_only_the_group_it_cannot_name(by_org) -> None:
    from server.memory.workers import embedding as w

    rows = [
        {"org": "acme", "namespace": "n", "memory_key": "k1", "content_excerpt": "a"},
        {"org": "beta", "namespace": "n", "memory_key": "k2", "content_excerpt": "b"},
    ]
    out = await w._embed_rows(_ByOrg(), rows)
    assert out == [(None, w.HELD), ([0.5, 0.5], "")]  # beta's vector is kept
    assert [k[-1] for k in w._HELD_STUBS.current()] == ["k1"]


def test_code_recovery_holds_only_the_group_it_cannot_name(by_org) -> None:
    from server.code.workers import embed_recovery as w

    class _Repo:
        rows: list[Any] = []

        def tombstone_of(self, table: str, live: dict[str, Any]) -> dict[str, Any]:
            return dict(live)

        async def insert_rows(self, table: str, rows: list[Any]) -> None:
            self.rows = self.rows + list(rows)

    repo = _Repo()
    rows = [
        {"org": org, "namespace": f"code-{org}", "text": "def f(): pass", "embed_attempts": 4}
        for org in ("acme", "beta")
    ]
    result = w.CodeEmbedRecoveryResult()
    embedded = asyncio.run(
        w._embed_rows(
            repo,
            "code_chunks",
            rows,
            _ByOrg(),
            text_of=lambda r: r["text"],
            max_attempts=5,
            result=result,
        )
    )
    assert embedded == 1 and result.failed == 0 and result.switched_off == 1
    assert [r["org"] for r in repo.rows] == ["beta"]


def test_reembed_writes_the_groups_it_could_name(by_org) -> None:
    from server.memory.app import reembed

    table = "conversation_embeddings"
    text = reembed._TABLES[table]["text"]

    class _CH:
        rows: list[Any] = []

        async def insert(self, table: str, rows: list[Any], **_: Any) -> None:
            self.rows = rows

    ch = _CH()
    chunk = [{"org": "acme", text: "a"}, {"org": "beta", text: "b"}]
    left = asyncio.run(reembed._rewrite_page(ch, _ByOrg(), table, chunk, key_only=False))
    assert left == {"acme": 1} and [r["org"] for r in ch.rows] == ["beta"]


def test_a_rebuild_stages_the_group_it_cannot_name_without_a_vector(by_org, monkeypatch) -> None:
    from server.insights.app import provider_rebuild_batch as batch

    monkeypatch.setattr(
        batch,
        "_inputs",
        lambda table, rows, source: (
            [r["text"] for r in rows],
            [(i, "embedding") for i in range(len(rows))],
        ),
    )
    monkeypatch.setattr(
        batch, "_empty_target_row", lambda t, row, m: {**row, "embedding": [], "embedding_dim": 0}
    )
    rows = [{"org": "acme", "text": "a"}, {"org": "beta", "text": "b"}]
    waiting: list[int] = []
    out = asyncio.run(batch.rebuild_batch("memory_embeddings", rows, _ByOrg(), waiting=waiting))
    assert waiting == [0]  # never "embedding_provider_failed": acme's row waits
    assert (out[1]["embedding"], out[1]["embedding_dim"]) == ([0.5, 0.5], 2)
