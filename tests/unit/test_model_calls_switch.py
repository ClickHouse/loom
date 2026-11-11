"""An org switched off (`org.set_model_calls`) gets no model call: a request's
is refused before anything is sent, a job's queued work waits with its
attempts untouched, and switching the org back on lets it run. Other orgs and
the deployment's own calls go on."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from server.core.errors import BackendUnavailableError
from server.llm import customer
from server.observability import genai, provider_health
from server.settings import settings
from tests.unit.clickhouse_local import CLICKHOUSE, ddl, run
from tests.unit.switch_state import switch_off, switch_stale, switch_unread


@pytest.fixture(autouse=True)
def _identity_left_clean() -> Any:
    genai._identity.set(None)
    yield
    genai._identity.set(None)


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
    switch_off("acme")
    _request("acme")
    with pytest.raises(customer.ModelCallsDisabled):
        _chat(monkeypatch)
    assert "openai:chat" not in provider_health.status().get("providers", {})  # not its failure


def test_other_orgs_and_the_deployments_own_calls_go_on(monkeypatch) -> None:
    switch_off("acme")
    _request("beta")
    assert len(_chat(monkeypatch).calls) == 1
    genai._identity.set(None)
    with customer.system():  # the deployment's own, naming no org
        assert len(_chat(monkeypatch).calls) == 1


def test_a_deployment_call_naming_a_switched_off_org_is_refused(monkeypatch) -> None:
    # The kill switch holds whatever path names the org: system() only covers
    # calls that name none.
    switch_off("acme")
    with customer.system(), customer.for_org("acme"), pytest.raises(customer.ModelCallsDisabled):
        _chat(monkeypatch)


def test_it_holds_with_no_customer_header_and_lets_an_unknown_org_through(monkeypatch) -> None:
    monkeypatch.setattr(settings, "inference_customer_header", "")
    switch_off("acme")
    _request("acme")
    with pytest.raises(customer.ModelCallsDisabled):
        asyncio.run(customer.require())
    genai._identity.set(None)  # a dev call naming no org: nothing to check, no header to send
    assert customer.headers(asyncio.run(customer.require())) == {}


@pytest.mark.parametrize("cmd", ["rechunk", "code"])
def test_a_cli_command_for_a_switched_off_org_sends_nothing(monkeypatch, cmd: str) -> None:
    # `--org` (or LOOM_CODE_ORG for `loom code`) is the CLI's only identity: with no
    # customer header it is what puts the org's switch in force.
    import argparse

    from server.cli import app
    from server.embedding.fake import FakeNGramEmbedder

    monkeypatch.setattr(settings, "inference_customer_header", "")
    monkeypatch.setattr(settings, "code_org", "acme")
    switch_off("acme")
    sent: list[list[float]] = []

    def command(args: argparse.Namespace) -> int:
        sent.append(asyncio.run(FakeNGramEmbedder(dim=8).embed("text")))  # as run_connected does
        return 0

    with pytest.raises(customer.ModelCallsDisabled):
        app._dispatch(
            argparse.Namespace(cmd=cmd, org="acme" if cmd != "code" else None, func=command)
        )
    assert sent == []


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
    switch_off("acme")
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
    switch_off("acme")
    _request("acme")
    with pytest.raises(customer.ModelCallsDisabled):
        asyncio.run(client.complete("s", "u"))
    assert asked == []


class _Switch:
    """loom.org_model_calls as the read sees it; `down` makes the read fail."""

    def __init__(self, off: set[str]) -> None:
        self.off, self.down, self.reads = off, False, 0
        self.sequential: list[bool] = []

    async def query_all(self, sql: str, **_: Any) -> list[dict[str, Any]]:
        from server.core.ch import sequential_reads_var

        assert "loom.org_model_calls FINAL WHERE enabled = 0" in sql
        self.reads += 1
        self.sequential.append(sequential_reads_var.get())
        if self.down:
            raise ConnectionError("clickhouse away")
        return [{"org": o} for o in sorted(self.off)]


@pytest.fixture
def switch(monkeypatch: pytest.MonkeyPatch) -> _Switch:
    table = _Switch(set())
    monkeypatch.setattr("server.core.ch.get_client", lambda: table)
    switch_unread()
    return table


def test_switching_back_on_takes_effect_and_the_switch_is_read_at_most_every_interval(
    switch: _Switch,
) -> None:
    switch.off = {"acme"}
    assert asyncio.run(customer.switched_off("acme")) is True
    assert asyncio.run(customer.switched_off("beta")) is False
    assert switch.reads == 1  # one read serves the interval
    switch.off = set()
    customer.note_switch_change()  # a change written on this server
    assert asyncio.run(customer.switched_off("acme")) is False and switch.reads == 2
    assert switch.sequential == [True, True]  # the read after a write sees it on any replica


def test_a_copy_too_old_to_trust_refuses_every_call(switch: _Switch) -> None:
    refresh = settings.model_calls_policy_refresh_seconds
    customer._switch._reset_for_tests(frozenset(), read_age_s=refresh + 1, due_in_s=refresh)
    assert asyncio.run(customer.switched_off("acme")) is False  # still young enough
    switch_stale()
    with pytest.raises(BackendUnavailableError):
        asyncio.run(customer.switched_off("beta"))


def test_calls_made_during_a_read_wait_for_it(switch: _Switch, monkeypatch) -> None:
    """A request's two model calls (the extraction and the digest) arrive
    together at a process's first read, or once its copy went stale: both get
    the copy the one read brings, neither answers SwitchUnread."""
    plain = switch.query_all

    async def slow(sql: str, **kw: Any) -> list[dict[str, Any]]:
        await asyncio.sleep(0)  # the other calls arrive while this read runs
        return await plain(sql, **kw)

    monkeypatch.setattr(switch, "query_all", slow)

    async def together() -> list[Any]:
        return list(await asyncio.gather(*(customer.switched_off("acme") for _ in range(3))))

    async def first_then_stale() -> None:
        assert await together() == [False] * 3
        assert switch.reads == 1
        stale = settings.model_calls_policy_max_stale_seconds + 1
        customer._switch._reset_for_tests(frozenset(), read_age_s=stale, due_in_s=0.0)
        assert await together() == [False] * 3
        assert switch.reads == 2

    asyncio.run(first_then_stale())  # one loop, as a server process has


def test_a_cancelled_read_leaves_the_next_call_to_read(switch: _Switch, monkeypatch) -> None:
    plain = switch.query_all
    reading = asyncio.Event()

    async def hang(sql: str, **kw: Any) -> list[dict[str, Any]]:
        reading.set()
        await asyncio.sleep(10)
        return []

    async def cancelled_then_read() -> bool:
        monkeypatch.setattr(switch, "query_all", hang)
        first = asyncio.create_task(customer.switched_off("acme"))
        await reading.wait()
        first.cancel()  # the request that made the read went away
        with pytest.raises(asyncio.CancelledError):
            await first
        monkeypatch.setattr(switch, "query_all", plain)
        return await customer.switched_off("acme")

    assert asyncio.run(cancelled_then_read()) is False


def test_a_call_made_during_a_read_is_served_the_copy_in_hand(switch: _Switch, monkeypatch) -> None:
    """A read is due and in progress; the copy in hand is young enough and no
    change made here waits to be read, so a second call does not wait for it."""
    reading, release = asyncio.Event(), asyncio.Event()
    plain = switch.query_all

    async def slow(sql: str, **kw: Any) -> list[dict[str, Any]]:
        reading.set()
        await release.wait()
        return await plain(sql, **kw)

    async def second_call_during_the_read() -> bool:
        # Read just now, due again, not asked for by a change made here.
        customer._switch._reset_for_tests(frozenset({"acme"}), read_age_s=0.0, due_in_s=0.0)
        monkeypatch.setattr(switch, "query_all", slow)
        first = asyncio.create_task(customer.switched_off("acme"))
        await reading.wait()
        second = await asyncio.wait_for(customer.switched_off("acme"), timeout=1.0)
        release.set()
        await first
        return second

    assert asyncio.run(second_call_during_the_read()) is True  # the copy in hand


def test_a_change_made_during_a_read_is_read_again(switch: _Switch, monkeypatch) -> None:
    plain = switch.query_all

    async def write_meanwhile(sql: str, **kw: Any) -> list[dict[str, Any]]:
        rows = await plain(sql, **kw)
        switch.off = {"acme"}
        customer.note_switch_change()  # the write lands after this read took its rows
        return rows

    monkeypatch.setattr(switch, "query_all", write_meanwhile)

    async def both() -> tuple[bool, bool]:
        first = await customer.switched_off("acme")
        monkeypatch.setattr(switch, "query_all", plain)
        return first, await customer.switched_off("acme")

    assert asyncio.run(both()) == (False, True)


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
    noted: list[bool] = []
    monkeypatch.setattr(customer, "note_switch_change", lambda: noted.append(True))
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
        assert out.enabled is False and out.updated_by == "ops@acme.com" and noted == [True]
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

    switch_off("acme")
    first = await w.run_pass(w.EmbeddingWorkerConfig(batch_size=10))
    assert first.embedded_count == 1
    assert {r["org"] for r in inserted} == {"beta"}  # acme's stub untouched: no retry, no attempt
    switch_off()
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

    async def nothing_waited_long(*a: Any, **k: Any) -> int:
        return 0

    monkeypatch.setattr(q, "drop_waiting", nothing_waited_long)
    monkeypatch.setattr(q, "drop_expiring", nothing_waited_long)
    monkeypatch.setattr("server.core.ch.get_client", lambda: object())

    switch_off("acme")
    await w.run_pass(w.ExtractionQueueConfig())
    assert done == ["q2"] and errors == []
    switch_off()
    await w.run_pass(w.ExtractionQueueConfig())
    assert done == ["q2", "q1"] and errors == []


@pytest.mark.parametrize(
    "error, waits",
    [
        (customer.ModelCallsDisabled, True),
        (customer.SwitchUnread, True),  # the switch went unread after the claim
        (customer.Unattributed, False),  # names no customer: it never will, so it fails
    ],
)
async def test_a_claimed_request_the_switch_holds_waits_and_one_naming_no_customer_fails(
    monkeypatch: pytest.MonkeyPatch, error: type[Exception], waits: bool
) -> None:
    from server.memory.app import extraction_queue as q
    from server.memory.workers import extraction_queue as w

    errors: list[str] = []
    held: list[str] = []

    async def claim_batch(**_: Any) -> list[Any]:
        return [{"org": "acme", "namespace": "n", "queue_id": "q1", "enqueued_at": 1}]

    async def replay_row(row: dict[str, Any]) -> int:
        raise error("held")

    async def mark_error(row: dict[str, Any], **_: Any) -> str:
        errors.append(row["queue_id"])
        return "error"

    async def mark_held(row: dict[str, Any], **_: Any) -> None:
        held.append(row["queue_id"])

    async def nothing(*a: Any, **k: Any) -> int:
        return 0

    monkeypatch.setattr(q, "claim_batch", claim_batch)
    monkeypatch.setattr(q, "replay_row", replay_row)
    monkeypatch.setattr(q, "mark_error", mark_error)
    monkeypatch.setattr(q, "mark_held", mark_held)
    monkeypatch.setattr(q, "drop_expiring", nothing)
    monkeypatch.setattr("server.core.ch.get_client", lambda: object())
    result = await w.run_pass(w.ExtractionQueueConfig())
    if waits:
        assert errors == [] and held == ["q1"] and result.skipped == 1
    else:
        assert errors == ["q1"] and held == [] and result.skipped == 0


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
    switch_off("acme")
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


def test_no_refusal_and_no_unreadable_switch_is_a_provider_failure(
    switch: _Switch, monkeypatch
) -> None:
    from server.llm import openai as chat_mod

    provider_health.reset()
    sent = _Recorder()
    monkeypatch.setattr(chat_mod, "_http_client", lambda: sent)
    client = chat_mod.OpenAILLMClient(model="gpt-4o-mini", api_key="k")
    switch.off = {"acme"}
    _request("acme")
    with pytest.raises(customer.ModelCallsDisabled):
        asyncio.run(client.complete("s", "u"))
    switch.down = True
    # Never read, and the read fails: fail closed.
    switch_unread()
    with pytest.raises(BackendUnavailableError):
        asyncio.run(client.complete("s", "u"))
    monkeypatch.setattr(settings, "inference_customer_header", "X-Customer")
    genai._identity.set(None)
    with pytest.raises(customer.Unattributed):
        asyncio.run(client.complete("s", "u"))
    assert sent.calls == [] and "openai:chat" not in provider_health.status().get("providers", {})


def test_a_failed_read_waits_its_turn_and_the_copy_serves_until_too_old(switch: _Switch) -> None:
    # A copy with acme switched off, read just now and due again; the read fails.
    customer._switch._reset_for_tests(frozenset({"acme"}), read_age_s=0.0, due_in_s=0.0)
    switch.down = True
    assert asyncio.run(customer.switched_off("acme")) is True  # the last copy serves
    assert asyncio.run(customer.switched_off("acme")) is True
    assert switch.reads == 1  # the failed read is not tried again before its turn


def test_the_refresh_must_be_shorter_than_the_stale_bound(monkeypatch) -> None:
    from server.settings import Settings

    monkeypatch.setenv("LOOM_MODEL_CALLS_POLICY_REFRESH_SECONDS", "60")
    monkeypatch.setenv("LOOM_MODEL_CALLS_POLICY_MAX_STALE_SECONDS", "60")
    with pytest.raises(ValueError, match="LOOM_MODEL_CALLS_POLICY_REFRESH_SECONDS"):
        Settings()


@pytest.mark.parametrize("org", ["", "-", "*", " acme"])
def test_the_ops_take_one_org(org: str) -> None:
    from server.core.errors import ValidationError
    from server.identity.api.models_model_calls import OrgModelCallsInput, OrgSetModelCallsInput
    from server.identity.app import model_calls

    with pytest.raises(ValidationError):
        asyncio.run(
            model_calls.handle_org_set_model_calls(OrgSetModelCallsInput(org=org, enabled=False))
        )
    with pytest.raises(ValidationError):
        asyncio.run(model_calls.handle_org_model_calls(OrgModelCallsInput(org=org)))


class _DropCH:
    """Answers the drop query with `rows`; records each insert by table."""

    def __init__(self, rows: list[dict[str, Any]], *, fail_drops: bool = False) -> None:
        self.rows, self.fail_drops = rows, fail_drops
        self.sql, self.params = "", {}
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []

    async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        self.sql, self.params = sql, params
        return self.rows

    async def insert(self, table: str, rows: list[dict[str, Any]], **_: Any) -> None:
        if self.fail_drops and table == "loom.memory_drops":
            raise BackendUnavailableError("down")
        self.inserts.append((table, rows))


_WAITING = [{"org": "acme", "namespace": "n", "queue_id": "q1", "status": "pending"}]


def test_a_dropped_request_is_marked_failed_after_its_drop_record_is_written() -> None:
    from server.memory.app import extraction_queue as q

    ch = _DropCH(_WAITING)
    n = asyncio.run(
        q.drop_waiting(frozenset({"acme"}), older_than_days=7, ch=ch, namespace_filter="n")
    )
    assert n == 1 and ch.params["orgs"] == ["acme"] and ch.params["ns_f"] == "n"
    [(t1, [drop]), (t2, [row])] = ch.inserts
    assert t1 == "loom.memory_drops" and t2 == q.TABLE
    assert (drop["org"], drop["stage"], drop["reason"], drop["item"]) == (
        "acme",
        "extraction_queue",
        "model_calls_off",
        "q1",
    )
    assert row["status"] == q.STATUS_FAILED and "switched off" in row["last_error"]
    assert asyncio.run(q.drop_waiting(frozenset(), older_than_days=7, ch=ch)) == 0


def test_a_request_whose_drop_record_fails_stays_pending() -> None:
    from server.memory.app import extraction_queue as q

    ch = _DropCH(_WAITING, fail_drops=True)
    with pytest.raises(BackendUnavailableError):
        asyncio.run(q.drop_waiting(frozenset({"acme"}), older_than_days=7, ch=ch))
    assert ch.inserts == []  # not marked failed: the next pass drops it with its record


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
def test_the_wait_counts_from_the_switch_off_when_it_is_later_than_the_request() -> None:
    """acme's old request waited 7 days past acme's switch-off: dropped.
    gamma's is as old, but gamma was switched off 2 days ago: it waits.
    acme's 3-day-old request waits too."""
    from server.memory.app import extraction_queue as q

    ch = _DropCH([])
    asyncio.run(q.drop_waiting(frozenset({"acme", "gamma"}), older_than_days=7, ch=ch))
    queued = ", ".join(
        f"('{org}', 'n', 'a', '{qid}', '', 1, 1, 'pending', now64(3) - toIntervalDay({age}), 1)"
        for org, qid, age in (("acme", "old", 20), ("gamma", "old", 20), ("acme", "new", 3))
    )
    switched = ", ".join(
        f"('{org}', 0, '', 'admin', now64(3) - toIntervalDay({age}), 0, '', 1)"
        for org, age in (("acme", 10), ("gamma", 2))
    )
    found = run(
        [
            ddl("memory_extraction_queue"),
            ddl("org_model_calls"),
            "INSERT INTO loom.memory_extraction_queue (org, namespace, agent, queue_id, payload,"
            f" message_count, content_chars, status, enqueued_at, version) VALUES {queued}",
            f"INSERT INTO loom.org_model_calls VALUES {switched}",
        ],
        ch.sql,
        ch.params,
    )
    assert [(r["org"], r["queue_id"]) for r in found] == [("acme", "old")]


