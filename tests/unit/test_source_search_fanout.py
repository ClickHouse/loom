"""Unified search retains checked knowledge after sibling work completes."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from server.core.diagnostics import codes_only
from server.knowledge.api.models_conversations import ConversationSearchOutput
from server.knowledge.api.models_knowledge import KnowledgeSearchOutput, _SearchHit
from server.knowledge.app import knowledge_search, repo_docs, source_access
from server.knowledge.app.source_corpus import LostCorpus
from server.memory.api.models_search import MemorySearchOutput
from server.memory.app.loom_search import fanout


def hit(uri, score):
    return _SearchHit(
        source_uri=uri,
        content=uri,
        chunk_idx=0,
        tags=[],
        distance=1 - score,
        similarity=score,
        score=score,
        namespace="home",
    )


def install(monkeypatch, *, ordinary_needs_widen=False, fail_ordinary_wider=False):
    events = []
    calls = []
    ready = asyncio.Event()
    state = {"revoked": False}

    async def resolve(inp):
        return knowledge_search.SearchRequest(inp.model_copy(deep=True), (inp.namespace,), ())

    async def prepare(org, namespaces):
        events.append("prepare")
        return source_access.ReadPlan(org, marked=True)

    async def leaf(request, plan):
        events.append(("knowledge", codes_only()))
        calls.append((request.input.top_k, plan.marked, request.namespaces))
        if fail_ordinary_wider and request.input.top_k > 2 and not plan.marked:
            raise RuntimeError("PRIVATE-WIDER-FAILURE")
        mixed = KnowledgeSearchOutput(
            results=[hit("private", 0.9)], embedding_model="fake", reranked_by="private-rank"
        )
        ordinary_rows = [hit("ordinary-a", 0.5), hit("ordinary-b", 0.4)]
        if ordinary_needs_widen:
            other_rule = hit("other-agent-rule", 0.8)
            other_rule.tags = ["source-kind:assistant_turn", "agent:another-bot"]
            ordinary_rows.insert(0, other_rule)
        ordinary = KnowledgeSearchOutput(
            results=ordinary_rows[: request.input.top_k],
            embedding_model="fake",
            stale_dropped=4,
        )

        async def check():
            if state["revoked"]:
                raise LostCorpus()

        async def render(*_args):
            events.append("ordinary")
            return ordinary

        return knowledge_search.CheckedSearch(
            mixed if plan.marked else ordinary,
            SimpleNamespace(check=check, fresh=lambda: not state["revoked"])
            if plan.marked
            else None,
            SimpleNamespace(marked=plan.marked, render=render),
            [],
            4,
        )

    original = repo_docs.run_repo_search

    async def repository(request, plan):
        result = await original(request, plan)
        ready.set()
        return result

    async def memory(*_args, **_kwargs):
        events.append(("memory", codes_only()))
        await ready.wait()
        state["revoked"] = True
        return MemorySearchOutput(results=[], embedding_model="fake")

    async def namespaces(org, namespace, *_args, **_kwargs):
        return [namespace], []

    async def profile(*_args, **_kwargs):
        events.append(("profile", codes_only()))
        return []

    async def conversations(*_args, **_kwargs):
        return ConversationSearchOutput(hits=[])

    async def tracker(*_args, **_kwargs):
        return {}

    monkeypatch.setattr(knowledge_search, "resolve_search", resolve)
    monkeypatch.setattr(knowledge_search, "run_checked_search", leaf)
    monkeypatch.setattr(source_access, "prepare", prepare)
    monkeypatch.setattr(fanout, "prepare", prepare)
    monkeypatch.setattr(repo_docs, "run_repo_search", repository)
    monkeypatch.setattr("server.auth.fanout.namespaces_to_search", namespaces)
    monkeypatch.setattr(fanout, "memory_with_repo_decisions", memory)
    monkeypatch.setattr(fanout, "search_profile", profile)
    monkeypatch.setattr(fanout, "_conversation_leg", conversations)
    monkeypatch.setattr(fanout, "tracker_leg", tracker)
    monkeypatch.setattr(fanout, "request_scope", lambda: SimpleNamespace(narrowed=False))
    monkeypatch.setattr(fanout, "answer_first", lambda payload, *_args, **_kwargs: payload)
    return SimpleNamespace(events=events, calls=calls, state=state)


async def run():
    return await fanout.search_sections(
        org="acme",
        namespace="home",
        agent="reader",
        query="cache",
        top_k=2,
        tags=[],
        namespace_scope="this",
        recipe="",
        bodies="excerpt",
        page="",
        explain=False,
        repo_namespace="",
        tracker_plan=None,
    )


async def test_sibling_revocation_restores_rows_and_metadata_before_final_assembly(monkeypatch):
    env = install(monkeypatch)
    payload = await run()
    know = payload["knowledge"]
    assert [h["source_uri"] for h in know["results"]] == ["ordinary-a", "ordinary-b"]
    assert know["reranked_by"] == "" and know["stale_dropped"] == 4
    assert env.events.count("ordinary") == 1


async def test_parent_prepares_before_all_sibling_providers(monkeypatch):
    env = install(monkeypatch)
    await run()
    prepared = env.events.index("prepare")
    assert all(prepared < i and e[1] for i, e in enumerate(env.events) if isinstance(e, tuple))
    assert not codes_only()


async def test_revoked_first_pass_refills_from_one_ordinary_only_wider_query(monkeypatch):
    from server.memory.domain.assistant_rules import hits_to_ask

    env = install(monkeypatch, ordinary_needs_widen=True)
    payload = await run()
    know = payload["knowledge"]
    assert [h["source_uri"] for h in know["results"]] == ["ordinary-a", "ordinary-b"]
    assert know["reranked_by"] == "" and know["stale_dropped"] == 4
    assert env.calls == [(2, True, ("home",)), (hits_to_ask(2), False, ("home",))]
    assert env.events.count("prepare") == 1


async def test_failed_ordinary_widen_keeps_ordinary_first_rows_and_metadata(monkeypatch, caplog):
    env = install(monkeypatch, ordinary_needs_widen=True, fail_ordinary_wider=True)
    payload = await run()
    know = payload["knowledge"]
    assert [h["source_uri"] for h in know["results"]] == ["ordinary-a"]
    assert know["reranked_by"] == "" and know["stale_dropped"] == 4
    assert len(env.calls) == 2 and not env.calls[-1][1]
    assert "PRIVATE-WIDER-FAILURE" not in caplog.text


async def test_base_scope_failure_leaves_other_search_sections_available(monkeypatch):
    install(monkeypatch)

    async def fail(inp):
        raise ValueError("knowledge scope refused")

    async def memory(*_args, **_kwargs):
        return MemorySearchOutput(results=[], embedding_model="fake")

    async def profile(*_args, **_kwargs):
        return [{"key": "ordinary-profile", "content": "still available"}]

    monkeypatch.setattr(knowledge_search, "resolve_search", fail)
    monkeypatch.setattr(fanout, "memory_with_repo_decisions", memory)
    monkeypatch.setattr(fanout, "search_profile", profile)
    payload = await run()
    assert payload["knowledge"]["error"] and payload["knowledge"]["results"] == []
    assert payload["memory"]["results"] == []
    assert payload["profile"]["results"] == [
        {"key": "ordinary-profile", "content": "still available"}
    ]
