"""The gateway refusing one identity is that identity's refusal, not the provider's
failure. Only an answer LOOM_INFERENCE_IDENTITY_REFUSALS_JSON lists counts, matched
exactly by its status and its error body's code, type or message: one under
"refused" answers MODEL_CALL_REFUSED, one under "wait" MODEL_CALL_DEFERRED with the
gateway's Retry-After. Nothing retries either, no provider reads degraded, no org
is switched off, and a background job holds that identity's calls to the same path
and model (the cooldown, or the Retry-After) while other work goes on. Any other
answer, another 403 included, is the provider's, as always."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from server.auth.context import current_auth_var
from server.auth.jwt import AuthInfo
from server.clients import _http
from server.core.errors import ModelCallDeferred, ModelCallRefused
from server.llm import client as llm_client
from server.llm import customer, gateway, identity
from server.observability import genai, provider_health
from server.settings import Settings, settings
from tests.unit.clock_fake import ManualClock
from tests.unit.model_client import through_the_model_client
from tests.unit.switch_state import switch_off

_BUSY = "The agent has too many inference requests in progress. Retry later."


def _chat(text: str) -> dict[str, Any]:
    return {
        "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }


def _error(status: int, kind: str, code: str, message: str, **headers: str) -> httpx.Response:
    """An answer in the inference proxy's error shape, its stable code included."""
    body = {"type": "error", "error": {"type": kind, "code": code, "message": message}}
    return httpx.Response(status, json=body, headers=headers)


# The metering gateway's budget refusal: OpenAI-shaped with a code, and in the Anthropic
# envelope of /v1/messages with none.
_BUDGET = httpx.Response(
    429,
    json={
        "error": {"type": "budget_error", "code": "budget_exceeded", "message": "budget exceeded"}
    },
)
_BUDGET_ANTHROPIC = httpx.Response(
    429, json={"type": "error", "error": {"type": "rate_limit_error", "message": "budget exceeded"}}
)


class _Gateway:
    """The gateway at gateway.example, answering by the agent a call names, as the
    inference proxy and the metering behind it do: agent-a and agent-q are over their
    budget (agent-q in the Anthropic envelope), agent-d denied, agent-n named
    incompletely, agent-l and agent-s have too many requests in progress (Retry-After
    1 and 30), agent-w once; agent-t meets no credential, agent-u no default agent and
    agent-f a caller that may not act for agents (none of them one identity's), agent-x
    and agent-r are rate limited, agent-m is over its budget for gpt-4o-mini only, and
    everyone else, or anyone in `answered`, is answered."""

    def __init__(self) -> None:
        self.agents: list[str] = []
        self.sent: list[httpx.Request] = []
        self.answered: set[str] = set()
        self.slept: list[float] = []

    def answer(self, request: httpx.Request) -> httpx.Response:
        agent = request.headers.get("x-agent", "")
        self.agents.append(agent)
        self.sent.append(request)
        if agent == "agent-w" and self.agents.count(agent) == 1:  # busy once, then answers
            return _error(
                429, "rate_limit_error", "agent_request_limit", _BUSY, **{"Retry-After": "1"}
            )
        refusal = None if agent in self.answered else self._refusal(agent, request)
        if refusal is not None:
            return refusal
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.5] * 8}]})
        if request.url.path.endswith("/rerank"):
            return httpx.Response(200, json={"ranked": [], "results": []})
        if request.url.path.endswith("/classify"):
            return httpx.Response(200, json={"memory_type": "decision"})
        return httpx.Response(200, json=_chat('["alice"]'))

    @staticmethod
    def _refusal(agent: str, request: httpx.Request) -> httpx.Response | None:
        model = json.loads(request.content or b"{}").get("model")
        busy = (429, "rate_limit_error", "agent_request_limit", _BUSY)
        return {
            "agent-a": _BUDGET,
            "agent-q": _BUDGET_ANTHROPIC,
            "agent-d": _error(403, "permission_error", "agent_denied", "The agent is not allowed."),
            "agent-n": _error(400, "invalid_request_error", "identity_incomplete", "Name both."),
            "agent-l": _error(*busy, **{"Retry-After": "1"}),
            "agent-s": _error(*busy, **{"Retry-After": "30"}),
            "agent-t": _error(503, "api_error", "token_unavailable", "Retry later."),
            "agent-u": _error(400, "invalid_request_error", "identity_missing", "No agent."),
            "agent-f": _error(403, "permission_error", "caller_forbidden", "Not for agents."),
            "agent-x": httpx.Response(429, json={"error": {"code": "budget_exceeded_soon"}}),
            "agent-r": httpx.Response(429, json={"error": {"code": "rate_limit_exceeded"}}),
            "agent-m": _BUDGET if model == "gpt-4o-mini" else None,
        }.get(agent)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> ManualClock:
    c = ManualClock(t=1000.0)
    monkeypatch.setattr(gateway.time, "monotonic", c.monotonic)
    return c


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch, clock: ManualClock) -> Iterator[_Gateway]:
    """Identity headers naming the agent, the gateway at gateway.example, the built-in
    refusals table, the model client (its hooks in place) answering as `_Gateway`,
    retries without waits."""
    gw = _Gateway()
    monkeypatch.setattr(settings, "inference_identity_headers_json", '{"X-Agent": "{agent}"}')
    # the built-in refusals table: the default matches the gateway's answers above
    monkeypatch.setattr(
        settings,
        "inference_identity_refusals_json",
        Settings.model_fields["inference_identity_refusals_json"].default,
    )
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

    async def slept(seconds: float) -> None:
        gw.slept.append(seconds)

    monkeypatch.setattr(gateway, "_sleep", slept)
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


