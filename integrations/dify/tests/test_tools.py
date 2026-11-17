"""Native Dify tool invocation through the installed Loom SDK."""

import json
import os
from pathlib import Path

import httpx
import pytest
import yaml
from provider.loom import LoomProvider
from tools.recall import RecallTool
from tools.remember import RememberTool
from tools.search_knowledge import SearchKnowledgeTool

PLUGIN = Path(__file__).resolve().parents[1]

CREDENTIALS = {
    "base_url": "http://loom.invalid",
    "api_key": "test-token",
    "org": "o",
    "namespace": "n",
    "agent": "dify",
}


@pytest.fixture
def fake(monkeypatch):
    calls = []
    reply = {"status": 200, "body": {"results": [{"content_excerpt": "aisle"}]}}

    async def send(self, request, **kwargs):
        calls.append((request.url.path, json.loads(request.content), request.headers))
        return httpx.Response(reply["status"], json=reply["body"], request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    return calls, reply


def output(cls, parameters, credentials=None):
    tool = cls.from_credentials(credentials or CREDENTIALS)
    messages = list(tool.invoke(parameters))
    assert len(messages) == 1
    return messages[0].message.json_object


def test_remember_uses_bound_identity_and_preserves_text(fake):
    calls, reply = fake
    reply["body"] = {"stored": True}
    assert output(RememberTool, {"user_id": "alice", "memory_key": "seat", "value": "aisle"})[
        "stored"
    ]
    path, body, headers = calls[-1]
    assert path == "/v1/memory.set"
    assert {
        k: body[k] for k in ("org", "namespace", "agent", "user_id", "memory_key", "value")
    } == {
        "org": "o",
        "namespace": "n",
        "agent": "dify",
        "user_id": "alice",
        "memory_key": "seat",
        "value": "aisle",
    }
    assert headers["authorization"] == "Bearer test-token"


@pytest.mark.parametrize(
    "cls,op", [(RecallTool, "memory.search"), (SearchKnowledgeTool, "knowledge.search")]
)
def test_searches_are_user_bound_and_namespace_local(fake, cls, op):
    calls, _ = fake
    assert output(cls, {"user_id": "alice", "query": "seat", "top_k": 3})["results"]
    assert calls[-1][0] == "/v1/" + op
    assert calls[-1][1]["namespace_scope"] == "this"
    assert calls[-1][1]["user_id"] == "alice"
    assert calls[-1][1]["top_k"] == 3
    if cls is RecallTool:
        assert calls[-1][1]["bodies"] == "full"
        assert calls[-1][1]["agent"] == "*"


@pytest.mark.parametrize("user_id", [None, "", " ", "-", "*", {}])
def test_missing_users_are_refused_before_network(fake, user_id):
    calls, _ = fake
    with pytest.raises(ValueError, match="end-user"):
        output(RecallTool, {"user_id": user_id, "query": "seat"})
    assert not calls


@pytest.mark.parametrize("top_k", [0, 501, 1.5, True, float("nan"), float("inf")])
def test_invalid_limits_are_refused_without_a_request(fake, top_k):
    calls, _ = fake
    with pytest.raises(ValueError, match="top_k"):
        output(RecallTool, {"user_id": "alice", "query": "seat", "top_k": top_k})
    assert not calls


def test_provider_checks_authentication_without_writing(fake):
    calls, _ = fake
    LoomProvider().validate_credentials(CREDENTIALS)
    assert calls[-1][0] == "/v1/memory.list"
    assert calls[-1][1]["limit"] == 1


def test_server_refusal_does_not_become_success_or_expose_credentials(fake):
    _, reply = fake
    reply.update(status=403, body={"detail": "test-token"})
    with pytest.raises(ValueError, match="Loom request failed") as caught:
        output(RecallTool, {"user_id": "alice", "query": "seat"})
    assert "test-token" not in str(caught.value)
    with pytest.raises(Exception, match="Loom connection failed") as caught:
        LoomProvider().validate_credentials(CREDENTIALS)
    assert "test-token" not in str(caught.value)


def test_invalid_endpoint_is_refused_before_network(fake):
    calls, _ = fake
    for url in ["file:///tmp/data", "http://name:secret@host", "http://host/?token=secret"]:
        with pytest.raises(ValueError, match="endpoint"):
            output(
                RecallTool, {"user_id": "alice", "query": "seat"}, dict(CREDENTIALS, base_url=url)
            )
    assert not calls


def test_native_sdk_loads_each_tool_schema(monkeypatch):
    from dify_plugin.entities.tool import ToolProviderConfiguration

    monkeypatch.chdir(PLUGIN)
    config = ToolProviderConfiguration.model_validate(
        yaml.safe_load(Path("provider/loom.yaml").read_text())
    )
    assert len(config.tools) == 3
    for tool in config.tools:
        assert next(p for p in tool.parameters if p.name == "user_id").form.value == "form"


@pytest.mark.skipif(
    not os.environ.get("LOOM_BASE_URL") or not os.environ.get("LOOM_API_KEY"),
    reason="needs a private Loom server",
)
def test_native_tools_against_live_loom():
    import time
    import uuid

    credentials = dict(
        CREDENTIALS,
        base_url=os.environ["LOOM_BASE_URL"],
        api_key=os.environ["LOOM_API_KEY"],
        org=os.environ["LOOM_ORG"],
        namespace="dify-" + uuid.uuid4().hex[:8],
    )
    stored = output(
        RememberTool,
        {"user_id": "alice", "memory_key": "seat", "value": "Alice prefers aisle seats"},
        credentials,
    )
    assert stored.get("memory_id") or stored.get("stored")
    for _ in range(30):
        found = output(RecallTool, {"user_id": "alice", "query": "aisle seats"}, credentials)
        if found.get("results"):
            break
        time.sleep(0.1)
    assert any("aisle" in row["content_excerpt"].lower() for row in found["results"])
    assert (
        output(RecallTool, {"user_id": "bob", "query": "aisle seats"}, credentials)["results"] == []
    )

    from client import request

    request(
        credentials,
        "knowledge.upsert",
        {"source_uri": "guide", "content": "Aisle seats are in the travel guide."},
    )
    for _ in range(30):
        documents = output(
            SearchKnowledgeTool, {"user_id": "bob", "query": "aisle seats"}, credentials
        )
        if documents.get("results"):
            break
        time.sleep(0.1)
    assert documents["results"][0]["source_uri"] == "guide"


def test_knowledge_limit_matches_the_wire_cap(fake):
    calls, _ = fake
    with pytest.raises(ValueError, match="100"):
        output(SearchKnowledgeTool, {"user_id": "alice", "query": "guide", "top_k": 101})
    assert not calls


def test_rate_limit_is_not_retried_or_exposed(fake):
    calls, reply = fake
    reply.update(status=429, body={"detail": "test-token"})
    with pytest.raises(ValueError, match="Loom request failed") as caught:
        output(RecallTool, {"user_id": "alice", "query": "seat"})
    assert len(calls) == 1 and "test-token" not in str(caught.value)


@pytest.mark.parametrize(
    "cls,parameters",
    [
        (RememberTool, {"user_id": "alice", "memory_key": "seat", "value": "aisle"}),
        (RecallTool, {"user_id": "alice", "query": "seat"}),
        (SearchKnowledgeTool, {"user_id": "alice", "query": "guide"}),
    ],
)
def test_sdk_deadline_becomes_a_sanitized_tool_failure(monkeypatch, cls, parameters):
    from loom import SyncAgent

    def expired(*args, **kwargs):
        raise TimeoutError("test-token")

    monkeypatch.setattr(SyncAgent, "call", expired)
    with pytest.raises(ValueError, match="Loom request failed") as caught:
        output(cls, parameters)
    assert "test-token" not in str(caught.value)


def test_sdk_deadline_becomes_a_credential_validation_failure(monkeypatch):
    from dify_plugin.errors.tool import ToolProviderCredentialValidationError
    from loom import SyncAgent

    def expired(*args, **kwargs):
        raise TimeoutError("test-token")

    monkeypatch.setattr(SyncAgent, "call", expired)
    with pytest.raises(
        ToolProviderCredentialValidationError, match="Loom connection failed"
    ) as caught:
        LoomProvider().validate_credentials(CREDENTIALS)
    assert "test-token" not in str(caught.value)
