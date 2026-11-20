"""Owned-session cleanup delivers cancellation before aborting its transport."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from server.core import pg

pytestmark = pytest.mark.asyncio


class Connection:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.finished = asyncio.Event()
        self.aborted = False
        self.timeouts: list[float] = []

    async def close(self, *, timeout: float) -> None:  # noqa: ASYNC109 — driver API.
        self.timeouts.append(timeout)
        self.started.set()
        await self.finished.wait()

    def terminate(self) -> None:
        self.aborted = True
        self.finished.set()


async def test_public_close_completes_before_transport_abort() -> None:
    conn = Connection()
    conn.finished.set()
    await pg.close_owned(conn, end=time.monotonic() + 1)
    assert conn.timeouts and 0 < conn.timeouts[0] <= 1
    assert not conn.aborted


async def test_expired_deadline_aborts_without_starting_close() -> None:
    conn = Connection()
    with pytest.raises(TimeoutError):
        await pg.close_owned(conn, end=time.monotonic() - 1)
    assert conn.aborted and not conn.timeouts


async def test_close_timeout_aborts_within_the_original_deadline() -> None:
    conn = Connection()
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await pg.close_owned(conn, end=started + 0.03)
    assert conn.aborted and time.monotonic() - started < 0.3


async def test_outer_signals_do_not_abort_pending_public_close() -> None:
    conn = Connection()
    task = asyncio.create_task(pg.close_owned(conn, end=time.monotonic() + 0.5))
    try:
        await conn.started.wait()
        task.cancel("first close interruption")
        await asyncio.sleep(0)
        task.cancel("second close interruption")
        await asyncio.sleep(0)
        assert not task.done()
        assert not conn.aborted
        conn.finished.set()
        with pytest.raises(asyncio.CancelledError, match="first close interruption"):
            await task
        assert not conn.aborted
        assert len(conn.timeouts) == 1
    finally:
        conn.finished.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_self_cancelled_close_child_does_not_spin_to_deadline() -> None:
    class SelfCancelled(Connection):
        async def close(self, **_: Any) -> None:
            raise asyncio.CancelledError("close child cancelled")

    started = time.monotonic()
    with pytest.raises(asyncio.CancelledError, match="close child cancelled"):
        await pg.close_owned(SelfCancelled(), end=started + 10)
    assert time.monotonic() - started < 0.3


@pytest.mark.parametrize(
    "later",
    [
        ValueError("late close"),
        asyncio.CancelledError("close child"),
        SystemExit(17),
        KeyboardInterrupt(),
    ],
)
async def test_first_outer_cancel_survives_a_later_close_failure(later: BaseException) -> None:
    class Failing(Connection):
        async def close(self, **_: Any) -> None:
            self.started.set()
            await self.finished.wait()
            raise later

        def is_closed(self) -> bool:
            return True  # An aborted transport cannot prove cancellation acknowledgement.

    conn = Failing()

    async def captured() -> BaseException | None:
        try:
            await pg.close_owned(conn, end=time.monotonic() + 10)
        except BaseException as error:  # noqa: BLE001 — keep native failures inside the test task.
            return error
        return None

    task = asyncio.create_task(captured())
    try:
        await conn.started.wait()
        task.cancel("first close interruption")
        await asyncio.sleep(0)
        assert not task.done()
        assert not conn.aborted
        started = time.monotonic()
        conn.finished.set()
        result = await task
        assert isinstance(result, asyncio.CancelledError)
        assert str(result) == "first close interruption"
        assert time.monotonic() - started < 0.3
        assert conn.aborted
    finally:
        conn.finished.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("abort_error", [ValueError("abort"), SystemExit(17), KeyboardInterrupt()])
async def test_abort_failure_cannot_replace_the_close_failure(abort_error: BaseException) -> None:
    original = ValueError("original close failure")

    class Broken(Connection):
        async def close(self, **_: Any) -> None:
            raise original

        def terminate(self) -> None:
            raise abort_error

    with pytest.raises(ValueError) as result:
        await pg.close_owned(Broken(), end=time.monotonic() + 1)
    assert result.value is original


async def test_second_cancellation_waits_for_the_original_cleanup_end() -> None:
    conn = Connection()
    supervisor_started = asyncio.Event()
    original = asyncio.CancelledError("operation cancelled")

    async def supervisor() -> None:
        try:
            supervisor_started.set()
            await asyncio.Future()
        except asyncio.CancelledError:
            try:
                await pg.close_owned(conn, end=time.monotonic() + 0.03)
            except BaseException:  # noqa: BLE001 — preserve the original cancellation.
                pass
            raise original from None

    task = asyncio.create_task(supervisor())
    await supervisor_started.wait()
    task.cancel()
    await asyncio.wait_for(conn.started.wait(), 1)
    task.cancel()
    await asyncio.sleep(0)
    assert not conn.aborted
    with pytest.raises(asyncio.CancelledError, match="operation cancelled"):
        await asyncio.wait_for(task, 0.3)
    assert conn.aborted


async def test_failed_public_close_aborts_and_reports_failure() -> None:
    class Broken(Connection):
        async def close(self, **_: Any) -> None:
            raise ValueError("close failed")

    conn = Broken()
    with pytest.raises(ValueError, match="close failed"):
        await pg.close_owned(conn, end=time.monotonic() + 1)
    assert conn.aborted


async def test_expired_close_child_exception_is_retrieved() -> None:
    release = asyncio.Event()
    errors: list[dict[str, Any]] = []

    class LateFailure(Connection):
        async def close(self, **_: Any) -> None:
            self.started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            raise ValueError("late close failure")

    conn = LateFailure()
    loop = asyncio.get_running_loop()
    old_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _, context: errors.append(context))
    try:
        with pytest.raises(TimeoutError):
            await pg.close_owned(conn, end=time.monotonic() + 0.01)
        assert conn.aborted
        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not errors
    finally:
        release.set()
        loop.set_exception_handler(old_handler)


async def test_migration_failure_survives_cleanup_failure(monkeypatch, tmp_path) -> None:
    import contextlib

    from server.core.schema import pg_migrate

    class Broken(Connection):
        async def close(self, **_: Any) -> None:
            raise RuntimeError("cleanup failed")

    conn = Broken()

    @contextlib.asynccontextmanager
    async def connect():
        yield conn

    async def fail(*_: Any) -> None:
        raise ValueError("migration failed")

    monkeypatch.setattr(pg_migrate, "_migrate", fail)
    with pytest.raises(ValueError, match="migration failed"):
        await pg_migrate.migrate(tmp_path, connect=connect, schema="test")
    assert conn.aborted


async def test_successful_migration_still_reports_context_exit_failure(
    monkeypatch, tmp_path
) -> None:
    import contextlib

    from server.core.schema import pg_migrate

    original = ValueError("context exit failed")

    @contextlib.asynccontextmanager
    async def connect():
        yield Connection()
        raise original

    async def applied(*_: Any) -> list[str]:
        return ["001.sql"]

    monkeypatch.setattr(pg_migrate, "_migrate", applied)
    with pytest.raises(ValueError) as error:
        await pg_migrate.migrate(tmp_path, connect=connect, schema="test")
    assert error.value is original


@pytest.mark.parametrize("cancel_work", [False, True])
async def test_second_cancellation_during_migration_context_exit_keeps_first_failure(
    monkeypatch, tmp_path, cancel_work
) -> None:
    import contextlib

    from server.core.schema import pg_migrate

    conn = Connection()
    conn.finished.set()
    started, exiting = asyncio.Event(), asyncio.Event()
    observed: list[BaseException] = []
    original = ValueError("migration failed")

    @contextlib.asynccontextmanager
    async def connect():
        try:
            yield conn
        except BaseException as error:
            observed.append(error)
            exiting.set()
            await asyncio.Future()
            raise

    async def work(*_: Any) -> list[str]:
        started.set()
        if cancel_work:
            await asyncio.Future()
        raise original

    monkeypatch.setattr(pg_migrate, "_migrate", work)
    monkeypatch.setattr(pg, "_RELEASE_TIMEOUT_S", 0.1)
    task = asyncio.create_task(pg_migrate.migrate(tmp_path, connect=connect, schema="test"))
    await asyncio.wait_for(started.wait(), 1)
    before = time.monotonic()
    if cancel_work:
        task.cancel("first cancellation")
    await asyncio.wait_for(exiting.wait(), 1)
    task.cancel("second cancellation")
    with pytest.raises(BaseException) as result:
        await asyncio.wait_for(task, 0.3)
    assert time.monotonic() - before < 0.3
    assert result.value is observed[0]
    assert (
        isinstance(result.value, asyncio.CancelledError)
        if cancel_work
        else result.value is original
    )
    assert len(conn.timeouts) == 1
    assert conn.aborted


@pytest.mark.parametrize("seam", ["reap", "exit"])
@pytest.mark.parametrize("original_kind", ["ordinary", "cancel", "exit", "keyboard"])
@pytest.mark.parametrize("abort_kind", ["ordinary", "cancel", "exit", "keyboard"])
async def test_failed_migration_abort_preserves_first_and_finishes_context(
    monkeypatch, tmp_path, seam, original_kind, abort_kind
):
    from server.core.schema import pg_migrate

    def error(kind, message):
        return {
            "ordinary": ValueError(message),
            "cancel": asyncio.CancelledError(message),
            "exit": SystemExit(message),
            "keyboard": KeyboardInterrupt(message),
        }[kind]

    original, secondary = (
        error(original_kind, "migration failed"),
        error(abort_kind, "abort failed"),
    )
    events, first, exits, ends, retrieved, triples = [], [], [], [], [], []
    conn = Connection()
    real_wait, real_failed, real_retrieve = (
        asyncio.wait,
        pg_migrate._failed_migration,
        pg._retrieve_task,
    )

    class Context:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, kind, value, traceback):
            exits.append((kind, value, traceback))
            events.append("exit")
            if seam == "exit":
                raise OSError("context exit failed")

    async def migrate(*args):
        raise original

    async def close(_conn, *, end):
        assert _conn is conn
        ends.append(end)
        events.append("close")

    async def failed(cm, actual_conn, task, observed, end):
        first.append(observed)
        triples.append((type(observed), observed, observed.__traceback__))
        return await real_failed(cm, actual_conn, task, observed, end)

    async def wait(tasks, *, timeout):  # noqa: ASYNC109 — asyncio.wait test seam.
        assert 0 <= timeout <= max(0, ends[0] - time.monotonic()) + 0.01
        if seam == "reap" and "reap" not in events:
            events.append("reap")
            raise asyncio.CancelledError("second signal during reap")
        return await real_wait(tasks, timeout=timeout)

    def abort():
        events.append("abort")
        raise secondary

    def retrieve(task):
        retrieved.append(task)
        real_retrieve(task)

    monkeypatch.setattr(pg_migrate, "_migrate", migrate)
    monkeypatch.setattr(pg_migrate, "_failed_migration", failed)
    monkeypatch.setattr(pg, "close_owned", close)
    monkeypatch.setattr(conn, "terminate", abort)
    monkeypatch.setattr(asyncio, "wait", wait)
    monkeypatch.setattr(pg, "_retrieve_task", retrieve)
    try:
        await pg_migrate.migrate(tmp_path, connect=Context, schema="test")
    except BaseException as observed:  # noqa: BLE001 — native test failures must remain inert.
        result = observed
    else:
        pytest.fail("failed migration returned success")
    assert len(first) == 1 and result is first[0]
    if original_kind != "cancel":
        assert result is original
    assert exits == triples
    assert len(ends) == 1 and events.count("close") == events.count("abort") == 1
    assert events[-1] == ("exit" if seam == "reap" else "abort")
    for _ in range(3):
        await asyncio.sleep(0)
    assert len(retrieved) == 2 and all(task.done() for task in retrieved)
