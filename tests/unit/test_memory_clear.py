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


def _clear_ch(monkeypatch: pytest.MonkeyPatch, tombstoned: list[dict]):
    from tests.unit.ch_fake import RecordingCH

    ch = RecordingCH().on("SELECT count() AS n FROM loom.memory FINAL", [{"n": 0}], times=1)
    ch.on("SELECT count() AS n", [{"n": 2}])
    if tombstoned:
        ch.on("ORDER BY agent, user_id, session_id, memory_key", tombstoned, times=1)
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

    webhooks._registry._reset_for_tests(
        {("acme", "chat"): (("wh1", frozenset({"memory.tombstoned"})),)},
        read_age_s=0.0,
        due_in_s=float("inf"),
    )
    monkeypatch.setattr(lifecycle, "CLEAR_PAGE_ROWS", 2)
    ch = _clear_ch(monkeypatch, [row("a"), row("b"), row("c")])
    announced: list[list[str]] = []

    async def announce(changes) -> None:
        announced.append([c["memory_key"] for _, c in changes])

    monkeypatch.setattr(webhooks, "announce", announce)
    await handle_memory_clear(
        MemoryClearInput(org="acme", namespace="chat", user_id="u1", confirm_subject="u1")
    )
    assert announced == [["a", "b"], ["c"]]
    # One read fills every page, so the scope is scanned once.
    (read,) = ch.statements("ORDER BY agent, user_id, session_id, memory_key")
    assert read.kind == "raw_stream"
    # The pages read back exactly the rows this clear's mutation stamped.
    (memory_alter,) = ch.statements("ALTER TABLE loom.memory\n", kind="execute")
    assert read.params["at"] == memory_alter.params["at"]


@pytest.mark.asyncio
async def test_clear_announces_before_the_embeddings_step(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed embeddings mutation answers 5xx, and a retry finds no live memory
    rows: the tombstoned rows are announced before that step runs."""
    from server.memory.api.models_crud import MemoryClearInput
    from server.memory.app import webhooks
    from server.memory.app.handlers import handle_memory_clear

    webhooks._registry._reset_for_tests(
        {("acme", "chat"): (("wh1", frozenset({"memory.tombstoned"})),)},
        read_age_s=0.0,
        due_in_s=float("inf"),
    )
    cleared = {"agent": "bot", "user_id": "u1", "session_id": "-", "memory_key": "a", "version": 1}
    ch = _clear_ch(monkeypatch, [cleared])

    async def execute(sql, params=None, **_kw):
        if "loom.memory_embeddings" in sql:
            raise TimeoutError("mutation timed out")

    monkeypatch.setattr(ch, "execute", execute)
    announced: list[str] = []

    async def announce(changes) -> None:
        announced.extend(c["memory_key"] for _, c in changes)

    monkeypatch.setattr(webhooks, "announce", announce)
    with pytest.raises(TimeoutError):
        await handle_memory_clear(
            MemoryClearInput(org="acme", namespace="chat", user_id="u1", confirm_subject="u1")
        )
    assert announced == ["a"]


@pytest.mark.asyncio
async def test_clear_with_a_failed_read_back_still_clears(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.api.models_crud import MemoryClearInput
    from server.memory.app import webhooks
    from server.memory.app.handlers import handle_memory_clear
    from server.observability import degradations

    webhooks._registry._reset_for_tests(
        {("acme", "chat"): (("wh1", frozenset({"memory.tombstoned"})),)},
        read_age_s=0.0,
        due_in_s=float("inf"),
    )

    def fail(sql, params):
        raise TimeoutError("read timed out")

    ch = _clear_ch(monkeypatch, [])
    ch.on("ORDER BY agent, user_id, session_id, memory_key", fail)
    noted: list[tuple[str, str]] = []
    monkeypatch.setattr(degradations, "note", lambda area, what, e=None: noted.append((area, what)))
    out = await handle_memory_clear(
        MemoryClearInput(org="acme", namespace="chat", user_id="u1", confirm_subject="u1")
    )
    assert out.rows_affected == {"loom.memory": 2, "loom.memory_embeddings": 2}
    assert noted == [("memory.webhook", "not_announced")]
