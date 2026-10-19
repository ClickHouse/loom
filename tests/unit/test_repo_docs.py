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
    assert rd.docs_namespace_of(ORG, "code-kj-26-7") == "code-kj"  # every branch, one place
    assert rd.docs_namespace_of(ORG, "memory-ns") == ""
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
    monkeypatch.setattr("server.auth.authz.row_in_scope", _gate({"code-kj"}))
    tok = set_code_scope(f"{ORG}/code-kj-26-7")
    try:
        out = await rd.knowledge_with_repo_docs(BASE)
    finally:
        reset_code_scope(tok)
    assert [(h.source_uri, h.namespace) for h in out.results] == [
        ("doc://eviction", "code-kj"),
        ("notes://a", "me"),
        ("notes://b", "me"),
    ]  # one list, by score, top_k kept; a repo hit is labelled with its namespace
    assert sorted(calls) == ["code-kj:this", "me:all_mine"]


@pytest.mark.asyncio
async def test_no_scope_a_refused_gate_or_a_slow_leg_answer_the_base_alone(
    monkeypatch,
) -> None:

    base_only = {"me": [_hit("notes://a", 0.5, "me")], "code-kj": [_hit("doc://secret", 0.99)]}
    monkeypatch.setattr(
        "server.knowledge.app.knowledge_search.handle_knowledge_search", _search(base_only)
    )
    monkeypatch.setattr("server.auth.authz.row_in_scope", _gate({"code-kj"}))
    # no scope: not a coding checkout
    out = await rd.knowledge_with_repo_docs(BASE)
    assert [h.source_uri for h in out.results] == ["notes://a"]
    tok = set_code_scope(f"{ORG}/code-kj")
    try:
        # the gate refuses the caller: a restricted repository's docs stay its own
        monkeypatch.setattr("server.auth.authz.row_in_scope", _gate(set()))
        assert [h.source_uri for h in (await rd.knowledge_with_repo_docs(BASE)).results] == [
            "notes://a"
        ]

        # the gate itself fails: closed
        async def boom(*a, **k):
            raise RuntimeError("grants read failed")

        monkeypatch.setattr("server.auth.authz.row_in_scope", boom)
        assert [h.source_uri for h in (await rd.knowledge_with_repo_docs(BASE)).results] == [
            "notes://a"
        ]
        monkeypatch.setattr("server.auth.authz.row_in_scope", _gate({"code-kj"}))
        # the repository's search does not answer in time
        monkeypatch.setattr(
            "server.knowledge.app.knowledge_search.handle_knowledge_search",
            _search(base_only, delay=0.05),
        )
        ns, hits = await rd.search_repo_docs(ORG, "code-kj", "q", 3, timeout_s=0.01)
        assert (ns, hits) == ("code-kj", [])
    finally:
        reset_code_scope(tok)


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
    monkeypatch.setattr("server.auth.authz.row_in_scope", _gate({"code-kj"}))
    out = await cc._with_repo_docs(ORG, "code-kj-26-7", "# map", 2500)
    assert out.startswith("# map\n\n## Repository docs (code-kj)")
    assert "- On-call runbook — doc://runbook" in out
    await cc._with_repo_docs(ORG, "code-kj", "# map", 2500)
    assert listed == ["code-kj"]  # one listing per minute per docs namespace
    # a map that already fills the budget gets nothing added, and no read
    assert await cc._with_repo_docs(ORG, "code-kj", "x" * 4 * 2490, 2500) == "x" * 4 * 2490
    # a caller the gate refuses reads no listing, cached or not
    monkeypatch.setattr("server.auth.authz.row_in_scope", _gate(set()))
    assert await cc._with_repo_docs(ORG, "code-kj", "# map", 2500) == "# map"
