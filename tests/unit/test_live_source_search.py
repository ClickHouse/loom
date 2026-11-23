"""Live search owns private work until cancellation or final publication."""

from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack, asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from server.auth.session_protection import protected_for, request_declaration
from server.core import request_tail, request_work
from server.core.diagnostics import codes_only
from server.knowledge.app import live_search
from server.knowledge.domain.mcp_identity import resource_uri
from server.memory.api.models_loom_search import SearchInput
from tests.unit.principals import acting_as
from tests.unit.test_mcp_source_live import live_setup as live_setup
from tests.unit.test_mcp_source_live import live_tools as live_tools
from tests.unit.test_mcp_source_read_policy import reader as reader
from tests.unit.test_mcp_source_sync import RESOURCE
from tests.unit.test_mcp_source_sync import harness as harness
from tests.unit.test_mcp_source_sync import upstream as upstream


@asynccontextmanager
async def _request(auth, *, bag=True, tail=True, protected=True):
    async with AsyncExitStack() as stack:
        stack.enter_context(request_work.http_owner({}))
        headers = [(b"x-loom-session-protection", b"v1")] if protected else []
        stack.enter_context(request_declaration({"headers": headers}))
        stack.enter_context(acting_as(auth))
        work = request_work.RequestWork()
        if bag:
            stack.enter_context(request_work.bind(work))
        if tail:
            await stack.enter_async_context(request_tail.streaming_scope())
        yield work


@pytest.fixture
def generic(live_tools, monkeypatch):
    from server.settings import settings

    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "knowledge_mirrored_sources_enabled", True)
    source, auth = live_tools.h.source, live_tools.auth
    state = SimpleNamespace(
        auth=auth,
        source=source,
        sources=[source],
        events=[],
        resolutions=[],
        authorities=[],
        revoked=False,
        revocation_seen=False,
        block=False,
        started=asyncio.Event(),
        closed=asyncio.Event(),
        harness=live_tools.h,
    )

    def private(stage):
        state.events.append(stage)
        assert request_work.mirror_marked()
        assert request_work.http_mirror_marked()
        if stage in ("subject", "sources", "latch", "resolve", "provider"):
            assert request_tail.required_active()
        assert protected_for(auth)
        assert codes_only()

    async def own_subject(actual):
        private("subject")
        assert actual is auth
        return "org", "owner"

    async def connected_sources(org, subject):
        private("sources")
        assert (org, subject) == ("org", "owner")
        return state.sources

    async def ensure(actual):
        private("latch")
        assert actual is auth

    actual_search = live_search.mcp_live.search
    actual_check = live_search.mcp_live.Contribution.check

    async def search(selected, actual, query, *, limit):
        private("resolve")
        assert selected is source and actual is auth
        assert query == "selected" and limit == 2
        state.resolutions.append((selected.namespace, selected.name, selected.source_id))
        private("authority")
        private("provider")
        state.started.set()
        try:
            if state.block:
                await asyncio.Event().wait()
            contribution = await actual_search(selected, actual, query, limit=limit)
            assert contribution is not None
            state.authorities.append(contribution)
            return contribution
        finally:
            state.closed.set()

    async def check(contribution):
        private("authority")
        if state.revoked:
            state.revocation_seen = True
            raise RuntimeError("authority revoked")
        private("membership")
        await actual_check(contribution)

    monkeypatch.setattr(live_search.connected_accounts, "own_subject", own_subject)
    monkeypatch.setattr(live_search.source_store, "connected_sources", connected_sources)
    monkeypatch.setattr(live_search.session_admission, "require", ensure)
    monkeypatch.setattr(live_search.mcp_live, "search", search)
    monkeypatch.setattr(live_search.mcp_live.Contribution, "check", check)
    return state


@pytest.mark.parametrize("missing", ["bag", "tail", "protection"])
async def test_live_search_requires_real_request_privacy_ownership(generic, missing):
    async with _request(
        generic.auth,
        bag=missing != "bag",
        tail=missing != "tail",
        protected=missing != "protection",
    ):
        assert await live_search.search("org", ["collection"], "selected", top_k=2) is None
    assert generic.events == []
    assert not generic.started.is_set()


