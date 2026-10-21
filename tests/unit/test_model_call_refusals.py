"""The gateway refusing one identity (a 429 naming a budget, or a 403) is that
identity's refusal: the request answers MODEL_CALL_REFUSED, nothing retries it,
no provider reads degraded, no org is switched off, and a background job leaves
that identity's work for its cooldown while the others' goes on."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from server.auth.context import current_auth_var
from server.auth.jwt import AuthInfo
from server.clients import _http
from server.core.errors import ModelCallRefused
from server.llm import client as llm_client
from server.llm import customer, gateway, identity
from server.observability import genai, provider_health
from server.settings import settings
from tests.unit.clock_fake import ManualClock
from tests.unit.switch_state import switch_off
from tests.unit.test_llm_gateway_bearer import through_the_model_client


def _chat(text: str) -> dict[str, Any]:
    return {
        "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }


class _Gateway:
    """The gateway at gateway.example: refuses agent-a over its budget, agent-c
    outright, rate-limits agent-r, and answers everyone else."""

    def __init__(self) -> None:
        self.agents: list[str] = []

    def answer(self, request: httpx.Request) -> httpx.Response:
        agent = request.headers.get("x-agent", "")
        self.agents.append(agent)
        if agent == "agent-a":
            body = {"error": {"code": "budget_exceeded", "message": "Budget has been exceeded"}}
            return httpx.Response(429, json=body)
        if agent == "agent-c":
            return httpx.Response(403, json={"error": {"code": "agent_forbidden"}})
        if agent == "agent-r":
            return httpx.Response(429, json={"error": {"code": "rate_limit_exceeded"}})
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.5] * 8}]})
        return httpx.Response(200, json=_chat('["alice"]'))


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> ManualClock:
    c = ManualClock(t=1000.0)
    monkeypatch.setattr(gateway.time, "monotonic", c.monotonic)
    return c


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch, clock: ManualClock) -> Iterator[_Gateway]:
    """Identity headers naming the agent, the gateway at gateway.example, the model
    client (its hooks in place) answering as `_Gateway`, retries without waits."""
    gw = _Gateway()
    monkeypatch.setattr(settings, "inference_identity_headers_json", '{"X-Agent": "{agent}"}')
    monkeypatch.setattr(settings, "inference_customer_header", "")
    monkeypatch.setattr(settings, "inference_gateway_hosts", "gateway.example")
    monkeypatch.setattr(settings, "openai_base_url", "https://gateway.example/v1")
    monkeypatch.setattr(settings, "llm_provider", "openai")
    monkeypatch.setattr(settings, "llm_model", "gpt-4o-mini")
    monkeypatch.setattr(settings, "llm_fallback_model", "")
    monkeypatch.setattr(settings, "llm_retry_max_wait_s", 0.0)
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    model_client = through_the_model_client(monkeypatch)
    monkeypatch.setattr(model_client, "_transport", httpx.MockTransport(gw.answer))
    for reset in (gateway._reset_for_tests, identity._reset_for_tests, llm_client.reset):
        reset()
    provider_health.reset()
    switch_off()
    genai._identity.set(None)
    token = current_auth_var.set(None)
    yield gw
    current_auth_var.reset(token)
    genai._identity.set(None)
    asyncio.run(model_client.aclose())
    for reset in (gateway._reset_for_tests, identity._reset_for_tests, llm_client.reset):
        reset()
    provider_health.reset()


def _as(agent: str, namespace: str = "ns") -> None:
    """A request of `agent`'s token (trust `t`) in acme/`namespace`."""
    current_auth_var.set(
        AuthInfo(
            subject=f"sa:t:{agent}",
            org="acme",
            namespaces=(namespace,),
            roles=(),
            is_service_account=True,
        )
    )
    genai.set_identity(org="acme", namespace=namespace, operation="memory.ask")


def _complete() -> Any:
    return asyncio.run(llm_client.get_llm_client().complete("s", "u"))


def test_a_budget_refusal_is_the_requests_error_and_touches_nothing_else(wire) -> None:
    _as("agent-a")
    with pytest.raises(ModelCallRefused) as refused:
        _complete()
    assert refused.value.status_code == 403 and refused.value.code == "MODEL_CALL_REFUSED"
    detail = refused.value.to_detail()
    assert (detail["gateway_status"], detail["gateway_code"]) == (429, "budget_exceeded")
    assert wire.agents == ["agent-a"]  # not retried
    assert provider_health.status() == {"degraded": False, "providers": {}}
    assert asyncio.run(customer.switched_off_orgs()) == frozenset()  # no org is off
    _as("agent-b")  # another agent of the same org goes on
    assert _complete().text == '["alice"]'
    _as("agent-a")
    with pytest.raises(ModelCallRefused):
        _complete()  # a request always asks the gateway: no cooldown on the request path
    assert wire.agents == ["agent-a", "agent-b", "agent-a"]


