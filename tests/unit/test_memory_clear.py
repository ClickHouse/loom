"""Unit tests for memory.clear (bulk-tombstone wire op)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import HTTPException

A = (("a", "alice", "-", "k1"), "m1")
B = (("a", "alice", "-", "k2"), "m2")


def _row(mode: str = "", until: datetime | None = None) -> Any:
    return type("Row", (), {"lock_mode": mode, "lock_until": until})()


@pytest.fixture
def clear(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The scope's live rows, the tombstones written and the locked count."""
    from server.memory import store as memory_repo

    seen: dict[str, Any] = {"live": [], "locked": 0, "written": [], "scope": None, "skip": None}

    async def _live(**k: Any) -> list:
        seen["scope"] = (k["user_id"], k["agent"])
        return seen["live"]

    async def _heads(keys: list, **_k: Any) -> dict:
        return {k: k[-1] for k in keys}  # the head "row" is its key, for the assertions

    async def _write(heads: list, update: Any, *, skip: Any, ch: Any = None) -> tuple[int, int]:
        assert update(_row())["status"] == "tombstoned"
        seen["skip"] = skip
        seen["written"].extend(heads)
        return len(heads), len(heads)

    async def _locked(**_k: Any) -> int:
        return seen["locked"]

    monkeypatch.setattr(memory_repo, "live_unlocked_identities", _live)
    monkeypatch.setattr(memory_repo, "memory_rows_by_identity_keys", _heads)
    monkeypatch.setattr(memory_repo, "write_versions_of_rows", _write)
    monkeypatch.setattr(memory_repo, "locked_in_scope", _locked)
    return seen


@pytest.mark.asyncio
async def test_clear_writes_a_versioned_tombstone_for_each_listed_row(clear) -> None:
    from server.memory.api.models_crud import MemoryClearInput
    from server.memory.app.handlers import handle_memory_clear

    clear["live"], clear["locked"] = [A, B], 1
    out = await handle_memory_clear(
        MemoryClearInput(org="o", namespace="n", user_id="alice", confirm_subject="alice")
    )
    # The rows written are the rows the journal retracts: one funnel for both.
    assert clear["written"] == ["k1", "k2"] and clear["scope"] == ("alice", None)
    affected = {"loom.memory": 2, "loom.memory_embeddings": 2}
    assert (out.total, out.rows_affected, out.locked_skipped) == (4, affected, 1)
    # A row locked since it was listed is skipped; a lapsed lock is not a lock.
    skip = clear["skip"]
    assert skip(_row("no_delete")) and not skip(_row())
    assert not skip(_row("read_only", datetime.now(UTC) - timedelta(days=1)))


@pytest.mark.asyncio
async def test_clear_with_agent_only(clear) -> None:
    from server.memory.api.models_crud import MemoryClearInput
    from server.memory.app.handlers import handle_memory_clear

    clear["live"] = [A]
    out = await handle_memory_clear(
        MemoryClearInput(org="o", namespace="n", agent="bot", confirm_subject="bot")
    )
    assert out.total == 2 and clear["scope"] == (None, "bot")


@pytest.mark.asyncio
async def test_clear_refuses_scopeless_call() -> None:
    from server.memory.api.models_crud import MemoryClearInput
    from server.memory.app.handlers import handle_memory_clear

    with pytest.raises(HTTPException) as exc:
        await handle_memory_clear(
            MemoryClearInput(org="clickhouse", namespace="finance", confirm_subject="anything")
        )
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "MISSING_SCOPE"
    assert "erasure" in exc.value.detail["message"]


@pytest.mark.asyncio
async def test_clear_refuses_wrong_confirm_subject() -> None:
    from server.memory.api.models_crud import MemoryClearInput
    from server.memory.app.handlers import handle_memory_clear

    with pytest.raises(HTTPException) as exc:
        await handle_memory_clear(
            MemoryClearInput(
                org="clickhouse", namespace="finance", user_id="alice", confirm_subject="bob"
            )
        )
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "CONFIRM_SUBJECT_MISMATCH"


@pytest.mark.asyncio
async def test_clear_both_scope_args_accepts_either_confirm(clear) -> None:
    """When both user_id and agent are set, confirm_subject must match one of them."""
    from server.memory.api.models_crud import MemoryClearInput
    from server.memory.app.handlers import handle_memory_clear

    for who in ("alice", "bot"):
        await handle_memory_clear(
            MemoryClearInput(
                org="a", namespace="n", user_id="alice", agent="bot", confirm_subject=who
            )
        )
    with pytest.raises(HTTPException):
        await handle_memory_clear(
            MemoryClearInput(
                org="a", namespace="n", user_id="alice", agent="bot", confirm_subject="charlie"
            )
        )


@pytest.mark.asyncio
async def test_clear_zero_rows_is_well_formed(clear) -> None:
    """Clearing a scope with no matching rows returns total=0 without raising."""
    from server.memory.api.models_crud import MemoryClearInput
    from server.memory.app.handlers import handle_memory_clear

    out = await handle_memory_clear(
        MemoryClearInput(org="o", namespace="ns", user_id="ghost", confirm_subject="ghost")
    )
    assert out.total == 0 and clear["written"] == []
