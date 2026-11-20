"""Directory control closes its actual backend before an interrupted call returns."""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest

from server.router import directory
from server.settings import settings
from tests.integration._pg_database import create_database, drop_database
from tests.integration.test_pg_owned_cleanup import released, sleeping

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def directory_database(monkeypatch) -> AsyncIterator[Any]:
    import asyncpg

    admin = os.environ.get("LOOM_PG_DSN") or pytest.skip("LOOM_PG_DSN not set")
    name, dsn = await create_database(admin)
    async with contextlib.AsyncExitStack() as cleanup:
        try:
            cleanup.push_async_callback(drop_database, admin, name)
            monkeypatch.setattr(settings, "router_pg_dsn", dsn)
            monkeypatch.setattr(directory, "_pool", None)
            observer = await asyncpg.connect(dsn)
            cleanup.push_async_callback(observer.close, timeout=1)
            cleanup.push_async_callback(directory.close)
            await observer.execute(
                "CREATE TABLE probe(id int PRIMARY KEY); INSERT INTO probe VALUES(1)"
            )
            yield observer
        except BaseException:  # Owned cleanup cannot replace the first failure.
            with contextlib.suppress(BaseException):
                await cleanup.aclose()
            raise


async def locked(conn):
    await conn.execute("SELECT pg_advisory_xact_lock($1)", directory._DIRECTORY_LOCK)
    await conn.execute("SELECT * FROM probe FOR UPDATE")


@pytest.mark.parametrize("interrupt", ["cancel", "deadline"])
async def test_directory_interrupt_releases_exact_backend_and_global_locks(
    directory_database, monkeypatch, interrupt
):
    seen = asyncio.Event()
    backend = {}
    monkeypatch.setattr(directory, "CONTROL_MAX_S", 0.5)

    async def work(conn):
        backend["pid"] = conn.get_server_pid()
        await locked(conn)
        seen.set()
        await conn.execute("SELECT pg_sleep(10)")

    task = asyncio.create_task(
        directory._control_transaction(work, seconds=directory.CONTROL_MAX_S)
    )
    try:
        await asyncio.wait_for(seen.wait(), 2)
        await sleeping(directory_database, backend["pid"])
        began = time.monotonic()
        if interrupt == "cancel":
            task.cancel("original directory cancellation")
        with pytest.raises(
            asyncio.CancelledError if interrupt == "cancel" else directory.DirectoryUnavailable
        ):
            await asyncio.wait_for(task, 1)
        assert time.monotonic() - began < 0.6
        await released(directory_database, backend["pid"])
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_directory_original_failure_never_waits_for_rollback(directory_database, monkeypatch):
    import asyncpg

    original = ValueError("original directory work failure")
    real = asyncpg.Connection.execute
    backend = {}
    monkeypatch.setattr(directory, "CONTROL_MAX_S", 0.4)

    async def stalled(self, query, *args, **kwargs):
        if query.startswith("ROLLBACK"):
            backend["rollback"] = True
            await real(self, "SELECT pg_sleep(10)")
        return await real(self, query, *args, **kwargs)

    monkeypatch.setattr(asyncpg.Connection, "execute", stalled)
    began = time.monotonic()

    async def work(conn):
        backend["pid"] = conn.get_server_pid()
        await locked(conn)
        raise original

    with pytest.raises(ValueError) as caught:
        await directory._control_transaction(work, seconds=directory.CONTROL_MAX_S)
    assert caught.value is original and "rollback" not in backend
    assert time.monotonic() - began < 0.4
    await released(directory_database, backend["pid"])


async def test_directory_normal_commit_closes_before_return(directory_database):
    backend = {}

    async def work(conn):
        backend["pid"] = conn.get_server_pid()
        await locked(conn)
        await conn.execute("UPDATE probe SET id=2")
        return "acknowledged"

    assert (
        await directory._control_transaction(work, seconds=directory.CONTROL_MAX_S)
        == "acknowledged"
    )
    assert await directory_database.fetchval("SELECT id FROM probe") == 2
    await released(directory_database, backend["pid"])