def test_a_403_is_the_identitys_refusal_too(wire) -> None:
    _as("agent-c")
    with pytest.raises(ModelCallRefused) as refused:
        _complete()
    assert refused.value.to_detail()["gateway_status"] == 403
    assert wire.agents == ["agent-c"]
    assert not provider_health.status()["degraded"]


def test_a_rate_limit_is_still_retried_and_still_the_providers(wire) -> None:
    _as("agent-r")
    with pytest.raises(httpx.HTTPStatusError):
        _complete()
    assert wire.agents == ["agent-r"] * settings.llm_retry_attempts
    assert provider_health.status()["degraded"]


def test_without_identity_headers_a_budget_429_is_what_it_always_was(wire, monkeypatch) -> None:
    monkeypatch.setattr(settings, "inference_identity_headers_json", "")
    monkeypatch.setattr(
        wire, "answer", lambda r: httpx.Response(429, json={"error": {"code": "budget_exceeded"}})
    )
    monkeypatch.setattr(_http.get_model_client(), "_transport", httpx.MockTransport(wire.answer))
    _as("agent-a")
    with pytest.raises(httpx.HTTPStatusError):
        _complete()
    assert provider_health.status()["degraded"]


def test_a_refusal_is_not_handed_to_the_fallback_model(wire) -> None:
    from server.llm.fallback import FallbackLLMClient
    from server.llm.openai import OpenAILLMClient

    class _Secondary:
        provider, model, calls = "openai", "second", 0

        async def complete(self, *a: Any, **k: Any) -> Any:
            _Secondary.calls += 1

    _as("agent-a")
    client = FallbackLLMClient(OpenAILLMClient(model="gpt-4o-mini"), _Secondary())  # type: ignore[arg-type]
    with pytest.raises(ModelCallRefused):
        asyncio.run(client.complete("s", "u"))
    assert _Secondary.calls == 0


def test_an_embedding_refusal_is_the_requests_error_too(wire) -> None:
    from server.embedding.compatible import CompatibleEmbedder

    _as("agent-a")
    embedder = CompatibleEmbedder(
        model="m", dim=8, base_url="https://gateway.example/v1", api_key="k"
    )
    with pytest.raises(ModelCallRefused):
        asyncio.run(embedder.embed_many(["text"]))
    assert wire.agents == ["agent-a"]


def test_a_job_leaves_a_refused_identitys_rows_for_the_cooldown(wire, clock, monkeypatch) -> None:
    from server.memory import store as memory_repo
    from server.memory.workers import entity_extractor as ee
    from tests.unit.test_entity_extractor_parse_failures import _Repo, _row

    rows = [
        {**_row("k1"), "org": "acme", "actor": "process:sa:t:agent-a"},
        {**_row("k2"), "org": "acme", "actor": "process:sa:t:agent-b"},
    ]
    repo = _Repo(rows)
    monkeypatch.setattr(memory_repo, "find_unentitied_memories", repo.find_unentitied_memories)
    monkeypatch.setattr(memory_repo, "apply_entities_batch", repo.apply_entities_batch)

    async def under_cap(pairs: Any) -> dict[Any, bool]:
        return dict.fromkeys(pairs, False)

    monkeypatch.setattr(ee, "namespaces_over_cap", under_cap)
    monkeypatch.setattr(ee, "emit_worker_cost_event_for", lambda **kw: None)

    def run() -> Any:
        repo.applied.clear()
        switch_off()  # the switch read now: the clock moves for it too
        return asyncio.run(ee.run_pass(ee.EntityExtractorConfig()))

    first = run()
    assert [a["memory_key"] for a in repo.applied] == ["k2"]  # agent-b's row goes on
    assert first.skipped_switched_off == 1 and not first.failures  # agent-a's waits, no sentinel
    assert wire.agents == ["agent-a", "agent-b"]
    clock.advance(settings.inference_refusal_cooldown_seconds - 1)
    run()
    assert wire.agents == ["agent-a", "agent-b", "agent-b"]  # agent-a's row: no call at all
    clock.advance(1)
    run()
    assert wire.agents[-2:] == ["agent-a", "agent-b"]  # the cooldown is over: asked again
    assert not provider_health.status()["degraded"]


