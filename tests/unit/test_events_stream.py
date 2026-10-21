"""Budget violations queue in-process and reach loom.events as batched inserts."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import pytest

from server.observability import events_stream as es
from server.settings import settings


def test_event_carries_every_column_and_the_rest_as_payload() -> None:
    row = {
        "ts": datetime(2026, 9, 14, 7, 0, 0, 123000, tzinfo=UTC),
        "org": "o",
        "namespace": "n",
        "actor": "alice",
        "token_id": "t1",
        "agent": "bot",
        "user_id": "u",
        "session_id": "s",
        "run_id": "r",
        "operation": "memory.search",
        "request_id": "req",
        "read_scope": "namespace",
        "query_digest": "d",
        "top_k": 10,
        "returned_keys": ["k1", "k2"],
        "returned_count": 2,
        "below_floor_count": 0,
    }
    ev = es.to_event(es.BUDGET_VIOLATION_KIND, row)
    assert ev["kind"] == es.BUDGET_VIOLATION_KIND
    assert set(ev) == {
        "ts",
        "kind",
        *es._IDENTITY,
        "build_commit",
        "instance",
        "instance_label",
        "payload",
    }
    assert ev["instance"] and ":" in ev["instance"]  # hostname:pid
    assert json.loads(ev["payload"]) == {
        "operation": "memory.search",
        "read_scope": "namespace",
        "query_digest": "d",
        "top_k": 10,
        "returned_keys": ["k1", "k2"],
        "returned_count": 2,
        "below_floor_count": 0,
    }


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[dict[str, Any]], dict[str, Any] | None]] = []

    async def insert(
        self, table: str, rows: list[dict[str, Any]], settings: dict[str, Any] | None = None
    ) -> None:
        self.calls.append((table, rows, settings))


def _reset() -> None:
    es._queue.clear()
    if es._drain_task is not None:
        es._drain_task.cancel()
        es._drain_task = None


@pytest.mark.asyncio
async def test_queued_events_write_as_one_committed_insert(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient()
    import server.core.ch as ch_mod

    monkeypatch.setattr(ch_mod, "get_client", lambda: fake)
    monkeypatch.setattr(
        settings, "events_stream_flush_interval_s", 60.0
    )  # the drain must not race the test
    _reset()
    es.record(
        es.BUDGET_VIOLATION_KIND,
        {
            "ts": datetime.now(UTC),
            "event_type": "a",
            "org": "o",
            "namespace": "n",
            "actor": "x",
            "subject": "{}",
            "request_id": "r",
            "row_hash": "",
        },
    )
    es.record(
        es.BUDGET_VIOLATION_KIND,
        {"org": "o", "namespace": "n", "operation": "memory.get", "returned_keys": ["k"]},
    )
    assert fake.calls == [] and es.queued() == 2  # queued, nothing written yet
    assert await es.flush_now() == 2
    assert len(fake.calls) == 1
    table, rows, st = fake.calls[0]
    assert table == "loom.events" and [r["kind"] for r in rows] == [
        es.BUDGET_VIOLATION_KIND,
        es.BUDGET_VIOLATION_KIND,
    ]
    assert st == {"async_insert": 0, "wait_for_async_insert": 1}  # committed
    assert all(set(r) == set(rows[0]) for r in rows)  # one column set → one statement
    assert await es.flush_now() == 0 and es.queued() == 0
    _reset()


@pytest.mark.asyncio
async def test_batch_size_triggers_a_flush_without_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient()
    import server.core.ch as ch_mod

    monkeypatch.setattr(ch_mod, "get_client", lambda: fake)
    monkeypatch.setattr(settings, "events_stream_flush_interval_s", 60.0)
    monkeypatch.setattr(settings, "events_stream_flush_batch", 3)
    _reset()
    for i in range(3):
        es.record(
            es.BUDGET_VIOLATION_KIND,
            {
                "event_type": "a",
                "org": "o",
                "namespace": "n",
                "actor": "x",
                "subject": "{}",
                "request_id": str(i),
                "row_hash": "",
            },
        )
    await asyncio.gather(*es._inflight)
    assert len(fake.calls) == 1 and len(fake.calls[0][1]) == 3
    _reset()


@pytest.mark.asyncio
async def test_a_failed_flush_is_logged_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Boom:
        async def insert(self, *a: Any, **k: Any) -> None:
            raise RuntimeError("down")

    import server.core.ch as ch_mod

    monkeypatch.setattr(ch_mod, "get_client", lambda: _Boom())
    monkeypatch.setattr(settings, "events_stream_flush_interval_s", 60.0)
    _reset()
    es.record(
        es.BUDGET_VIOLATION_KIND,
        {
            "event_type": "a",
            "org": "o",
            "namespace": "n",
            "actor": "x",
            "subject": "{}",
            "request_id": "r",
            "row_hash": "",
        },
    )
    assert await es.flush_now() == 0
    _reset()


@pytest.mark.asyncio
async def test_audit_writes_its_table_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.observability import audit

    fake = _FakeClient()
    monkeypatch.setattr(audit, "get_client", lambda: fake)
    await audit.emit(
        event_type="memory.set", org="o", namespace="n", actor="a", subject={}, request_id="r"
    )
    assert fake.calls and fake.calls[0][0] == "loom.audit_log"


@pytest.mark.asyncio
async def test_drain_stops_the_loop_and_writes_the_rest(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient()
    import server.core.ch as ch_mod

    monkeypatch.setattr(ch_mod, "get_client", lambda: fake)
    monkeypatch.setattr(settings, "events_stream_flush_interval_s", 60.0)
    _reset()
    es.record(
        es.BUDGET_VIOLATION_KIND,
        {
            "event_type": "a",
            "org": "o",
            "namespace": "n",
            "actor": "x",
            "subject": "{}",
            "request_id": "r",
            "row_hash": "",
        },
    )
    assert es._drain_task is not None
    await es.drain()
    assert es._drain_task is None and len(fake.calls) == 1 and es.queued() == 0


@pytest.mark.asyncio
async def test_a_flush_cancelled_mid_insert_keeps_its_rows_for_the_final_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shutdown can cancel the drain loop while a flush is inside the insert,
    after the rows have left the queue. A cancelled flush puts them back;
    drain writes them."""
    written: list[list[dict[str, Any]]] = []
    gate = asyncio.Event()

    class _Slow:
        async def insert(
            self, table: str, rows: list[dict[str, Any]], settings: dict[str, Any] | None = None
        ) -> None:
            if not gate.is_set():
                gate.set()
                await asyncio.sleep(60)  # cancelled here on the first attempt
            written.append(rows)

    import server.core.ch as ch_mod

    monkeypatch.setattr(ch_mod, "get_client", lambda: _Slow())
    monkeypatch.setattr(settings, "events_stream_flush_interval_s", 60.0)
    _reset()
    es.record(
        es.BUDGET_VIOLATION_KIND,
        {
            "event_type": "a",
            "org": "o",
            "namespace": "n",
            "actor": "x",
            "subject": "{}",
            "request_id": "r",
            "row_hash": "",
        },
    )
    first = asyncio.get_running_loop().create_task(es.flush_now())
    await gate.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert es.queued() == 1  # the row came back
    await es.drain()
    assert written and written[0][0]["request_id"] == "r" and es.queued() == 0


