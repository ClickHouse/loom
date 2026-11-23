"""Installed SDK CLI context behavior; hosted SDK environment only."""

import argparse
import asyncio
import contextvars
import threading
from concurrent.futures import Future

import httpx
import pytest
from loom import cli_context as context
from loom import source_cli
from loom.session_protection import AutomaticCaptureSuppressed, SessionProtection


def provision(tmp_path, monkeypatch):
    root = tmp_path / "application"
    root.mkdir(mode=0o700)
    path = root / "pointer.json"
    family = SessionProtection.create(base=root / "families")
    source_cli.save_session_pointer(path, family)
    raw = context.session_environment(str(path))[context.CONTEXT_ENV]
    monkeypatch.setenv(context.CONTEXT_ENV, raw)
    return family, path, raw


def http(monkeypatch, handler):
    original = httpx.AsyncClient
    clients = []

    def client(*args, **kwargs):
        result = original(*args, transport=httpx.MockTransport(handler), **kwargs)
        clients.append(result)
        return result

    monkeypatch.setattr(httpx, "AsyncClient", client)
    return clients


def test_read_observation_blocks_other_scope_write_but_allows_known_read_and_explicit(
    tmp_path, monkeypatch
):
    family, _, raw = provision(tmp_path, monkeypatch)
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200, json={"mirrored": True, "value": "private"})

    clients = http(monkeypatch, handler)

    async def scenario():
        assert (
            await context.request(
                raw,
                "POST",
                "https://one.invalid/v1/memory.get",
                "one",
                body={"namespace": "private"},
            )
        ).json()["value"] == "private"
        assert family.protected
        for route in ("memory.set", "not-in-generated-metadata", "knowledge.upload"):
            with pytest.raises(AutomaticCaptureSuppressed):
                await context.request(
                    raw,
                    "POST",
                    "https://other.invalid/v1/" + route,
                    "changed",
                    body={"namespace": "another"},
                )
        await context.request(raw, "POST", "https://one.invalid/v1/memory.get", "one", body={})
        await context.request(
            raw, "POST", "https://one.invalid/v1/memory.set", "one", body={}, explicit=True
        )

    asyncio.run(scenario())
    assert len(sent) == 3
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize("damage", ["missing-pointer", "lost-proof", "replacement", "malformed"])
def test_pinned_context_refuses_without_repair_or_http(tmp_path, monkeypatch, damage):
    family, path, raw = provision(tmp_path, monkeypatch)
    if damage == "missing-pointer":
        path.unlink()
    elif damage == "lost-proof":
        (family.base / family.id / "state").unlink()
    elif damage == "malformed":
        path.write_text("{}")
    else:
        path.unlink()
        other = SessionProtection.create(base=tmp_path / "other")
        source_cli.save_session_pointer(path, other)
    before = sorted(item.name for item in family.base.iterdir())

    def forbidden(request):
        raise AssertionError("Configured context must refuse before HTTP")

    clients = http(monkeypatch, forbidden)
    with pytest.raises(context.ContextUnavailable):
        asyncio.run(context.request(raw, "POST", "https://one.invalid/v1/memory.get", "one"))
    assert clients == []
    assert sorted(item.name for item in family.base.iterdir()) == before
    if damage == "missing-pointer":
        assert not path.exists()


def test_automatic_source_requires_matching_inherited_context(tmp_path, monkeypatch):
    _, path, raw = provision(tmp_path, monkeypatch)
    args = argparse.Namespace(
        request_file=None,
        token_file=None,
        session_file=str(path),
        intent="automatic",
        namespace="private",
        org="org",
    )
    command = source_cli._COMMANDS["list"]
    monkeypatch.delenv(context.CONTEXT_ENV)
    with pytest.raises(source_cli.InputError, match="require inherited"):
        source_cli._inputs(args, command)
    monkeypatch.setenv(context.CONTEXT_ENV, raw)
    monkeypatch.setenv("LOOM_API_KEY", "token")
    assert source_cli._inputs(args, command)[1] == {"org": "org", "namespace": "private"}
    args.session_file = str(path.with_name("other.json"))
    with pytest.raises(context.ContextUnavailable):
        source_cli._inputs(args, command)


