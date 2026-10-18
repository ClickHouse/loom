"""memory.list and knowledge.list match tags in any case.

Tags keep the case they were written in (`API`, `api`). The list reads compare them
lowercased on both sides, so `tags=["api"]` finds a row tagged `API` with no flag, and the
totals count the same rows. memory.search keeps its exact match (its own tests)."""

from __future__ import annotations

from typing import Any

import pytest

from server.core.tag_sql import TagFilter, list_tag_filter, tag_where
from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.memory import store as memory_repo

# Both sides of the tag match, folded by ClickHouse.
_FOLDED = "arrayMap(t -> lowerUTF8(t), tags), arrayMap(t -> lowerUTF8(t), {tags:Array(String)})"
_EXACT = "hasAny(tags, {tags:Array(String)})"


class StubCH:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
        self.calls.append((sql, params or {}))
        return []

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> Any:
        self.calls.append((sql, params or {}))
        return {"n": 0}


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[dict[str, Any]]]:
    from server.memory.app.query import reset_list_counts_cache

    reset_list_counts_cache()
    seen: dict[str, list[dict[str, Any]]] = {"list": [], "count": []}

    async def namespace_version(org: str, ns: str, **kw: Any) -> int:
        return 4200

    async def list_by_namespace(identity: Any, **kw: Any) -> list[Any]:
        seen["list"].append(kw)
        return []

    async def count_by_namespace(identity: Any, **kw: Any) -> int:
        seen["count"].append(kw)
        return 0

    monkeypatch.setattr(memory_repo, "namespace_version", namespace_version)
    monkeypatch.setattr(memory_repo, "list_by_namespace", list_by_namespace)
    monkeypatch.setattr(memory_repo, "count_by_namespace", count_by_namespace)
    return seen


@pytest.mark.asyncio
async def test_folded_any_renders_both_sides_lowercased() -> None:
    """`tags=["API"]` with nothing else set reaches ClickHouse as the folded predicate, so a
    row tagged `api` matches."""
    identity = memory_repo.Identity(org="acme", namespace="team", agent="*")
    ch = StubCH()
    await memory_repo.list_by_namespace(
        identity, scope=WHOLE_NAMESPACE, tag_filter=TagFilter(("API",), "folded_any"), ch=ch
    )
    await memory_repo.count_by_namespace(
        identity, scope=WHOLE_NAMESPACE, tag_filter=TagFilter(("API",), "folded_any"), ch=ch
    )
    for sql, params in ch.calls:
        assert f"hasAny({_FOLDED})" in sql
        assert _EXACT not in sql
        assert params["tags"] == ["API"]


@pytest.mark.asyncio
async def test_the_page_and_both_counts_get_the_same_tags(
    calls: dict[str, list[dict[str, Any]]],
) -> None:
    from server.memory.api.models_crud import MemoryListInput
    from server.memory.app.query import _handle_memory_list

    await _handle_memory_list(
        MemoryListInput(org="acme", namespace="team", agent="*", tags=["API"])
    )
    asked = TagFilter(("API",), "folded_any")
    assert [c["tag_filter"] for c in calls["list"]] == [asked]
    assert [c["tag_filter"] for c in calls["count"]] == [asked, asked], "total and today"


@pytest.mark.asyncio
async def test_the_hidden_under_prefix_count_matches_tags_as_the_total_does(
    calls: dict[str, list[dict[str, Any]]],
) -> None:
    """Under a prefix with nothing live, the "every status" count says what the status filter
    hid; it must match the tags as `total` did, or the tag filter reads as hidden rows."""
    from server.memory.api.models_crud import MemoryListInput
    from server.memory.app.query import _handle_memory_list

    await _handle_memory_list(
        MemoryListInput(
            org="acme",
            namespace="team",
            agent="*",
            prefix="alerts/",
            tags=["a", "b"],
            tags_every=True,
        )
    )
    assert len(calls["count"]) == 3, "total, today and every status under the prefix"
    assert [c["tag_filter"] for c in calls["count"]] == [TagFilter(("a", "b"), "folded_all")] * 3


@pytest.mark.asyncio
async def test_the_store_requires_every_tag_when_asked() -> None:
    """`folded_all` (`tags_every=True`) needs every tag."""
    identity = memory_repo.Identity(org="acme", namespace="team", agent="*")
    ch = StubCH()
    await memory_repo.count_by_namespace(
        identity, scope=WHOLE_NAMESPACE, tag_filter=TagFilter(("A", "b"), "folded_all"), ch=ch
    )
    sql, params = ch.calls[-1]
    assert f"hasAll({_FOLDED})" in sql
    assert params["tags"] == ["A", "b"]


