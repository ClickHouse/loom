"""Every model call names its customer, the org whose data it carries, for a
gateway that meters by it; one that names none is refused, never billed to no
one, and a job working on several orgs' rows makes one call per org."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from server.llm import customer
from server.observability import genai
from server.settings import settings


@pytest.fixture
def metered(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(settings, "inference_customer_header", "X-Customer")
    monkeypatch.setattr(settings, "inference_feature_header", "X-Feature")
    monkeypatch.setattr(settings, "inference_system_customer", "loom")
    genai._identity.set(None)
    yield
    genai._identity.set(None)  # the tests stamp identities a later test must not inherit


def _request(org: str, op: str = "memory.search") -> None:
    genai.set_identity(org=org, namespace="ns", operation=op)


def test_without_a_customer_header_nothing_is_sent_or_refused() -> None:
    genai._identity.set(None)
    assert customer.headers() == {}


def test_a_request_names_the_org_of_its_verified_identity(metered) -> None:
    _request("acme")
    assert customer.headers() == {"X-Customer": "acme", "X-Feature": "memory.search"}
    with genai.llm_stage("hyde"):
        assert customer.headers()["X-Feature"] == "hyde"


@pytest.mark.parametrize("org", ["", "-", "*"])
def test_a_call_naming_no_org_is_refused(metered, org: str) -> None:
    _request(org)
    with pytest.raises(customer.Unattributed):
        customer.headers()


def test_a_worker_names_its_rows_org_and_never_inherits_its_identity(metered) -> None:
    # A pass stamps the org of the row it last worked on; the next call is not that org's.
    genai.set_worker_identity("embedding", org="acme")
    with pytest.raises(customer.Unattributed):
        customer.headers()
    with customer.for_org("beta"):
        assert customer.headers() == {"X-Customer": "beta", "X-Feature": "worker.embedding"}
    with pytest.raises(customer.Unattributed):
        customer.headers()


def test_the_deployments_own_calls_name_the_system_customer(metered) -> None:
    with customer.system():
        assert customer.headers()["X-Customer"] == "loom"
    with customer.system(), customer.for_org("acme"):
        assert customer.headers()["X-Customer"] == "acme"  # an org's data is its own


def test_an_org_that_needs_it_is_quoted_for_the_header(metered) -> None:
    with customer.for_org("org one/ü"):
        assert customer.headers()["X-Customer"] == "org%20one%2F%C3%BC"


class _Recorder:
    def __init__(self, body: dict[str, Any]) -> None:
        self.calls: list[dict[str, Any]] = []
        self.body = body

    async def post(self, url: str, **kw: Any) -> httpx.Response:
        self.calls.append({"url": url, **kw})
        return httpx.Response(200, json=self.body, request=httpx.Request("POST", url))


_CHAT = {
    "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
}
_MESSAGES = {
    "content": [{"type": "text", "text": "hi"}],
    "usage": {"input_tokens": 1, "output_tokens": 1},
    "stop_reason": "end_turn",
}


def test_chat_and_embedding_requests_carry_the_customer(metered, monkeypatch) -> None:
    from server.embedding import compatible as compatible_mod
    from server.embedding import gemini as gemini_mod
    from server.embedding import openai as embed_mod
    from server.llm import anthropic as anthropic_mod
    from server.llm import openai as chat_mod

    _request("acme")
    sent: list[dict[str, str]] = []

    chat = _Recorder(_CHAT)
    monkeypatch.setattr(chat_mod, "_http_client", lambda: chat)
    asyncio.run(chat_mod.OpenAILLMClient(model="gpt-4o-mini", api_key="k").complete("s", "u"))
    sent.append(chat.calls[0]["headers"])

    messages = _Recorder(_MESSAGES)
    monkeypatch.setattr(anthropic_mod, "_http_client", lambda: messages)
    client = anthropic_mod.AnthropicLLMClient(model="claude-haiku-4-5", api_key="k")
    asyncio.run(client.complete("s", "u"))
    sent.append(messages.calls[0]["headers"])

    vectors = _Recorder({"data": [{"embedding": [0.0] * 8}], "usage": {"total_tokens": 1}})
    monkeypatch.setattr(embed_mod, "_http_client", lambda: vectors)
    embedder = embed_mod.OpenAIEmbedder(model="text-embedding-3-small", dim=8, api_key="k")
    asyncio.run(embedder._post(embedder._payload("text")))
    sent.append(vectors.calls[0]["headers"])

    gemini = _Recorder({"embedding": {"values": [0.0] * 8}})
    monkeypatch.setattr(gemini_mod, "_http_client", lambda: gemini)
    asyncio.run(
        gemini_mod.GeminiEmbedder(model="gemini-embedding-001", dim=8, api_key="k")._post(
            "https://gemini.example/embed", {}
        )
    )
    sent.append(gemini.calls[0]["headers"])

    compatible = _Recorder({"data": [{"index": 0, "embedding": [0.5] * 8}]})
    monkeypatch.setattr(compatible_mod, "get_client", lambda: compatible)
    other = compatible_mod.CompatibleEmbedder(
        model="m", dim=8, api_key="k", base_url="https://embed.example/v1"
    )
    asyncio.run(other._request(["text"]))
    sent.append(compatible.calls[0]["headers"])

    assert [h["X-Customer"] for h in sent] == ["acme"] * 5


def test_rerank_and_classify_requests_carry_the_customer(metered, monkeypatch) -> None:
    from server.memory.app import classify as classify_mod
    from server.memory.app import rerank as rerank_mod

    sent: list[dict[str, str]] = []

    class _Client:
        def __init__(self, **_: Any) -> None:
            pass

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

        async def post(self, url: str, **kw: Any) -> httpx.Response:
            sent.append(kw.get("headers") or {})
            return httpx.Response(200, json={}, request=httpx.Request("POST", url))

    monkeypatch.setattr(rerank_mod.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(classify_mod.httpx, "AsyncClient", _Client)
    monkeypatch.setenv("COHERE_API_KEY", "key")
    _request("acme")
    candidates = [rerank_mod.RerankCandidate("k", "ns", "v", "fact", 1.0)]
    asyncio.run(rerank_mod._rerank_with_builtin_cohere("builtin:cohere", "q", candidates))
    asyncio.run(
        classify_mod.classify(
            endpoint="https://classify.example", prompt_template="t", value="v", valid_types=["a"]
        )
    )
    assert [h.get("X-Customer") for h in sent] == ["acme", "acme"]


def test_a_refused_call_sends_nothing(metered, monkeypatch) -> None:
    from server.llm import openai as chat_mod

    genai.set_worker_identity("extraction_queue", org="acme")  # a worker that named no org
    chat = _Recorder(_CHAT)
    monkeypatch.setattr(chat_mod, "_http_client", lambda: chat)
    with pytest.raises(customer.Unattributed):
        asyncio.run(chat_mod.OpenAILLMClient(model="gpt-4o-mini", api_key="k").complete("s", "u"))
    assert chat.calls == []


class _ByOrg:
    """An embedder that records the customer of each call."""

    model_name, dim = "fake", 2

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        self.calls.append((customer.current(), len(texts)))
        return [[float(len(t)), 0.0] for t in texts]


def test_the_embedding_worker_makes_one_call_per_org(metered) -> None:
    from server.memory.workers import embedding

    rows = [
        {"org": "acme", "content_excerpt": "a"},
        {"org": "beta", "content_excerpt": "bb"},
        {"org": "acme", "content_excerpt": "ccc"},
    ]
    emb = _ByOrg()
    out = asyncio.run(embedding._embed_rows(emb, rows))
    assert emb.calls == [("acme", 2), ("beta", 1)]
    assert [v[0] for v, _ in out] == [1.0, 2.0, 3.0]  # in the rows' order


def test_a_reembed_page_spanning_two_orgs_makes_one_call_per_org(metered, monkeypatch) -> None:
    from server.memory.app import reembed

    table = next(t for t, cfg in reembed._TABLES.items() if not cfg.get("key_text"))
    text = reembed._TABLES[table]["text"]
    chunk = [{"org": "acme", text: "a"}, {"org": "beta", text: "bb"}]

    class _CH:
        rows: list[Any] = []

        async def insert(self, table: str, rows: list[Any], **_: Any) -> None:
            self.rows = rows

    emb = _ByOrg()
    ch = _CH()
    monkeypatch.setattr(
        reembed, "rewrite_row", lambda r, cfg, model, dim, vec, kvec: {**r, "vec": vec}
    )
    asyncio.run(reembed._rewrite_page(ch, emb, table, chunk, key_only=False))
    assert emb.calls == [("acme", 1), ("beta", 1)]
    assert [r["vec"][0] for r in ch.rows] == [1.0, 2.0]


def test_a_provider_rebuild_batch_makes_one_call_per_org(metered, monkeypatch) -> None:
    from server.insights.app import provider_rebuild_batch as batch

    monkeypatch.setattr(
        batch,
        "_inputs",
        lambda table, rows, source: (
            [r["text"] for r in rows],
            [(i, "embedding") for i in range(len(rows))],
        ),
    )
    monkeypatch.setattr(batch, "_empty_target_row", lambda table, row, model: dict(row))
    rows = [
        {"org": "acme", "text": "a"},
        {"org": "beta", "text": "bb"},
        {"org": "acme", "text": "c"},
    ]
    emb = _ByOrg()
    out = asyncio.run(batch.rebuild_batch("memory_embeddings", rows, emb))
    assert emb.calls == [("acme", 2), ("beta", 1)]
    assert [r["embedding"][0] for r in out] == [1.0, 2.0, 1.0]


def test_a_cli_command_for_one_org_bills_it(metered, monkeypatch) -> None:
    import argparse

    from server.cli import app

    seen: list[str] = []

    def command(args: argparse.Namespace) -> int:
        seen.append(customer.current())
        return 0

    app._dispatch(argparse.Namespace(cmd="reembed", org="acme", func=command))
    monkeypatch.setattr(settings, "code_org", "code-org")
    app._dispatch(argparse.Namespace(cmd="code", org=None, func=command))  # LOOM_CODE_ORG
    app._dispatch(
        argparse.Namespace(cmd="reembed", func=command)
    )  # every org: each row names its own
    assert seen == ["acme", "code-org", ""]


def test_a_refusal_is_logged_once_a_minute_by_its_code(metered, monkeypatch, caplog) -> None:
    # A fail-open caller may swallow the error: the log still names the missed path.
    monkeypatch.setattr(customer, "_logged_at", {})
    clock = [1000.0]
    monkeypatch.setattr(customer.time, "monotonic", lambda: clock[0])
    for _ in range(3):
        with pytest.raises(customer.Unattributed):
            customer.require()
    clock[0] += 61
    with pytest.raises(customer.Unattributed):
        customer.require()
    logged = [r for r in caplog.records if "MODEL_CALL_UNATTRIBUTED" in r.getMessage()]
    assert len(logged) == 2


def test_a_refusal_comes_before_the_gateway_the_cache_and_provider_health(
    metered, monkeypatch
) -> None:
    from server.embedding import _cache
    from server.embedding import openai as embed_mod
    from server.llm import gateway

    async def no_token(*a: Any) -> str:
        raise AssertionError("asked the gateway for a token")

    monkeypatch.setattr(gateway, "bearer", no_token)
    monkeypatch.setattr(embed_mod, "recording", lambda name: pytest.fail("recorded as an outage"))
    embedder = embed_mod.OpenAIEmbedder(model="text-embedding-3-small", dim=8, api_key="k")
    with pytest.raises(customer.Unattributed):
        asyncio.run(embedder._embed_one_raw("text"))
    # A cached vector does not hide the missed path either.
    _cache._cache.put(_cache._key(embedder.model_name, "cached"), [1.0] * 8)
    with pytest.raises(customer.Unattributed):
        asyncio.run(embedder.embed("cached"))


def test_the_fakes_check_the_customer_as_a_metered_provider_would(metered) -> None:
    from server.embedding.fake import FakeNGramEmbedder
    from server.llm.fake import FakeLLMClient

    with pytest.raises(customer.Unattributed):
        asyncio.run(FakeNGramEmbedder(dim=8).embed("text"))
    with pytest.raises(customer.Unattributed):
        asyncio.run(FakeLLMClient().complete("s", "u"))
    with customer.for_org("acme"):
        asyncio.run(FakeNGramEmbedder(dim=8).embed_many(["text"]))


def test_a_reflection_is_written_for_the_org_of_its_pair(metered) -> None:
    # The contrast call and the reflection's write (which embeds) are both that org's.
    from server.memory.app import session_outcomes as so

    seen: list[tuple[str, str]] = []
    neg = {"org": "acme", "namespace": "ns", "session_id": "s1", "summary_embedding": [0.1]}

    class _CH:
        async def query_all(self, sql: str, params: Any) -> list[Any]:
            return [neg]

        async def query_one(self, sql: str, params: Any) -> Any:
            return {"session_id": "s2", "similarity": 0.99, "summary_text": "ok"}

    class _LLM:
        async def complete(self, *a: Any, **k: Any) -> Any:
            seen.append(("llm", customer.current()))
            return type("R", (), {"text": '{"lessons": ["check the input"], "what_failed": "x"}'})()

    async def writer(**kw: Any) -> None:
        seen.append(("write", customer.current()))

    cfg = so.DistillConfig()
    result = so.DistillResult()
    asyncio.run(so.distil_contrasts(_CH(), _LLM(), cfg, result, writer=writer))
    assert seen == [("llm", "acme"), ("write", "acme")] and result.reflections_written == 1


def test_the_tracker_refit_embeds_for_the_namespaces_org(metered, monkeypatch) -> None:
    from server.tracker.domain import namespaces
    from server.tracker.domain.namespaces import TrackerNamespace
    from server.tracker.workers import links

    seen: list[str] = []

    async def score(*a: Any, **k: Any) -> None:
        seen.append(customer.current())

    monkeypatch.setattr(
        namespaces,
        "configured_namespaces",
        lambda: {"t": TrackerNamespace(org="acme", namespace="t")},
    )
    monkeypatch.setattr(links, "score_namespace", score)
    monkeypatch.setattr(links, "refit_dupcheck", score)
    asyncio.run(links._run(links.TrackerLinksResult(), source_factory=lambda org, ns: None))
    assert seen == ["acme", "acme"]


def test_an_operator_reranker_carries_the_customer_and_refuses_outside_its_fail_open(
    metered, monkeypatch
) -> None:
    from server.memory.app import rerank as rerank_mod

    sent: list[dict[str, str]] = []

    class _Client:
        def __init__(self, **_: Any) -> None:
            pass

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

        async def post(self, url: str, **kw: Any) -> httpx.Response:
            sent.append(kw.get("headers") or {})
            return httpx.Response(200, json={"ranked": []}, request=httpx.Request("POST", url))

    monkeypatch.setattr(rerank_mod.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(
        type(settings), "rerank_endpoint_map", lambda self: {"ops": "https://rerank.example"}
    )
    candidates = [rerank_mod.RerankCandidate("k", "ns", "v", "fact", 1.0)]

    def call() -> Any:
        return asyncio.run(
            rerank_mod.rerank(
                endpoint="ops",
                prompt_template="",
                query="q",
                candidates=candidates,
            )
        )

    with customer.for_org("acme"):
        call()
    assert sent[0]["X-Customer"] == "acme"
    with pytest.raises(customer.Unattributed):  # a missed path is an error, not "kept the order"
        call()
    monkeypatch.setattr(settings, "inference_customer_header", "")
    assert call() == candidates  # unset: nothing to name, and nothing refused


@pytest.mark.parametrize(
    "env, error",
    [
        ({"LOOM_INFERENCE_CUSTOMER_HEADER": "X Customer"}, "HTTP header name"),
        ({"LOOM_INFERENCE_FEATURE_HEADER": "X-Feature:"}, "HTTP header name"),
        (
            {"LOOM_INFERENCE_CUSTOMER_HEADER": "X-Customer", "LOOM_INFERENCE_SYSTEM_CUSTOMER": " "},
            "LOOM_INFERENCE_SYSTEM_CUSTOMER",
        ),
    ],
)
def test_settings_refuse_headers_a_call_could_not_send(monkeypatch, env, error) -> None:
    from server.settings import Settings

    for k, v in env.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(ValueError, match=error):
        Settings()


@pytest.mark.parametrize("client", ["openai", "anthropic"])
def test_a_chat_refusal_comes_before_the_gateway_and_provider_health(
    metered, monkeypatch, client: str
) -> None:
    from server.llm import anthropic as anthropic_mod
    from server.llm import gateway
    from server.llm import openai as chat_mod

    async def no_token(*a: Any) -> str:
        raise AssertionError("asked the gateway for a token")

    monkeypatch.setattr(gateway, "bearer", no_token)
    monkeypatch.setattr(
        "server.observability.provider_health.recording",
        lambda name: pytest.fail("recorded as an outage"),
    )
    llm = (
        chat_mod.OpenAILLMClient(model="gpt-4o-mini", api_key="k")
        if client == "openai"
        else anthropic_mod.AnthropicLLMClient(model="claude-haiku-4-5", api_key="k")
    )
    with pytest.raises(customer.Unattributed):
        asyncio.run(llm.complete("s", "u"))


def test_a_cached_answer_does_not_hide_a_missed_path(metered) -> None:
    from server.llm.cache import CachedLLMClient
    from server.llm.fake import FakeLLMClient

    cached = CachedLLMClient(FakeLLMClient(), org="acme")
    with pytest.raises(customer.Unattributed):
        asyncio.run(cached.complete("s", "u"))


def test_the_llm_reranker_lets_a_refusal_through_and_records_no_outage(
    metered, monkeypatch
) -> None:
    from server.memory.app import rerank as rerank_mod
    from server.observability import provider_health

    class _Metered:
        """A real-looking client: the reranker skips the fake one."""

        async def complete(self, *a: Any, **k: Any) -> Any:
            customer.require()
            raise AssertionError("not reached")

    monkeypatch.setattr("server.llm.client.get_llm_client", lambda: _Metered())
    provider_health._state.clear()
    candidates = [rerank_mod.RerankCandidate("k", "ns", "v", "fact", 1.0)]
    with pytest.raises(customer.Unattributed):
        asyncio.run(rerank_mod._rerank_with_builtin_openai("builtin:openai", "q", candidates))
    assert not provider_health._state.get("rerank:openai", {}).get("failing")


def test_settings_refuse_one_name_for_both_headers(monkeypatch) -> None:
    from server.settings import Settings

    monkeypatch.setenv("LOOM_INFERENCE_CUSTOMER_HEADER", "X-Meter")
    monkeypatch.setenv("LOOM_INFERENCE_FEATURE_HEADER", "x-meter")
    with pytest.raises(ValueError, match="must differ"):
        Settings()