def test_build_commit_is_stamped_from_the_row_or_the_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "build_commit", "deadbeef")
    ev = es.to_event(
        es.BUDGET_VIOLATION_KIND, {"org": "o", "namespace": "n", "returned_keys": ["k"]}
    )
    assert ev["build_commit"] == "deadbeef"
    ev = es.to_event(
        es.BUDGET_VIOLATION_KIND,
        {"org": "o", "namespace": "n", "event_type": "x", "build_commit": "cafe0001"},
    )
    assert ev["build_commit"] == "cafe0001" and "build_commit" not in json.loads(ev["payload"])


@pytest.mark.asyncio
async def test_audit_emit_never_raises_on_a_subject_json_cannot_sort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mixed str/int keys make `json.dumps(sort_keys=True)` raise TypeError;
    emit's promise is "never raises", so the write that called it survives."""
    from server.observability import audit

    fake = _FakeClient()
    monkeypatch.setattr(audit, "get_client", lambda: fake)
    rh = await audit.emit(
        event_type="memory.set",
        org="o",
        namespace="n",
        actor="a",
        subject={1: "x", "b": 2},
        request_id="r",
    )
    assert rh == "" and fake.calls == []


@pytest.mark.asyncio
async def test_audit_hash_payload_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-row hashes already stored must keep verifying: the hash of a fixed
    row is pinned byte for byte."""
    from server.observability import audit

    fake = _FakeClient()
    monkeypatch.setattr(audit, "get_client", lambda: fake)
    rh = await audit.emit(
        "memory.set",
        org="o",
        namespace="n",
        actor="alice",
        subject={"b": [1, 2], "a": "x"},
        request_id="r",
        ts=datetime(2026, 9, 14, 7, 0, 0, 123456, tzinfo=UTC),
    )
    assert rh == "71e9999594b6444fd60cb44be94360a220d93f082051fc0aef207ee3bd5f05a6"


def test_record_never_raises_on_an_unmappable_row(caplog: pytest.LogCaptureFixture) -> None:
    """An unknown kind (KeyError) or a payload json cannot sort (TypeError) is
    dropped and logged, not raised into the request that recorded it."""
    _reset()
    with caplog.at_level("WARNING"):
        assert es.record("no_such_kind", {"org": "o"}) == {}
        assert es.record(es.BUDGET_VIOLATION_KIND, {"org": "o", 1: "x", "b": 2}) == {}
    assert es.queued() == 0
    assert len([r for r in caplog.records if "unmappable" in r.getMessage()]) == 2
    _reset()


@pytest.mark.asyncio
async def test_lost_rows_are_counted_by_reason_and_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    """An audit row rides this stream, so a loss is a number on
    /v1/health?include=events, not only a log line."""

    class _Boom:
        async def insert(self, *a: Any, **k: Any) -> None:
            raise RuntimeError("down")

    import server.core.ch as ch_mod

    monkeypatch.setattr(ch_mod, "get_client", lambda: _Boom())
    monkeypatch.setattr(settings, "events_stream_flush_interval_s", 60.0)
    monkeypatch.setattr(es, "_dropped", {})
    _reset()
    audit = {"event_type": "a", "org": "o", "namespace": "n", "actor": "x", "subject": "{}"}
    es.record(es.BUDGET_VIOLATION_KIND, dict(audit))
    es.record(es.BUDGET_VIOLATION_KIND, dict(audit))
    assert await es.flush_now() == 0
    monkeypatch.setattr(es, "QUEUE_MAX", 0)
    es.record(es.BUDGET_VIOLATION_KIND, dict(audit))
    es.record("no_such_kind", {"org": "o"})
    assert es.stats() == {
        "queued": 0,
        "dropped": {
            "flush_failed": {"budget_violation": 2},
            "queue_full": {"budget_violation": 1},
            "unmappable": {"no_such_kind": 1},
        },
    }
    _reset()


@pytest.mark.asyncio
async def test_a_batch_flush_carries_the_streams_tag_not_the_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The batch that one request's row fills holds every queued request's
    rows; its insert is the stream's own, not that request's."""
    from server.core.ch import current_query_tag, set_query_tag

    tags: list[str] = []

    class _Tagged:
        async def insert(self, *a: Any, **k: Any) -> None:
            tags.append(current_query_tag())

    import server.core.ch as ch_mod

    monkeypatch.setattr(ch_mod, "get_client", lambda: _Tagged())
    monkeypatch.setattr(settings, "events_stream_flush_interval_s", 60.0)
    monkeypatch.setattr(settings, "events_stream_flush_batch", 2)
    _reset()

    async def _request() -> None:
        set_query_tag("memory.search req-1")
        for _ in range(2):
            es.record(
                es.BUDGET_VIOLATION_KIND, {"org": "o", "namespace": "n", "returned_keys": ["k"]}
            )

    await asyncio.get_running_loop().create_task(_request())
    await asyncio.gather(*es._inflight)
    assert tags == [es.FLUSH_QUERY_TAG]
    _reset()