async def test_live_search_keeps_only_asker_connected_authorized_sources_and_original_authority(
    generic,
):
    base = generic.source
    generic.sources = [
        replace(base, **change)
        for change in (
            {"namespace": "foreign"},
            {"credential_mode": "shared"},
            {"owner_subject": "other@example.test"},
            {"active": False},
            {"access": "shared"},
        )
    ] + [generic.source]
    async with _request(generic.auth):
        result = await live_search.search("org", ["collection"], "selected", top_k=2)
        assert result is not None
        assert len(generic.resolutions) == len(result.contributions) == 1
        assert result.contributions[0] is generic.authorities[0]
        assert result.contributions[0].source is generic.source
        assert result.contributions[0].auth is generic.auth
        assert generic.harness.reads == [RESOURCE]
        assert not generic.harness.doc_rows and not generic.harness.originals
        assert await live_search.publish(result) is result
        assert result.fresh()
        assert generic.harness.reads == [RESOURCE, RESOURCE]
        assert generic.events[:4] == ["subject", "sources", "latch", "resolve"]
        assert generic.events.count("provider") == generic.events.count("membership") == 1


async def test_live_search_expiry_joins_the_started_generic_call(generic, monkeypatch):
    generic.block = True
    monkeypatch.setattr(live_search.settings, "knowledge_live_search_timeout_s", 0)
    async with _request(generic.auth) as bag:
        task = asyncio.create_task(live_search.search("org", ["collection"], "selected", top_k=2))
        assert await task is None
        assert task.done()
        assert generic.started.is_set() and generic.closed.is_set()
        assert bag.mirror_marked


async def test_final_response_publication_refuses_revoked_live_authority(generic):
    sent = []

    async def app(scope, receive, send):
        result = await live_search.search("org", ["collection"], "selected", top_k=2)
        assert result is not None
        assert await live_search.publish(result) is result
        generic.revoked = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"private bytes"})

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    async with _request(generic.auth, tail=False):
        with pytest.raises(request_tail.RequiredTailError):
            await request_tail.RequestTailMiddleware(app)({"type": "http"}, receive, send)
    assert generic.events.count("authority") == 3
    assert generic.revocation_seen
    assert sent == []


def _indexed():
    return {
        "memory": {"results": [], "term_leg": "empty", "abstain": True},
        "knowledge": {"results": []},
        "profile": {"results": []},
        "conversations": {"results": []},
    }


@pytest.fixture
def indexed(monkeypatch):
    from server.auth import fanout
    from server.memory.app.loom_search import handler
    from server.observability import capture_consent
    from server.settings import settings

    monkeypatch.setattr(settings, "knowledge_gaps_enabled", False)

    async def namespaces(*args):
        return ["collection"], []

    async def nothing(*args, **kwargs):
        return None

    async def sections(**kwargs):
        return deepcopy(_indexed())

    monkeypatch.setattr(fanout, "namespaces_to_search", namespaces)
    monkeypatch.setattr(handler, "repo_namespace_in_scope", nothing)
    monkeypatch.setattr(handler, "prepare_tracker_search", nothing)
    monkeypatch.setattr(capture_consent, "narrow_to", nothing)
    monkeypatch.setattr(handler, "search_sections", sections)
    return handler


async def test_delivered_live_answer_prevents_an_otherwise_recorded_gap(
    generic, indexed, monkeypatch
):
    from server.knowledge.app import gaps
    from server.settings import settings

    misses = []
    monkeypatch.setattr(settings, "knowledge_gaps_enabled", True)
    monkeypatch.setattr(gaps, "reads_whole_namespace", lambda: True)
    monkeypatch.setattr(gaps, "record_miss", lambda **kwargs: misses.append(kwargs))
    request = SearchInput(org="org", namespace="collection", query="selected", top_k=2, live=True)
    indexed._record_gap_miss(request, _indexed(), "")
    assert len(misses) == 1
    async with _request(generic.auth):
        output = await indexed.handle_search(request)
    assert output.live is not None and output.live.results[0].id == resource_uri(
        generic.source.source_id, RESOURCE
    )
    assert output.memory == _indexed()["memory"]
    assert len(misses) == 1