async def test_the_extraction_queue_drops_with_the_configured_wait(monkeypatch) -> None:
    from server.memory.app import extraction_queue as q
    from server.memory.workers import extraction_queue as w

    asked: list[tuple[frozenset[str], int]] = []

    async def drop_waiting(orgs: frozenset[str], *, older_than_days: int, **_: Any) -> int:
        asked.append((orgs, older_than_days))
        return 2

    async def claim_batch(**_: Any) -> list[Any]:
        return []

    async def nothing(*a: Any, **k: Any) -> int:
        return 0

    monkeypatch.setattr(q, "drop_waiting", drop_waiting)
    monkeypatch.setattr(q, "drop_expiring", nothing)
    monkeypatch.setattr(q, "claim_batch", claim_batch)
    monkeypatch.setattr(settings, "model_calls_off_max_wait_days", 3)
    monkeypatch.setattr("server.core.ch.get_client", lambda: object())
    switch_off("acme")
    result = await w.run_pass(w.ExtractionQueueConfig())
    assert asked == [(frozenset({"acme"}), 3)] and result.dropped == 2


class _Store:
    """loom.org_model_calls as the handlers read and write it."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def insert_row(self, row: dict[str, Any], **_: Any) -> None:
        self.rows.append(row)

    async def get(self, org: str, **_: Any) -> dict[str, Any] | None:
        mine = [r for r in self.rows if r["org"] == org]
        return max(mine, key=lambda r: r["version"]) if mine else None


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> _Store:
    from contextlib import asynccontextmanager

    from server.auth import grant_claims
    from server.identity.app import model_calls

    table = _Store()

    @asynccontextmanager
    async def held(org: str) -> Any:
        yield

    monkeypatch.setattr(model_calls.repo, "insert_row", table.insert_row)
    monkeypatch.setattr(model_calls.repo, "get", table.get)
    monkeypatch.setattr(grant_claims, "model_calls_change", held)
    return table


def _as(subject: str, *roles: Any) -> None:
    from server.auth.context import set_current_auth
    from server.auth.jwt import AuthInfo

    set_current_auth(
        AuthInfo(subject=subject, org="acme", namespaces=(), roles=roles, is_service_account=False)
    )


def _set(**body: Any) -> Any:
    from server.identity.api.models_model_calls import OrgSetModelCallsInput
    from server.identity.app import model_calls

    return asyncio.run(model_calls.handle_org_set_model_calls(OrgSetModelCallsInput(**body)))


def test_a_platform_lock_holds_against_the_orgs_own_admin(store: _Store) -> None:
    from server.auth.context import set_current_auth
    from server.core.errors import AuthInsufficientError
    from server.identity.api.models_model_calls import OrgModelCallsInput
    from server.identity.app import model_calls
    from server.operations.base import Role

    try:
        _as("ops@acme.com", Role.ORG_ADMIN)
        with pytest.raises(AuthInsufficientError, match="platform admin"):
            _set(org="acme", enabled=False, locked=True)  # an org admin cannot lock
        _as("root@platform", Role.PLATFORM_ADMIN)
        out = _set(org="acme", enabled=False, reason="offboarded", locked=True)
        assert (out.enabled, out.locked, out.locked_by) == (False, True, "root@platform")
        _as("ops@acme.com", Role.ORG_ADMIN)
        with pytest.raises(AuthInsufficientError, match="locked off by 'root@platform'"):
            _set(org="acme", enabled=True)  # nor switch it back on
        with pytest.raises(AuthInsufficientError):
            _set(org="acme", enabled=True, locked=False)  # nor unlock it
        read = asyncio.run(model_calls.handle_org_model_calls(OrgModelCallsInput(org="acme")))
        assert (read.enabled, read.locked, read.locked_by) == (False, True, "root@platform")
        _as("root@platform", Role.PLATFORM_ADMIN)
        out = _set(org="acme", enabled=True, locked=False)
        assert (out.enabled, out.locked, out.locked_by) == (True, False, "")
        _as("ops@acme.com", Role.ORG_ADMIN)
        assert _set(org="acme", enabled=False).enabled is False  # unlocked: its admin may again
    finally:
        set_current_auth(None)


def test_a_lock_never_stops_the_orgs_admin_switching_off(store: _Store) -> None:
    from server.auth.context import set_current_auth
    from server.operations.base import Role

    try:
        _as("root@platform", Role.PLATFORM_ADMIN)
        _set(org="acme", enabled=True, locked=True)  # locked on, say for a contract
        _as("ops@acme.com", Role.ORG_ADMIN)
        out = _set(org="acme", enabled=False, reason="our call")
        assert (out.enabled, out.locked, out.locked_by) == (False, True, "root@platform")
    finally:
        set_current_auth(None)


def test_a_platform_admins_change_keeps_a_lock_it_does_not_name(store: _Store) -> None:
    from server.auth.context import set_current_auth
    from server.operations.base import Role

    try:
        _as("root@platform", Role.PLATFORM_ADMIN)
        _set(org="acme", enabled=False, locked=True)
        out = _set(org="acme", enabled=False, reason="still off")
        assert (out.locked, out.locked_by) == (True, "root@platform")
        out = _set(org="acme", enabled=True, reason="back on")
        assert (out.enabled, out.locked, out.locked_by) == (True, True, "root@platform")
        out = _set(org="acme", enabled=True, locked=False)
        assert (out.enabled, out.locked) == (True, False)
    finally:
        set_current_auth(None)


def test_an_org_still_off_keeps_the_time_it_was_switched_off(store: _Store) -> None:
    """Queued work waits from that time: a lock or a new reason must not restart it."""
    from datetime import datetime

    from server.auth.context import set_current_auth
    from server.operations.base import Role

    then = datetime(2026, 1, 1)
    try:
        _as("ops@acme.com", Role.ORG_ADMIN)
        _set(org="acme", enabled=False)
        store.rows[-1]["updated_at"] = then
        _as("root@platform", Role.PLATFORM_ADMIN)
        assert _set(org="acme", enabled=False, locked=True).updated_at == then
        assert _set(org="acme", enabled=False, reason="offboarded").updated_at == then
        assert _set(org="acme", enabled=True).updated_at != then
        assert _set(org="acme", enabled=False).updated_at != then  # off again: a new wait
    finally:
        set_current_auth(None)


def test_reading_the_setting_sees_the_change_just_made(monkeypatch) -> None:
    from server.core import ch
    from server.identity.api.models_model_calls import OrgModelCallsInput
    from server.identity.app import model_calls

    seen: list[bool] = []

    async def get(org: str, **_: Any) -> None:
        seen.append(ch.sequential_reads_var.get())

    monkeypatch.setattr(model_calls.repo, "get", get)
    asyncio.run(model_calls.handle_org_model_calls(OrgModelCallsInput(org="acme")))
    assert seen == [True]


def test_a_change_read_before_a_lock_cannot_write_the_lock_away(monkeypatch) -> None:
    """The org admin's change reads the switch (unlocked), then a platform admin
    locks it off. Held from read to write, the admin's change ends first and the
    lock lands after it; unheld, the admin's row would write `locked = 0`."""
    from server.auth import grant_claims
    from server.auth.context import set_current_auth
    from server.identity.api.models_model_calls import OrgSetModelCallsInput
    from server.identity.app import model_calls
    from server.operations.base import Role
    from tests.unit.claims_fake import MemoryClaims

    table = _Store()
    reads = 0

    async def slow_get(org: str, **_: Any) -> dict[str, Any] | None:
        nonlocal reads
        reads += 1
        row = await table.get(org)
        if reads == 1:
            await asyncio.sleep(0.05)  # the platform admin's change starts meanwhile
        return row

    monkeypatch.setattr(model_calls.repo, "insert_row", table.insert_row)
    monkeypatch.setattr(model_calls.repo, "get", slow_get)
    monkeypatch.setattr(grant_claims, "take", MemoryClaims().take)

    async def change(subject: str, role: Any, **body: Any) -> None:
        _as(subject, role)
        await model_calls.handle_org_set_model_calls(OrgSetModelCallsInput(org="acme", **body))

    async def both() -> None:
        admin = asyncio.create_task(change("ops@acme.com", Role.ORG_ADMIN, enabled=True))
        await asyncio.sleep(0.01)
        await change("root@platform", Role.PLATFORM_ADMIN, enabled=False, locked=True)
        await admin

    try:
        asyncio.run(both())
    finally:
        set_current_auth(None)
    last = max(table.rows, key=lambda r: r["version"])
    assert (last["enabled"], last["locked"], last["locked_by"]) == (0, 1, "root@platform")


