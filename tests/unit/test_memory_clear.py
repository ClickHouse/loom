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
    from server.memory.app import temporal_projection
    from server.memory.app import temporal_publisher as publisher
    from server.memory.store import temporal as temporal_store

    seen: dict[str, Any] = {
        "live": [], "locked": 0, "written": [], "scope": None, "skip": None,
        "journal": [], "published": [],
    }  # fmt: skip

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

    async def _journal_only(*_a: Any, **_k: Any) -> list:
        return seen["journal"]

    async def _publish(org: str, ns: str, items: list, **_k: Any) -> list:
        seen["published"].extend(items)
        return [type("R", (), {"commit_seq": 7, "ident": ident})() for ident, _m, _b in items]

    async def _project(org: str, ns: str, idents: list, **_k: Any) -> dict:
        seen["projected"] = idents
        return {}

    monkeypatch.setattr(temporal_store, "journal_only_identities", _journal_only)
    monkeypatch.setattr(publisher, "publish_amendments", _publish)
    monkeypatch.setattr(temporal_projection, "project_keys", _project)
    monkeypatch.setattr(publisher, "read_token", lambda receipts: "t")
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
    affected = {"loom.memory": 2, "loom.memory_embeddings": 2, "loom.memory_temporal_commits": 0}
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


@pytest.mark.asyncio
async def test_clear_retracts_a_key_only_the_journal_holds_unless_a_lock_keeps_it(clear) -> None:
    from server.memory.api.models_crud import MemoryClearInput
    from server.memory.app.handlers import handle_memory_clear
    from server.memory.domain import temporal_model as tm

    ident = ("a", "alice", "-", "plan")
    clear["journal"] = [(ident, "00000000-0000-0000-0000-000000000009", "human:alice")]
    out = await handle_memory_clear(
        MemoryClearInput(org="o", namespace="n", user_id="alice", confirm_subject="alice")
    )
    assert out.rows_affected["loom.memory_temporal_commits"] == 1
    # A plan that came due while the clear ran has a row: it is brought in line.
    assert clear["projected"] == [ident] and out.current_rows_pending is False
    ((got, _mid, build),) = clear["published"]
    plan = tm.Assertion("p1", "p1", "launch")
    assert got == ident and build([plan]).retired == ("p1",)
    locked = tm.Assertion("p2", "p2", "launch", lock_mode="read_only")
    assert build([locked]).empty


@pytest.mark.asyncio
async def test_delete_of_a_journal_only_key_prefers_the_exact_identity_and_checks_its_author(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.api.models_crud import MemoryDeleteInput
    from server.memory.app import handlers as hm
    from server.memory.store import temporal as temporal_store

    asked: list[dict] = []
    mine, theirs = ("a", "u", "s1", "k"), ("b", "u", "s2", "k")

    async def _journal_only(org: str, ns: str, **k: Any) -> list:
        asked.append(k)
        exact = k.get("agent") == "a"
        return (
            [(mine, "m1", "human:me")] if exact else [(theirs, "m2", "human:x"), (mine, "m1", "")]
        )

    retracted: list = []

    async def _retract(org: str, ns: str, idents: list, op: str) -> tuple[list, bool]:
        retracted.extend(idents)
        return [type("R", (), {"commit_seq": 3, "ident": idents[0][0]})()], False

    authors: list[str] = []
    monkeypatch.setattr(temporal_store, "journal_only_identities", _journal_only)
    monkeypatch.setattr(hm, "_retract_journal_only", _retract)
    monkeypatch.setattr(hm, "assert_own_row", lambda row, op: authors.append(row.actor))
    monkeypatch.setattr(hm, "request_scope", lambda: "scope")
    monkeypatch.setattr("server.memory.app.temporal_publisher.read_token", lambda r: "t")
    out = await hm._delete_journal_only(
        MemoryDeleteInput(
            org="o", namespace="n", agent="a", user_id="u", session_id="s1", memory_key="k"
        )
    )
    assert out is not None and out.found and out.commit_seq == 3
    assert retracted == [(mine, "m1")] and authors == ["human:me"]
    assert asked[0]["scope"] == "scope" and asked[0]["session_id"] == "s1"
