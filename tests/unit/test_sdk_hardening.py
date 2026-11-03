"""The Python SDK identifies itself, never holds the credential
where a repr can see it, parses `Retry-After` as seconds or an HTTP-date, and
hands back response metadata. Server-free: httpx.MockTransport."""

from __future__ import annotations

import re
import sys
import uuid
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import httpx
import loom
import pytest
from loom import Agent, CallResult
from loom.errors import (
    INVALID_RESPONSE_CODE,
    NETWORK_ERROR_CODE,
    BackendUnavailable,
    LoomError,
    NotFound,
    Overloaded,
    RateLimited,
)
from loom.transport import (
    SDK_IDENT,
    Transport,
    parse_retry_after,
    retry_after_from,
)

SDK_ROOT = Path(__file__).resolve().parents[2] / "sdk" / "python"


def _mock(t: Transport, handler: Any) -> None:
    t._client = httpx.AsyncClient(base_url=t.base_url, transport=httpx.MockTransport(handler))


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    import loom.transport as tmod

    out: list[float] = []

    async def _sleep(s: float) -> None:
        out.append(s)

    monkeypatch.setattr(tmod.asyncio, "sleep", _sleep)
    return out


# ---- Retry-After parsing -------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2", 2.0),
        (" 0.5 ", 0.5),
        ("0", 0.0),
        ("-3", 0.0),
        ("", None),
        (None, None),
        ("soon", None),
        ("nan", None),
        ("inf", None),
    ],
)
def test_parse_retry_after_numbers_and_garbage(value: str | None, expected: float | None) -> None:
    assert parse_retry_after(value) == expected


def test_parse_retry_after_http_date_is_measured_from_now() -> None:
    now = datetime(2026, 10, 21, 7, 28, 0, tzinfo=UTC)
    header = format_datetime(now + timedelta(seconds=90), usegmt=True)
    assert parse_retry_after(header, now=now.timestamp()) == 90.0
    past = format_datetime(now - timedelta(minutes=5), usegmt=True)
    assert parse_retry_after(past, now=now.timestamp()) == 0.0


@pytest.mark.parametrize("tz", ["America/Los_Angeles", "Asia/Kolkata", "UTC"])
def test_a_zoneless_http_date_is_read_as_utc(monkeypatch: pytest.MonkeyPatch, tz: str) -> None:
    import time

    monkeypatch.setenv("TZ", tz)
    time.tzset()
    try:
        now = datetime(2026, 10, 21, 7, 28, 0, tzinfo=UTC).timestamp()
        assert parse_retry_after("Wed Oct 21 07:29:30 2026", now=now) == 90.0  # asctime
        assert parse_retry_after("Wed, 21 Oct 2026 07:29:30 -0000", now=now) == 90.0
    finally:
        monkeypatch.undo()
        time.tzset()


def test_repr_and_errors_never_show_url_userinfo() -> None:
    t = Transport("https://svc:hunter2pass@loom.example")
    assert "hunter2pass" not in repr(t) and "https://loom.example" in repr(t)
    assert t.base_url == "https://svc:hunter2pass@loom.example"  # the client still uses it


async def test_an_unreachable_error_never_shows_url_userinfo() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    t = Transport("https://svc:hunter2pass@loom.example", retry={"max_attempts": 1})
    _mock(t, handler)
    with pytest.raises(BackendUnavailable) as info:
        await t.post("/v1/memory.get", {})
    await t.close()
    assert str(info.value).startswith("cannot reach https://loom.example: ConnectError")
    assert "hunter2pass" not in str(info.value)


def test_retry_after_from_caps_and_defaults() -> None:
    def _resp(**headers: str) -> httpx.Response:
        return httpx.Response(429, headers=headers)

    assert retry_after_from(_resp()) == 1.0
    assert retry_after_from(_resp(**{"Retry-After": "7"})) == 7.0
    assert retry_after_from(_resp(**{"Retry-After": "3600"})) == 1.0  # over the 60 s cap
    assert retry_after_from(_resp(**{"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"})) == 0.0
    assert retry_after_from(_resp(), {"retry_after_s": 3}) == 3.0  # body-only hint
    assert retry_after_from(_resp(**{"Retry-After": "7"}), {"retry_after_s": 3}) == 7.0


