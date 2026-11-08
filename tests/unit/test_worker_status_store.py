"""Worker health is written to the store. The supervisor
queues one `loom.worker_status` row per finished pass (idle included), the
reader folds the table to the latest row per (worker, instance_label), the
merge prefers the freshest report per worker, the admin handler returns the
union with its provenance, and a restarted pass reads the cursor the previous
one left. CH-free: a fake client records the insert."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from server.core.model_work_gate import ModelWorkGate
from server.observability import events as ev_mod
from server.observability import genai
from server.workers import pass_context
from server.workers import status as wstatus
from server.workers import status_store as store
from server.workers import supervisor as sup
from server.workers.registry import WorkerSpec


class _FakeCH:
    def __init__(self, cursor: str = "") -> None:
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []
        self.queries: list[tuple[str, dict[str, Any]]] = []
        self.cursor = cursor
        self.latest: list[dict[str, Any]] = []

    async def insert(self, table: str, rows: list[dict[str, Any]], settings: Any = None) -> None:
        self.inserts.append((table, rows))

    async def query_one(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        self.queries.append((sql, dict(params or {})))
        return {"cursor": self.cursor}

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.queries.append((sql, dict(params or {})))
        return list(self.latest)


class _Emitter:
    def __init__(self) -> None:
        self.events: list = []

    def emit(self, event) -> None:
        self.events.append(event)


@pytest.fixture
def ch(monkeypatch: pytest.MonkeyPatch) -> _FakeCH:
    fake = _FakeCH(cursor="k=42")
    store.reset_for_tests(client=fake)
    monkeypatch.setattr(ev_mod, "get_emitter", lambda: _Emitter())
    wstatus.get_registry().reset()
    yield fake
    store.reset_for_tests()
    wstatus.get_registry().reset()
    genai.set_identity()


def _supervisor(stop_after_passes: int, counter: dict[str, int]) -> sup.WorkerSupervisor:
    s = sup.WorkerSupervisor()

    async def _sleep(_seconds: float) -> None:
        if counter["n"] >= stop_after_passes:
            s._stopping.set()

    s._sleep_or_drain = _sleep  # type: ignore[method-assign]
    return s


def _rows(ch: _FakeCH) -> list[dict[str, Any]]:
    return [r for _t, rows in ch.inserts for r in rows]


# --------------------------------------------------------------------------- supervisor hook


async def test_model_maintenance_wait_does_not_spend_worker_deadline_or_retry(
    ch: _FakeCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = ModelWorkGate()
    gate.close("change")
    monkeypatch.setattr(sup, "get_model_work_gate", lambda: gate)
    calls = {"n": 0}

    async def _pass() -> None:
        calls["n"] += 1

    supervisor = _supervisor(1, calls)
    spec = WorkerSpec("model-maintenance-test", 1.0, _pass, max_pass_seconds=0.001)
    task = asyncio.create_task(supervisor._supervise(spec))
    try:
        await asyncio.sleep(0.02)
        assert calls["n"] == 0
        assert store.queued() == 0
        assert not task.done()
        gate.resume("change")
        await asyncio.wait_for(task, timeout=5)
        await store.flush()
        assert [row["status"] for row in _rows(ch)] == ["ok"]
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_model_drain_waits_for_an_admitted_worker_without_cancelling_it(
    ch: _FakeCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = ModelWorkGate()
    monkeypatch.setattr(sup, "get_model_work_gate", lambda: gate)
    calls = {"n": 0}
    entered, finish = asyncio.Event(), asyncio.Event()

    async def _pass() -> None:
        calls["n"] += 1
        entered.set()
        await finish.wait()

    supervisor = _supervisor(1, calls)
    spec = WorkerSpec("model-drain-test", 1.0, _pass, max_pass_seconds=5)
    task = asyncio.create_task(supervisor._supervise(spec))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        gate.close("change")
        assert not await gate.wait_drained("change", timeout_s=0)
        assert not task.done()
        finish.set()
        await asyncio.wait_for(task, timeout=5)
        assert await gate.wait_drained("change", timeout_s=0)
        await store.flush()
        assert [row["status"] for row in _rows(ch)] == ["ok"]
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_supervisor_queues_one_row_per_pass_and_flushes_them_in_one_insert(
    ch: _FakeCH,
) -> None:
    calls = {"n": 0}

    async def _pass() -> None:
        calls["n"] += 1
        with wstatus.record_pass("census") as rec:
            # The cursor the previous pass left (read from the store at loop start).
            assert pass_context.resume_cursor() == ("k=42" if calls["n"] == 1 else "k=43")
            pass_context.set_cursor("k=43")
            rec.outcome = {"scanned": 10, "processed": 4, "rows_written": 3}
        if calls["n"] == 2:
            raise RuntimeError("boom 'secret'")

    s = _supervisor(2, calls)
    spec = WorkerSpec("census", 1.0, _pass, max_pass_seconds=5.0)  # interval < 2 s: no boot stagger
    await asyncio.wait_for(s._supervise(spec), timeout=5)
    await store.flush()

    assert ch.inserts and all(t == store.TABLE for t, _ in ch.inserts)
    rows = _rows(ch)
    assert [r["status"] for r in rows] == ["ok", "failed"]
    assert [r["trigger"] for r in rows] == ["boot", "interval"]
    ok, failed = rows
    assert ok["worker"] == "census" and ok["interval_s"] == 1.0 and ok["deadline_s"] == 5.0
    assert (ok["items_planned"], ok["items_done"], ok["rows_written"]) == (10, 4, 3)
    assert ok["resume_cursor"] == "k=43" and ok["consecutive_failures"] == 0
    assert ok["error"] == "" and ok["error_class"] == ""
    assert '"processed": 4' in ok["detail"]
    assert failed["error_class"] == "RuntimeError" and "boom" in failed["error"]
    assert "secret" not in failed["error"]  # bounded_error elides quoted spans
    assert failed["consecutive_failures"] == 1 and failed["partial_failures"] == 0
    assert set(ok) == set(store.COLUMNS)
    assert ok["version"] > 0 and ok["pass_finished"] >= ok["pass_started"]
    # The cursor read happened once, at loop start, for this worker.
    cursor_reads = [q for q in ch.queries if "resume_cursor != ''" in q[0]]
    assert len(cursor_reads) == 1 and cursor_reads[0][1] == {"worker": "census"}
    # One flush = one insert for both passes: no one-row part per pass.
    assert len(ch.inserts) == 1


async def test_a_deadline_pass_is_a_timeout_row_and_an_idle_pass_writes_a_row_too(
    ch: _FakeCH,
) -> None:
    calls = {"n": 0}

    async def _pass() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            with wstatus.record_pass("sweeper") as rec:
                rec.outcome = {"processed": 0, "skipped": 0}
            return
        await asyncio.sleep(5)

    s = _supervisor(2, calls)
    spec = WorkerSpec("sweeper", 1.0, _pass, max_pass_seconds=0.05)
    await asyncio.wait_for(s._supervise(spec), timeout=5)
    await store.flush()
    idle, timeout = _rows(ch)
    assert idle["status"] == "idle" and idle["error"] == ""
    assert timeout["status"] == "timeout" and timeout["error_class"] == "TimeoutError"
    assert "deadline of 0s" in timeout["error"] and timeout["deadline_s"] == 0.05


async def test_rows_after_a_success_carry_only_their_own_pass_counts(ch: _FakeCH) -> None:
    """A failed, timed-out or partial pass after a success does not repeat the
    success's counts: the registry keeps the last success's outcome, the row
    reads the one this pass recorded."""
    calls = {"n": 0}

    async def _pass() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            with wstatus.record_pass("steady") as rec:
                rec.outcome = {"scanned": 9, "processed": 5, "rows_written": 2}
            return
        if calls["n"] == 2:
            with wstatus.record_pass("steady"):
                raise RuntimeError("down")
        if calls["n"] == 3:
            with wstatus.record_pass("steady"):
                await asyncio.sleep(5)
        with wstatus.record_pass("steady") as rec:
            pass_context.note_failure(KeyError("leg"), "leg")
            rec.outcome = {"scanned": 1, "processed": 1, "rows_written": 0}

    s = _supervisor(4, calls)
    spec = WorkerSpec("steady", 1.0, _pass, max_pass_seconds=0.2)
    await asyncio.wait_for(s._supervise(spec), timeout=5)
    await store.flush()
    rows = _rows(ch)
    assert [r["status"] for r in rows] == ["ok", "failed", "timeout", "partial"]
    counts = [(r["items_planned"], r["items_done"], r["rows_written"]) for r in rows]
    assert counts == [(9, 5, 2), (0, 0, 0), (0, 0, 0), (1, 1, 0)]
    assert rows[1]["detail"] == rows[2]["detail"] == store.detail_text({})
    assert wstatus.get_registry().snapshot()["steady"]["last_outcome"]["processed"] == 1


async def test_a_stop_mid_pass_after_a_success_writes_a_cancelled_row_without_counts(
    ch: _FakeCH,
) -> None:
    calls = {"n": 0}
    second_pass = asyncio.Event()
    s = sup.WorkerSupervisor()

    async def _pass() -> None:
        calls["n"] += 1
        with wstatus.record_pass("stopper") as rec:
            if calls["n"] == 1:
                rec.outcome = {"scanned": 3, "processed": 3, "rows_written": 3}
                return
            second_pass.set()
            await asyncio.sleep(60)

    async def _sleep(_seconds: float) -> None:
        return None

    s._sleep_or_drain = _sleep  # type: ignore[method-assign]
    task = asyncio.create_task(s._supervise(WorkerSpec("stopper", 1.0, _pass)))
    await asyncio.wait_for(second_pass.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await store.flush()
    ok, cancelled = _rows(ch)
    assert ok["status"] == "ok" and ok["rows_written"] == 3
    assert cancelled["status"] == "cancelled"
    assert (cancelled["items_planned"], cancelled["items_done"], cancelled["rows_written"]) == (
        0,
        0,
        0,
    )


async def test_three_failures_write_the_degraded_verdict_on_the_row(ch: _FakeCH) -> None:
    calls = {"n": 0}

    async def _pass() -> None:
        calls["n"] += 1
        raise ValueError("no")

    s = _supervisor(3, calls)
    await asyncio.wait_for(s._supervise(WorkerSpec("flaky", 1.0, _pass)), timeout=5)
    await store.flush()
    rows = _rows(ch)
    assert [r["consecutive_failures"] for r in rows] == [1, 2, 3]
    assert [r["trigger"] for r in rows] == ["boot", "retry_backoff", "retry_backoff"]
    assert rows[2]["error"].startswith("DEGRADED — 3 consecutive failures")
    assert '"degraded": true' in rows[2]["detail"]
    assert "DEGRADED" not in rows[1]["error"]


async def test_a_partial_pass_is_a_partial_row_with_the_first_failed_leg(ch: _FakeCH) -> None:
    calls = {"n": 0}

    async def _pass() -> None:
        calls["n"] += 1
        pass_context.note_failure(KeyError("orphan"), "reconcile")

    s = _supervisor(1, calls)
    await asyncio.wait_for(s._supervise(WorkerSpec("embed", 1.0, _pass)), timeout=5)
    await store.flush()
    (row,) = _rows(ch)
    assert row["status"] == "partial" and row["partial_failures"] == 1
    assert row["error_class"] == "KeyError" and row["error"].startswith("reconcile:")


async def test_stop_all_drains_the_queue(ch: _FakeCH) -> None:
    store.record_pass("x", status="ok", started=datetime.now(UTC), finished=datetime.now(UTC))
    assert store.queued() == 1
    s = sup.WorkerSupervisor()

    async def _idle() -> None:
        await asyncio.sleep(60)

    s._tasks.append(asyncio.create_task(_idle(), name="loom-worker:idle"))
    await s.stop_all(drain_timeout_s=1.0)
    assert store.queued() == 0 and len(_rows(ch)) == 1


async def test_the_flush_tasks_attribute_their_insert_to_the_store_not_the_first_worker(
    ch: _FakeCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.core import ch as ch_mod

    tags: list[str] = []

    async def _insert(table: str, rows: list[dict[str, Any]], settings: Any = None) -> None:
        tags.append(ch_mod.current_query_tag())

    ch.insert = _insert  # type: ignore[method-assign]
    monkeypatch.setattr(store, "FLUSH_INTERVAL_S", 0.0)
    monkeypatch.setattr(store, "FLUSH_ROWS", 1)

    async def _worker_pass() -> None:
        ch_mod.set_query_tag("worker.alpha")
        store.record_pass(
            "alpha", status="ok", started=datetime.now(UTC), finished=datetime.now(UTC)
        )
        assert ch_mod.current_query_tag() == "worker.alpha"

    await asyncio.create_task(_worker_pass())
    for _ in range(20):
        await asyncio.sleep(0)
    assert tags and set(tags) == {"worker_status.flush"}


async def test_a_full_queue_starts_one_held_early_flush(
    ch: _FakeCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The early flush is referenced until it finishes (the loop holds tasks
    weakly), and passes queued while it runs do not start more."""
    monkeypatch.setattr(store, "FLUSH_ROWS", 1)
    now = datetime.now(UTC)
    store.record_pass("a", status="ok", started=now, finished=now)
    first = store._flush_now_task
    assert first is not None and not first.done()
    store.record_pass("b", status="ok", started=now, finished=now)
    assert store._flush_now_task is first
    await first
    assert store.queued() == 0 and len(_rows(ch)) == 2


