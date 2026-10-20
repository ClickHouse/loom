"""Every model call names its customer, the org whose data it carries, for a
gateway that meters by it; one that names none is refused, never billed to no
one, and a job working on several orgs' rows makes one call per org."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from server.llm import customer
from server.observability import genai
from server.settings import settings


@pytest.fixture
def metered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "inference_customer_header", "X-Customer")
    monkeypatch.setattr(settings, "inference_feature_header", "X-Feature")
    monkeypatch.setattr(settings, "inference_system_customer", "loom")
    genai._identity.set(None)


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


def test_a_reembed_page_spanning_two_orgs_makes_one_call_per_org(metered) -> None:
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
    original = reembed.rewrite_row
    try:
        reembed.rewrite_row = lambda r, cfg, model, dim, vec, kvec: {**r, "vec": vec}
        asyncio.run(reembed._rewrite_page(ch, emb, table, chunk, key_only=False))
    finally:
        reembed.rewrite_row = original
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
