"""Conditional writes (`if_match_sha256`) to one memory key are serialized."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

import server.memory.store as memory_repo
from server.core.errors import BackendUnavailableError, PreconditionFailedError
from server.memory.app import update as update_mod
from server.memory.app import write as write_mod
from server.memory.store import key_write_lock as kl
from tests.unit.claims_fake import FakeLockTable

_IDENT = ("org", "ns", "agent", "-", "-", "proc/deploy/steps")
_ROW_ID = "33333333-3333-3333-3333-333333333333"


class FakeKeeper(FakeLockTable):
    def __init__(self, *, fail: bool = False) -> None:
        super().__init__()
        self.down = fail


def _locks(ch: FakeKeeper, monkeypatch: pytest.MonkeyPatch) -> kl.KeyWriteLocks:
    locks = kl.KeyWriteLocks(ch, wait_s=0.3)  # type: ignore[arg-type]
    monkeypatch.setattr(kl, "key_write_locks", locks)
    return locks


def test_lock_name_is_per_identity_and_key() -> None:
    assert kl.lock_name(*_IDENT) == kl.lock_name(*_IDENT)
    assert kl.lock_name(*_IDENT) != kl.lock_name("org", "ns", "agent-b", "-", "-", _IDENT[5])


async def test_writers_of_one_key_never_overlap(monkeypatch: pytest.MonkeyPatch) -> None:
    locks = _locks(FakeKeeper(), monkeypatch)
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
        assert ch.holder(kl.lock_name(*_IDENT))
    assert ch.keys == {}


async def test_held_key_times_out_as_precondition_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = FakeKeeper()
    ch.seed(kl.lock_name(*_IDENT), "other")
    with pytest.raises(PreconditionFailedError):
        async with _locks(ch, monkeypatch).hold(*_IDENT):
            pytest.fail("entered a held lock")


async def test_stale_holder_is_taken_over(monkeypatch: pytest.MonkeyPatch) -> None:
    ch = FakeKeeper()
    name = kl.lock_name(*_IDENT)
    ch.seed(name, "crashed", stale=True)
    async with _locks(ch, monkeypatch).hold(*_IDENT):
        assert ch.holder(name) != "crashed"
    assert name not in ch.keys


async def test_keeper_failure_refuses_the_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notes: list[tuple[str, str]] = []
    monkeypatch.setattr(kl.degradations, "note", lambda p, e, r="": notes.append((p, e)))
    with pytest.raises(BackendUnavailableError):
        async with _locks(FakeKeeper(fail=True), monkeypatch).hold(*_IDENT):
            pytest.fail("wrote under a lock only this process held")
    assert notes == [("memory_key_lock", "keeper_unavailable")]


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


async def test_a_pin_holds_the_keys_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """A retirement takes the key's lock too: a pin landing beside it waits for it."""
    locks = _locks(FakeKeeper(), monkeypatch)
    seen: list[int] = []

    async def phases(input: Any) -> str:
        seen.append(len(locks))
        return "written"

    monkeypatch.setattr(write_mod, "_memory_set_phases", phases)
    assert await write_mod._handle_memory_set(_set_input("x", "", pinned=True)) == "written"
    assert seen == [1]


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


def _set_input(value: str, if_match_sha256: str, pinned: bool = False) -> Any:
    return SimpleNamespace(
        org="org",
        namespace="ns",
        agent="agent",
        user_id="-",
        session_id="-",
        memory_key=_IDENT[5],
        value=value,
        if_match_sha256=if_match_sha256,
        pinned=pinned,
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
    ch.seed("job", "other")
    locks = _locks(ch, monkeypatch)
    with pytest.raises(kl.LeaseBusy):
        async with locks.lease("job"):
            pytest.fail("entered a held lease")
    # a refusal raised inside the body is not mistaken for a busy lease
    with pytest.raises(PreconditionFailedError):
        async with locks.lease("other-job"):
            raise PreconditionFailedError("the body's own")
    assert "other-job" not in ch.keys  # released


async def test_a_hold_cancelled_after_its_insert_landed_releases_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SlowReply(FakeKeeper):
        landed = asyncio.Event()

        async def execute(self, sql: str, params: Any = None, **kw: Any) -> None:
            await super().execute(sql, params, **kw)
            if sql.startswith("INSERT"):
                self.landed.set()
                await asyncio.sleep(5)

    ch = SlowReply()
    locks = _locks(ch, monkeypatch)

    async def take() -> None:
        async with locks.hold(*_IDENT):
            pytest.fail("entered before the insert answered")

    taking = asyncio.create_task(take())
    await ch.landed.wait()
    taking.cancel()
    with pytest.raises(asyncio.CancelledError):
        await taking
    assert ch.keys == {} and len(locks) == 0


async def test_a_writer_queued_in_this_process_past_its_wait_asks_no_keeper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = FakeKeeper()
    locks = kl.KeyWriteLocks(ch, wait_s=0.2)  # type: ignore[arg-type]
    entered = asyncio.Event()

    async def first() -> None:
        async with locks.hold(*_IDENT):
            entered.set()
            await asyncio.sleep(0.4)

    holding = asyncio.create_task(first())
    await entered.wait()
    sent = len(ch.sql)
    with pytest.raises(PreconditionFailedError):
        async with locks.hold(*_IDENT):
            pytest.fail("entered a held lock")
    assert len(ch.sql) == sent
    await holding


@pytest.mark.parametrize(("op", "what"), [("memory.pin", "the pin"), ("memory.set", "the write")])
async def test_a_busy_key_names_what_to_retry(
    monkeypatch: pytest.MonkeyPatch, op: str, what: str
) -> None:
    """memory.pin, or a memory.set with pinned=true: each says to retry itself."""
    from server.core.errors import PreconditionFailedError
    from server.memory.app.handlers import handle_memory_pin
    from server.memory.store import key_write_lock as kl

    handler = {"memory.pin": handle_memory_pin, "memory.set": write_mod.handle_memory_set}[op]
    _locks(FakeKeeper(), monkeypatch)

    async def phases(input: Any) -> str:
        return "written"

    monkeypatch.setattr(write_mod, "_memory_set_phases", phases)
    held = _set_input("x", "", pinned=True)
    async with kl.key_write_locks.hold("org", "ns", "agent", "-", "-", _IDENT[5]):
        with pytest.raises(PreconditionFailedError, match=f"retry {what}"):
            await handler(held)
