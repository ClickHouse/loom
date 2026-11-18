"""Composed repository searches widen fixed scopes and retain final deadlines."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from server.knowledge.api.models_knowledge import KnowledgeSearchInput, KnowledgeSearchOutput
from server.knowledge.app.knowledge_search import CheckedSearch, SearchRequest
from server.knowledge.app.repo_docs import RepoSearch, RepoSearchRequest
from server.settings import settings


def request(*, extra=True):
    base = SearchRequest(
        KnowledgeSearchInput(
            org="acme",
            namespace="home",
            query="cache",
            top_k=2,
            tags=["bounded"],
            uri_prefix=["notes:"],
        ),
        ("home", "team"),
        ("private",),
    )
    additive = (
        SearchRequest(
            KnowledgeSearchInput(
                org="acme",
                namespace="repo",
                query="cache",
                top_k=2,
                rerank="none",
                namespace_scope="this",
            ),
            ("repo",),
            (),
        )
        if extra
        else None
    )
    return RepoSearchRequest(base, additive, "repo", asyncio.get_running_loop().time() + 100)


def child(*, mirrored=True, lost=False, fresh=True):
    proof = SimpleNamespace(fresh=Mock(return_value=fresh)) if mirrored else None
    return CheckedSearch(
        KnowledgeSearchOutput(results=[], embedding_model="fake"),
        proof,
        SimpleNamespace(marked=True),
        [],
        0,
        lost=lost,
    )


async def test_wider_recipe_keeps_authorized_scope_filters_and_caps_additive_leg():
    initial = request()
    began = asyncio.get_running_loop().time()
    widened = initial.wider(50)
    ended = asyncio.get_running_loop().time()
    assert initial.base.input.top_k == initial.extra.input.top_k == 2
    assert widened.base.input.top_k == 50 and widened.extra.input.top_k == 20
    assert widened.base.namespaces == ("home", "team") and widened.base.skipped == ("private",)
    assert widened.extra.namespaces == ("repo",) and widened.extra.input.rerank == "none"
    assert widened.extra.input.namespace_scope == "this"
    assert widened.base.input.tags == ["bounded"] and widened.base.input.uri_prefix == ["notes:"]
    initial.base.input.tags.append("later")
    initial.base.input.uri_prefix.append("later:")
    assert widened.base.input.tags == ["bounded"] and widened.base.input.uri_prefix == ["notes:"]
    assert (
        began + settings.knowledge_repo_docs_timeout_s
        <= widened.extra_deadline
        <= ended + settings.knowledge_repo_docs_timeout_s
    )


async def test_failed_initial_additive_resolution_is_never_rediscovered_by_widening():
    initial = request(extra=False)
    widened = initial.wider(10)
    assert widened.extra is None and widened.repo_namespace == initial.repo_namespace
    assert widened.namespaces == ["home", "team"]


@pytest.mark.parametrize("which", ["base", "extra"])
@pytest.mark.parametrize("state", ["lost", "stale"])
async def test_final_pure_sweep_rejects_any_retained_child_loss(which, state):
    result = RepoSearch(request(), child(), child())
    target = getattr(result, which)
    if state == "lost":
        target.lost = True
    else:
        target.proofs.fresh.return_value = False
    assert not result.fresh()


async def test_expired_additive_budget_rejects_mixed_proof_even_if_acl_is_fresh():
    result = RepoSearch(request(), child(), child())
    assert result.fresh()
    result.request = replace(result.request, extra_deadline=asyncio.get_running_loop().time() - 1)
    assert not result.fresh()
    assert result.extra is not None


@pytest.mark.parametrize("extra", [False, True])
async def test_complete_ordinary_results_need_no_conditional_read_or_extra_deadline(extra):
    result = RepoSearch(
        request(extra=extra), child(mirrored=False), child(mirrored=False) if extra else None
    )
    result.request = replace(result.request, extra_deadline=asyncio.get_running_loop().time() - 1)
    assert result.fresh()


async def test_composition_loss_stays_stale_after_failed_extra_is_dropped():
    result = RepoSearch(request(), child(mirrored=False), None)
    result._lost = True
    assert not result.fresh()