async def test_a_429_with_an_http_date_does_not_crash_and_is_honoured(sleeps: list[float]) -> None:
    """An HTTP-date Retry-After must not raise ValueError from int()."""
    when = datetime.now(UTC) + timedelta(seconds=3)
    calls: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": format_datetime(when, usegmt=True)})
        return httpx.Response(200, json={"ok": True})

    t = Transport("http://x", api_key="k")
    _mock(t, handler)
    out = await t.post("/v1/memory.set", {})
    assert out == {"ok": True} and len(calls) == 2
    assert len(sleeps) == 1 and 1.5 <= sleeps[0] <= 3.0
    await t.close()


async def test_a_429_past_the_budget_surfaces_with_the_parsed_hint_and_request_id() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "4", "X-Request-Id": "req-9"})

    t = Transport("http://x", api_key="k", pacing=False)
    _mock(t, handler)
    with pytest.raises(RateLimited) as ei:
        await t.post("/v1/memory.set", {})
    assert ei.value.retry_after_s == 4.0
    assert ei.value.request_id == "req-9" and ei.value.status == 429
    await t.close()


# ---- the credential ------------------------------------------------------


def test_repr_and_attributes_never_carry_the_key() -> None:
    key = "sk-loom-very-secret-1234"
    t = Transport("http://x", api_key=key)
    assert key not in repr(t)
    assert key not in repr(vars(t))
    assert "…1234" in repr(t)
    assert t.has_credential is True
    a = Agent(org="o", namespace="n", agent="a", api_key=key)
    assert key not in repr(a.transport)


async def test_blank_key_sends_no_authorization_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOOM_API_KEY", "   ")
    seen: list[httpx.Headers] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.headers)
        return httpx.Response(200, json={})

    a = Agent(org="o", namespace="n", agent="a", base_url="http://x")
    assert a.transport.has_credential is False
    _mock(a.transport, handler)
    await a.transport.post("/v1/ops.capabilities", {})
    assert "authorization" not in seen[0]
    assert "none" in repr(a.transport)
    await a.close()


# ---- identifying headers and the response envelope ----------------------


async def test_every_request_identifies_the_sdk_and_carries_a_request_id() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"ok": True})

    t = Transport("http://x", api_key="k")
    _mock(t, handler)
    await t.post("/v1/memory.get", {})
    h = seen[0].headers
    assert h["User-Agent"] == SDK_IDENT == f"loom-py/{loom.__version__}"
    assert h["X-Loom-SDK"] == SDK_IDENT
    v = sys.version_info
    assert h["X-Loom-Runtime"].startswith(f"python/{v.major}.{v.minor}.{v.micro} ")
    assert h["X-Loom-Retry-Count"] == "0"
    assert re.fullmatch(r"[0-9a-f-]{36}", h["X-Request-Id"])
    await t.close()


async def test_retry_count_header_increments_across_a_5xx_retry() -> None:
    counts: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        counts.append(req.headers["X-Loom-Retry-Count"])
        if len(counts) < 3:
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(200, json={"ok": True})

    t = Transport("http://x", api_key="k")
    _mock(t, handler)
    assert await t.post("/v1/memory.get", {}) == {"ok": True}
    assert counts == ["0", "1", "2"]
    await t.close()


async def test_attempts_keep_counting_across_a_429_pacing_wait(sleeps: list[float]) -> None:
    script = [503, 429, 503, 200]
    counts: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        counts.append(req.headers["X-Loom-Retry-Count"])
        status = script[len(counts) - 1]
        return httpx.Response(
            status, json={"detail": {"code": "OVERLOADED"}} if status != 200 else {}
        )

    t = Transport("http://x")
    _mock(t, handler)
    r = await t.post_with_meta("/v1/memory.get", {})
    await t.close()
    assert counts == ["0", "1", "2", "3"] and r.attempts == 4


