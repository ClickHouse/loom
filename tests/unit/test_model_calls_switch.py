"""An org switched off (`org.set_model_calls`) gets no model call: a request's
is refused before anything is sent, a job's queued work waits with its
attempts untouched, and switching the org back on lets it run. Other orgs and
the deployment's own calls go on."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
import pytest

from server.core.errors import BackendUnavailableError
from server.llm import customer
from server.observability import genai, provider_health
from server.settings import settings


@pytest.fixture(autouse=True)
def _identity_left_clean() -> Any:
    genai._identity.set(None)
    yield
    genai._identity.set(None)


def _off(*orgs: str) -> None:
    customer._reset_for_tests(frozenset(orgs))


def _request(org: str) -> None:
    genai.set_identity(org=org, namespace="ns", operation="memory.search")


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def post(self, url: str, **kw: Any) -> httpx.Response:
        self.calls.append({"url": url, **kw})
        body = {
            "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))


def _chat(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    from server.llm import openai as chat_mod

    sent = _Recorder()
    monkeypatch.setattr(chat_mod, "_http_client", lambda: sent)
    asyncio.run(chat_mod.OpenAILLMClient(model="gpt-4o-mini", api_key="k").complete("s", "u"))
    return sent


def test_a_switched_off_orgs_request_call_is_refused_before_anything_is_sent(monkeypatch) -> None:
    provider_health.reset()
    _off("acme")
    _request("acme")
    with pytest.raises(customer.ModelCallsDisabled):
        _chat(monkeypatch)
    assert "openai:chat" not in provider_health.status().get("providers", {})  # not its failure


def test_other_orgs_and_the_deployments_own_calls_go_on(monkeypatch) -> None:
    _off("acme")
    _request("beta")
    assert len(_chat(monkeypatch).calls) == 1
    _request("acme")
    with customer.system():
        assert len(_chat(monkeypatch).calls) == 1


def test_it_holds_with_no_customer_header_and_lets_an_unknown_org_through(monkeypatch) -> None:
    monkeypatch.setattr(settings, "inference_customer_header", "")
    _off("acme")
    _request("acme")
    with pytest.raises(customer.ModelCallsDisabled):
        asyncio.run(customer.admit())
    genai._identity.set(None)  # a dev call naming no org: nothing to check, no header to send
    assert asyncio.run(customer.admit()) == "" and customer.headers() == {}


def test_a_switched_off_org_is_served_no_cached_vector(monkeypatch) -> None:
    # The switch is a kill switch: a vector another org's call cached is not
    # served to it, though it would cost nothing.
    from server.embedding.fake import FakeNGramEmbedder
    from server.embedding.openai import OpenAIEmbedder

    calls: list[str] = []

    async def fetch(self: Any, text: str) -> list[float]:
        calls.append(text)
        return [0.5] * 8

    monkeypatch.setattr(OpenAIEmbedder, "_embed_one_raw", fetch)
    embedder = OpenAIEmbedder(model="text-embedding-3-small", dim=8, api_key="k")
    _off("acme")
    _request("beta")
    asyncio.run(embedder.embed("the same text"))
    _request("acme")
    with pytest.raises(customer.ModelCallsDisabled):
        asyncio.run(embedder.embed("the same text"))
    assert calls == ["the same text"]
    with pytest.raises(customer.ModelCallsDisabled):  # the fakes stand in for a provider
        asyncio.run(FakeNGramEmbedder(dim=8).embed("text"))


def test_a_switched_off_org_is_served_no_cached_answer(monkeypatch) -> None:
    from server.llm import cache as cache_mod
    from server.llm.client import LLMResult

    asked: list[str] = []

    class _Inner:
        provider, model = "openai", "m"

        async def complete(self, system: str, user: str, **kw: Any) -> LLMResult:
            asked.append(user)
            return LLMResult(text="answer", tokens_in=1, tokens_out=1)

    async def hit(self: Any, key: str) -> LLMResult:
        return LLMResult(text="cached", tokens_in=0, tokens_out=0)

    monkeypatch.setattr(cache_mod.CachedLLMClient, "_get", hit, raising=False)
    client = cache_mod.CachedLLMClient(_Inner())
    _off("acme")
    _request("acme")
    with pytest.raises(customer.ModelCallsDisabled):
        asyncio.run(client.complete("s", "u"))
    assert asked == []


class _Switch:
    """loom.org_model_calls as the read sees it; `down` makes the read fail."""

    def __init__(self, off: set[str]) -> None:
        self.off, self.down, self.reads = off, False, 0

    async def query_all(self, sql: str, **_: Any) -> list[dict[str, Any]]:
        assert "loom.org_model_calls FINAL WHERE enabled = 0" in sql
        self.reads += 1
        if self.down:
            raise ConnectionError("clickhouse away")
        return [{"org": o} for o in sorted(self.off)]


@pytest.fixture
def switch(monkeypatch: pytest.MonkeyPatch) -> _Switch:
    table = _Switch(set())
    monkeypatch.setattr("server.core.ch.get_client", lambda: table)
    customer._reset_for_tests(read=False)
    return table


def test_switching_back_on_takes_effect_and_the_switch_is_read_at_most_every_interval(
    switch: _Switch,
) -> None:
    switch.off = {"acme"}
    assert asyncio.run(customer.switched_off("acme")) is True
    assert asyncio.run(customer.switched_off("beta")) is False
    assert switch.reads == 1  # one read serves the interval
    switch.off = set()
    customer.forget_switch()  # a change written on this server
    assert asyncio.run(customer.switched_off("acme")) is False and switch.reads == 2


def test_a_copy_too_old_to_trust_refuses_every_call(switch: _Switch, monkeypatch) -> None:
    assert asyncio.run(customer.switched_off("acme")) is False
    switch.down = True
    customer._read_at = time.monotonic() - settings.model_calls_policy_refresh_seconds - 1
    assert asyncio.run(customer.switched_off("acme")) is False  # still young enough
    customer._read_at = time.monotonic() - settings.jwt_revocation_max_stale_seconds - 1
    with pytest.raises(BackendUnavailableError):
        asyncio.run(customer.switched_off("beta"))


def test_a_switch_never_read_refuses(switch: _Switch) -> None:
    switch.down = True
    with pytest.raises(BackendUnavailableError):
        asyncio.run(customer.switched_off("acme"))


# ----------------------------------------------------------------- the ops


def test_an_org_admin_switches_its_org_and_reads_it_back(monkeypatch) -> None:
    from server.auth.context import set_current_auth
    from server.auth.jwt import AuthInfo
    from server.identity.api.models_model_calls import OrgModelCallsInput, OrgSetModelCallsInput
    from server.identity.app import model_calls
    from server.operations.base import Role

    rows: list[dict[str, Any]] = []

    async def insert_row(row: dict[str, Any], **_: Any) -> None:
        rows.append(row)

    async def get(org: str, **_: Any) -> dict[str, Any] | None:
        mine = [r for r in rows if r["org"] == org]
        return max(mine, key=lambda r: r["version"]) if mine else None

    monkeypatch.setattr(model_calls.repo, "insert_row", insert_row)
    monkeypatch.setattr(model_calls.repo, "get", get)
    forgot: list[bool] = []
    monkeypatch.setattr(customer, "forget_switch", lambda: forgot.append(True))
    admin = AuthInfo(
        subject="ops@acme.com",
        org="acme",
        namespaces=(),
        roles=(Role.ORG_ADMIN,),
        is_service_account=False,
    )
    set_current_auth(admin)
    try:
        before = asyncio.run(model_calls.handle_org_model_calls(OrgModelCallsInput(org="acme")))
        assert before.enabled is True and before.updated_at is None  # never set: on
        body = OrgSetModelCallsInput(org="acme", enabled=False, reason="AI off")
        out = asyncio.run(model_calls.handle_org_set_model_calls(body))
        assert out.enabled is False and out.updated_by == "ops@acme.com" and forgot == [True]
        [row] = rows
        assert (row["org"], row["enabled"], row["reason"], row["updated_by"]) == (
            "acme",
            0,
            "AI off",
            "ops@acme.com",
        )
        now = asyncio.run(model_calls.handle_org_model_calls(OrgModelCallsInput(org="acme")))
        assert (now.enabled, now.reason, now.updated_by) == (False, "AI off", "ops@acme.com")
    finally:
        set_current_auth(None)


def test_an_org_admin_of_another_org_is_refused() -> None:
    from server.auth.authz import check_authorized
    from server.auth.jwt import AuthInfo
    from server.identity.api import ops_model_calls
    from server.operations.base import Role

    other = AuthInfo(
        subject="ops@beta.com",
        org="beta",
        namespaces=(),
        roles=(Role.ORG_ADMIN,),
        is_service_account=False,
    )
    for op in (ops_model_calls.org_set_model_calls, ops_model_calls.org_model_calls):
        assert op.required_role is Role.ORG_ADMIN
        with pytest.raises(PermissionError):
            asyncio.run(check_authorized(other, op.required_role, "acme", None))
    asyncio.run(check_authorized(other, Role.ORG_ADMIN, "beta", None))


def test_a_namespace_admin_is_not_an_org_admin() -> None:
    from server.auth.authz import check_authorized
    from server.auth.jwt import AuthInfo
    from server.operations.base import Role

    ns_admin = AuthInfo(
        subject="lead@acme.com",
        org="acme",
        namespaces=("team",),
        roles=(Role.ADMIN,),
        is_service_account=False,
    )
    with pytest.raises(PermissionError):
        asyncio.run(check_authorized(ns_admin, Role.ORG_ADMIN, "acme", None))


# ----------------------------------------------------------------- the queues


def _stub(org: str, key: str) -> dict[str, Any]:
    return {
        "org": org,
        "namespace": "n",
        "agent": "a",
        "user_id": "-",
        "session_id": "-",
        "memory_key": key,
        "memory_type": "semantic",
        "scope": "private",
        "content_excerpt": f"text of {key}",
        "embedding_model": "fake-ngram-3",
        "created_at": None,
        "embed_attempts": 0,
        "run_id": "-",
        "entities": [],
        "tags": [],
        "version": 10,
    }


async def test_the_embedding_worker_leaves_a_switched_off_orgs_stubs_until_it_is_back_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory import store as memory_repo
    from server.memory.workers import embedding as w

    stubs = [_stub("acme", "a1"), _stub("beta", "b1")]
    inserted: list[dict[str, Any]] = []

    async def find_stubs(*, exclude_orgs: frozenset[str] = frozenset(), **_: Any) -> list[Any]:
        # The stubs the query would return: the switched-off org's stay in the table.
        done = {r["memory_key"] for r in inserted if r.get("embedding_dim")}
        return [
            dict(s) for s in stubs if s["org"] not in exclude_orgs and s["memory_key"] not in done
        ]

    async def insert_rows(rows: list[dict[str, Any]], *a: Any, **kw: Any) -> None:
        inserted.extend(rows)

    async def none(**_: Any) -> list[Any]:
        return []

    async def no_refresh(rows: Any) -> None:
        return None

    class _E:
        model_name, dim = "fake-ngram-3", 3

        async def embed_many(self, texts: list[str]) -> list[list[float]]:
            return [[1.0, 2.0, 3.0] for _ in texts]

    monkeypatch.setattr(memory_repo, "find_embedding_stubs", find_stubs)
    monkeypatch.setattr(memory_repo, "insert_embedding_rows", insert_rows)
    monkeypatch.setattr(memory_repo, "find_memories_without_embedding_rows", none)
    monkeypatch.setattr(w, "_refresh_entities", no_refresh)
    monkeypatch.setattr(w, "get_embedder", lambda: _E())

    _off("acme")
    first = await w.run_pass(w.EmbeddingWorkerConfig(batch_size=10))
    assert first.embedded_count == 1
    assert {r["org"] for r in inserted} == {"beta"}  # acme's stub untouched: no retry, no attempt
    _off()
    second = await w.run_pass(w.EmbeddingWorkerConfig(batch_size=10))
    assert second.embedded_count == 1
    assert [r["org"] for r in inserted if r.get("embedding_dim")] == ["beta", "acme"]


async def test_the_extraction_queue_leaves_a_switched_off_orgs_requests_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app import extraction_queue as q
    from server.memory.workers import extraction_queue as w

    queued = [
        {"org": "acme", "namespace": "n", "queue_id": "q1", "enqueued_at": 1},
        {"org": "beta", "namespace": "n", "queue_id": "q2", "enqueued_at": 2},
    ]
    done: list[str] = []
    errors: list[str] = []

    async def claim_batch(*, exclude_orgs: frozenset[str] = frozenset(), **_: Any) -> list[Any]:
        return [r for r in queued if r["org"] not in exclude_orgs and r["queue_id"] not in done]

    async def replay_row(row: dict[str, Any]) -> int:
        return 1

    async def mark_done(row: dict[str, Any], **_: Any) -> None:
        done.append(row["queue_id"])

    async def mark_error(row: dict[str, Any], **_: Any) -> str:
        errors.append(row["queue_id"])
        return "error"

    monkeypatch.setattr(q, "claim_batch", claim_batch)
    monkeypatch.setattr(q, "replay_row", replay_row)
    monkeypatch.setattr(q, "mark_done", mark_done)
    monkeypatch.setattr(q, "mark_error", mark_error)
    monkeypatch.setattr("server.core.ch.get_client", lambda: object())

    _off("acme")
    await w.run_pass(w.ExtractionQueueConfig())
    assert done == ["q2"] and errors == []
    _off()
    await w.run_pass(w.ExtractionQueueConfig())
    assert done == ["q2", "q1"] and errors == []


async def test_a_request_switched_off_after_its_claim_spends_no_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app import extraction_queue as q
    from server.memory.workers import extraction_queue as w

    errors: list[str] = []

    async def claim_batch(**_: Any) -> list[Any]:
        return [{"org": "acme", "namespace": "n", "queue_id": "q1", "enqueued_at": 1}]

    async def replay_row(row: dict[str, Any]) -> int:
        raise customer.ModelCallsDisabled("switched off")

    async def mark_error(row: dict[str, Any], **_: Any) -> str:
        errors.append(row["queue_id"])
        return "error"

    monkeypatch.setattr(q, "claim_batch", claim_batch)
    monkeypatch.setattr(q, "replay_row", replay_row)
    monkeypatch.setattr(q, "mark_error", mark_error)
    monkeypatch.setattr("server.core.ch.get_client", lambda: object())
    result = await w.run_pass(w.ExtractionQueueConfig())
    assert errors == [] and result.skipped == 1


async def test_the_entity_extractor_selects_no_row_of_a_switched_off_org(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory import store as memory_repo
    from server.memory.workers import entity_extractor as w

    seen: list[frozenset[str]] = []

    async def find(**kw: Any) -> list[Any]:
        seen.append(kw.get("exclude_orgs", frozenset()))
        return []

    monkeypatch.setattr(memory_repo, "find_unentitied_memories", find)
    monkeypatch.setattr(memory_repo, "find_unentitied_keys", find)
    _off("acme")
    await w._next_candidates(w.EntityExtractorConfig(batch_size=5))
    assert seen and all(s == frozenset({"acme"}) for s in seen)


def test_the_stub_query_leaves_out_switched_off_orgs() -> None:
    from server.memory.store import sweeps

    sent: list[tuple[str, dict[str, Any]]] = []

    class _CH:
        async def query_all(self, sql: str, params: dict[str, Any]) -> list[Any]:
            sent.append((sql, params))
            return []

    asyncio.run(
        sweeps.find_embedding_stubs(
            batch_size=5, max_attempts=3, exclude_orgs=frozenset({"acme"}), ch=_CH()
        )
    )
    sql, params = sent[0]
    assert "org NOT IN {exclude_orgs:Array(String)}" in sql and params["exclude_orgs"] == ["acme"]
