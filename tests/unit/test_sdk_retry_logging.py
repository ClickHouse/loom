"""The retry policy as one validated object with a per-call
override, timeouts retried only under idempotency, Retry-After honoured on a
retried status, and logging through the `loom` logger with one redaction
point. Server-free: httpx.MockTransport."""

from __future__ import annotations

import logging
import math
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import loom
import pytest
from loom import Agent, SyncAgent
from loom._logging import (
    SensitiveHeadersFilter,
    logger,
    parse_log_level,
    redact_header_value,
    redact_headers,
    redact_text,
)
from loom.errors import BackendUnavailable, NotFound, Overloaded
from loom.retry import DEFAULT_RETRY_POLICY, RetryPolicy, coerce_policy
from loom.transport import Transport

KEY = "sk-loom-very-secret-1234"


def _mock(t: Transport, handler: Any) -> None:
    t._client = httpx.AsyncClient(
        base_url=t.base_url,
        transport=httpx.MockTransport(handler),
        headers={"Authorization": f"Bearer {KEY}"},
    )


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    import loom.transport as tmod

    out: list[float] = []

    async def _sleep(s: float) -> None:
        out.append(s)

    monkeypatch.setattr(tmod.asyncio, "sleep", _sleep)
    return out


# ---- the policy object ---------------------------------------------------


def test_defaults_and_validation_name_the_field() -> None:
    p = DEFAULT_RETRY_POLICY
    assert p.max_attempts == 3 and 408 in p.statuses and 503 in p.statuses and 429 not in p.statuses
    assert p.retry_timeouts == "idempotent" and p.max_retry_after_s == 60.0
    assert math.isinf(p.total_backoff_budget_s)
    with pytest.raises(ValueError, match="retry.max_attempts must be an integer >= 1, got 0"):
        RetryPolicy(max_attempts=0)
    with pytest.raises(ValueError, match="retry.backoff_s must be a non-negative number"):
        RetryPolicy(backoff_s=-1)
    with pytest.raises(ValueError, match="retry.backoff_jitter must be within 0..1"):
        RetryPolicy(backoff_jitter=2)
    with pytest.raises(ValueError, match="retry_timeouts must be never"):
        RetryPolicy(retry_timeouts="sometimes")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="non-HTTP status 42"):
        RetryPolicy(statuses=frozenset({42}))
    with pytest.raises(ValueError, match="retry has no field \\['max_retries'\\]"):
        p.replace(max_retries=5)
    q = p.replace(max_attempts=5, statuses=[502, 503])
    assert (
        q.max_attempts == 5 and q.statuses == frozenset({502, 503}) and q.backoff_s == p.backoff_s
    )
    assert coerce_policy({"max_attempts": 1}, p).max_attempts == 1
    assert coerce_policy(None, p) is p and coerce_policy(q, p) is q
    with pytest.raises(TypeError, match="retry must be a RetryPolicy or a dict"):
        coerce_policy(3, p)  # type: ignore[arg-type]
    assert p.as_dict()["statuses"][:2] == [408, 500]


def test_backoff_doubles_is_capped_and_jittered_inside_the_band() -> None:
    p = RetryPolicy(backoff_s=0.1, backoff_max_s=0.35, backoff_jitter=0.5)
    assert p.backoff_for(1, lambda: 0.5) == pytest.approx(0.1)
    assert p.backoff_for(2, lambda: 0.5) == pytest.approx(0.2)
    assert p.backoff_for(3, lambda: 0.5) == pytest.approx(0.35)
    assert p.backoff_for(1, lambda: 0.0) == pytest.approx(0.075)
    assert p.backoff_for(1, lambda: 1.0) == pytest.approx(0.125)


def test_agent_and_sync_agent_take_a_policy_and_repr_shows_attempts() -> None:
    a = Agent(org="o", namespace="n", agent="a", api_key="k", retry={"max_attempts": 5})
    assert a.transport.retry.max_attempts == 5 and "retry=5 attempts" in repr(a.transport)
    with pytest.raises(ValueError, match="retry.max_attempts"):
        Agent(org="o", namespace="n", agent="a", api_key="k", retry={"max_attempts": 0})
    with SyncAgent(
        org="o", namespace="n", agent="a", api_key="k", retry=RetryPolicy(max_attempts=1)
    ) as sa:
        assert sa.transport.retry.max_attempts == 1
    assert loom.RetryPolicy is RetryPolicy and loom.DEFAULT_RETRY_POLICY is DEFAULT_RETRY_POLICY