def test_a_reembed_leaves_a_switched_off_orgs_rows_and_counts_them(monkeypatch) -> None:
    from server.memory.app import reembed

    seen: list[frozenset[str]] = []

    async def table(ch: Any, embedder: Any, name: str, **kw: Any) -> int:
        seen.append(kw["exclude_orgs"])
        return 4

    async def waiting(ch: Any, name: str, *a: Any) -> int:
        return 3

    async def width(table: str, dim: int) -> None:
        return None

    monkeypatch.setattr(reembed, "reembed_table", table)
    monkeypatch.setattr(reembed, "_waiting", waiting)
    monkeypatch.setattr("server.core.vectors.codes_state.refuse_width", width)
    switch_off("acme")
    lines: list[str] = []
    embedder = type("E", (), {"model_name": "m", "dim": 8})()
    out = asyncio.run(reembed.reembed_all(object(), embedder, log=lines.append))
    assert seen and all(s == frozenset({"acme"}) for s in seen)
    assert out["skipped_switched_off"] == 3 * len(reembed._TABLES)
    assert any("switched-off org" in line for line in lines)  # the CLI prints it


def test_without_a_header_a_batch_splits_by_org_only_when_one_is_switched_off() -> None:
    assert settings.inference_customer_header == ""
    rows = [customer.Attribution(org) for org in ("acme", "beta", "acme")]
    switch_off()
    assert asyncio.run(customer.groups(rows)) == {customer.Attribution(): [0, 1, 2]}
    switch_off("acme")
    assert asyncio.run(customer.groups(rows)) == {rows[0]: [0, 2], rows[1]: [1]}


