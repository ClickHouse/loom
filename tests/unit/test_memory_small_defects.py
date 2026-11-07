"""Three small memory-plane defects.

(a) `memory.list(sort_by=score)` under agent `*` must not filter
    `agent = '*'` literally; the wildcard axes leave WHERE the way the
    recency path's do, and the writer columns are selected.
(c) Synthetic evidence rows and boosted chunks must not rise above the pool
    (`pool_max + 1.0`); both lifts are bounded to the pool's ceiling + ε,
    order-preserving, itemised in `score_components`.
(d) `memory.get(follow=true)` serves the live canonical a duplicate points
    to, disclosed as `resolved_from`; default off, exact row otherwise.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.api.models_crud import MemoryGetInput, MemoryGetOutput, MemoryListInput
from server.memory.domain.search_types import hit_sort_key
from server.memory.store.dedup import CanonicalRow

_NOW = datetime(2026, 1, 1, 12, 0, 0)

# ------------------------------------------------------------------ (a) list sort_by=score


class _ListCH:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.rows = rows or []

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append((sql, dict(params or {})))
        return list(self.rows)

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append((sql, dict(params or {})))
        return {"n": len(self.rows)}


def _list_row(agent: str) -> dict[str, Any]:
    now = datetime(2026, 9, 16, tzinfo=UTC)
    return {
        "memory_key": f"lesson_{agent}",
        "memory_type": "semantic",
        "confidence": 1.0,
        "version": 1,
        "created_at": now,
        "expires_at": None,
        "valid_at": None,
        "pinned": False,
        "evidence_count": 2,
        "recall_count": 4,
        "last_recalled_at": None,
        "tags": [],
        "entities": [],
        "source_trace": "",
        "verified_by": "",
        "verified_sha256": "",
        "content_sha256": "",
        "agent": agent,
        "user_id": "-",
        "session_id": "-",
        "value": json.dumps({"t": 1}),
        "value_truncated": 0,
    }


@pytest.mark.asyncio
async def test_list_by_score_drops_the_wildcard_axes_and_keeps_the_named_ones(monkeypatch) -> None:
    from server.memory.app import query as mq

    ch = _ListCH([_list_row("reviewer"), _list_row("bot")])
    monkeypatch.setattr(mq, "get_client", lambda: ch)
    monkeypatch.setattr(mq, "_record_read_exposure", lambda *a, **k: None)

    out = await mq._handle_memory_list(
        MemoryListInput(org="o", namespace="n", agent="*", sort_by="score", prefix="lesson_")
    )
    sql, params = ch.calls[0]
    assert "agent = {agent:String}" not in sql and "agent" not in params
    assert "user_id = {uid:String}" in sql and params["uid"] == "-"
    assert "session_id = {sid:String}" in sql and params["sid"] == "-"
    assert "agent, user_id, session_id," in sql, "the writer columns ride the score read"
    assert [e.agent for e in out.entries] == ["reviewer", "bot"] and out.total == 2

    ch.calls.clear()
    await mq._handle_memory_list(
        MemoryListInput(org="o", namespace="n", agent="a", user_id="*", sort_by="score")
    )
    sql, params = ch.calls[0]
    assert "agent = {agent:String}" in sql and params["agent"] == "a"
    assert "user_id = {uid:String}" not in sql and "uid" not in params
    assert "session_id = {sid:String}" in sql
    count_sql, _ = ch.calls[1]
    assert "agent = {agent:String}" in count_sql, "the total uses the same predicate"


# ------------------------------------------------------------------ (c) bounded lifts


def _hit(key: str, rank: float, boost: float = 0.0, distance: float = 0.3) -> SimpleNamespace:
    comps: dict[str, float] = {"rank_score": rank}
    if boost:
        comps["chunk_parent_boost"] = boost
    return SimpleNamespace(
        memory_key=key, rank_score=rank, distance=distance, score_components=comps
    )


def _pool(*specs: tuple[str, float, float]) -> list[SimpleNamespace]:
    hits = [_hit(k, r, b) for k, r, b in specs]
    hits.sort(key=hit_sort_key)
    return hits


def test_evidence_rows_sit_one_epsilon_above_the_pool_and_itemise_the_lift() -> None:
    from server.memory.app.search.evidence import (
        EVIDENCE_LIFT_EPS,
        _derived_evidence_hit,
        _pinned_rank,
        _temporal_diff_evidence_hit,
    )

    assert _pinned_rank([]) == (EVIDENCE_LIFT_EPS, 0.0)
    pool = _pool(("f1", 0.91, 0.0), ("f2", 0.3, 0.0))
    derived = _derived_evidence_hit({"op": "count", "count": 3, "items": []}, pool, namespace="n")
    assert derived.rank_score == pytest.approx(0.91 + EVIDENCE_LIFT_EPS)
    assert derived.rank_score < 1.0 and derived.similarity == 1.0
    assert derived.score_components["pool_ceiling"] == 0.91
    assert derived.score_components["evidence_lift"] == pytest.approx(EVIDENCE_LIFT_EPS)
    assert derived.score_components["rank_score"] == derived.rank_score
    assert sorted([derived, *pool], key=hit_sort_key)[0] is derived, "still first"

    diff = {"interval_days": 10, "from_date": "2024-01-01", "to_date": "2024-01-11"}
    computed = _temporal_diff_evidence_hit(diff, pool, namespace="n")
    assert computed.rank_score == pytest.approx(0.91 + EVIDENCE_LIFT_EPS)
    assert computed.score_components["evidence_lift"] == pytest.approx(EVIDENCE_LIFT_EPS)
    # A pool that already sits above 1.0 keeps the row above it: the bound is
    # the pool's ceiling, not a cap.
    high = _pool(("f1", 1.05, 0.0))
    assert _derived_evidence_hit({"op": "count", "count": 1}, high, namespace="n").rank_score > 1.05


def test_the_chunk_parent_lift_is_bounded_to_the_ceiling_and_keeps_the_order() -> None:
    from server.memory.domain.query_planner import bound_component_lift

    hits = _pool(
        ("c1", 1.15, 0.40),
        ("c2", 1.05, 0.25),
        ("f1", 0.90, 0.0),
        ("c3", 0.70, 0.15),
        ("f2", 0.50, 0.0),
    )
    before = [h.memory_key for h in hits]
    moved = bound_component_lift(hits)
    assert moved == 2
    assert [h.memory_key for h in sorted(hits, key=hit_sort_key)] == before, "order preserved"
    by = {h.memory_key: h for h in hits}
    assert by["c1"].rank_score == pytest.approx(0.90 + 2e-6)
    assert by["c2"].rank_score == pytest.approx(0.90 + 1e-6)
    assert all(h.rank_score <= 1.0 for h in hits)
    assert by["c1"].score_components["chunk_parent_lift"] == pytest.approx(0.90 + 2e-6 - 0.75)
    assert by["c1"].score_components["chunk_parent_boost"] == 0.40, "the nominal boost stays"
    assert by["c1"].score_components["rank_score"] == by["c1"].rank_score
    assert by["f1"].rank_score == 0.90 and "chunk_parent_lift" not in by["f1"].score_components
    assert by["c3"].rank_score == 0.70 and "chunk_parent_lift" not in by["c3"].score_components


def test_the_chunk_lift_bound_is_a_noop_when_nothing_or_everything_is_lifted() -> None:
    from server.memory.domain.query_planner import bound_component_lift

    facts = _pool(("f1", 1.2, 0.0), ("f2", 0.9, 0.0))
    assert bound_component_lift(facts) == 0 and facts[0].rank_score == 1.2
    chunks = _pool(("c1", 1.2, 0.4), ("c2", 1.1, 0.25))
    assert bound_component_lift(chunks) == 0 and chunks[0].rank_score == 1.2
    assert bound_component_lift([]) == 0
    # Within ε of the ceiling already: never raised, never moved.
    close = _pool(("c1", 0.90 + 5e-7, 0.4), ("f1", 0.90, 0.0))
    assert bound_component_lift(close) == 0 and close[0].rank_score == pytest.approx(0.90 + 5e-7)


def test_ties_and_near_ties_keep_a_strictly_ordered_pool() -> None:
    from server.memory.domain.query_planner import bound_component_lift

    hits = _pool(
        ("c1", 1.30, 0.40),
        ("c2", 0.90 + 2.5e-6, 0.40),
        ("c3", 0.90 + 2.4e-6, 0.25),
        ("c4", 0.90 + 2.3e-6, 0.25),
        ("f1", 0.90, 0.0),
    )
    before = [h.memory_key for h in hits]
    bound_component_lift(hits)
    ranks = [h.rank_score for h in hits]
    assert ranks == sorted(ranks, reverse=True) and len(set(ranks)) == len(ranks)
    assert [h.memory_key for h in sorted(hits, key=hit_sort_key)] == before
    assert all(h.rank_score > 0.90 for h in hits[:-1])


def test_the_bound_runs_in_the_assembler_after_summaries_take_their_band_score() -> None:
    """Bounded at fusion, boosted chunks would land under the summaries
    (which join the pool only in the assembler, normalised into the facts' raw
    band) — an order change. Bounded in the assembler the ceiling is the whole pool's, summaries included, and the
    chunks stay above them, in order."""
    from server.memory.domain.context_block import assemble_context_block

    def mk(key, rank, *, source="fact", boost=0.0, raw=None):
        comps = {"rank_score": rank, "rank_score_raw": raw if raw is not None else rank}
        if boost:
            comps["chunk_parent_boost"] = boost
        return SimpleNamespace(
            memory_key=key,
            rank_score=rank,
            distance=0.3,
            source=source,
            score_components=comps,
            content_excerpt=key,
            namespace="n",
            session_id="",
            memory_type="semantic",
        )

    def pool():
        return [
            mk("chunk:a", 1.10, source="chunk", boost=0.40, raw=0.70),
            mk("chunk:b", 0.95, source="chunk", boost=0.25, raw=0.70),
            mk("fact_1", 0.62),
            mk("fact_low", 0.20),
            mk("summary:s1", 0.98, source="summary"),  # → 0.20 + 0.98 × 0.50 = 0.69, above fact_1
        ]

    control = assemble_context_block(pool(), top_k=10)
    assert [h.memory_key for h in control] == [
        "chunk:a",
        "chunk:b",
        "summary:s1",
        "fact_1",
        "fact_low",
    ]
    assert control[0].rank_score == 1.10, "default off: values untouched"
    arm = assemble_context_block(pool(), top_k=10, bound_component_lifts=True)
    assert [h.memory_key for h in arm] == [h.memory_key for h in control], "same order"
    by = {h.memory_key: h for h in arm}
    ceiling = by["summary:s1"].rank_score
    assert ceiling == pytest.approx(0.69), "the summary is the best un-lifted row"
    assert by["chunk:a"].rank_score == pytest.approx(ceiling + 2e-6)
    assert by["chunk:b"].rank_score == pytest.approx(ceiling + 1e-6)
    assert by["chunk:a"].rank_score < 1.0 and "chunk_parent_lift" in by["chunk:a"].score_components
    assert "chunk_parent_lift" not in by["fact_1"].score_components
    assert "chunk_parent_lift" not in by["summary:s1"].score_components


# ------------------------------------------------------------------ (d) get follow


def _row(
    key: str = "k",
    status: str = "active",
    duplicate_of: str = "",
    *,
    namespace: str = "n",
    value: Any = None,
    memory_id: str = "11111111-1111-1111-1111-111111111111",
    version: int = 3,
) -> SimpleNamespace:
    now = datetime(2026, 9, 12, tzinfo=UTC)
    return SimpleNamespace(
        memory_id=memory_id,
        memory_key=key,
        namespace=namespace,
        value={"t": 1} if value is None else value,
        memory_type="semantic",
        confidence=1.0,
        version=version,
        created_at=now,
        updated_at=now,
        expires_at=None,
        tombstoned_at=None,
        on_expire="",
        expire_reason="",
        valid_at=None,
        valid_to=None,
        temporal_anchor=None,
        temporal_confidence=0.0,
        pinned=False,
        actor="",
        source_class="",
        sensitivity="",
        recall_count=0,
        last_recalled_at=None,
        evidence_count=1,
        agent="a",
        user_id="-",
        session_id="-",
        reinforcement_count=1,
        last_reinforced_at=None,
        tags=[],
        entities=[],
        status=status,
        duplicate_of=duplicate_of,
        source_trace="",
        source_resource_id="",
        run_id="-",
        verified_by="",
        verified_at=None,
        verified_sha256="",
        grounding="",
        evidence="",
        char_limit=None,
        mentioned_at=None,
    )


@pytest.fixture
def follow_env(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    from server.memory import store as memory_repo

    e = SimpleNamespace(row=_row(), by_id={}, id_calls=[], canon={}, live=set())

    async def get_by_key(identity: Any, key: str, **kw: Any) -> Any:
        return e.row

    async def get_by_id(memory_id: str, **kw: Any) -> Any:
        e.id_calls.append((memory_id, kw.get("org")))
        row = e.by_id.get(memory_id)
        # The store reads the id in the namespace it is given.
        return row if row is not None and row.namespace == kw["namespace"] else None

    async def canonical_rows_by_ids(
        org: str, nss: list[str], ids: list[str], *, scope: Any = None, ch: Any = None
    ) -> list[CanonicalRow]:
        return [
            CanonicalRow.from_row(x)
            for x in [
                {"namespace": nss[0], "canonical_id": i, "memory_key": e.canon[i]}
                for i in ids
                if i in e.canon
            ]
        ]

    monkeypatch.setattr(memory_repo, "get_by_key", get_by_key)
    monkeypatch.setattr(memory_repo, "get_by_id", get_by_id)

    async def live_ids_in_scope(
        org: str, nss: list[str], ids: list[str], *, scope: Any = None, ch: Any = None
    ) -> set[str]:
        return {i for i in ids if i in e.live}

    monkeypatch.setattr(memory_repo, "canonical_rows_by_ids", canonical_rows_by_ids)
    monkeypatch.setattr(memory_repo, "live_ids_in_scope", live_ids_in_scope)
    monkeypatch.setattr("server.memory.app.query._record_read_exposure", lambda *a, **k: None)
    return e


_CANON_ID = "22222222-2222-2222-2222-222222222222"


@pytest.mark.asyncio
async def test_follow_serves_the_canonical_and_discloses_the_key_asked_for(follow_env) -> None:
    from server.memory.app import handlers as mem

    follow_env.row = _row("k", "duplicate", _CANON_ID)
    follow_env.by_id = {
        _CANON_ID: _row("canon_k", "active", value={"t": 2}, memory_id=_CANON_ID, version=9)
    }
    follow_env.canon = {_CANON_ID: "canon_k"}

    exact = await mem.handle_memory_get(
        MemoryGetInput(org="o", namespace="n", agent="a", memory_key="k")
    )
    d = exact.model_dump()
    assert (
        exact.status == "duplicate" and d["canonical_key"] == "canon_k" and exact.value == {"t": 1}
    )
    assert "resolved_from" not in d and follow_env.id_calls == [], (
        "default: the exact row, no id read"
    )

    out = await mem.handle_memory_get(
        MemoryGetInput(org="o", namespace="n", agent="a", memory_key="k", follow=True)
    )
    d = out.model_dump()
    assert out.status == "active" and out.value == {"t": 2} and out.version == 9
    assert d["resolved_from"] == "k" and d["canonical_key"] == "canon_k"
    assert out.memory_id == _CANON_ID and out.duplicate_of == "" and out.conflict_verdict == "clear"
    assert follow_env.id_calls == [(_CANON_ID, "o")], "read by id, in the caller's org"

    # if_version compares against the row served.
    held = await mem.handle_memory_get(
        MemoryGetInput(org="o", namespace="n", agent="a", memory_key="k", follow=True, if_version=9)
    )
    assert held.unchanged is True and held.version == 9


@pytest.mark.asyncio
async def test_follow_falls_back_to_the_exact_row_when_the_canonical_is_not_live(
    follow_env,
) -> None:
    from server.memory.app import handlers as mem

    inp = MemoryGetInput(org="o", namespace="n", agent="a", memory_key="k", follow=True)
    follow_env.row = _row("k", "duplicate", _CANON_ID)

    follow_env.by_id = {}  # gone
    out = await mem.handle_memory_get(inp)
    assert out.status == "duplicate" and out.value == {"t": 1}
    assert "resolved_from" not in out.model_dump() and out.model_dump()["canonical_key"] == ""

    follow_env.by_id = {_CANON_ID: _row("canon_k", "superseded", memory_id=_CANON_ID)}  # not live
    assert "resolved_from" not in (await mem.handle_memory_get(inp)).model_dump()

    other_ns = _row("canon_k", "active", memory_id=_CANON_ID, namespace="elsewhere")
    follow_env.by_id = {_CANON_ID: other_ns}  # another namespace: never crossed
    assert "resolved_from" not in (await mem.handle_memory_get(inp)).model_dump()

    gone = _row("canon_k", "active", memory_id=_CANON_ID)
    gone.tombstoned_at = datetime(2026, 9, 1, tzinfo=UTC)
    follow_env.by_id = {_CANON_ID: gone}
    assert "resolved_from" not in (await mem.handle_memory_get(inp)).model_dump()

    expired = _row("canon_k", "active", memory_id=_CANON_ID)
    expired.expires_at = datetime(2020, 1, 1, tzinfo=UTC)
    follow_env.by_id = {_CANON_ID: expired}
    assert "resolved_from" not in (await mem.handle_memory_get(inp)).model_dump()


@pytest.mark.asyncio
async def test_follow_walks_a_chain_and_stops_after_three_hops(follow_env) -> None:
    from server.memory.app import handlers as mem

    inp = MemoryGetInput(org="o", namespace="n", agent="a", memory_key="k", follow=True)
    mid = "33333333-3333-3333-3333-333333333333"
    follow_env.row = _row("k", "duplicate", mid)
    follow_env.by_id = {
        mid: _row("mid_k", "duplicate", _CANON_ID, memory_id=mid),
        _CANON_ID: _row("canon_k", "active", value={"t": 3}, memory_id=_CANON_ID),
    }
    out = await mem.handle_memory_get(inp)
    assert out.value == {"t": 3} and out.model_dump()["resolved_from"] == "k"
    assert out.model_dump()["canonical_key"] == "canon_k"

    # A loop of duplicates never resolves: the exact row comes back.
    follow_env.by_id = {mid: _row("mid_k", "duplicate", mid, memory_id=mid)}
    follow_env.id_calls.clear()
    out = await mem.handle_memory_get(inp)
    assert out.status == "duplicate" and "resolved_from" not in out.model_dump()
    assert len(follow_env.id_calls) == 3


@pytest.mark.asyncio
async def test_follow_on_an_active_row_is_byte_identical_to_the_default_read(follow_env) -> None:
    from server.memory.app import handlers as mem

    a = await mem.handle_memory_get(
        MemoryGetInput(org="o", namespace="n", agent="a", memory_key="k")
    )
    b = await mem.handle_memory_get(
        MemoryGetInput(org="o", namespace="n", agent="a", memory_key="k", follow=True)
    )
    assert a.model_dump_json() == b.model_dump_json() and follow_env.id_calls == []
    assert "resolved_from" not in MemoryGetOutput(found=True).model_dump()
    assert "resolved_from" not in json.loads(MemoryGetOutput(found=False).model_dump_json())
    assert MemoryGetInput(org="o", namespace="n", agent="a", memory_key="k").follow is False


@pytest.mark.asyncio
async def test_the_python_sdk_sends_follow_only_when_asked() -> None:
    from sdk.python.loom.memory import MemoryClient

    posts: list[tuple[str, dict[str, Any]]] = []

    class _T:
        async def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
            posts.append((path, dict(payload)))
            return {"found": True}

    from loom import Agent

    agent = Agent("o", "n", "a", api_key="k")
    agent.transport = _T()  # type: ignore[assignment]
    api = MemoryClient(agent)
    await api.get("k")
    await api.get("k", follow=True)
    assert "follow" not in posts[0][1] and posts[1][1]["follow"] is True


@pytest.mark.asyncio
async def test_get_never_names_a_canonical_outside_the_read_scope(
    follow_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """U's duplicate pointing at V's canonical: a read pinned to U gets neither
    V's key nor V's id."""
    from server.identity.domain.read_scope import ReadFilters
    from server.memory import store as memory_repo
    from server.memory.app import handlers as mem

    only_u = ReadFilters(scope="own_user", user_id="u", session_id=None)
    seen: list[Any] = []

    async def canonical_rows_by_ids(
        org: str, nss: list[str], ids: list[str], *, scope: Any = None, ch: Any = None
    ) -> list[CanonicalRow]:
        seen.append(scope)
        if scope is not None and scope.narrowed:
            return []  # V's canonical is outside U's scope
        row = {"namespace": nss[0], "canonical_id": _CANON_ID, "memory_key": "v_secret_key"}
        return [CanonicalRow.from_row(row)]

    monkeypatch.setattr(memory_repo, "canonical_rows_by_ids", canonical_rows_by_ids)
    monkeypatch.setattr("server.memory.app.served_duplicates.request_scope", lambda: only_u)
    monkeypatch.setattr("server.memory.app.get_output.request_narrowed", lambda: True)
    follow_env.row = _row("k", "duplicate", _CANON_ID)

    out = await mem.handle_memory_get(
        MemoryGetInput(org="o", namespace="n", agent="a", memory_key="k", user_id="u")
    )
    assert seen == [only_u]
    dumped = json.dumps(out.model_dump(), default=str)
    assert "v_secret_key" not in dumped and _CANON_ID not in dumped
    assert out.status == "duplicate" and out.value == {"t": 1}


@pytest.mark.asyncio
async def test_get_names_the_canonical_id_to_an_unbounded_read(follow_env) -> None:
    from server.memory.app import handlers as mem

    follow_env.row = _row("k", "duplicate", _CANON_ID)
    out = await mem.handle_memory_get(
        MemoryGetInput(org="o", namespace="n", agent="a", memory_key="k")
    )
    # Gone to a whole-namespace read too, but nothing there is foreign: the id stays.
    assert out.duplicate_of == _CANON_ID and out.model_dump()["canonical_key"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("bounded", [True, False])
async def test_get_names_a_successor_only_inside_the_read_scope(
    follow_env, monkeypatch: pytest.MonkeyPatch, bounded: bool
) -> None:
    from server.memory.app import handlers as mem

    monkeypatch.setattr("server.identity.app.request_scope.request_narrowed", lambda: bounded)
    row = _row("k", "superseded")
    row.superseded_by = _CANON_ID  # V's row: outside a scope bounded to U
    follow_env.row = row
    out = await mem.handle_memory_get(
        MemoryGetInput(org="o", namespace="n", agent="a", memory_key="k")
    )
    assert out.status == "superseded"
    assert out.superseded_by == ("" if bounded else _CANON_ID)


@pytest.mark.asyncio
async def test_bounded_get_names_an_in_scope_successor_that_is_itself_superseded(
    follow_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A -> B -> C: B is superseded (not active) but inside the read's scope, so a
    # bounded get names it, as memory.search's successor read does.
    from server.memory.app import handlers as mem

    monkeypatch.setattr("server.identity.app.request_scope.request_narrowed", lambda: True)
    row = _row("k", "superseded")
    row.superseded_by = _CANON_ID
    follow_env.row = row
    follow_env.live = {_CANON_ID}  # live, but no active canonical row behind it
    out = await mem.handle_memory_get(
        MemoryGetInput(org="o", namespace="n", agent="a", memory_key="k")
    )
    assert out.superseded_by == _CANON_ID


@pytest.mark.asyncio
async def test_live_ids_in_scope_reads_any_status_inside_the_scope() -> None:
    from server.identity.domain.read_scope import ReadFilters
    from server.memory.store.dedup import live_ids_in_scope

    seen: list[tuple[str, dict[str, Any]]] = []

    class _CH:
        async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
            seen.append((sql, params))
            return [{"mid": _CANON_ID}]

    scope = ReadFilters(scope="own_user", user_id="u", session_id=None)
    got = await live_ids_in_scope("o", ["n"], [_CANON_ID, "not-a-uuid"], scope=scope, ch=_CH())
    assert got == {_CANON_ID}
    sql, params = seen[0]
    assert "status" not in sql and "tombstoned_at IS NULL" in sql
    assert params["ids"] == [_CANON_ID] and "u" in params.values()
