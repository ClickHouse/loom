"""The `pg_knowledge.delete` erasure step's own logic: the retry after a deadlock, the
user scope's namespace locks, and the wait for an earlier run's publishers."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import asyncpg
import pytest

from server.core import ch, pg
from server.erasure.store import steps_pg
from server.settings import settings


def test_a_deadlocked_run_runs_again_and_waits_for_what_it_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs: list[int] = []
    waited: list[list[float]] = []

    async def _once(org: str, namespaces: Any, user_id: Any) -> tuple[int, list[float]]:
        runs.append(1)
        if len(runs) == 1:
            raise asyncpg.exceptions.DeadlockDetectedError("deadlock detected")
        return 3, [2.5]

    async def _wait(leases: list[float]) -> None:
        waited.append(leases)

    monkeypatch.setattr(steps_pg, "_erase_knowledge_once", _once)
    monkeypatch.setattr(pg, "wait_out", _wait)
    assert asyncio.run(steps_pg._erase_knowledge("o", ["ns"], None)) == 3
    assert len(runs) == 2 and waited == [[2.5]]


class _Conn:
    """Answers the namespace reads of a user's scope in turn, and records locks."""

    def __init__(self, scopes: list[set[str]], quiet_left: float | None = None) -> None:
        self.scopes, self.locked, self.quiet_left = scopes, [], quiet_left
        self.reads: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, str]]:
        return [{"namespace": n} for n in sorted(self.scopes.pop(0))]

    async def execute(self, sql: str, *args: Any) -> None:
        assert "pg_advisory_xact_lock(" in sql
        self.locked.append(args[1])

    async def fetchval(self, sql: str, *args: Any) -> float | None:
        self.reads.append((sql, args))
        return self.quiet_left


def test_a_user_scope_locks_each_namespace_it_reaches_until_it_reaches_no_more() -> None:
    """A commit landed while the first namespaces were locked put the user's document in
    another: that one is locked too, and the read repeats until nothing new turns up."""
    conn = _Conn([{"b", "d"}, {"a", "b", "d"}, {"a", "b", "d"}])
    asyncio.run(steps_pg._lock_user_knowledge(conn, "o", "alice"))
    assert conn.locked == ["b", "d", "a"]


@pytest.mark.parametrize(("left", "expected"), [(None, []), (30.0, [30.0])])
def test_an_earlier_run_that_stopped_before_its_wait_is_waited_for(
    monkeypatch: pytest.MonkeyPatch, left: float | None, expected: list[float]
) -> None:
    """Its evidence holds when its publishers go quiet, the insert bound included, which
    `pg.wait_out` adds again."""
    monkeypatch.setattr(pg, "insert_bound_s", lambda: 5.0)
    conn = _Conn([], quiet_left=None if left is None else left + 5.0)
    assert (
        asyncio.run(steps_pg._earlier_run(conn, "o", ["ns"], "loom.erased_documents")) == expected
    )
    sql, args = conn.reads[0]
    assert "loom.erased_documents" in sql and args == ("o", ["ns"])
    asyncio.run(
        steps_pg._earlier_run(conn, "o", None, "loom.erased_documents")
    )  # a user's: the org's
    assert conn.reads[1][1] == ("o",)


def test_the_evidence_read_pages_each_namespace_by_its_key_and_reads_sequentially(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never the whole scope in memory: each namespace in pages along the table's key
    (source URI, then id), the next after the last key; a user's scope first reads the
    namespaces holding the user's rows."""
    monkeypatch.setattr(settings, "pg_erasure_evidence_page_rows", 2)
    asked: list[tuple[str, str, str, bool]] = []
    answers = {
        "a": [
            [{"source_uri": "u1", "id": "1"}, {"source_uri": "u2", "id": "2"}],
            [{"source_uri": "u3", "id": "3"}],
        ],
        "b": [[]],
    }

    class _CH:
        async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, str]]:
            if "SELECT DISTINCT namespace" in sql:
                return [{"namespace": "a"}, {"namespace": "b"}]
            asked.append((params["ns"], params["uri"], params["id"], ch.sequential_reads_active()))
            assert "namespace = {ns:String}" in sql and "ORDER BY source_uri, id" in sql
            return answers[params["ns"]].pop(0)

    async def collect(namespaces: Any, user: Any) -> list[list[tuple[str, str]]]:
        return [p async for p in steps_pg.documents_in_clickhouse("o", namespaces, user, ch=_CH())]

    got = asyncio.run(collect(None, "alice"))
    assert got == [[("a", "1"), ("a", "2")], [("a", "3")]]
    assert asked == [("a", "", "", True), ("a", "u2", "2", True), ("b", "", "", True)]