def test_record_pass_without_a_loop_queues_and_never_raises(ch: _FakeCH) -> None:
    row = store.record_pass(
        "x", status="nonsense", started=datetime.now(UTC), finished=datetime.now(UTC)
    )
    assert row is not None and row["status"] == "failed" and store.queued() == 1


async def test_a_failing_insert_is_a_warning_not_an_exception(
    ch: _FakeCH, caplog: pytest.LogCaptureFixture
) -> None:
    async def _boom(*_a: Any, **_k: Any) -> None:
        raise ConnectionError("refused")

    ch.insert = _boom  # type: ignore[method-assign]
    store.record_pass("x", status="ok", started=datetime.now(UTC), finished=datetime.now(UTC))
    with caplog.at_level("WARNING"):
        assert await store.flush() == 0
    assert any("not written" in r.message for r in caplog.records)
    assert store.queued() == 0


# --------------------------------------------------------------------------- pure pieces


def test_classify_covers_the_census_vocabulary() -> None:
    assert store.classify(success=False, cancelled=True) == "cancelled"
    assert store.classify(success=False, deadline_seconds=9) == "timeout"
    assert store.classify(success=False) == "failed"
    assert store.classify(success=True, partial_failures=2) == "partial"
    assert store.classify(success=True, outcome={"processed": 0, "note": "x"}) == "idle"
    assert store.classify(success=True, outcome={"processed": 3}) == "ok"
    assert store.classify(success=True, outcome={}) == "ok"
    assert store.classify(success=True, outcome={"ok": True}) == "ok"  # bools are not counters


