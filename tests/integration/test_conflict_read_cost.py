"""A writer narrowed to one user pays, in the conflict check, its own scoped read
plus a read of the namespace's pinned rows' vectors, so at most about twice a
namespace-wide writer's cost; and another user's pinned row blocks it whatever its
vector row says and however many of the writer's rows sit closer. Measured from
system.query_log on the configured ClickHouse; skips without LOOM_CH_HOST."""

from __future__ import annotations

import secrets
from typing import Any

import pytest

from server.settings import settings
from tests.integration._resources import unique_org

pytestmark = pytest.mark.usefixtures("ch_target_required")

_USERS = 20
_ROWS_PER_USER = 500
_NS = "cost"
# The configured width: a coded vector column takes vectors of exactly it.
_DIM = settings.embedding_dim


def _vec(seed: str) -> str:
    return f"arrayMap(j -> toFloat32((cityHash64({seed}, j) % 2000) / 1000.0 - 1.0), range({_DIM}))"


# All semantic, or a quarter semantic (number % 4 = 3: row 47 and every u3 row are).
_TYPES = {
    "one_type": "'semantic'",
    "mixed_types": "if(number % 4 = 3, 'semantic', 'episodic')",
}


async def _seed(ch: Any, org: str, types: str) -> None:
    """20 users x 500 rows, every 47th row pinned: 47 is coprime with 20, so the
    pinned rows spread over every user (and every vector granule)."""
    user = f"concat('u', toString(number % {_USERS}))"
    mt = _TYPES[types]
    key = "concat('k', toString(number))"
    await ch.execute(
        f"""
        INSERT INTO loom.memory
            (org, namespace, agent, user_id, session_id, memory_key, memory_type,
             value, version, pinned)
        SELECT '{org}', '{_NS}', 'bot', {user}, '-', {key}, {mt},
               concat('fact ', toString(number)), 1, number % 47 = 0
        FROM numbers({_USERS * _ROWS_PER_USER})
        """
    )
    await ch.execute(
        f"""
        INSERT INTO loom.memory_embeddings
            (org, namespace, agent, user_id, session_id, memory_key, memory_type, scope,
             content_excerpt, embedding_model, embedding_dim, embedding, memory_key_embedding)
        SELECT '{org}', '{_NS}', 'bot', {user}, '-', {key}, {mt}, 'private',
               concat('fact ', toString(number)), 'fake', {_DIM}, {_vec("number")},
               arrayWithConstant({_DIM}, 0)
        FROM numbers({_USERS * _ROWS_PER_USER})
        """
    )


async def _logged(ch: Any, tag: str) -> list[dict[str, Any]]:
    await ch.execute("SYSTEM FLUSH LOGS")
    return await ch.query_all(
        """
        SELECT query, read_rows, read_bytes, memory_usage
        FROM system.query_log
        WHERE type = 'QueryFinish'
          AND (log_comment = {tag:String} OR startsWith(log_comment, {tag_sp:String}))
        ORDER BY event_time_microseconds
        """,
        params={"tag": tag, "tag_sp": f"{tag} "},  # a statement digest may follow the tag
    )


@pytest.fixture
async def ch() -> Any:
    from server.core.ch import CHClient

    c = CHClient()
    await c.connect()
    yield c
    await c.disconnect()


def _cost(logged: list[dict[str, Any]]) -> dict[str, int]:
    return {k: sum(int(q[k]) for q in logged) for k in ("read_rows", "read_bytes", "memory_usage")}


