"""Required work stays owned across entry drains, failure and cancellation."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from types import SimpleNamespace

import anyio
import pytest
from starlette.requests import ClientDisconnect

from server.core import ch, request_tail


@pytest.fixture
def world():
    tail = request_tail._Tail()
    token = request_tail._tail.set(tail)
    state = SimpleNamespace(tail=tail, events=[])
    try:
        yield state
    finally:
        request_tail._tail.reset(token)


def record(world, name):
    async def step():
        world.events.append(name)

    return step


async def test_required_registration_refuses_outside_a_live_tail(world):
    token = request_tail._tail.set(None)
    try:
        assert not request_tail.active() and not request_tail.required_active()
        assert not request_tail.defer_required(record(world, "unowned"))
        await request_tail.drain_required()
        await request_tail.settle_current()
    finally:
        request_tail._tail.reset(token)
    await request_tail.settle_current()
    assert not request_tail.defer_required(record(world, "closed"))
    assert world.events == []


async def test_ordinary_order_keyed_registration_and_best_effort_remain(world, caplog):
    async def broken():
        world.events.append("broken")
        raise ValueError("ordinary cleanup detail")

    assert request_tail.defer_once("claim", record(world, "claim"))
    assert request_tail.defer_once("claim", record(world, "duplicate"))
    assert request_tail.deferred("claim")
    assert request_tail.defer(broken)
    assert request_tail.defer(record(world, "last"))
    await request_tail.settle_current()
    await request_tail.settle_current()
    assert world.events == ["claim", "broken", "last"]
    assert "ordinary cleanup detail" in caplog.text
    assert not request_tail.active()


async def test_unmarked_cancelled_tail_does_not_resume_cleanup_before_headers(world):
    control = asyncio.CancelledError("ordinary cancellation")
    state = SimpleNamespace(tail=None)
    sent = []

    async def ordinary():
        world.events.append("ordinary")
        raise control

    async def app(scope, receive, send):
        state.tail = request_tail._tail.get()
        request_tail.defer(ordinary)
        request_tail.defer(record(world, "later"))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def send(message):
        sent.append(message)

    with pytest.raises(type(control)) as caught:
        await request_tail.RequestTailMiddleware(app)({"type": "http"}, None, send)
    assert caught.value is control
    await request_tail.settle(state.tail)
    assert world.events == ["ordinary"] and sent == []
    assert request_tail._tail.get() is world.tail


async def test_unmarked_cleanup_disconnect_keeps_best_effort_response(world, caplog):
    sent = []

    async def ordinary():
        world.events.append("ordinary")
        raise ClientDisconnect("ordinary cleanup disconnect")

    async def app(scope, receive, send):
        request_tail.defer(ordinary)
        request_tail.defer(record(world, "later"))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def send(message):
        sent.append(message["type"])

    await request_tail.RequestTailMiddleware(app)({"type": "http"}, None, send)
    assert world.events == ["ordinary", "later"]
    assert sent == ["http.response.start", "http.response.body"]
    assert "ordinary cleanup disconnect" in caplog.text
    assert request_tail._tail.get() is world.tail


async def test_required_entry_drains_preserve_ordinary_timing_keys_and_later_registration(world):
    required = record(world, "marked")
    assert request_tail.defer_once("claim", record(world, "claim"))
    assert request_tail.defer_required(required)
    assert request_tail.defer(record(world, "ordinary"))
    await request_tail.drain_required()
    assert world.events == ["marked"]
    assert request_tail.active() and request_tail.required_active()
    assert request_tail.deferred("claim")
    assert request_tail.defer_required(required)
    await request_tail.drain_required()
    assert world.events == ["marked", "marked"]
    await request_tail.settle_current()
    await request_tail.drain_required()
    assert world.events == ["marked", "marked", "claim", "ordinary"]


async def test_required_failure_drains_cleanup_and_stays_failed_without_backend_details(
    world, caplog
):
    async def broken():
        world.events.append("required")
        raise RuntimeError("private backend canary")

    request_tail.defer(record(world, "before"))
    request_tail.defer_required(broken)
    request_tail.defer(record(world, "after"))
    for settle in (
        request_tail.settle_current,
        request_tail.settle_current,
        request_tail.drain_required,
    ):
        with pytest.raises(request_tail.RequiredTailError) as caught:
            await settle()
        assert str(caught.value) == "Required request work was not confirmed"
        assert caught.value.__cause__ is None
    assert world.events == ["before", "required", "after"]
    assert "private backend canary" not in caplog.text
    assert not request_tail.required_active()
    assert not request_tail.defer_required(record(world, "later"))


async def test_failed_entry_drain_never_runs_ordinary_work_early(world):
    async def broken():
        world.events.append("required")
        raise ValueError("private backend detail")

    request_tail.defer(record(world, "ordinary"))
    request_tail.defer_required(broken)
    with pytest.raises(request_tail.RequiredTailError):
        await request_tail.drain_required()
    assert world.events == ["required"]
    assert request_tail.active() and not request_tail.required_active()
    with pytest.raises(request_tail.RequiredTailError):
        await request_tail.settle_current()
    assert world.events == ["required", "ordinary"]


async def test_cooperative_timeout_finishes_owned_callback_and_remaining_cleanup(
    world, monkeypatch
):
    monkeypatch.setattr(request_tail, "REQUIRED_STEP_TIMEOUT_S", 0)
    release = asyncio.Event()

    async def blocked():
        world.events.append("started")
        try:
            await release.wait()
        finally:
            world.events.append("ended")

    request_tail.defer_required(blocked)
    request_tail.defer(record(world, "cleanup"))
    with pytest.raises(request_tail.RequiredTailError):
        await request_tail.settle_current()
    assert world.events == ["started", "ended", "cleanup"]
    assert not any(task.get_name() == "request.required-tail" for task in asyncio.all_tasks())


@pytest.mark.parametrize("fails", [False, True])
async def test_concurrent_entry_and_final_drains_wait_for_one_required_attempt(world, fails):
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked():
        world.events.append("required")
        entered.set()
        await release.wait()
        if fails:
            raise ValueError("private failure")

    request_tail.defer(record(world, "ordinary"))
    request_tail.defer_required(blocked)
    first = asyncio.create_task(request_tail.drain_required())
    others = []
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        others = [
            asyncio.create_task(request_tail.drain_required()),
            asyncio.create_task(request_tail.settle_current()),
        ]
        await asyncio.sleep(0)
        assert not first.done() and not any(task.done() for task in others)
        assert not request_tail.defer_required(record(world, "too late"))
        release.set()
        results = await asyncio.gather(first, *others, return_exceptions=True)
        assert (
            all(isinstance(result, request_tail.RequiredTailError) for result in results)
            if fails
            else results == [None] * 3
        )
        assert world.events == ["required", "ordinary"]
    finally:
        release.set()
        for task in [first, *others]:
            if not task.done():
                task.cancel()
        await asyncio.gather(first, *others, return_exceptions=True)


@pytest.mark.parametrize("fails", [False, True])
async def test_repeated_caller_cancellation_waits_and_preserves_primary_without_raw_cause(
    world, fails
):
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked():
        world.events.append("required")
        entered.set()
        await release.wait()
        if fails:
            raise ValueError("private backend canary")

    request_tail.defer_required(blocked)
    task = asyncio.create_task(request_tail.drain_required())
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel("first caller cancellation")
        await asyncio.sleep(0)
        task.cancel("later caller cancellation")
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        assert caught.value.args == ("first caller cancellation",)
        assert caught.value.__cause__ is None
        assert world.events == ["required"]
        if fails:
            with pytest.raises(request_tail.RequiredTailError):
                await request_tail.settle_current()
        else:
            await request_tail.settle_current()
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_callback_self_cancellation_is_failed_delivery_not_caller_cancellation(world):
    async def cancelled():
        world.events.append("required")
        raise asyncio.CancelledError("private callback canary")

    request_tail.defer_required(cancelled)
    request_tail.defer(record(world, "cleanup"))
    with pytest.raises(request_tail.RequiredTailError):
        await request_tail.settle_current()
    assert world.events == ["required", "cleanup"]


async def test_owned_wrapper_starts_in_its_own_context_before_factory_returns(world, monkeypatch):
    factory = asyncio.eager_task_factory
    loop = asyncio.get_running_loop()
    previous_factory = loop.get_task_factory()
    caller = asyncio.current_task()
    value = ContextVar("required-tail-test-value", default="ordinary")
    state = SimpleNamespace(started=False, callback_task=None, factory_task=None)

    async def required():
        state.started = True
        state.callback_task = asyncio.current_task()
        value.set("private")
        world.events.append("required")

    def observed_factory(loop, coroutine, **kwargs):
        task = factory(loop, coroutine, **kwargs)
        assert state.started
        state.factory_task = task
        assert asyncio.current_task() is caller
        return task

    request_tail.defer_required(required)
    monkeypatch.setattr(request_tail.asyncio, "eager_task_factory", observed_factory)
    await request_tail.settle_current()
    assert state.callback_task is state.factory_task and state.callback_task is not caller
    assert state.factory_task.done() and world.events == ["required"]
    assert value.get() == "ordinary"
    assert loop.get_task_factory() is previous_factory


@pytest.mark.parametrize("disconnect", [False, True])
async def test_cancellation_after_owned_start_in_sole_final_settle_finishes_cleanup(
    world, monkeypatch, disconnect
):
    factory, shield = asyncio.eager_task_factory, asyncio.shield
    shield_calls = 0
    primary = ClientDisconnect()
    tasks = []

    def cancel_after_start(loop, coroutine, **kwargs):
        task = factory(loop, coroutine, **kwargs)
        assert world.events == ["required"]
        task.cancel("private callback cancellation")
        tasks.append(task)
        return task

    def bounded_shield(awaitable):
        nonlocal shield_calls
        shield_calls += 1
        if shield_calls > 4:
            raise AssertionError("terminal-task join did not finish")
        return shield(awaitable)

    async def app(scope, receive, send):
        async def required():
            world.events.append("required")
            try:
                await asyncio.Event().wait()
            finally:
                world.events.append("ended")

        request_tail.defer_required(required)
        request_tail.defer(record(world, "claim"))
        request_tail.defer(record(world, "cleanup"))
        if disconnect:
            raise primary

    with monkeypatch.context() as patch:
        patch.setattr(request_tail.asyncio, "eager_task_factory", cancel_after_start)
        patch.setattr(request_tail.asyncio, "shield", bounded_shield)
        with pytest.raises(
            ClientDisconnect if disconnect else request_tail.RequiredTailError
        ) as caught:
            await request_tail.RequestTailMiddleware(app)({"type": "http"}, None, None)
    if disconnect:
        assert caught.value is primary
    assert world.events == ["required", "ended", "claim", "cleanup"]
    assert request_tail._tail.get() is world.tail
    assert all(task.done() for task in tasks)


@pytest.mark.parametrize("outcome", ["return", "suspend", "raise"])
async def test_actual_callback_self_cancellation_is_constant_and_does_not_cancel_waiter(
    world, outcome
):
    caller = asyncio.current_task()
    before = caller.cancelling()

    async def required():
        world.events.append("required")
        assert asyncio.current_task() is not caller
        asyncio.current_task().cancel("private callback canary")
        if outcome == "suspend":
            await asyncio.sleep(0)
        if outcome == "raise":
            raise ValueError("private callback failure")

    request_tail.defer_required(required)
    request_tail.defer(record(world, "cleanup"))
    for _ in range(2):
        with pytest.raises(request_tail.RequiredTailError) as caught:
            await request_tail.settle_current()
        assert str(caught.value) == "Required request work was not confirmed"
        assert caught.value.__cause__ is None
        assert "private" not in str(caught.value)
    assert world.events == ["required", "cleanup"]
    assert caller.cancelling() == before


@pytest.mark.parametrize("cancel_waiter", [False, True])
async def test_cancelled_owned_lock_wait_finishes_cleanup_before_sole_final_owner_exits(
    world, monkeypatch, cancel_waiter
):
    factory = asyncio.eager_task_factory
    entered, release, waiting = asyncio.Event(), asyncio.Event(), asyncio.Event()
    state = SimpleNamespace(first=None, jobs=[])

    def observed_factory(loop, coroutine, **kwargs):
        task = factory(loop, coroutine, **kwargs)
        state.jobs.append(task)
        if len(state.jobs) == 2:
            waiting.set()
        return task

    async def required():
        world.events.append("required")
        entered.set()
        await release.wait()

    async def app(scope, receive, send):
        request_tail.defer_required(required)
        request_tail.defer(record(world, "claim"))
        request_tail.defer(record(world, "cleanup"))
        state.first = asyncio.create_task(request_tail.drain_required())
        await entered.wait()

    monkeypatch.setattr(request_tail.asyncio, "eager_task_factory", observed_factory)
    task = asyncio.create_task(
        request_tail.RequestTailMiddleware(app)({"type": "http"}, None, None)
    )
    try:
        await asyncio.wait_for(waiting.wait(), timeout=5)
        state.jobs[1].cancel("private lock cancellation")
        if cancel_waiter:
            task.cancel("real caller cancellation")
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(
            asyncio.CancelledError if cancel_waiter else request_tail.RequiredTailError
        ) as caught:
            await task
        if cancel_waiter:
            assert caught.value.args == ("real caller cancellation",)
        else:
            assert str(caught.value) == "Required request work was not confirmed"
        assert caught.value.__cause__ is None
        assert world.events == ["required", "claim", "cleanup"]
        assert state.first.done() and all(job.done() for job in state.jobs)
        assert request_tail._tail.get() is world.tail
    finally:
        release.set()
        for owned in [task, state.first]:
            if owned is not None and not owned.done():
                owned.cancel()
        await asyncio.gather(
            *(owned for owned in [task, state.first] if owned is not None), return_exceptions=True
        )


@pytest.mark.parametrize("disconnect", [False, True])
@pytest.mark.parametrize("outcome", ["bare_cancel", "return", "suspend", "raise_error"])
async def test_sole_final_settle_drains_required_work_after_ordinary_control_outcome(
    world, disconnect, outcome
):
    cancelled = asyncio.CancelledError("ordinary callback cancellation")
    primary = ClientDisconnect()
    caller = asyncio.current_task()
    before = caller.cancelling()
    state = SimpleNamespace(tail=None)

    async def ordinary():
        world.events.append("ordinary")
        assert asyncio.current_task() is not caller
        if outcome == "bare_cancel":
            raise cancelled
        asyncio.current_task().cancel("ordinary callback cancellation")
        if outcome == "suspend":
            await asyncio.sleep(0)
        if outcome == "raise_error":
            raise ValueError("ordinary callback failure")

    async def app(scope, receive, send):
        state.tail = request_tail._tail.get()
        request_tail.defer(ordinary)
        request_tail.defer_required(record(world, "required"))
        request_tail.defer(record(world, "cleanup"))
        if disconnect:
            raise primary

    with pytest.raises(ClientDisconnect if disconnect else asyncio.CancelledError) as caught:
        await request_tail.RequestTailMiddleware(app)({"type": "http"}, None, None)
    if disconnect:
        assert caught.value is primary
    else:
        assert caught.value.args == cancelled.args
        if outcome == "bare_cancel":
            assert caught.value is cancelled
    assert caller.cancelling() == before
    assert caught.value.__cause__ is None
    await request_tail.settle(state.tail)
    assert world.events == ["ordinary", "required", "cleanup"]
    assert request_tail._tail.get() is world.tail


async def test_required_callback_group_is_sanitized_and_finishes_remaining_cleanup(world, caplog):
    group = BaseExceptionGroup(
        "private group canary",
        [asyncio.CancelledError("private callback canary"), RuntimeError("private backend canary")],
    )

    async def grouped():
        world.events.append("required")
        raise group

    request_tail.defer_required(grouped)
    request_tail.defer(record(world, "cleanup"))
    with pytest.raises(request_tail.RequiredTailError) as caught:
        await request_tail.settle_current()
    assert str(caught.value) == "Required request work was not confirmed"
    assert caught.value.__cause__ is None
    assert world.events == ["required", "cleanup"]
    assert "canary" not in caplog.text
    with pytest.raises(request_tail.RequiredTailError):
        await request_tail.settle_current()


async def test_anyio_level_cancellation_is_observed_after_owned_join(world):
    entered, release = asyncio.Event(), asyncio.Event()
    state = SimpleNamespace(scope=None, cancelled=False)

    async def blocked():
        entered.set()
        await release.wait()
        world.events.append("committed")

    async def caller():
        with anyio.CancelScope() as scope:
            state.scope = scope
            try:
                await request_tail.drain_required()
                world.events.append("unexpected continuation")
            except asyncio.CancelledError:
                state.cancelled = True
                raise

    request_tail.defer_required(blocked)
    task = asyncio.create_task(caller())
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        state.scope.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        await task
        assert state.cancelled and world.events == ["committed"]
        await request_tail.settle_current()
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_completed_write_racing_with_caller_cancellation_keeps_cancellation(world):
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked():
        entered.set()
        await release.wait()
        world.events.append("committed")

    request_tail.defer_required(blocked)
    task = asyncio.create_task(request_tail.drain_required())
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        release.set()
        task.cancel("caller")
        with pytest.raises(asyncio.CancelledError):
            await task
        assert world.events == ["committed"]
        await request_tail.settle_current()
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("existing", [0, 0.25])
async def test_required_statement_cap_is_finite_and_never_widens_an_existing_limit(
    world, monkeypatch, existing
):
    monkeypatch.setattr(request_tail, "class_timeout_s", lambda kind: existing)
    values = []

    async def inspect_cap():
        values.append((ch.class_timeout_s("read"), ch.class_timeout_s("write")))

    request_tail.defer_required(inspect_cap)
    await request_tail.settle_current()
    expected = existing or request_tail.REQUIRED_STEP_TIMEOUT_S
    assert values == [(expected, expected)]


async def test_ordinary_and_required_steps_keep_their_captured_query_tags(world):
    values = []

    async def inspect_tag():
        values.append(ch.current_query_tag())

    with ch.query_tag("ordinary-tag"):
        request_tail.defer(inspect_tag)
    with ch.query_tag("required-tag"):
        request_tail.defer_required(inspect_tag)
    with ch.query_tag("later-tag"):
        await request_tail.settle_current()
        assert ch.current_query_tag() == "later-tag"
    assert values == ["ordinary-tag", "required-tag"]


@pytest.mark.parametrize(
    "mode",
    ["success", "app_error", "send_error", "cancel", "required_error", "disconnect", "no_response"],
)
async def test_middleware_restores_outer_context_and_preserves_primary_while_draining(world, mode):
    original = (
        asyncio.CancelledError("caller")
        if mode == "cancel"
        else ClientDisconnect()
        if mode == "disconnect"
        else ValueError("ordinary app error")
    )
    sent = []

    async def required():
        world.events.append("required")
        if mode in ("app_error", "cancel", "disconnect", "required_error"):
            raise ValueError("private backend canary")

    async def app(scope, receive, send):
        assert request_tail._tail.get() is not world.tail
        request_tail.defer_required(required)
        request_tail.defer(record(world, "cleanup"))
        if mode in ("app_error", "cancel", "disconnect"):
            raise original
        if mode != "no_response":
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"answer"})

    async def send(message):
        assert world.events == ["required", "cleanup"]
        if mode == "send_error":
            raise original
        sent.append(message)

    middleware = request_tail.RequestTailMiddleware(app)
    if mode in ("success", "no_response"):
        await middleware({"type": "http"}, None, send)
    else:
        expected = request_tail.RequiredTailError if mode == "required_error" else type(original)
        with pytest.raises(expected) as caught:
            await middleware({"type": "http"}, None, send)
        if mode != "required_error":
            assert caught.value is original
        assert sent == []
    assert request_tail._tail.get() is world.tail and request_tail.active()
    assert world.events == ["required", "cleanup"]
    assert not any(task.get_name() == "request.required-tail" for task in asyncio.all_tasks())