def test_counts_read_planned_done_and_written_off_the_outcome() -> None:
    assert store.counts({"candidates": 5, "processed": 3, "inserted": 2, "flag": True}) == (5, 3, 2)
    assert store.counts({"memories_embedded": 4, "keys_updated": 1}) == (0, 4, 1)
    assert store.counts(None) == (0, 0, 0)


def test_latest_sql_never_aliases_an_aggregate_to_its_source_column() -> None:
    assert (
        "AS version" not in store.LATEST_SQL
        and "max(version) AS latest_version" in store.LATEST_SQL
    )
    # `pass_started` is in the WHERE: an aggregate aliased to it resolves there (code 184).
    assert "AS pass_started" not in store.LATEST_SQL and "AS last_started" in store.LATEST_SQL
    assert "FINAL" not in store.LATEST_SQL
    assert "argMax(resume_cursor, version) AS cursor" in store.RESUME_CURSOR_SQL


def test_build_row_has_every_column_and_bounds_its_text() -> None:
    t0 = datetime(2026, 9, 16, 10, 0, tzinfo=UTC)
    row = store.build_row(
        "w",
        status="ok",
        started=t0,
        finished=t0 + timedelta(milliseconds=1500),
        stamps={"instance_label": "deploy:workers:green", "instance": "h:1", "build_commit": "abc"},
        error="e" * 5000,
        detail="d" * 5000,
        resume_cursor="c" * 5000,
        trigger="weird",
        consecutive_failures=-3,
        statements=7,
        read_bytes=1024,
        spent_usd="0.5",
    )
    assert tuple(row) == store.COLUMNS
    assert row["instance_label"] == "deploy:workers:green" and row["process"] == "h:1"
    assert (
        len(row["error"]) == 1000
        and len(row["detail"]) == 2000
        and len(row["resume_cursor"]) == 512
    )
    assert row["trigger"] == "" and row["consecutive_failures"] == 0
    assert row["statements"] == 7 and row["read_bytes"] == 1024 and row["spent_usd"] == 0.5
    assert row["pass_started"].tzinfo is None and row["version"] == int(
        (t0.timestamp() + 1.5) * 1e9
    )