# ---- the transport under a policy ---------------------------------------


async def test_retry_after_is_honoured_on_a_retried_503_and_capped(sleeps: list[float]) -> None:
    n = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal n
        n += 1
        if n == 1:
            return httpx.Response(
                503,
                json={"detail": {"code": "OVERLOADED", "message": "busy"}},
                headers={"Retry-After": "2"},
            )
        if n == 2:
            return httpx.Response(
                503,
                json={"detail": {"code": "OVERLOADED", "message": "busy"}},
                headers={"Retry-After": "3600"},
            )
        return httpx.Response(200, json={"ok": True})

    t = Transport("http://x", retry=RetryPolicy(backoff_s=0.1, backoff_jitter=0))
    _mock(t, handler)
    assert await t.post("/v1/memory.get", {}) == {"ok": True}
    assert sleeps == [pytest.approx(2.0), pytest.approx(0.2)]
    await t.close()


@pytest.mark.parametrize("headers", [{"Retry-After": "3"}, {}], ids=["header", "body-only"])
async def test_an_overloaded_503_waits_the_servers_hint(
    sleeps: list[float], headers: dict[str, str]
) -> None:
    """The server sends `retry_after_s` in the body and as `Retry-After`; a
    server that sends only the body field is honoured too."""
    n = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal n
        n += 1
        if n == 1:
            detail = {"code": "OVERLOADED", "message": "busy", "retry_after_s": 3}
            return httpx.Response(503, json={"detail": detail}, headers=headers)
        return httpx.Response(200, json={"ok": True})

    t = Transport("http://x", retry=RetryPolicy(backoff_s=0.1, backoff_jitter=0))
    _mock(t, handler)
    assert await t.post("/v1/memory.get", {}) == {"ok": True}
    assert sleeps == [pytest.approx(3.0)]
    await t.close()


async def test_a_key_still_running_is_retried_with_the_same_key(sleeps: list[float]) -> None:
    """A write that timed out client-side and is retried while the first
    request runs waits for its answer; a read sends no key at all."""
    keys: list[str | None] = []

    def handler(req: httpx.Request) -> httpx.Response:
        keys.append(req.headers.get("Idempotency-Key"))
        if req.url.path == "/v1/memory.set" and len(keys) == 1:
            detail = {"code": "IDEMPOTENT_IN_PROGRESS", "message": "m", "in_progress": True}
            return httpx.Response(503, json={"detail": detail}, headers={"Retry-After": "1"})
        return httpx.Response(200, json={"ok": True})

    t = Transport("http://x", retry=RetryPolicy(backoff_s=0.1, backoff_jitter=0))
    _mock(t, handler)
    assert await t.post("/v1/memory.set", {}) == {"ok": True}
    assert sleeps == [pytest.approx(1.0)]
    assert keys[0] is not None and keys[0] == keys[1]
    assert await t.post("/v1/memory.get", {}) == {"ok": True}
    assert keys[2] is None
    await t.close()


async def test_per_call_override_disables_retries_and_the_transport_policy_stays(
    sleeps: list[float],
) -> None:
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return (
            httpx.Response(502, text="bad gateway") if calls < 3 else httpx.Response(200, json={})
        )

    t = Transport("http://x")
    _mock(t, handler)
    with pytest.raises(BackendUnavailable):
        await t.post("/v1/memory.get", {}, retry={"max_attempts": 1})
    assert calls == 1
    await t.post("/v1/memory.get", {})
    assert calls == 3 and len(sleeps) == 1
    await t.close()


