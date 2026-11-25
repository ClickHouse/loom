"""The Rust integration runner must bound and reap the Cargo test tree."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.integration import _rust_sdk_ci as runner


@pytest.mark.parametrize("outcome", ["complete", "failure", "timeout", "cancel", "exited-parent"])
@pytest.mark.asyncio
async def test_rust_runner_finishes_and_reaps_test_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    monkeypatch.setenv("PATH", "")
    pid_file = tmp_path / "test.pid"
    child_script = tmp_path / "child.py"
    child_script.write_text(
        "import os, pathlib, signal, sys\n"
        'pathlib.Path(os.environ["PID_FILE"]).write_text(str(os.getpid()))\n'
        'if os.environ["OUTCOME"] in ("timeout", "cancel", "exited-parent"):\n'
        "    while True: signal.pause()\n"
        'if os.environ["OUTCOME"] == "failure":\n'
        '    print("reported test failure", file=sys.stderr, flush=True)\n'
        "    sys.exit(1)\n"
    )
    parent_script = tmp_path / "parent.py"
    parent_script.write_text(
        "import os, subprocess, sys\n"
        "child = subprocess.Popen([sys.executable, sys.argv[1]])\n"
        'if os.environ["OUTCOME"] == "exited-parent":\n'
        "    sys.exit(0)\n"
        "sys.exit(child.wait())\n"
    )
    # Only authentication and process launch are replaced. The runner's actual
    # communicate/timeout/kill/wait control flow remains under test.
    http_client = AsyncMock()
    http_client.get.return_value.status_code = 401
    context = AsyncMock()
    context.__aenter__.return_value = http_client
    monkeypatch.setattr(runner.httpx, "AsyncClient", lambda **_: context)
    monkeypatch.setattr(runner, "unique_org", lambda _: "runner-unit")
    processes: list[asyncio.subprocess.Process] = []
    communicating = asyncio.Event()

    async def spawn(*command, **kwargs):
        assert command[:2] == ("cargo", "test")
        assert kwargs["start_new_session"] is True
        env = {
            **kwargs["env"],
            "PID_FILE": str(pid_file),
            "OUTCOME": outcome,
        }
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(parent_script),
            str(child_script),
            env=env,
            stdout=kwargs["stdout"],
            stderr=kwargs["stderr"],
            start_new_session=kwargs["start_new_session"],
        )
        processes.append(process)
        deadline = asyncio.get_running_loop().time() + 20
        while not pid_file.exists():
            if process.returncode is not None and outcome != "exited-parent":
                output, _ = await process.communicate()
                pytest.fail(output.decode(errors="replace"))
            assert asyncio.get_running_loop().time() < deadline, "Process fixture did not start"
            await asyncio.sleep(0.01)
        if outcome == "exited-parent":
            while process.returncode is None:
                assert asyncio.get_running_loop().time() < deadline, "Parent did not exit"
                await asyncio.sleep(0.01)
        return process

    async def bounded_wait(awaitable, **options):
        assert options == {"timeout": 300}
        communicating.set()
        timeout = 0.1 if outcome in {"timeout", "exited-parent"} else 5
        return await asyncio.wait_for(awaitable, timeout=timeout)

    # Patch the runner's module reference rather than global asyncio functions.
    monkeypatch.setattr(
        runner,
        "asyncio",
        SimpleNamespace(
            create_subprocess_exec=spawn,
            wait_for=bounded_wait,
            subprocess=asyncio.subprocess,
        ),
    )
    task = asyncio.create_task(
        runner.test_rust_sdk_live_contracts("http://unused", lambda **_: "unit-token")
    )
    finished = False
    child_alive = False
    parent_returncode = None
    try:
        deadline = asyncio.get_running_loop().time() + 20
        while not pid_file.exists() and not task.done():
            assert asyncio.get_running_loop().time() < deadline, "Parent did not start"
            await asyncio.sleep(0.01)
        if outcome == "cancel":
            # The spawn fixture waits for readiness before returning the handle;
            # cancel the runner only after it owns the process in its try block.
            await asyncio.wait_for(communicating.wait(), 5)
            task.cancel()
        exit_deadline = asyncio.get_running_loop().time() + 1
        done, _ = await asyncio.wait({task}, timeout=1)
        finished = task in done
        if processes:
            parent_returncode = processes[0].returncode
        if pid_file.exists():
            child_pid = int(pid_file.read_text())
            while True:
                try:
                    os.kill(child_pid, 0)
                    child_alive = True
                except ProcessLookupError:
                    child_alive = False
                    break
                if asyncio.get_running_loop().time() >= exit_deadline:
                    break
                await asyncio.sleep(0.01)
    finally:
        # Reap only this fixture's group if the runner leaves descendants alive.
        for process in processes:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        result = (await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5))[0]
        for process in processes:
            await asyncio.wait_for(process.wait(), 5)
    assert finished and not child_alive, (
        f"runner finished within its timeout: {finished}; "
        f"Test child survived: {child_alive}; parent returncode: {parent_returncode}"
    )
    if outcome in {"timeout", "exited-parent"}:
        assert isinstance(result, TimeoutError)
        if outcome == "exited-parent":
            assert parent_returncode == 0
    elif outcome == "cancel":
        assert isinstance(result, asyncio.CancelledError)
    elif outcome == "failure":
        assert isinstance(result, AssertionError)
        assert "reported test failure" in str(result)
    else:
        assert result is None
