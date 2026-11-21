"""The threaded API fixture owns entry and exit until cleanup has finished."""

import ast
import asyncio
import contextlib
import sys
import threading
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "integration/test_cell_attach_two_replicas.py"


def _fixture(context):
    # Execute the actual helper alone: no server imports, services or subprocesses.
    tree = ast.parse(SOURCE.read_text())
    helper = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_api")

    def spawn(name, env, *, port):
        assert (name, env, port) == ("owned", {"LOOM_INSTANCE_LABEL": "owned"}, 1)
        return context

    namespace = {
        "asyncio": asyncio,
        "contextlib": contextlib,
        "sys": sys,
        "AsyncIterator": AsyncIterator,
        "loom_server": spawn,
    }
    exec(compile(ast.Module(body=[helper], type_ignores=[]), str(SOURCE), "exec"), namespace)
    return namespace["_api"]


@pytest.mark.parametrize("phase", ["entry", "exit", "body-error"])
def test_cancellation_waits_for_owned_context_before_fixture_unwinds(phase):
    async def scenario():
        observed = asyncio.Event()
        entry_finished = threading.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        unwound = []
        first = ValueError("body failed") if phase == "body-error" else None

        class Context:
            alive = False
            exits = 0

            def __enter__(self):
                self.alive = True
                try:
                    if phase == "entry":
                        loop.call_soon_threadsafe(observed.set)
                        assert release.wait(5), "test did not release entry"
                    return "http://owned"
                finally:
                    entry_finished.set()

            def __exit__(self, *_):
                self.exits += 1
                if phase != "entry":
                    loop.call_soon_threadsafe(observed.set)
                    assert release.wait(5), "test did not release exit"
                self.alive = False

        context = Context()
        api = _fixture(context)

        async def owner():
            try:
                async with api("owned", {}, 1) as base:
                    assert base == "http://owned"
                    if first is not None:
                        raise first
            finally:
                unwound.append(context.alive)

        task = asyncio.create_task(owner())
        try:
            await asyncio.wait_for(observed.wait(), timeout=2)
            for message in ("first cancel", "second cancel", "third cancel"):
                task.cancel(message)
                await asyncio.sleep(0)
                assert not task.done(), "owner unwound while its context was still live"
                assert unwound == [] and context.alive
            release.set()
            with pytest.raises(
                ValueError if first is not None else asyncio.CancelledError
            ) as caught:
                await asyncio.wait_for(task, timeout=2)
            if first is not None:
                assert caught.value is first
            else:
                assert caught.value.args == ("first cancel",)
            assert context.exits == 1 and not context.alive
            assert unwound == [False]
        finally:
            release.set()
            with contextlib.suppress(BaseException):
                await task
            assert await asyncio.to_thread(entry_finished.wait, 2)

    asyncio.run(scenario())


@pytest.mark.parametrize("startup_fails", [False, True])
def test_entry_outcome_selects_exactly_one_exit_or_its_existing_failure_cleanup(startup_fails):
    first = ValueError("startup failed after its own cleanup")
    exits = []

    class Context:
        def __enter__(self):
            if startup_fails:
                raise first
            return "http://owned"

        def __exit__(self, *_):
            exits.append(True)

    async def scenario():
        async with _fixture(Context())("owned", {}, 1) as base:
            assert base == "http://owned"

    if startup_fails:
        with pytest.raises(ValueError) as caught:
            asyncio.run(scenario())
        assert caught.value is first
        assert exits == []
    else:
        asyncio.run(scenario())
        assert exits == [True]
