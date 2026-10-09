"""Conditional writes (`if_match_sha256`) to one memory key are serialized."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from server.core.errors import PreconditionFailedError
from server.memory.app import update as update_mod
from server.memory.app import write as write_mod
from server.memory.store import key_write_lock as kl

_IDENT = ("org", "ns", "agent", "-", "-", "proc/deploy/steps")


class FakeKeeper:
    """The lock table under keeper_map_strict_mode."""

    def __init__(self, *, engine: str = "KeeperMap", fail: bool = False) -> None:
        self.engine = engine
        self.fail = fail
        self.rows: dict[str, dict[str, Any]] = {}
        self.statements: list[str] = []

    async def query_one(self, sql: str, params: dict[str, Any]) -> dict[str, Any] | None:
        if "system.tables" in sql:
            return {"engine": self.engine} if self.engine else None
        row = self.rows.get(params["name"])
        return None if row is None else {"holder": row["holder"], "stale": row["stale"]}

    async def execute(self, sql: str, params: dict[str, Any]) -> None:
        self.statements.append(sql.split()[0])
        if self.fail:
            raise RuntimeError("Code: 999. Coordination error: connection loss")
        name = params["name"]
        if sql.startswith("INSERT"):
            if name in self.rows:
                raise RuntimeError("Code: 999. KEEPER_EXCEPTION: Node exists")
            self.rows[name] = {"holder": params["me"], "stale": False}
        elif " UPDATE " in sql:
            row = self.rows.get(name)
            if row and row["holder"] == params["old"] and row["stale"]:
                self.rows[name] = {"holder": params["me"], "stale": False}
        elif " DELETE " in sql:
            if self.rows.get(name, {}).get("holder") == params["me"]:
                del self.rows[name]


@pytest.fixture(autouse=True)
def _fresh_lock_state(monkeypatch: pytest.MonkeyPatch) -> Any:
    kl._reset_for_tests()
    monkeypatch.setattr(kl, "WAIT_S", 0.3)
    yield
    kl._reset_for_tests()


def test_lock_name_is_per_identity_and_key() -> None:
    assert kl.lock_name(*_IDENT) == kl.lock_name(*_IDENT)
    other_agent = (*_IDENT[:2], "agent-b", *_IDENT[3:])
    assert kl.lock_name(*_IDENT) != kl.lock_name(*other_agent)
    assert kl.lock_name(*_IDENT).startswith("memory_key/")


async def test_two_writers_of_one_key_never_overlap() -> None:
    ch = FakeKeeper(engine="")
    inside: list[int] = []
    overlap = False

    async def writer() -> None:
        nonlocal overlap
        async with kl.key_write_lock(*_IDENT, ch=ch):
            inside.append(1)
            overlap = overlap or len(inside) > 1
            await asyncio.sleep(0.02)
            inside.pop()

    await asyncio.gather(writer(), writer(), writer())
    assert not overlap
    assert len(kl._local) == 0


async def test_keeper_key_is_taken_and_released() -> None:
    ch = FakeKeeper()
    async with kl.key_write_lock(*_IDENT, ch=ch) as name:
        assert ch.rows[name]["holder"]
    assert ch.rows == {}
    assert ch.statements == ["INSERT", "ALTER"]


async def test_held_key_answers_precondition_failed_after_the_wait() -> None:
    ch = FakeKeeper()
    name = kl.lock_name(*_IDENT)
    ch.rows[name] = {"holder": "another-process", "stale": False}
    with pytest.raises(PreconditionFailedError):
        async with kl.key_write_lock(*_IDENT, ch=ch):
            pytest.fail("entered a held lock")
    assert ch.rows[name]["holder"] == "another-process"


async def test_holder_past_its_ttl_is_taken_over() -> None:
    ch = FakeKeeper()
    name = kl.lock_name(*_IDENT)
    ch.rows[name] = {"holder": "crashed-process", "stale": True}
    async with kl.key_write_lock(*_IDENT, ch=ch):
        assert ch.rows[name]["holder"] != "crashed-process"
    assert name not in ch.rows


async def test_keeper_failure_falls_back_to_the_process_lock() -> None:
    ch = FakeKeeper(fail=True)
    ran = False
    async with kl.key_write_lock(*_IDENT, ch=ch):
        ran = True
    assert ran


async def test_racing_conditional_sets_let_exactly_one_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(kl, "_keeper_available", _no_keeper)
    stored = {"sha": "v1"}

    async def phases(input: Any) -> str:
        if stored["sha"] != input.if_match_sha256:
            raise PreconditionFailedError("stale", current_sha256=stored["sha"])
        await asyncio.sleep(0.02)
        stored["sha"] = input.value
        return input.value

    monkeypatch.setattr(write_mod, "_memory_set_phases", phases)
    a = _set_input(value="from-a", if_match_sha256="v1")
    b = _set_input(value="from-b", if_match_sha256="v1")
    results = await asyncio.gather(
        write_mod._handle_memory_set(a), write_mod._handle_memory_set(b), return_exceptions=True
    )
    assert sorted(type(r).__name__ for r in results) == ["PreconditionFailedError", "str"]
    assert stored["sha"] in {"from-a", "from-b"}


async def test_unconditional_set_takes_no_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    async def phases(input: Any) -> str:
        return "written"

    def no_lock(*a: Any, **k: Any) -> Any:
        raise AssertionError("a write without if_match_sha256 must not lock")

    monkeypatch.setattr(write_mod, "_memory_set_phases", phases)
    monkeypatch.setattr(kl, "key_write_lock", no_lock)
    assert await write_mod._handle_memory_set(_set_input(value="x", if_match_sha256="")) == (
        "written"
    )


async def test_conditional_update_rereads_the_head_under_the_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(kl, "_keeper_available", _no_keeper)
    stale = _row(version=1)
    head = _row(version=2)
    reads: list[str] = []

    async def get_by_id(memory_id: str, **kw: Any) -> Any:
        reads.append(memory_id)
        assert len(kl._local) == 1
        return head

    seen: list[Any] = []

    async def write_revision(input: Any, existing: Any, ch: Any, *, op: str) -> str:
        seen.append(existing)
        return "ok"

    import server.memory.store as memory_repo

    monkeypatch.setattr(memory_repo, "get_by_id", get_by_id)
    monkeypatch.setattr(update_mod, "_write_revision", write_revision)
    inp = SimpleNamespace(org="org", if_match_sha256="h")
    assert await update_mod._update_live_row(inp, stale, None, op="memory.update") == "ok"
    assert reads == [stale.memory_id] and seen == [head]

    seen.clear()
    reads.clear()
    plain = SimpleNamespace(org="org", if_match_sha256="")
    assert await update_mod._update_live_row(plain, stale, None, op="memory.update") == "ok"
    assert reads == [] and seen == [stale]


async def _no_keeper(ch: Any) -> bool:
    return False


def _set_input(*, value: str, if_match_sha256: str) -> Any:
    return SimpleNamespace(
        org="org",
        namespace="ns",
        agent="agent",
        user_id="-",
        session_id="-",
        memory_key="proc/deploy/steps",
        value=value,
        if_match_sha256=if_match_sha256,
    )


def _row(*, version: int) -> Any:
    return SimpleNamespace(
        memory_id="33333333-3333-3333-3333-333333333333",
        namespace="ns",
        agent="agent",
        user_id="-",
        session_id="-",
        memory_key="proc/deploy/steps",
        version=version,
    )
