"""The authority's head reads and the usage fold's head update
(`server.memory.store.pg_memory`): the statement each builds for its filters, every
`$n` bound to an argument, and text Postgres cannot store never sent."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any

import pytest

from server.core import pg
from server.identity.domain.read_scope import WHOLE_NAMESPACE, ReadFilters
from server.memory.store import pg_memory

NUL = "a\x00b"
OWN_SESSION = ReadFilters(scope="own_session", user_id="u", session_id="s")
OWN_USER = ReadFilters(scope="own_user", user_id="u", session_id=None)


class _PG:
    """Records each statement and answers it with `rows`."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append((sql, args))
        return self.rows

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        self.calls.append((sql, args))
        return self.rows[0] if self.rows else None

    async def execute(self, sql: str, *args: Any) -> None:
        self.calls.append((sql, args))

    def only(self) -> tuple[str, tuple[Any, ...]]:
        (call,) = self.calls
        sql, args = call
        # Every placeholder names an argument, and every argument is used.
        assert {int(n) for n in re.findall(r"\$(\d+)", sql)} == set(range(1, len(args) + 1))
        return call


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> _PG:
    store = _PG()
    monkeypatch.setattr(pg, "fetch", store.fetch)
    monkeypatch.setattr(pg, "fetchrow", store.fetchrow)
    monkeypatch.setattr(pg, "execute", store.execute)
    return store


def _head(**over: Any) -> dict[str, Any]:
    row = {"org": "o", "namespace": "n", "agent": "a", "user_id": "u", "session_id": "s"}
    return {**row, "memory_key": "k", "memory_id": str(uuid.uuid4()), "version": 1, **over}


@pytest.mark.asyncio
async def test_heads_for_identity_opens_the_wildcard_axes(fake: _PG) -> None:
    fake.rows = [{"memory_key": "k", "row": pg_memory.encode_row(_head())}]
    heads = await pg_memory.heads_for_identity(
        ("o", "n", "*", "-", "-"), ["k", NUL], scope=OWN_SESSION, exclude_high_sensitivity=True
    )
    assert list(heads) == ["k"]
    sql, args = fake.only()
    assert args[2] == ["k"]  # a key with a NUL is in no head
    assert "agent =" not in sql and "AND user_id = $4 AND session_id = $5" in sql
    assert "IS DISTINCT FROM 'high'" in sql and "tombstoned_at' IS NULL" in sql


@pytest.mark.asyncio
async def test_heads_for_identity_narrows_each_named_axis(fake: _PG) -> None:
    await pg_memory.heads_for_identity(
        ("o", "n", "a", "u", "s"), ["k"], scope=WHOLE_NAMESPACE, retained_only=True
    )
    sql, args = fake.only()
    assert args[3:] == ("a", "u", "s")
    assert "AND agent = $4 AND user_id = $5 AND session_id = $6" in sql
    assert "NOT (row->>'sensitivity' IS NOT DISTINCT FROM 'high'" in sql


@pytest.mark.asyncio
async def test_heads_for_identity_reads_nothing_for_unstorable_text(fake: _PG) -> None:
    assert (
        await pg_memory.heads_for_identity(("o", "n", "a", "u", "s"), [NUL], scope=OWN_USER) == {}
    )
    assert (
        await pg_memory.heads_for_identity((NUL, "n", "a", "u", "s"), ["k"], scope=OWN_USER) == {}
    )
    assert fake.calls == []


@pytest.mark.asyncio
async def test_a_page_of_live_heads_narrows_and_continues_after_a_key(fake: _PG) -> None:
    fake.rows = [{"row": pg_memory.encode_row(_head())}]
    after = ("o", "n", "a", "u", "s", "k0")
    page = await pg_memory.live_heads_in("o", "n", user_id="u", agent="a", after=after, limit=5)
    assert [h["memory_key"] for h in page] == ["k"]
    sql, args = fake.only()
    assert args == ("o", "n", 5, "u", "a", "a", "u", "s", "k0")
    assert "(agent, user_id, session_id, memory_key) > ($6, $7, $8, $9)" in sql


@pytest.mark.asyncio
async def test_a_first_page_of_live_heads_reads_the_whole_namespace(fake: _PG) -> None:
    await pg_memory.live_heads_in("o", "n")
    sql, args = fake.only()
    assert args == ("o", "n", 1000) and "user_id =" not in sql and ") >" not in sql


