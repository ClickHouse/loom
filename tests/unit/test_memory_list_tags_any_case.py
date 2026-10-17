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
    assert "hasAny(arrayMap(t -> lower(t), tags), {tags:Array(String)})" in any_sql
    assert any_case["tags"] == ["api"]
