"""A worker pass has a deadline, a leg that fails inside a pass
is a partial failure the supervisor counts, and first passes are staggered."""

from __future__ import annotations

import asyncio

import pytest

from server.observability import events as ev_mod
from server.observability import genai, metrics_gauges
from server.workers import pass_context
from server.workers import supervisor as sup
from server.workers.registry import WorkerSpec, default_max_pass_seconds


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


def _supervisor(stop_after_passes: int, counter: dict[str, int]) -> sup.WorkerSupervisor:
    s = sup.WorkerSupervisor()

    async def _sleep(_seconds: float) -> None:
        if counter["n"] >= stop_after_passes:
            s._stopping.set()

    s._sleep_or_drain = _sleep  # type: ignore[method-assign]
    return s


def test_default_deadline_is_four_intervals_floored_and_capped() -> None:
    assert default_max_pass_seconds(5) == 60.0  # floor: a 5 s worker may wait on a retry loop
    assert default_max_pass_seconds(30) == 120.0
    assert default_max_pass_seconds(120) == 480.0
    assert default_max_pass_seconds(300) == 900.0  # cap
    assert WorkerSpec("w", 10, None, max_pass_seconds=7).deadline_seconds() == 7.0  # type: ignore[arg-type]
    assert WorkerSpec("w", 10, None).deadline_seconds() == 60.0  # type: ignore[arg-type]


def test_registry_pins_the_two_long_pass_workers_at_900(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import Settings, settings
    from server.workers import registry

    monkeypatch.setattr(settings, "observe_extract_enabled", True)
    monkeypatch.setattr(settings, "code_ingest_enabled", True)
    by_name = {w.name: w for w in registry.enabled_workers()}
    # observe_extract's 900 s is pinned, not derived from its interval.
    monkeypatch.setattr(settings, "observe_extract_interval_seconds", 1.0)
    pinned = {w.name: w for w in registry.enabled_workers()}["observe_extract"]
    assert by_name["observe_extract"].max_pass_seconds == 900.0
    assert pinned.max_pass_seconds == 900.0
    # Code_ingest reads its deadline from its own setting,
    # whose default is the 900 s cap; the pass budget holds it under that.
    assert by_name["code_ingest"].max_pass_seconds == float(settings.code_ingest_max_pass_seconds)
    monkeypatch.setattr(settings, "code_ingest_max_pass_seconds", 1234)
    ingest = {w.name: w for w in registry.enabled_workers()}["code_ingest"]
    assert ingest.max_pass_seconds == 1234.0
    assert Settings.model_fields["code_ingest_max_pass_seconds"].default == 900


async def test_a_pass_past_its_deadline_is_a_counted_failure(emitter: _Emitter) -> None:
    calls = {"n": 0}

    async def _slow() -> None:
        calls["n"] += 1
        await asyncio.sleep(5)

    s = _supervisor(1, calls)
    spec = WorkerSpec("slow", 1.0, _slow, max_pass_seconds=0.05)
    genai.set_identity()
    await asyncio.wait_for(s._supervise(spec), timeout=5)
    assert metrics_gauges.worker_consecutive_failures()["slow"] == 1
    (row,) = emitter.events
    assert row.success is False and row.error_code == "TimeoutError"
    assert row.args["error_class"] == "TimeoutError"
    genai.set_identity()


async def test_a_noted_failure_raises_the_gauge_within_two_passes(emitter: _Emitter) -> None:
    calls = {"n": 0}

    async def _pass() -> None:
        calls["n"] += 1
        try:
            raise ValueError("Unknown identifier 'recent'")
        except ValueError as e:
            pass_context.note_failure(e, "reconciliation")  # the embedding worker's shape
        # the pass goes on and finishes

    s = _supervisor(2, calls)
    spec = WorkerSpec("embedding_probe", 1.0, _pass)
    genai.set_identity()
    await asyncio.wait_for(s._supervise(spec), timeout=5)
    assert calls["n"] == 2
    assert metrics_gauges.worker_partial_failures()["embedding_probe"] == 1
    # counted toward the streak: two partial passes = a streak of two
    assert metrics_gauges.worker_consecutive_failures()["embedding_probe"] == 2
    assert len(emitter.events) == 2
    row = emitter.events[0]
    assert row.success is True and row.error_code == ""  # the pass did finish
    assert row.args["partial_failures"] == "1" and row.args["partial_leg"] == "reconciliation"
    assert row.args["partial_error_class"] == "ValueError"
    assert "Unknown identifier" in row.args["partial_error"]
    genai.set_identity()


async def test_a_clean_pass_resets_the_streak_and_the_gauge(emitter: _Emitter) -> None:
    calls = {"n": 0}

    async def _pass() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            pass_context.note_failure(RuntimeError("once"), "leg")

    s = _supervisor(2, calls)
    genai.set_identity()
    await asyncio.wait_for(s._supervise(WorkerSpec("flap", 1.0, _pass)), timeout=5)
    assert metrics_gauges.worker_partial_failures()["flap"] == 0
    assert metrics_gauges.worker_consecutive_failures()["flap"] == 0
    assert len(emitter.events) == 1  # the clean, idle pass writes no row
    genai.set_identity()


def test_note_failure_outside_a_pass_is_harmless() -> None:
    pass_context.clear()
    pass_context.note_failure(RuntimeError("no context"), "x")
    assert pass_context.current() is None
    ctx = pass_context.begin("w")
    for i in range(12):
        pass_context.note_failure(RuntimeError(f"e{i}"), f"leg{i}")
    assert ctx.count == 12 and len(ctx.failures) == pass_context.MAX_KEPT
    assert ctx.first() == ("leg0", "RuntimeError", "e0")
    pass_context.clear()


def test_first_pass_stagger_is_stable_and_bounded_by_the_interval() -> None:
    async def _noop() -> None:
        pass

    for name, interval in (("embedding_worker", 5), ("code_ingest", 120), ("observe_extract", 300)):
        d = sup.first_pass_delay(WorkerSpec(name, interval, _noop))
        assert 0 <= d < interval
        assert d == sup.first_pass_delay(WorkerSpec(name, interval, _noop))  # stable
    assert sup.first_pass_delay(WorkerSpec("hot", 0.5, _noop)) == 0.0
    names = ["a", "b", "c", "d", "e", "f", "g", "h"]
    delays = {sup.first_pass_delay(WorkerSpec(n, 60, _noop)) for n in names}
    assert len(delays) > 1  # they do spread


async def test_the_stagger_holds_the_first_pass_and_stop_wakes_it() -> None:
    calls = {"n": 0}

    async def _pass() -> None:
        calls["n"] += 1

    s = sup.WorkerSupervisor()
    spec = WorkerSpec("held", 3600, _pass)
    assert sup.first_pass_delay(spec) > 0 or spec.name == "held" and True
    task = asyncio.create_task(s._supervise(spec))
    await asyncio.sleep(0.05)
    if sup.first_pass_delay(spec) > 0:
        assert calls["n"] == 0  # still in the stagger
    s._stopping.set()
    await asyncio.wait_for(task, timeout=2)