async def test_agent_close_closes_the_pool_even_when_the_flush_fails() -> None:
    a = Agent(org="o", namespace="n", agent="a", base_url="http://x", api_key="k")

    async def boom() -> None:
        raise RuntimeError("flush failed")

    a.memory.flush_sets = boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await a.close()
    assert a.transport._client.is_closed


async def test_post_with_meta_returns_the_server_metadata() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"hits": []},
            headers={
                "X-Request-Id": req.headers["X-Request-Id"],
                "X-Schema-Version": "2026-09-16",
                "Server-Timing": 'app;dur=12.5, ch;dur=3.1;desc="2 statements, 4 kB"',
                "X-RateLimit-Limit": "100",
                "X-RateLimit-Remaining": "99",
                "X-RateLimit-Reset": "5",
            },
        )

    t = Transport("http://x", api_key="k")
    _mock(t, handler)
    r = await t.post_with_meta("/v1/memory.search", {}, request_id="my-req")
    assert isinstance(r, CallResult)
    assert r.data == {"hits": []} and r.status == 200 and r.attempts == 1
    assert r.request_id == "my-req" and r.schema_version == "2026-09-16"
    assert r.server_timing_ms == 12.5
    assert r.rate_limit is not None and (r.rate_limit.limit, r.rate_limit.remaining) == (100, 99)
    assert t.rate_limit("/v1/memory.search") is r.rate_limit
    await t.close()


async def test_a_request_id_the_server_would_replace_is_not_sent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sent: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        sent.append(req.headers["X-Request-Id"])
        return httpx.Response(200, json={}, headers={"X-Request-Id": req.headers["X-Request-Id"]})

    t = Transport("http://x", api_key="k")
    _mock(t, handler)
    kept = await t.post_with_meta("/v1/memory.search", {}, request_id="dbg-0001")
    with caplog.at_level("WARNING", logger="loom"):
        replaced = await t.post_with_meta("/v1/memory.search", {}, request_id="my id")
    assert [r.levelname for r in caplog.records if "'my id'" in r.getMessage()] == ["WARNING"]
    assert sent[0] == "dbg-0001" and kept.request_id == "dbg-0001"
    assert str(uuid.UUID(sent[1])) == sent[1] and replaced.request_id == sent[1]
    await t.close()


async def test_post_with_meta_sends_the_extra_headers() -> None:
    seen: list[httpx.Headers] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.headers)
        return httpx.Response(200, json={"results": []})

    t = Transport("http://x", api_key="k")
    _mock(t, handler)
    await t.post_with_meta("/v1/memory.search", {}, extra_headers={"X-Loom-Run-Id": "r1"})
    assert seen[0]["X-Loom-Run-Id"] == "r1" and seen[0]["X-Request-Id"]
    await t.close()


async def test_agent_call_with_meta_stamps_identity_and_returns_metadata() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        import json

        bodies.append(json.loads(req.content))
        return httpx.Response(200, json={"n": 1}, headers={"X-Request-Id": "abc"})

    a = Agent(org="o", namespace="n", agent="bot", api_key="k", base_url="http://x")
    _mock(a.transport, handler)
    r = await a.call_with_meta("memory.count", user_id="u1")
    assert r.data == {"n": 1} and r.request_id == "abc"
    assert (
        bodies[0]["org"] == "o" and bodies[0]["user_id"] == "u1" and bodies[0]["session_id"] == "-"
    )
    assert await a.call("memory.count") == {"n": 1}
    await a.close()


async def test_errors_carry_request_id_status_and_the_5xx_body_class() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("missing"):
            return httpx.Response(
                404, json={"detail": "Not Found"}, headers={"X-Request-Id": "r404"}
            )
        # A server before the Retry-After header: the hint is in the body only.
        return httpx.Response(
            503,
            json={"detail": {"code": "OVERLOADED", "message": "pool busy", "retry_after_s": 2}},
            headers={"X-Request-Id": "r503"},
        )

    t = Transport("http://x", api_key="k")
    _mock(t, handler)
    with pytest.raises(NotFound) as e404:
        await t.post("/v1/nope.missing", {})
    assert e404.value.request_id == "r404" and e404.value.status == 404
    with pytest.raises(Overloaded) as e503:
        await t.post("/v1/memory.get", {})
    assert e503.value.request_id == "r503" and e503.value.retry_after_s == 2.0
    assert e503.value.status == 503
    await t.close()


