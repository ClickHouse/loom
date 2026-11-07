"""Every model call names whom it is for in the headers
LOOM_INFERENCE_IDENTITY_HEADERS_JSON configures: a request's token, namespace and
user, a job's row (its writer, else the agent whose namespace it is in); a header
whose template renders empty is not sent, and a job batching rows makes one call
per identity."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from server.auth.context import current_auth_var
from server.auth.jwt import AuthInfo
from server.llm import customer, identity
from server.observability import genai
from server.settings import Settings, settings
from tests.unit.model_client import through_the_model_client

AGENT = "sa:cell-t1:agent-7"
AGENT_NS = "cell-t1-agent-7"
_HEADERS = (
    '{"X-Tenant": "{tenant}", "X-Agent": "{agent}", "X-Actor": "{email}", '
    '"X-Org": "{org}", "X-Namespace": "{namespace}", "X-User": "{user_id}"}'
)


class _Directory:
    """The reads identity headers make, stubbed: trust `cell-t1` requires
    `tenant_id=t1`, namespace `cell-t1-agent-7` is its agent's, `sso-alice`'s
    address is alice@example.com."""

    def __init__(self) -> None:
        self.claims: dict[str, dict[str, str]] = {
            "cell-t1": {"tenant_id": "t1", "org_id": "o9", "agent_id": "*"}
        }
        self.owners: dict[tuple[str, str], str] = {("acme", AGENT_NS): AGENT, ("acme", "ns"): ""}
        self.reads: list[tuple[str, str]] = []
        self.failing = False
        # The writer (`actor`) of each memory, by identity key and memory key.
        self.writers: dict[tuple[str, str, str, str, str, str], str] = {}

    async def machine(self, name: str, *, active_only: bool = True, ch: Any = None) -> Any:
        self.reads.append(("trust", name))
        if self.failing:
            raise ConnectionError("clickhouse is away")
        claims = self.claims.get(name)
        return None if claims is None else {"name": name, "claims": claims}

    async def owner(self, ch: Any, org: str, namespace: str) -> str | None:
        self.reads.append(("owner", namespace))
        if self.failing:
            raise ConnectionError("clickhouse is away")
        return self.owners.get((org, namespace))

    async def actors_by_key(
        self, org: str, keys: Any, *, scope: Any, ch: Any = None
    ) -> dict[tuple[str, str], list[dict[str, str]]]:
        wanted = {(org, ns, key) for ns, key in keys}
        out: dict[tuple[str, str], list[dict[str, str]]] = {}
        for (o, ns, agent, uid, sid, key), actor in self.writers.items():
            if (o, ns, key) in wanted:
                head = {"agent": agent, "user_id": uid, "session_id": sid, "actor": actor}
                out.setdefault((ns, key), []).append(head)
        return out

    async def aliases(self, org: str, subject: str, *, ch: Any = None) -> list[str]:
        self.reads.append(("aliases", subject))
        return [subject, "alice@example.com"] if subject == "sso-alice" else [subject]


@pytest.fixture
def directory(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Directory]:
    d = _Directory()
    monkeypatch.setattr(settings, "inference_identity_headers_json", _HEADERS)
    monkeypatch.setattr(settings, "inference_customer_header", "")
    monkeypatch.setattr("server.auth.machines.get", d.machine)
    monkeypatch.setattr("server.auth.personal_namespace.namespace_owner", d.owner)
    monkeypatch.setattr("server.auth.aliases.subject_aliases", d.aliases)
    monkeypatch.setattr("server.auth.aliases.read_aliases", d.aliases)
    monkeypatch.setattr("server.memory.store.actors_by_key", d.actors_by_key)
    monkeypatch.setattr("server.core.ch.get_client", lambda: None)
    identity._reset_for_tests()
    genai._identity.set(None)
    token = current_auth_var.set(None)
    yield d
    current_auth_var.reset(token)
    genai._identity.set(None)
    identity._reset_for_tests()


def _as(
    subject: str, *, service: bool = True, namespace: str = AGENT_NS, user_id: str = ""
) -> None:
    """A request of `subject`'s token, addressing acme/`namespace`."""
    current_auth_var.set(
        AuthInfo(
            subject=subject,
            org="acme",
            namespaces=(namespace,),
            roles=(),
            is_service_account=service,
        )
    )
    genai.set_identity(org="acme", namespace=namespace, user_id=user_id, operation="memory.search")


