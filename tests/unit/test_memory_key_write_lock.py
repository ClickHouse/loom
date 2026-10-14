"""Conditional writes (`if_match_sha256`) to one memory key are serialized."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

import server.memory.store as memory_repo
from server.core.errors import PreconditionFailedError
from server.memory.app import update as update_mod
from server.memory.app import write as write_mod
from server.memory.store import key_write_lock as kl

_IDENT = ("org", "ns", "agent", "-", "-", "proc/deploy/steps")
_ROW_ID = "33333333-3333-3333-3333-333333333333"


class FakeKeeper:
    """The lock table under keeper_map_strict_mode."""

    def __init__(self, *, engine: str = "KeeperMap", fail: bool = False) -> None:
        self.engine = engine
        self.fail = fail
        self.rows: dict[str, dict[str, Any]] = {}

    async def query_one(self, sql: str, params: dict[str, Any]) -> dict[str, Any] | None:
        if "system.tables" in sql:
            return {"engine": self.engine} if self.engine else None
        return self.rows.get(params["name"])

    async def execute(self, sql: str, params: dict[str, Any]) -> None:
        if self.fail:
            raise RuntimeError("Code: 999. Coordination error: connection loss")
        name, row = params["name"], self.rows.get(params["name"])
        if sql.startswith("INSERT"):
            if row:
                raise RuntimeError("Code: 999. KEEPER_EXCEPTION: Node exists")
            self.rows[name] = {"holder": params["me"], "stale": False}
        elif " UPDATE " in sql and "old" not in params:
            pass  # a renewal: the fake keeps no time
        elif " UPDATE " in sql and row and row["holder"] == params["old"] and row["stale"]:
            self.rows[name] = {"holder": params["me"], "stale": False}
        elif " DELETE " in sql and row and row["holder"] == params["me"]:
            del self.rows[name]


def _locks(ch: FakeKeeper, monkeypatch: pytest.MonkeyPatch) -> kl.KeyWriteLocks:
    locks = kl.KeyWriteLocks(ch, wait_s=0.3)  # type: ignore[arg-type]
    monkeypatch.setattr(kl, "key_write_locks", locks)
    return locks


def test_lock_name_is_per_identity_and_key() -> None:
    assert kl.lock_name(*_IDENT) == kl.lock_name(*_IDENT)
    assert kl.lock_name(*_IDENT) != kl.lock_name("org", "ns", "agent-b", "-", "-", _IDENT[5])


async def test_writers_of_one_key_never_overlap(monkeypatch: pytest.MonkeyPatch) -> None:
    locks = _locks(FakeKeeper(engine=""), monkeypatch)
    inside: list[int] = []
    overlap = False

    async def writer() -> None:
        nonlocal overlap
        async with locks.hold(*_IDENT):
            inside.append(1)
            overlap = overlap or len(inside) > 1
            await asyncio.sleep(0.02)
            inside.pop()

    await asyncio.gather(writer(), writer(), writer())
    assert not overlap
    assert len(locks) == 0


async def test_keeper_key_is_taken_and_released(monkeypatch: pytest.MonkeyPatch) -> None:
    ch = FakeKeeper()
    locks = _locks(ch, monkeypatch)
    async with locks.hold(*_IDENT):
        assert ch.rows[kl.lock_name(*_IDENT)]["holder"]
    assert ch.rows == {}


async def test_held_key_times_out_as_precondition_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = FakeKeeper()
    ch.rows[kl.lock_name(*_IDENT)] = {"holder": "other", "stale": False}
    with pytest.raises(PreconditionFailedError):
        async with _locks(ch, monkeypatch).hold(*_IDENT):
            pytest.fail("entered a held lock")


async def test_stale_holder_is_taken_over(monkeypatch: pytest.MonkeyPatch) -> None:
    ch = FakeKeeper()
    name = kl.lock_name(*_IDENT)
    ch.rows[name] = {"holder": "crashed", "stale": True}
    async with _locks(ch, monkeypatch).hold(*_IDENT):
        assert ch.rows[name]["holder"] != "crashed"
    assert name not in ch.rows


async def test_keeper_failure_degrades_to_the_process_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notes: list[tuple[str, str]] = []
    monkeypatch.setattr(kl.degradations, "note", lambda p, e, r="": notes.append((p, e)))
    async with _locks(FakeKeeper(fail=True), monkeypatch).hold(*_IDENT):
        pass
    assert notes == [("memory_key_lock", "process_lock_only")]


async def test_racing_conditional_sets_let_one_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _locks(FakeKeeper(), monkeypatch)
    stored = {"sha": "v1"}

    async def phases(input: Any) -> str:
        if stored["sha"] != input.if_match_sha256:
            raise PreconditionFailedError("stale", current_sha256=stored["sha"])
        await asyncio.sleep(0.02)
        stored["sha"] = input.value
        return str(input.value)

    monkeypatch.setattr(write_mod, "_memory_set_phases", phases)
    results = await asyncio.gather(
        write_mod._handle_memory_set(_set_input("from-a", "v1")),
        write_mod._handle_memory_set(_set_input("from-b", "v1")),
        return_exceptions=True,
    )
    assert sorted(type(r).__name__ for r in results) == ["PreconditionFailedError", "str"]


async def test_unconditional_set_takes_no_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    locks = _locks(FakeKeeper(), monkeypatch)
    seen: list[int] = []

    async def phases(input: Any) -> str:
        seen.append(len(locks))
        return "written"

    monkeypatch.setattr(write_mod, "_memory_set_phases", phases)
    assert await write_mod._handle_memory_set(_set_input("x", "")) == "written"
    assert seen == [0]


async def test_conditional_update_rereads_the_head_under_the_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locks = _locks(FakeKeeper(), monkeypatch)
    stale, head = _row(version=1), _row(version=2)
    held_during_read: list[int] = []
    written: list[Any] = []

    async def get_by_id(memory_id: str, **kw: Any) -> Any:
        held_during_read.append(len(locks))
        return head

    async def write_revision(input: Any, existing: Any, ch: Any, *, op: str) -> str:
        written.append(existing)
        return "ok"

    monkeypatch.setattr(memory_repo, "get_by_id", get_by_id)
    monkeypatch.setattr(update_mod, "_write_revision", write_revision)
    conditional = SimpleNamespace(org="org", if_match_sha256="h")
    assert await update_mod._update_live_row(conditional, stale, None, op="memory.update") == "ok"
    assert held_during_read == [1] and written == [head]

    written.clear()
    plain = SimpleNamespace(org="org", if_match_sha256="")
    assert await update_mod._update_live_row(plain, stale, None, op="memory.update") == "ok"
    assert held_during_read == [1] and written == [stale]


def _set_input(value: str, if_match_sha256: str) -> Any:
    return SimpleNamespace(
        org="org",
        namespace="ns",
        agent="agent",
        user_id="-",
        session_id="-",
        memory_key=_IDENT[5],
        value=value,
        if_match_sha256=if_match_sha256,
    )


def _row(*, version: int) -> Any:
    return SimpleNamespace(
        memory_id=_ROW_ID,
        namespace="ns",
        agent="agent",
        user_id="-",
        session_id="-",
        memory_key=_IDENT[5],
        version=version,
    )


async def test_a_held_lease_is_busy_and_the_body_errors_are_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = FakeKeeper()
    ch.rows["job"] = {"holder": "other", "stale": False}
    locks = _locks(ch, monkeypatch)
    with pytest.raises(kl.LeaseBusy):
        async with locks.lease("job"):
            pytest.fail("entered a held lease")
    # a refusal raised inside the body is not mistaken for a busy lease
    with pytest.raises(PreconditionFailedError):
        async with locks.lease("other-job"):
            raise PreconditionFailedError("the body's own")
    assert "other-job" not in ch.rows  # released


async def test_a_lease_is_renewed_and_a_lost_one_stops_the_body() -> None:
    ch = FakeKeeper()
    locks = kl.KeyWriteLocks(ch, wait_s=0.3, ttl_s=0.03)  # type: ignore[arg-type]
    async with locks.lease("job"):
        await asyncio.sleep(0.05)  # past a third of the TTL: renewed, still ours
        assert ch.rows["job"]["holder"]
    with pytest.raises(kl.LeaseLost):
        async with locks.lease("job"):
            ch.rows["job"] = {"holder": "other", "stale": False}  # taken over meanwhile
            await asyncio.sleep(1)
            pytest.fail("the body ran on without its lease")


async def test_a_lease_keeper_cannot_confirm_is_busy_and_one_without_keeper_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran: list[str] = []
    with pytest.raises(kl.LeaseBusy):
        async with _locks(FakeKeeper(fail=True), monkeypatch).lease("job"):
            ran.append("keeper failing")
    async with _locks(FakeKeeper(engine=""), monkeypatch).lease("job"):
        ran.append("no keeper")
    assert ran == ["no keeper"]


class FlakyKeeper(FakeKeeper):
    """A lock table whose renewals and reads fail while `down` holds."""

    def __init__(self) -> None:
        super().__init__()
        self.down = False

    async def query_one(self, sql: str, params: dict[str, Any]) -> dict[str, Any] | None:
        if self.down and "system.tables" not in sql:
            raise RuntimeError("Code: 999. Coordination error: connection loss")
        return await super().query_one(sql, params)

    async def execute(self, sql: str, params: dict[str, Any]) -> None:
        if self.down and not sql.startswith("INSERT"):
            raise RuntimeError("Code: 999. Coordination error: connection loss")
        await super().execute(sql, params)


async def _run_past_ttl(locks: kl.KeyWriteLocks, ch: FlakyKeeper, *, stale: bool) -> float:
    """Seconds the body ran before its lease was lost."""
    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(kl.LeaseLost):
        async with locks.lease("job"):
            ch.down = not stale
            ch.rows["job"]["stale"] = stale
            await asyncio.sleep(5)
            pytest.fail("the body ran on past its lease")
    return loop.time() - started


async def test_a_lease_whose_renewals_fail_is_lost_at_its_deadline() -> None:
    ch = FlakyKeeper()
    locks = kl.KeyWriteLocks(ch, wait_s=0.3, ttl_s=0.3)  # type: ignore[arg-type]
    assert 0.25 <= await _run_past_ttl(locks, ch, stale=False) < 1.5


async def test_an_expired_lease_is_lost_though_it_names_this_holder() -> None:
    ch = FlakyKeeper()
    locks = kl.KeyWriteLocks(ch, wait_s=0.3, ttl_s=0.3)  # type: ignore[arg-type]
    assert await _run_past_ttl(locks, ch, stale=True) < 1.5


async def test_a_renewal_that_recovers_inside_the_ttl_keeps_the_lease() -> None:
    ch = FlakyKeeper()
    locks = kl.KeyWriteLocks(ch, wait_s=0.3, ttl_s=0.6)  # type: ignore[arg-type]
    async with locks.lease("job"):
        ch.down = True  # the first renewal, at a third of the TTL, fails
        await asyncio.sleep(0.3)
        ch.down = False  # the second, at two thirds, is confirmed
        await asyncio.sleep(1.2)  # two TTLs past the last stamp before the outage
        assert ch.rows["job"]["holder"]
    assert "job" not in ch.rows


async def test_a_renewal_that_fails_is_retried_before_the_next_third_of_the_ttl() -> None:
    ch = FlakyKeeper()
    locks = kl.KeyWriteLocks(ch, wait_s=0.3, ttl_s=0.9)  # type: ignore[arg-type]
    async with locks.lease("job"):
        ch.down = True  # the renewals at a third and two thirds of the TTL both fail
        await asyncio.sleep(0.7)
        ch.down = False  # a retry before the deadline is confirmed
        await asyncio.sleep(0.6)
        assert ch.rows["job"]["holder"]


async def test_a_lease_waits_as_long_as_its_caller_asks(monkeypatch: pytest.MonkeyPatch) -> None:
    ch = FakeKeeper()
    ch.rows["job"] = {"holder": "other", "stale": False}
    locks = _locks(ch, monkeypatch)

    async def release() -> None:
        await asyncio.sleep(0.5)  # past the instance's wait of 0.3 s
        del ch.rows["job"]

    releasing = asyncio.create_task(release())
    async with locks.lease("job", wait_s=3):
        assert ch.rows["job"]["holder"] != "other"
    await releasing


async def test_a_lease_waits_once_for_this_process_and_another_host_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = FakeKeeper()
    locks = _locks(ch, monkeypatch)
    entered = asyncio.Event()

    async def local_pass() -> None:
        async with locks.lease("job"):
            entered.set()
            await asyncio.sleep(0.6)
            ch.rows["job"] = {"holder": "other", "stale": False}  # another host takes it next

    passing = asyncio.create_task(local_pass())
    await entered.wait()
    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(kl.LeaseBusy):
        async with locks.lease("job", wait_s=1.0):
            pytest.fail("entered a held lease")
    assert loop.time() - started < 1.4  # one wait of 1 s for both holders, not two
    await passing