def test_the_extraction_queue_leaves_a_refused_row_without_spending_an_attempt(
    monkeypatch,
) -> None:
    from server.memory.app import extraction_queue as q
    from server.memory.workers import extraction_queue as worker

    rows = [
        {"org": "acme", "namespace": "ns", "queue_id": "a"},
        {"org": "acme", "namespace": "nb", "queue_id": "b"},
    ]
    done: list[str] = []
    errors: list[str] = []

    async def claim(**_: Any) -> list[dict[str, Any]]:
        return rows

    async def drop(*_: Any, **__: Any) -> int:
        return 0

    async def replay(row: dict[str, Any]) -> int:
        if row["queue_id"] == "a":
            raise ModelCallRefused("refused", gateway_status=429, gateway_code="budget_exceeded")
        return 2

    async def mark_done(row: dict[str, Any], **_: Any) -> None:
        done.append(row["queue_id"])

    async def mark_error(row: dict[str, Any], **_: Any) -> str:
        errors.append(row["queue_id"])
        return "pending"

    async def off() -> frozenset[str]:
        return frozenset()

    monkeypatch.setattr("server.core.ch.get_client", lambda: None)
    monkeypatch.setattr(customer, "switched_off_orgs", off)
    for name, fn in (("claim_batch", claim), ("drop_waiting", drop), ("replay_row", replay)):
        monkeypatch.setattr(q, name, fn)
    monkeypatch.setattr(q, "mark_done", mark_done)
    monkeypatch.setattr(q, "mark_error", mark_error)
    result = asyncio.run(worker.run_pass(worker.ExtractionQueueConfig()))
    assert (result.skipped, done, errors) == (1, ["b"], [])


def test_the_embedding_worker_leaves_a_refused_identitys_rows(wire, monkeypatch) -> None:
    from server.memory.workers import embedding

    monkeypatch.setattr(
        settings, "inference_identity_headers_json", '{"X-Namespace": "{namespace}"}'
    )

    class _Embedder:
        model_name, dim = "fake", 2

        async def embed_many(self, texts: list[str]) -> list[list[float]]:
            sent = customer.headers(await customer.require())
            if sent["X-Namespace"] == "ns-a":
                raise ModelCallRefused("refused", gateway_status=403, gateway_code="")
            return [[1.0, 0.0] for _ in texts]

    rows = [
        {"org": "acme", "namespace": "ns-a", "content_excerpt": "a"},
        {"org": "acme", "namespace": "ns-b", "content_excerpt": "b"},
    ]
    out = asyncio.run(embedding._embed_rows(_Embedder(), rows))
    assert out == [(None, embedding.SWITCHED_OFF), ([1.0, 0.0], "")]  # left, no attempt


def test_a_waiter_on_a_refused_owner_embeds_for_itself(monkeypatch) -> None:
    from server.embedding import _cache

    monkeypatch.setattr(settings, "embedding_cache_shared", False)
    genai.set_identity(org="acme", namespace="ns", operation="memory.search")
    gate = asyncio.Event()

    async def refused(text: str) -> list[float]:
        await gate.wait()
        raise ModelCallRefused("refused", gateway_status=429, gateway_code="budget_exceeded")

    async def own(text: str) -> list[float]:
        return [1.0, 0.0]

    async def run() -> list[float]:
        text = "a query only this test embeds"
        owner = asyncio.create_task(_cache.cached_embed("m-refusal", text, refused, dim=2))
        await asyncio.sleep(0)
        waiter = asyncio.create_task(_cache.cached_embed("m-refusal", text, own, dim=2))
        await asyncio.sleep(0)
        gate.set()
        with pytest.raises(ModelCallRefused):
            await owner
        return (await waiter)[0]

    try:
        assert asyncio.run(run()) == [1.0, 0.0]
    finally:
        genai._identity.set(None)


def test_the_api_answers_a_refusal_with_its_code(wire, monkeypatch) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from pydantic import BaseModel

    from server.operations.base import Operation, Role
    from server.settings.catalog import OP_BUDGETS_P99_MS
    from server.transport import http as http_mod

    monkeypatch.setattr(settings, "inference_identity_headers_json", '{"X-Agent": "{namespace}"}')

    class _In(BaseModel):
        org: str = "-"
        namespace: str = "-"

    class _Out(BaseModel):
        text: str = ""

    async def handler(_: _In) -> _Out:
        return _Out(text=(await llm_client.get_llm_client().complete("s", "u")).text)

    op = Operation(
        name="probe.refusal",
        input_model=_In,
        output_model=_Out,
        handler=handler,
        idempotent=False,
        required_role=Role.VIEWER,
    )
    monkeypatch.setitem(OP_BUDGETS_P99_MS, op.name, 100)
    monkeypatch.setattr(http_mod, "_emit_event", lambda *a, **k: None)
    monkeypatch.setattr(
        type(http_mod.settings), "auth_actually_required", lambda self: False, raising=False
    )
    app = FastAPI()
    app.add_api_route(op.http_path, http_mod._make_endpoint(op), methods=["POST"])
    client = TestClient(app)
    refused = client.post(op.http_path, json={"org": "acme", "namespace": "agent-a"})
    assert refused.status_code == 403, refused.text
    assert refused.json()["detail"]["code"] == "MODEL_CALL_REFUSED"
    assert refused.json()["detail"]["gateway_status"] == 429
    served = client.post(op.http_path, json={"org": "acme", "namespace": "agent-b"})
    assert served.status_code == 200 and served.json()["text"] == '["alice"]'
