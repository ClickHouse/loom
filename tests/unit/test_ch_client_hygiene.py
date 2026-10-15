"""ClickHouse client hygiene and backpressure.

Every statement carries its class deadline; an INSERT is retried after a
dropped socket only under a per-batch deduplication token; a reconnect is a
generation change that no task can trigger twice and that never closes a
client under a statement still running on it; the pool has a slot gate that
fails fast with 503 OVERLOADED and reports its waiters.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from clickhouse_connect.driver.exceptions import DatabaseError, OperationalError

import server.core.ch as ch_mod
from server.core import ch_errors, ch_writes
from server.core.errors import BackendTimeoutError, BackendUnavailableError, OverloadedError
from server.settings import settings

_NET = "Network Error: Server disconnected"


class _Result:
    column_names = ("name", "type")
    result_rows = [("a", "Int64"), ("b", "String")]


class _FakeAsync:
    """One driver client. `plan` maps a statement prefix to a list of
    behaviours consumed per call: an exception instance (raised), a float
    (seconds to hang), or None (succeed)."""

    def __init__(self, gen: int = 0, plan: dict[str, list[Any]] | None = None) -> None:
        self.gen = gen
        self.plan = plan or {}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.closed = False

    async def _act(self, kind: str, key: str, settings: dict[str, Any] | None) -> None:
        self.calls.append((kind, key, dict(settings or {})))
        for prefix, steps in self.plan.items():
            if key.startswith(prefix) and steps:
                step = steps.pop(0)
                if isinstance(step, BaseException):
                    raise step
                if isinstance(step, (int, float)) and step > 0:
                    await asyncio.sleep(float(step))
                return

    async def query(self, sql: str, parameters: Any = None, settings: Any = None) -> Any:
        await self._act("query", sql, settings)
        return _Result()

    async def command(self, sql: str, parameters: Any = None, settings: Any = None) -> Any:
        await self._act("command", sql, settings)

    async def insert(
        self,
        table: str,
        data: Any,
        column_names: Any = None,
        column_type_names: Any = None,
        settings: Any = None,
    ) -> None:
        await self._act("insert", table, settings)

    async def close(self) -> None:
        self.closed = True


def _client(monkeypatch: pytest.MonkeyPatch, fake: _FakeAsync, **over: Any) -> Any:
    for k, v in over.items():
        monkeypatch.setattr(settings, k, v)
    c = ch_mod.CHClient()
    c._client = fake
    c.fakes = [fake]

    async def _connect() -> None:
        nf = _FakeAsync(gen=len(c.fakes))
        c.fakes.append(nf)
        c._client = nf

    monkeypatch.setattr(c, "connect", _connect)
    return c


@pytest.fixture(autouse=True)
def _fresh_cache() -> Any:
    ch_mod.describe_cache_clear()
    yield
    ch_mod.describe_cache_clear()


# ---------------------------------------------------------------- classes


def test_statement_classes_follow_the_leading_keyword() -> None:
    assert ch_mod.statement_class("SELECT 1") == "read"
    assert ch_mod.statement_class("  -- note\n WITH a AS (SELECT 1) SELECT * FROM a") == "read"
    assert ch_mod.statement_class("DESCRIBE TABLE loom.memory") == "read"
    assert ch_mod.statement_class("INSERT INTO loom.t (a) VALUES (1)") == "write"
    assert ch_mod.statement_class("/* lw */ UPDATE loom.t SET a = 1 WHERE b = 2") == "write"
    assert ch_mod.statement_class("ALTER TABLE loom.t UPDATE a = 1 WHERE 1") == "ddl"
    assert ch_mod.statement_class("DELETE FROM loom.t WHERE 1") == "ddl"
    assert ch_mod.statement_class("OPTIMIZE TABLE loom.t FINAL") == "ddl"
    assert ch_mod.statement_class("CREATE TABLE IF NOT EXISTS x (a Int8) ENGINE = Memory") == "ddl"
    assert ch_mod.statement_class("SYSTEM FLUSH LOGS") == "ddl"


def test_class_deadlines_come_from_settings_and_a_scope_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ch_timeout_read_s", 30.0)
    monkeypatch.setattr(settings, "ch_timeout_write_s", 60.0)
    monkeypatch.setattr(settings, "ch_timeout_ddl_s", 300.0)
    assert (
        ch_mod.class_timeout_s("read"),
        ch_mod.class_timeout_s("write"),
        ch_mod.class_timeout_s("ddl"),
    ) == (30.0, 60.0, 300.0)
    with ch_mod.statement_timeout(1800):
        assert ch_mod.class_timeout_s("read") == 1800.0
    assert ch_mod.class_timeout_s("read") == 30.0
    monkeypatch.setattr(settings, "ch_timeout_read_s", 0.0)
    assert ch_mod.class_timeout_s("read") == 0.0
    assert "max_execution_time" not in ch_mod.statement_settings("SELECT 1")


async def test_every_statement_sends_its_class_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeAsync()
    c = _client(
        monkeypatch, fake, ch_timeout_read_s=30.0, ch_timeout_write_s=60.0, ch_timeout_ddl_s=300.0
    )
    await c.query_all("SELECT 1")
    await c.execute("ALTER TABLE loom.t UPDATE a = 1 WHERE 1")
    await c.insert("loom.t", [{"a": 1, "b": "x"}])
    by_kind = {k: s for k, _key, s in fake.calls if k != "query" or "DESCRIBE" not in _key}
    assert by_kind["query"]["max_execution_time"] == 30
    assert by_kind["command"]["max_execution_time"] == 300
    assert by_kind["insert"]["max_execution_time"] == 60
    describe = [s for k, key, s in fake.calls if key.startswith("DESCRIBE")]
    assert describe and describe[0]["max_execution_time"] == 30
    # A caller's own bound is kept.
    assert (
        ch_mod.statement_settings("SELECT 1", {"max_execution_time": 5})["max_execution_time"] == 5
    )


async def test_the_driver_read_timeout_is_the_largest_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def _fake_get_async_client(**kw: Any) -> Any:
        captured.update(kw)

        class _C:
            async def close(self) -> None: ...

        return _C()

    monkeypatch.setattr(ch_mod.clickhouse_connect, "get_async_client", _fake_get_async_client)
    monkeypatch.setattr(settings, "ch_timeout_ddl_s", 300.0)
    await ch_mod.CHClient().connect()
    assert captured["send_receive_timeout"] == 300
    # Every bound off (0) falls back to the driver's own default, never 1 s.
    monkeypatch.setattr(settings, "ch_timeout_ddl_s", 0.0)
    await ch_mod.CHClient().connect()
    assert captured["send_receive_timeout"] == 300
    monkeypatch.setattr(settings, "ch_timeout_ddl_s", 120.0)
    await ch_mod.CHClient().connect()
    assert captured["send_receive_timeout"] == 120
    # A client rebuilt inside a short scope still serves every later caller.
    with ch_mod.statement_timeout(5):
        await ch_mod.CHClient().connect()
    assert captured["send_receive_timeout"] == 120
    with ch_mod.statement_timeout(600):
        await ch_mod.CHClient().connect()
    assert captured["send_receive_timeout"] == 600


# ------------------------------------------------------------- retries


def test_transient_network_predicate_sees_the_driver_wrapper() -> None:
    assert ch_errors.is_transient_network_error(OperationalError(_NET))
    assert ch_errors.is_transient_network_error(ConnectionResetError("peer"))
    assert not ch_errors.is_transient_network_error(DatabaseError("Code: 60. Table missing"))
    assert not ch_errors.is_transient_network_error(OperationalError("Code: 241. Memory limit"))
    assert not ch_errors.is_transient_network_error(TimeoutError())  # loom's own deadline


async def test_insert_retries_a_dropped_socket_under_one_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeAsync(plan={"loom.t": [OperationalError(_NET)]})
    c = _client(monkeypatch, fake)
    rows = [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]
    await c.insert("loom.t", rows)
    first = [s for k, _t, s in fake.calls if k == "insert"]
    second = [s for k, _t, s in c.fakes[1].calls if k == "insert"]
    assert len(first) == 1 and len(second) == 1 and len(c.fakes) == 2
    assert first[0]["insert_deduplication_token"] == second[0]["insert_deduplication_token"]
    assert first[0]["insert_deduplicate"] == 1 and second[0]["insert_deduplicate"] == 1
    # A second batch of identical content is a different batch.
    await c.insert("loom.t", rows)
    third = [s for k, _t, s in c.fakes[1].calls if k == "insert"]
    assert third[1]["insert_deduplication_token"] != third[0]["insert_deduplication_token"]


async def test_exhausted_read_reconnect_is_an_actionable_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeAsync(plan={"SELECT": [OperationalError(_NET)]})
    c = _client(monkeypatch, fake)

    async def _failed_reconnect(_seen: int) -> None:
        raise OperationalError(_NET)

    monkeypatch.setattr(c, "_reconnect", _failed_reconnect)
    with pytest.raises(BackendUnavailableError) as exc:
        await c.query_all("SELECT 1")
    assert exc.value.status_code == 503
    assert exc.value.code == "BACKEND_UNAVAILABLE"
    assert exc.value.extra["outcome_unknown"] is False


async def test_async_batches_deduplicate_through_the_async_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeAsync()
    c = _client(monkeypatch, fake)
    await c.insert(
        "loom.t", [{"a": 1, "b": "x"}], settings={"async_insert": 1, "wait_for_async_insert": 0}
    )
    s = [s for k, _t, s in fake.calls if k == "insert"][0]
    assert s["async_insert"] == 1 and s["async_insert_deduplicate"] == 1
    assert s["insert_deduplication_token"] and "insert_deduplicate" not in s


def test_token_is_stable_for_a_batch_and_distinct_across_nonces() -> None:
    rows = [{"a": 1, "v": [0.1] * 1536}, {"a": 2, "v": [0.2] * 1536}]
    t1 = ch_writes.insert_dedup_token("loom.t", ["a", "v"], rows, "n1")
    assert t1 == ch_writes.insert_dedup_token("loom.t", ["a", "v"], rows, "n1")
    assert t1 != ch_writes.insert_dedup_token("loom.t", ["a", "v"], rows, "n2")
    assert t1 != ch_writes.insert_dedup_token("loom.u", ["a", "v"], rows, "n1")
    assert t1 != ch_writes.insert_dedup_token("loom.t", ["a", "v"], rows[:1], "n1")
    assert len(t1) == 40


async def test_execute_insert_gets_a_token_and_a_mutation_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeAsync()
    c = _client(monkeypatch, fake)
    await c.execute("INSERT INTO loom.t (a) SELECT 1")
    await c.execute("ALTER TABLE loom.t UPDATE a = 1 WHERE 1")
    ins, alt = (s for k, _t, s in fake.calls if k == "command")
    assert ins["insert_deduplicate"] == 1 and ins["insert_deduplication_token"]
    assert "insert_deduplication_token" not in alt


async def test_a_raw_insert_and_a_read_retry_a_dropped_socket_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeAsync(plan={"INSERT": [OperationalError(_NET)]})
    c = _client(monkeypatch, fake)
    await c.execute("INSERT INTO loom.t (a) SELECT 1")
    assert len(c.fakes) == 2
    first = [s for k, _t, s in fake.calls if k == "command"]
    second = [s for k, _t, s in c.fakes[1].calls if k == "command"]
    assert first[0]["insert_deduplication_token"] == second[0]["insert_deduplication_token"]
    # A read on the same failure is retried once on a fresh connection.
    c.fakes[1].plan = {"SELECT": [OperationalError(_NET)]}
    assert await c.query_all("SELECT 1") == [
        {"name": "a", "type": "Int64"},
        {"name": "b", "type": "String"},
    ]
    assert len(c.fakes) == 3


# ------------------------------------------------------------ deadlines


async def test_a_hung_statement_is_cut_at_the_class_deadline_and_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeAsync(plan={"SELECT": [5.0]})
    c = _client(monkeypatch, fake, ch_timeout_read_s=0.05, ch_pool_maxsize=4, ch_pool_wait_ms=500)
    before = ch_mod.pool_reading()["timeouts_total"]
    t0 = time.perf_counter()
    with pytest.raises(BackendTimeoutError) as ei:
        await c.query_all("SELECT sleep(5)")
    assert time.perf_counter() - t0 < 1.0
    assert ei.value.status_code == 504 and ei.value.code == "BACKEND_TIMEOUT"
    assert ei.value.to_detail()["statement_class"] == "read"
    assert len([1 for k, _t, _s in fake.calls if k == "query"]) == 1 and len(c.fakes) == 1
    r = ch_mod.pool_reading()
    assert r["in_use"] == 0 and r["timeouts_total"] == before + 1


# ------------------------------------------------------------ slot gate


async def test_slot_gate_fails_fast_with_503_and_counts_waiters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeAsync(plan={"SELECT slow": [0.4, 0.4]})
    c = _client(monkeypatch, fake, ch_pool_maxsize=1, ch_pool_wait_ms=60, ch_timeout_read_s=30.0)
    holder = asyncio.create_task(c.query_all("SELECT slow 1"))
    await asyncio.sleep(0.02)
    assert ch_mod.pool_reading()["in_use"] == 1
    seen_waiters: list[int] = []

    async def _watch() -> None:
        for _ in range(10):
            await asyncio.sleep(0.005)
            seen_waiters.append(ch_mod.pool_reading()["waiters"])

    watcher = asyncio.create_task(_watch())
    t0 = time.perf_counter()
    with pytest.raises(OverloadedError) as ei:
        await c.query_all("SELECT 2")
    waited = time.perf_counter() - t0
    await watcher
    assert 0.05 <= waited < 0.5
    assert ei.value.status_code == 503 and ei.value.code == "OVERLOADED"
    assert ei.value.to_detail()["retry_after_s"] == 1
    assert max(seen_waiters) == 1
    await holder
    r = ch_mod.pool_reading()
    assert r["in_use"] == 0 and r["waiters"] == 0
    # The refused statement never reached the driver.
    assert [key for k, key, _s in fake.calls if k == "query"] == ["SELECT slow 1"]


async def test_slot_gate_off_queues_instead_of_refusing(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeAsync(plan={"SELECT slow": [0.1]})
    c = _client(monkeypatch, fake, ch_pool_maxsize=1, ch_pool_wait_ms=0)
    a = asyncio.create_task(c.query_all("SELECT slow"))
    await asyncio.sleep(0.01)
    await c.query_all("SELECT 2")  # no gate: runs alongside
    await a
    assert ch_mod.pool_reading()["in_use"] == 0


# ---------------------------------------------------------- generations


async def test_many_tasks_failing_together_cost_one_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeAsync(plan={"SELECT": [OperationalError(_NET)] * 5})
    c = _client(monkeypatch, fake, ch_pool_maxsize=16, ch_pool_wait_ms=500)
    out = await asyncio.gather(*(c.query_all(f"SELECT {i}") for i in range(5)))
    assert len(out) == 5 and len(c.fakes) == 2  # one reconnect, not five
    assert c.generation == 1
    retried = [key for k, key, _s in c.fakes[1].calls if k == "query"]
    assert sorted(retried) == [f"SELECT {i}" for i in range(5)]


async def test_a_reconnect_for_a_generation_already_replaced_is_a_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = _client(monkeypatch, _FakeAsync())
    await c._reconnect(seen=0)
    assert c.generation == 1 and len(c.fakes) == 2
    await c._reconnect(seen=0)  # stale view of the world: nothing happens
    assert c.generation == 1 and len(c.fakes) == 2
    await c._reconnect()  # unconditional (boot-time callers) still works
    assert c.generation == 2 and len(c.fakes) == 3


async def test_the_old_client_closes_after_its_last_statement_not_under_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeAsync(plan={"SELECT slow": [0.2], "SELECT fail": [OperationalError(_NET)]})
    c = _client(monkeypatch, fake, ch_pool_maxsize=8, ch_pool_wait_ms=500)
    slow = asyncio.create_task(c.query_all("SELECT slow"))
    await asyncio.sleep(0.02)
    await c.query_all("SELECT fail")  # fails on generation 0, retried on 1
    assert c.generation == 1 and len(c.fakes) == 2
    assert not fake.closed, "generation 0 is still serving the slow statement"
    await slow
    assert fake.closed, "generation 0 closes when its last statement finishes"
    assert c._retired == {}


async def test_a_reconnect_with_no_statement_in_flight_closes_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeAsync(plan={"SELECT": [OperationalError(_NET)]})
    c = _client(monkeypatch, fake)
    await c.query_all("SELECT 1")
    assert fake.closed and c.generation == 1


async def test_disconnect_closes_retired_and_current(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeAsync(plan={"SELECT slow": [0.2], "SELECT fail": [OperationalError(_NET)]})
    c = _client(monkeypatch, fake)
    slow = asyncio.create_task(c.query_all("SELECT slow"))
    await asyncio.sleep(0.02)
    await c.query_all("SELECT fail")
    await c.disconnect()
    assert fake.closed and c.fakes[1].closed and c._client is None
    await slow


async def test_a_refused_insert_is_not_rerun_by_the_redescribe_ladder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.core.errors import OverloadedError

    fake = _FakeAsync(plan={"loom.t": [OverloadedError("pool full")]})
    c = _client(monkeypatch, fake)
    with pytest.raises(OverloadedError):
        await c.insert("loom.t", [{"a": 1, "b": "x"}])
    assert len([k for k, _t, _s in fake.calls if k == "insert"]) == 1


async def test_a_graph_table_insert_notes_its_namespaces(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.app import graph_write_marks

    noted: list[tuple[str, str]] = []

    class _Marks:
        def note_write(self, org: str, ns: str) -> None:
            noted.append((org, ns))

    monkeypatch.setattr(graph_write_marks, "marks", lambda: _Marks())
    fake = _FakeAsync()
    c = _client(monkeypatch, fake)
    await c.insert("loom.entity_edges", [{"org": "o", "namespace": "n", "a": 1}])
    assert noted == [("o", "n")]
