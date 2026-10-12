"""A writer narrowed to one user pays for its own rows' vectors and the pinned
rows' vectors in the conflict check, not for every vector in the namespace.
Measured from system.query_log on the configured ClickHouse; skips without
LOOM_CH_HOST."""

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
    """20 users x 500 rows, every 50th row pinned."""
    user = f"concat('u', toString(number % {_USERS}))"
    key = "concat('k', toString(number))"
    await ch.execute(
        f"""
        INSERT INTO loom.memory
            (org, namespace, agent, user_id, session_id, memory_key, memory_type,
             value, version, pinned)
        SELECT '{org}', '{_NS}', 'bot', {user}, '-', {key}, 'semantic',
               concat('fact ', toString(number)), 1, number % 50 = 0
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
        WHERE type = 'QueryFinish' AND log_comment LIKE {tag:String}
          AND query LIKE '%cosineDistance%'
        ORDER BY event_time_microseconds
        """,
        params={"tag": f"{tag}%"},
    )


@pytest.fixture
async def ch() -> Any:
    from server.core.ch import CHClient

    c = CHClient()
    await c.connect()
    yield c
    await c.disconnect()


async def test_a_narrowed_writers_conflict_statements_read_a_bounded_slice(ch: Any) -> None:
    from server.auth.context import set_current_auth
    from server.auth.jwt import AuthInfo
    from server.core.ch import set_query_tag
    from server.memory.app import conflicts

    org = unique_org("conflict-cost")
    await _seed(ch, org)
    # Rounded so four facts' vectors fit in ClickHouse's HTTP form-field limit.
    emb = [round(float(x), 3) for x in (await ch.query_one(f"SELECT {_vec('7')} AS v"))["v"]]
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
    tag = f"it.conflict_cost.{secrets.token_hex(4)}"
    try:
        set_query_tag(tag)
        params = {
            "org": org,
            "ns": _NS,
            "mt": "semantic",
            "self_key": "new",
            "emb": emb,
            "max_dist": 2.0,
            "dim": _DIM,
        }
        single = await conflicts._foreign_pinned_rows(ch, params, conflicts._scope_or_none(), [])
        items = [("semantic", f"new{i}", emb, 2.0) for i in range(4)]
        batched = await conflicts.candidate_rows_many(
            org=org, namespace=_NS, items=items, batch=4, ch=ch
        )
    finally:
        set_query_tag("")
        set_current_auth(None)

    assert len(single) == 10 and all(r["pinned"] for r in single)
    for rows in batched.values():
        assert sum(1 for r in rows if r["user_id"] == "u3" and not r["pinned"]) == 10
        assert any(r["pinned"] and r["user_id"] != "u3" for r in rows)

    logged = await _logged(ch, tag)

    # The logged text is cut short (the vectors are inlined); the head tells them apart.
    def head(q: dict[str, Any]) -> str:
        return " ".join(str(q["query"])[:400].split())

    kinds = {
        "single pinned": [q for q in logged if "qidx" not in head(q)],
        "batched candidates": [q for q in logged if "qidx" in head(q) and "JOIN (" not in head(q)],
        "batched pinned": [q for q in logged if "qidx" in head(q) and "JOIN (" in head(q)],
    }
    total_rows = 2 * _USERS * _ROWS_PER_USER  # memory + memory_embeddings
    vector_bytes = _USERS * _ROWS_PER_USER * _DIM * 2  # BFloat16
    for name, qs in kinds.items():
        assert len(qs) == 1, name
        q = qs[0]
        print(f"{name}: {q['read_rows']} rows, {q['read_bytes']} B, {q['memory_usage']} B mem")
        # A whole-namespace read is `total_rows` rows and `vector_bytes` of vectors. The
        # pinned arms read whole granules around every pinned key (every 50th row here),
        # so their share depends on the part layout; the regression read all of it.
        bound = vector_bytes / 5 if name == "batched candidates" else vector_bytes / 2
        assert int(q["read_bytes"]) < bound, name
    assert int(kinds["batched candidates"][0]["read_rows"]) < total_rows / 5
