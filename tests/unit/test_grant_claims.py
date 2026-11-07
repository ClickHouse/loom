"""One change at a time per grant: an op that reads a grant and then writes it
holds the grant from the read to the write, so a concurrent change of the same
grant waits instead of being written over."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.auth import grant_claims
from server.core.errors import BackendUnavailableError, GrantBusyError
from server.core.leases import LeaseUnavailable
from server.identity.api.models_grants import GrantsApproveInput, GrantsCreateInput
from server.identity.app import grants as grants_app
from server.identity.domain.grants import GrantState
from server.identity.store import grants as grants_repo
from tests.unit.claims_fake import MemoryClaims

# A revoke reads no directory ties (`conftest.no_directory_ties`) nor the code
# registry (`conftest.off_code_repos`) here.
pytestmark = pytest.mark.usefixtures("no_directory_ties", "off_code_repos")


class _Lease:
    def __init__(self, released: list[str], name: str) -> None:
        self.released = released
        self.name = name

    async def release(self) -> None:
        self.released.append(self.name)


def _held(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every claim is granted at once; the list collects the names released."""
    released: list[str] = []

    async def _take(name: str, **_k: Any) -> _Lease:
        return _Lease(released, name)

    monkeypatch.setattr(grant_claims, "take", _take)
    return released


async def _change(log: list[str], name: str, key: tuple[str, str, str], hold: float) -> None:
    async with grant_claims.grant_change(*key):
        log.append(f"{name} in")
        await asyncio.sleep(hold)
        log.append(f"{name} out")


