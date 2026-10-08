"""An operation row says how much the agent had to read
(`agent_events.response_tokens`), and an MCP tool call the
limiter refuses waits out its own Retry-After once before the client sees it."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import BaseModel

from server.observability.event_args import response_tokens_of
from server.observability.events import AgentEvent


class _Out(BaseModel):
    answer: str = ""
    results: list[dict[str, Any]] = []


def test_response_tokens_is_json_length_over_four() -> None:
    body = {"results": [{"memory_key": "k", "content_excerpt": "x" * 400}]}
    import json

    expect = len(json.dumps(body, separators=(",", ":"), ensure_ascii=False)) // 4
    assert response_tokens_of(body) == expect
    assert expect > 100


def test_response_tokens_dumps_a_model_and_is_zero_for_none() -> None:
    assert response_tokens_of(None) == 0
    out = _Out(answer="Blue.", results=[{"memory_key": "m1"}])
    assert response_tokens_of(out) == response_tokens_of(out.model_dump(mode="json"))
    assert response_tokens_of(out) > 0


def test_response_tokens_never_raises() -> None:
    class _Bad:
        def model_dump(self, **_: Any) -> Any:
            raise RuntimeError("no")

    assert response_tokens_of(_Bad()) == 0
    assert response_tokens_of({"ts": datetime.now(UTC)}) > 0  # default=str


def test_event_row_carries_response_tokens_default_zero() -> None:
    base = dict(
        ts=datetime.now(UTC),
        org="o",
        namespace="n",
        agent="a",
        user_id="-",
        session_id="-",
        operation="memory.search",
        request_id="r",
        latency_ms=1,
        success=True,
    )
    assert AgentEvent(**base).to_row()["response_tokens"] == 0
    assert AgentEvent(**base, response_tokens=512).to_row()["response_tokens"] == 512


class _FakeEmitter:
    def __init__(self) -> None:
        self.events: list[AgentEvent] = []

    def emit(self, event: AgentEvent) -> None:
        self.events.append(event)


@pytest.fixture
def emitted(monkeypatch: pytest.MonkeyPatch) -> _FakeEmitter:
    from server.observability import ledger

    fake = _FakeEmitter()
    monkeypatch.setattr(ledger, "get_emitter", lambda: fake)
    return fake


def _emit(success: bool, result: Any, error: BaseException | None = None) -> None:
    from server.observability.ledger import emit_agent_event

    emit_agent_event(
        op_name="memory.search",
        identity={"org": "o", "namespace": "n", "agent": "a", "user_id": "-", "session_id": "-"},
        request_id="r",
        latency_ms=3,
        success=success,
        error_code="" if success else "INTERNAL_ERROR",
        raw_args={"org": "o", "namespace": "n", "query": "q"},
        result=result,
        error=error,
    )


def test_emit_sets_response_tokens_from_the_result(emitted: _FakeEmitter) -> None:
    out = _Out(answer="Blue.", results=[{"memory_key": "m1", "content_excerpt": "y" * 200}])
    _emit(True, out)
    assert len(emitted.events) == 1
    assert emitted.events[0].response_tokens == response_tokens_of(out) > 0


def test_emit_zero_response_tokens_on_failure(emitted: _FakeEmitter) -> None:
    _emit(False, None, error=RuntimeError("boom"))
    assert emitted.events[0].response_tokens == 0


# ---------------------------------------------------------------- MCP retry once


class _RL:
    def __init__(self, allowed: bool, retry_after_s: float = 0.0) -> None:
        self.allowed = allowed
        self.retry_after_s = retry_after_s


class _FakeLimiter:
    def __init__(self, second: _RL) -> None:
        self.second = second
        self.checks = 0

    def check(self, *a: Any, **k: Any) -> _RL:
        self.checks += 1
        return self.second


def _call(op_name: str, org: str) -> tuple[Any, None, Any]:
    """The op, auth and gate target `_ratelimit_retry_once` re-checks with."""
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.transport.gates import Target

    target = Target(org=org or None, namespace="ns", named_namespace="ns", register_namespace="ns")
    return OPERATIONS_BY_NAME[op_name], None, target


@pytest.fixture
def mcp_env(monkeypatch: pytest.MonkeyPatch):
    import server.core.ratelimit as rl_mod
    from server.transport import mcp_call as mcp_mod

    sleeps: list[float] = []

    async def _sleep(s: float) -> None:
        sleeps.append(s)

    monkeypatch.setattr(mcp_mod.asyncio, "sleep", _sleep)

    def _install(second: _RL) -> _FakeLimiter:
        lim = _FakeLimiter(second)
        monkeypatch.setattr(rl_mod, "get_limiter", lambda: lim)
        return lim

    return mcp_mod, sleeps, _install


def test_mcp_refusal_waits_the_hint_once_and_rechecks(mcp_env, settings_override) -> None:
    mcp_mod, sleeps, install = mcp_env
    settings_override(mcp_ratelimit_retry_max_wait_s=2.0)
    lim = install(_RL(True))
    out = asyncio.run(
        mcp_mod._ratelimit_retry_once(_RL(False, 0.7), *_call("memory.set", "o"), traffic_class="")
    )
    assert out.allowed is True and lim.checks == 1 and sleeps == [0.7]


def test_mcp_refusal_above_the_max_wait_is_returned_at_once(mcp_env, settings_override) -> None:
    mcp_mod, sleeps, install = mcp_env
    settings_override(mcp_ratelimit_retry_max_wait_s=2.0)
    lim = install(_RL(True))
    first = _RL(False, 30.0)
    out = asyncio.run(
        mcp_mod._ratelimit_retry_once(first, *_call("memory.set", "o"), traffic_class="")
    )
    assert out is first and lim.checks == 0 and sleeps == []


def test_a_zero_max_wait_returns_the_refusal(mcp_env, settings_override) -> None:
    mcp_mod, sleeps, install = mcp_env
    settings_override(mcp_ratelimit_retry_max_wait_s=0.0)
    lim = install(_RL(True))
    first = _RL(False, 0.5)
    out = asyncio.run(
        mcp_mod._ratelimit_retry_once(first, *_call("memory.set", "o"), traffic_class="")
    )
    assert out is first and lim.checks == 0 and sleeps == []


def test_mcp_second_refusal_is_the_one_reported(mcp_env, settings_override) -> None:
    mcp_mod, sleeps, install = mcp_env
    settings_override(mcp_ratelimit_retry_max_wait_s=2.0)
    second = _RL(False, 1.0)
    install(second)
    out = asyncio.run(
        mcp_mod._ratelimit_retry_once(_RL(False, 0.4), *_call("memory.set", "o"), traffic_class="")
    )
    assert out is second and sleeps == [0.4]


def test_a_limiter_that_fails_on_the_recheck_admits_the_call(
    mcp_env, settings_override, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The re-check is the shared gate, which fails open like the first
    check: a raising limiter admits the call instead of repeating the
    refusal it could no longer confirm."""
    import server.core.ratelimit as rl_mod

    mcp_mod, sleeps, _install = mcp_env
    settings_override(mcp_ratelimit_retry_max_wait_s=2.0)

    class _Down:
        def check(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("limiter down")

    monkeypatch.setattr(rl_mod, "get_limiter", lambda: _Down())
    out = asyncio.run(
        mcp_mod._ratelimit_retry_once(_RL(False, 0.3), *_call("memory.set", "o"), traffic_class="")
    )
    assert out is None and sleeps == [0.3]


def test_mcp_retry_rechecks_the_bench_bucket_not_production(
    mcp_env, settings_override, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bench call its own bucket refused stays refused: the re-check draws
    from the bench bucket, and production's tokens are untouched."""
    import server.core.ratelimit as rl_mod
    from server.core.ratelimit import Limit, Limiter

    mcp_mod, sleeps, _install = mcp_env
    settings_override(
        mcp_ratelimit_retry_max_wait_s=2.0,
        ratelimit_class_fractions='{"bench": 0.5}',
    )
    lim = Limiter(clock=lambda: 0.0)
    lim.set_namespace_limit("ns", "memory", Limit(capacity=4, rate=0))
    monkeypatch.setattr(rl_mod, "get_limiter", lambda: lim)
    for _ in range(2):
        assert lim.check("ns", "memory.search", traffic_class="bench").allowed

    out = asyncio.run(
        mcp_mod._ratelimit_retry_once(
            _RL(False, 0.5), *_call("memory.search", ""), traffic_class="bench"
        )
    )

    assert out.allowed is False and sleeps == [0.5]
    for _ in range(4):
        assert lim.check("ns", "memory.search", traffic_class="production").allowed


def test_settings_defaults() -> None:
    from server.settings import settings

    assert settings.mcp_ratelimit_retry_max_wait_s == 2.0
