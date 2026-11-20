"""An absent conditional set keeps the head it guarded through admission."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi import HTTPException

import server.memory.store as memory_repo
from server.core.errors import ValidationError
from server.memory.api.models_crud import MemorySetInput
from server.memory.app import set_commit, write
from server.memory.app.set_commit import _SetWrite
from server.memory.store.pg_memory import HeadMoved, heads_unchanged, identity_key
from tests.unit.test_memory_key_write_lock import FakeKeeper, _locks

_NOW = datetime(2030, 1, 1)


def _state(existing: Any = None, **kwargs: Any) -> _SetWrite:
    return _SetWrite(
        input=MemorySetInput(
            org="o",
            namespace="n",
            agent="a",
            user_id="u",
            session_id="s",
            memory_key="resource",
            value={"metadata": {"flag": True}},
            if_match_sha256="absent",
            **kwargs,
        ),
        version=10,
        retyped_from="",
        existing=existing,
        expires_at=None,
    )


def _row(*, expired: bool = False, version: int = 1) -> memory_repo.MemoryRow:
    return memory_repo.MemoryRow(
        memory_id="11111111-1111-1111-1111-111111111111",
        org="o",
        namespace="n",
        agent="a",
        user_id="u",
        session_id="s",
        memory_key="resource",
        memory_type="semantic",
        value={"workingMemory": "notes"},
        confidence=1.0,
        created_at=_NOW - timedelta(days=1),
        version=version,
        expires_at=_NOW - timedelta(seconds=1) if expired else None,
        tombstoned_at=None,
        pinned=False,
        lock_mode="",
        lock_until=None,
        status="active",
        redacted_at=None,
    )


@pytest.fixture
def observed(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    reads: list[Any] = []

    async def get_by_key(*args: Any, **kwargs: Any) -> Any:
        reads.append((args, kwargs))
        return None

    monkeypatch.setattr(memory_repo, "get_by_key", get_by_key)
    monkeypatch.setattr(write, "naive_utc_now", lambda: _NOW)
    return reads


async def test_absent_set_accepts_missing_head(observed: list[Any]) -> None:
    w = _state()
    await write._set_check_if_match(w)
    assert w.existing is None
    assert observed == []


async def test_absent_set_rejects_a_live_observed_head(observed: list[Any]) -> None:
    row = _row()
    w = _state(row)
    with pytest.raises(HTTPException) as exc:
        await write._set_check_if_match(w)
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "PRECONDITION_FAILED"
    assert exc.value.detail["current_sha256"] == write.served_sha256(row)
    assert w.existing is row
    assert observed == []


async def test_absent_set_keeps_expired_physical_head_for_admission(observed: list[Any]) -> None:
    row = _row(expired=True)
    w = _state(row)
    await write._set_check_if_match(w)
    assert w.existing is row
    assert observed == []
    recheck = heads_unchanged({identity_key(vars(row)): w.existing}, exact=True)
    recheck(identity_key(vars(row)), vars(row))
    with pytest.raises(HeadMoved):
        recheck(identity_key(vars(row)), vars(_row(expired=True, version=2)))


async def test_absent_set_treats_expiry_boundary_as_absent(observed: list[Any]) -> None:
    row = _row(expired=True)
    row = row.model_copy(update={"expires_at": _NOW})
    w = _state(row)
    await write._set_check_if_match(w)
    assert w.existing is row


async def test_expired_absence_uses_exact_commit_admission(
    monkeypatch: pytest.MonkeyPatch,
    observed: list[Any],
) -> None:
    row = _row(expired=True)
    w = _state(row)
    await write._set_check_if_match(w)
    w.memory_row = vars(row)

    async def commit(_rows: Any, *, admission: Any, **kwargs: Any) -> Any:
        admission.recheck(identity_key(vars(row)), vars(_row(version=2)))
        pytest.fail("an unseen revival passed admission")

    monkeypatch.setattr(set_commit.pg_publish, "commit", commit)
    with pytest.raises(HeadMoved):
        await set_commit.commit_set_rows(w, [w.memory_row], 0)


@pytest.mark.parametrize("guard,dry_run", [("pin", False), ("lock", False), ("pin", True)])
async def test_absent_write_keeps_expired_row_guards(
    monkeypatch: pytest.MonkeyPatch,
    observed: list[Any],
    guard: str,
    dry_run: bool,
) -> None:
    row = _row(expired=True)
    row = row.model_copy(
        update={"pinned": guard == "pin", "lock_mode": "read_only" if guard == "lock" else ""}
    )
    w = _state(dry_run=dry_run)

    async def resolve(inp: Any) -> Any:
        return inp, inp.memory_type, ""

    async def fetch(inp: Any) -> Any:
        return None, row

    monkeypatch.setattr(write, "get_client", lambda: object())
    monkeypatch.setattr(write, "_set_resolve_type", resolve)
    monkeypatch.setattr(write, "_set_fetch_existing", fetch)
    if dry_run:
        result = await write._memory_set_once(w.input)
        assert result.version == 0 and result.noop
        assert result.conflict_verdict == "rejected"
    else:
        with pytest.raises(HTTPException) as exc:
            await write._memory_set_once(w.input)
        assert exc.value.status_code == (409 if guard == "pin" else 423)


@pytest.mark.parametrize("field,value", [("status", "superseded"), ("valid_to", _NOW)])
async def test_absent_set_does_not_hide_readable_states(
    observed: list[Any],
    field: str,
    value: Any,
) -> None:
    row = _row()
    row = row.model_copy(update={field: value})
    with pytest.raises(HTTPException):
        await write._set_check_if_match(_state(row))


async def test_absent_set_refuses_wildcard_identity_before_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def phases(_input: Any) -> Any:
        pytest.fail("wildcard absence condition reached the read")

    monkeypatch.setattr(write, "_memory_set_phases", phases)
    w = _state()
    w.input.agent = "*"
    with pytest.raises(ValidationError, match="concrete agent"):
        await write._handle_memory_set(w.input)


async def test_racing_first_sets_keep_one_observed_absence(
    monkeypatch: pytest.MonkeyPatch,
    observed: list[Any],
) -> None:
    locks = _locks(FakeKeeper(), monkeypatch)
    stored: list[Any] = []
    held: list[int] = []

    async def phases(_input: Any) -> str:
        w = _state(stored[-1] if stored else None)
        held.append(len(locks))
        await write._set_check_if_match(w)
        await asyncio.sleep(0)
        stored.append(_row())
        return "created"

    monkeypatch.setattr(write, "_memory_set_phases", phases)
    results = await asyncio.gather(
        write._handle_memory_set(_state().input),
        write._handle_memory_set(_state().input),
        return_exceptions=True,
    )
    assert results.count("created") == 1
    assert sum(isinstance(result, HTTPException) for result in results) == 1
    assert len(stored) == 1
    assert held == [1, 1]
