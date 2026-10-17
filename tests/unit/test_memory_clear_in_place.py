"""memory.clear without the journal: the scope tombstoned in place
(`bulk_tombstone_scope`), as before the journal existed."""

from __future__ import annotations

import pytest
from fastapi import HTTPException


@pytest.mark.asyncio
async def test_clear_with_user_id_tombstones_across_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory import store as memory_repo
    from server.memory.api.models_crud import MemoryClearInput
    from server.memory.app.handlers import handle_memory_clear

    captured: dict[str, object] = {}

    async def fake_bulk(*, org, namespace, user_id=None, agent=None, ch=None):
        captured.update(
            org=org,
            namespace=namespace,
            user_id=user_id,
            agent=agent,
        )
        return {
            "loom.memory": 47,
            "loom.memory_embeddings": 42,
        }

    monkeypatch.setattr(memory_repo, "bulk_tombstone_scope", fake_bulk)

    out = await handle_memory_clear(
        MemoryClearInput(
            org="clickhouse",
            namespace="finance",
            user_id="alice",
            confirm_subject="alice",
        )
    )
    assert out.total == 47 + 42
    assert out.rows_affected["loom.memory"] == 47
    assert out.rows_affected["loom.memory_embeddings"] == 42
    assert captured["user_id"] == "alice"
    assert captured["agent"] is None


@pytest.mark.asyncio
async def test_clear_with_agent_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory import store as memory_repo
    from server.memory.api.models_crud import MemoryClearInput
    from server.memory.app.handlers import handle_memory_clear

    async def fake_bulk(*, org, namespace, user_id=None, agent=None, ch=None):
        return {"loom.memory": 3, "loom.memory_embeddings": 0}

    monkeypatch.setattr(memory_repo, "bulk_tombstone_scope", fake_bulk)

    out = await handle_memory_clear(
        MemoryClearInput(
            org="clickhouse",
            namespace="finance",
            agent="research-bot",
            confirm_subject="research-bot",
        )
    )
    assert out.total == 3


@pytest.mark.asyncio
async def test_clear_both_scope_args_accepts_either_confirm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When both user_id and agent are set, confirm_subject must match
    one of them (either is OK)."""
    from server.memory import store as memory_repo
    from server.memory.api.models_crud import MemoryClearInput
    from server.memory.app.handlers import handle_memory_clear

    async def fake_bulk(**_kw):
        return {"loom.memory": 0, "loom.memory_embeddings": 0}

    monkeypatch.setattr(memory_repo, "bulk_tombstone_scope", fake_bulk)

    # confirm_subject matches user_id → OK
    await handle_memory_clear(
        MemoryClearInput(
            org="a",
            namespace="n",
            user_id="alice",
            agent="bot",
            confirm_subject="alice",
        )
    )
    # confirm_subject matches agent → also OK
    await handle_memory_clear(
        MemoryClearInput(
            org="a",
            namespace="n",
            user_id="alice",
            agent="bot",
            confirm_subject="bot",
        )
    )
    # confirm_subject matches neither → REJECT
    with pytest.raises(HTTPException):
        await handle_memory_clear(
            MemoryClearInput(
                org="a",
                namespace="n",
                user_id="alice",
                agent="bot",
                confirm_subject="charlie",
            )
        )


@pytest.mark.asyncio
async def test_clear_zero_rows_is_well_formed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clearing a scope with no matching rows returns total=0 without
    raising — same semantics as memory.delete on a missing key."""
    from server.memory import store as memory_repo
    from server.memory.api.models_crud import MemoryClearInput
    from server.memory.app.handlers import handle_memory_clear

    async def fake_bulk(**_kw):
        return {"loom.memory": 0, "loom.memory_embeddings": 0}

    monkeypatch.setattr(memory_repo, "bulk_tombstone_scope", fake_bulk)

    out = await handle_memory_clear(
        MemoryClearInput(
            org="clickhouse",
            namespace="ns",
            user_id="ghost",
            confirm_subject="ghost",
        )
    )
    assert out.total == 0
    assert all(v == 0 for v in out.rows_affected.values())