@pytest.mark.asyncio
@pytest.mark.parametrize("servable", [True, False])
async def test_chunk_heads_group_each_writers_values(fake: _PG, servable: bool) -> None:
    ident = {"namespace": "n", "agent": "a", "user_id": "u", "memory_key": "k"}
    fake.rows = [
        {**ident, "value": '{"x": 1}', "memory_type": "semantic"},
        {**ident, "value": "plain text", "memory_type": None},
    ]
    out = await pg_memory.chunk_heads("o", [("n", "a", "u", "k")], servable=servable)
    assert out == {("n", "a", "u", "k"): [({"x": 1}, "semantic"), ("plain text", "")]}
    sql, _ = fake.only()
    # Search serves only unexpired heads it may show; the check of a writer reads them all.
    assert ("rejected" in sql) is servable
    assert await pg_memory.chunk_heads("o", []) == {}


@pytest.mark.asyncio
async def test_parked_heads_page_by_namespace_and_seq(fake: _PG) -> None:
    mid = uuid.uuid4()
    fake.rows = [{"namespace": "n", "seq": 7, "memory_id": mid, "value_text": "v"}]
    heads = await pg_memory.parked_heads("o", "n", after=("n", 3), limit=10)
    assert heads == [pg_memory.ParkedHead("n", 7, str(mid), "v")]
    sql, args = fake.only()
    assert args == ("o", 10, "n", "n", 3)
    assert "d.namespace = $3" in sql and "(d.namespace, d.seq) > ($4, $5::bigint)" in sql


@pytest.mark.asyncio
async def test_parked_heads_of_an_org_read_every_namespace(fake: _PG) -> None:
    await pg_memory.parked_heads("o", None, after=None, limit=10)
    sql, args = fake.only()
    assert args == ("o", 10) and "d.namespace =" not in sql
    assert await pg_memory.parked_heads("o", NUL, after=None, limit=10) == []
    assert await pg_memory.parked_heads(NUL, None, after=None, limit=10) == []


@pytest.mark.asyncio
async def test_head_by_key_under_the_wildcard_agent_reads_any_agent(fake: _PG) -> None:
    assert await pg_memory.head_by_key(("o", "n", "*", "u", "s", "k"), scope=OWN_USER) is None
    sql, args = fake.only()
    assert args == ("o", "n", "k", "u") and "agent =" not in sql and "user_id = $4" in sql


@pytest.mark.asyncio
async def test_head_by_key_of_one_identity(fake: _PG) -> None:
    fake.rows = [{"row": pg_memory.encode_row(_head())}]
    head = await pg_memory.head_by_key(
        ("o", "n", "a", "u", "s", "k"),
        scope=WHOLE_NAMESPACE,
        include_tombstoned=True,
        include_expired=False,
    )
    assert head is not None and head["memory_key"] == "k"
    sql, args = fake.only()
    assert args == ("o", "n", "k", "a", "u", "s")
    assert "tombstoned_at' IS NULL" not in sql and "AND NOT" in sql  # expired heads left out
    assert await pg_memory.head_by_key(("o", "n", "a", "u", "s", NUL), scope=OWN_USER) is None


@pytest.mark.asyncio
async def test_the_search_barrier_reads_the_namespaces_asked_for(fake: _PG) -> None:
    fake.rows = [{"scope": "n", "generation": 4}]
    assert await pg_memory.unsearchable_through("o", ["n", NUL]) == {"n": 4}
    sql, args = fake.only()
    assert args[2] == ["n"] and "d.namespace = ANY($3::text[])" in sql
    fake.calls.clear()
    await pg_memory.unsearchable_through("o", None)
    sql, args = fake.only()
    assert len(args) == 2 and "ANY(" not in sql
    assert await pg_memory.unsearchable_through(NUL, None) == {}


@pytest.mark.asyncio
async def test_head_by_id_answers_none_for_an_id_it_cannot_hold(fake: _PG) -> None:
    assert await pg_memory.head_by_id("not-a-uuid", scope=OWN_USER, org="o", namespace="n") is None
    mid = str(uuid.uuid4())
    assert await pg_memory.head_by_id(mid, scope=OWN_USER, org=NUL, namespace="n") is None
    assert fake.calls == []
    fake.rows = [{"row": pg_memory.encode_row(_head(memory_id=mid))}]
    head = await pg_memory.head_by_id(mid, scope=OWN_USER, org="o", namespace="n")
    assert head is not None and str(head["memory_id"]) == mid
    _, args = fake.only()
    assert args == (uuid.UUID(mid), "o", "n", "u")


