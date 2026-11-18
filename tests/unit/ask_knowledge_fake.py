"""Ordinary search scope and checked replies for ask's external-I/O fakes."""

from types import SimpleNamespace

import pytest

from server.knowledge.app import knowledge_search, source_access
from server.knowledge.app.knowledge_search import CheckedSearch, SearchRequest
from server.knowledge.app.source_access import ReadPlan
from server.memory.app.ask.source_context import prepare_knowledge


@pytest.fixture
def ordinary_knowledge_scope(monkeypatch):
    async def resolve(input):
        return SearchRequest(input.model_copy(deep=True), (input.namespace,), ())

    async def prepare(org, namespaces):
        return ReadPlan(org)

    monkeypatch.setattr(knowledge_search, "resolve_search", resolve)
    monkeypatch.setattr(source_access, "prepare", prepare)


def checked_reply(output):
    return CheckedSearch(output, None, SimpleNamespace(marked=False), [], 0)


async def route_scope(input):
    return await prepare_knowledge(input, input.query, enabled=input.knowledge, corpus=True)
