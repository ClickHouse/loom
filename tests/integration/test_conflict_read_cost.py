"""A writer narrowed to one user pays, in the conflict check, its own scoped read
plus the read a namespace-wide writer makes (where other users' pinned rows come
from), so at most about twice a namespace-wide writer's cost. Measured from
system.query_log on the configured ClickHouse; skips without LOOM_CH_HOST."""

from __future__ import annotations

import secrets
from typing import Any

import pytest

from tests.integration._resources import unique_org

pytestmark = pytest.mark.usefixtures("ch_target_required")

_USERS = 20
_ROWS_PER_USER = 500
_DIM = 3072
_NS = "cost"


def _vec(seed: str) -> str:
    return f"arrayMap(j -> toFloat32((cityHash64({seed}, j) % 2000) / 1000.0 - 1.0), range({_DIM}))"


async def _seed(ch: Any, org: str) -> None:
    """20 users x 500 rows, every 47th row pinned: 47 is coprime with 20, so the
    pinned rows spread over every user (and every vector granule)."""
    user = f"concat('u', toString(number % {_USERS}))"
    key = "concat('k', toString(number))"
    await ch.execute(
        f"""
        INSERT INTO loom.memory
            (org, namespace, agent, user_id, session_id, memory_key, memory_type,
             value, version, pinned)
        SELECT '{org}', '{_NS}', 'bot', {user}, '-', {key}, 'semantic',
               concat('fact ', toString(number)), 1, number % 47 = 0
        FROM numbers({_USERS * _ROWS_PER_USER})
        """
    )
    await ch.execute(
        f"""
        INSERT INTO loom.memory_embeddings
            (org, namespace, agent, user_id, session_id, memory_key, memory_type, scope,
             content_excerpt, embedding_model, embedding_dim, embedding)
        SELECT '{org}', '{_NS}', 'bot', {user}, '-', {key}, 'semantic', 'private',
               concat('fact ', toString(number)), 'fake', {_DIM}, {_vec("number")}
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


async def test_a_narrowed_writers_conflict_reads_cost_at_most_two_namespace_reads(
    ch: Any,
) -> None:
    from server.auth.context import set_current_auth
    from server.auth.jwt import AuthInfo
    from server.core.ch import set_query_tag
    from server.identity.domain.read_scope import WHOLE_NAMESPACE
    from server.memory.app import conflicts

    org = unique_org("conflict-cost")
    await _seed(ch, org)
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

    wide = await measured("wide", lambda: conflicts._peer_rows(ch, params))
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
        own = await measured("scoped", lambda: conflicts._peer_rows(ch, params, scope))
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
    # The pinned arm is the namespace-wide writer's read, so a narrowed write
    # costs its own scoped read on top of what any other writer pays. Without a
    # vector index that read is every vector in the namespace (a brute-force
    # stage A), the same as before read scopes existed.
    single = cost["scoped"]["read_bytes"] + cost["pinned"]["read_bytes"]
    assert single <= 2 * cost["wide"]["read_bytes"]
    assert cost["scoped"]["read_bytes"] < cost["wide"]["read_bytes"] / 5
    assert cost["bulk"]["read_bytes"] <= 2 * cost["wide_bulk"]["read_bytes"]