def _sent() -> dict[str, str]:
    return customer.headers(asyncio.run(customer.require()))


def test_a_request_names_its_agent_and_the_tenant_its_trust_requires(directory) -> None:
    _as(AGENT)
    assert _sent() == {
        "X-Tenant": "t1",
        "X-Agent": "agent-7",
        "X-Org": "acme",
        "X-Namespace": AGENT_NS,
        "X-User": AGENT,  # no user_id: the caller, as the traces name the end user
    }


def test_a_persons_request_names_their_address_and_no_agent(directory) -> None:
    _as("sso-alice", service=False, namespace="ns", user_id="end-user-1")
    assert _sent() == {
        "X-Actor": "alice@example.com",
        "X-Org": "acme",
        "X-Namespace": "ns",
        "X-User": "end-user-1",
    }


def test_an_empty_header_is_left_out_and_a_value_is_encoded_only_where_it_must(
    directory, monkeypatch
) -> None:
    monkeypatch.setattr(
        settings,
        "inference_identity_headers_json",
        '{"X-Caller": "loom", "X-Agent": "{agent}", "X-Seat": "agent:{agent}", '
        '"X-User": "u:{user_id}", "X-Who": "{email}"}',
    )
    _as("sso-alice", service=False, namespace="ns", user_id="a b%c@d+e.f/ü")
    assert _sent() == {
        "X-Caller": "loom",  # no placeholder: always sent
        # X-Seat names the agent, which a person's call has none of: left out whole
        "X-User": "u:a%20b%25c@d+e.f/%C3%BC",
        "X-Who": "alice@example.com",
    }


def test_a_value_is_trimmed_and_left_out_when_nothing_but_space_remains(
    directory, monkeypatch
) -> None:
    monkeypatch.setattr(
        settings,
        "inference_identity_headers_json",
        '{"X-Actor": "{email} via loom", "X-Pad": "  {email}  ", "X-Agent": "{agent}"}',
    )
    _as(AGENT)  # an agent has no address: a header value never starts with a space
    assert _sent() == {"X-Actor": "via loom", "X-Agent": "agent-7"}
    _as("sso-alice", service=False, namespace="ns")
    assert _sent() == {"X-Actor": "alice@example.com via loom", "X-Pad": "alice@example.com"}


def test_without_identity_headers_nothing_is_sent_or_read(directory, monkeypatch) -> None:
    monkeypatch.setattr(settings, "inference_identity_headers_json", "")
    _as(AGENT)
    assert _sent() == {}
    assert directory.reads == []


def test_only_what_the_templates_name_is_read(directory, monkeypatch) -> None:
    monkeypatch.setattr(settings, "inference_identity_headers_json", '{"X-Agent": "{agent}"}')
    _as(AGENT)
    assert _sent() == {"X-Agent": "agent-7"}
    assert directory.reads == []  # the agent is in the subject itself
    monkeypatch.setattr(settings, "inference_identity_headers_json", '{"X-User": "{user_id}"}')
    with customer.for_org("acme", namespace=AGENT_NS, user_id="u1"):
        assert _sent() == {"X-User": "u1"}
    assert directory.reads == []  # a user_id in hand: the namespace's owner is not read


def test_a_trust_is_read_once_a_minute_and_a_prefix_claim_names_no_tenant(directory) -> None:
    _as(AGENT)
    _sent()
    _sent()
    assert directory.reads.count(("trust", "cell-t1")) == 1
    directory.claims["cell-t2"] = {"tenant_id": "t*"}
    _as("sa:cell-t2:agent-1")
    sent = _sent()  # no tenant: the agent does not go alone either
    assert "X-Tenant" not in sent and "X-Agent" not in sent and sent["X-Org"] == "acme"


def test_the_tenant_is_the_claim_inference_tenant_claim_names(directory, monkeypatch) -> None:
    monkeypatch.setattr(settings, "inference_tenant_claim", "org_id")
    _as(AGENT)
    assert _sent()["X-Tenant"] == "o9"


def test_an_unreadable_identity_holds_the_call_and_a_stale_copy_serves(
    directory, monkeypatch
) -> None:
    _as(AGENT)
    directory.failing = True
    with pytest.raises(identity.IdentityUnread):
        _sent()
    assert identity.IdentityUnread in customer.HOLDS  # a job leaves its rows as they are
    directory.failing = False
    assert _sent()["X-Tenant"] == "t1"
    directory.failing = True
    monkeypatch.setattr(identity, "_KEEP_S", 0.0)  # every call re-reads
    assert _sent()["X-Tenant"] == "t1"  # the copy in hand


