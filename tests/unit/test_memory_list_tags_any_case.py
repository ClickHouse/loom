"""memory.list `tags_any_case`: a browse page's tag filter in any case.

Tags keep the case they were written in (`API`, `api`), and the hard tag filter matches
them exactly, as agents rely on. A person filtering by `tag:api` means every case of it, so
the dashboard asks for `tags_any_case`: the tags are compared lowercased, and the total
counts the same rows. A build that predates the flag ignores it and matches exactly."""

from __future__ import annotations

from typing import Any

import pytest

from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.memory import store as memory_repo

# Both sides of an any-case tag match, folded by ClickHouse.
_FOLDED = "arrayMap(t -> lowerUTF8(t), tags), arrayMap(t -> lowerUTF8(t), {tags:Array(String)})"


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
async def test_the_flag_reaches_the_page_and_both_counts(
    calls: dict[str, list[dict[str, Any]]],
) -> None:
    from server.memory.api.models_crud import MemoryListInput
    from server.memory.app.query import _handle_memory_list

    await _handle_memory_list(
        MemoryListInput(org="acme", namespace="team", agent="*", tags=["API"], tags_any_case=True)
    )
    assert [c["tags_any_case"] for c in calls["list"]] == [True]
    assert [c["tags_any_case"] for c in calls["count"]] == [True, True], "total and today"


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
            tags_any_case=True,
            tags_all=True,
        )
    )
    assert len(calls["count"]) == 3, "total, today and every status under the prefix"
    assert [c["tags_any_case"] for c in calls["count"]] == [True, True, True]
    assert [c["tags_all"] for c in calls["count"]] == [True, True, True]


@pytest.mark.asyncio
async def test_the_counts_cache_keys_on_the_flag(calls: dict[str, list[dict[str, Any]]]) -> None:
    from server.memory.api.models_crud import MemoryListInput
    from server.memory.app.query import _handle_memory_list

    for any_case in (False, True):
        await _handle_memory_list(
            MemoryListInput(
                org="acme", namespace="team", agent="*", tags=["API"], tags_any_case=any_case
            )
        )
    assert [c["tags_any_case"] for c in calls["count"]] == [False, False, True, True]


@pytest.mark.asyncio
async def test_the_store_compares_lowercased_tags_only_when_asked() -> None:
    identity = memory_repo.Identity(org="acme", namespace="team", agent="*")
    scope = WHOLE_NAMESPACE
    ch = StubCH()
    await memory_repo.count_by_namespace(identity, scope=scope, tags=["API"], ch=ch)
    exact_sql, exact = ch.calls[-1]
    assert "hasAny(tags, {tags:Array(String)})" in exact_sql
    assert exact["tags"] == ["API"]

    await memory_repo.count_by_namespace(
        identity, scope=scope, tags=["API"], tags_any_case=True, ch=ch
    )
    any_sql, any_case = ch.calls[-1]
    assert f"hasAny({_FOLDED})" in any_sql
    assert any_case["tags"] == ["API"]


@pytest.mark.asyncio
async def test_the_store_requires_every_tag_when_asked() -> None:
    """`tags_all`: stacked tag filters (`tag:a tag:b`) need every tag, so rows holding all
    of them are found past the newest rows that hold any one."""
    identity = memory_repo.Identity(org="acme", namespace="team", agent="*")
    ch = StubCH()
    await memory_repo.count_by_namespace(
        identity, scope=WHOLE_NAMESPACE, tags=["A", "b"], tags_any_case=True, tags_all=True, ch=ch
    )
    sql, params = ch.calls[-1]
    assert f"hasAll({_FOLDED})" in sql
    assert params["tags"] == ["A", "b"]


@pytest.mark.asyncio
async def test_tags_all_reaches_the_page_both_counts_and_the_cache_key(
    calls: dict[str, list[dict[str, Any]]],
) -> None:
    from server.memory.api.models_crud import MemoryListInput
    from server.memory.app.query import _handle_memory_list

    for all_tags in (False, True):
        await _handle_memory_list(
            MemoryListInput(
                org="acme", namespace="team", agent="*", tags=["a", "b"], tags_all=all_tags
            )
        )
    assert [c["tags_all"] for c in calls["list"]] == [False, True]
    assert [c["tags_all"] for c in calls["count"]] == [False, False, True, True]


@pytest.mark.asyncio
async def test_a_non_ascii_tag_folds_the_same_on_both_sides() -> None:
    """ClickHouse folds both sides: the asked tag is bound as typed and lowercased by the same
    `lowerUTF8` as the row's tags (not `lower`, which folds only A–Z), so a non-ASCII tag such
    as `ΟΔΟΣ` meets the row's tags under one case mapping."""
    identity = memory_repo.Identity(org="acme", namespace="team", agent="*")
    ch = StubCH()
    await memory_repo.count_by_namespace(
        identity, scope=WHOLE_NAMESPACE, tags=["ΟΔΟΣ"], tags_any_case=True, ch=ch
    )
    sql, params = ch.calls[-1]
    assert f"hasAny({_FOLDED})" in sql
    assert params["tags"] == ["ΟΔΟΣ"]


@pytest.mark.asyncio
async def test_the_score_sorted_list_matches_tags_as_the_recency_list_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`sort_by="score"` builds its own statement: both flags reach it as they reach the rest."""
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
            tags_any_case=True,
            tags_all=True,
        )
    )
    sql, params = ch.calls[0]
    assert f"hasAll({_FOLDED})" in sql
    assert params["tags"] == ["A", "b"]


@pytest.mark.asyncio
async def test_knowledge_list_hands_both_flags_to_the_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.knowledge.api.models_knowledge import KnowledgeListInput
    from server.knowledge.app.document_reads import handle_knowledge_list
    from server.knowledge.store import documents as documents_repo

    seen: dict[str, Any] = {}

    async def list_with_chunk_counts(org: str, namespace: str, **kw: Any) -> list[Any]:
        seen.update(kw)
        return []

    monkeypatch.setattr(documents_repo, "list_with_chunk_counts", list_with_chunk_counts)
    await handle_knowledge_list(
        KnowledgeListInput(
            org="acme", namespace="team", tags=["Specs"], tags_any_case=True, tags_all=True
        )
    )
    assert seen["tags"] == ["Specs"]
    assert seen["tags_any_case"] is True and seen["tags_all"] is True
