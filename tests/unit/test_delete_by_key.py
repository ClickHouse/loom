"""A delete names a key, not the identity that wrote it — the
handler looks namespace-wide after an exact-identity miss and says `found:
false` when nothing is live; loom_search re-seeds onto the caller's own
namespace, not the first alphabetically."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

import server.memory.app.handlers as hm
from server.identity.app.caller_home import home_namespace
from server.memory import store as memory_repo
from server.memory.app import restore as _restore_mod
from server.memory.app import stack as _stack_mod
from server.memory.store import key_write_lock


def test_home_namespace_prefers_the_callers_own() -> None:
    mine = ["core-tracker", "team-alerts", "jdoe"]
    assert home_namespace(mine, "jdoe") == "jdoe"
    assert home_namespace(mine, "jdoe@example.com") == "jdoe"
    assert home_namespace(mine, "JDoe") == "jdoe"
    assert home_namespace(mine, "someone-else") == "core-tracker", (
        "no own namespace: the first of the list"
    )
    assert home_namespace(mine, "someone-else", "team-alerts") == "team-alerts", (
        "rev 1: a saved default in the list is kept"
    )
    assert home_namespace(mine, "someone-else", "code-x") == "core-tracker", (
        "a saved default outside the list is not"
    )
    assert home_namespace(mine, "jdoe", "core-tracker") == "jdoe", (
        "the caller's own namespace beats the saved default"
    )
    assert home_namespace([], "jdoe") == ""


class _CH:
    def __init__(self) -> None:
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []

    async def insert(self, table: str, rows: list[dict[str, Any]], settings: Any = None) -> None:
        self.inserts.append((table, rows))

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        return []

    async def query_one(self, sql: str, params: Any = None) -> Any:
        return None


def _row(agent: str, session_id: str) -> Any:
    return SimpleNamespace(
        org="clickhouse",
        namespace="jdoe",
        agent=agent,
        user_id="-",
        session_id=session_id,
        memory_key="user_home_location",
        memory_id="33333333-3333-3333-3333-333333333333",
        memory_type="semantic",
        value='"x"',
        run_id="",
        source_class="extracted",
        tags=[],
        entities=[],
        version=1,
        locked=False,
    )


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {
        "lookups": [],
        "row": _row("claude-code", "sess-65187490"),
        "ch": _CH(),
    }

    async def _get_by_key(identity: Any, key: str, **kw: Any) -> Any:
        state["lookups"].append((identity.agent, identity.user_id, identity.session_id, key))
        if identity.agent == "*":
            return state["row"]
        return None  # the caller's own identity never wrote this key

    @asynccontextmanager
    async def _no_lease(*_key: str) -> AsyncIterator[None]:
        yield

    async def _no_later_write(*_a: Any, **_k: Any) -> dict[Any, Any]:
        return {}

    monkeypatch.setattr(memory_repo, "get_by_key", _get_by_key)
    monkeypatch.setattr(key_write_lock.key_write_locks, "hold", _no_lease)
    # memory.delete_version's re-read after its tombstone: no write landed in between.
    monkeypatch.setattr(memory_repo, "versions_above", _no_later_write)

    async def _record_check(check: dict[str, Any], **kw: Any) -> None:
        state.setdefault("checks", []).append({**check, **kw})

    monkeypatch.setattr(memory_repo, "record_delete_check", _record_check)

    async def _no_check(*_a: Any, **_k: Any) -> None:
        return None  # no delete of this version recorded a check

    monkeypatch.setattr(memory_repo, "find_delete_check", _no_check)
    monkeypatch.setattr(hm, "get_client", lambda: state["ch"])
    monkeypatch.setattr(_stack_mod, "get_client", lambda: state["ch"])
    monkeypatch.setattr(_restore_mod, "get_client", lambda: state["ch"])
    monkeypatch.setattr("server.memory.app.write.get_client", hm.get_client)
    monkeypatch.setattr("server.memory.app.update.get_client", hm.get_client)
    monkeypatch.setattr(
        memory_repo,
        "build_memory_version_row",
        lambda existing, **kw: {
            "memory_key": existing.memory_key,
            "agent": existing.agent,
            "session_id": existing.session_id,
            **kw,
        },
    )
    # The stub builder returns partial rows; the funnel's full-row check is
    # pinned in test_rmt_write_is_typed.
    monkeypatch.setattr("server.memory.store.inserts.check_memory_rows", lambda rows: None)
    monkeypatch.setattr(hm._lock, "assert_unlocked", lambda *a, **k: None)
    return state


def test_delete_looks_namespace_wide_after_an_exact_miss_and_tombstones_the_rows_identity(
    world,
) -> None:
    from server.memory.api.models_crud import MemoryDeleteInput

    async def _run() -> Any:
        return await hm.handle_memory_delete(
            MemoryDeleteInput(
                org="clickhouse",
                namespace="jdoe",
                agent="claude-desktop",
                memory_key="user_home_location",
            )
        )

    out = asyncio.run(_run())
    assert out.tombstoned is True and out.found is True
    assert world["lookups"][0][0] == "claude-desktop" and world["lookups"][1][0] == "*"
    table, rows = world["ch"].inserts[0]
    assert table == "loom.memory"
    assert rows[0]["agent"] == "claude-code" and rows[0]["session_id"] == "sess-65187490"


@pytest.mark.parametrize(("wrote", "shape"), [(True, "webhook"), (False, "")])
def test_a_delete_is_held_to_the_webhook_budget_only_when_it_wrote_outbox_rows(
    world, monkeypatch: pytest.MonkeyPatch, wrote: bool, shape: str
) -> None:
    from server.memory.api.models_crud import MemoryDeleteInput
    from server.observability.budget_shape import current_budget_shape

    async def _announce(changes: Any) -> bool:
        return wrote

    monkeypatch.setattr(hm.webhooks, "announce", _announce)

    async def _run() -> str:
        await hm.handle_memory_delete(
            MemoryDeleteInput(
                org="clickhouse",
                namespace="jdoe",
                agent="claude-desktop",
                memory_key="user_home_location",
            )
        )
        return current_budget_shape()

    assert asyncio.run(_run()) == shape


def test_delete_says_when_no_live_row_exists_anywhere(world, monkeypatch) -> None:
    from server.memory.api.models_crud import MemoryDeleteInput

    world["row"] = None

    async def _run() -> Any:
        return await hm.handle_memory_delete(
            MemoryDeleteInput(
                org="clickhouse",
                namespace="jdoe",
                agent="claude-desktop",
                memory_key="never_written",
            )
        )

    out = asyncio.run(_run())
    assert out.tombstoned is False and out.found is False
    assert world["ch"].inserts == []


def _delete_at(version: int | str, agent: str = "claude-code") -> Any:
    from server.memory.api.models_crud import MemoryDeleteVersionInput

    return asyncio.run(
        hm.handle_memory_delete_version(
            MemoryDeleteVersionInput(
                org="clickhouse",
                namespace="jdoe",
                agent=agent,
                session_id="sess-65187490",
                memory_key="user_home_location",
                version=version,
            )
        )
    )


def test_a_versioned_delete_never_falls_back_to_another_agents_row(world) -> None:
    out = _delete_at(1, agent="claude-desktop")
    assert out.tombstoned is False and out.found is False
    assert [lk[0] for lk in world["lookups"]] == ["claude-desktop"]
    assert world["ch"].inserts == []


def test_a_versioned_delete_keeps_a_row_replaced_since_the_read(world, monkeypatch) -> None:
    async def _get_by_key(identity: Any, key: str, **kw: Any) -> Any:
        return world["row"]

    monkeypatch.setattr(memory_repo, "get_by_key", _get_by_key)
    out = _delete_at(world["row"].version + 1)
    assert out.tombstoned is False and out.reason == "version_changed"
    assert world["ch"].inserts == []
    assert _delete_at(world["row"].version).tombstoned is True


def test_every_unaddressed_search_takes_the_home_namespace() -> None:
    """The rule fires whenever the caller named no namespace, not only
    when the saved default fell outside the fan-out list."""
    import inspect

    from server.transport import mcp_call

    bind = inspect.getsource(mcp_call._bind_caller_identity)
    assert "if op.name == SEARCH_OP and filled_ns:" in bind
    assert "home_namespace(" in inspect.getsource(mcp_call._address_home)


def test_get_looks_namespace_wide_after_an_exact_miss_for_the_same_user(world) -> None:
    """memory.get has the delete's fallback — the caller's own identity
    first, then the namespace-wide `*` form; the wide row is accepted only
    when it belongs to the caller's user."""
    from server.memory.api.models_crud import MemoryGetInput

    world["row"] = SimpleNamespace(
        **{**vars(world["row"]), "memory_key": "coffee_type", "value": '"espresso"'}
    )
    mine = memory_repo.Identity(
        org="clickhouse", namespace="jdoe", agent="claude-code", user_id="-", session_id="-"
    )
    inp = MemoryGetInput(
        org="clickhouse", namespace="jdoe", agent="claude-code", memory_key="coffee_type"
    )
    row = asyncio.run(hm._point_lookup(mine, inp))
    assert row is not None and row.value == '"espresso"'
    assert [lk[0] for lk in world["lookups"]] == ["claude-code", "*"]
    world["lookups"].clear()
    wide = memory_repo.Identity(
        org="clickhouse", namespace="jdoe", agent="*", user_id="-", session_id="-"
    )
    assert asyncio.run(hm._point_lookup(wide, inp)) is not None
    assert [lk[0] for lk in world["lookups"]] == ["*"], (
        "the wide form is not retried against itself"
    )
    # Another user's row under the same key is not served (per-user isolation).
    bob = memory_repo.Identity(
        org="clickhouse", namespace="jdoe", agent="claude-code", user_id="bob", session_id="-"
    )
    bob_inp = MemoryGetInput(
        org="clickhouse",
        namespace="jdoe",
        agent="claude-code",
        user_id="bob",
        memory_key="coffee_type",
    )
    assert asyncio.run(hm._point_lookup(bob, bob_inp)) is None