@pytest.mark.parametrize(
    ("status", "code", "posts"),
    [(502, "", 1), (503, "OVERLOADED", 1), (500, "INTERNAL", 1), (503, "AUTH_UNAVAILABLE", 2)],
)
async def test_a_5xx_is_retried_for_a_non_idempotent_op_only_when_it_never_ran(
    sleeps: list[float], status: int, code: str, posts: int
) -> None:
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(status, json={"detail": {"code": code, "message": "x"}})
        return httpx.Response(200, json={})

    t = Transport("http://x")
    _mock(t, handler)
    if posts == 1:
        with pytest.raises(BackendUnavailable):
            await t.post("/v1/memory.set_from_messages", {})
    else:
        await t.post("/v1/memory.set_from_messages", {})
    assert calls == posts
    calls = 0
    await t.post("/v1/memory.get", {})  # idempotent: every retried status retries
    assert calls == 2
    await t.close()


@pytest.mark.parametrize(
    "code",
    ["ANALYSIS_UNAVAILABLE", "CODED_WIDTH_MISMATCH", "GATEWAY_REQUIRED", "MODEL_CALL_UNATTRIBUTED"],
)
async def test_a_configuration_error_is_not_retried(sleeps: list[float], code: str) -> None:
    """A 503 an operator must fix: a retry gets the same answer."""
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={"detail": {"code": code, "message": "x"}})

    t = Transport("http://x")
    _mock(t, handler)
    with pytest.raises(BackendUnavailable):
        await t.post("/v1/memory.get", {})  # idempotent, yet not retried
    assert calls == 1 and sleeps == []
    await t.close()


async def test_timeouts_retry_only_for_ops_the_server_dedups_unless_told(
    sleeps: list[float],
) -> None:
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        if len([c for c in calls if c == req.url.path]) == 1:
            raise httpx.ReadTimeout("slow")
        return httpx.Response(200, json={"ok": True})

    t = Transport("http://x")
    _mock(t, handler)
    assert await t.post("/v1/memory.set", {}) == {"ok": True}  # its key is deduplicated
    assert calls.count("/v1/memory.set") == 2
    # Read-only: no replay, so a retry would run a second `memory.ask` beside the first.
    with pytest.raises(BackendUnavailable, match="ReadTimeout"):
        await t.post("/v1/memory.ask", {})
    assert calls.count("/v1/memory.ask") == 1
    # Not idempotent: surfaces at once — as the typed error naming the server,
    # the httpx exception as its cause.
    with pytest.raises(BackendUnavailable, match="no response from http://x: ReadTimeout") as exc:
        await t.post("/v1/memory.set_from_messages", {})
    assert isinstance(exc.value.__cause__, httpx.ReadTimeout)
    assert calls.count("/v1/memory.set_from_messages") == 1
    calls.clear()
    assert await t.post("/v1/memory.set_from_messages", {}, retry={"retry_timeouts": "always"}) == {
        "ok": True
    }
    assert len(calls) == 2
    calls.clear()
    with pytest.raises(BackendUnavailable, match="no response from http://x"):
        await t.post("/v1/memory.get", {}, retry={"retry_timeouts": "never"})
    assert len(calls) == 1
    await t.close()


async def test_connection_errors_retry_by_default_and_not_when_told(sleeps: list[float]) -> None:
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("refused")
        return httpx.Response(200, json={})

    t = Transport("http://x")
    _mock(t, handler)
    await t.post("/v1/memory.get", {})
    assert calls == 2
    calls = 0
    with pytest.raises(BackendUnavailable, match="cannot reach http://x: ConnectError") as exc:
        await t.post("/v1/memory.get", {}, retry={"retry_connection_errors": False})
    assert exc.value.code == "NETWORK" and isinstance(exc.value.__cause__, httpx.ConnectError)
    assert calls == 1
    await t.close()


async def test_total_backoff_budget_stops_before_the_attempt_count(sleeps: list[float]) -> None:
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, text="boom")

    t = Transport(
        "http://x",
        retry=RetryPolicy(
            max_attempts=4, backoff_s=1.0, backoff_jitter=0, total_backoff_budget_s=3.5
        ),
    )
    _mock(t, handler)
    with pytest.raises(BackendUnavailable):
        await t.post("/v1/memory.get", {})
    assert sleeps == [pytest.approx(1.0), pytest.approx(2.0)] and calls == 3
    await t.close()