def test_a_deployment_call_names_no_one(directory) -> None:
    with customer.system():
        assert _sent() == {}


def test_a_job_names_its_rows_identity_never_the_requests(directory) -> None:
    _as("sa:cell-t1:agent-9", namespace="other")
    with customer.for_org("acme", namespace=AGENT_NS, user_id="u1"):
        sent = _sent()
    assert (sent["X-Agent"], sent["X-User"]) == ("agent-7", "u1")  # the namespace's agent
    with customer.for_org("acme", namespace="ns"):
        assert "X-Agent" not in _sent()  # no writer, and the namespace is no agent's


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
_WANT = {"X-Tenant": "t1", "X-Agent": "agent-7"}


def _carries(headers: dict[str, str]) -> bool:
    return all(headers.get(k) == v for k, v in _WANT.items())


def test_openai_chat_carries_the_identity(directory, monkeypatch) -> None:
    from server.llm import openai as chat_mod

    _as(AGENT)
    chat = _Recorder(_CHAT)
    monkeypatch.setattr(chat_mod, "_http_client", lambda: chat)
    asyncio.run(chat_mod.OpenAILLMClient(model="gpt-4o-mini", api_key="k").complete("s", "u"))
    assert _carries(chat.calls[0]["headers"])


def test_anthropic_messages_carry_the_identity(directory, monkeypatch) -> None:
    from server.llm import anthropic as anthropic_mod

    _as(AGENT)
    messages = _Recorder(_MESSAGES)
    monkeypatch.setattr(anthropic_mod, "_http_client", lambda: messages)
    asyncio.run(
        anthropic_mod.AnthropicLLMClient(model="claude-haiku-4-5", api_key="k").complete("s", "u")
    )
    assert _carries(messages.calls[0]["headers"])


def test_openai_embeddings_carry_the_identity(directory, monkeypatch) -> None:
    from server.embedding import openai as embed_mod

    _as(AGENT)
    vectors = _Recorder(
        {"data": [{"index": 0, "embedding": [0.0] * 8}], "usage": {"total_tokens": 1}}
    )
    monkeypatch.setattr(embed_mod, "_http_client", lambda: vectors)
    embedder = embed_mod.OpenAIEmbedder(model="text-embedding-3-small", dim=8, api_key="k")
    asyncio.run(embedder._embed_one_raw("text"))
    asyncio.run(embedder.embed_many(["a batch"]))
    assert all(_carries(c["headers"]) for c in vectors.calls) and len(vectors.calls) == 2


def test_compatible_embeddings_carry_the_identity(directory, monkeypatch) -> None:
    from server.embedding import compatible as compatible_mod

    _as(AGENT)
    vectors = _Recorder({"data": [{"index": 0, "embedding": [0.5] * 8}]})
    monkeypatch.setattr(compatible_mod, "get_model_client", lambda: vectors)
    embedder = compatible_mod.CompatibleEmbedder(
        model="m", dim=8, api_key="k", base_url="https://embed.example/v1"
    )
    asyncio.run(embedder.embed_many(["text"]))
    assert _carries(vectors.calls[0]["headers"])


def test_gemini_embeddings_carry_the_identity(directory, monkeypatch) -> None:
    from server.embedding import gemini as gemini_mod

    _as(AGENT)
    vectors = _Recorder({"embedding": {"values": [0.0] * 8}})
    monkeypatch.setattr(gemini_mod, "_http_client", lambda: vectors)
    embedder = gemini_mod.GeminiEmbedder(model="gemini-embedding-001", dim=8, api_key="k")
    asyncio.run(embedder._embed_one_raw("text"))
    assert _carries(vectors.calls[0]["headers"])


