"""The reference client over a fake transport: consumer tag
and identity on every call, one wait budget for a 429 (the transport's),
honest attempt counts, fail-soft on 5xx / 4xx, additive-field guards on both sides, the catalogue
probe through its three sources, namespace registration with the reserved
traffic class, and outcome / run reporting."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from loom.errors import BackendUnavailable, NotFound, RateLimited
from loom.reference_client import (
    ARCHETYPES,
    TRAFFIC_CLASSES,
    MissingField,
    ReferenceClient,
    normalize_consumer,
)
from loom.transport import CallResult as WireResult
from loom.transport import Transport


class FakeTransport:
    """Scripted transport: `responses[op]` is a value or a list consumed in
    order; an exception instance is raised instead of returned. It stamps
    `consumer` like the real one, and every call took `attempts` attempts."""

    def __init__(self, responses: dict[str, Any] | None = None, consumer: str = ""):
        self.responses: dict[str, Any] = dict(responses or {})
        self.calls: list[tuple[str, dict[str, Any], str | None, float | None]] = []
        self.closed = False
        self.consumer = consumer
        self.attempts = 1
        self.headers: list[dict[str, str]] = []

    async def post_with_meta(
        self,
        op_path: str,
        payload: dict[str, Any],
        idempotency_key: str | None = None,
        *,
        timeout_s: float | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> WireResult:
        if self.consumer and "consumer" not in payload:
            payload = {**payload, "consumer": self.consumer}
        self.headers.append(dict(extra_headers or {}))
        self.calls.append((op_path, dict(payload), idempotency_key, timeout_s))
        op = op_path.removeprefix("/v1/")
        scripted = self.responses.get(op)
        if isinstance(scripted, list):
            scripted = scripted.pop(0) if scripted else NotFound(f"[RESOURCE_NOT_FOUND] {op}")
        if scripted is None:
            raise NotFound(f"[RESOURCE_NOT_FOUND] no route {op}")
        if isinstance(scripted, BaseException):
            raise scripted
        return WireResult(dict(scripted), 200, "rid", "", None, None, self.attempts)

    async def close(self) -> None:
        self.closed = True

    def payloads(self, op: str) -> list[dict[str, Any]]:
        return [p for path, p, _k, _t in self.calls if path == f"/v1/{op}"]


def client(
    responses: dict[str, Any] | None = None, **kw: Any
) -> tuple[ReferenceClient, FakeTransport]:
    consumer = kw.pop("consumer", "test-consumer")
    t = FakeTransport(responses, consumer=consumer)
    c = ReferenceClient("o", "ns", consumer=consumer, agent="ag", transport=t, **kw)
    return c, t


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []

    async def fake_sleep(s: float) -> None:
        slept.append(s)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return slept


# -- identity + consumer -------------------------------------------------------


def test_consumer_is_normalized_and_bounded() -> None:
    assert normalize_consumer("  qa-agent\n") == "qa-agent"
    assert len(normalize_consumer("x" * 100)) == 64
    with pytest.raises(ValueError):
        normalize_consumer("   ")
    assert set(TRAFFIC_CLASSES) == {"production", "interactive", "bench"}


async def test_every_call_carries_identity_and_consumer() -> None:
    c, t = client({"memory.search": {"results": []}})
    r = await c.search("q", top_k=3)
    assert r.ok and r.data == {"results": []}
    p = t.payloads("memory.search")[0]
    assert p["org"] == "o" and p["namespace"] == "ns" and p["agent"] == "ag"
    assert p["consumer"] == "test-consumer" and p["top_k"] == 3
    assert t.calls[0][3] == 30.0  # the client's timeout rides the call


async def test_org_scoped_ops_carry_org_and_consumer_only() -> None:
    c, t = client(
        {"cost.summary": {"rows": [], "total_ops": 0}, "erasure.request": {"erasure_id": "e"}}
    )
    await c.call(
        "cost.summary", {"namespace": "other", "from_date": "2026-09-14", "to_date": "2026-09-14"}
    )
    p = t.payloads("cost.summary")[0]
    assert p["org"] == "o" and p["consumer"] == "test-consumer" and p["namespace"] == "other"
    assert "agent" not in p
    await c.call(
        "erasure.request", {"scope_type": "namespace_list", "scope_value": "[]", "requester": "r"}
    )
    q = t.payloads("erasure.request")[0]
    assert q["org"] == "o" and "namespace" not in q and "agent" not in q


async def test_namespace_ops_do_not_carry_agent_and_ask_gets_double_timeout() -> None:
    c, t = client(
        {
            "namespace.register": {"registered": True},
            "memory.ask": {"answer": "a", "abstain": False},
        }
    )
    await c.register_namespace("owner@x")
    assert "agent" not in t.payloads("namespace.register")[0]
    await c.ask("q")
    assert t.calls[-1][3] == 60.0


# -- fail-soft + retries ----------------------------------------------------------


async def test_a_429_past_the_transports_wait_is_a_result_not_a_second_wait(
    _no_sleep: list[float],
) -> None:
    c, t = client({"memory.set": [RateLimited(retry_after_s=2), {"memory_key": "k"}]})
    r = await c.set("k", "v")
    assert not r.ok and r.code == "RATE_LIMITED" and r.attempts == 1
    assert _no_sleep == [] and len(t.calls) == 1
    assert c.failures_by_code == {"RATE_LIMITED": 1}


async def test_the_owned_transport_carries_the_wait_budget_and_consumer(
    _no_sleep: list[float],
) -> None:
    """A 429 storm waits `max_retry_wait_s` in all, not once per layer."""
    requests = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(429, headers={"Retry-After": "5"})

    c = ReferenceClient("o", "ns", consumer="bench", base_url="http://x", max_retry_wait_s=15.0)
    assert isinstance(c.transport, Transport) and c.transport.consumer == "bench"
    c.transport._client = httpx.AsyncClient(
        base_url="http://x", transport=httpx.MockTransport(handler)
    )
    r = await c.set("k", "v")
    assert not r.ok and r.code == "RATE_LIMITED"
    assert sum(_no_sleep) <= 15.0 and requests == 4
    await c.close()
    assert c.transport._client.is_closed


async def test_attempts_count_the_transports_http_attempts() -> None:
    c, t = client({"memory.search": {"results": []}})
    t.attempts = 3  # two retries inside the transport
    r = await c.search("q")
    assert r.ok and r.attempts == 3
    assert c.stats()["attempts"] == 3 and c.stats()["retries"] == 2


async def test_5xx_and_4xx_are_results_not_exceptions() -> None:
    c, _t = client(
        {
            "memory.search": BackendUnavailable("502: bad gateway"),
            "memory.get": NotFound("[RESOURCE_NOT_FOUND] nope"),
        }
    )
    r = await c.search("q")
    assert not r.ok and r.code == "BACKEND_UNAVAILABLE" and r.data == {}
    g = await c.get("k")
    assert not g.ok and g.code == "RESOURCE_NOT_FOUND"
    with pytest.raises(MissingField):
        r.require("results")


async def test_fail_soft_off_raises_the_typed_error() -> None:
    c, _t = client({"memory.search": BackendUnavailable("502")}, fail_soft=False)
    with pytest.raises(BackendUnavailable):
        await c.search("q")


# -- additive-field guards ---------------------------------------------------------------


async def test_required_response_fields_are_asserted_and_unknown_ignored() -> None:
    c, _t = client(
        {
            "memory.set": {"memory_key": "k", "version": 1, "brand_new_field": 7},
            "memory.get": {"value": "x"},
        }
    )
    r = await c.set("k", "v")
    assert r.ok and r.data["brand_new_field"] == 7  # kept, never an error
    assert r.require("memory_key", "version")["version"] == 1
    g = await c.get("k")  # `found` is required on get
    assert not g.ok and g.code == "MISSING_FIELD"
    with pytest.raises(MissingField):
        r.require("no_such_field")


async def test_request_fields_the_catalogue_does_not_list_are_dropped() -> None:
    caps = {
        "build_commit": "abc",
        "ops": [
            {
                "name": op,
                "request_fields": ["org", "namespace", "agent", "consumer", "query", "top_k"],
            }
            for op in ARCHETYPES["memory_app"]
        ]
        + [
            {
                "name": "memory.search",
                "request_fields": ["org", "namespace", "agent", "consumer", "query", "top_k"],
            }
        ],
    }
    c, t = client({"ops.capabilities": caps, "memory.search": {"results": []}})
    probe = await c.probe("memory_app")
    assert probe.ok and probe.source == "ops.capabilities" and probe.build_commit == "abc"
    r = await c.search("q", top_k=2, include_tags=True)
    assert r.ok and r.dropped_fields == ("include_tags",)
    assert "include_tags" not in t.payloads("memory.search")[0]
    # no catalogue → nothing is dropped
    c2, t2 = client({"memory.search": {"results": []}})
    r2 = await c2.search("q", include_tags=True)
    assert r2.dropped_fields == () and t2.payloads("memory.search")[0]["include_tags"] is True


# -- probe ----------------------------------------------------------------------------------


async def test_probe_without_ops_capabilities_leaves_every_op_unverified() -> None:
    c, _t = client({})
    p = await c.probe("plugin")
    assert p.source == "none" and p.ok and p.unverified == list(ARCHETYPES["plugin"])
    with pytest.raises(ValueError):
        await c.probe("nope")


async def test_probe_reports_an_op_the_catalogue_lacks_as_missing() -> None:
    caps = {"ops": [{"name": n} for n in ARCHETYPES["harness"] if n != "cost.summary"]}
    c, _t = client({"ops.capabilities": caps})
    p = await c.probe("harness")
    assert p.missing == ["cost.summary"] and not p.ok
    assert "missing cost.summary" in p.summary()


async def test_probe_does_not_flip_fail_soft_off_permanently() -> None:
    c, _t = client({}, fail_soft=False)
    await c.probe("plugin")
    assert c.fail_soft is False


# -- namespace registration -------------------------------------------------------------------


async def test_register_namespace_carries_owner_kind_and_traffic_class() -> None:
    c, t = client({"namespace.register": {"registered": True}})
    r = await c.register_namespace("owner@x", traffic_class="bench", tags=["team:a"])
    assert r.ok
    p = t.payloads("namespace.register")[0]
    assert p["owner_email"] == "owner@x" and p["kind"] == "bench"
    assert p["traffic_class"] == "bench"
    assert p["tags"] == ["team:a", "consumer:test-consumer"]
    assert "retention_days" not in p
    r2 = await c.register_namespace("owner@x")
    p2 = t.payloads("namespace.register")[1]
    assert p2["kind"] == "memory" and p2["traffic_class"] == "production" and r2.ok
    with pytest.raises(ValueError):
        await c.register_namespace("owner@x", traffic_class="batch")


# -- outcomes + runs -------------------------------------------------------------------------------


async def test_outcome_and_code_outcome_default_source_to_the_consumer() -> None:
    c, t = client(
        {
            "memory.outcome": {"recorded": True, "evidence_count_after": 1},
            "code.outcome": {"recorded": True},
        }
    )
    r = await c.outcome("k", "used")
    assert r.ok
    p = t.payloads("memory.outcome")[0]
    assert p["memory_key"] == "k" and p["agent"] == "*" and p["source"] == "test-consumer"
    await c.code_outcome("used_in_answer", trace_run_id="tr1", targets=["src/a.cpp"])
    q = t.payloads("code.outcome")[0]
    assert (
        q["trace_run_id"] == "tr1"
        and q["targets"] == ["src/a.cpp"]
        and q["source"] == "test-consumer"
    )
    await c.code_outcome("shunt_pass", source="under_max_lines")
    assert "trace_run_id" not in t.payloads("code.outcome")[1]
    assert t.payloads("code.outcome")[1]["source"] == "under_max_lines"
    # A fallback names its reason; absent, the field is not sent
    assert "reason" not in t.payloads("code.outcome")[0]
    await c.code_outcome("fell_back_grep", trace_run_id="tr2", reason="abstained")
    assert t.payloads("code.outcome")[2]["reason"] == "abstained"


async def test_an_open_run_stamps_writes_and_leaves_reads_unfiltered() -> None:
    """A body run_id on a read filters it to the run's own rows, so only writes carry it."""
    c, t = client(
        {
            "run.begin": {"run_id": "r1", "status": "active", "started_at": "x"},
            "memory.set": {"version": 1},
            "memory.search": {"results": []},
            "memory.ask": {"answer": "", "abstained": True},
            "run.complete": {"run_id": "r1", "status": "completed"},
        }
    )
    await c.run_begin("fix CI")
    assert c.run_id == "r1"
    await c.set("k1", "v")
    await c.search("q")
    await c.ask("q")
    assert t.payloads("memory.set")[0]["run_id"] == "r1"
    assert "run_id" not in t.payloads("memory.search")[0]
    assert "run_id" not in t.payloads("memory.ask")[0]
    # The reads are labelled with the run instead, so the ledger counts them.
    by_op = {call[0]: h for call, h in zip(t.calls, t.headers, strict=True)}
    assert by_op["/v1/memory.search"] == {"X-Loom-Run-Id": "r1"}
    assert by_op["/v1/memory.ask"] == {"X-Loom-Run-Id": "r1"}
    assert by_op["/v1/memory.set"] == {}
    r = await c.run_complete(memories_used=["k1"], outcome="positive")
    assert r.ok and c.run_id is None
    p = t.payloads("run.complete")[0]
    assert p["run_id"] == "r1" and p["memories_used"] == ["k1"] and p["outcome"] == "positive"
    r2 = await c.run_complete()
    assert not r2.ok and r2.code == "NO_RUN"


async def test_an_injected_transport_without_extra_headers_still_works() -> None:
    """The label goes only on a recall read under an open run; every other call keeps the
    plain `post_with_meta` signature a caller's own transport may have."""

    class _Plain:
        async def post_with_meta(self, op_path: str, payload: dict[str, Any], *, timeout_s=None):
            return WireResult({"results": []}, 200, "rid", "", None, None, 1)

    c = ReferenceClient("o", "ns", consumer="test", agent="ag", transport=_Plain())
    assert (await c.search("q")).ok
    c.run_id = "r1"  # an open run: the read goes out unlabelled rather than failing
    assert (await c.search("q")).ok


async def test_stats_and_close() -> None:
    c, t = client({"memory.search": {"results": []}})
    async with c:
        await c.search("a")
        await c.search("b")
    assert not t.closed  # an injected transport is the caller's to close
    s = c.stats()
    assert s["calls"] == 2 and s["attempts"] == 2 and s["calls_by_op"] == {"memory.search": 2}
