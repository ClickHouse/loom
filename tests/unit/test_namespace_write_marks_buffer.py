"""loom.namespace_write_marks is written from an in-process
max per (org, namespace, plane), flushed as one INSERT per interval, instead
of one part per source insert through a view on each plane table.
CH-free: the flush lands in a stub client."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from server.core import namespace_write_marks as m
from server.core import request_tail
from server.core.ch_writes import committed_insert_settings
from tests.unit.cross_replica_fake import in_request


class _CH:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.inserts: list[tuple[str, list[dict[str, Any]], Any]] = []
        self.rows = rows or []

    async def insert(self, table, rows, settings=None, **kw):
        self.inserts.append((table, rows, settings))

    async def query_all(self, sql, params=None):
        return self.rows


@pytest.fixture(autouse=True)
def _clean():
    m._reset_for_tests()
    yield
    m._reset_for_tests()


def test_every_plane_table_is_mapped() -> None:
    assert m.PLANE_BY_TABLE == {
        "loom.memory": "memory",
        "loom.memory_chunks": "chunk",
        "loom.memory_evidence": "evidence",
        "loom.memory_records": "record",
        "loom.session_summaries": "summary",
    }


def test_observe_insert_records_one_mark_per_distinct_namespace_and_plane() -> None:
    rows = [
        {"org": "o", "namespace": "a", "memory_key": "k1"},
        {"org": "o", "namespace": "a", "memory_key": "k2"},
        {"org": "o", "namespace": "b", "memory_key": "k3"},
        {"org": "o", "namespace": "", "memory_key": "k4"},  # unknown namespace -> org-wide sentinel
        {"org": "", "namespace": "x"},  # no org: ignored
    ]
    m.observe_insert("loom.memory", rows)
    m.observe_insert("loom.memory_chunks", rows[:1])
    m.observe_insert("loom.audit_log", rows)  # not a plane table
    m.observe_insert(
        "loom.namespace_write_marks", [{"org": "o", "namespace": "a"}]
    )  # the flush itself
    assert set(m.pending()) == {
        ("o", "a", "memory"),
        ("o", "b", "memory"),
        ("o", "*", "memory"),
        ("o", "a", "chunk"),
    }


def test_touch_keeps_the_maximum() -> None:
    t0 = datetime(2026, 9, 14, 23, 0, 0)
    m.touch("o", "a", "memory", at=t0)
    m.touch("o", "a", "memory", at=t0 - timedelta(seconds=5))
    m.touch("o", "a", "memory", at=t0 + timedelta(seconds=5))
    assert m.pending()[("o", "a", "memory")] == t0 + timedelta(seconds=5)


def test_flush_writes_one_insert_and_empties_the_buffer() -> None:
    ch = _CH()
    m.touch("o", "a", "memory")
    m.touch("o", "b", "chunk")
    n = asyncio.run(m.flush_now(ch=ch))
    assert n == 2 and m.pending() == {}
    assert len(ch.inserts) == 1
    table, rows, _ = ch.inserts[0]
    assert table == m.TABLE
    assert sorted((r["org"], r["namespace"], r["plane"]) for r in rows) == [
        ("o", "a", "memory"),
        ("o", "b", "chunk"),
    ]
    assert all(isinstance(r["last_write"], datetime) for r in rows)
    assert asyncio.run(m.flush_now(ch=ch)) == 0 and len(ch.inserts) == 1


def test_flush_failure_is_logged_not_raised() -> None:
    class _Broken(_CH):
        async def insert(self, *a, **k):
            raise RuntimeError("boom")

    m.touch("o", "a", "memory")
    assert asyncio.run(m.flush_now(ch=_Broken())) == 0
    # kept: the process still reads its own write, and the next flush lands it
    assert list(m.pending()) == [("o", "a", "memory")]
    ch = _CH()
    assert asyncio.run(m.flush_now(ch=ch)) == 1 and m.pending() == {}


def test_a_mark_stays_readable_while_its_flush_is_in_flight() -> None:
    """The insert is awaited with the mark still buffered, and a newer write
    to the same plane during the insert is kept for the next flush."""
    seen: list[dict] = []

    class _Slow(_CH):
        async def insert(self, table, rows, **k):
            seen.append(m.pending())
            m.touch("o", "a", "memory", at=datetime(2099, 1, 1))
            return await super().insert(table, rows, **k)

    m.touch("o", "a", "memory", at=datetime(2030, 1, 1))
    assert asyncio.run(m.flush_now(ch=_Slow())) == 1
    assert seen == [{("o", "a", "memory"): datetime(2030, 1, 1)}]
    assert m.pending() == {("o", "a", "memory"): datetime(2099, 1, 1)}


def test_timer_coalesces_touches_within_the_interval(settings_override) -> None:
    """Two writes inside one interval arm one timer and land as one part."""
    settings_override(namespace_write_marks_flush_interval_s=0.05)
    ch = _CH()

    async def run():
        import server.core.namespace_write_marks as mod

        # the timer's flush resolves the client itself: point it at the stub
        mod._default_client = lambda: ch  # type: ignore[assignment]
        try:
            m.touch("o", "a", "memory")
            m.touch("o", "a", "memory")
            m.touch("o", "b", "memory")
            assert m._timer is not None
            await asyncio.sleep(0.2)
            assert m._timer is None and m.pending() == {}
        finally:
            del mod._default_client
            from server.core.repo_base import _default_client

            mod._default_client = _default_client

    asyncio.run(run())
    assert len(ch.inserts) == 1 and len(ch.inserts[0][1]) == 2


def test_last_writes_folds_this_process_pending_marks_in() -> None:
    db_ts = datetime(2026, 9, 14, 22, 0, tzinfo=UTC)
    ch = _CH([{"namespace": "a", "last_write": db_ts.replace(tzinfo=None)}])
    later = datetime(2026, 9, 14, 23, 0)
    m.touch("o", "a", "memory", at=later)
    m.touch("o", "c", "memory", at=later)  # not asked for: not returned
    m.touch("p", "a", "memory", at=later)  # another org: not returned
    out = asyncio.run(m.last_writes("o", ["a", "b"], ch=ch))
    assert out == {"a": later.replace(tzinfo=UTC)}
    ch = _CH([{"namespace": "a", "last_write": later}])
    m._reset_for_tests()
    m.touch("o", "a", "memory", at=later - timedelta(hours=1))  # older than the row: the row wins
    assert asyncio.run(m.last_writes("o", ["a"], ch=ch))["a"] == later.replace(tzinfo=UTC)


def test_client_insert_feeds_the_buffer(monkeypatch) -> None:
    """The hook sits in CHClient.insert, after a successful write, followed by
    the registered observers; an observer that raises never fails the write."""
    from server.core import ch as chmod
    from server.core import rmt_carry

    monkeypatch.setattr(rmt_carry, "carries", lambda _table: False)  # its own tests
    seen: list[tuple[str, int]] = []
    monkeypatch.setattr(m, "observe_insert", lambda table, rows: seen.append((table, len(rows))))

    def _boom(table: str, rows: list) -> None:
        raise RuntimeError("observer bug")

    observed: list[str] = []
    monkeypatch.setattr(chmod, "_INSERT_OBSERVERS", [_boom, lambda t, r: observed.append(t)])

    class _Driver:
        async def insert(self, *a, **k):
            return None

    client = chmod.CHClient.__new__(chmod.CHClient)
    client._client = _Driver()

    async def _types(table, columns):
        return None

    async def _retry(_name, fn, **_kw):  # the retry wrapper passes cls=/idempotent=
        return await fn()

    client._column_type_names = _types  # type: ignore[method-assign]
    client._with_retry = _retry  # type: ignore[method-assign]
    asyncio.run(client.insert("loom.memory", [{"org": "o", "namespace": "a", "memory_key": "k"}]))
    assert seen == [("loom.memory", 1)] and observed == ["loom.memory"]


def test_the_refreshable_rollups_and_the_rollup_reader() -> None:
    """The refreshable views replace the insert-time fan-out; the cost reader
    moves to the refreshed table; the settle and the watermark are in the
    view's SELECT."""
    from pathlib import Path

    from tests.unit.schema_ddl import column_names, ddl

    root = Path(__file__).resolve().parents[2]
    for view, target in (
        ("agent_cost_rollup_refresh", "agent_cost_rollup_daily"),
        ("memory_exposure_breadth_hourly_refresh", "memory_exposure_breadth_hourly"),
    ):
        code = ddl(view)
        assert f"REFRESH EVERY 1 MINUTE APPEND TO loom.{target}\n" in code
        assert f"SELECT max(ts_hwm)\n    FROM loom.{target}\n" in code
        assert "(ts <= (now64(3) - toIntervalSecond(10)))" in code
        assert "ts_hwm" in column_names(target)
    assert "`ts_hwm` SimpleAggregateFunction(max, DateTime64(3))" in ddl("agent_cost_rollup_daily")
    src = (root / "server" / "insights" / "store" / "cost.py").read_text()
    assert "FROM loom.agent_cost_rollup_daily" in src
    assert "FROM loom.agent_cost_rollup\n" not in src


