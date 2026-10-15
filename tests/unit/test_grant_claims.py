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