def test_two_changes_of_one_grant_run_one_after_the_other(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claims = MemoryClaims()
    monkeypatch.setattr(grant_claims, "take", claims.take)
    log: list[str] = []

    async def _both() -> None:
        key = ("acme", "team", "alice")
        await asyncio.gather(_change(log, "a", key, 0.05), _change(log, "b", key, 0))

    asyncio.run(_both())
    assert log == ["a in", "a out", "b in", "b out"]
    assert not claims.held, "the last change out releases the claim"


def test_reads_under_a_claim_see_the_last_holder_s_write() -> None:
    from server.core.ch import sequential_reads_active

    async def _read() -> bool:
        async with grant_claims.grant_change("acme", "team", "alice"):
            return sequential_reads_active()

    assert asyncio.run(_read())


def test_a_change_that_outruns_its_bound_is_cut_off_and_released(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    released = _held(monkeypatch)
    monkeypatch.setattr(grant_claims, "CHANGE_S", 0.05)
    log: list[str] = []
    with pytest.raises(BackendUnavailableError):
        asyncio.run(_change(log, "slow", ("acme", "team", "alice"), 1.0))
    assert log == ["slow in"] and len(released) == 1, "no write after the bound"


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
    waits: list[float] = []

    async def _taken(name: str, *, wait_s: float = 0.0, **_k: Any) -> None:
        waits.append(wait_s)
        return None

    monkeypatch.setattr(grant_claims, "take", _taken)
    with pytest.raises(GrantBusyError) as busy:
        asyncio.run(_change([], "a", ("acme", "team", "alice"), 0))
    assert waits == [grant_claims.WAIT_S], "it waited for the holder before giving up"
    assert busy.value.status_code == 503 and busy.value.extra["retry_after_s"] == 1


def test_a_lease_store_that_cannot_answer_refuses_retryably(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _down(name: str, **_k: Any) -> None:
        raise LeaseUnavailable("down")

    monkeypatch.setattr(grant_claims, "take", _down)
    with pytest.raises(BackendUnavailableError):
        asyncio.run(_change([], "a", ("acme", "team", "alice"), 0))


@pytest.mark.parametrize("fails", [False, True])
def test_a_held_claim_is_released_when_the_change_ends(
    monkeypatch: pytest.MonkeyPatch, fails: bool
) -> None:
    released = _held(monkeypatch)

    async def _run() -> None:
        async with grant_claims.grant_change("acme", "team", "alice"):
            if fails:
                raise RuntimeError("the write failed")

    if fails:
        with pytest.raises(RuntimeError):
            asyncio.run(_run())
    else:
        asyncio.run(_run())
    assert released == ['grant:["acme", "team", "alice"]']


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


def _two_admins(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    """alice and bob, admins of acme/team; a change that takes admin from one
    of them lands in the returned set. The admin count reads, then replies late."""
    gone: set[str] = set()

    async def _find(org: str, ns: str, subject: str, **_k: Any) -> GrantState | None:
        if subject in gone:
            return None
        return GrantState.from_row({"role": "admin", "status": "active", "granted_by": "x"})

    async def _count(*_a: Any, **_k: Any) -> int:
        n = 2 - len(gone)
        await asyncio.sleep(0.01)
        return n

    async def _insert(row: dict[str, Any], **_k: Any) -> None:
        gone.add(row["subject"])

    async def _same(org: str, subject: str, **_k: Any) -> str:
        return subject

    async def _nothing(*_a: Any, **_k: Any) -> None:
        return None

    for name, fake in (
        ("find_any_grant", _find),
        ("find_active_grant", _find),
        ("count_active_admins", _count),
        ("insert_row", _insert),
    ):
        monkeypatch.setattr(grants_repo, name, fake)
    monkeypatch.setattr("server.auth.aliases.canonical_subject", _same)
    monkeypatch.setattr(grants_app, "_enforce_code_ceiling", _nothing)
    return gone


async def _outcome(change: Any) -> str:
    from server.core.errors import PreconditionFailedError

    try:
        await change
        return "done"
    except PreconditionFailedError:
        return "refused"


def test_two_admins_lowered_at_once_leave_one(monkeypatch: pytest.MonkeyPatch) -> None:
    gone = _two_admins(monkeypatch)

    def _lower(subject: str) -> Any:
        return grants_app.handle_grants_create(
            GrantsCreateInput(
                org="acme", namespace="team", subject=subject, role="viewer", replace=True
            )
        )

    async def _both() -> list[str]:
        return list(await asyncio.gather(_outcome(_lower("alice")), _outcome(_lower("bob"))))

    assert sorted(asyncio.run(_both())) == ["done", "refused"] and len(gone) == 1


def test_two_admins_leaving_at_once_leave_one(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.identity.api.models import NamespaceLeaveInput
    from server.identity.app import namespace_membership

    gone = _two_admins(monkeypatch)

    def _leave(subject: str) -> Any:
        return namespace_membership.handle_namespace_leave(
            NamespaceLeaveInput(org="acme", namespace="team", subject=subject)
        )

    async def _both() -> list[str]:
        return list(await asyncio.gather(_outcome(_leave("alice")), _outcome(_leave("bob"))))

    assert sorted(asyncio.run(_both())) == ["done", "refused"] and len(gone) == 1


def test_a_leave_whose_grant_became_admin_meanwhile_counts_the_admins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import asynccontextmanager

    from server.identity.api.models import NamespaceLeaveInput
    from server.identity.app import namespace_membership

    reads = iter(["writer", "admin", "admin"])  # promoted between the read and the claim
    claims: list[str] = []

    async def _find(*_a: Any, **_k: Any) -> GrantState:
        return GrantState.from_row({"role": next(reads), "status": "active", "granted_by": "x"})

    @asynccontextmanager
    async def _admins(org: str, ns: str) -> Any:
        claims.append("admins")
        yield

    async def _left(input: Any, caller: str) -> str:
        return "left"

    monkeypatch.setattr(grants_repo, "find_active_grant", _find)
    monkeypatch.setattr(namespace_membership, "admins_change", _admins)
    monkeypatch.setattr(namespace_membership, "_leave", _left)
    out = asyncio.run(
        namespace_membership.handle_namespace_leave(
            NamespaceLeaveInput(org="acme", namespace="team", subject="alice")
        )
    )
    assert out == "left" and claims == ["admins"]


@pytest.mark.parametrize(("roles", "admins"), [(["viewer"], 0), (["viewer", "admin"], 1)])
def test_a_revoke_counts_the_admins_only_for_an_admin(
    monkeypatch: pytest.MonkeyPatch, roles: list[str], admins: int
) -> None:
    # A reader's revoke takes only its grant's claim; one promoted between the read
    # and the claim moves under the admins claims.
    from contextlib import asynccontextmanager

    from server.identity.api.models_grants import GrantsRevokeInput

    reads = iter(roles + roles[-1:] * 4)
    claims: list[str] = []

    async def _find(*_a: Any, **_k: Any) -> GrantState:
        return GrantState.from_row({"role": next(reads), "status": "active", "granted_by": "x"})

    @asynccontextmanager
    async def _admins(org: str, ns: str) -> Any:
        claims.append("admins")
        yield

    async def _two(*_a: Any, **_k: Any) -> int:
        return 2

    async def _same(org: str, subject: str, **_k: Any) -> str:
        return subject

    async def _nothing(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(grants_repo, "find_any_grant", _find)
    monkeypatch.setattr(grants_repo, "count_active_admins", _two)
    monkeypatch.setattr(grants_repo, "insert_row", _nothing)
    monkeypatch.setattr(grants_app, "admins_change", _admins)
    monkeypatch.setattr("server.auth.aliases.canonical_subject", _same)
    monkeypatch.setattr(
        "server.identity.app.token_grants.cascade_revoke_to_created_tokens", _nothing
    )
    out = asyncio.run(
        grants_app.handle_grants_revoke(
            GrantsRevokeInput(org="acme", namespace="team", subject="alice")
        )
    )
    assert out.revoked and claims.count("admins") == admins
