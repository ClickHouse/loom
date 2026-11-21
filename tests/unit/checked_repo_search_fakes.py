"""Ordinary typed repository results for unified-search orchestration tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from server.knowledge.app import repo_docs
from server.knowledge.app.knowledge_search import CheckedSearch, SearchRequest
from server.knowledge.app.source_access import ReadPlan
from server.memory.app.loom_search import fanout
from server.settings import settings


def install_repo_search(monkeypatch, handler):
    async def resolve(input, *, repo_namespace):
        return repo_docs.RepoSearchRequest(
            SearchRequest(input.model_copy(deep=True), (input.namespace,), ()),
            None,
            repo_namespace,
            asyncio.get_running_loop().time() + settings.knowledge_repo_docs_timeout_s,
        )

    async def prepare(org, namespaces):
        return ReadPlan(org)

    async def run(request, plan):
        output = await handler(request.base.input, repo_namespace=request.repo_namespace)
        checked = CheckedSearch(output, None, SimpleNamespace(marked=False), [], 0)
        return repo_docs.RepoSearch(request, checked, None)

    monkeypatch.setattr(repo_docs, "resolve_repo_search", resolve)
    monkeypatch.setattr(repo_docs, "run_repo_search", run)
    monkeypatch.setattr(fanout, "prepare", prepare)
