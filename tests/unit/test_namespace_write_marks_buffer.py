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
