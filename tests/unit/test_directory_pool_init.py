"""Failed fixed-codec initialization never admits its unborrowed raw connection."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from server.router import directory


@pytest.mark.parametrize(
    "error", [ValueError("codec failure"), asyncio.CancelledError("codec cancelled")]
)
@pytest.mark.parametrize("position", [0, 1])
async def test_every_failed_initialization_quarantines_its_exact_raw_session(error, position):
    conn = Mock(set_type_codec=AsyncMock(side_effect=[None] * position + [error]))
    with pytest.raises(type(error)) as caught:
        await directory._init(conn)
    assert caught.value is error
    conn.terminate.assert_called_once_with()
    assert conn.set_type_codec.await_count == position + 1


async def test_fixed_builtin_codecs_do_not_execute_sql():
    import json
    from types import MethodType, SimpleNamespace

    import asyncpg

    conn = SimpleNamespace(
        _check_open=Mock(),
        _protocol=Mock(),
        _drop_local_statement_cache=Mock(),
        _execute=AsyncMock(side_effect=AssertionError("codec initialization executed SQL")),
        terminate=Mock(),
    )
    conn._introspect_type = MethodType(asyncpg.Connection._introspect_type, conn)
    conn.set_type_codec = MethodType(asyncpg.Connection.set_type_codec, conn)
    await directory._init(conn)
    conn._execute.assert_not_awaited()
    conn.terminate.assert_not_called()
    calls = conn._protocol.get_settings.return_value.add_python_codec.call_args_list
    assert len(calls) == 2
    assert [call.args[1] for call in calls] == ["json", "jsonb"]
    assert all(call.args[2] == "pg_catalog" for call in calls)
    assert all(call.args[5:7] == (json.dumps, json.loads) for call in calls)


@pytest.mark.parametrize("kind", ["ordinary", "cancel", "exit", "keyboard"])
@pytest.mark.parametrize("secondary", ["ordinary", "cancel", "exit", "keyboard"])
async def test_secondary_quarantine_failure_keeps_first_and_reports_unknown(kind, secondary):
    def error(which):
        return {
            "ordinary": ValueError("original codec failure"),
            "cancel": asyncio.CancelledError("codec cancelled"),
            "exit": SystemExit(17),
            "keyboard": KeyboardInterrupt("codec interrupted"),
        }[which]

    original = error(kind)
    conn = Mock(
        set_type_codec=AsyncMock(side_effect=original), terminate=Mock(side_effect=error(secondary))
    )
    if isinstance(original, (SystemExit, KeyboardInterrupt)):
        with pytest.raises(directory._PoolInitFailure) as caught:
            await directory._init(conn)
        assert caught.value.error is original
    else:
        with pytest.raises(type(original)) as caught:
            await directory._init(conn)
        assert caught.value is original
    assert original.__notes__ == ["directory initialization session closure is unknown"]
    conn.terminate.assert_called_once_with()
    conn.set_type_codec.assert_awaited_once()


async def test_diagnostic_failure_cannot_replace_original_native_failure():
    class BrokenNote(SystemExit):
        def add_note(self, note):
            raise KeyboardInterrupt("secondary diagnostic failure")

    original = BrokenNote(17)
    conn = Mock(
        set_type_codec=AsyncMock(side_effect=original), terminate=Mock(side_effect=RuntimeError)
    )
    with pytest.raises(directory._PoolInitFailure) as caught:
        await directory._init(conn)
    assert caught.value.error is original


async def test_pool_has_no_autonomous_opening_and_keeps_existing_limits(monkeypatch):
    import asyncpg

    opened = object()
    create = AsyncMock(return_value=opened)
    monkeypatch.setattr(directory, "_pool", None)
    monkeypatch.setattr(directory, "_require_dsn", lambda: "private-test-dsn")
    monkeypatch.setattr(asyncpg, "create_pool", create)
    assert await directory.pool() is opened
    assert await directory.pool() is opened
    create.assert_awaited_once_with(
        "private-test-dsn",
        min_size=0,
        max_size=10,
        init=directory._init,
        server_settings={
            **directory._SESSION,
            "statement_timeout": "10000",
            "idle_in_transaction_session_timeout": "20000",
        },
        command_timeout=11,
        timeout=10,
    )


@pytest.mark.parametrize("boundary", ["creation", "acquisition", "lookup"])
@pytest.mark.parametrize("kind", ["exit", "keyboard"])
async def test_every_pool_boundary_unwraps_identical_native_after_driver_cleanup(
    monkeypatch, boundary, kind
):
    from contextlib import asynccontextmanager

    import asyncpg

    original = SystemExit(17) if kind == "exit" else KeyboardInterrupt("codec interrupted")
    events = []
    carrier = directory._PoolInitFailure(original)
    monkeypatch.setattr(directory, "_pool", None)
    monkeypatch.setattr(directory, "_require_dsn", lambda: "private-test-dsn")

    async def create(*args, **kwargs):
        events.append("driver creation cleanup")
        raise carrier

    @asynccontextmanager
    async def acquire():
        try:
            raise carrier
            yield  # pragma: no cover
        finally:
            events.append("driver acquisition cleanup")

    if boundary == "creation":
        monkeypatch.setattr(asyncpg, "create_pool", create)
    else:
        monkeypatch.setattr(directory, "_pool", Mock(acquire=acquire))
    slots = directory._lookups._value
    with pytest.raises(type(original)) as caught:
        if boundary == "creation":
            await directory.pool()
        else:
            async with directory._connection(lookup=boundary == "lookup"):
                pytest.fail("failed initializer admitted a connection")
    assert caught.value is original
    assert events == [
        "driver creation cleanup" if boundary == "creation" else "driver acquisition cleanup"
    ]
    assert directory._lookups._value == slots
