"""memory.list `contains`: the keyword filter of a browse page's search box.

A ranked search returns its top k, so a word that many rows name hides the
newest of them. The list filter returns every row whose value or key holds the
text, newest first, and its total counts the same rows."""

from __future__ import annotations

from typing import Any

import pytest

from server.memory import store as memory_repo


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
async def test_contains_reaches_the_page_and_both_counts(
    calls: dict[str, list[dict[str, Any]]],
) -> None:
    from server.memory.api.models_crud import MemoryListInput
    from server.memory.app.query import _handle_memory_list

    await _handle_memory_list(
        MemoryListInput(org="acme", namespace="team", agent="*", contains="Azure")
    )

    assert [c["contains"] for c in calls["list"]] == ["Azure"]
    assert [c["contains"] for c in calls["count"]] == ["Azure", "Azure"], "total and today"
    assert calls["list"][0]["order_by"] == "created_at_desc"


@pytest.mark.asyncio
async def test_the_counts_cache_keys_on_the_text(calls: dict[str, list[dict[str, Any]]]) -> None:
    """Two texts on one namespace version are two totals, not one remembered for both."""
    from server.memory.api.models_crud import MemoryListInput
    from server.memory.app.query import _handle_memory_list

    for text in ("Azure", "S3"):
        await _handle_memory_list(
            MemoryListInput(org="acme", namespace="team", agent="*", contains=text)
        )

    assert [c["contains"] for c in calls["count"]] == ["Azure", "Azure", "S3", "S3"]


@pytest.mark.asyncio
async def test_no_text_is_no_filter(calls: dict[str, list[dict[str, Any]]]) -> None:
    from server.memory.api.models_crud import MemoryListInput
    from server.memory.app.query import _handle_memory_list

    await _handle_memory_list(MemoryListInput(org="acme", namespace="team", agent="*", contains=""))

    assert calls["list"][0]["contains"] is None


def test_contains_is_hidden_from_the_mcp_listing() -> None:
    """Agents search with memory.search; the field serves the browse page."""
    from server.memory.api.models_crud import MemoryListInput

    extra = MemoryListInput.model_fields["contains"].json_schema_extra
    assert isinstance(extra, dict) and extra, "carries the MCP-hidden marker"