async def test_directory_second_cancel_keeps_first_and_close_precedes_worker_cancel(
    directory_database, monkeypatch
):
    import asyncpg

    monkeypatch.setattr(directory, "CONTROL_MAX_S", 0.4)
    real_close = asyncpg.Connection.close
    seen, closing, allow_close = asyncio.Event(), asyncio.Event(), asyncio.Event()
    backend = {}
    events = []

    async def work(conn):
        backend["pid"] = conn.get_server_pid()
        await locked(conn)
        seen.set()
        try:
            await conn.execute("SELECT pg_sleep(10)")
        finally:
            events.append("work stopped")

    async def close(self, **kwargs):
        if self.get_server_pid() == backend.get("pid"):
            events.append("close started")
            await real_close(self, **kwargs)
            closing.set()
            await allow_close.wait()
        else:
            await real_close(self, **kwargs)

    monkeypatch.setattr(asyncpg.Connection, "close", close)
    task = asyncio.create_task(
        directory._control_transaction(work, seconds=directory.CONTROL_MAX_S)
    )
    try:
        await asyncio.wait_for(seen.wait(), 2)
        await sleeping(directory_database, backend["pid"])
        task.cancel("first directory cancellation")
        await asyncio.wait_for(closing.wait(), 1)
        task.cancel("second directory cancellation")
        with pytest.raises(asyncio.CancelledError, match="first directory cancellation"):
            await asyncio.wait_for(task, 0.5)
        assert events.index("close started") < events.index("work stopped")
        assert events.count("close started") == 1
        await released(directory_database, backend["pid"])
    finally:
        allow_close.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_directory_commit_acknowledgement_loss_is_not_success(
    directory_database, monkeypatch
):
    import asyncpg

    real = asyncpg.Connection.execute
    backend = {}
    original = OSError("commit acknowledgement lost")

    async def execute(self, sql, *args, **kwargs):
        value = await real(self, sql, *args, **kwargs)
        if sql.startswith("COMMIT") and self.get_server_pid() == backend.get("pid"):
            raise original
        return value

    async def work(conn):
        backend["pid"] = conn.get_server_pid()
        await locked(conn)
        await conn.execute("UPDATE probe SET id=2")

    monkeypatch.setattr(asyncpg.Connection, "execute", execute)
    with pytest.raises(directory.DirectoryUnavailable) as caught:
        await directory._control_transaction(work, seconds=directory.CONTROL_MAX_S)
    assert caught.value.__cause__ is original
    assert await directory_database.fetchval("SELECT id FROM probe") == 2
    await released(directory_database, backend["pid"])


async def test_directory_required_close_failure_refuses_committed_success(
    directory_database, monkeypatch
):
    import asyncpg

    real_close = asyncpg.Connection.close
    backend = {}
    original = ValueError("required close failed")

    async def close(self, **kwargs):
        ours = self.get_server_pid() == backend.get("pid")
        await real_close(self, **kwargs)
        if ours:
            raise original

    async def work(conn):
        backend["pid"] = conn.get_server_pid()
        await locked(conn)
        await conn.execute("UPDATE probe SET id=2")

    monkeypatch.setattr(asyncpg.Connection, "close", close)
    with pytest.raises(ValueError) as caught:
        await directory._control_transaction(work, seconds=directory.CONTROL_MAX_S)
    assert caught.value is original
    assert await directory_database.fetchval("SELECT id FROM probe") == 2
    await released(directory_database, backend["pid"])


