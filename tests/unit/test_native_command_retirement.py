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


def _claim_prompt_subprocess(result_path):
    """Run the real command and asyncio.run with only its external work faked."""
    import json
    import os
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
        outcome = None
        try:
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

    def start(mode="pipe", contents=b""):
        result = tmp_path / f"claim-{len(owned)}.json"
        child = SimpleNamespace(process=None, writer=None, reader=None, file=None, result=result)
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
                "from tests.unit.test_native_command_retirement import _claim_prompt_subprocess; "
                "import sys; _claim_prompt_subprocess(sys.argv[1])",
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
        pytest.fail("cancelled claim-release did not exit while stdin remained open")
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
