"""Every model call names its customer, the org whose data it carries, for a
gateway that meters by it; one that names none is refused, never billed to no
one, and a job working on several orgs' rows makes one call per org."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from server.llm import customer
from server.observability import genai
from server.settings import settings
from tests.unit.model_client import through_the_model_client
from tests.unit.test_conflict_resolver import row_env  # noqa: F401


@pytest.fixture
def metered(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(settings, "inference_customer_header", "X-Customer")
    monkeypatch.setattr(settings, "inference_feature_header", "X-Feature")
    monkeypatch.setattr(settings, "inference_system_customer", "loom")
    monkeypatch.setattr(customer, "_logged_at", {})  # each test sees its own refusals logged
    genai._identity.set(None)
    yield
    genai._identity.set(None)  # the tests stamp identities a later test must not inherit


def _headers() -> dict[str, str]:
    return customer.headers(asyncio.run(customer.require()))


def _request(org: str, op: str = "memory.search") -> None:
    genai.set_identity(org=org, namespace="ns", operation=op)


def test_without_a_customer_header_nothing_is_sent_or_refused() -> None:
    genai._identity.set(None)
    assert _headers() == {}


def test_a_request_names_the_org_of_its_verified_identity(metered) -> None:
    _request("acme")
    assert _headers() == {"X-Customer": "acme", "X-Feature": "memory.search"}
    with genai.llm_stage("hyde"):
        assert _headers()["X-Feature"] == "hyde"


@pytest.mark.parametrize("org", ["", "-", "*"])
def test_a_call_naming_no_org_is_refused(metered, org: str) -> None:
    _request(org)
    with pytest.raises(customer.Unattributed):
        _headers()


def test_a_worker_names_its_rows_org_and_never_inherits_its_identity(metered) -> None:
    # A pass stamps the org of the row it last worked on; the next call is not that org's.
    genai.set_worker_identity("embedding", org="acme")
    with pytest.raises(customer.Unattributed):
        _headers()
    with customer.for_org("beta"):
        assert _headers() == {"X-Customer": "beta", "X-Feature": "worker.embedding"}
    with pytest.raises(customer.Unattributed):
        _headers()


def test_the_deployments_own_calls_name_the_system_customer(metered) -> None:
    with customer.system():
        assert _headers()["X-Customer"] == "loom"
    with customer.system(), customer.for_org("acme"):
        assert _headers()["X-Customer"] == "acme"  # an org's data is its own


def test_an_org_that_needs_it_is_quoted_for_the_header(metered) -> None:
    with customer.for_org("org one/ü"):
        assert _headers()["X-Customer"] == "org%20one%2F%C3%BC"


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
    asyncio.run(embedder._embed_one_raw("text"))
    sent.append(vectors.calls[0]["headers"])

    gemini = _Recorder({"embedding": {"values": [0.0] * 8}})
    monkeypatch.setattr(gemini_mod, "_http_client", lambda: gemini)
    asyncio.run(
        gemini_mod.GeminiEmbedder(model="gemini-embedding-001", dim=8, api_key="k")._embed_one_raw(
            "text"
        )
    )
    sent.append(gemini.calls[0]["headers"])

    compatible = _Recorder({"data": [{"index": 0, "embedding": [0.5] * 8}]})
    monkeypatch.setattr(compatible_mod, "get_model_client", lambda: compatible)
    other = compatible_mod.CompatibleEmbedder(
        model="m", dim=8, api_key="k", base_url="https://embed.example/v1"
    )
    asyncio.run(other._request(["text"]))
    sent.append(compatible.calls[0]["headers"])

    assert [h["X-Customer"] for h in sent] == ["acme"] * 5


def _sent_headers(monkeypatch: pytest.MonkeyPatch, answer: dict[str, Any]) -> list[Any]:
    """The headers of each request the model client sends, every one answered `answer`."""
    sent: list[Any] = []

    def wire(request: httpx.Request) -> httpx.Response:
        sent.append(request.headers)
        return httpx.Response(200, json=answer)

    client = through_the_model_client(monkeypatch)
    monkeypatch.setattr(client, "_transport", httpx.MockTransport(wire))
    return sent


def test_rerank_and_classify_requests_carry_the_customer(metered, monkeypatch) -> None:
    from server.memory.app import classify as classify_mod
    from server.memory.app import rerank as rerank_mod

    sent = _sent_headers(monkeypatch, {})
    monkeypatch.setenv("COHERE_API_KEY", "key")
    # A classifier at a listed gateway host is a model call; any other is posted apart.
    monkeypatch.setattr(settings, "inference_gateway_hosts", "classify.example")
    _request("acme")
    candidates = [rerank_mod.RerankCandidate("k", "ns", "v", "fact", 1.0)]
    asyncio.run(rerank_mod._rerank_with_builtin_cohere("builtin:cohere", "q", candidates))
    asyncio.run(
        classify_mod.classify(
            org="acme",
            namespace="ns",
            endpoint="https://classify.example",
            prompt_template="t",
            value="v",
            valid_types=["a"],
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


def test_with_no_customer_header_a_mixed_org_pass_is_one_call(monkeypatch) -> None:
    from server.memory.workers import embedding

    monkeypatch.setattr(settings, "inference_customer_header", "")
    genai._identity.set(None)
    rows = [
        {"org": "acme", "content_excerpt": "a"},
        {"org": "beta", "content_excerpt": "bb"},
    ]
    emb = _ByOrg()
    out = asyncio.run(embedding._embed_rows(emb, rows))
    assert emb.calls == [("", 2)]
    assert [v[0] for v, _ in out] == [1.0, 2.0]


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
        lambda table, rows, source, held: (
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
    out = asyncio.run(batch.rebuild_batch("memory_embeddings", rows, emb)).rows
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
            asyncio.run(customer.require())
    clock[0] += 61
    with pytest.raises(customer.Unattributed):
        asyncio.run(customer.require())
    logged = [r for r in caplog.records if "MODEL_CALL_UNATTRIBUTED" in r.getMessage()]
    assert len(logged) == 2


def test_a_refusal_comes_before_the_gateway_and_provider_health(metered, monkeypatch) -> None:
    from server.embedding import openai as embed_mod
    from server.llm import gateway

    async def no_token(*a: Any) -> str:
        raise AssertionError("asked the gateway for a token")

    monkeypatch.setattr(gateway, "bearer", no_token)
    monkeypatch.setattr(embed_mod, "recording", lambda name: pytest.fail("recorded as an outage"))
    embedder = embed_mod.OpenAIEmbedder(model="text-embedding-3-small", dim=8, api_key="k")
    with pytest.raises(customer.Unattributed):
        asyncio.run(embedder._embed_one_raw("text"))
    # A call naming no customer has no request org either, so it reads no cache
    # (test_model_cache_per_org) and reaches the refusal above.


def test_a_refused_call_gets_no_cached_vector(monkeypatch) -> None:
    # The one refusal a request org can carry here: required set after load
    # without its header.
    from server.embedding import _cache
    from server.embedding import openai as embed_mod

    monkeypatch.setattr(settings, "inference_customer_header", "")
    monkeypatch.setattr(settings, "inference_customer_required", True)
    monkeypatch.setattr(_cache, "_cache", _cache._LRU(max_size=8, ttl_s=60.0))
    embedder = embed_mod.OpenAIEmbedder(model="text-embedding-3-small", dim=8, api_key="k")
    _request("acme")
    try:
        _cache._cache.put(_cache._key(embedder.model_name, "cached", "acme"), [1.0] * 8)
        with pytest.raises(customer.Unattributed, match="REQUIRED"):
            asyncio.run(embedder.embed("cached"))
    finally:
        genai._identity.set(None)


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

    sent = _sent_headers(monkeypatch, {"ranked": []})
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
        (
            {
                "LOOM_INFERENCE_CUSTOMER_HEADER": "X-Customer",
                "LOOM_INFERENCE_SYSTEM_CUSTOMER": "loom deployment",
            },
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
    from server.llm import cache as cache_mod
    from server.llm.cache import CachedLLMClient

    class _Hit:
        async def query_one(self, sql: str, params: Any) -> Any:
            return {"text": "cached", "tokens_in": 1, "tokens_out": 1}

    class _Provider:
        model = "m"
        provider = "fake"

        async def complete(self, *a: Any, **k: Any) -> Any:
            raise AssertionError("called the provider")

    cached = CachedLLMClient(_Provider(), org="acme", ch=_Hit())
    with pytest.raises(customer.Unattributed):  # before the hit, not only on a miss
        asyncio.run(cached.complete("s", "u"))
    with customer.for_org("acme"):
        assert asyncio.run(cached.complete("s", "u")).text == "cached"
    cache_mod.reset_stats()  # the hit counts in the process-wide stats other tests read


@pytest.mark.parametrize("held", ["unattributed", "stale"])
def test_code_search_rerank_lets_a_refusal_through(metered, monkeypatch, held: str) -> None:
    from server.code.app.search import rerank as code_rerank
    from server.memory.app import rerank as rerank_mod

    async def refusing(**kw: Any) -> Any:
        if held == "stale":
            raise customer.SwitchUnread("stale")
        await customer.require()
        raise AssertionError("not reached")

    error = customer.SwitchUnread if held == "stale" else customer.Unattributed
    monkeypatch.setattr(rerank_mod, "rerank", refusing)
    with pytest.raises(error):  # not "fused order kept"
        asyncio.run(code_rerank._rerank_units("ops", "q", []))


def test_inline_classify_lets_an_unread_switch_through(metered, monkeypatch) -> None:
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app import classify as classify_mod
    from server.memory.app import namespace_types, write

    async def config(org: str, ns: str) -> tuple[str, str]:
        return "https://classify.example", "t"

    async def types(org: str, ns: str) -> list[Any]:
        return [{"memory_type": "observation"}]

    async def stale(**_: Any) -> str:
        raise customer.SwitchUnread("stale")

    monkeypatch.setattr(classify_mod, "get_namespace_classify_config", config)
    monkeypatch.setattr(namespace_types, "list_type_configs", types)
    monkeypatch.setattr(classify_mod, "classify", stale)
    body = MemorySetInput(
        org="acme", namespace="ns", agent="a", memory_key="k", value="v", classify=True
    )
    with customer.for_org("acme"), pytest.raises(customer.SwitchUnread):  # not the caller's type
        asyncio.run(write._set_resolve_type(body))


def test_inline_classify_lets_a_refusal_through_and_is_untouched_without_a_header(
    metered, monkeypatch
) -> None:
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app import classify as classify_mod
    from server.memory.app import namespace_types, write

    async def config(org: str, ns: str) -> tuple[str, str]:
        return "https://classify.example", "t"

    async def types(org: str, ns: str) -> list[Any]:
        return [{"memory_type": "observation"}]

    sent = _sent_headers(monkeypatch, {"memory_type": "observation"})
    monkeypatch.setattr(settings, "inference_gateway_hosts", "classify.example")
    monkeypatch.setattr(classify_mod, "get_namespace_classify_config", config)
    monkeypatch.setattr(namespace_types, "list_type_configs", types)
    body = MemorySetInput(
        org="acme",
        namespace="ns",
        agent="a",
        memory_key="k",
        value="v",
        memory_type="semantic",
        classify=True,
    )
    with pytest.raises(customer.Unattributed):  # a worker path that named no org
        asyncio.run(write._set_resolve_type(body))
    monkeypatch.setattr(settings, "inference_customer_header", "")
    resolved, _, _ = asyncio.run(write._set_resolve_type(body))
    # Unset: the classifier is asked once, with no metering header, and its type applies.
    assert resolved.memory_type == "observation" and len(sent) == 1
    assert "x-customer" not in sent[0]
    from server.observability import provider_health

    provider_health.reset()  # classify recorded a success in the process-wide state


@pytest.mark.parametrize("held", ["unattributed", "stale"])
def test_the_llm_reranker_lets_a_refusal_through_and_records_no_outage(
    metered, monkeypatch, held: str
) -> None:
    from server.memory.app import rerank as rerank_mod
    from server.observability import provider_health

    class _Metered:
        """A real-looking client: the reranker skips the fake one."""

        async def complete(self, *a: Any, **k: Any) -> Any:
            if held == "stale":
                raise customer.SwitchUnread("stale")
            await customer.require()
            raise AssertionError("not reached")

    error = customer.SwitchUnread if held == "stale" else customer.Unattributed
    monkeypatch.setattr("server.llm.client.get_llm_client", lambda: _Metered())
    provider_health.reset()
    candidates = [rerank_mod.RerankCandidate("k", "ns", "v", "fact", 1.0)]
    with pytest.raises(error):
        asyncio.run(rerank_mod._rerank_with_builtin_openai("builtin:openai", "q", candidates))
    assert not provider_health.status()["providers"].get("rerank:openai", {}).get("failing")


def test_settings_refuse_one_name_for_both_headers(monkeypatch) -> None:
    from server.settings import Settings

    monkeypatch.setenv("LOOM_INFERENCE_CUSTOMER_HEADER", "X-Meter")
    monkeypatch.setenv("LOOM_INFERENCE_FEATURE_HEADER", "x-meter")
    with pytest.raises(ValueError, match="must differ"):
        Settings()


def test_the_gateway_names_the_customer_and_feature_its_own_way(metered, monkeypatch) -> None:
    monkeypatch.setattr(settings, "inference_customer_format", "cloud:{org}")
    monkeypatch.setattr(
        settings, "inference_feature_map_json", '{"hyde": "search-expand", "*": "loom-other"}'
    )
    _request("acme")
    with genai.llm_stage("hyde"):
        assert _headers() == {"X-Customer": "cloud:acme", "X-Feature": "search-expand"}
    assert _headers()["X-Feature"] == "loom-other"  # memory.search is not mapped


@pytest.mark.parametrize(
    "env, error",
    [
        ({"LOOM_INFERENCE_CUSTOMER_REQUIRED": "true"}, "needs LOOM_INFERENCE_CUSTOMER_HEADER"),
        ({"LOOM_INFERENCE_CUSTOMER_FORMAT": "cloud"}, "must contain"),
        ({"LOOM_INFERENCE_FEATURE_MAP_JSON": '["hyde"]'}, "inference_feature_map_json"),
        ({"LOOM_INFERENCE_FEATURE_MAP_JSON": "{not json"}, "inference_feature_map_json"),
        ({"LOOM_INFERENCE_CUSTOMER_FORMAT": "cloud/{org}"}, "as they are"),
        ({"LOOM_INFERENCE_FEATURE_MAP_JSON": '{"hyde": "search/expand"}'}, "as they are"),
        ({"LOOM_INFERENCE_FEATURE_MAP_JSON": '{"hyde": null}'}, "map names to strings"),
        ({"LOOM_INFERENCE_FEATURE_MAP_JSON": '{"hyde": ""}'}, "as they are"),
    ],
)
def test_settings_refuse_a_cell_that_could_not_name_its_customers(monkeypatch, env, error) -> None:
    from server.settings import Settings

    for k, v in env.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(ValueError, match=error):
        Settings()


def test_the_system_customer_is_named_as_configured_not_formatted(metered, monkeypatch) -> None:
    monkeypatch.setattr(settings, "inference_customer_format", "cloud:{org}")
    monkeypatch.setattr(settings, "inference_system_customer", "loom-deployment")
    with customer.system():
        assert _headers()["X-Customer"] == "loom-deployment"
    with customer.system(), customer.for_org("acme"):
        assert _headers()["X-Customer"] == "cloud:acme"
    with customer.system(), customer.for_org("loom-deployment"):  # an org named like it
        assert _headers()["X-Customer"] == "cloud:loom-deployment"


def test_a_required_cell_without_its_header_refuses_rather_than_sends(monkeypatch) -> None:
    # Load refuses this pairing; set after load, no call goes out unmetered.
    monkeypatch.setattr(settings, "inference_customer_header", "")
    monkeypatch.setattr(settings, "inference_customer_required", True)
    with customer.for_org("acme"), pytest.raises(customer.Unattributed, match="REQUIRED"):
        asyncio.run(customer.require())


@pytest.mark.parametrize(
    "header, required, scope, org",
    [
        ("", False, None, ""),
        ("", True, "org", "acme"),
        ("X-Customer", False, "org", "acme"),
        ("X-Customer", False, "request", "acme"),
        ("X-Customer", False, "worker", "acme"),
        ("X-Customer", False, "system", ""),
        ("X-Customer", False, None, ""),
    ],
)
def test_names_one_agrees_with_require(metered, monkeypatch, header, required, scope, org) -> None:
    monkeypatch.setattr(settings, "inference_customer_header", header)
    monkeypatch.setattr(settings, "inference_customer_required", required)
    with contextlib.ExitStack() as stack:
        if scope == "org":
            stack.enter_context(customer.for_org(org))
        elif scope == "request":
            _request(org)
        elif scope == "worker":
            _request(org, op="worker.embedding")
        elif scope == "system":
            stack.enter_context(customer.system())
        try:
            asyncio.run(customer.require())
            accepted = True
        except customer.Unattributed:
            accepted = False
        assert customer.names_one() is accepted


# --- each job names the org of its rows ---------------------------------------


async def _model_call(seen: list[str]) -> None:
    """One embedding and one chat call on the fakes, which refuse a call naming
    no customer as a metered provider would; records the customer it ran for."""
    from server.embedding.fake import FakeNGramEmbedder
    from server.llm.fake import FakeLLMClient

    await FakeNGramEmbedder(dim=8).embed("text")
    await FakeLLMClient().complete("s", "u")
    seen.append(customer.current())


class _MeteredLLM:
    """A canned LLM that checks the customer first, as the fake LLM does."""

    def __init__(self, text: str) -> None:
        self.text = text

    async def complete(self, *a: Any, **k: Any) -> Any:
        await customer.require()
        return type("Out", (), {"text": self.text, "tokens_in": 1, "tokens_out": 1})()


def _no_refusal_logged(caplog: pytest.LogCaptureFixture) -> None:
    assert not [r for r in caplog.records if "MODEL_CALL_UNATTRIBUTED" in r.getMessage()]


def test_code_ingest_names_each_repos_org_in_its_commits_bootstrap_and_walk(
    metered, monkeypatch, caplog
) -> None:
    from types import SimpleNamespace

    from server.code.app.index import ingest_pass as w
    from server.code.app.index import pass_budget, repo_registry

    seen: list[str] = []
    cfg = SimpleNamespace(org="acme", namespace="code-a", label="code-a", source="config")

    async def nothing(*a: Any, **k: Any) -> Any:
        return set()

    async def run_repo(cfg: Any, result: Any, budget: Any) -> Any:
        await _model_call(seen)
        return SimpleNamespace(cfg=cfg, bootstrap=True, walk=True)

    async def leg(plan: Any, result: Any, budget: Any) -> None:
        await _model_call(seen)

    monkeypatch.setattr(repo_registry, "refresh_db_repos", nothing)
    monkeypatch.setattr(w, "_inactive_namespaces", nothing)
    monkeypatch.setattr(w, "selected_repos", lambda org, ns: [cfg])
    monkeypatch.setattr(w, "_without_unsandboxed", lambda repos, result: repos)
    monkeypatch.setattr(w, "prefetch_mirrors", nothing)
    monkeypatch.setattr(w, "ingest_oldest_first", nothing)
    monkeypatch.setattr(w, "_post_steps", nothing)
    monkeypatch.setattr(w, "_run_repo", run_repo)
    monkeypatch.setattr(w, "_bootstrap_leg", leg)
    monkeypatch.setattr(w, "_walk_leg", leg)
    result = pass_budget.CodeIngestResult()
    asyncio.run(w._run(result))
    assert seen == ["acme"] * 3 and not result.errors
    _no_refusal_logged(caplog)


def test_code_ingest_names_each_entrys_org_for_its_commits(metered, monkeypatch, caplog) -> None:
    from server.code.app.index import pass_budget, pass_stream
    from server.code.app.index import pass_commit as commit_mod
    from tests.unit.test_code_ingest_fair_fetch import _plan, _wire_streams
    from tests.unit.test_code_ingest_pass_budget import _Clock

    seen: list[str] = []
    clock = _Clock()
    _wire_streams(monkeypatch, clock, {"a1": int(clock.time()) - 50})

    async def ingest_commit(*a: Any, **k: Any) -> None:
        await _model_call(seen)

    monkeypatch.setattr(commit_mod, "_ingest_commit", ingest_commit)
    monkeypatch.setattr(pass_stream, "_ingest_commit", ingest_commit)
    plan, _ = _plan("code-a", ["a1"])
    result = pass_budget.CodeIngestResult()
    budget = pass_budget.PassBudget(clock.now, clock.now + 1000.0)
    asyncio.run(pass_stream.ingest_oldest_first([plan], result, budget))
    assert seen == [plan.cfg.org] and not result.errors
    _no_refusal_logged(caplog)


def test_code_summary_names_each_repos_org(metered, monkeypatch, caplog) -> None:
    from types import SimpleNamespace

    from server.code.domain.index import repos
    from server.code.workers import summary

    seen: list[str] = []

    async def summarize(org: str, ns: str, result: Any, cap: int) -> None:
        await _model_call(seen)

    cfg = SimpleNamespace(org="acme", namespace="code-a")
    monkeypatch.setattr(repos, "configured_repos", lambda: [cfg])
    monkeypatch.setattr(summary, "_summarize_repo", summarize)
    monkeypatch.setattr(settings, "code_summary_max_scopes_per_pass", 10)
    result = summary.CodeSummaryResult()
    asyncio.run(summary._run(result))
    assert seen == ["acme"] and not result.errors
    _no_refusal_logged(caplog)


def test_code_embed_recovery_embeds_each_group_for_its_org(metered, caplog) -> None:
    from server.code.workers import embed_recovery
    from server.embedding.fake import FakeNGramEmbedder

    seen: list[str] = []

    class _Embedder(FakeNGramEmbedder):
        async def embed_many(self, texts: list[str]) -> list[list[float]]:
            seen.append(customer.current())
            return await super().embed_many(texts)

    class _Repo:
        rows: list[Any] = []

        def tombstone_of(self, table: str, live: dict[str, Any]) -> dict[str, Any]:
            return dict(live)

        async def insert_rows(self, table: str, rows: list[Any]) -> None:
            self.rows = rows

    rows = [
        {"org": "acme", "namespace": "code-a", "text": "def f(): pass", "embed_attempts": 0},
        {"org": "beta", "namespace": "code-b", "text": "class A: ...", "embed_attempts": 0},
    ]
    result = embed_recovery.CodeEmbedRecoveryResult()
    embedded = asyncio.run(
        embed_recovery._embed_rows(
            _Repo(),
            "code_chunks",
            rows,
            _Embedder(dim=8),
            text_of=lambda r: r["text"],
            max_attempts=5,
            result=result,
        )
    )
    assert embedded == 2 and seen == ["acme", "beta"]
    _no_refusal_logged(caplog)


def test_code_embed_recovery_gives_up_on_a_row_naming_no_customer_without_a_call(
    metered, caplog
) -> None:
    from server.code.workers import embed_recovery

    seen: list[str] = []

    class _Embedder:
        async def embed_many(self, texts: list[str]) -> list[list[float]]:
            seen.append(customer.current())  # a row naming no customer is never sent
            return [[0.5, 0.5] for _ in texts]

    class _Repo:
        rows: list[Any] = []

        def tombstone_of(self, table: str, live: dict[str, Any]) -> dict[str, Any]:
            return dict(live)

        async def insert_rows(self, table: str, rows: list[Any]) -> None:
            self.rows = rows

    repo = _Repo()
    rows = [
        {"org": "", "namespace": "code-a", "text": "def f(): pass", "embed_attempts": 0},
        {"org": "acme", "namespace": "code-a", "text": "def g(): pass", "embed_attempts": 0},
    ]
    result = embed_recovery.CodeEmbedRecoveryResult()
    embedded = asyncio.run(
        embed_recovery._embed_rows(
            repo,
            "code_chunks",
            rows,
            _Embedder(),
            text_of=lambda r: r["text"],
            max_attempts=5,
            result=result,
        )
    )
    assert embedded == 1 and result.poisoned == 1 and seen == ["acme"]
    (row,) = [r for r in repo.rows if r["org"] == ""]
    assert row["embed_last_error"] == customer.NO_CUSTOMER and row["embed_failed_at"]
    assert row["embed_attempts"] == 1
    _no_refusal_logged(caplog)  # bad data, not a missed call path


def test_the_conversation_titler_names_the_threads_org(metered, monkeypatch, caplog) -> None:
    import server.knowledge.store.conversations as repo
    from server.knowledge.workers.conversation_titler import run_pass
    from tests.unit.test_conversation_titler import _CAND, _TURNS, StubCH, _patch

    _patch(monkeypatch, StubCH([_CAND], _TURNS, None))
    monkeypatch.setattr("server.llm.client.get_llm_client", lambda: _MeteredLLM("A title"))

    async def insert(row: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(repo, "insert_thread", insert)
    assert asyncio.run(run_pass()).titled == 1
    _no_refusal_logged(caplog)


def test_the_entity_extractor_names_each_rows_org(metered, monkeypatch, caplog) -> None:
    from server.memory import store as memory_repo
    from server.memory.workers import entity_extractor as ee
    from tests.unit.test_entity_extractor_parse_failures import _Repo, _row

    seen: list[str] = []
    repo = _Repo([_row()])
    monkeypatch.setattr(memory_repo, "find_unentitied_memories", repo.find_unentitied_memories)
    monkeypatch.setattr(memory_repo, "apply_entities_batch", repo.apply_entities_batch)

    async def under_cap(pairs: Any) -> dict[Any, bool]:
        return dict.fromkeys(pairs, False)

    async def extract(value: str) -> list[str]:
        await _model_call(seen)
        return ["alice"]

    monkeypatch.setattr(ee, "namespaces_over_cap", under_cap)
    monkeypatch.setattr(ee, "emit_worker_cost_event_for", lambda **kw: None)
    monkeypatch.setattr(ee, "_extract", extract)
    result = asyncio.run(ee.run_pass(ee.EntityExtractorConfig()))
    assert seen == [_row()["org"]] and not result.failures
    _no_refusal_logged(caplog)


def test_observe_extract_names_each_sessions_org(metered, monkeypatch, caplog) -> None:
    from types import SimpleNamespace

    from server.memory.workers import observe_extract

    seen: list[str] = []
    session = {"org": "acme", "namespace": "ns", "agent": "a", "session_id": "s1"}

    async def sessions(cfg: Any, ch: Any) -> list[dict[str, Any]]:
        return [session]

    async def observations(session: Any, cfg: Any, ch: Any) -> list[Any]:
        return [
            SimpleNamespace(
                memory_key="obs:1", actor="", version=1, sensitivity="", value="prefers tea"
            )
        ]

    async def distill(session: Any, observations: Any) -> Any:
        await _model_call(seen)
        return SimpleNamespace(written=1, observations=1, events=0, extra_failures=[])

    monkeypatch.setattr("server.core.ch.get_client", lambda: None)
    monkeypatch.setattr(observe_extract, "find_ready_sessions", sessions)
    monkeypatch.setattr(observe_extract, "_load_session_observations", observations)
    monkeypatch.setattr(observe_extract, "_distill_session", distill)
    result = asyncio.run(observe_extract.run_pass())
    assert seen == ["acme"] and result.written_memories == 1 and not result.failures
    _no_refusal_logged(caplog)


def test_the_profile_fold_names_the_identitys_org(metered, monkeypatch, caplog) -> None:
    from server.llm.fake import FakeLLMClient
    from server.memory.app import profile
    from server.memory.workers import profile_consolidation as pc

    class _CH:
        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            return [{"memory_id": "m1", "value": "prefers tea", "since": "2026-01-01"}]

    written: list[str] = []

    async def write_section(**kw: Any) -> None:
        written.append(kw["org"])

    monkeypatch.setattr("server.llm.client.get_llm_client", lambda: FakeLLMClient())
    monkeypatch.setattr(pc, "emit_worker_cost_event_for", lambda **kw: None)
    monkeypatch.setattr(profile, "write_folded_section", write_section)
    row = {"org": "acme", "namespace": "ns", "user_id": "u1"}
    cfg = pc.ProfileConsolidationConfig(model="")
    assert asyncio.run(pc._consolidate_identity(_CH(), row, cfg)) is True
    assert written == ["acme"]
    _no_refusal_logged(caplog)


def test_projection_repair_names_each_failures_org(metered, monkeypatch, caplog) -> None:
    from server.memory.app import projection_failures as pf
    from server.memory.workers import projection_repair
    from tests.unit.test_memory_repair_workers import _failure

    seen: list[str] = []

    async def scan(**k: Any) -> list[Any]:
        return [_failure(1, "memory_chunks")]

    async def retry(f: Any, **k: Any) -> Any:
        await _model_call(seen)
        return pf.ProjectionFailureRetryResult(f.failure_id, f.projection, True, True)

    async def marked(done: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(pf, "scan_active_projection_failures", scan)
    monkeypatch.setattr(pf, "_retry", retry)
    monkeypatch.setattr(pf, "mark_retried", marked)
    result = asyncio.run(projection_repair.run_pass())
    assert seen == ["acme"] and result.resolved == 1
    _no_refusal_logged(caplog)


def test_the_conflict_resolver_names_the_rows_org(
    metered,
    monkeypatch,
    caplog,
    row_env,  # noqa: F811
) -> None:
    from server.memory.app import conflict_resolver as resolver
    from tests.unit.claims_fake import fake_key_locks
    from tests.unit.test_conflict_resolver import _q

    fake_key_locks(monkeypatch, wait_s=10.0)
    seen: list[str] = []
    original = resolver.judge

    async def judge(new: Any, peers: Any) -> Any:
        await _model_call(seen)
        return await original(new, peers)

    monkeypatch.setattr(resolver, "judge", judge)
    counts = resolver.Counts()
    asyncio.run(resolver.resolve_row(_q(org="acme"), policy="manual_review", counts=counts))
    assert seen == ["acme"]
    assert row_env.llm_calls == counts.judged == 1
    _no_refusal_logged(caplog)


def test_the_code_link_judge_names_the_links_org(metered, monkeypatch, caplog) -> None:
    from server.code.domain.anchor import Definition
    from server.memory.app import code_link_judge

    monkeypatch.setattr(
        "server.llm.client.get_llm_client_for_model",
        lambda model: _MeteredLLM('{"verdict": "unsure"}'),
    )
    link: Any = {"org": "acme"}
    out = asyncio.run(
        code_link_judge.judge("f adds", link, "old", "new", Definition("a.py", "a.f", "f"), "h")
    )
    assert out is None  # unsure: judged, and nothing to change
    _no_refusal_logged(caplog)


def test_the_period_digest_names_the_periods_org(metered, monkeypatch, caplog) -> None:
    from server.embedding.fake import FakeNGramEmbedder
    from server.memory.app.period_digest import PeriodDigestConfig, run_period_digest
    from tests.unit.test_period_digest import _digests, _FakeCH

    async def link(**kw: Any) -> None:
        return None

    monkeypatch.setattr("server.memory.app.evidence.link_belief_sources", link)
    cfg = PeriodDigestConfig(sessions_per_period=5, namespace_filter="ns", require_opt_in=False)
    res = asyncio.run(
        run_period_digest(
            cfg,
            ch=_FakeCH(_digests(1, 5)),
            llm=_MeteredLLM("Between 2023-07-01 and 2023-07-05, Caroline painted."),
            embedder=FakeNGramEmbedder(dim=3),
        )
    )
    assert res.periods_written == 1 and not res.failures
    _no_refusal_logged(caplog)


def test_an_extraction_queue_replay_names_the_rows_org(metered, monkeypatch, caplog) -> None:
    import server.memory.app.extract.handler as mx
    from server.identity.domain.read_scope import ReadFilters
    from server.memory.api.models_bulk import MemorySetFromMessagesOutput
    from server.memory.app import extraction_queue as queue
    from tests.unit.test_extraction_queue import _input

    seen: list[str] = []

    async def handler(inp: Any, **_kw: Any) -> Any:
        await _model_call(seen)
        return MemorySetFromMessagesOutput()

    monkeypatch.setattr(mx, "handle_memory_set_from_messages", handler)
    payload, _, _ = queue.payload_for(
        _input(org="acme"), scope=ReadFilters(scope="own_user", user_id="u1", session_id=None)
    )
    asyncio.run(queue.replay_row({"org": "acme", "payload": payload}))
    assert seen == ["acme"]
    _no_refusal_logged(caplog)


def test_tracker_embeddings_name_the_namespaces_org(metered, caplog) -> None:
    from datetime import UTC, datetime

    from server.embedding.fake import FakeNGramEmbedder
    from server.tracker.workers import embed as tracker_embed

    class _Repo:
        rows: list[Any] = []

        async def insert_rows(self, rows: list[Any]) -> None:
            self.rows = rows

    repo = _Repo()
    pending = [{"kind": "issue", "number": 1, "source_id": "s", "text": "a bug", "sha": "x"}]
    result = tracker_embed.TrackerEmbedResult()
    asyncio.run(
        tracker_embed._flush(
            repo,
            FakeNGramEmbedder(dim=8),
            "acme",
            "tracker",
            pending,
            result=result,
            now=datetime.now(UTC),
        )
    )
    assert result.rows_embedded == 1 and repo.rows[0]["org"] == "acme"
    _no_refusal_logged(caplog)


def test_the_retrieval_judge_names_the_judged_org(metered, monkeypatch, caplog) -> None:
    from server.llm import client as llm_client
    from server.observability import langfuse_scores, retrieval_judge
    from server.workers import cost_event

    seen: list[dict[str, str]] = []

    class _Client:
        async def complete(self, *a: Any, **k: Any) -> Any:
            seen.append(customer.headers(await customer.require()))
            return type("Out", (), {"text": "{}"})()

    monkeypatch.setattr(retrieval_judge.settings, "retrieval_judge_model", "")
    monkeypatch.setattr(llm_client, "get_llm_client", lambda: _Client())
    monkeypatch.setattr(langfuse_scores, "post_score", lambda **kw: None)
    monkeypatch.setattr(cost_event, "emit_worker_cost_event_for", lambda **kw: None)
    item = {
        "trace_id": "0" * 32,
        "org": "acme",
        "namespace": "ns",
        "query": "q",
        "excerpts": ["e"],
    }
    asyncio.run(retrieval_judge._judge_one(item))
    # The prompt is the org's content, so the call is the org's, named as the judge.
    assert seen == [{"X-Customer": "acme", "X-Feature": "retrieval_judge"}]
    _no_refusal_logged(caplog)


# --- transports ----------------------------------------------------------------


def test_an_http_request_names_the_org_it_addresses(metered, monkeypatch) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from pydantic import BaseModel

    from server.operations.base import Operation, Role
    from server.settings.catalog import OP_BUDGETS_P99_MS
    from server.transport import http as http_mod

    class _In(BaseModel):
        org: str = "-"
        namespace: str = "-"

    class _Out(BaseModel):
        sent: dict[str, str] = {}

    async def handler(_: _In) -> _Out:
        return _Out(sent=customer.headers(await customer.require()))

    op = Operation(
        name="probe.customer",
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
    resp = TestClient(app).post(op.http_path, json={"org": "acme", "namespace": "ns"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["sent"] == {"X-Customer": "acme", "X-Feature": "probe.customer"}


def test_an_mcp_call_whose_identity_fails_names_no_earlier_entrys_org(metered, monkeypatch) -> None:
    # In a JSON-RPC batch the identity is task state: entry N must not bill entry N-1's org.
    import dataclasses

    from server.operations.registry import OPERATIONS_BY_NAME
    from server.transport import mcp_call, mcp_outcome

    base = OPERATIONS_BY_NAME["code.repos"]
    seen: list[str] = []

    async def handler(inp: Any) -> Any:
        seen.append(customer.current())
        return base.output_model.model_construct()

    def broken(*a: Any, **k: Any) -> dict[str, Any]:
        raise RuntimeError("identity unavailable")

    async def none(*a: Any, **k: Any) -> None:
        return None

    op = dataclasses.replace(base, handler=handler)
    monkeypatch.setitem(mcp_call.TOOLS_BY_NAME, op.mcp_tool_name, op)
    monkeypatch.setattr(settings, "code_org", "acme")
    monkeypatch.setattr(settings, "code_namespace", "code-acme")
    monkeypatch.setattr(mcp_call, "_caller_defaults", none)
    monkeypatch.setattr(mcp_call, "_emit_mcp_event", lambda *a, **k: None)
    monkeypatch.setattr(mcp_outcome, "_emit_mcp_event", lambda *a, **k: None)
    monkeypatch.setattr(mcp_call, "_span_identity", broken)
    _request("previous-entry")
    out = asyncio.run(mcp_call._call_tool(1, {"name": op.mcp_tool_name, "arguments": {}}))
    assert out["result"]["isError"] is False, out
    assert seen == [""]  # its model calls refuse instead of billing another org


# --- embedders and settings ----------------------------------------------------


@pytest.mark.parametrize("provider", ["openai", "gemini"])
def test_a_batch_embed_refuses_before_reading_the_cache(metered, monkeypatch, provider) -> None:
    from server.embedding import gemini as gemini_mod
    from server.embedding import openai as embed_mod

    mod = embed_mod if provider == "openai" else gemini_mod
    monkeypatch.setattr(mod, "split_cached", lambda *a: pytest.fail("read the cache"))
    embedder = (
        embed_mod.OpenAIEmbedder(model="text-embedding-3-small", dim=8, api_key="k")
        if provider == "openai"
        else gemini_mod.GeminiEmbedder(model="gemini-embedding-001", dim=8, api_key="k")
    )
    with pytest.raises(customer.Unattributed):
        asyncio.run(embedder._embed_many_raw(["text"]))


def test_settings_refuse_a_feature_header_without_the_customer_header(monkeypatch) -> None:
    from server.settings import Settings

    monkeypatch.delenv("LOOM_INFERENCE_CUSTOMER_HEADER", raising=False)
    monkeypatch.setenv("LOOM_INFERENCE_FEATURE_HEADER", "X-Feature")
    with pytest.raises(ValueError, match="needs LOOM_INFERENCE_CUSTOMER_HEADER"):
        Settings()


@pytest.mark.parametrize(
    "field, name",
    [
        ("CUSTOMER", "Authorization"),
        ("CUSTOMER", "X-API-Key"),
        ("CUSTOMER", "x-goog-api-key"),
        ("CUSTOMER", "Content-Type"),
        ("CUSTOMER", "content-length"),
        ("CUSTOMER", "HOST"),
        ("FEATURE", "Anthropic-Version"),
    ],
)
def test_settings_refuse_a_header_the_senders_set(monkeypatch, field: str, name: str) -> None:
    from server.settings import Settings

    monkeypatch.setenv("LOOM_INFERENCE_CUSTOMER_HEADER", "X-Customer")
    monkeypatch.setenv(f"LOOM_INFERENCE_{field}_HEADER", name)
    with pytest.raises(ValueError, match="senders set themselves"):
        Settings()
