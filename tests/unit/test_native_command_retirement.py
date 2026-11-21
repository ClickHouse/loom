"""The actual native command cannot outlive its certified participant."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from server.cli import _ch, access
from server.core import retirement
from server.insights.app import provider_barrier as barrier
from server.insights.app import provider_command as command


@pytest.fixture
def native(monkeypatch):
    from server.core import background, ch, pg, pg_profile
    from server.core.model_work_gate import ModelWorkGate
    from server.settings import settings

    events = []
    gate = retirement.get_gate()
    client = SimpleNamespace(connected=False)
    monkeypatch.setattr(settings, "storage_authority", "clickhouse")

    async def authority(sql, params=None):
        assert sql == pg_profile._CH_RECORD
        return {"authority": "clickhouse", "deployment_id": str(UUID(int=1)), "epoch": 1}

    client.query_one = authority

    async def connect():
        client.connected = True

    async def disconnect():
        client.connected = False

    client.connect, client.disconnect = connect, disconnect
    monkeypatch.setattr(ch, "get_client", lambda: client)
    monkeypatch.setattr(command, "get_client", lambda: client)
    monkeypatch.setattr(pg, "close_pool", AsyncMock())

    async def verified(_):
        assert not gate.enrolled
        events.append("verified")

    async def enroll():
        assert events == ["verified"]
        gate.enrolled = True
        events.append("enrolled")

    async def acceptance(**_):
        assert gate.enrolled and gate.active > 0 and not gate.closed
        events.append("acceptance")

    async def certificate(proof, *, ch):
        assert ch is client
        assert gate.quiescent and client.connected
        assert events[-1] == "buffer"
        events.append("certificate")

    async def flush():
        events.append("buffer")
        return True

    monkeypatch.setattr(pg_profile, "check_configured", verified)
    monkeypatch.setattr(command, "register_process", enroll)
    monkeypatch.setattr(_ch, "check_acceptance", acceptance)
    model_gate = ModelWorkGate()
    monkeypatch.setattr(command, "get_model_work_gate", lambda: model_gate)
    monkeypatch.setattr(barrier, "get_model_work_gate", lambda: model_gate)
    monkeypatch.setattr(command, "load_active_configuration", AsyncMock())
    monkeypatch.setattr(command, "_adopt_overrides", AsyncMock(return_value=True))
    monkeypatch.setattr(background, "_registered_tasks", set())
    monkeypatch.setattr(command, "drain_task_sets", background.drain_task_sets)
    monkeypatch.setattr(barrier, "drain_task_sets", background.drain_task_sets)
    monkeypatch.setattr(command, "flush_registered_buffers", flush)
    monkeypatch.setattr(barrier, "flush_registered_buffers", flush)
    monkeypatch.setattr(barrier, "_server_side_settled", AsyncMock(return_value=True))
    monkeypatch.setattr(barrier, "running_queries", AsyncMock(return_value=0))
    monkeypatch.setattr(barrier, "_OWN", barrier._Own())
    monkeypatch.setattr(
        barrier, "_HEARTBEAT", SimpleNamespace(signal_stop=lambda: None, running=False)
    )
    monkeypatch.setattr(barrier, "record_retirement", certificate)
    return gate, events


def test_actual_claim_release_keeps_lookup_and_release_in_one_lifetime(native, monkeypatch):
    import asyncio

    from server.core.background import create_deferred_task
    from server.identity.app import claim_release
    from server.identity.store import namespaces

    gate, events = native

    async def lookup(*_, **__):
        assert gate.active > 0 and not gate.closed
        events.append("lookup")
        return {"owner_email": "owner@example.test"}

    async def release(**_):
        assert gate.active > 0 and gate.enrolled and not gate.closed
        assert "certificate" not in events
        events.append("release")

        async def descendant():
            assert gate.enrolled and gate.closed
            assert "certificate" not in events
            events.append("descendant")

        async def write():
            await asyncio.sleep(0)
            assert gate.enrolled and gate.closed
            assert "certificate" not in events
            events.append("deferred")
            create_deferred_task(descendant())

        create_deferred_task(write())
        return SimpleNamespace(grants_revoked=2)

    monkeypatch.setattr(namespaces, "fetch_row", lookup)
    monkeypatch.setattr(claim_release, "release_claim", release)
    assert access._cmd_claim_release(SimpleNamespace(org="o", namespace="n", yes=True)) == 0
    assert events.count("enrolled") == events.count("certificate") == 1
    assert events.index("lookup") < events.index("release") < events.index("certificate")
    assert events.index("release") < events.index("deferred") < events.index("descendant")
    assert events.index("descendant") < events.index("buffer") < events.index("certificate")
    assert gate.quiescent


@pytest.mark.parametrize("platform_admin", [False, True])
def test_actual_mint_readiness_keyring_signing_and_audit_precede_retirement(
    native, monkeypatch, platform_admin
):
    import argparse

    from server.auth import jwt, keyring, machines
    from server.observability import audit

    gate, events = native

    def observed(event):
        assert gate.active > 0 and gate.enrolled and not gate.closed
        assert "certificate" not in events
        events.append(event)

    async def ready(_):
        observed("readiness")
        return True

    async def refresh(**_):
        observed("keyring")
        return True

    def mint(**_):
        observed("mint")
        return "test-token"

    async def record(*_, **__):
        observed("audit")
        return "audit-id"

    monkeypatch.setattr(machines, "platform_admin_tokens_ready", ready)
    monkeypatch.setattr(keyring, "refresh", refresh)
    monkeypatch.setattr(jwt, "mint_token", mint)
    monkeypatch.setattr(audit, "emit", record)
    parser = argparse.ArgumentParser()
    access._add_mint_token_parser(parser.add_subparsers(dest="cmd"))
    argv = ["mint-token", "--subject", "sa:test", "--org", "o", "--ttl-days", "1"]
    argv += ["--platform-admin"] if platform_admin else ["--namespaces", "n"]
    assert access._cmd_mint_token(parser.parse_args(argv)) == 0
    assert events.count("enrolled") == events.count("certificate") == 1
    assert events.index("keyring") < events.index("mint") < events.index("certificate")
    if platform_admin:
        assert events.index("readiness") < events.index("keyring")
        assert events.index("mint") < events.index("audit") < events.index("certificate")
    assert gate.quiescent


@pytest.mark.parametrize("failure", ["unavailable", "wrong-pair"])
def test_actual_mint_never_signs_without_verified_enrollment(native, monkeypatch, failure):
    import argparse
    from unittest.mock import Mock

    from server.auth import jwt
    from server.core import pg_profile
    from server.core.pg import AuthorityMismatch

    gate, events = native

    async def refuse(_):
        if failure == "wrong-pair":
            raise AuthorityMismatch("different paired stores")
        raise ConnectionError("unavailable")

    minted = Mock(side_effect=AssertionError("unenrolled mint"))
    monkeypatch.setattr(pg_profile, "check_configured", refuse)
    monkeypatch.setattr(jwt, "mint_token", minted)
    parser = argparse.ArgumentParser()
    access._add_mint_token_parser(parser.add_subparsers(dest="cmd"))
    args = parser.parse_args(
        ["mint-token", "--subject", "sa:test", "--org", "o", "--namespaces", "n"]
    )
    if failure == "wrong-pair":
        with pytest.raises(AuthorityMismatch):
            access._cmd_mint_token(args)
    else:
        assert access._cmd_mint_token(args) == 1
    minted.assert_not_called()
    assert not gate.enrolled and not gate.closed and events == []


def _claim_async_state(gate, events, stdin_fd=None):
    """Fixed metadata only: never coroutine arguments, locals, task names or exception text."""
    import asyncio
    import os
    import signal
    from itertools import islice
    from pathlib import Path

    handler = signal.getsignal(signal.SIGINT)
    state = {
        "sigint_blocked": signal.SIGINT in signal.pthread_sigmask(signal.SIG_BLOCK, []),
        "sigint_pending": signal.SIGINT in signal.sigpending(),
        "stdin_blocking": os.get_blocking(stdin_fd) if stdin_fd is not None else None,
        "sigint_handler": (
            "default"
            if handler is signal.default_int_handler
            else "ignored"
            if handler == signal.SIG_IGN
            else "custom"
        ),
        "gate": {
            key: getattr(gate, key) for key in ("active", "closed", "enrolled", "producers_stopped")
        },
        "events": {
            key: events.count(key)
            for key in (
                "verified",
                "enrolled",
                "acceptance",
                "lookup",
                "release",
                "buffer",
                "certificate",
            )
        },
        "tasks": [],
    }
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        state["loop_running"] = False
        return state
    state["loop_running"] = True
    tasks = asyncio.all_tasks(loop)
    state["tasks_truncated"] = len(tasks) > 8
    for task in islice(tasks, 8):
        frames = []
        coroutine = task.get_coro()
        for _ in range(8):
            frame = getattr(coroutine, "cr_frame", None) or getattr(coroutine, "gi_frame", None)
            if frame is not None:
                frames.append(
                    [
                        Path(frame.f_code.co_filename).name[:64],
                        frame.f_code.co_name[:64],
                        frame.f_lineno,
                    ]
                )
            coroutine = getattr(coroutine, "cr_await", None) or getattr(
                coroutine, "gi_yieldfrom", None
            )
            if coroutine is None:
                break
        waiter = getattr(task, "_fut_waiter", None)
        row = {
            "cancelling": task.cancelling(),
            "done": task.done(),
            "cancelled": task.cancelled(),
            "waiter_present": waiter is not None,
            "waiter_done": waiter.done() if waiter is not None else False,
            "waiter_cancelled": waiter.cancelled() if waiter is not None else False,
            "frames": frames,
        }
        pending = getattr(task, "_must_cancel", None)
        if isinstance(pending, bool):
            row["pending_cancellation"] = pending
        state["tasks"].append(row)
    return state


def _write_claim_async_state(trace_fd, gate, events, stdin_fd=None):
    import contextlib
    import json
    import os

    try:
        packet = json.dumps(
            _claim_async_state(gate, events, stdin_fd), separators=(",", ":")
        ).encode()
        if len(packet) > 32700:
            packet = b'{"observer_truncated":true}'
        os.write(trace_fd, b"claim_async_state=" + packet + b"\n")
    except Exception:  # noqa: BLE001 — diagnostics must preserve the failed exit check.
        with contextlib.suppress(OSError):
            os.write(trace_fd, b'claim_async_state={"observer_failed":true}\n')


def _claim_prompt_subprocess(result_path):
    """Run the real command and asyncio.run with only its external work faked."""
    import faulthandler
    import json
    import os
    import signal
    import sys
    from pathlib import Path

    from server.identity.app import claim_release
    from server.identity.store import namespaces

    with pytest.MonkeyPatch.context() as monkeypatch:
        gate, events = native.__wrapped__(monkeypatch)

        async def lookup(*_, **__):
            assert gate.enrolled and gate.active > 0 and not gate.closed
            events.append("lookup")
            return {"owner_email": "owner@example.test"}

        async def release(**_):
            assert gate.enrolled and gate.active > 0 and not gate.closed
            assert events[-1] == "acceptance"
            events.append("release")
            return SimpleNamespace(grants_revoked=2)

        monkeypatch.setattr(namespaces, "fetch_row", lookup)
        monkeypatch.setattr(claim_release, "release_claim", release)
        blocking = os.get_blocking(sys.stdin.fileno())
        trace_path = Path(result_path).with_suffix(".trace")
        # The owned child keeps this descriptor open through interpreter shutdown.
        trace_fd = os.open(trace_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(
            trace_fd,
            f"sigint_ignored={signal.getsignal(signal.SIGINT) == signal.SIG_IGN}\n".encode(),
        )
        outcome = None
        try:

            def diagnostic(_signum, _frame):
                _write_claim_async_state(trace_fd, gate, events, sys.stdin.fileno())

            signal.signal(signal.SIGUSR1, diagnostic)
            faulthandler.register(signal.SIGUSR1, file=trace_fd, all_threads=True, chain=True)
            outcome = access._cmd_claim_release(SimpleNamespace(org="o", namespace="n", yes=False))
        except KeyboardInterrupt:
            outcome = "interrupted"
        finally:
            Path(result_path).write_text(
                json.dumps(
                    {
                        "outcome": outcome,
                        "events": events,
                        "quiescent": gate.quiescent,
                        "blocking_restored": os.get_blocking(sys.stdin.fileno()) == blocking,
                    }
                )
            )


@pytest.fixture
def claim_prompt_child(tmp_path):
    import contextlib
    import os
    import pty
    import subprocess
    import sys
    import tempfile
    from pathlib import Path

    owned = []

    def start(mode="pipe", contents=b"", *, stalled=False):
        result = tmp_path / f"claim-{len(owned)}.json"
        child = SimpleNamespace(
            process=None,
            writer=None,
            reader=None,
            file=None,
            result=result,
            trace=result.with_suffix(".trace"),
        )
        owned.append(child)
        if mode == "regular":
            child.file = tempfile.TemporaryFile()
            child.file.write(contents)
            child.file.seek(0)
            child.reader = child.file.fileno()
        else:
            child.reader, write_fd = pty.openpty() if mode == "terminal" else os.pipe()
            # openpty returns master, slave: input is the slave and the writer is the master.
            if mode == "terminal":
                write_fd, child.reader = child.reader, write_fd
            child.writer = os.fdopen(write_fd, "wb", buffering=0)
        child.process = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-c",
                "from tests.unit.test_native_command_retirement import "
                + ("_claim_prompt_stalled_subprocess" if stalled else "_claim_prompt_subprocess")
                + " as run; import sys; run(sys.argv[1])",
                str(result),
            ],
            cwd=Path(__file__).resolve().parents[2],
            stdin=child.reader,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        return child

    yield start
    for child in owned:
        # Close only our input first: this also releases the baseline's blocked executor read.
        if child.writer is not None:
            with contextlib.suppress(BrokenPipeError):
                child.writer.close()
        if child.process is not None:
            try:
                child.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.process.terminate()
                try:
                    child.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    child.process.kill()
                    child.process.wait(timeout=2)
            child.process.stdout.close()
        if child.file is not None:
            child.file.close()
        elif child.reader is not None:
            os.close(child.reader)


def _wait_for_claim_prompt(child):
    import os
    import select
    import time

    output = bytearray()
    end = time.monotonic() + 10
    while b"Continue? [y/N] " not in output:
        remaining = end - time.monotonic()
        assert remaining > 0 and select.select([child.process.stdout], [], [], remaining)[0], (
            "the actual claim-release command did not reach its prompt"
        )
        chunk = os.read(child.process.stdout.fileno(), 4096)
        assert chunk and len(output) + len(chunk) <= 16384, bytes(output)
        output.extend(chunk)
    return bytes(output)


@pytest.mark.parametrize("mode,partial", [("pipe", b""), ("pipe", b"y"), ("terminal", b"")])
def test_actual_claim_prompt_cancellation_exits_with_stdin_still_open(
    claim_prompt_child, mode, partial
):
    import json
    import os
    import select
    import signal
    import subprocess
    import time

    child = claim_prompt_child(mode)
    output = _wait_for_claim_prompt(child)
    assert b"currently owned by 'owner@example.test'" in output
    if partial:
        child.writer.write(partial)
        end = time.monotonic() + 5
        # No newline or EOF: verify the child consumed the partial pipe input before cancellation.
        while select.select([child.reader], [], [], 0)[0]:
            assert time.monotonic() < end, "the actual prompt did not consume partial stdin"
            time.sleep(0.01)
    child.process.send_signal(signal.SIGINT)
    try:
        child.process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        # The exit deadline has already failed; collect only our child's bounded evidence.
        diagnostic = b"child diagnostic unavailable"
        try:
            child.process.send_signal(signal.SIGUSR1)
            time.sleep(0.5)
            with child.trace.open("rb") as trace:
                diagnostic = trace.read(65536)
        except OSError as error:
            diagnostic = type(error).__name__.encode()
        pytest.fail(
            "cancelled claim-release did not exit while stdin remained open\n"
            + diagnostic.decode(errors="replace")
        )
    assert not child.writer.closed  # communicate() or EOF must not make the assertion pass.
    assert child.process.returncode == 0
    result = json.loads(child.result.read_text())
    assert result["outcome"] == "interrupted"
    assert result["quiescent"] and result["blocking_restored"]
    assert os.get_blocking(child.reader)
    assert result["events"].count("enrolled") == result["events"].count("certificate") == 1
    assert result["events"].index("lookup") < result["events"].index("certificate")
    assert result["events"].count("acceptance") == 1 and "release" not in result["events"]


@pytest.mark.parametrize(
    "mode,answer,eof,expected",
    [
        ("pipe", b"yes\n", False, 0),
        ("pipe", b"N\n", False, 1),
        ("pipe", b"", True, 1),
        ("pipe", b"Y", True, 0),
        ("regular", b"yes\n", True, 0),
        ("terminal", b"yes\n", False, 0),
    ],
)
def test_actual_claim_prompt_answer_and_eof_controls(
    claim_prompt_child, mode, answer, eof, expected
):
    import json
    import os

    child = claim_prompt_child(mode, answer if mode == "regular" else b"")
    _wait_for_claim_prompt(child)
    if child.writer is not None:
        child.writer.write(answer)
        if eof:
            child.writer.close()
    child.process.wait(timeout=5)
    assert child.process.returncode == 0
    result = json.loads(child.result.read_text())
    assert result["outcome"] == expected
    assert result["quiescent"] and result["blocking_restored"]
    assert os.get_blocking(child.reader)
    assert result["events"].count("enrolled") == result["events"].count("certificate") == 1
    assert result["events"].index("lookup") < result["events"].index("certificate")
    assert result["events"].count("acceptance") == (2 if expected == 0 else 1)
    assert result["events"].count("release") == (1 if expected == 0 else 0)


def _claim_prompt_stalled_subprocess(result_path):
    """Signal the actual command before publishing the observer's ready marker."""
    import asyncio
    import os
    import signal
    from pathlib import Path

    async def stalled_answer():
        os.kill(os.getpid(), signal.SIGINT)
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            Path(result_path).with_suffix(".stalled").touch()
            print("Continue? [y/N] ", end="", flush=True)
            await asyncio.Future()

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(access, "_claim_release_answer", stalled_answer)
        _claim_prompt_subprocess(result_path)