def test_the_timers_flush_is_attributed_to_the_buffer(
    settings_override, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The timer flush runs in a detached, empty context (no request's
    tag); it tags its own insert rather than logging `untagged:`."""
    from server.core.ch import current_query_tag, set_query_tag

    settings_override(namespace_write_marks_flush_interval_s=0.01)
    tags: list[str] = []

    class _TagCH(_CH):
        async def insert(self, table, rows, settings=None, **kw):
            tags.append(current_query_tag())

    ch = _TagCH()
    monkeypatch.setattr(m, "_default_client", lambda: ch)

    async def run():
        set_query_tag("memory.set")  # the request that armed the timer
        m.touch("o", "a", "memory")
        await asyncio.sleep(0.1)

    asyncio.run(run())
    assert tags == [m.FLUSH_QUERY_TAG]


def test_the_wiring_registers_the_memory_planes_insert_observer() -> None:
    from server.core import ch as chmod
    from server.memory.app import recent_writes

    assert recent_writes.observe_insert in chmod._INSERT_OBSERVERS
    before = list(chmod._INSERT_OBSERVERS)
    chmod.add_insert_observer(recent_writes.observe_insert)  # idempotent
    assert chmod._INSERT_OBSERVERS == before


def test_a_process_start_moves_the_all_orgs_mark_committed() -> None:
    """A crashed process took its unflushed marks with it; its replacement's
    start mark blocks every fast precedent proven unmoved before then."""
    ch = _CH()
    asyncio.run(m.mark_process_start(ch=ch))
    ((table, rows, settings),) = ch.inserts
    assert table == m.TABLE and settings == committed_insert_settings()
    assert [(r["org"], r["namespace"], r["plane"]) for r in rows] == [("*", "*", "restart")]


def test_last_writes_folds_in_the_all_orgs_mark() -> None:
    from server.memory.domain.precedents import marks_block

    seen: list[str] = []
    started = datetime(2026, 10, 14, 12, 0, 0)

    class _Read(_CH):
        async def query_all(self, sql, params=None):
            seen.append(sql)
            return [{"namespace": "*", "last_write": started}]

    out = asyncio.run(m.last_writes("o", ["n"], ch=_Read()))
    assert "(org = '*' AND namespace = '*')" in seen[0]
    assert out["*"] == started.replace(tzinfo=UTC)

    class _Prec:
        retrieved_at = started.replace(tzinfo=UTC) - timedelta(minutes=1)

    assert marks_block(_Prec(), out, namespaces=["n"]) == "moved:*"


def test_one_timed_flush_at_a_time(settings_override, monkeypatch) -> None:
    """A timer that fires while a flush is still in flight starts no second one;
    the marks touched meanwhile go with the next."""
    settings_override(namespace_write_marks_flush_interval_s=0.05)
    live = {"now": 0, "peak": 0}
    ch = _CH()

    async def slow_insert(table, rows, settings=None, **kw):
        live["now"] += 1
        live["peak"] = max(live["peak"], live["now"])
        await asyncio.sleep(0.15)
        ch.inserts.append((table, rows, settings))
        live["now"] -= 1

    ch.insert = slow_insert  # type: ignore[method-assign]
    monkeypatch.setattr(m, "_default_client", lambda: ch)

    async def run():
        m.touch("o", "a", "memory")
        await asyncio.sleep(0.07)  # its flush is in flight
        m.touch("o", "b", "memory")
        await asyncio.sleep(0.6)

    asyncio.run(run())
    assert live["peak"] == 1
    assert [[r["namespace"] for r in rows] for _, rows, _ in ch.inserts] == [["a"], ["b"]]


class _KeyCH:
    """The Keeper-key statements: the key insert and the key read."""

    def __init__(self, *, n: int = 0) -> None:
        self.n = n
        self.statements: list[tuple[str, Any, str]] = []
        self.inserts: list[str] = []

    async def query_one(self, sql, params=None):
        from server.core.ch import current_query_tag

        self.statements.append((sql, params, current_query_tag()))
        return {"n": self.n}

    async def execute(self, sql, params=None, **kw):
        from server.core.ch import current_query_tag

        self.statements.append((sql, params, current_query_tag()))

    async def insert(self, table, rows, settings=None, **kw):
        self.inserts.append(table)


def _in_a_request_tail(ch: _KeyCH, monkeypatch, body) -> None:
    from server.core import request_tail

    monkeypatch.setattr(m, "_default_client", lambda: ch)

    async def run():
        tail = request_tail._Tail()
        token = request_tail._tail.set(tail)
        try:
            body()
            await request_tail.settle(tail)
        finally:
            request_tail._tail.reset(token)

    asyncio.run(run())


def test_a_writes_request_tail_is_one_keeper_key_write(monkeypatch) -> None:
    """A memory.set's answer waits for one Keeper write per namespace it wrote: no
    async insert (whose busy timeout the answer would wait out), no part, tagged."""
    ch = _KeyCH()

    def body():
        rows = [{"org": "o", "namespace": "a"}]
        m.observe_insert("loom.memory", rows)
        m.observe_insert("loom.memory_chunks", rows)

    _in_a_request_tail(ch, monkeypatch, body)
    assert ch.inserts == []
    [(sql, params, tag)] = ch.statements
    assert sql.startswith(f"INSERT INTO {m.KEYS_TABLE} (key) SETTINGS async_insert = 0 ")
    assert params == {"key": "o\x1fa"} and tag == m.KEY_QUERY_TAG
    assert set(m.pending()) == {("o", "a", "memory"), ("o", "a", "chunk")}  # the timer's


def test_a_stalled_key_holds_the_answer_back_at_most_a_second(monkeypatch) -> None:
    class _Stalled(_KeyCH):
        async def execute(self, sql, params=None, **kw):
            await asyncio.sleep(30)

    ch = _Stalled()
    monkeypatch.setattr(m, "KEY_WAIT_S", 0.1)

    async def run() -> float:
        loop = asyncio.get_running_loop()
        started = loop.time()
        with in_request() as tail:
            monkeypatch.setattr(m, "_default_client", lambda: ch)
            m.observe_insert("loom.memory", [{"org": "o", "namespace": "a"}])
            await request_tail.settle(tail)
        waited = loop.time() - started
        assert [t for t in m._inflight if not t.done()], "the key lands detached"
        for t in m._inflight:
            t.cancel()
        return waited

    assert asyncio.run(run()) < 1.0


def test_written_within_reads_the_keys_by_the_servers_clock() -> None:
    ch = _KeyCH(n=1)
    assert asyncio.run(m.written_within("o", "a", 20.0, ch=ch)) is True
    sql, params, _ = ch.statements[-1]
    assert "FROM loom.namespace_write_keys" in sql
    assert "now64(3) - toIntervalMillisecond({ms:UInt32})" in sql
    assert params == {"keys": ["o\x1fa", "o\x1f*"], "ms": 20000}
    assert asyncio.run(m.written_within("o", "a", 20.0, ch=_KeyCH(n=0))) is False


def test_a_bump_sets_the_namespaces_key() -> None:
    ch = _KeyCH()
    asyncio.run(m.bump("o", "a", "memory", ch=ch))
    assert ch.inserts == [m.TABLE]
    assert ch.statements[-1][1] == {"key": "o\x1fa"}
