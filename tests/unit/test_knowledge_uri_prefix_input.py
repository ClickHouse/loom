"""`uri_prefix` on knowledge.search / knowledge.list."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from server.knowledge.api.models_knowledge import KnowledgeListInput, KnowledgeSearchInput


def test_search_input_defaults_to_whole_namespace() -> None:
    inp = KnowledgeSearchInput(org="o", namespace="n", query="q")
    assert inp.uri_prefix == []


def test_search_input_strips_and_dedupes_prefixes() -> None:
    inp = KnowledgeSearchInput(
        org="o",
        namespace="n",
        query="q",
        uri_prefix=[" docs/decisions/ ", "docs/decisions/", "docs/adr/"],
    )
    assert inp.uri_prefix == ["docs/decisions/", "docs/adr/"]


def test_search_input_rejects_blank_prefix() -> None:
    with pytest.raises(ValidationError):
        KnowledgeSearchInput(org="o", namespace="n", query="q", uri_prefix=["  "])


def test_search_input_caps_prefix_count() -> None:
    with pytest.raises(ValidationError):
        KnowledgeSearchInput(
            org="o", namespace="n", query="q", uri_prefix=[f"p{i}/" for i in range(9)]
        )


def test_list_input_accepts_prefix() -> None:
    inp = KnowledgeListInput(org="o", namespace="n", uri_prefix=["docs/"])
    assert inp.uri_prefix == ["docs/"]


@pytest.mark.asyncio
async def test_list_repo_emits_prefix_clause() -> None:
    from server.knowledge.store import documents as documents_repo

    class StubCH:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        async def query_all(self, sql, params=None):
            self.calls.append((sql, params or {}))
            return []

    ch = StubCH()
    await documents_repo.list_with_chunk_counts(
        "o", ["n"], uri_prefixes=["docs/decisions/", ""], ch=ch
    )
    sql, params = ch.calls[0]
    assert "startsWith(source_uri, {pfx0:String})" in sql
    assert params["pfx0"] == "docs/decisions/"
    assert "pfx1" not in params