# --------------------------------------------------------------------------- merge


def _store_row(worker: str, label: str, finished: datetime, **extra: Any) -> dict[str, Any]:
    row = {
        "worker": worker,
        "instance_label": label,
        "process": "h:9",
        "build_commit": "deadbeef",
        "pass_started": (finished - timedelta(seconds=2)).isoformat(),
        "pass_finished": finished.isoformat(),
        "status": "ok",
        "error": "",
        "consecutive_failures": 0,
        "rows_written": 5,
        "items_done": 2,
        "detail": "{}",
        "interval_s": 60.0,
    }
    row.update(extra)
    return row


def test_merge_prefers_the_freshest_report_per_worker_and_unions_unknown_workers() -> None:
    now = datetime.now(UTC)
    base = {
        "a": {
            "enabled": True,
            "effective": True,
            "interval_seconds": 60,
            "last_run_at": (now - timedelta(minutes=5)).isoformat(),
            "last_error": None,
            "consecutive_failures": 0,
            "total_runs": 3,
        },
        "b": {
            "enabled": True,
            "effective": True,
            "interval_seconds": 60,
            "last_run_at": now.isoformat(),
            "last_error": "X",
            "consecutive_failures": 1,
        },
        "c": {"enabled": True, "effective": True, "interval_seconds": 60},
    }
    rows = [
        _store_row("a", "deploy:workers:blue", now - timedelta(minutes=2), status="idle"),
        _store_row(
            "a",
            "deploy:workers:green",
            now - timedelta(minutes=1),
            status="failed",
            error="boom",
            consecutive_failures=2,
        ),
        _store_row("b", "deploy:workers:green", now - timedelta(minutes=9)),
        _store_row("d", "deploy:workers:green", now - timedelta(seconds=30)),
    ]
    out = store.merge_status(base, rows)
    a = out["a"]
    assert a["source"] == "store" and a["reporting_from"] == "deploy:workers:green"
    assert a["last_pass_status"] == "failed" and a["last_error"] == "boom"
    assert a["consecutive_failures"] == 2 and a["last_duration_ms"] == 2000
    assert a["reporters"] == ["deploy:workers:blue", "deploy:workers:green"]
    assert a["total_runs"] == 3  # registry-only fields survive
    b = out["b"]
    assert (
        b["source"] == "registry" and b["last_pass_status"] == "failed" and b["last_error"] == "X"
    )
    assert out["c"]["source"] is None and "last_pass_status" not in out["c"]
    d = out["d"]
    assert d["effective"] is True and d["source"] == "store" and d["interval_seconds"] == 60.0
    assert list(out) == ["a", "b", "c", "d"]


