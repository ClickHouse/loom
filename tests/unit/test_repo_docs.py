"""A repository's documents live in its docs namespace — the
trunk branch's code namespace — carry the repository's access, and are
read where agents work: loom_search under a code scope merges them into its
knowledge section, and the orientation context pack names them. Every read
passes the gate for the caller first; any failure answers nothing."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

import server.code.app.repo_scope as repo_scope
import server.knowledge.app.repo_docs as rd
from server.code.domain.index import repos
from server.code.domain.scope import reset_code_scope, set_code_scope
from server.knowledge.api.models_knowledge import (
    KnowledgeSearchInput,
    KnowledgeSearchOutput,
    _SearchHit,
)

ORG = "acme"


def _cfg(ns: str, repo: str, branch: str) -> Any:
    return SimpleNamespace(org=ORG, namespace=ns, repo=repo, git_branch=branch)


ENTRIES = [
    _cfg("code-kj-26-7", "Acme/kj", "26.7"),
    _cfg("code-kj-26-10", "Acme/kj", "26.10"),
    _cfg("code-kj", "Acme/kj", "master"),
    _cfg("code-w-dev", "Acme/w", "dev"),
    _cfg("code-w-26-1", "Acme/w", "26.1"),
    _cfg("code-z-b", "Acme/z", "beta"),
    _cfg("code-z-a", "Acme/z", "alpha"),
]


def _hit(uri: str, score: float, ns: str = "", chunk: int = 0) -> _SearchHit:
    return _SearchHit(
        source_uri=uri,
        chunk_idx=chunk,
        content=uri,
        tags=[],
        distance=1 - score,
        similarity=score,
        namespace=ns,
        score=score,
    )


@pytest.fixture(autouse=True)
def _repos(monkeypatch):
    monkeypatch.setattr(repos, "configured_repos", lambda: list(ENTRIES))
    rd.reset_cache_for_tests()
    yield
    rd.reset_cache_for_tests()


def test_the_docs_namespace_is_the_trunk_branchs_the_page_order() -> None:
    assert repos.docs_namespaces() == {
        (ORG, "Acme/kj"): "code-kj",  # master first
        (ORG, "Acme/w"): "code-w-26-1",  # then release branches, newest first
        (ORG, "Acme/z"): "code-z-a",  # then by name
    }
    # every branch of a repository reads one namespace, its trunk's
    trunk = repo_scope.trunk_namespace_of(ORG, "code-kj-26-7")
    assert trunk == "code-kj"
    assert repo_scope.trunk_namespace_of(ORG, "memory-ns") == ""
    ENTRIES.append(_cfg("code-kj-27-1", "Acme/kj", "27.1"))
    try:
        assert repos.docs_namespaces()[(ORG, "Acme/kj")] == "code-kj"
        assert repos._trunk_rank("26.10") < repos._trunk_rank("26.7")  # numeric, not lexical
    finally:
        ENTRIES.pop()


def _gate(allowed: set[str]):
    async def row_in_scope(org, ns, role, *, hides_answer=True):
        return ns in allowed

    return row_in_scope


def _search(
    results_by_ns: dict[str, list[_SearchHit]], calls: list[str] | None = None, delay: float = 0.0
):
    async def handle_knowledge_search(inp: KnowledgeSearchInput) -> KnowledgeSearchOutput:
        if calls is not None:
            calls.append(f"{inp.namespace}:{inp.namespace_scope}")
        if delay:
            await asyncio.sleep(delay)
        return KnowledgeSearchOutput(
            results=list(results_by_ns.get(inp.namespace, [])), embedding_model="fake"
        )

    return handle_knowledge_search


BASE = KnowledgeSearchInput(org=ORG, namespace="me", query="how does the kj cache evict", top_k=3)


@pytest.mark.asyncio
async def test_under_a_code_scope_the_repositorys_docs_join_the_knowledge_section(
    monkeypatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        "server.knowledge.app.knowledge_search.handle_knowledge_search",
        _search(
            {
                "me": [_hit("notes://a", 0.5, "me"), _hit("notes://b", 0.2, "me")],
                "code-kj": [_hit("doc://eviction", 0.9), _hit("doc://intro", 0.1)],
            },
            calls,
        ),
    )
    monkeypatch.setattr("server.code.app.repo_scope.row_in_scope", _gate({"code-kj"}))
    tok = set_code_scope(f"{ORG}/code-kj-26-7")
    try:
        repo_ns = await repo_scope.repo_namespace_in_scope(ORG)  # every branch reads the trunk
    finally:
        reset_code_scope(tok)
    # a release branch's scope resolves to its repository's trunk
    assert repo_ns == "code-kj"
    out = await rd.knowledge_with_repo_docs(BASE, repo_namespace=repo_ns)
    assert [(h.source_uri, h.namespace) for h in out.results] == [
        ("doc://eviction", "code-kj"),
        ("notes://a", "me"),
        ("notes://b", "me"),
    ]  # one list, by score, top_k kept; a repo hit is labelled with its namespace
    assert sorted(calls) == ["code-kj:this", "me:all_mine"]


@pytest.mark.asyncio
async def test_the_knowledge_leg_reads_only_the_namespace_the_gate_resolved(
    monkeypatch,
) -> None:
    base_only = {"me": [_hit("notes://a", 0.5, "me")], "code-kj": [_hit("doc://secret", 0.99)]}
    calls: list[str] = []
    monkeypatch.setattr(
        "server.knowledge.app.knowledge_search.handle_knowledge_search", _search(base_only, calls)
    )

    async def never(*a, **k):
        raise AssertionError("the leg must not run a gate of its own")

    monkeypatch.setattr("server.code.app.repo_scope.row_in_scope", never)
    # the namespace the gate admitted is read as given
    out = await rd.knowledge_with_repo_docs(BASE, repo_namespace="code-kj")
    assert [h.source_uri for h in out.results] == ["doc://secret", "notes://a"]
    assert sorted(calls) == ["code-kj:this", "me:all_mine"]
    # the gate refused (or there is no scope): the base search alone, even under a scope
    calls.clear()
    tok = set_code_scope(f"{ORG}/code-kj")
    try:
        out = await rd.knowledge_with_repo_docs(BASE, repo_namespace="")
    finally:
        reset_code_scope(tok)
    assert [h.source_uri for h in out.results] == ["notes://a"]
    assert calls == ["me:all_mine"]


@pytest.mark.asyncio
async def test_the_gate_admits_only_a_readable_repository_in_the_callers_org(
    monkeypatch,
) -> None:
    async def scoped(scope: str | None) -> str:
        tok = set_code_scope(scope)
        try:
            return await repo_scope.repo_namespace_in_scope(ORG)
        finally:
            reset_code_scope(tok)

    monkeypatch.setattr("server.code.app.repo_scope.row_in_scope", _gate({"code-kj"}))
    assert await scoped(f"{ORG}/code-kj-26-7") == "code-kj"
    assert await scoped(None) == ""  # not a coding checkout
    assert await scoped("other/code-kj") == ""  # another org's repository
    assert await scoped(f"{ORG}/memory-ns") == ""  # not a repository index
    monkeypatch.setattr("server.code.app.repo_scope.row_in_scope", _gate(set()))
    assert await scoped(f"{ORG}/code-kj") == ""  # refused: a restricted repository stays its own

    async def boom(*a, **k):
        raise RuntimeError("grants read failed")

    monkeypatch.setattr("server.code.app.repo_scope.row_in_scope", boom)
    assert await scoped(f"{ORG}/code-kj") == ""  # the check fails: closed


@pytest.mark.asyncio
async def test_a_slow_repository_search_answers_nothing(monkeypatch) -> None:
    base_only = {"code-kj": [_hit("doc://secret", 0.99)]}
    monkeypatch.setattr(
        "server.knowledge.app.knowledge_search.handle_knowledge_search",
        _search(base_only, delay=0.05),
    )
    assert await rd.search_repo_docs(ORG, "code-kj", "q", 3, timeout_s=0.01) == []


def test_a_hit_both_legs_return_is_listed_once() -> None:
    a = _hit("doc://x", 0.8, "code-kj")
    b = _hit("doc://x", 0.7, "code-kj")
    c = _hit("doc://x", 0.6, "code-kj", chunk=1)
    assert [(h.source_uri, h.chunk_idx, h.score) for h in rd.merge_hits([b], [a, c], 5)] == [
        ("doc://x", 0, 0.8),
        ("doc://x", 1, 0.6),
    ]


def test_the_pack_section_names_each_doc_within_its_budget() -> None:
    docs = [
        {
            "source_uri": f"doc://{i}",
            "title": f"Doc {i}",
            "description": "why the cache evicts\nearly",
        }
        for i in range(30)
    ]
    text = rd.docs_section("code-kj", docs, 120)
    assert text.startswith(
        "## Repository docs (code-kj)\nRead one with knowledge.get(namespace='code-kj'"
    )
    assert "- Doc 0 — doc://0: why the cache evicts early" in text
    assert text.splitlines()[-1].startswith("- … ") and "more (knowledge.list on code-kj)" in text
    assert len(text) <= 120 * 4 + 80
    assert rd.docs_section("code-kj", [], 300) == "" and rd.docs_section("", docs, 300) == ""


@pytest.mark.asyncio
async def test_the_orientation_pack_appends_the_docs_and_caches_the_listing(monkeypatch) -> None:
    from server.code.app import context as cc

    listed: list[str] = []

    async def handle_knowledge_list(inp):
        listed.append(inp.namespace)
        return SimpleNamespace(
            rows=[
                SimpleNamespace(source_uri="doc://runbook", title="On-call runbook", description="")
            ]
        )

    monkeypatch.setattr(
        "server.knowledge.app.document_reads.handle_knowledge_list", handle_knowledge_list
    )
    monkeypatch.setattr("server.code.app.repo_scope.row_in_scope", _gate({"code-kj"}))
    out = await cc._with_repo_docs(ORG, "code-kj-26-7", "# map", 2500)
    assert out.startswith("# map\n\n## Repository docs (code-kj)")
    assert "- On-call runbook — doc://runbook" in out
    await cc._with_repo_docs(ORG, "code-kj", "# map", 2500)
    assert listed == ["code-kj"]  # one listing per minute per docs namespace
    # a map that already fills the budget gets nothing added, and no read
    assert await cc._with_repo_docs(ORG, "code-kj", "x" * 4 * 2490, 2500) == "x" * 4 * 2490
    # a caller the gate refuses reads no listing, cached or not
    monkeypatch.setattr("server.code.app.repo_scope.row_in_scope", _gate(set()))
    assert await cc._with_repo_docs(ORG, "code-kj", "# map", 2500) == "# map"


@pytest.mark.asyncio
async def test_the_repository_leg_never_reranks_and_a_reranked_answer_keeps_its_order(
    monkeypatch,
) -> None:
    """The repository leg runs under a short timeout a reranker's model call would
    outlast; a reranked base answer keeps its order, the repository's hits after it."""
    seen: list[str] = []

    async def handle_knowledge_search(inp: KnowledgeSearchInput) -> KnowledgeSearchOutput:
        seen.append(f"{inp.namespace}:{inp.rerank}")
        if inp.namespace == "code-kj":
            hits = [_hit("doc://repo", 0.9), _hit("n://b", 0.8, "me")]
            return KnowledgeSearchOutput(results=hits, embedding_model="fake")
        # the reranker put the lower-scored hit first
        hits = [_hit("n://b", 0.1, "me"), _hit("n://a", 0.5, "me")]
        return KnowledgeSearchOutput(results=hits, embedding_model="fake", reranked_by="builtin:x")

    monkeypatch.setattr(
        "server.knowledge.app.knowledge_search.handle_knowledge_search", handle_knowledge_search
    )
    out = await rd.knowledge_with_repo_docs(BASE, repo_namespace="code-kj")
    assert sorted(seen) == ["code-kj:none", "me:"]
    assert [h.source_uri for h in out.results] == ["n://b", "n://a", "doc://repo"]
    assert out.reranked_by == "builtin:x"