def test_delete_does_not_tombstone_another_users_row(world) -> None:
    """The wide by-key lookup can hand the delete another user's row; it is
    refused as not found, and nothing is inserted."""
    from server.memory.api.models_crud import MemoryDeleteInput

    async def _run() -> Any:
        return await hm.handle_memory_delete(
            MemoryDeleteInput(
                org="clickhouse",
                namespace="jdoe",
                agent="claude-desktop",
                user_id="bob",
                memory_key="user_home_location",
            )
        )

    out = asyncio.run(_run())
    assert out.tombstoned is False and out.found is False
    assert world["ch"].inserts == []


def test_a_versioned_delete_needs_a_concrete_agent(world) -> None:
    from server.core.errors import ValidationError

    with pytest.raises(ValidationError, match="concrete agent"):
        _delete_at(1, agent="*")
    assert world["ch"].inserts == []


def test_a_versioned_delete_takes_the_version_as_a_decimal_string() -> None:
    # A nanosecond version is above 2^53: a JavaScript caller sends it as a string.
    import pydantic

    from server.memory.api.models_crud import MemoryDeleteVersionInput

    ident = {"org": "o", "namespace": "n", "agent": "a", "memory_key": "k"}
    big = 1_760_000_000_000_000_001
    assert MemoryDeleteVersionInput(**ident, version=str(big)).version == big
    # Anything a UInt64 cannot hold, or digits outside ASCII, is refused before a read.
    for bad in ("latest", -1, 2**64, str(2**64), "١٢٣"):
        with pytest.raises(pydantic.ValidationError, match="version number"):
            MemoryDeleteVersionInput(**ident, version=bad)