@pytest.mark.parametrize("stage", ["acquire", "work", "commit", "close", "release"])
@pytest.mark.parametrize("kind", ["exit", "keyboard"])
async def test_directory_fatal_child_is_inert_until_owned_cleanup(directory_database, stage, kind):
    import sys

    from tests.integration._pg_database import with_database

    dsn = with_database(
        os.environ["LOOM_PG_DSN"], await directory_database.fetchval("SELECT current_database()")
    )
    script = r"""
import asyncio, contextlib, os, sys
import asyncpg
from server.router import directory
from server.settings import settings
stage, kind = sys.argv[1:]
original = SystemExit(17) if kind == 'exit' else KeyboardInterrupt('fatal guard')
async def main():
 settings.router_pg_dsn = os.environ['CONTROL_TEST_DSN']
 observer = await asyncpg.connect(settings.router_pg_dsn)
 backend = {}
 real_close = asyncpg.Connection.close
 real_execute = asyncpg.Connection.execute
 real_connection = directory._connection
 async def execute(self, sql, *args, **kw):
  if stage == 'commit' and sql.startswith('COMMIT'): raise original
  return await real_execute(self, sql, *args, **kw)
 async def close(self, **kw):
  ours = self.get_server_pid() == backend.get('pid')
  await real_close(self, **kw)
  if ours and stage == 'close': raise original
 @contextlib.asynccontextmanager
 async def connection():
  if stage == 'acquire': raise original
  async with real_connection() as conn:
   yield conn
  if stage == 'release': raise original
 async def work(conn):
  backend['pid'] = conn.get_server_pid()
  await conn.execute('SELECT pg_advisory_xact_lock($1)', directory._DIRECTORY_LOCK)
  await conn.execute('SELECT * FROM probe FOR UPDATE')
  if stage == 'work': raise original
 asyncpg.Connection.close = close
 asyncpg.Connection.execute = execute
 directory._connection = connection
 try:
  try: await directory._control_transaction(work, seconds=directory.CONTROL_MAX_S)
  except (SystemExit, KeyboardInterrupt) as caught: assert caught is original
  else: raise AssertionError('fatal guard returned success')
  if stage == 'acquire':
   assert not backend and directory._pool is None
  else:
   assert not await observer.fetchval('SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=$1)', backend['pid'])
   assert not await observer.fetchval('SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE pid=$1)', backend['pid'])
 finally:
  asyncpg.Connection.close = real_close
  asyncpg.Connection.execute = real_execute
  await directory.close()
  await observer.close(timeout=1)
asyncio.run(main())
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        stage,
        kind,
        env={**os.environ, "CONTROL_TEST_DSN": dsn},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
        assert process.returncode == 0, (stdout.decode(), stderr.decode())
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


@pytest.mark.parametrize("kind", ["ordinary", "cancel", "exit", "keyboard"])
@pytest.mark.parametrize("healthy_loan", [False, True])
@pytest.mark.parametrize("driver_close", ["normal", "exit", "keyboard"])
async def test_failed_codec_raw_session_is_disposed_without_touching_other_loans(
    directory_database, monkeypatch, kind, healthy_loan, driver_close
):
    import asyncpg

    original = {
        "ordinary": ValueError("fixed codec failed"),
        "cancel": asyncio.CancelledError("fixed codec cancelled"),
        "exit": SystemExit(17),
        "keyboard": KeyboardInterrupt("fixed codec interrupted"),
    }[kind]
    pool = await directory.pool()
    assert pool.get_size() == 0  # Pool object creation does not open a backend.
    healthy = await pool.acquire() if healthy_loan else None
    healthy_pid = healthy.get_server_pid() if healthy else None
    real_codec, real_close = asyncpg.Connection.set_type_codec, asyncpg.Connection.close
    failed = {}
    attempts = 0

    async def codec(self, *args, **kwargs):
        nonlocal attempts
        attempts += 1
        failed["raw"], failed["pid"] = self, self.get_server_pid()
        raise original

    async def close(self, **kwargs):
        if self is failed.get("raw"):
            # This is the failed, never-borrowed raw codec session, not an active
            # command session: synchronous quarantine must precede driver cleanup.
            assert self.is_closed()
            failed["driver close"] = True
            if driver_close == "exit":
                raise SystemExit(21)
            if driver_close == "keyboard":
                raise KeyboardInterrupt("secondary closed-raw cleanup failure")
        return await real_close(self, **kwargs)

    async def acquire():
        async with directory._connection() as conn:
            pytest.fail(f"failed initializer admitted backend {conn.get_server_pid()}")

    monkeypatch.setattr(asyncpg.Connection, "set_type_codec", codec)
    monkeypatch.setattr(asyncpg.Connection, "close", close)
    try:
        if kind in {"exit", "keyboard"}:
            result = await directory._control_result(acquire)
            assert isinstance(result, directory._ControlFatal) and result.error is original
        else:
            with pytest.raises(type(original)) as caught:
                await acquire()
            assert caught.value is original
        assert failed["driver close"] and attempts == 1
        await released(directory_database, failed["pid"])
        # Driver background replenishment must not swallow/retry control intent.
        for _ in range(3):
            await asyncio.sleep(0)
        assert attempts == 1
        if healthy:
            assert healthy.get_server_pid() == healthy_pid
            assert await healthy.fetchval("SELECT 1") == 1
        monkeypatch.setattr(asyncpg.Connection, "set_type_codec", real_codec)
        async with directory._connection() as recovered:
            recovered_pid = recovered.get_server_pid()
            assert recovered_pid not in {failed["pid"], healthy_pid}
            assert await recovered.fetchval("SELECT 1") == 1
        async with directory._connection() as cached:
            assert cached.get_server_pid() == recovered_pid
    finally:
        monkeypatch.setattr(asyncpg.Connection, "set_type_codec", real_codec)
        if healthy:
            await pool.release(healthy)


@pytest.mark.parametrize("kind", ["exit", "keyboard"])
@pytest.mark.parametrize("secondary", ["ordinary", "exit", "keyboard"])
async def test_codec_quarantine_failure_keeps_native_original_and_unknown_diagnostic(
    directory_database, monkeypatch, kind, secondary
):
    import asyncpg

    original = SystemExit(17) if kind == "exit" else KeyboardInterrupt("codec interrupted")
    second = {
        "ordinary": ValueError("termination failed"),
        "exit": SystemExit(19),
        "keyboard": KeyboardInterrupt("termination interrupted"),
    }[secondary]
    real_codec, real_terminate = asyncpg.Connection.set_type_codec, asyncpg.Connection.terminate
    failed = {}

    async def codec(self, *args, **kwargs):
        failed["raw"], failed["pid"] = self, self.get_server_pid()
        raise original

    def terminate(self):
        if self is failed.get("raw"):
            raise second
        return real_terminate(self)

    async def acquire():
        async with directory._connection():
            pytest.fail("failed initialization returned success")

    monkeypatch.setattr(asyncpg.Connection, "set_type_codec", codec)
    monkeypatch.setattr(asyncpg.Connection, "terminate", terminate)
    try:
        result = await directory._control_result(acquire)
        assert isinstance(result, directory._ControlFatal) and result.error is original
        assert original.__notes__ == ["directory initialization session closure is unknown"]
        # Termination's request is not the proof; here the driver's subsequent
        # ordinary-error public close really completes and the observer proves it.
        await released(directory_database, failed["pid"])
    finally:
        monkeypatch.setattr(asyncpg.Connection, "set_type_codec", real_codec)
        monkeypatch.setattr(asyncpg.Connection, "terminate", real_terminate)
        raw = failed.get("raw")
        if raw is not None:
            real_terminate(raw)


@pytest.mark.parametrize("signal_count", [1, 2])
async def test_directory_signals_keep_real_cancel_request_alive_until_original_end(
    directory_database, monkeypatch, signal_count
):
    from asyncpg import connect_utils

    seen, cancelling, allow_cancel = asyncio.Event(), asyncio.Event(), asyncio.Event()
    backend = {}
    first_signal = {}
    real_wait = directory._ControlOwner.wait
    real_cancel = connect_utils._cancel
    seconds = 0.5

    async def cancel(**kwargs):
        if kwargs["backend_pid"] == backend.get("pid"):
            cancelling.set()
            await allow_cancel.wait()
        return await real_cancel(**kwargs)

    async def work(conn):
        backend["pid"] = conn.get_server_pid()
        await locked(conn)
        seen.set()
        await conn.execute("SELECT pg_sleep(10)")

    async def remember(self, task, end):
        try:
            return await real_wait(self, task, end)
        except asyncio.CancelledError as error:
            first_signal.setdefault("error", error)
            raise

    monkeypatch.setattr(directory._ControlOwner, "wait", remember)
    monkeypatch.setattr(connect_utils, "_cancel", cancel)
    began = time.monotonic()
    task = asyncio.create_task(directory._control_transaction(work, seconds=seconds))
    try:
        await asyncio.wait_for(seen.wait(), 2)
        await sleeping(directory_database, backend["pid"])
        task.cancel("first directory signal")
        await asyncio.wait_for(cancelling.wait(), 2)
        if signal_count == 2:
            task.cancel("second directory signal")
        await asyncio.sleep(0.03)
        assert not task.done(), "second signal abandoned a live server cancel request"
        assert await directory_database.fetchval(
            "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE pid=$1 AND wait_event='PgSleep')",
            backend["pid"],
        )
        assert (
            await directory_database.fetchval(
                "SELECT count(*) FROM pg_locks WHERE pid=$1", backend["pid"]
            )
            > 0
        )
        allow_cancel.set()
        with pytest.raises(asyncio.CancelledError, match="first directory signal") as caught:
            await asyncio.wait_for(task, seconds)
        assert caught.value is first_signal["error"]
        assert time.monotonic() - began <= seconds + 0.1
        await released(directory_database, backend["pid"])
    finally:
        allow_cancel.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if backend:
            await directory_database.execute("SELECT pg_cancel_backend($1)", backend["pid"])
            async with asyncio.timeout(2):
                for _ in range(200):
                    if not await directory_database.fetchval(
                        "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE pid=$1)", backend["pid"]
                    ):
                        break
                    await asyncio.sleep(0.01)
                else:
                    pytest.fail("test-owned backend remained after exact cancellation")