def _complete(model: str = "") -> Any:
    client = llm_client.get_llm_client_for_model(model) if model else llm_client.get_llm_client()
    return asyncio.run(client.complete("s", "u"))


def _job(agent: str, model: str = "") -> Any:
    """A background job's chat call for a row `agent` wrote."""
    switch_off()  # the switch read now: the clock moves for it too
    with customer.for_org("acme", namespace="ns", subject=f"sa:t:{agent}"):
        return _complete(model)


def _embed_for_job(agent: str) -> Any:
    from server.embedding.compatible import CompatibleEmbedder

    embedder = CompatibleEmbedder(
        model="m", dim=8, base_url="https://gateway.example/v1", api_key="k"
    )
    with customer.for_org("acme", namespace="ns", subject=f"sa:t:{agent}"):
        return asyncio.run(embedder.embed_many(["text"]))


def test_a_budget_refusal_is_the_requests_error_and_touches_nothing_else(wire) -> None:
    _as("agent-a")
    with pytest.raises(ModelCallRefused) as refused:
        _complete()
    assert refused.value.status_code == 403 and refused.value.code == "MODEL_CALL_REFUSED"
    detail = refused.value.to_detail()
    assert (detail["gateway_status"], detail["gateway_code"]) == (429, "budget_exceeded")
    # Extraction rows and agent events store the message: the gateway's own text stays out.
    assert detail["message"] == "the inference gateway refused this call (429 budget_exceeded)"
    assert wire.agents == ["agent-a"]  # not retried
    assert provider_health.status() == {"degraded": False, "providers": {}}
    assert asyncio.run(customer.switched_off_orgs()) == frozenset()  # no org is off
    _as("agent-b")  # another agent of the same org goes on
    assert _complete().text == '["alice"]'
    _as("agent-a")
    with pytest.raises(ModelCallRefused):
        _complete()  # a request always asks the gateway: no hold on the request path
    assert wire.agents == ["agent-a", "agent-b", "agent-a"]


