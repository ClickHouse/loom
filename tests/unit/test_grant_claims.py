"""One change at a time per grant: an op that reads a grant and then writes it
holds the grant from the read to the write, so a concurrent change of the same
grant waits instead of being written over."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.auth import grant_claims
from server.core.errors import BackendUnavailableError, GrantBusyError
from server.core.keeper_claims import Claim
from server.identity.api.models_grants import GrantsApproveInput, GrantsCreateInput
from server.identity.app import grants as grants_app
from server.identity.domain.grants import GrantState
from server.identity.store import grants as grants_repo


async def _change(log: list[str], name: str, key: tuple[str, str, str], hold: float) -> None:
    async with grant_claims.grant_change(*key):
        log.append(f"{name} in")
        await asyncio.sleep(hold)
        log.append(f"{name} out")


def test_without_a_keeper_two_changes_of_one_grant_run_one_after_the_other() -> None:
    log: list[str] = []

    async def _both() -> None:
        key = ("acme", "team", "alice")
        await asyncio.gather(_change(log, "a", key, 0.05), _change(log, "b", key, 0))

    asyncio.run(_both())
    assert log == ["a in", "a out", "b in", "b out"]
    assert len(grant_claims._local) == 0, "the last change out drops the key's lock"


def test_reads_under_a_claim_see_the_last_holder_s_write() -> None:
    from server.core.ch import sequential_reads_active

    async def _read() -> bool:
        async with grant_claims.grant_change("acme", "team", "alice"):
            return sequential_reads_active()

    assert asyncio.run(_read())


def test_a_change_that_outruns_its_bound_is_cut_off_and_released(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    released: list[str] = []

    async def _held(ch: Any, key: str) -> tuple[Claim, str]:
        return Claim.HELD, "me"

    async def _release(ch: Any, key: str, holder: str) -> None:
        released.append(holder)

    monkeypatch.setattr(grant_claims.CLAIMS, "claim", _held)
    monkeypatch.setattr(grant_claims.CLAIMS, "release_shielded", _release)
    monkeypatch.setattr(grant_claims, "CHANGE_S", 0.05)
    log: list[str] = []
    with pytest.raises(BackendUnavailableError):
        asyncio.run(_change(log, "slow", ("acme", "team", "alice"), 1.0))
    assert log == ["slow in"] and released == ["me"], "no write after the bound"


def test_changes_of_different_grants_do_not_wait_on_each_other() -> None:
    log: list[str] = []

    async def _both() -> None:
        await asyncio.gather(
            _change(log, "a", ("acme", "team", "alice"), 0.05),
            _change(log, "b", ("acme", "team", "bob"), 0),
        )

    asyncio.run(_both())
    assert log == ["a in", "b in", "b out", "a out"]


def test_a_change_waits_for_the_holder_then_gives_up_retryably(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claims: list[str] = []

    async def _taken(ch: Any, key: str) -> tuple[Claim, str]:
        claims.append(key)
        return Claim.TAKEN, ""

    monkeypatch.setattr(grant_claims.CLAIMS, "claim", _taken)
    monkeypatch.setattr(grant_claims, "WAIT_S", 0.12)
    with pytest.raises(GrantBusyError) as busy:
        asyncio.run(_change([], "a", ("acme", "team", "alice"), 0))
    assert len(claims) > 1, "it retried before giving up"
    assert busy.value.status_code == 503 and busy.value.extra["retry_after_s"] == 1


def test_a_keeper_that_cannot_answer_refuses_retryably(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _down(ch: Any, key: str) -> tuple[Claim, str]:
        raise TimeoutError

    monkeypatch.setattr(grant_claims.CLAIMS, "claim", _down)
    with pytest.raises(BackendUnavailableError):
        asyncio.run(_change([], "a", ("acme", "team", "alice"), 0))


@pytest.mark.parametrize("fails", [False, True])
def test_a_held_claim_is_released_when_the_change_ends(
    monkeypatch: pytest.MonkeyPatch, fails: bool
) -> None:
    released: list[tuple[str, str]] = []

    async def _held(ch: Any, key: str) -> tuple[Claim, str]:
        return Claim.HELD, "me"

    async def _release(ch: Any, key: str, holder: str) -> None:
        released.append((key, holder))

    monkeypatch.setattr(grant_claims.CLAIMS, "claim", _held)
    monkeypatch.setattr(grant_claims.CLAIMS, "release_shielded", _release)

    async def _run() -> None:
        async with grant_claims.grant_change("acme", "team", "alice"):
            if fails:
                raise RuntimeError("the write failed")

    if fails:
        with pytest.raises(RuntimeError):
            asyncio.run(_run())
    else:
        asyncio.run(_run())
    assert released == [('["acme", "team", "alice"]', "me")]


def test_a_promotion_during_an_approval_waits_for_it_instead_of_being_written_over(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The approval reads writer with a pending elevation; the promotion to admin
    # arrives while it decides. Kept apart, the promotion lands last.
    rows: list[dict[str, Any]] = []
    approving = asyncio.Event()

    async def _find(org: str, ns: str, subject: str, **_k: Any) -> GrantState:
        approving.set()
        await asyncio.sleep(0.05)
        return GrantState.from_row(
            {"role": "writer", "status": "active", "pending_role": "writer", "granted_by": "x"}
        )

    async def _active(org: str, ns: str, subject: str, **_k: Any) -> None:
        return None

    async def _insert(row: dict[str, Any], **_k: Any) -> None:
        rows.append(row)

    async def _same(org: str, subject: str, **_k: Any) -> str:
        return subject

    async def _nothing(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(grants_repo, "find_any_grant", _find)
    monkeypatch.setattr(grants_repo, "find_active_grant", _active)
    monkeypatch.setattr(grants_repo, "insert_row", _insert)
    monkeypatch.setattr("server.auth.aliases.canonical_subject", _same)
    monkeypatch.setattr(grants_app, "_enforce_code_ceiling", _nothing)
    monkeypatch.setattr("server.code.app.index.maintainers.sync_quietly", _nothing)

    async def _promote() -> None:
        await approving.wait()
        await grants_app.handle_grants_create(
            GrantsCreateInput(org="acme", namespace="team", subject="alice", role="admin")
        )

    async def _both() -> None:
        approve = GrantsApproveInput(org="acme", namespace="team", subject="alice")
        await asyncio.gather(grants_app.handle_grants_approve(approve), _promote())

    asyncio.run(_both())
    assert [r["role"] for r in rows] == ["writer", "admin"]


def test_without_a_keeper_a_change_waits_no_longer_than_with_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(grant_claims, "WAIT_S", 0.05)
    log: list[str] = []

    async def _both() -> None:
        key = ("acme", "team", "alice")
        holder = asyncio.ensure_future(_change(log, "a", key, 0.3))
        await asyncio.sleep(0)
        with pytest.raises(GrantBusyError):
            await _change(log, "b", key, 0)
        await holder

    asyncio.run(_both())
    assert log == ["a in", "a out"]
    assert len(grant_claims._local) == 0, "the waiter that gave up left no lock behind"


def test_a_cancelled_wait_for_a_local_lock_leaves_nothing_behind() -> None:
    from server.core.keyed_locks import KeyedLocks

    locks = KeyedLocks()

    async def _run() -> None:
        async with locks.hold("k"):
            waiter = asyncio.ensure_future(locks.hold("k", wait_s=5).__aenter__())
            await asyncio.sleep(0)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
        async with locks.hold("k", wait_s=0.1):
            pass  # the lock is free again

    asyncio.run(_run())
    assert len(locks) == 0


def test_two_admins_revoked_at_once_leave_one(monkeypatch: pytest.MonkeyPatch) -> None:
    # Two admins, each revoked by a concurrent call: the admins claim makes the
    # second count the first's revoke, so the namespace keeps an admin.
    from server.core.errors import PreconditionFailedError
    from server.identity.api.models_grants import GrantsRevokeInput

    revoked: set[str] = set()

    async def _find(org: str, ns: str, subject: str, **_k: Any) -> GrantState | None:
        if subject in revoked:
            return None
        return GrantState.from_row({"role": "admin", "status": "active", "granted_by": "x"})

    async def _count(*_a: Any, **_k: Any) -> int:
        n = 2 - len(revoked)  # what the read saw; its reply comes later
        await asyncio.sleep(0.01)
        return n

    async def _insert(row: dict[str, Any], **_k: Any) -> None:
        revoked.add(row["subject"])

    async def _same(org: str, subject: str, **_k: Any) -> str:
        return subject

    async def _nothing(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(grants_repo, "find_any_grant", _find)
    monkeypatch.setattr(grants_repo, "count_active_admins", _count)
    monkeypatch.setattr(grants_repo, "insert_row", _insert)
    monkeypatch.setattr("server.auth.aliases.canonical_subject", _same)
    monkeypatch.setattr(
        "server.identity.app.token_grants.cascade_revoke_to_created_tokens", _nothing
    )

    async def _revoke(subject: str) -> str:
        try:
            await grants_app.handle_grants_revoke(
                GrantsRevokeInput(org="acme", namespace="team", subject=subject)
            )
            return "revoked"
        except PreconditionFailedError:
            return "refused"

    async def _both() -> list[str]:
        return list(await asyncio.gather(_revoke("alice"), _revoke("bob")))

    assert sorted(asyncio.run(_both())) == ["refused", "revoked"]
    assert len(revoked) == 1