@pytest.mark.asyncio
async def test_tags_every_reaches_the_page_both_counts_and_the_cache_key(
    calls: dict[str, list[dict[str, Any]]],
) -> None:
    from server.memory.api.models_crud import MemoryListInput
    from server.memory.app.query import _handle_memory_list

    for all_tags in (False, True):
        await _handle_memory_list(
            MemoryListInput(
                org="acme", namespace="team", agent="*", tags=["a", "b"], tags_every=all_tags
            )
        )
    assert [c["tag_filter"].match for c in calls["list"]] == ["folded_any", "folded_all"]
    assert [c["tag_filter"].match for c in calls["count"]] == [
        "folded_any",
        "folded_any",
        "folded_all",
        "folded_all",
    ]


@pytest.mark.asyncio
async def test_the_score_sorted_list_matches_tags_as_the_recency_list_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`sort_by="score"` builds its own statement with the same folded predicate."""
    from server.memory.api.models_crud import MemoryListInput
    from server.memory.app import query as mq

    ch = StubCH()
    monkeypatch.setattr(mq, "get_client", lambda: ch)
    monkeypatch.setattr(mq, "_record_read_exposure", lambda *a, **k: None)
    await mq._handle_memory_list(
        MemoryListInput(org="acme", namespace="team", agent="*", sort_by="score", tags=["API"])
    )
    sql, params = ch.calls[0]
    assert f"hasAny({_FOLDED})" in sql
    assert params["tags"] == ["API"]


@pytest.mark.asyncio
async def test_the_score_sorted_list_with_tags_every_needs_every_folded_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.api.models_crud import MemoryListInput
    from server.memory.app import query as mq

    ch = StubCH()
    monkeypatch.setattr(mq, "get_client", lambda: ch)
    monkeypatch.setattr(mq, "_record_read_exposure", lambda *a, **k: None)
    await mq._handle_memory_list(
        MemoryListInput(
            org="acme",
            namespace="team",
            agent="*",
            sort_by="score",
            tags=["A", "b"],
            tags_every=True,
        )
    )
    sql, params = ch.calls[0]
    assert f"hasAll({_FOLDED})" in sql
    assert params["tags"] == ["A", "b"]


@pytest.mark.asyncio
async def test_knowledge_list_matches_tags_in_any_case(monkeypatch: pytest.MonkeyPatch) -> None:
    """knowledge.list's handler down to its statement: both sides folded with no flag, the
    asked tags bound as typed."""
    from server.knowledge.api.models_knowledge import KnowledgeListInput
    from server.knowledge.app.document_reads import handle_knowledge_list
    from server.knowledge.store import documents as documents_repo

    ch = StubCH()
    monkeypatch.setattr(documents_repo, "_default_client", lambda: ch)
    await handle_knowledge_list(KnowledgeListInput(org="acme", namespace="team", tags=["Specs"]))
    sql, params = ch.calls[0]
    assert f"hasAny({_FOLDED})" in sql
    assert _EXACT not in sql
    assert params["tags"] == ["Specs"]


@pytest.mark.asyncio
async def test_knowledge_list_drops_empty_tags_as_memory_list_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty tag is not a filter: `["spec", ""]` binds only `spec`, and `[""]` adds none."""
    from server.knowledge.api.models_knowledge import KnowledgeListInput
    from server.knowledge.app.document_reads import handle_knowledge_list
    from server.knowledge.store import documents as documents_repo

    ch = StubCH()
    monkeypatch.setattr(documents_repo, "_default_client", lambda: ch)
    await handle_knowledge_list(
        KnowledgeListInput(org="acme", namespace="team", tags=["spec", ""], tags_every=True)
    )
    sql, params = ch.calls[-1]
    assert f"hasAll({_FOLDED})" in sql
    assert params["tags"] == ["spec"]

    await handle_knowledge_list(KnowledgeListInput(org="acme", namespace="team", tags=[""]))
    sql, params = ch.calls[-1]
    assert "tags" not in params
    assert _FOLDED not in sql


def test_a_tag_filter_needs_a_tag() -> None:
    with pytest.raises(ValueError):
        TagFilter((), "folded_any")
    assert list_tag_filter(["", ""], every=False) is None


def test_exact_any_renders_the_tags_as_typed() -> None:
    """memory.search's tiers match exactly: the exact filter never folds case."""
    params: dict[str, Any] = {}
    assert tag_where(TagFilter(("API",), "exact_any"), params) == _EXACT
    assert params["tags"] == ["API"]


def test_every_tag_empty_asks_for_no_tag_filter() -> None:
    """An empty `hasAll` would match every row: empty tags never become a filter."""
    assert list_tag_filter(["", ""], every=True) is None
