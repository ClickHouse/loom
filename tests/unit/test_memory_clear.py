"""Unit tests for memory.clear (bulk-tombstone wire op)."""

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

    async def fake_bulk(*, org, namespace, user_id=None, agent=None, on_tombstoned=None, ch=None):
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

    async def fake_bulk(*, org, namespace, user_id=None, agent=None, on_tombstoned=None, ch=None):
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
async def test_clear_refuses_scopeless_call() -> None:
    from server.memory.api.models_crud import MemoryClearInput
    from server.memory.app.handlers import handle_memory_clear

    with pytest.raises(HTTPException) as exc:
        await handle_memory_clear(
            MemoryClearInput(
                org="clickhouse",
                namespace="finance",
                confirm_subject="anything",
            )
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
                org="clickhouse",
                namespace="finance",
                user_id="alice",
                confirm_subject="bob",  # mismatch
            )
        )
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "CONFIRM_SUBJECT_MISMATCH"


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


def _clear_ch(monkeypatch: pytest.MonkeyPatch, pages: list[list[dict]]):
    from tests.unit.ch_fake import RecordingCH

    ch = RecordingCH().on("SELECT count() AS n FROM loom.memory FINAL", [{"n": 0}], times=1)
    ch.on("SELECT count() AS n", [{"n": 2}])
    for page in pages:
        ch.on("ORDER BY agent, user_id, session_id, memory_key", page, times=1)
    return ch.install(monkeypatch)


@pytest.mark.asyncio
async def test_clear_with_no_subscribers_reads_no_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """A namespace with no webhook pays the counts and the mutations, never a row read."""
    from server.memory.api.models_crud import MemoryClearInput
    from server.memory.app.handlers import handle_memory_clear

    ch = _clear_ch(monkeypatch, [])
    out = await handle_memory_clear(
        MemoryClearInput(org="acme", namespace="chat", user_id="u1", confirm_subject="u1")
    )
    assert out.rows_affected == {"loom.memory": 2, "loom.memory_embeddings": 2}
    assert ch.statements("memory_id") == []
    assert len(ch.statements("ALTER TABLE", kind="execute")) == 2


@pytest.mark.asyncio
async def test_clear_announces_what_it_tombstoned_a_page_at_a_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.api.models_crud import MemoryClearInput
    from server.memory.app import webhooks
    from server.memory.app.handlers import handle_memory_clear
    from server.memory.store import lifecycle

    def row(key: str) -> dict:
        return {
            "org": "acme",
            "namespace": "chat",
            "agent": "bot",
            "user_id": "u1",
            "session_id": "-",
            "memory_key": key,
            "memory_id": f"id-{key}",
            "version": 7,
        }

    monkeypatch.setattr(
        webhooks, "_hooks", {("acme", "chat"): (("wh1", frozenset({"memory.tombstoned"})),)}
    )
    monkeypatch.setattr(lifecycle, "CLEAR_PAGE_ROWS", 2)
    ch = _clear_ch(monkeypatch, [[row("a"), row("b")], [row("c")]])
    announced: list[list[str]] = []

    async def announce(changes) -> None:
        announced.append([c["memory_key"] for _, c in changes])

    monkeypatch.setattr(webhooks, "announce", announce)
    await handle_memory_clear(
        MemoryClearInput(org="acme", namespace="chat", user_id="u1", confirm_subject="u1")
    )
    assert announced == [["a", "b"], ["c"]]
    first, second = ch.statements("ORDER BY agent, user_id, session_id, memory_key")
    assert first.params["a_key"] == "" and second.params["a_key"] == "b"
    # The pages read back exactly the rows this clear's mutation stamped.
    (memory_alter,) = ch.statements("ALTER TABLE loom.memory\n", kind="execute")
    assert first.params["at"] == memory_alter.params["at"]