def test_claim_cancel_diagnostic_exposes_suspended_native_coroutine(claim_prompt_child):
    import json
    import signal
    import time

    child = claim_prompt_child(stalled=True)
    _wait_for_claim_prompt(child)
    assert child.result.with_suffix(".stalled").exists()
    child.process.send_signal(signal.SIGUSR1)
    end = time.monotonic() + 2
    trace = ""
    while "claim_async_state=" not in trace:
        assert time.monotonic() < end, "the trace omitted the suspended native command"
        trace = child.trace.read_bytes()[:65536].decode(errors="replace")
        time.sleep(0.01)
    packet = json.loads(trace.split("claim_async_state=", 1)[1].splitlines()[0])
    frames = [frame for task in packet["tasks"] for frame in task["frames"]]
    assert any(frame[1] == "stalled_answer" for frame in frames)
    assert any(task["cancelling"] == 1 for task in packet["tasks"])
    assert packet["gate"] == {
        "active": 1,
        "closed": False,
        "enrolled": True,
        "producers_stopped": False,
    }
    assert not packet["sigint_blocked"] and not packet["sigint_pending"]
    assert "Current thread" in trace
    child.process.send_signal(signal.SIGINT)
    child.process.wait(timeout=5)
    assert child.process.returncode == 0
    result = json.loads(child.result.read_text())
    assert result["outcome"] == "interrupted" and result["quiescent"]