def test_merge_with_no_store_rows_leaves_the_registry_view_intact() -> None:
    base = {"a": {"enabled": False, "effective": False, "interval_seconds": 5}}
    out = store.merge_status(base, [])
    assert (
        out["a"]["enabled"] is False and out["a"]["source"] is None and out["a"]["reporters"] == []
    )


# --------------------------------------------------------------------------- reader + handler


async def test_latest_rows_renders_timestamps_and_swallows_store_errors(ch: _FakeCH) -> None:
    ch.latest = [
        {
            "worker": "a",
            "instance_label": "deploy:workers:green",
            "last_started": datetime(2026, 9, 16, 10, 0),
            "last_finished": datetime(2026, 9, 16, 10, 0, 2),
            "status": "ok",
        }
    ]
    rows = await store.latest_rows(days=3)
    assert rows[0]["pass_finished"] == "2026-09-16T10:00:02+00:00"
    assert rows[0]["pass_started"] == "2026-09-16T10:00:00+00:00" and "last_started" not in rows[0]
    assert ch.queries[-1][1] == {"days": 3}

    async def _boom(*_a: Any, **_k: Any) -> list[dict[str, Any]]:
        raise RuntimeError("store down")

    ch.query_all = _boom  # type: ignore[method-assign]
    assert await store.latest_rows() == []