def test_a_reembed_page_without_a_header_leaves_the_rows_of_an_org_switched_off_mid_run(
    monkeypatch,
) -> None:
    from server.memory.app import reembed

    assert settings.inference_customer_header == ""
    table = "conversation_embeddings"
    text = reembed._TABLES[table]["text"]
    sent: list[str] = []

    class _Emb:
        model_name, dim = "m", 2

        async def embed_many(self, texts: list[str]) -> list[list[float]]:
            await customer.require()
            sent.append(customer.current())
            return [[0.5, 0.5] for _ in texts]

    class _CH:
        rows: list[Any] = []

        async def insert(self, table: str, rows: list[Any], **_: Any) -> None:
            self.rows = rows

    monkeypatch.setattr(
        reembed, "rewrite_row", lambda r, cfg, model, dim, vec, kvec: {**r, "vec": vec}
    )
    chunk = [{"org": "acme", text: "a"}, {"org": "beta", text: "b"}]
    ch = _CH()
    switch_off("acme")
    left = asyncio.run(reembed._rewrite_page(ch, _Emb(), table, chunk, key_only=False))
    assert left == {"acme": 1} and sent == ["beta"]
    assert [r["org"] for r in ch.rows] == ["beta"]


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
def test_reembed_delete_old_keeps_the_vectors_of_every_org_it_left(monkeypatch) -> None:
    """acme was off when the run began, gamma was switched off during it: both
    keep their only vectors; beta's replaced rows are deleted."""
    from server.memory.app import reembed

    async def table(ch: Any, embedder: Any, name: str, **kw: Any) -> int:
        kw["left_orgs"].add("gamma")
        return 0

    async def waiting(*a: Any) -> int:
        return 1

    async def width(table: str, dim: int) -> None:
        return None

    deletes: list[tuple[str, dict[str, Any]]] = []

    class _CH:
        async def execute(self, sql: str, params: dict[str, Any]) -> None:
            deletes.append((sql, params))

    monkeypatch.setattr(reembed, "reembed_table", table)
    monkeypatch.setattr(reembed, "_waiting", waiting)
    monkeypatch.setattr("server.core.vectors.codes_state.refuse_width", width)
    switch_off("acme")
    embedder = type("E", (), {"model_name": "new", "dim": 2})()
    asyncio.run(reembed.reembed_all(_CH(), embedder, delete_old=True, log=lambda *_: None))
    sql, params = next((s, p) for s, p in deletes if "loom.entity_vectors" in s)
    create = ddl("entity_vectors").replace("CODEC(Quantized('rabitq', 3072, 0), LZ4)", "")
    rows = ", ".join(
        f"('{org}', 'n', 'a', 'u', 'e', [0.5, 0.5], 'old', 2, '{org}-k', now(), NULL)"
        for org in ("acme", "beta", "gamma")
    )
    left = run(
        [create, f"INSERT INTO loom.entity_vectors VALUES {rows}", sql],
        "SELECT org FROM loom.entity_vectors ORDER BY org",
        params,
    )
    assert [r["org"] for r in left] == ["acme", "gamma"]