@pytest.mark.parametrize(
    ("agent", "status", "code"),
    [
        ("agent-d", 403, "agent_denied"),
        ("agent-n", 400, "identity_incomplete"),
        ("agent-q", 429, "rate_limit_error"),  # the Anthropic envelope: by type and message
    ],
)
def test_each_listed_answer_is_the_identitys_refusal(wire, agent, status, code) -> None:
    _as(agent)
    with pytest.raises(ModelCallRefused) as refused:
        _complete()
    assert type(refused.value) is ModelCallRefused
    assert (
        refused.value.to_detail()["gateway_status"],
        refused.value.to_detail()["gateway_code"],
    ) == (
        status,
        code,
    )
    assert wire.agents == [agent]
    assert not provider_health.status()["degraded"]


@pytest.mark.parametrize(
    ("agent", "status", "asked"),
    [
        ("agent-f", 403, 1),  # the gateway refuses Loom itself, for every agent alike
        ("agent-u", 400, 1),  # no default agent: the gateway's configuration
        ("agent-t", 503, 4),  # no credential: also what a credential outage answers
    ],
)
def test_an_answer_the_table_leaves_out_is_the_providers_failure(
    wire, agent, status, asked
) -> None:
    _as(agent)
    with pytest.raises(httpx.HTTPStatusError) as failed:
        _complete()
    assert failed.value.response.status_code == status
    assert wire.agents == [agent] * asked  # retried only as that status always was
    assert provider_health.status()["degraded"]


def test_an_answer_is_matched_exactly_never_by_a_word_in_it(wire) -> None:
    _as("agent-x")  # 429 budget_exceeded_soon: not budget_exceeded
    with pytest.raises(httpx.HTTPStatusError):
        _complete()
    assert wire.agents == ["agent-x"] * settings.llm_retry_attempts  # a rate limit: retried
    assert provider_health.status()["degraded"]


@pytest.mark.parametrize(
    ("agent", "status", "wait", "asked"),
    [("agent-l", 429, 1, 2), ("agent-s", 429, 30, 1)],
)
def test_a_wait_answer_is_deferred_for_its_retry_after(wire, agent, status, wait, asked) -> None:
    _as(agent)
    with pytest.raises(ModelCallDeferred) as deferred:
        _complete()
    assert deferred.value.status_code == 503 and deferred.value.code == "MODEL_CALL_DEFERRED"
    detail = deferred.value.to_detail()
    assert (detail["gateway_status"], detail["retry_after_s"]) == (status, wait)
    wait_message = "the inference gateway asked this call to wait (429 agent_request_limit)"
    assert detail["message"] == wait_message
    assert deferred.value.headers() == {"Retry-After": str(wait)}
    # a wait of two seconds or less is waited out once here, then deferred; longer, at once
    assert wire.agents == [agent] * asked and wire.slept == [1.0] * (asked - 1)
    assert not provider_health.status()["degraded"]


def test_a_short_wait_on_a_request_is_waited_out_once_and_asked_again(wire) -> None:
    _as("agent-w")  # too many requests in progress once, then answered
    assert _complete().text == '["alice"]'
    assert wire.agents == ["agent-w", "agent-w"] and wire.slept == [1.0]
    assert not provider_health.status()["degraded"]  # the call ended answered


def test_a_customers_hold_does_not_block_another_customer(wire, monkeypatch) -> None:
    monkeypatch.setattr(settings, "inference_customer_header", "X-Customer")
    monkeypatch.setattr(
        settings, "inference_identity_headers_json", '{"X-Namespace": "{namespace}"}'
    )
    sent: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(request.headers["X-Customer"])
        return _BUDGET if sent[-1] == "first" else httpx.Response(200, json=_chat("ok"))

    monkeypatch.setattr(_http.get_model_client(), "_transport", httpx.MockTransport(answer))
    with customer.for_org("first", namespace="shared"), pytest.raises(ModelCallRefused):
        _complete()
    with customer.for_org("second", namespace="shared"):
        assert _complete().text == "ok"
    assert sent == ["first", "second"]