@pytest.mark.asyncio
async def test_a_reranked_page_admits_repository_hits_by_score_and_keeps_its_order(
    monkeypatch,
) -> None:
    """Which repository hits join is the fused-score rule of the unreranked merge; a
    reranked page keeps its own order for the rest and the admitted hits follow it."""

    async def handle_knowledge_search(inp: KnowledgeSearchInput) -> KnowledgeSearchOutput:
        if inp.namespace == "code-kj":
            # one strong repository hit, two weak ones
            hits = [_hit("doc://strong", 0.9), _hit("doc://weak1", 0.05), _hit("doc://weak2", 0.04)]
            return KnowledgeSearchOutput(results=hits, embedding_model="fake")
        # reranked: fused scores 0.2-0.5, in the reranker's order
        hits = [
            _hit(f"n://{c}", s, "me") for c, s in zip("xyzw", (0.2, 0.5, 0.3, 0.4), strict=True)
        ]
        return KnowledgeSearchOutput(results=hits, embedding_model="fake", reranked_by="builtin:x")

    monkeypatch.setattr(
        "server.knowledge.app.knowledge_search.handle_knowledge_search", handle_knowledge_search
    )
    page = BASE.model_copy(update={"top_k": 4})
    out = await rd.knowledge_with_repo_docs(page, repo_namespace="code-kj")
    # only the strong repository hit beats a base hit; the reranked head keeps its order
    assert [h.source_uri for h in out.results] == ["n://x", "n://y", "n://z", "doc://strong"]
