"""Every supervised worker pass drains a cost accumulator into
one `agent_events` row (operation worker.<name>.pass, kind='worker'); idle passes
write no row (loom.worker_status records them)."""

from __future__ import annotations

import asyncio
import time

import pytest

from server.observability import cost_accumulator as _cost
from server.observability import events as ev_mod
from server.workers import supervisor as sup
from server.workers.registry import WorkerSpec


class _Emitter:
    def __init__(self) -> None:
        self.events: list = []

    def emit(self, event) -> None:
        self.events.append(event)


@pytest.fixture
def emitter(monkeypatch: pytest.MonkeyPatch) -> _Emitter:
    em = _Emitter()
    monkeypatch.setattr(ev_mod, "get_emitter", lambda: em)
    return em


@pytest.fixture(autouse=True)
def _unbind_pass_work() -> None:
    """`_begin_pass_accounting` also binds the ClickHouse work bag for the
    pass's status row. These tests call it in the
    test's own context, not a worker task, so the bag would stay bound and
    give later tests' statements a `:seq` query-id suffix
    (test_ch_statement_digest). Unbind after each test."""
    from server.core import request_work

    yield
    request_work._current.set(None)


def test_spending_pass_writes_a_row_with_model_and_cost(emitter: _Emitter) -> None:
    acc = sup._begin_pass_accounting()
    _cost.report_llm("gpt-4.1-mini", 2000, 300)
    _cost.report_embed("text-embedding-3-large", 120)
    sup._emit_pass_row("entity_extractor", acc, started=time.monotonic() - 0.25, success=True)
    (ev,) = emitter.events
    assert ev.operation == "worker.entity_extractor.pass" and ev.kind == "worker"
    assert ev.success and ev.error_code == ""
    assert ev.model == "gpt-4.1-mini" and ev.llm_calls == 1
    assert ev.input_tokens == 2000 and ev.output_tokens == 300 and ev.embedding_tokens == 120
    assert ev.upstream_cost_usd > 0
    assert ev.args == {"worker": "entity_extractor", "spent": "true"}
    assert ev.latency_ms >= 200
    # the accumulator was drained: a second pass starts from zero
    assert acc.llm_calls == 0 and acc.cost_usd == 0.0


def test_idle_pass_is_silent(emitter: _Emitter) -> None:
    acc = sup._begin_pass_accounting()
    sup._emit_pass_row("namespace_gc", acc, started=time.monotonic(), success=True)
    assert emitter.events == []


def test_failed_pass_writes_a_row_even_when_idle(emitter: _Emitter) -> None:
    acc = sup._begin_pass_accounting()
    sup._emit_pass_row(
        "projection_repair",
        acc,
        started=time.monotonic(),
        success=False,
        error_code="TimeoutError",
    )
    (ev,) = emitter.events
    assert (
        not ev.success
        and ev.error_code == "TimeoutError"
        and ev.operation == "worker.projection_repair.pass"
    )


def test_rows_can_be_switched_off(emitter: _Emitter, monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "worker_ledger_rows", False)
    acc = sup._begin_pass_accounting()
    _cost.report_llm("gpt-4.1-mini", 10, 10)
    sup._emit_pass_row("x", acc, started=time.monotonic(), success=True)
    assert emitter.events == []


def test_supervised_loop_accounts_the_pass_it_runs(emitter: _Emitter) -> None:
    """The accumulator bound before the pass is the one the pass reports
    into; the row carries the pass's spend and the supervisor stops after."""
    s = sup.WorkerSupervisor.__new__(sup.WorkerSupervisor)
    s._stopping = asyncio.Event()
    s._tasks = []
    s._workers = []

    async def _pass() -> None:
        _cost.report_llm("gpt-4.1-mini", 500, 50)
        s._stopping.set()

    async def _sleep(_: float) -> None:
        return None

    s._sleep_or_drain = _sleep  # type: ignore[method-assign]
    spec = WorkerSpec("probe_worker", 1.0, _pass)
    asyncio.run(s._supervise(spec))
    (ev,) = emitter.events
    assert (
        ev.operation == "worker.probe_worker.pass" and ev.llm_calls == 1 and ev.input_tokens == 500
    )


def test_failed_pass_carries_the_bounded_error(emitter: _Emitter) -> None:
    """A failed pass says why on its row."""
    acc = sup._begin_pass_accounting()
    try:
        raise OSError(24, "Too many open files", "/dev/null 'secret-path'")
    except OSError as exc:
        sup._emit_pass_row(
            "embedding_worker",
            acc,
            started=time.monotonic() - 0.1,
            success=False,
            error_code=type(exc).__name__,
            error=exc,
        )
    (ev,) = emitter.events
    assert ev.success is False and ev.error_code == "OSError"
    assert ev.args["error_class"] == "OSError"
    assert "Too many open files" in ev.args["error"]
    assert "secret-path" not in ev.args["error"]  # quoted spans are elided
    assert len(ev.args["error"]) <= 200


def test_the_pass_row_ticks_the_cost_counters_once(
    emitter: _Emitter, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.observability import metrics

    ticks: list[tuple[str, dict]] = []
    monkeypatch.setattr(metrics, "record_cost", lambda op, **kw: ticks.append((op, kw)))
    acc = sup._begin_pass_accounting()
    _cost.report_llm("gpt-4.1-mini", 1000, 100)
    sup._emit_pass_row("reflection_distill", acc, started=time.monotonic(), success=True)
    ((op, kw),) = ticks
    assert op == "worker.reflection_distill.pass" and kw["namespace"] == "-"
    assert kw["llm_input_tokens"] == 1000 and kw["llm_output_tokens"] == 100
    assert kw["cost_usd"] == emitter.events[0].upstream_cost_usd > 0


def test_spend_a_worker_drained_itself_is_not_counted_again(
    emitter: _Emitter, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.observability import metrics
    from server.workers.cost_event import emit_worker_cost_event_for

    ticks: list[str] = []
    monkeypatch.setattr(metrics, "record_cost", lambda op, **kw: ticks.append(op))
    acc = sup._begin_pass_accounting()
    _cost.report_embed("text-embedding-3-large", 50)
    emit_worker_cost_event_for(
        org="o", namespace="n", operation="worker.titler", started=time.monotonic()
    )
    sup._emit_pass_row("titler", acc, started=time.monotonic(), success=True)
    assert [e.operation for e in emitter.events] == ["worker.titler"]
    assert ticks == ["worker.titler"]
    assert emitter.events[0].embedding_tokens == 50 and emitter.events[0].namespace == "n"
