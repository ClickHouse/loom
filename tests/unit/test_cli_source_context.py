"""Pure common CLI context and one-dispatch intent contracts; no installed SDK needed."""

import asyncio
import contextvars
import urllib.request
from types import SimpleNamespace

import pytest

from server.cli import _client
from server.cli import _session_context as context


@pytest.fixture
def bound(monkeypatch):
    monkeypatch.setenv(context.CONTEXT_ENV, "trusted-capsule")


def test_unbound_helpers_do_not_load_sdk(monkeypatch):
    monkeypatch.delenv(context.CONTEXT_ENV, raising=False)

    def forbidden():
        raise AssertionError("Unbound CLI must not import SDK")

    monkeypatch.setattr(context, "_entry", forbidden)
    assert context.send(urllib.request.Request("https://loom.invalid/v1/memory.set"), 1) is None


def test_configured_context_has_actionable_missing_sdk_refusal(bound, monkeypatch):
    def missing(name):
        assert name == "loom.cli_context"
        raise ModuleNotFoundError("loom")

    monkeypatch.setattr(context.importlib, "import_module", missing)
    with pytest.raises(context.CLIContextError, match="matching-release clickhouse-loom"):
        _client.post_op("https://loom.invalid", "memory.set", {}, "token")


def test_context_cannot_change_or_disappear_during_dispatch(bound, monkeypatch):
    for replacement in (None, "other-capsule"):
        with context.invocation():
            if replacement is None:
                monkeypatch.delenv(context.CONTEXT_ENV)
            else:
                monkeypatch.setenv(context.CONTEXT_ENV, replacement)
            with pytest.raises(context.CLIContextError, match="changed during dispatch"):
                context.capsule()
        monkeypatch.setenv(context.CONTEXT_ENV, "trusted-capsule")


def test_explicit_intent_expires_in_copied_callback_context(bound):
    with context.invocation("explicit"):
        assert context._explicit()
        copied = contextvars.copy_context()
    assert not context._explicit()
    assert not copied.run(context._explicit)
    with context.invocation():
        assert not context._explicit()


def test_automatic_task_cannot_inherit_direct_user_intent(bound):
    async def scenario():
        with context.invocation("explicit"):
            assert context._explicit()

            async def callback():
                return context._explicit()

            assert not await asyncio.create_task(callback())
            assert context._explicit()
        assert not context._explicit()

    asyncio.run(scenario())


def test_raw_file_and_json_posts_reach_same_bridge(bound, monkeypatch):
    calls = []

    async def request(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(status_code=200, json=lambda: {"ok": True})

    def run_sync(factory):
        return asyncio.run(factory())

    entry = SimpleNamespace(request=request, run_sync=run_sync)
    monkeypatch.setattr(context, "_entry", lambda: entry)
    with context.invocation("explicit"):
        assert _client.post_op("https://loom.invalid", "memory.set", {"key": "x"}, "token") == (
            200,
            {"ok": True},
        )
        assert _client.post_bytes(
            "https://other.invalid/v1/knowledge.upload", b"file", "other"
        ) == (200, {"ok": True})
    assert calls[0][0] == ("trusted-capsule", "POST", "https://loom.invalid/v1/memory.set", "token")
    assert calls[0][1]["body"] == {"key": "x"}
    assert calls[0][1]["content"] is None
    assert calls[1][1]["content"] == b"file" and calls[1][1]["body"] is None
    assert all(kwargs["explicit"] for _, kwargs in calls)


def test_async_helper_keeps_ordinary_client_request(monkeypatch):
    monkeypatch.delenv(context.CONTEXT_ENV, raising=False)
    calls = []

    async def post(*args, **kwargs):
        calls.append((args, kwargs))
        return "response"

    async def scenario():
        assert (
            await context.post(
                SimpleNamespace(post=post), "/v1/memory.set", "token", body={"key": "x"}
            )
            == "response"
        )

    asyncio.run(scenario())
    assert calls == [(("/v1/memory.set",), {"json": {"key": "x"}})]


def test_root_dispatch_consumes_and_expires_explicit_intent(bound, monkeypatch):
    from server.cli import app

    copied = []

    def build(sub):
        sub.add_parser("test-request")

    def dispatch(args):
        assert not hasattr(args, "request_intent")
        assert context._explicit()
        copied.append(contextvars.copy_context())
        return 7

    monkeypatch.setattr(app, "_SUBCOMMAND_BUILDERS", [build])
    monkeypatch.setattr(app, "wire", lambda: None)
    monkeypatch.setattr(app, "_load_dotenv_like_the_container", lambda: None)
    monkeypatch.setattr(app, "_dispatch", dispatch)
    assert app.main(["--request-intent", "explicit", "test-request"]) == 7
    assert not context._explicit() and not copied[0].run(context._explicit)


@pytest.mark.parametrize(
    "prefix", [["--request-intent", "explicit"], ["--request-intent=explicit"]]
)
def test_root_sdk_delegation_keeps_one_invocation_intent(prefix, monkeypatch):
    from server.cli import app

    seen = []
    monkeypatch.setattr(app, "run_sdk_command", lambda raw: seen.append(raw) or 7)
    args = [*prefix, "call", "memory.set", "key=x"]
    assert app.main(args) == 7
    assert seen == [args]


def test_async_command_borrows_intent_only_for_its_dispatch(bound, monkeypatch):
    copied = []
    monkeypatch.setattr(
        context, "_entry", lambda: SimpleNamespace(run_sync=lambda factory: asyncio.run(factory()))
    )

    async def command():
        assert context._explicit()
        copied.append(contextvars.copy_context())
        return 7

    with context.invocation("explicit"):
        assert context.run_async(command) == 7
        assert context._explicit()
        assert not copied[0].run(context._explicit)
    assert not context._explicit()