def test_the_reembed_filter_leaves_out_switched_off_orgs() -> None:
    from server.memory.app import reembed

    sql, params = reembed.where_sql(
        "memory_embeddings",
        ["org", "tombstoned_at"],
        "m",
        org=None,
        namespace=None,
        exclude_orgs=frozenset({"acme"}),
    )
    assert "NOT has({off:Array(String)}, org)" in sql and params["off"] == ["acme"]


def test_a_switch_gone_unread_mid_rebuild_batch_is_no_provider_failure(monkeypatch) -> None:
    from server.insights.app import provider_rebuild_batch as batch

    monkeypatch.setattr(
        batch,
        "_inputs",
        lambda table, rows, source, held: ([r["text"] for r in rows], [(0, "embedding")]),
    )

    class _E:
        model_name, dim = "fake", 2

        async def embed_many(self, texts: list[str]) -> list[list[float]]:
            raise customer.SwitchUnread("stale")

    switch_off()
    with pytest.raises(customer.SwitchUnread):  # the step waits; the job does not fail
        asyncio.run(batch.rebuild_batch("memory_embeddings", [{"org": "acme", "text": "a"}], _E()))


def test_a_rebuild_stages_a_switched_off_orgs_rows_without_a_vector(monkeypatch) -> None:
    from server.insights.app import provider_rebuild_batch as batch

    monkeypatch.setattr(
        batch,
        "_inputs",
        lambda table, rows, source, held: (
            [r["text"] for r in rows],
            [(i, "embedding") for i in range(len(rows))],
        ),
    )
    monkeypatch.setattr(
        batch, "_empty_target_row", lambda t, row, m: {**row, "embedding": [], "embedding_dim": 0}
    )

    class _E:
        model_name, dim = "fake", 2

        async def embed_many(self, texts: list[str]) -> list[list[float]]:
            await customer.require()
            return [[0.5, 0.5] for _ in texts]

    rows = [{"org": "acme", "text": "a"}, {"org": "beta", "text": "b"}]
    switch_off("acme")
    out = asyncio.run(batch.rebuild_batch("memory_embeddings", rows, _E()))
    assert out.rows_waiting == 1
    assert out.retry_after_s == 30.0
    assert (out.rows[0]["embedding"], out.rows[0]["embedding_dim"]) == ([], 0)
    assert (out.rows[1]["embedding"], out.rows[1]["embedding_dim"]) == ([0.5, 0.5], 2)


def test_the_dupcheck_refit_command_sends_nothing_for_a_switched_off_org(monkeypatch) -> None:
    import argparse

    from server.cli import tracker as cli
    from server.tracker.app import dupcheck_fit as df

    sent: list[str] = []

    async def refit(src: Any, repo: Any, org: str, ns: str, **k: Any) -> Any:
        await customer.require()  # what the replay's vector embed checks first
        sent.append(org)

    monkeypatch.setattr(cli, "run_connected", lambda run, **_declared: asyncio.run(run(None)))
    monkeypatch.setattr("server.tracker.app.source.tracker_source", lambda org, ns: object())
    monkeypatch.setattr(df, "state_repo", lambda: object())
    monkeypatch.setattr(df, "refit_namespace", refit)
    switch_off("acme")
    args = argparse.Namespace(org="acme", namespace="t-a", dry_run=False, negatives=None, out=None)
    with pytest.raises(customer.ModelCallsDisabled):
        cli._cmd_tracker_dupcheck_fit(args)
    assert sent == []