@pytest.mark.parametrize("preceding_errors", [0, 3])
def test_a_short_wait_is_deferred_even_after_chat_retries_are_spent(
    wire, monkeypatch, preceding_errors: int
) -> None:
    monkeypatch.setattr(settings, "llm_retry_attempts", preceding_errors + 1)
    sent: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if len(sent) <= preceding_errors:
            return httpx.Response(503)
        return _error(429, "rate_limit_error", "agent_request_limit", _BUSY, **{"Retry-After": "0"})

    monkeypatch.setattr(_http.get_model_client(), "_transport", httpx.MockTransport(answer))
    _as("agent-l")
    with pytest.raises(ModelCallDeferred):
        _complete()
    assert len(sent) == preceding_errors + 2
    assert not provider_health.status()["degraded"]


@pytest.mark.parametrize("sender", ["classify", "rerank"])
def test_operator_endpoints_defer_a_short_identity_wait(wire, monkeypatch, sender: str) -> None:
    from server.memory.app import classify, rerank

    monkeypatch.setattr(
        settings, "rerank_endpoints_json", '{"gw": "https://gateway.example/rerank"}'
    )
    _as("agent-l")
    with pytest.raises(ModelCallDeferred):
        if sender == "classify":
            asyncio.run(
                classify.classify(
                    org="acme",
                    namespace="ns",
                    endpoint="https://gateway.example/classify",
                    prompt_template="",
                    value="v",
                    valid_types=[],
                )
            )
        else:
            asyncio.run(
                rerank.rerank(
                    endpoint="gw",
                    prompt_template="",
                    query="q",
                    candidates=[rerank.RerankCandidate("k", "ns", "v", "fact", 1.0)],
                )
            )
    assert wire.agents == ["agent-l", "agent-l"]


def test_a_failed_email_lookup_prevents_the_model_call(wire, monkeypatch) -> None:
    from server.auth import aliases
    from server.llm.identity import IdentityUnread

    async def unavailable(*args: Any, **kwargs: Any) -> list[str]:
        raise ConnectionError("directory unavailable")

    monkeypatch.setattr(aliases, "read_aliases", unavailable)
    monkeypatch.setattr(settings, "inference_identity_headers_json", '{"X-Actor": "{email}"}')
    current_auth_var.set(
        AuthInfo(
            subject="person-id", org="acme", namespaces=("ns",), roles=(), is_service_account=False
        )
    )
    genai.set_identity(org="acme", namespace="ns", operation="memory.ask")
    with pytest.raises(IdentityUnread):
        _complete()
    assert wire.sent == []


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


def test_a_jobs_hold_is_for_one_path_and_model(wire) -> None:
    with pytest.raises(ModelCallRefused) as refused:
        _job("agent-m")  # over its budget for gpt-4o-mini
    assert refused.value.hold_s == settings.inference_refusal_cooldown_seconds
    sent = len(wire.sent)
    with pytest.raises(ModelCallRefused):
        _job("agent-m")  # held: nothing sent
    assert len(wire.sent) == sent
    assert _job("agent-m", "gpt-4.1-mini").text == '["alice"]'  # another model goes on
    assert _embed_for_job("agent-m") == [[0.5] * 8]  # and so do its embeddings
    assert [(r.url.path, json.loads(r.content)["model"]) for r in wire.sent] == [
        ("/v1/chat/completions", "gpt-4o-mini"),
        ("/v1/chat/completions", "gpt-4.1-mini"),
        ("/v1/embeddings", "m"),
    ]


def test_a_success_there_ends_a_jobs_hold(wire) -> None:
    with pytest.raises(ModelCallRefused):
        _job("agent-d")
    wire.answered.add("agent-d")  # its budget raised, say
    with pytest.raises(ModelCallRefused):
        _job("agent-d")  # still held: nothing sent
    assert wire.agents == ["agent-d"]
    _as("agent-d")
    assert _complete().text == '["alice"]'  # a request asks the gateway, and is answered
    genai._identity.set(None)
    current_auth_var.set(None)
    assert _job("agent-d").text == '["alice"]'  # the hold is over
    assert wire.agents == ["agent-d"] * 3


