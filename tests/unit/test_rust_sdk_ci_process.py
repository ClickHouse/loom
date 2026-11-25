"""The Rust integration runner must bound and reap the Cargo test tree."""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.integration import _rust_sdk_ci as runner


@pytest.mark.parametrize("outcome", ["complete", "failure", "timeout", "cancel"])
@pytest.mark.asyncio
async def test_rust_runner_finishes_and_reaps_test_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    cargo = shutil.which("cargo")
    assert cargo is not None, "This runner test requires the Rust SDK's Cargo toolchain"
    (tmp_path / "src").mkdir()
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "runner-lifecycle"\nversion = "0.0.0"\nedition = "2021"\n'
    )
    pid_file = tmp_path / "test.pid"
    (tmp_path / "src/lib.rs").write_text(
        "#[test]\nfn test_process() {\n"
        '    std::fs::write(std::env::var("PID_FILE").unwrap(), '
        "std::process::id().to_string()).unwrap();\n"
        '    match std::env::var("OUTCOME").unwrap().as_str() {\n'
        '        "timeout" => loop { std::thread::park(); },\n'
        '        "failure" => panic!("reported test failure"),\n'
        "        _ => (),\n"
        "    }\n}\n"
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
            "OUTCOME": "timeout" if outcome == "cancel" else outcome,
        }
        process = await asyncio.create_subprocess_exec(
            cargo,
            "test",
            "--manifest-path",
            str(tmp_path / "Cargo.toml"),
            "--",
            "--nocapture",
            env=env,
            stdout=kwargs["stdout"],
            stderr=kwargs["stderr"],
            start_new_session=kwargs["start_new_session"],
        )
        processes.append(process)
        deadline = asyncio.get_running_loop().time() + 20
        while not pid_file.exists():
            if process.returncode is not None:
                output, _ = await process.communicate()
                pytest.fail(output.decode(errors="replace"))
            assert asyncio.get_running_loop().time() < deadline, "Rust fixture did not start"
            await asyncio.sleep(0.01)
        return process

    async def bounded_wait(awaitable, **options):
        assert options == {"timeout": 300}
        communicating.set()
        return await asyncio.wait_for(awaitable, timeout=0.1 if outcome == "timeout" else 5)

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
    cargo_returncode = None
    try:
        deadline = asyncio.get_running_loop().time() + 20
        while not pid_file.exists() and not task.done():
            assert asyncio.get_running_loop().time() < deadline, "Cargo did not start"
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
            cargo_returncode = processes[0].returncode
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
        # Reap only the process group this fixture created, including any Rust
        # executable that Cargo's parent-only kill left holding the output pipe.
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
        f"Rust test child survived: {child_alive}; Cargo returncode: {cargo_returncode}"
    )
    if outcome == "timeout":
        assert isinstance(result, TimeoutError)
    elif outcome == "cancel":
        assert isinstance(result, asyncio.CancelledError)
    elif outcome == "failure":
        assert isinstance(result, AssertionError)
        assert "reported test failure" in str(result)
    else:
        assert result is None
