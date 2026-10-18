"""memory.delete + memory.list end-to-end tests."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_delete_nonexistent_is_noop(agent) -> None:
    """Deleting a key that doesn't exist returns tombstoned=False (idempotent)."""
    result = await agent.memory.delete("never-existed")
    assert result is False


@pytest.mark.asyncio
async def test_delete_then_get_returns_none(agent) -> None:
    """After delete, get returns None (tombstoned row hidden)."""
    await agent.memory.set("doomed", {"x": 1}, memory_type="semantic")
    assert await agent.memory.get("doomed") is not None

    deleted = await agent.memory.delete("doomed")
    assert deleted is True

    assert await agent.memory.get("doomed") is None


@pytest.mark.asyncio
async def test_set_after_delete_resurrects(agent) -> None:
    """Setting a key after delete writes a new live row (higher version wins)."""
    await agent.memory.set("phoenix", {"v": 1}, memory_type="semantic")
    await agent.memory.delete("phoenix")
    assert await agent.memory.get("phoenix") is None

    await agent.memory.set("phoenix", {"v": 2}, memory_type="semantic")
    result = await agent.memory.get("phoenix")
    assert result is not None
    assert result["value"] == {"v": 2}


@pytest.mark.asyncio
async def test_list_empty_namespace(agent) -> None:
    """Fresh namespace has no entries."""
    result = await agent.memory.list()
    assert result["entries"] == []
    assert result["truncated"] is False


@pytest.mark.asyncio
async def test_list_returns_all_set_keys(agent) -> None:
    """memory.list surfaces every key the caller set under this identity.

    Order is `created_at_desc` per server/memory/app/handlers.py
    (newest-first feed). Asserting set membership
    instead of list order so this test pins the contract that
    matters: nothing the caller wrote went missing.
    """
    await agent.memory.set("zebra", 1, memory_type="semantic")
    await agent.memory.set("apple", 2, memory_type="semantic")
    await agent.memory.set("mango", 3, memory_type="semantic")

    result = await agent.memory.list()
    keys = {e["memory_key"] for e in result["entries"]}
    assert keys == {"apple", "mango", "zebra"}


@pytest.mark.asyncio
async def test_list_filters_by_prefix(agent) -> None:
    """`prefix=` only returns keys starting with the prefix."""
    await agent.memory.set("user.alice", {"a": 1}, memory_type="preference")
    await agent.memory.set("user.bob", {"a": 2}, memory_type="preference")
    await agent.memory.set("system.config", {"a": 3}, memory_type="semantic")

    result = await agent.memory.list(prefix="user.")
    keys = [e["memory_key"] for e in result["entries"]]
    assert set(keys) == {"user.alice", "user.bob"}


@pytest.mark.asyncio
async def test_list_filters_by_memory_type(agent) -> None:
    """`memory_type=` only returns entries of that type."""
    await agent.memory.set("k1", 1, memory_type="semantic")
    await agent.memory.set("k2", 2, memory_type="episodic")
    await agent.memory.set("k3", 3, memory_type="semantic")

    result = await agent.memory.list(memory_type="semantic")
    keys = [e["memory_key"] for e in result["entries"]]
    assert set(keys) == {"k1", "k3"}


@pytest.mark.asyncio
async def test_list_hides_tombstoned(agent) -> None:
    """Deleted entries don't appear in list."""
    await agent.memory.set("live", 1, memory_type="semantic")
    await agent.memory.set("dead", 2, memory_type="semantic")
    await agent.memory.delete("dead")

    result = await agent.memory.list()
    keys = [e["memory_key"] for e in result["entries"]]
    assert keys == ["live"]


@pytest.mark.asyncio
async def test_list_truncation(agent) -> None:
    """When more entries exist than `limit`, `truncated` is True."""
    for i in range(5):
        await agent.memory.set(f"key{i:02d}", i, memory_type="semantic")

    result = await agent.memory.list(limit=3)
    assert len(result["entries"]) == 3
    assert result["truncated"] is True

    result_all = await agent.memory.list(limit=100)
    assert len(result_all["entries"]) == 5
    assert result_all["truncated"] is False


@pytest.mark.asyncio
async def test_delete_under_another_user_leaves_the_row(agent) -> None:
    """The namespace-wide lookup a delete falls back to
    never tombstones another user's row."""
    await agent.memory.set("prefers_tabs_owned", True, memory_type="preference", user_id="alice")
    tombstoned = await agent.memory.delete("prefers_tabs_owned", user_id="bob")
    assert tombstoned is False  # the SDK returns the tombstoned flag
    alice = await agent.memory.get("prefers_tabs_owned", user_id="alice")
    assert alice is not None and alice["value"] is True


@pytest.mark.asyncio
async def test_list_tags_match_in_any_case(agent) -> None:
    """`api` finds a row tagged `API`, `ärger` one tagged `ÄRGER`, and `tags_all` needs every tag."""
    # Unrelated sentences, so no write is folded into another as a duplicate.
    await agent.memory.set(
        "upper",
        "The billing API retries a failed charge three times.",
        memory_type="semantic",
        tags=["API"],
    )
    await agent.memory.set(
        "umlaut",
        "Nightly backups are copied to a second region.",
        memory_type="semantic",
        tags=["ÄRGER"],
    )
    await agent.memory.set(
        "both",
        "Release notes are drafted in the docs repository.",
        memory_type="semantic",
        tags=["api", "Docs"],
    )
    await agent.memory.set(
        "untagged", "The office plants are watered on Fridays.", memory_type="semantic"
    )

    def keys(page: dict) -> set[str]:
        return {e["memory_key"] for e in page["entries"]}

    assert keys(await agent.memory.list(tags=["api"])) == {"upper", "both"}
    assert keys(await agent.memory.list(tags=["ärger"])) == {"umlaut"}
    every = await agent.call("memory.list", tags=["API", "docs"], tags_all=True)
    assert keys(every) == {"both"}
    assert every["total"] == 1
