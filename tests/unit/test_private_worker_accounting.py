"""Private source work stays masked after its local context has exited."""

from __future__ import annotations

import time
from contextvars import ContextVar
from types import SimpleNamespace
from typing import Any

import pytest

from server.cli import knowledge as cli
from server.knowledge.app import private_sync, source_sync
from server.knowledge.workers import source_sync as worker
from server.observability import cost_accumulator, events
from server.workers import pass_context, status, status_store, supervisor
from tests.unit.test_private_diagnostics import spans as spans  # real recording parent fixture


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pass_context, "_current", ContextVar("test_pass", default=None))
    monkeypatch.setattr(cost_accumulator, "_current", ContextVar("test_cost", default=None))
    monkeypatch.setattr(status, "_registry", status.WorkerStatusRegistry())


async def test_private_worker_wrapper_omits_root_span_and_registry_figures(
    monkeypatch: pytest.MonkeyPatch, spans: Any
) -> None:
    tracer, exporter = spans

    async def run() -> source_sync.PassResult:
        with private_sync.scope():
            pass_context.note_failure(RuntimeError("private title"), "private address")
        return source_sync.PassResult(changed=10000)

    monkeypatch.setattr(worker, "run_sync", run)
    with pass_context.ensure_pass(worker.WORKER):
        await worker.run_pass()
    shown = status.get_registry().snapshot()[worker.WORKER]
    assert shown["last_duration_ms"] is None
    assert shown["last_outcome"] == {"code": "SYNC_FAILED"}
    assert exporter.get_finished_spans() == ()


def test_private_status_row_has_no_work_figures_duration_or_failure_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: dict[str, Any] = {}
    monkeypatch.setattr(status_store, "record_pass", lambda name, **kw: recorded.update(kw))
    with pass_context.ensure_pass(worker.WORKER) as ctx:
        ctx.private_work = True
        ctx.count = 17
        ctx.failures = [("private address", "OSError", "private title")]
        ctx.outcome = {"items_done": 9000, "rows_written": 9000, "error": "private title"}
        ctx.cursor = '["source metadata", "org hash"]'
        state = supervisor.LoopState()
        supervisor._record_status_row(
            worker.WORKER, started=time.monotonic() - 17, success=True, partial=ctx, state=state
        )
    assert recorded["started"] == recorded["finished"]
    assert recorded["error"] == "SYNC_FAILED"
    assert recorded["partial_failures"] == 0
    assert recorded.get("items_done", 0) == recorded.get("rows_written", 0) == 0
    assert "private" not in str(recorded)
    assert state.cursor == '["source metadata", "org hash"]'


def test_private_pass_ledger_keeps_only_ordinary_spend_without_pass_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted: list[Any] = []
    monkeypatch.setattr(events, "get_emitter", lambda: SimpleNamespace(emit=emitted.append))
    with pass_context.ensure_pass(worker.WORKER) as ctx:
        ctx.private_work = True
        ctx.count = 17
        ctx.failures = [("private address", "OSError", "private title")]
        acc = cost_accumulator.begin_op()
        cost_accumulator.report_embed("fake-ngram-1024", 2)
        supervisor._emit_pass_row(
            worker.WORKER,
            acc,
            started=time.monotonic() - 17,
            success=True,
            partial=ctx,
            outcome={"rows_written": 9000},
        )
    [event] = emitted
    assert event.embedding_tokens == 2
    assert event.latency_ms == 0
    assert event.args == {}
    assert "private" not in str(event)


def test_private_completed_pass_emits_no_count_or_streak_metrics_or_logs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    published: list[Any] = []
    monkeypatch.setattr(supervisor, "_publish_partial", lambda *args: published.append(args))
    monkeypatch.setattr(supervisor, "_publish_streak", lambda *args: published.append(args))
    monkeypatch.setattr(supervisor, "_finish_pass", lambda *args, **kwargs: None)
    with pass_context.ensure_pass(worker.WORKER) as ctx:
        ctx.private_work = True
        ctx.count = 17
        ctx.failures = [("private address", "OSError", "private title")]
        supervisor._after_pass(
            SimpleNamespace(name=worker.WORKER),
            None,
            ctx,
            supervisor.LoopState(),
            started=time.monotonic() - 17,
            streak=1,
        )
    assert published == []
    assert not caplog.records


async def test_manual_private_sync_persists_codes_without_duration_or_figures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: dict[str, Any] = {}

    async def record(name: str, **kw: Any) -> None:
        recorded.update(kw)

    monkeypatch.setattr(status_store, "record_manual_pass", record)
    result = source_sync.PassResult(changed=10000)
    result.private_work = True
    await cli._record("team", time.monotonic() - 17, result)
    assert recorded["started"] == recorded["finished"]
    assert recorded.get("rows_written", 0) == 0
    assert recorded.get("partial_failures", 0) == 0
    assert "10000" not in str(recorded)


def test_private_failures_never_increase_a_later_ordinary_pass_streak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[Any] = []
    monkeypatch.setattr(supervisor, "_publish_partial", lambda *args: None)
    monkeypatch.setattr(supervisor, "_publish_streak", lambda *args: published.append(args))
    monkeypatch.setattr(supervisor, "_finish_pass", lambda *args, **kwargs: None)
    spec = SimpleNamespace(name=worker.WORKER)
    ctx = pass_context.PassContext(worker=worker.WORKER, private_work=True, count=17)
    streak = supervisor._after_pass(
        spec, None, ctx, supervisor.LoopState(), started=time.monotonic(), streak=0
    )
    ordinary = pass_context.PassContext(worker=worker.WORKER, count=1)
    streak = supervisor._after_pass(
        spec, None, ordinary, supervisor.LoopState(), started=time.monotonic(), streak=streak
    )
    assert streak == 1
    assert published == [(worker.WORKER, 1)]


async def test_private_worker_error_log_has_no_aggregate_count(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def run() -> source_sync.PassResult:
        with private_sync.scope():
            pass_context.note_failure(RuntimeError("SYNC_FAILED"))
        return source_sync.PassResult(
            private_work=True, errors=["source-one: SYNC_FAILED", "source-two: SYNC_FAILED"]
        )

    monkeypatch.setattr(worker, "run_sync", run)
    await worker.run_pass()
    assert caplog.messages == [f"{worker.WORKER}: SYNC_FAILED"]


async def test_private_exception_history_stays_out_of_later_ordinary_streaks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from server.workers.registry import WorkerSpec

    calls = 0
    published: list[Any] = []
    loop = supervisor.WorkerSupervisor()

    async def run() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            with private_sync.scope():
                raise RuntimeError("private fault")
        raise ValueError("ordinary fault")

    async def sleep(seconds: float) -> None:
        if calls >= 2:
            loop._stopping.set()

    async def begin(spec: Any) -> Any:
        return 0.0, supervisor.LoopState()

    monkeypatch.setattr(loop, "_sleep_or_drain", sleep)
    monkeypatch.setattr(supervisor, "_begin_loop", begin)
    monkeypatch.setattr(supervisor, "_publish_partial", lambda *args: None)
    monkeypatch.setattr(supervisor, "_publish_streak", lambda *args: published.append(args))
    monkeypatch.setattr(supervisor, "_finish_pass", lambda *args, **kwargs: None)
    await asyncio.wait_for(loop._supervise(WorkerSpec(worker.WORKER, 1.0, run)), timeout=2.0)
    assert published == [(worker.WORKER, 1)]