async def test_statuses_are_the_policys_and_4xx_never_retry(sleeps: list[float]) -> None:
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if req.url.path.endswith("408"):
            return httpx.Response(408) if calls == 1 else httpx.Response(200, json={})
        return httpx.Response(404, json={"detail": "Not Found"})

    t = Transport("http://x")
    _mock(t, handler)
    await t.post("/v1/op.408", {})
    assert calls == 2
    calls = 0
    with pytest.raises(NotFound):
        await t.post("/v1/op.404", {})
    assert calls == 1
    t2 = Transport("http://x", retry={"statuses": [502]})
    _mock(t2, lambda req: httpx.Response(500, text="boom"))
    with pytest.raises(BackendUnavailable):
        await t2.post("/v1/memory.get", {})
    await t.close()
    await t2.close()


async def test_the_pacing_loop_is_unchanged_a_429_is_not_a_retry(sleeps: list[float]) -> None:
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return (
            httpx.Response(429, headers={"Retry-After": "1"})
            if calls == 1
            else httpx.Response(200, json={})
        )

    t = Transport(
        "http://x", retry={"max_attempts": 1}
    )  # retries off; pacing still handles the 429
    _mock(t, handler)
    await t.post("/v1/memory.set", {})
    assert calls == 2 and sleeps == [1.0]
    await t.close()


# ---- logging -------------------------------------------------------------


def test_redaction_and_levels() -> None:
    assert redact_header_value("Authorization", f"Bearer {KEY}") == "Bearer …1234"
    assert redact_header_value("authorization", "Bearer short") == "Bearer …"
    assert redact_header_value("X-Api-Key", KEY) == "…1234"
    assert redact_header_value("X-Request-Id", "rid") == "rid"
    assert redact_headers({"Authorization": f"Bearer {KEY}", "Accept": "a"}) == {
        "Authorization": "Bearer …1234",
        "Accept": "a",
    }
    assert (
        redact_text(f"headers={{'Authorization': 'Bearer {KEY}'}}")
        == "headers={'Authorization': 'Bearer …1234'}"
    )
    assert (
        parse_log_level(" INFO ", "x") == logging.INFO
        and parse_log_level("off", "x") > logging.CRITICAL
    )
    with pytest.raises(ValueError, match="LOOM_LOG_LEVEL: invalid log level 'loud'"):
        parse_log_level("loud", "LOOM_LOG_LEVEL")
    assert any(isinstance(f, SensitiveHeadersFilter) for f in logger.filters)
    assert any(isinstance(h, logging.NullHandler) for h in logger.handlers)


@pytest.mark.parametrize("level", [logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR])
async def test_the_key_never_reaches_a_record_at_any_level(
    level: int, caplog: pytest.LogCaptureFixture, sleeps: list[float]
) -> None:
    n = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal n
        n += 1
        if n == 1:
            return httpx.Response(503, json={"detail": {"code": "OVERLOADED", "message": "busy"}})
        if n == 2:
            return httpx.Response(200, json={"ok": True}, headers={"X-Request-Id": "rid-7"})
        return httpx.Response(404, json={"detail": "Not Found"})

    t = Transport("http://x", api_key=KEY, retry={"backoff_s": 0.001})
    _mock(t, handler)
    with caplog.at_level(level, logger="loom"):
        await t.post("/v1/memory.get", {})
        with pytest.raises(NotFound):
            await t.post("/v1/memory.get", {})
    assert caplog.records, "expected log records"
    for r in caplog.records:
        assert KEY not in r.getMessage()
    if level <= logging.DEBUG:
        debug = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
        assert debug and "Bearer …1234" in debug[0]
    if level <= logging.INFO:
        info = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
        assert (
            len(info) == 1
            and info[0].startswith("/v1/memory.get 200 ")
            and info[0].endswith("attempts=2 rid=rid-7")
        )
    if level <= logging.WARNING:
        warn = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert warn and warn[0].startswith(
            "/v1/memory.get attempt 1/3 503 OVERLOADED; retrying in "
        )
    err = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert (
        err
        and err[0].startswith("/v1/memory.get failed after ")
        and "NotFound RESOURCE_NOT_FOUND" in err[0]
    )
    await t.close()