def test_a_jobs_wait_lasts_the_retry_after(wire, clock) -> None:
    with pytest.raises(ModelCallDeferred) as deferred:
        _job("agent-l")
    assert deferred.value.hold_s == 1.0
    with pytest.raises(ModelCallDeferred) as held:
        _job("agent-l")
    assert held.value.to_detail()["retry_after_s"] == 1
    assert wire.agents == ["agent-l"]
    clock.advance(1)
    with pytest.raises(ModelCallDeferred):
        _job("agent-l")  # asked again
    assert wire.agents == ["agent-l", "agent-l"]


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
            raise ModelCallRefused(
                "refused", hold_s=600.0, gateway_status=429, gateway_code="budget_exceeded"
            )
        return 2

    async def mark_done(row: dict[str, Any], **_: Any) -> None:
        done.append(row["queue_id"])

    async def mark_error(row: dict[str, Any], **_: Any) -> str:
        errors.append(row["queue_id"])
        return "pending"

    async def mark_held(row: dict[str, Any], *, reason: str, seconds: float, **_: Any) -> None:
        held.append((row["queue_id"], reason.split(":")[0], seconds))

    async def off() -> frozenset[str]:
        return frozenset()

    held: list[tuple[str, str, float]] = []
    monkeypatch.setattr("server.core.ch.get_client", lambda: None)
    monkeypatch.setattr(customer, "switched_off_orgs", off)
    for name, fn in (("claim_batch", claim), ("drop_waiting", drop), ("replay_row", replay)):
        monkeypatch.setattr(q, name, fn)
    monkeypatch.setattr(q, "drop_expiring", drop)
    monkeypatch.setattr(q, "mark_done", mark_done)
    monkeypatch.setattr(q, "mark_error", mark_error)
    monkeypatch.setattr(q, "mark_held", mark_held)
    result = asyncio.run(worker.run_pass(worker.ExtractionQueueConfig()))
    assert (result.skipped, done, errors) == (1, ["b"], [])
    assert held == [("a", "MODEL_CALL_REFUSED", 600.0)]  # held for its cooldown, not `running`


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
    assert out == [(None, embedding.HELD), ([1.0, 0.0], "")]  # left, no attempt


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


def test_identity_headers_go_only_to_the_gateways_hosts(wire, monkeypatch) -> None:
    from server.memory.app import classify, rerank

    monkeypatch.setattr(
        settings,
        "rerank_endpoints_json",
        '{"gw": "https://gateway.example/rerank", "ops": "https://rerank.example/rerank"}',
    )
    monkeypatch.setenv("COHERE_API_KEY", "c-key")
    _as("agent-b")
    candidates = [rerank.RerankCandidate("k", "ns", "v", "fact", 1.0)]
    for endpoint in ("gw", "ops", "builtin:cohere"):
        asyncio.run(
            rerank.rerank(endpoint=endpoint, prompt_template="", query="q", candidates=candidates)
        )
    # Only a classifier at a gateway host is a model call; one elsewhere is posted on its
    # own client with no identity headers (tests/unit/test_classify_endpoint_off_gateway.py).
    asyncio.run(
        classify.classify(
            org="acme",
            namespace="ns",
            endpoint="https://gateway.example/classify",
            prompt_template="",
            value="v",
            valid_types=[],
        )
    )
    assert [(r.url.host, r.headers.get("x-agent")) for r in wire.sent] == [
        ("gateway.example", "agent-b"),
        ("rerank.example", None),
        ("api.cohere.com", None),
        ("gateway.example", "agent-b"),
    ]


def test_a_reranker_or_classifier_refusal_is_an_error_not_a_kept_order(wire, monkeypatch) -> None:
    from server.memory.app import classify, rerank

    monkeypatch.setattr(
        settings, "rerank_endpoints_json", '{"gw": "https://gateway.example/rerank"}'
    )
    _as("agent-a")
    candidates = [rerank.RerankCandidate("k", "ns", "v", "fact", 1.0)]
    with pytest.raises(ModelCallRefused):
        asyncio.run(
            rerank.rerank(endpoint="gw", prompt_template="", query="q", candidates=candidates)
        )
    with pytest.raises(ModelCallRefused):
        asyncio.run(
            classify.classify(
                org="acme",
                namespace="ns",
                endpoint="https://gateway.example/classify",
                prompt_template="",
                value="v",
                valid_types=[],
            )
        )
    _as("agent-f")  # the provider's failure: the input order and the caller's type stay
    assert (
        asyncio.run(
            rerank.rerank(endpoint="gw", prompt_template="", query="q", candidates=candidates)
        )
        == candidates
    )