@pytest.mark.asyncio
async def test_drain_is_bounded_and_counts_what_it_could_not_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Hung:
        async def insert(self, *a: Any, **k: Any) -> None:
            await asyncio.sleep(60)

    import server.core.ch as ch_mod

    monkeypatch.setattr(ch_mod, "get_client", lambda: _Hung())
    monkeypatch.setattr(settings, "events_stream_flush_interval_s", 60.0)
    monkeypatch.setattr(settings, "events_stream_flush_batch", 1)
    monkeypatch.setattr(es, "_dropped", {})
    _reset()
    es.record(es.BUDGET_VIOLATION_KIND, {"org": "o", "namespace": "n", "returned_keys": ["k"]})
    assert es._inflight  # the batch flush is inside the hung insert
    await asyncio.wait_for(es.drain(timeout_s=0.05), timeout=2.0)
    assert es.queued() == 0 and es.stats()["dropped"] == {"shutdown": {"budget_violation": 1}}
    _reset()


@pytest.mark.asyncio
async def test_slow_full_batches_share_one_threshold_flush(monkeypatch: pytest.MonkeyPatch) -> None:
    started: asyncio.Queue[asyncio.Event] = asyncio.Queue()
    inserted = 0

    class _Slow:
        async def insert(self, table: str, rows: list[dict[str, Any]], **kw: Any) -> None:
            nonlocal inserted
            gate = asyncio.Event()
            started.put_nowait(gate)
            await gate.wait()
            inserted += len(rows)

    monkeypatch.setattr("server.core.ch.get_client", _Slow)
    monkeypatch.setattr(es, "QUEUE_MAX", 10)
    monkeypatch.setattr(es, "_batch", lambda: 1)
    monkeypatch.setattr(es, "_ensure_drain", lambda: None)
    _reset()
    es.record(es.BUDGET_VIOLATION_KIND, {"org": "o", "namespace": "n"})
    gate = await started.get()
    try:
        for _ in range(5):
            for _ in range(10):
                es.record(es.BUDGET_VIOLATION_KIND, {"org": "o", "namespace": "n"})
            await asyncio.sleep(0)
            assert es.queued() == 10
            assert len(es._inflight) == 1
            gate.set()
            gate = await asyncio.wait_for(started.get(), timeout=1)
        gate.set()
        await asyncio.gather(*es._inflight)
        assert es.queued() == 0
        assert inserted == 51
        assert not es._inflight
    finally:
        gate.set()
        for task in list(es._inflight):
            task.cancel()
        await asyncio.gather(*es._inflight, return_exceptions=True)
        _reset()