async def test_a_header_dict_logged_by_mistake_is_still_redacted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG, logger="loom"):
        logger.debug("oops %s", {"Authorization": f"Bearer {KEY}"})
        logger.debug(f"oops Bearer {KEY}")
    for r in caplog.records:
        assert KEY not in r.getMessage()


def test_loom_log_level_env_sets_the_logger(monkeypatch: pytest.MonkeyPatch) -> None:
    from loom._logging import configure_from_env

    before = logger.level
    try:
        configure_from_env({"LOOM_LOG_LEVEL": "debug"})
        assert logger.level == logging.DEBUG
        configure_from_env({})
        assert logger.level == logging.DEBUG  # unset leaves it alone
        configure_from_env({"LOOM_LOG_LEVEL": "critical"})  # any stdlib name the server takes
        assert logger.level == logging.CRITICAL
        with pytest.warns(RuntimeWarning, match="LOOM_LOG_LEVEL: invalid log level 'loud'"):
            configure_from_env({"LOOM_LOG_LEVEL": "loud"})
        assert logger.level == logging.CRITICAL
    finally:
        logger.setLevel(before)


def test_tenacity_is_no_longer_a_dependency() -> None:
    from pathlib import Path

    text = (Path(__file__).resolve().parents[2] / "sdk" / "python" / "pyproject.toml").read_text()
    assert "tenacity" not in text
    assert "Overloaded" in dir(loom.errors) and Overloaded.code == "OVERLOADED"


def test_import_loom_survives_any_loom_log_level() -> None:
    """The server's own values (`critical`) and garbage alike: `import loom`
    never fails on the environment; garbage is a warning."""
    # The child finds the SDK the way this process did (CI does not install it).
    sdk_root = str(Path(loom.__file__).resolve().parents[1])
    for value, warned in (("critical", False), ("loud", True)):
        out = subprocess.run(
            [sys.executable, "-c", "import loom"],
            env={**os.environ, "LOOM_LOG_LEVEL": value, "PYTHONPATH": sdk_root},
            capture_output=True,
            text=True,
            check=False,
        )
        assert out.returncode == 0, out.stderr
        assert ("invalid log level 'loud'" in out.stderr) is warned


async def test_a_call_whose_token_endpoint_was_away_is_retried_once_sent(
    sleeps: list[float],
) -> None:
    """A bot's token provider raising BackendUnavailable means the op was
    never sent: retried as a connection failure, and the server sees it once."""
    seen: list[str] = []

    class _FlakyTokens(httpx.Auth):
        asked = 0

        def auth_flow(self, request: httpx.Request) -> Any:
            _FlakyTokens.asked += 1
            if _FlakyTokens.asked == 1:
                raise BackendUnavailable("token endpoint: 503; retry shortly")
            request.headers["Authorization"] = "Bearer t"
            yield request

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["X-Loom-Retry-Count"])
        return httpx.Response(200, json={"ok": True})

    t = Transport("http://x", retry=RetryPolicy(backoff_s=0.1, backoff_jitter=0))
    t._client = httpx.AsyncClient(
        base_url=t.base_url, transport=httpx.MockTransport(handler), auth=_FlakyTokens()
    )
    assert await t.post("/v1/memory.get", {}) == {"ok": True}
    assert (_FlakyTokens.asked, seen, len(sleeps)) == (2, ["0"], 1)
    await t.close()


async def test_a_token_endpoint_that_stays_away_keeps_its_cause(sleeps: list[float]) -> None:
    """Once the retries are spent, the provider's BackendUnavailable is raised
    with the network error it was raised from, for the traceback."""
    cause = httpx.ConnectError("refused")

    class _AwayTokens(httpx.Auth):
        def auth_flow(self, request: httpx.Request) -> Any:
            raise BackendUnavailable("token endpoint: cannot reach it") from cause
            yield request  # pragma: no cover - a generator, as httpx expects

    t = Transport("http://x", retry=RetryPolicy(max_attempts=2, backoff_s=0.1, backoff_jitter=0))
    t._client = httpx.AsyncClient(
        base_url=t.base_url,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
        auth=_AwayTokens(),
    )
    with pytest.raises(BackendUnavailable) as err:
        await t.post("/v1/memory.get", {})
    assert err.value.__cause__ is cause and len(sleeps) == 1
    await t.close()