@pytest.mark.asyncio
async def test_erased_ids_and_decision_writers_skip_what_cannot_be_stored(fake: _PG) -> None:
    mid = uuid.uuid4()
    assert await pg_memory.erased_ids("o", NUL, [str(mid)]) == set()
    assert await pg_memory.erased_ids("o", "n", []) == set()
    assert await pg_memory.live_decision_writers("o", "n", [NUL]) == []
    assert fake.calls == []
    fake.rows = [{"memory_id": mid}]
    assert await pg_memory.erased_ids("o", "n", [str(mid)]) == {str(mid)}
    fake.calls.clear()
    fake.rows = [{"memory_key": "k", "agent": "a", "user_id": "u", "session_id": "s"}]
    assert await pg_memory.live_decision_writers("o", "n", ["k", NUL]) == [("k", "a", "u", "s")]
    _, args = fake.only()
    assert args == ("o", "n", ["k"])


@pytest.mark.asyncio
async def test_the_namespaces_of_an_id_are_read_only_for_a_real_id(fake: _PG) -> None:
    assert await pg_memory.namespaces_of_id("o", "not-a-uuid") == []
    assert await pg_memory.namespaces_of_id(NUL, str(uuid.uuid4())) == []
    assert fake.calls == []
    fake.rows = [{"namespace": "n1"}, {"namespace": "n2"}]
    assert await pg_memory.namespaces_of_id("o", str(uuid.uuid4())) == ["n1", "n2"]
    fake.only()


@pytest.mark.asyncio
async def test_key_heads_across_sessions_read_each_key_once(fake: _PG) -> None:
    assert (
        await pg_memory.key_heads_across_sessions("o", "n", "a", "u", [NUL], scope=OWN_USER) == []
    )
    assert fake.calls == []
    fake.rows = [{"memory_key": "k", "deleted": False}]
    rows = await pg_memory.key_heads_across_sessions(
        "o", "n", "a", "u", ["k", "k"], scope=OWN_SESSION
    )
    assert rows == [{"memory_key": "k", "deleted": False}]
    sql, args = fake.only()
    assert args[4] == ["k"] and "user_id = $6 AND session_id = $7" in sql


@pytest.mark.asyncio
async def test_head_versions_answer_by_identity_and_never_bind_an_unstorable_key(
    fake: _PG,
) -> None:
    """Each head's version comes back keyed by identity; a key Postgres cannot store is
    never sent. That a deleted head is read too is proven against a real Postgres."""
    fake.rows = [_head(version=7)]
    assert await pg_memory.head_versions(
        [("o", "n", "a", "u", "s", "k"), ("o", "n", "a", "u", "s", NUL)]
    ) == {("o", "n", "a", "u", "s", "k"): 7}
    _, args = fake.only()
    assert args[5] == ["k"]
    fake.calls.clear()
    assert await pg_memory.head_versions([("o", "n", "a", "u", "s", NUL)]) == {}
    assert fake.calls == []


@pytest.mark.asyncio
async def test_heads_by_keys_key_each_head_by_identity(fake: _PG) -> None:
    """Each head comes back decoded and keyed by identity; that a deleted head is left
    out unless asked is proven against a real Postgres."""
    created = datetime(2026, 1, 1, 12)
    fake.rows = [{"row": pg_memory.encode_row(_head(version=3, created_at=created))}]
    heads = await pg_memory.heads_by_keys(
        [("o", "n", "a", "u", "s", "k"), ("o", "n", "a", "u", "s", NUL)]
    )
    assert heads[("o", "n", "a", "u", "s", "k")]["created_at"] == created
    _, args = fake.only()
    assert args[5] == ["k"]  # a key Postgres cannot store is never sent


@pytest.mark.asyncio
async def test_the_usage_fold_skips_an_org_or_namespace_postgres_cannot_store(fake: _PG) -> None:
    """A NUL or a lone surrogate in a row's org or namespace would fail the whole batch,
    and the fold would stall on it each pass; such a row holds no head, so it is left
    out."""
    mid = str(uuid.uuid4())
    lone = "a\ud800"
    await pg_memory.fold_usage(
        [(NUL, "n", mid, 1, 1, None), ("o", NUL, mid, 1, 1, None), ("o", lone, mid, 1, 1, None)]
    )
    assert fake.calls == []
    await pg_memory.fold_usage([(NUL, "n", mid, 1, 1, None), ("o", "n", mid, 2, 3, None)])
    _, args = fake.only()
    assert args[:3] == (["o"], ["n"], [uuid.UUID(mid)])