def test_the_api_answers_a_refusal_and_a_wait_with_their_codes(wire, monkeypatch) -> None:
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
    waiting = client.post(op.http_path, json={"org": "acme", "namespace": "agent-s"})
    assert waiting.status_code == 503, waiting.text
    assert waiting.json()["detail"]["code"] == "MODEL_CALL_DEFERRED"
    assert waiting.headers["Retry-After"] == "30"
    served = client.post(op.http_path, json={"org": "acme", "namespace": "agent-b"})
    assert served.status_code == 200 and served.json()["text"] == '["alice"]'


@pytest.mark.parametrize(
    "table",
    [
        '{"blocked": []}',  # a kind that is neither "refused" nor "wait"
        '{"refused": {"status": 429, "code": "x"}}',  # not a list
        '{"refused": [{"status": 429}]}',  # nothing to match but the status
        '{"refused": [{"status": "429", "code": "x"}]}',
        '{"refused": [{"status": 200, "code": "x"}]}',  # not an error status
        '{"refused": [{"status": 429, "code": ""}]}',
        '{"wait": [{"status": 503, "detail": "x"}]}',  # a field the match does not read
    ],
)
def test_a_refusals_table_that_does_not_parse_is_refused_at_load(table: str) -> None:
    with pytest.raises(ValidationError, match="LOOM_INFERENCE_IDENTITY_REFUSALS_JSON"):
        Settings(_env_file=None, inference_identity_refusals_json=table)  # type: ignore[call-arg]


def test_the_default_table_matches_by_code_and_the_codeless_budget_envelope() -> None:
    assert Settings(_env_file=None).identity_refusals() == {  # type: ignore[call-arg]
        "refused": (
            {"status": 429, "code": "budget_exceeded"},
            {"status": 429, "type": "rate_limit_error", "message": "budget exceeded"},
            {"status": 403, "code": "agent_denied"},
            {"status": 400, "code": "identity_incomplete"},
            {"status": 400, "code": "identity_malformed"},
        ),
        "wait": ({"status": 429, "code": "agent_request_limit"},),
    }


def test_identity_headers_need_the_gateways_hosts() -> None:
    with pytest.raises(ValidationError, match="LOOM_INFERENCE_GATEWAY_HOSTS"):
        Settings(_env_file=None, inference_identity_headers_json='{"X-Agent": "{agent}"}')  # type: ignore[call-arg]


@pytest.mark.parametrize("wait", [False, True])
def test_redirects_preserve_the_model_and_permit_one_short_retry(wire, monkeypatch, wait):
    import json

    sent = []

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            body = b"".join([part async for part in request.stream])
            model = json.loads(body)["model"]
            sent.append((request.url.path, model))
            if not request.url.path.endswith("/"):
                return httpx.Response(307, headers={"Location": request.url.path + "/"})
            if model == "model-a":
                if wait and sum(path.endswith("/") for path, _ in sent) > 1:
                    return httpx.Response(200, json=_chat("ok"))
                if wait:
                    return _error(
                        429,
                        "rate_limit_error",
                        "agent_request_limit",
                        _BUSY,
                        **{"Retry-After": "0"},
                    )
                return _BUDGET
            return httpx.Response(200, json=_chat("ok"))

    monkeypatch.setattr(_http.get_model_client(), "_transport", Transport())
    _as("agent-a")
    if wait:
        assert _complete("model-a").text == "ok"
        assert wire.slept == [0.0]
        assert len(sent) == 4
    else:
        with pytest.raises(ModelCallRefused):
            _complete("model-a")
        assert _complete("model-b").text == "ok"
        assert sent[-1] == ("/v1/chat/completions/", "model-b")
