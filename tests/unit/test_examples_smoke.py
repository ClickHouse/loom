"""Every script under examples/ runs end to end against a scripted Loom.

Each example is loaded by path and driven against an httpx MockTransport
that answers the ops it calls, so a renamed SDK method or a changed
signature fails here rather than in a reader's terminal. The scripted
server records every op so each test can check what the example did —
including that it wrote where it said it would.
"""

from __future__ import annotations

import functools
import importlib.util
import json
import sys
import types
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples"


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"example_{name}", EXAMPLES / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


Handler = Callable[[str, dict[str, Any]], dict[str, Any] | None]


def serve(monkeypatch: pytest.MonkeyPatch, handler: Handler) -> list[tuple[str, dict[str, Any]]]:
    """Route the SDK's HTTP client to `handler(op, body)`; a None answer is a
    generic `{}`. Returns the (op, body) log."""
    calls: list[tuple[str, dict[str, Any]]] = []

    def handle(req: httpx.Request) -> httpx.Response:
        op = req.url.path.removeprefix("/v1/")
        body = json.loads(req.content or b"{}")
        calls.append((op, body))
        return httpx.Response(200, json=handler(op, body) or {})

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        functools.partial(httpx.AsyncClient, transport=httpx.MockTransport(handle)),
    )
    for var in ("LOOM_ORG", "LOOM_NAMESPACE", "LOOM_AGENT", "LOOM_AUTO_RUN", "LOOM_MENTOR_BRIEF"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LOOM_API_KEY", "test-token")
    monkeypatch.setenv("LOOM_BASE_URL", "http://loom.test")
    return calls


def ops(calls: list[tuple[str, dict[str, Any]]]) -> list[str]:
    return [op for op, _ in calls]


# ---- log_conversation --------------------------------------------------------


async def test_log_conversation_appends_every_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = serve(monkeypatch, lambda op, body: {"ok": True})
    example = load("log_conversation")
    await example.main()
    appends = [b for op, b in calls if op == "conversation.append"]
    assert [(b["role"], b["content"]) for b in appends] == list(example.TURNS)
    assert len({b["thread_id"] for b in appends}) == 1
    assert ops(calls)[0] == "conversation.create"
    assert {(b["org"], b["namespace"]) for _, b in calls} == {("dev", "examples")}


# ---- ledger_run -------------------------------------------------------------


async def test_ledger_run_credits_the_fact_it_relied_on(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(op: str, body: dict[str, Any]) -> dict[str, Any] | None:
        if op == "run.begin":
            return {"run_id": "run-1", "status": "running"}
        if op == "memory.search":
            return {"results": []}
        if op == "memory.get":
            return {"found": True, "memory_key": body["memory_key"], "value": "x", "version": 1}
        if op == "run.complete":
            return {"run_id": body["run_id"], "status": body.get("status")}
        return {"memory_key": body.get("memory_key", "")}

    calls = serve(monkeypatch, handler)
    example = load("ledger_run")
    out = await example.run_task(org="dev", namespace="examples")
    assert out["run_id"] == "run-1"
    assert out["memories_used"] == [example.DEPLOY_KEY]
    complete = next(b for op, b in calls if op == "run.complete")
    assert complete["memories_used"] == [example.DEPLOY_KEY]
    assert "run.begin" in ops(calls) and ops(calls)[-1] == "run.complete"


# ---- onboarding_walkthrough -------------------------------------------------


async def test_onboarding_walkthrough_reaches_a_correct_answer(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    example = load("onboarding_walkthrough")
    from loom.reference_client import ARCHETYPES

    def handler(op: str, body: dict[str, Any]) -> dict[str, Any] | None:
        if op == "ops.capabilities":
            names = [*ARCHETYPES["memory_app"], "namespace.register"]
            return {"ops": [{"name": n} for n in names], "build_commit": "t"}
        if op == "namespace.register":
            return {"registered": True}
        if op == "memory.ask":
            return {
                "answer": "Tuesday and Thursday.",
                "abstain": False,
                "memory_keys": ["deploy_window"],
            }
        if op == "memory.outcome":
            return {"recorded": True}
        return {"memory_key": body.get("memory_key", "")}

    calls = serve(monkeypatch, handler)
    argv = ["--org", "dev", "--namespace", "examples", "--owner", "you@example.com"]
    assert await example.main(argv) == 0
    assert "correct" in capsys.readouterr().out
    assert ops(calls).count("memory.outcome") == 2


def test_onboarding_walkthrough_help() -> None:
    import asyncio

    example = load("onboarding_walkthrough")
    with pytest.raises(SystemExit) as ei:
        asyncio.run(example.main(["--help"]))
    assert ei.value.code == 0


# ---- claude_agent_loop ------------------------------------------------------


class _FakeMessages:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text="Noted.")])


def _fake_anthropic(monkeypatch: pytest.MonkeyPatch) -> _FakeMessages:
    messages = _FakeMessages()
    module = types.ModuleType("anthropic")
    module.AsyncAnthropic = lambda **_: types.SimpleNamespace(messages=messages)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", module)
    return messages


async def test_claude_agent_loop_recalls_reasons_and_writes_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages = _fake_anthropic(monkeypatch)

    def handler(op: str, body: dict[str, Any]) -> dict[str, Any] | None:
        if op == "memory.search":
            return {"results": [], "abstain": True}
        if op == "memory.set_from_messages":
            return {"written": [{"memory_key": "ops:deploy-days"}]}
        return None

    calls = serve(monkeypatch, handler)
    turns = iter(["we deploy Tue/Thu, never Friday"])

    def fake_input(_prompt: str = "") -> str:
        try:
            return next(turns)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr("builtins.input", fake_input)
    example = load("claude_agent_loop")
    await example.main()
    assert len(messages.requests) == 1
    assert messages.requests[0]["model"] == example.MODEL
    wrote = next(b for op, b in calls if op == "memory.set_from_messages")
    assert [m["role"] for m in wrote["messages"]] == ["user", "assistant"]
    assert "memory.search" in ops(calls)
    assert {b.get("namespace") for _, b in calls if "namespace" in b} == {"examples"}


def test_claude_agent_loop_names_its_missing_prerequisite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "anthropic", None)  # import raises ImportError
    with pytest.raises(SystemExit) as ei:
        load("claude_agent_loop")
    assert "pip install anthropic" in str(ei.value.code)