def test_claim_cancel_diagnostic_limits_tasks_frames_and_metadata():
    import asyncio
    import json

    async def run():
        async def nested(depth):
            if depth:
                await nested(depth - 1)
            else:
                await asyncio.Future()

        tasks = [asyncio.create_task(nested(20), name="private-task-name") for _ in range(20)]
        try:
            await asyncio.sleep(0)
            state = _claim_async_state(retirement.RetirementGate(), ["private-event"])
            assert state["loop_running"] and state["tasks_truncated"]
            assert len(state["tasks"]) == 8
            assert all(len(task["frames"]) <= 8 for task in state["tasks"])
            packet = json.dumps(state)
            assert len(packet.encode()) < 32768
            assert "private-task-name" not in packet and "private-event" not in packet
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(run())


def test_claim_cancel_diagnostic_without_running_loop():
    state = _claim_async_state(retirement.RetirementGate(), [])
    assert not state["loop_running"] and state["tasks"] == []


def test_claim_cancel_diagnostic_failure_has_no_exception_text(monkeypatch):
    import os
    import sys

    def failed(*_):
        raise ValueError("private exception detail")

    packets = []
    monkeypatch.setattr(sys.modules[__name__], "_claim_async_state", failed)
    monkeypatch.setattr(os, "write", lambda fd, packet: packets.append((fd, packet)))
    _write_claim_async_state(42, retirement.RetirementGate(), [])
    assert packets == [(42, b'claim_async_state={"observer_failed":true}\n')]

    monkeypatch.setattr(
        sys.modules[__name__], "_claim_async_state", lambda *_: {"large": "x" * 40000}
    )
    _write_claim_async_state(42, retirement.RetirementGate(), [])
    assert packets[-1] == (42, b'claim_async_state={"observer_truncated":true}\n')

    def closed(*_):
        raise OSError("private descriptor detail")

    monkeypatch.setattr(os, "write", closed)
    _write_claim_async_state(42, retirement.RetirementGate(), [])


@pytest.mark.parametrize("consumed", [False, True])
def test_claim_cancel_diagnostic_distinguishes_waiter_states(consumed):
    import asyncio

    async def run():
        loop = asyncio.get_running_loop()
        first = loop.create_future()
        following = loop.create_future()
        waiting = asyncio.Event()
        continued = asyncio.Event()

        async def wait_for_input():
            waiting.set()
            try:
                await first
            except asyncio.CancelledError:
                if not consumed:
                    raise
                continued.set()
                await following

        task = asyncio.create_task(wait_for_input())
        try:
            await waiting.wait()
            task.cancel()
            if consumed:
                await continued.wait()
            state = _claim_async_state(retirement.RetirementGate(), [])
            observed = next(
                row
                for row in state["tasks"]
                if any(frame[1] == "wait_for_input" for frame in row["frames"])
            )
            assert observed["cancelling"] == 1
            assert observed["done"] is False and observed["cancelled"] is False
            assert observed["waiter_present"] is True
            assert observed["waiter_done"] is (not consumed)
            assert observed["waiter_cancelled"] is (not consumed)
            assert observed["pending_cancellation"] is False
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