def test_a_namespace_scope_pages_each_named_namespace_with_a_bound_the_key_analysis_uses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No namespace read first; each named one is paged, and the plain `source_uri >=`
    bound (which the key analysis uses, unlike the tuple) is in every page's statement."""
    monkeypatch.setattr(settings, "pg_erasure_evidence_page_rows", 5)
    seen: list[str] = []

    class _CH:
        async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, str]]:
            assert "SELECT DISTINCT namespace" not in sql
            assert "source_uri >= {uri:String}" in sql and "user_id" not in sql
            seen.append(params["ns"])
            return [{"source_uri": "u", "id": params["ns"]}]

    async def collect() -> list[list[tuple[str, str]]]:
        return [p async for p in steps_pg.documents_in_clickhouse("o", ["b", "a"], None, ch=_CH())]

    assert asyncio.run(collect()) == [[("a", "a")], [("b", "b")]]
    assert seen == ["a", "b"]


def test_the_erasure_reads_each_named_namespace_once_with_an_idle_limit_for_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scope naming a namespace twice is read for evidence once, in a transaction whose
    idle limit covers that read."""
    read: list[Any] = []
    executed: list[str] = []

    class _ErasureConn:
        async def execute(self, sql: str, *args: Any) -> None:
            executed.append(sql)

        async def fetch(self, sql: str, *args: Any) -> list[Any]:
            return []

        async def fetchval(self, sql: str, *args: Any) -> int | None:
            # A count always answers, 0 when nothing matched (the jobs deleted).
            return 0 if "count(*)" in sql else None

        @contextlib.asynccontextmanager
        async def transaction(self) -> Any:
            yield

    conn = _ErasureConn()

    @contextlib.asynccontextmanager
    async def dedicated() -> Any:
        yield conn

    async def evidence(org: str, namespaces: Any, user_id: Any, **_: Any) -> Any:
        read.append(namespaces)
        for page in ():
            yield page

    monkeypatch.setattr(pg, "dedicated", dedicated)
    monkeypatch.setattr(steps_pg, "documents_in_clickhouse", evidence)
    monkeypatch.setattr(steps_pg, "_evidence_idle_ms", lambda: 1234)
    asyncio.run(steps_pg._erase_knowledge_once("o", ["a", "a"], None))
    assert read == [["a"]]
    assert "SET LOCAL idle_in_transaction_session_timeout = 1234" in executed


@pytest.mark.parametrize(
    ("read_s", "retries", "pool_ms", "expected"),
    [
        (30.0, 2, 10_000, 100_000),  # three attempts of 30 s, and 10 s of reconnects
        (1.0, 1, 20_000, 20_000),  # never below the pool's own limit
        (0.0, 2, 10_000, 0),  # reads unbounded: no idle limit
    ],
)
def test_the_erasure_idle_limit_covers_every_attempt_of_one_read(
    monkeypatch: pytest.MonkeyPatch, read_s: float, retries: int, pool_ms: int, expected: int
) -> None:
    monkeypatch.setattr(ch, "class_timeout_s", lambda cls: read_s)
    monkeypatch.setattr(settings, "ch_replica_bound_retries", retries)
    monkeypatch.setattr(pg, "idle_timeout_ms", lambda: str(pool_ms))
    assert steps_pg._evidence_idle_ms() == expected


def test_the_knowledge_erasure_deletes_the_scope_s_dataset_catalog_rows_last(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The catalog rows go after the jobs, in the same transaction, and count toward what
    the step removed."""
    deleted: list[str] = []

    class _Conn:
        async def execute(self, sql: str, *args: Any) -> None:
            return None

        async def fetch(self, sql: str, *args: Any) -> list[Any]:
            return []

        async def fetchval(self, sql: str, *args: Any) -> int | None:
            for table in ("loom.knowledge_jobs", "loom.knowledge_datasets"):
                if f"DELETE FROM {table}" in sql:
                    deleted.append(table)
                    return 2 if table.endswith("datasets") else 1
            return 0 if "count(*)" in sql else None

        @contextlib.asynccontextmanager
        async def transaction(self) -> Any:
            yield

    @contextlib.asynccontextmanager
    async def dedicated() -> Any:
        yield _Conn()

    async def evidence(*a: Any, **k: Any) -> Any:
        for page in ():
            yield page

    monkeypatch.setattr(pg, "dedicated", dedicated)
    monkeypatch.setattr(steps_pg, "documents_in_clickhouse", evidence)
    monkeypatch.setattr(steps_pg, "_evidence_idle_ms", lambda: 1)
    removed, _ = asyncio.run(steps_pg._erase_knowledge_once("o", ["a"], None))
    assert deleted == ["loom.knowledge_jobs", "loom.knowledge_datasets"] and removed == 3