async def test_live_mcp_output_protects_the_sdk_before_automatic_capture(
    generic, indexed, tmp_path, monkeypatch
):
    import httpx
    from loom.session_protection import AutomaticCaptureSuppressed, SessionProtection
    from loom.transport import Transport

    from server.memory.app.served_reads import search_query_keys
    from server.transport.mcp_outcome import _search_answer

    request = SearchInput(org="org", namespace="collection", query="selected", top_k=2, live=True)
    call = SimpleNamespace(
        arguments={"query": "selected", "top_k": 2}, rl_ns="collection", mcp_session_id=""
    )
    family = SessionProtection.create(base=tmp_path / "protection")
    assert not family.protected
    async with _request(generic.auth):
        output = await indexed.handle_search(request)
        answer = _search_answer(call, output)
    assert output.live is not None
    assert search_query_keys(request, output) == []
    assert json.loads(answer["content"][0]["text"]) == {"mirrored": True}
    assert answer["mirrored"] is answer["structuredContent"]["mirrored"] is True
    assert answer["structuredContent"]["live"] == output.model_dump()["live"]
    rendered = answer["content"][1]["text"]
    for field in ("title", "url", "content"):
        assert getattr(output.live.results[0], field) in rendered

    envelope = {"jsonrpc": "2.0", "id": 1, "result": answer}
    dispatched = []

    def respond(actual):
        dispatched.append(actual.url.path)
        return httpx.Response(200, json=envelope if actual.url.path == "/mcp" else {"ok": True})

    # Environment proxy mounts must not bypass the mock transport.
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("no_proxy", "*")
    transport = Transport("http://inert.invalid", session_protection=family)
    transport._client._transport = httpx.MockTransport(respond)
    try:
        with family.automatic():
            observed = await transport.post("/mcp", {"method": "tools/call"})
            assert observed == envelope
            assert family.protected
            with pytest.raises(AutomaticCaptureSuppressed):
                await transport.post("/v1/memory.set", {"value": "automatic"})
        assert dispatched == ["/mcp"]
        assert await transport.post("/v1/memory.set", {"value": "explicit"}) == {"ok": True}
        assert dispatched == ["/mcp", "/v1/memory.set"]
        assert SessionProtection.open(family.pointer()).protected
        directory = family.base / family.id
        assert sorted(path.name for path in directory.iterdir()) == ["root.json", "state"]
        assert json.loads((directory / "root.json").read_bytes()) == {"id": family.id, "version": 1}
        assert (directory / "state").read_bytes() == b"0\n1\n"
    finally:
        await transport.close()


@pytest.mark.parametrize("outcome", ["indexed_failure", "caller_cancel"])
async def test_handler_cancels_and_joins_started_live_work(generic, indexed, monkeypatch, outcome):
    generic.block = True

    async def sections(**kwargs):
        await generic.started.wait()
        if outcome == "indexed_failure":
            raise ValueError("indexed failed")
        await asyncio.Event().wait()

    monkeypatch.setattr(indexed, "search_sections", sections)
    request = SearchInput(org="org", namespace="collection", query="selected", top_k=2, live=True)
    async with _request(generic.auth):
        task = asyncio.create_task(indexed.handle_search(request))
        await asyncio.wait_for(generic.started.wait(), 1)
        if outcome == "caller_cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(ValueError, match="indexed failed"):
                await task
        assert task.done() and generic.closed.is_set()


@pytest.mark.parametrize(
    "options", [{}, {"live": True, "tags": ["tag"]}, {"live": True, "page": "page"}]
)
async def test_default_filtered_and_page_calls_do_not_start_live_work(generic, indexed, options):
    request = SearchInput(org="org", namespace="collection", query="selected", top_k=2, **options)
    async with _request(generic.auth):
        output = await indexed.handle_search(request)
    assert output.live is None
    assert generic.events == []
    assert not generic.started.is_set()