@pytest.mark.parametrize(
    ("exc", "said"),
    [
        (httpx.ReadError("connection reset"), "response from http://x cut off: ReadError"),
        (httpx.RemoteProtocolError("peer closed"), "response from http://x cut off"),
        (httpx.WriteError("broken pipe"), "cannot reach http://x: WriteError"),
        (httpx.ProxyError("407"), "cannot reach http://x: ProxyError"),
    ],
)
async def test_every_other_httpx_failure_is_typed_and_not_retried(
    exc: Exception, said: str
) -> None:
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise exc

    t = Transport("http://x")
    _mock(t, handler)
    with pytest.raises(BackendUnavailable, match=re.escape(said)) as info:
        await t.post("/v1/memory.get", {})  # idempotent: still one POST
    await t.close()
    assert info.value.code == NETWORK_ERROR_CODE and info.value.__cause__ is exc
    assert info.value.request_id and calls == 1


@pytest.mark.parametrize(
    "resp",
    [
        httpx.Response(200, text="<html>proxy login</html>" + "x" * 500),
        httpx.Response(200, json=[1, 2]),
        httpx.Response(200, json="ok"),
    ],
    ids=["html", "list", "string"],
)
async def test_a_2xx_body_that_is_not_a_json_object_is_invalid_response(
    resp: httpx.Response,
) -> None:
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return resp

    t = Transport("http://x")
    _mock(t, handler)
    with pytest.raises(LoomError) as info:
        await t.post("/v1/memory.get", {})
    await t.close()
    err = info.value
    assert type(err) is LoomError and err.code == INVALID_RESPONSE_CODE
    assert err.status == 200 and err.request_id and calls == 1
    assert len(str(err)) < 300
    assert err.detail["message"] == resp.text[:200]


async def test_an_empty_2xx_body_is_an_empty_object() -> None:
    t = Transport("http://x")
    _mock(t, lambda req: httpx.Response(204))
    assert await t.post("/v1/memory.delete", {}) == {}
    await t.close()


# ---- packaging -----------------------------------------------------------


def test_version_is_single_sourced_and_the_package_is_typed() -> None:
    pyproject = (SDK_ROOT / "pyproject.toml").read_text()
    m = re.search(r'^version = "([^"]+)"', pyproject, re.M)
    assert m and m.group(1) == loom.__version__
    assert (SDK_ROOT / "loom" / "py.typed").exists()
    assert '"Typing :: Typed"' in pyproject
    assert "__version__" in loom.__all__


def test_each_sdk_package_ships_the_repository_license() -> None:
    # The packages cannot reach the root LICENSE, so they carry byte-identical copies.
    root = SDK_ROOT.parents[1] / "LICENSE"
    for sdk in ("python", "typescript"):
        assert (SDK_ROOT.parent / sdk / "LICENSE").read_bytes() == root.read_bytes()


def test_app_duration_reads_the_app_metric_of_server_timing() -> None:
    from loom.transport import app_duration_ms

    assert app_duration_ms('ch;dur=3.1;desc="2 statements", app;dur=42') == 42.0
    assert app_duration_ms("app;dur=9") == 9.0
    assert app_duration_ms("ch;dur=3") is None
    assert app_duration_ms("app;dur=x") is None
    assert app_duration_ms(None) is None


def test_search_hit_keeps_only_numeric_score_components() -> None:
    from loom.memory import _parse_search_hit

    hit = _parse_search_hit(
        {
            "memory_key": "k",
            "memory_type": "semantic",
            "content_excerpt": "x",
            "distance": 0.1,
            "similarity": 0.9,
            "score_components": {"sim": 0.5, "boost": 2, "why": "text"},
        }
    )
    assert hit.score_components == {"sim": 0.5, "boost": 2.0}
