"""The list locates, then reads; the total is a count.

Reading every version of every key with its 2 KB preview under FINAL to
keep 100 rows would make `value_preview` most of a page's bytes, so
`memory.list` locates the page on the key and card columns and fetches the
previews by (memory_id, version) pairs without FINAL. A caller that wants
only the number (`count_only`) gets the two counts and no rows.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.memory import store as memory_repo
from server.memory.app import query as mq
from server.transport import mcp_schemas


class _CH:
    def __init__(self, answers: list[list[dict[str, Any]]]) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._answers = answers

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list:
        self.calls.append((sql, dict(params or {})))
        return self._answers.pop(0) if self._answers else []


_M1 = "00000000-0000-0000-0000-000000000001"
_M2 = "00000000-0000-0000-0000-000000000002"


def _located(key: str, version: int, mid: str = "", agent: str = "a") -> dict[str, Any]:
    t = datetime(2026, 9, 19, 11, 0, 0)
    return {
        "memory_id": mid or _M1,
        "org": "o",
        "namespace": "n",
        "agent": agent,
        "user_id": "-",
        "session_id": "-",
        "memory_key": key,
        "memory_type": "fact",
        "value_truncated": 0,
        "confidence": 1.0,
        "version": version,
        "pinned": False,
        "created_at": t,
        "updated_at": t,
        "expires_at": None,
        "valid_at": None,
        "tombstoned_at": None,
        "status": "active",
        "run_id": "",
        "source_trace": "",
        "tags": [],
        "entities": [],
        "content_sha256": "",
        "verified_by": "",
        "verified_at": None,
        "verified_sha256": "",
        "actor": "",
        "source_class": "asserted",
    }


@pytest.mark.asyncio
async def test_two_identities_sharing_a_key_and_version_each_get_their_own_preview() -> None:
    """The entity pass stamps one version on every row of its batch, so the
    same key under two agents can share (memory_key, version). The previews
    are keyed on memory_id: each row shows its own value, neither is dropped."""
    ch = _CH(
        [
            [_located("k", 7, _M1, agent="a"), _located("k", 7, _M2, agent="b")],
            [
                {"memory_id": _M2, "version": 7, "value_preview": '"b"'},
                {"memory_id": _M1, "version": 7, "value_preview": '"a"'},
            ],
        ]
    )
    ident = memory_repo.Identity(org="o", namespace="n", agent="*", user_id="*", session_id="*")
    rows = await memory_repo.list_by_namespace(
        ident, limit=100, preview=True, ch=ch, scope=WHOLE_NAMESPACE
    )
    assert ch.calls[1][1]["pairs"] == [(_M1, 7), (_M2, 7)]
    assert [(r.agent, r.value) for r in rows] == [("a", "a"), ("b", "b")]


@pytest.mark.asyncio
async def test_a_preview_page_locates_without_previews_then_reads_them_by_pairs() -> None:
    ch = _CH(
        [
            [_located("k2", 20, _M2), _located("k1", 10, _M1)],
            [
                {"memory_id": _M1, "version": 10, "value_preview": '"one"'},
                {"memory_id": _M2, "version": 20, "value_preview": '"two"'},
            ],
        ]
    )
    ident = memory_repo.Identity(org="o", namespace="n", agent="*", user_id="*", session_id="*")
    rows = await memory_repo.list_by_namespace(
        ident, limit=100, preview=True, ch=ch, scope=WHOLE_NAMESPACE
    )
    assert len(ch.calls) == 2
    locate_sql, locate_params = ch.calls[0]
    fetch_sql, fetch_params = ch.calls[1]
    assert "FROM loom.memory FINAL" in locate_sql and "ORDER BY created_at DESC" in locate_sql
    assert "value_preview" not in locate_sql and " value," not in locate_sql
    assert "value_len > 2048 AS value_truncated" in locate_sql
    assert locate_params["limit"] == 100
    assert "FINAL" not in fetch_sql
    assert "(memory_id, version) IN {pairs:Array(Tuple(UUID, UInt64))}" in fetch_sql
    assert "LIMIT 1 BY memory_id, version" in fetch_sql
    assert fetch_params["pairs"] == [(_M2, 20), (_M1, 10)]
    # the page keeps its order and each row its own preview
    assert [r.memory_key for r in rows] == ["k2", "k1"]
    assert rows[0].value == "two" and rows[1].value == "one"


@pytest.mark.asyncio
async def test_an_empty_page_is_one_statement_and_a_full_value_page_is_unchanged() -> None:
    ch = _CH([[]])
    ident = memory_repo.Identity(org="o", namespace="n", agent="a", user_id="-", session_id="-")
    assert (
        await memory_repo.list_by_namespace(ident, preview=True, ch=ch, scope=WHOLE_NAMESPACE) == []
    )
    assert len(ch.calls) == 1
    ch2 = _CH([[]])
    await memory_repo.list_by_namespace(ident, preview=False, ch=ch2, scope=WHOLE_NAMESPACE)
    ((sql, _),) = ch2.calls
    assert "FROM loom.memory FINAL" in sql and " value, 0 AS value_truncated" in sql


@pytest.mark.asyncio
async def test_count_only_lists_no_page(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.api.models_crud import MemoryListInput

    async def never(*a: Any, **kw: Any) -> list:
        raise AssertionError("count_only must not read the page")

    counts: list[dict[str, Any]] = []

    async def count(identity: Any, **kw: Any) -> int:
        counts.append(kw)
        return 7 if kw.get("created_since") is None else 2

    async def nsv(org: str, ns: str) -> int:
        return 1234

    monkeypatch.setattr(
        mq.memory_repo if hasattr(mq, "memory_repo") else memory_repo, "list_by_namespace", never
    )
    monkeypatch.setattr(memory_repo, "list_by_namespace", never)
    monkeypatch.setattr(memory_repo, "count_by_namespace", count)
    monkeypatch.setattr(memory_repo, "namespace_version", nsv)
    mq.reset_list_counts_cache()
    out = await mq._handle_memory_list(
        MemoryListInput(org="o", namespace="n", agent="*", count_only=True)
    )
    assert out.entries == [] and out.total == 7 and out.today == 2
    assert out.namespace_version == "1234" and out.truncated is False
    assert len(counts) == 2


def test_count_only_is_hidden_from_the_mcp_listing() -> None:
    assert "count_only" in mcp_schemas._HIDDEN_OP_PROPS["memory.list"]


@pytest.mark.parametrize("as_of", [None, datetime(2026, 9, 19, 11, 0, 0)])
async def test_the_list_waits_for_the_namespaces_memory_rows(
    monkeypatch: pytest.MonkeyPatch, as_of: datetime | None
) -> None:
    """Under Postgres a write answers at its commit: memory.list reads ClickHouse once it
    holds the memory rows the namespace committed, so the caller's own write is listed;
    an `as_of` replay too, since the history it reads holds a write only once flushed."""
    from server.memory.api.models_crud import MemoryListInput
    from server.memory.app.search import pg_validation
    from server.memory.store import pg_memory

    admitted: list[tuple[Any, ...]] = []

    async def _admit(org: str, namespaces: list[str], **kw: Any) -> bool:
        admitted.append((org, namespaces, kw.get("owed")))
        return False

    async def _listed(input: Any) -> Any:
        return mq.MemoryListOutput(entries=[], truncated=False)

    monkeypatch.setattr(pg_validation, "admit", _admit)
    monkeypatch.setattr(mq, "_handle_memory_list", _listed)
    monkeypatch.setattr(mq, "_record_read_exposure", lambda *a: None)
    await mq.handle_memory_list(MemoryListInput(org="o", namespace="n", agent="a", as_of=as_of))
    assert admitted == [("o", ["n"], pg_memory.MEMORY_ROW)]


@pytest.mark.parametrize("as_of", [None, datetime(2026, 9, 19, 11, 0, 0)])
async def test_a_get_replay_waits_for_the_namespaces_memory_rows(
    monkeypatch: pytest.MonkeyPatch, as_of: datetime | None
) -> None:
    """A point read answers from the authority's heads; an `as_of` replay reads
    ClickHouse's history, so it waits, as memory.list does, for the write that answered at
    its commit."""
    import server.memory.store as memory_repo
    from server.memory.api.models_crud import MemoryGetInput
    from server.memory.app import handlers
    from server.memory.app.search import pg_validation
    from server.memory.store import pg_memory

    admitted: list[tuple[Any, ...]] = []

    async def _admit(org: str, namespaces: list[str], **kw: Any) -> bool:
        admitted.append((org, namespaces, kw.get("owed")))
        return False

    async def _row(*_a: Any, **_k: Any) -> Any:
        return None

    monkeypatch.setattr(pg_validation, "admit", _admit)
    monkeypatch.setattr(memory_repo, "get_by_key", _row)
    identity = memory_repo.Identity(org="o", namespace="n", agent="a", user_id="-", session_id="-")
    get = MemoryGetInput(org="o", namespace="n", agent="a", memory_key="k", as_of=as_of)
    assert await handlers._point_lookup(identity, get) is None
    assert admitted == ([("o", ["n"], pg_memory.MEMORY_ROW)] if as_of else [])


async def test_history_and_a_get_by_id_replay_wait_for_the_namespaces_memory_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """memory.history and a memory.get_by_id `as_of` replay read ClickHouse's history, so
    each waits for a write that answered at its commit; a current get_by_id reads the
    authority and waits for nothing."""
    import server.memory.store as memory_repo
    from server.memory.api.models_crud import MemoryGetByIdInput
    from server.memory.api.models_temporal import MemoryHistoryInput
    from server.memory.app import curation, handlers, id_namespace
    from server.memory.app.search import pg_validation
    from server.memory.store import pg_memory

    admitted: list[tuple[Any, ...]] = []

    async def _admit(org: str, namespaces: list[str], **kw: Any) -> bool:
        admitted.append((org, namespaces, kw.get("owed")))
        return False

    async def _none(*_a: Any, **_k: Any) -> Any:
        return None

    async def _no_rows(*_a: Any, **_k: Any) -> list[Any]:
        return []

    async def _ns(*_a: Any, **_k: Any) -> str:
        return "n"

    monkeypatch.setattr(pg_validation, "admit", _admit)
    monkeypatch.setattr(memory_repo, "get_by_id", _none)
    monkeypatch.setattr(memory_repo, "list_history", _no_rows)
    monkeypatch.setattr(id_namespace, "namespace_for_id", _ns)
    await curation.handle_memory_history(
        MemoryHistoryInput(org="o", namespace="n", agent="a", memory_key="k")
    )
    assert admitted == [("o", ["n"], pg_memory.MEMORY_ROW)]
    mid = "00000000-0000-0000-0000-000000000001"
    await handlers.handle_memory_get_by_id(MemoryGetByIdInput(org="o", memory_id=mid))
    assert len(admitted) == 1
    replay = MemoryGetByIdInput(org="o", memory_id=mid, as_of=datetime(2026, 9, 19, 11, 0, 0))
    await handlers.handle_memory_get_by_id(replay)
    assert admitted[1:] == [("o", ["n"], pg_memory.MEMORY_ROW)]


@pytest.mark.parametrize("behind", [False, True])
async def test_history_reads_sequentially_once_the_barrier_waited(
    monkeypatch: pytest.MonkeyPatch, behind: bool
) -> None:
    """A wait at the barrier means the flush landed moments ago: history then reads with
    sequential consistency, so a lagging replica does not miss it."""
    import server.memory.store as memory_repo
    from server.core import ch as core_ch
    from server.memory.api.models_temporal import MemoryHistoryInput
    from server.memory.app import curation
    from server.memory.app.search import pg_validation

    async def _admit(*_a: Any, **_k: Any) -> bool:
        return behind

    seen: list[bool] = []

    async def _history(*_a: Any, **_k: Any) -> list[Any]:
        seen.append(core_ch.sequential_reads_active())
        return []

    monkeypatch.setattr(pg_validation, "admit", _admit)
    monkeypatch.setattr(memory_repo, "list_history", _history)
    await curation.handle_memory_history(
        MemoryHistoryInput(org="o", namespace="n", agent="a", memory_key="k")
    )
    assert seen == [behind]