@pytest.mark.parametrize("types", sorted(_TYPES))
async def test_a_narrowed_writers_conflict_reads_cost_at_most_two_namespace_reads(
    ch: Any, types: str
) -> None:
    from server.auth.context import set_current_auth
    from server.auth.jwt import AuthInfo
    from server.core.ch import set_query_tag
    from server.identity.domain.read_scope import WHOLE_NAMESPACE
    from server.memory.app import conflicts

    org = unique_org("conflict-cost")
    await _seed(ch, org, types)
    # Row 47 is u7's and pinned, so it is the write's nearest row. Rounded so four
    # facts' vectors fit in ClickHouse's HTTP form-field limit.
    emb = [round(float(x), 3) for x in (await ch.query_one(f"SELECT {_vec('47')} AS v"))["v"]]
    params = {
        "org": org,
        "ns": _NS,
        "mt": "semantic",
        "self_key": "new",
        "emb": emb,
        "max_dist": 2.0,
        "dim": _DIM,
    }
    items = [("semantic", f"new{i}", emb, 2.0) for i in range(4)]
    run = secrets.token_hex(4)

    async def measured(phase: str, call: Any) -> Any:
        set_query_tag(f"it.conflict_cost.{run}.{phase}")
        try:
            return await call()
        finally:
            set_query_tag("")

    wide = await measured("wide", lambda: conflicts.nearest_peer_rows(ch, params))
    wide_bulk = await measured(
        "wide_bulk",
        lambda: conflicts.candidate_rows_many(org=org, namespace=_NS, items=items, batch=4, ch=ch),
    )
    set_current_auth(
        AuthInfo(
            subject="sa:bot",
            org=org,
            namespaces=(_NS,),
            roles=("writer",),
            is_service_account=True,
            read_user_id="u3",
        )
    )
    try:
        scope = conflicts._scope_or_none()
        own = await measured("scoped", lambda: conflicts.nearest_peer_rows(ch, params, scope))
        pinned = await measured(
            "pinned", lambda: conflicts._foreign_pinned_rows(ch, params, scope, own)
        )
        bulk = await measured(
            "bulk",
            lambda: conflicts.candidate_rows_many(
                org=org, namespace=_NS, items=items, batch=4, ch=ch
            ),
        )
    finally:
        set_current_auth(None)

    assert wide[0]["memory_key"] == "k47" and scope is not WHOLE_NAMESPACE
    assert len(own) == 10 and all(r["user_id"] == "u3" for r in own)
    assert [r["memory_key"] for r in pinned][:1] == ["k47"]
    assert all(r["pinned"] and r["user_id"] != "u3" for r in pinned)
    assert len(wide_bulk) == len(bulk) == 4
    for rows in bulk.values():
        assert sum(1 for r in rows if r["user_id"] == "u3" and not r["pinned"]) == 10
        assert any(r["memory_key"] == "k47" for r in rows)

    cost = {
        phase: _cost(await _logged(ch, f"it.conflict_cost.{run}.{phase}"))
        for phase in ("wide", "wide_bulk", "scoped", "pinned", "bulk")
    }
    for phase, c in cost.items():
        print(f"{phase}: {c['read_rows']} rows, {c['read_bytes']} B, {c['memory_usage']} B mem")
    # With pins spread over every granule the pinned arm reads about as much as
    # the namespace-wide stage A (every vector, brute force, no vector index), so a
    # narrowed write costs its own scoped read on top of what any other writer pays.
    single = cost["scoped"]["read_bytes"] + cost["pinned"]["read_bytes"]
    assert single <= 2 * cost["wide"]["read_bytes"]
    assert cost["scoped"]["read_bytes"] < cost["wide"]["read_bytes"] / 5
    assert cost["bulk"]["read_bytes"] <= 2 * cost["wide_bulk"]["read_bytes"]


def _small(i: int) -> str:
    """A vector at angle i/1000 from [1, 0, ...] in its first two axes: a larger i
    is farther. Zero-padded to the configured width."""
    return f"arrayResize([cos({i} / 1000), sin({i} / 1000)], {_DIM}, 0)"


async def _seed_close_rows(ch: Any, org: str, own_rows: int, vector_type: str) -> None:
    """V's pinned semantic memory row, its embedding row typed `vector_type`, and
    `own_rows` of U's own semantic rows closer to the write than V's."""
    rows = [("V", "v-fact", "V lives in Oslo", 1, 50, vector_type)] + [
        ("U", f"u-{i}", f"U note number {i}", 0, i % 40, "semantic") for i in range(own_rows)
    ]
    for user, key, value, pinned, angle, vtype in rows:
        await ch.execute(
            f"""
            INSERT INTO loom.memory
                (org, namespace, agent, user_id, session_id, memory_key, memory_type,
                 value, version, pinned)
            VALUES ('{org}', '{_NS}', 'bot', '{user}', '-', '{key}', 'semantic',
                    '{value}', 1, {pinned})
            """
        )
        await ch.execute(
            f"""
            INSERT INTO loom.memory_embeddings
                (org, namespace, agent, user_id, session_id, memory_key, memory_type, scope,
                 content_excerpt, embedding_model, embedding_dim, embedding, memory_key_embedding)
            VALUES ('{org}', '{_NS}', 'bot', '{user}', '-', '{key}', '{vtype}', 'private',
                    '{value}', 'fake', {_DIM}, {_small(angle)}, arrayWithConstant({_DIM}, 0))
            """
        )


@pytest.mark.parametrize(("own_rows", "vector_type"), [(0, "episodic"), (40, "semantic")])
async def test_a_foreign_pinned_row_blocks_a_narrowed_write_single_and_bulk(
    ch: Any, monkeypatch: pytest.MonkeyPatch, own_rows: int, vector_type: str
) -> None:
    from server.auth.context import set_current_auth
    from server.auth.jwt import AuthInfo
    from server.memory.app import conflicts

    org = unique_org("conflict-pinned")
    await _seed_close_rows(ch, org, own_rows, vector_type)
    monkeypatch.setattr(conflicts, "get_client", lambda: ch)
    emb = [1.0] + [0.0] * (_DIM - 1)

    def check(candidate_rows: Any) -> Any:
        return conflicts.check_on_commit(
            org=org,
            namespace=_NS,
            agent="bot",
            memory_type="semantic",
            memory_key="new",
            new_value="U lives in Lisbon",
            new_embedding=emb,
            candidate_rows=candidate_rows,
            resolved_threshold=0.85,
            resolved_duplicate_threshold=0.9,
            resolved_policy="auto_reinforce",
            allowlisted_keys=set(),
        )

    set_current_auth(
        AuthInfo(
            subject="sa:bot",
            org=org,
            namespaces=(_NS,),
            roles=("writer",),
            is_service_account=True,
            read_user_id="U",
        )
    )
    try:
        single = await check(None)
        rows = await conflicts.candidate_rows_many(
            org=org, namespace=_NS, items=[("semantic", "new", emb, 0.3)], ch=ch
        )
        batched = await check(rows[0])
    finally:
        set_current_auth(None)
    assert single.verdict == batched.verdict == "rejected"
    assert all(p.user_id == "U" for p in single.peers + batched.peers), "V's row is never named"