def _sent_headers(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """The headers of each request the model client sends to the operator endpoints."""
    sent: list[Any] = []

    def wire(request: httpx.Request) -> httpx.Response:
        sent.append(request.headers)
        return httpx.Response(200, json={"ranked": [], "results": [], "memory_type": "a"})

    client = through_the_model_client(monkeypatch)
    monkeypatch.setattr(client, "_transport", httpx.MockTransport(wire))
    return sent


def test_rerank_requests_carry_the_identity_to_the_gateway_only(directory, monkeypatch) -> None:
    from server.memory.app import rerank as rerank_mod

    sent = _sent_headers(monkeypatch)
    monkeypatch.setattr(settings, "inference_gateway_hosts", "rerank.example")
    monkeypatch.setattr(settings, "rerank_endpoints_json", '{"gw": "https://rerank.example/v1"}')
    monkeypatch.setenv("COHERE_API_KEY", "key")
    _as(AGENT)
    candidates = [rerank_mod.RerankCandidate("k", AGENT_NS, "v", "fact", 1.0)]
    asyncio.run(
        rerank_mod.rerank(endpoint="gw", prompt_template="t", query="q", candidates=candidates)
    )
    asyncio.run(rerank_mod._rerank_with_builtin_cohere("builtin:cohere", "q", candidates))
    assert len(sent) == 2 and _carries(sent[0])
    assert not {"x-tenant", "x-agent", "x-org"} & set(sent[1])  # Cohere is no gateway host


def test_classify_requests_carry_the_identity(directory, monkeypatch) -> None:
    from server.memory.app import classify as classify_mod

    sent = _sent_headers(monkeypatch)
    monkeypatch.setattr(settings, "inference_gateway_hosts", "classify.example")
    _as(AGENT)
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
    assert _carries(sent[0])


class _Headers:
    """A model call that records the headers it would send."""

    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []

    async def __call__(self, *a: Any, **k: Any) -> Any:
        self.sent.append(customer.headers(await customer.require()))
        return type("Out", (), {"text": "A title", "tokens_in": 1, "tokens_out": 1})()


def test_an_extraction_queue_replay_is_the_enqueuers(directory, monkeypatch) -> None:
    import server.memory.app.extract.handler as mx
    from server.identity.domain.read_scope import ReadFilters
    from server.memory.api.models_bulk import MemorySetFromMessagesOutput
    from server.memory.app import extraction_queue as queue
    from tests.unit.test_extraction_queue import _input

    call = _Headers()

    async def handler(inp: Any, **_: Any) -> Any:
        await call()
        return MemorySetFromMessagesOutput()

    monkeypatch.setattr(mx, "handle_memory_set_from_messages", handler)
    payload, _, _ = queue.payload_for(
        _input(org="acme", namespace=AGENT_NS, user_id="u1"),
        scope=ReadFilters(scope="own_user", user_id="u1", session_id=None),
        enqueued_by=f"process:{AGENT}",
    )
    row = {"org": "acme", "namespace": AGENT_NS, "user_id": "u1", "payload": payload}
    asyncio.run(queue.replay_row(row))
    assert call.sent == [
        {
            "X-Tenant": "t1",
            "X-Agent": "agent-7",
            "X-Org": "acme",
            "X-Namespace": AGENT_NS,
            "X-User": "u1",
        }
    ]


def test_the_entity_extractor_calls_for_each_rows_writer(directory, monkeypatch) -> None:
    from server.memory import store as memory_repo
    from server.memory.workers import entity_extractor as ee
    from tests.unit.test_entity_extractor_parse_failures import _Repo, _row

    agents_row = {**_row("k1"), "org": "acme", "namespace": "ns", "actor": f"process:{AGENT}"}
    persons_row = {**_row("k2"), "org": "acme", "namespace": "ns", "actor": "human:sso-alice"}
    repo = _Repo([agents_row, persons_row])
    monkeypatch.setattr(memory_repo, "find_unentitied_memories", repo.find_unentitied_memories)
    monkeypatch.setattr(memory_repo, "apply_entities_batch", repo.apply_entities_batch)

    async def under_cap(pairs: Any) -> dict[Any, bool]:
        return dict.fromkeys(pairs, False)

    call = _Headers()

    async def extract(value: str) -> list[str]:
        await call()
        return ["alice"]

    monkeypatch.setattr(ee, "namespaces_over_cap", under_cap)
    monkeypatch.setattr(ee, "emit_worker_cost_event_for", lambda **kw: None)
    monkeypatch.setattr(ee, "_extract", extract)
    result = asyncio.run(ee.run_pass(ee.EntityExtractorConfig()))
    assert not result.failures
    assert [(h.get("X-Agent"), h.get("X-Tenant"), h.get("X-Actor")) for h in call.sent] == [
        ("agent-7", "t1", None),
        (None, None, "alice@example.com"),
    ]


def test_the_titler_calls_for_the_agent_whose_namespace_holds_the_thread(
    directory, monkeypatch
) -> None:
    import server.knowledge.store.conversations as repo
    from server.knowledge.workers.conversation_titler import run_pass
    from tests.unit.test_conversation_titler import _CAND, _TURNS, StubCH, _patch

    call = _Headers()
    _patch(monkeypatch, StubCH([{**_CAND, "org": "acme", "namespace": AGENT_NS}], _TURNS, None))

    class _LLM:
        model = "real"
        complete = call

    monkeypatch.setattr("server.llm.client.get_llm_client", lambda: _LLM())

    async def insert(row: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(repo, "insert_thread", insert)
    assert asyncio.run(run_pass()).titled == 1
    assert (call.sent[0]["X-Agent"], call.sent[0]["X-Tenant"]) == ("agent-7", "t1")


def test_the_embedding_worker_makes_one_call_per_identity(directory, monkeypatch) -> None:
    from server.memory.workers import embedding

    monkeypatch.setattr(settings, "inference_identity_headers_json", '{"X-Agent": "{agent}"}')
    directory.owners[("acme", "cell-t1-agent-8")] = "sa:cell-t1:agent-8"
    sent: list[tuple[str, int]] = []

    class _Embedder:
        model_name, dim = "fake", 2

        async def embed_many(self, texts: list[str]) -> list[list[float]]:
            sent.append((customer.headers(await customer.require()).get("X-Agent", ""), len(texts)))
            return [[float(len(t)), 0.0] for t in texts]

    rows = [
        _stub(AGENT_NS, "k1", "a", user_id="u1"),
        _stub("cell-t1-agent-8", "k2", "bb", user_id="u1"),
        _stub(AGENT_NS, "k3", "ccc", user_id="u2"),
    ]
    out = asyncio.run(embedding._embed_rows(_Embedder(), rows))
    # One call per agent: the user_id is no part of these headers, so it splits nothing.
    assert sent == [("agent-7", 2), ("agent-8", 1)]
    assert [v[0] for v, _ in out] == [1.0, 2.0, 3.0]  # in the rows' order


def _stub(namespace: str, key: str, text: str, *, user_id: str = "u1") -> dict[str, Any]:
    """An embedding stub of acme/`namespace`, its memory keyed `key`."""
    return {
        "org": "acme",
        "namespace": namespace,
        "agent": "a",
        "user_id": user_id,
        "session_id": "-",
        "memory_key": key,
        "content_excerpt": text,
    }


def _load(headers: str, **extra: str) -> Settings:
    extra.setdefault("inference_gateway_hosts", "gateway.example")
    return Settings(_env_file=None, inference_identity_headers_json=headers, **extra)  # type: ignore[call-arg, arg-type]


def test_settings_take_identity_headers_and_refuse_ones_a_call_could_not_send() -> None:
    assert _load(_HEADERS).identity_headers()[1] == ("X-Agent", "{agent}")
    for bad, why in [
        ('{"X Agent": "{agent}"}', "HTTP header name"),
        ('{"Authorization": "{agent}"}', "HTTP header name"),
        ('{"X-Agent": "{agent}", "x-agent": "{tenant}"}', "once"),
        ('{"X-Agent": "{agents}"}', "may name only"),
        ('{"X-Agent": "{agent"}', "may name only"),
        ('{"X-Agent": "agent\\n{agent}"}', "printable ASCII"),
        ('{"X-Agent": 7}', "strings"),
    ]:
        with pytest.raises(ValidationError, match=why):
            _load(bad)
    with pytest.raises(ValidationError, match="HTTP header name"):
        _load('{"X-Customer": "{org}"}', inference_customer_header="X-Customer")


def test_with_identity_headers_a_call_naming_no_org_is_refused_unless_the_deployments(
    directory, caplog, monkeypatch
) -> None:
    monkeypatch.setattr(customer, "_logged_at", {})
    # The MCP path whose identity stamping failed names its op and no org.
    genai.set_identity(operation="memory.search", capture_level="off")
    with pytest.raises(customer.Unattributed):
        _sent()
    assert any("MODEL_CALL_UNATTRIBUTED" in r.getMessage() for r in caplog.records)
    with customer.for_org(""):  # a `loom` command run without --org
        with pytest.raises(customer.Unattributed):
            _sent()
        assert not customer.names_one()
    with customer.system():  # the deployment's own call names no one, on purpose
        assert _sent() == {}
        assert customer.names_one()


def test_the_agent_and_its_tenant_go_together_or_not_at_all(directory, monkeypatch) -> None:
    monkeypatch.setattr(
        settings,
        "inference_identity_headers_json",
        '{"X-Tenant": "{tenant}", "X-Agent": "{agent}", "X-Actor": "{email}"}',
    )
    directory.claims["cell-t3"] = {"agent_id": "*"}  # a trust that requires no tenant
    _as("sa:cell-t3:agent-2")
    assert _sent() == {}  # an agent alone is no identity the gateway takes: neither goes
    _as("sa:cell-t1")  # the trust's own token: a tenant, and no agent
    assert _sent() == {}
    _as("sso-alice", service=False, namespace="ns")
    assert _sent() == {"X-Actor": "alice@example.com"}  # the actor stands alone
    _as(AGENT)
    assert _sent() == {"X-Tenant": "t1", "X-Agent": "agent-7"}


def test_settings_trim_templates_and_refuse_empty_ones_and_hop_by_hop_names() -> None:
    assert _load('{"X-Agent": "  {agent} "}').identity_headers() == (("X-Agent", "{agent}"),)
    for bad, why in (
        ('{"X-Agent": "   "}', "is empty"),
        ('{"Connection": "{agent}"}', "end-to-end"),
        ('{"Transfer-Encoding": "{agent}"}', "end-to-end"),
        ('{"TE": "{agent}"}', "end-to-end"),
        ('{"Proxy-Authorization": "{agent}"}', "end-to-end"),
    ):
        with pytest.raises(ValidationError, match=why):
            _load(bad)


def test_a_jobs_batch_groups_rows_by_the_identity_they_render(directory, monkeypatch) -> None:
    from server.memory.workers import embedding

    monkeypatch.setattr(settings, "inference_identity_headers_json", '{"X-Agent": "{agent}"}')
    directory.owners[("acme", "cell-t1-agent-8")] = "sa:cell-t1:agent-8"
    directory.owners[("acme", "shared")] = ""
    calls: list[tuple[str, int]] = []

    class _Embedder:
        model_name, dim = "fake", 2

        async def embed_many(self, texts: list[str]) -> list[list[float]]:
            agent = customer.headers(await customer.require()).get("X-Agent", "")
            calls.append((agent, len(texts)))
            return [[1.0, 0.0] for _ in texts]

    rows = [
        _stub("cell-t1-agent-8", "k1", "its own namespace"),
        _stub("shared", "k2", "the shared namespace, written by agent-8"),
        _stub(AGENT_NS, "k3", "agent-7's"),
    ]
    directory.writers[("acme", "shared", "a", "u1", "-", "k2")] = "process:sa:cell-t1:agent-8"
    asyncio.run(embedding._embed_rows(_Embedder(), rows))
    assert calls == [("agent-8", 2), ("agent-7", 1)]  # two namespaces, one identity: one call


def test_a_loom_command_bills_its_namespaces_agent_and_one_naming_none_is_refused(
    directory,
) -> None:
    import argparse

    from server.cli import app

    seen: list[dict[str, str]] = []

    def command(args: argparse.Namespace) -> int:
        seen.append(_sent())
        return 0

    app._dispatch(argparse.Namespace(cmd="import", org="acme", namespace=AGENT_NS, func=command))
    assert seen[0]["X-Agent"] == "agent-7"  # the agent whose namespace it writes into
    with pytest.raises(customer.Unattributed, match="--namespace"):
        # --org alone: no agent to name, and never the gateway's default agent
        app._dispatch(argparse.Namespace(cmd="import", org="acme", func=command))
    assert len(seen) == 1  # the refused call returned nothing


def test_a_deployment_call_inside_a_loom_command_is_never_refused(directory) -> None:
    """`loom recalibrate`, or a model change's calibration a command runs, makes the
    deployment's own calls (`customer.system()`): no namespace is needed for them."""
    import argparse

    from server.cli import app

    seen: list[dict[str, str]] = []

    def command(args: argparse.Namespace) -> int:
        with customer.system():
            seen.append(_sent())
        return 0

    for org in ("", "acme"):
        app._dispatch(argparse.Namespace(cmd="recalibrate", org=org, func=command))
    assert len(seen) == 2 and not any("X-Agent" in h for h in seen)