async def test_resume_cursor_is_empty_when_the_store_cannot_answer(ch: _FakeCH) -> None:
    assert await store.resume_cursor("w") == "k=42"

    async def _boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("down")

    ch.query_one = _boom  # type: ignore[method-assign]
    assert await store.resume_cursor("w") == ""


async def test_admin_workers_status_returns_the_union_with_its_provenance(
    ch: _FakeCH,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.insights.app import admin_models
    from server.insights.app import admin_routes as admin

    now = datetime.now(UTC)
    ch.latest = [
        _store_row(
            "embedding_worker", "deploy:workers:green", now - timedelta(seconds=10), status="idle"
        ),
        _store_row("brand_new_worker", "deploy:workers:green", now - timedelta(seconds=5)),
    ]
    out = admin_models.WorkersStatusOutput.model_validate(await admin.admin_workers_status())
    w = out.workers
    assert w["embedding_worker"]["source"] == "store"
    assert w["embedding_worker"]["reporting_from"] == "deploy:workers:green"
    assert w["embedding_worker"]["last_pass_status"] == "idle"
    assert "interval_seconds" in w["embedding_worker"] and "enabled" in w["embedding_worker"]
    # A worker that waits otherwise when idle says so: the panel judges a stall by it.
    from server.settings import settings

    assert w["knowledge_ingest"]["idle_interval_seconds"] == 15
    idle = settings.embedding_worker_idle_interval_seconds
    assert w["embedding_worker"]["idle_interval_seconds"] == idle
    assert w["keeper_sweep"]["idle_interval_seconds"] is None  # it has none
    assert w["brand_new_worker"]["source"] == "store"
    assert out.store["table"] == store.TABLE and out.store["rows"] == 2
    assert out.store["reporters"] == ["deploy:workers:green"]
    assert out.store["from_store"] == 2 and out.store["in_process"] == 0
    # Workers the store has not heard of keep their meta row, unreported.
    unreported = [k for k, v in w.items() if v.get("source") is None]
    assert unreported and all("last_run_at" not in w[k] for k in unreported)


# --------------------------------------------------------------------------- namespace and trigger


def test_record_pass_carries_the_namespace_and_the_manual_trigger(ch: _FakeCH) -> None:
    now = datetime.now(UTC)
    row = store.record_pass(
        "code_graph_stats",
        status="timeout",
        started=now,
        finished=now,
        namespace="clickhouse-main",
        trigger="manual",
    )
    assert row is not None and row["namespace"] == "clickhouse-main" and row["trigger"] == "manual"
    assert tuple(row) == store.COLUMNS and store.COLUMNS.index("namespace") == 3
    # Default: '' — the many workers with no notion of a namespace.
    bare = store.record_pass("x", status="ok", started=now, finished=now)
    assert bare is not None and bare["namespace"] == ""
    assert "argMax(namespace, version) AS namespace" in store.LATEST_SQL
    assert "manual" in store.TRIGGERS


async def test_set_namespace_flows_through_the_supervisor_onto_the_row(ch: _FakeCH) -> None:
    calls = {"n": 0}

    async def _pass() -> None:
        calls["n"] += 1
        with wstatus.record_pass("code_graph_stats") as rec:
            for ns in ("ns-one", "ns-two", "ns-three"):
                pass_context.set_namespace(ns)
                if ns == "ns-two" and calls["n"] == 2:
                    raise TimeoutError("cut")
            rec.outcome = {"files_ranked": 3}

    s = _supervisor(2, calls)
    spec = WorkerSpec("code_graph_stats", 1.0, _pass, max_pass_seconds=5.0)
    await asyncio.wait_for(s._supervise(spec), timeout=5)
    await store.flush()
    ok, cut = _rows(ch)
    assert ok["status"] == "ok" and ok["namespace"] == "ns-three"
    # The failed pass says WHERE it stopped — the census's question.
    assert cut["status"] == "timeout" and cut["namespace"] == "ns-two"
    assert cut["trigger"] == "interval"


async def test_record_manual_pass_writes_now_with_the_manual_trigger(ch: _FakeCH) -> None:
    now = datetime.now(UTC)
    row = await store.record_manual_pass(
        "code_ingest",
        namespace="clickgap-public",
        status="ok",
        started=now,
        finished=now,
        items_done=12,
        rows_written=40,
        trigger="interval",  # a caller cannot override the trigger
    )
    assert row is not None and row["trigger"] == "manual" and row["namespace"] == "clickgap-public"
    # Drained at once: the CLI process exits before the 15 s flusher fires.
    assert store.queued() == 0 and len(ch.inserts) == 1 and _rows(ch)[0]["items_done"] == 12


async def test_backfill_embed_leaves_a_manual_trigger_row_for_its_namespace(ch: _FakeCH) -> None:
    import argparse

    from server.cli import code_backfill

    class _Repo:
        async def pending_embed_chunks(self, *_a: Any, **_k: Any) -> list[dict[str, Any]]:
            return []

    rc = await code_backfill._backfill_embed(
        _Repo(), "acme", "clickgap-public", argparse.Namespace()
    )
    assert rc == 0
    (row,) = _rows(ch)
    assert row["worker"] == "code_embed_recovery" and row["trigger"] == "manual"
    assert row["namespace"] == "clickgap-public" and row["status"] == "idle"
    assert '"command": "loom code"' in row["detail"] and '"phase": "embed"' in row["detail"]
    assert '"org": "acme"' in row["detail"]
    assert store.queued() == 0


def test_manual_pass_status_uses_the_census_vocabulary() -> None:
    from server.cli.code_backfill import manual_pass_status

    assert manual_pass_status(0, 0) == "idle"
    assert manual_pass_status(5, 0) == "ok"
    assert manual_pass_status(5, 2) == "partial"
    assert manual_pass_status(0, 2) == "failed"


def test_merge_carries_namespace_and_trigger_from_the_store_row() -> None:
    now = datetime.now(UTC)
    base = {"a": {"enabled": True, "effective": True, "interval_seconds": 60}}
    rows = [_store_row("a", "iso:wt:7801", now, namespace="clickhouse-main", trigger="manual")]
    out = store.merge_status(base, rows)
    assert out["a"]["namespace"] == "clickhouse-main" and out["a"]["trigger"] == "manual"
    plain = store.merge_status(base, [_store_row("a", "deploy:workers:green", now)])
    assert plain["a"]["namespace"] == "" and plain["a"]["trigger"] == ""


def test_pass_context_namespace_hook_is_bounded_and_quiet_outside_a_pass() -> None:
    pass_context.clear()
    pass_context.set_namespace("ignored")  # no pass bound: a debug line, never an error
    ctx = pass_context.begin("w")
    assert ctx.namespace == ""
    pass_context.set_namespace("n" * 1000)
    assert len(ctx.namespace) == pass_context.MAX_NAMESPACE
    pass_context.clear()


def test_pass_context_cursor_hook_is_bounded_and_quiet_outside_a_pass() -> None:
    pass_context.clear()
    assert pass_context.resume_cursor() == ""
    pass_context.set_cursor("ignored")  # no pass bound: a debug line, never an error
    ctx = pass_context.begin("w", resume_cursor="x" * 1000)
    assert len(ctx.resume_cursor) == pass_context.MAX_CURSOR
    pass_context.set_cursor("y" * 1000)
    assert len(ctx.cursor) == pass_context.MAX_CURSOR
    pass_context.clear()


async def test_pass_summaries_read_one_plane_by_name_prefix(ch: _FakeCH) -> None:
    ch.latest = [{"worker": "code_ingest", "total_runs": 3}]
    rows = await store.pass_summaries("code_", 14)
    assert rows == [{"worker": "code_ingest", "total_runs": 3}]
    sql, params = ch.queries[-1]
    assert params == {"prefix": "code_", "days": 14}
    assert "startsWith(worker, {prefix:String})" in sql and store.TABLE in sql