def test_a_redact_takes_a_decimal_version_back_exactly() -> None:
    import pydantic

    from server.memory.api.models_crud import MemoryRedactInput

    big = 1_760_000_000_000_000_001
    assert MemoryRedactInput(org="o", memory_id="m", version=str(big)).version == big
    with pytest.raises(pydantic.ValidationError, match="version number"):
        MemoryRedactInput(org="o", memory_id="m", version="latest")


def test_a_versioned_delete_tombstones_the_version_it_read(world, monkeypatch) -> None:
    async def _get_by_key(identity: Any, key: str, **kw: Any) -> Any:
        return world["row"]

    monkeypatch.setattr(memory_repo, "get_by_key", _get_by_key)
    out = _delete_at(world["row"].version)
    assert out.tombstoned is True and out.version > world["row"].version
    memory_rows = [r for t, rows in world["ch"].inserts if t == "loom.memory" for r in rows]
    assert [(r["version"], r["tombstoned_at"] is not None) for r in memory_rows] == [
        (out.version, True)
    ]


def test_a_versioned_delete_checks_and_tombstones_under_the_key_lock(world, monkeypatch) -> None:
    async def _get_by_key(identity: Any, key: str, **kw: Any) -> Any:
        return world["row"]

    held: list[tuple[str, ...]] = []

    @asynccontextmanager
    async def _hold(*key: str):
        held.append(key)
        yield
        # the tombstone landed inside the lease
        assert world["ch"].inserts

    monkeypatch.setattr(memory_repo, "get_by_key", _get_by_key)
    monkeypatch.setattr(key_write_lock.key_write_locks, "hold", _hold)
    assert _delete_at(world["row"].version).tombstoned is True
    assert held == [
        ("clickhouse", "jdoe", "claude-code", "-", "sess-65187490", "user_home_location")
    ]