@pytest.mark.parametrize("status", [429, 500, 503])
def test_error_response_observed_once_and_all_resources_closed(tmp_path, monkeypatch, status):
    family, _, raw = provision(tmp_path, monkeypatch)
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(status, json={"mirrored": True, "detail": {"message": "private"}})

    clients = http(monkeypatch, handler)
    response = asyncio.run(context.request(raw, "POST", "https://one.invalid/v1/memory.get", "one"))
    assert response.status_code == status
    assert "private" not in response.text
    assert len(sent) == 1 and family.protected
    assert all(client.is_closed for client in clients)


def test_cancelled_request_closes_transport(tmp_path, monkeypatch):
    _, _, raw = provision(tmp_path, monkeypatch)
    entered = asyncio.Event()
    released = asyncio.Event()

    async def handler(request):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            released.set()

    clients = http(monkeypatch, handler)

    async def scenario():
        task = asyncio.create_task(
            context.request(raw, "POST", "https://one.invalid/v1/memory.get", "one")
        )
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert released.is_set()

    asyncio.run(scenario())
    assert all(client.is_closed for client in clients)


def test_stream_keeps_observers_until_cancel_and_close(tmp_path, monkeypatch):
    family, _, raw = provision(tmp_path, monkeypatch)
    closed = []
    consumed = asyncio.Event()

    class Events(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"mirrored":true,"text":"private"}\n\n'
            await asyncio.Event().wait()

        async def aclose(self):
            closed.append(True)

    clients = http(
        monkeypatch,
        lambda request: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=Events()
        ),
    )

    async def consume():
        async with context.stream(
            raw, "https://one.invalid/v1/assistant/chat", "one", body={}, explicit=True
        ) as response:
            async for chunk in response.aiter_bytes():
                assert chunk and family.protected
                consumed.set()

    async def scenario():
        task = asyncio.create_task(consume())
        await consumed.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert closed and all(client.is_closed for client in clients)


def test_run_sync_inside_existing_loop_preserves_context_and_closes():
    value = contextvars.ContextVar("caller", default="missing")

    async def scenario():
        value.set("inherited")
        parent = asyncio.get_running_loop()

        async def child():
            assert asyncio.get_running_loop() is not parent
            assert value.get() == "inherited"
            return 7

        assert context.run_sync(child) == 7

    asyncio.run(scenario())


def test_run_sync_interrupt_cancels_and_joins_owned_loop(monkeypatch):
    entered = threading.Event()
    closed = threading.Event()

    class Interrupted(Future):
        def result(self, timeout=None):
            assert entered.wait(5)
            raise KeyboardInterrupt

    monkeypatch.setattr(context, "Future", Interrupted)

    async def work():
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            closed.set()

    async def scenario():
        with pytest.raises(KeyboardInterrupt):
            context.run_sync(work)
        assert closed.is_set()

    asyncio.run(scenario())


def test_explicit_sdk_intent_is_consumed_and_revoked_for_copied_callbacks(tmp_path, monkeypatch):
    family, _, _ = provision(tmp_path, monkeypatch)
    family.mark()
    args = argparse.Namespace(
        cmd="call", target="memory.set", method=None, body={}, request_intent="explicit"
    )

    async def write(args):
        return 7

    with context.command_intent(args):
        assert not hasattr(args, "request_intent")
        copied = contextvars.copy_context()
        assert asyncio.run(context.guarded_command(args, write)) == 7
    with pytest.raises(AutomaticCaptureSuppressed):
        copied.run(asyncio.run, context.guarded_command(args, write))


def test_sdk_cli_actual_dispatch_observes_and_guards_without_agent_facade_flags(
    tmp_path, monkeypatch, capsys
):
    from loom import cli

    family, _, _ = provision(tmp_path, monkeypatch)
    monkeypatch.setenv("LOOM_API_KEY", "test-token")
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"mirrored": True, "value": "private"})

    clients = http(monkeypatch, handler)
    common = ["--server", "https://one.invalid", "--org", "test-org", "--namespace", "private"]
    assert cli.main(["call", "memory.get", *common, "key=x"]) == 0
    assert family.protected and "private" in capsys.readouterr().out
    assert cli.main(["call", "memory.set", *common, "key=x", "value=automatic"]) == 2
    assert "protection refused" in capsys.readouterr().err
    assert len(seen) == 1
    assert (
        cli.main(
            ["--request-intent", "explicit", "call", "memory.set", *common, "key=x", "value=human"]
        )
        == 0
    )
    capsys.readouterr()
    assert cli.main(["call", "memory.get", *common, "key=x"]) == 0
    assert len(seen) == 3 and all(client.is_closed for client in clients)
