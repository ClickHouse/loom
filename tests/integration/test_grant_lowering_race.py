"""Grant changes end the claims they take away, against a real grant log.

`grants.create` decides whether it lowers from a read of the current grant; a
concurrent admin grant can land after that read, and only the grant log on
ClickHouse shows it. A lowering is cut before its row and again after it.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Any

import pytest

from server.auth import revocation
from server.core.repo_base import _default_client
from server.identity.api.models_grants import GrantsCreateInput
from server.identity.app import grant_writes
from server.identity.app import grants as grants_app
from server.identity.store import grants as grants_repo
from tests.integration._resources import unique_org


@pytest.fixture
async def ch() -> AsyncIterator[Any]:
    client = _default_client()
    await client.connect()
    try:
        yield client
    finally:
        await client.disconnect()


def _grant(org: str, subject: str, role: str) -> dict[str, Any]:
    return {
        "org": org,
        "namespace": "team",
        "subject": subject,
        "role": role,
        "status": "active",
        "granted_by": "test",
        "revoked_at": None,
        "version": time.time_ns(),
    }


async def _cutoff_reason(org: str, subject: str) -> str | None:
    key = revocation.grant_claim_key(org, "team", subject)
    rows = await revocation._read_revocations([key])
    return str(rows[0]["reason"]) if rows else None


def _create(org: str, subject: str, role: str, *, replace: bool = False) -> GrantsCreateInput:
    return GrantsCreateInput(
        org=org,
        namespace="team",
        subject=subject,
        role=role,
        replace=replace,  # type: ignore[arg-type]
    )


async def test_a_write_that_read_before_a_concurrent_raise_cuts_it(
    ch: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    org = unique_org("lowering-race")
    await grants_repo.insert_row(_grant(org, "alice", "viewer"))
    read = grants_repo.find_active_grant

    async def _read_then_raise(*args: Any, **kwargs: Any) -> Any:
        before = await read(*args, **kwargs)
        await grants_repo.insert_row(_grant(org, "alice", "admin"))
        return before

    monkeypatch.setattr(grants_repo, "find_active_grant", _read_then_raise)
    await grants_app.handle_grants_create(_create(org, "alice", "writer"))

    assert (await grants_repo.find_active_grant(org, "team", "alice")).role == "writer"
    assert await _cutoff_reason(org, "alice") == revocation.RESTAMPED


async def test_a_raise_writes_no_cutoff(ch: Any) -> None:
    org = unique_org("lowering-race")
    await grants_repo.insert_row(_grant(org, "bob", "viewer"))
    await grants_app.handle_grants_create(_create(org, "bob", "writer"))
    assert await _cutoff_reason(org, "bob") is None


async def test_a_raise_of_a_pair_cut_before_writes_no_cutoff(ch: Any) -> None:
    # A token minted after the old cutoff, by `loom mint-token` say, keeps its claim.
    org = unique_org("lowering-race")
    await grants_repo.insert_row(_grant(org, "fay", "admin"))
    await grants_app.handle_grants_create(_create(org, "fay", "viewer", replace=True))
    await revocation.write_grant_cutoffs(org, ["team"], ["fay"], reason="test", revoked_by="t")
    await grants_app.handle_grants_create(_create(org, "fay", "writer"))
    assert await _cutoff_reason(org, "fay") == "test"


async def test_a_lowering_is_cut_again_once_its_row_landed(ch: Any) -> None:
    org = unique_org("lowering-race")
    await grants_repo.insert_row(_grant(org, "dave", "admin"))
    await grants_app.handle_grants_create(_create(org, "dave", "viewer", replace=True))
    assert await _cutoff_reason(org, "dave") == revocation.RESTAMPED


async def test_a_retried_lowering_gets_the_second_cutoff_its_first_attempt_lost(
    ch: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    org = unique_org("lowering-race")
    await grants_repo.insert_row(_grant(org, "erin", "admin"))

    async def _down(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("replica away")

    with monkeypatch.context() as m:
        m.setattr(grant_writes, "restamp", _down)
        with pytest.raises(RuntimeError):
            await grants_app.handle_grants_create(_create(org, "erin", "viewer", replace=True))
    assert await _cutoff_reason(org, "erin") == "grants.create lowered"

    await grants_app.handle_grants_create(_create(org, "erin", "viewer", replace=True))
    assert await _cutoff_reason(org, "erin") == revocation.RESTAMPED


async def test_a_raise_versioned_before_an_old_cutoff_is_still_seen(ch: Any) -> None:
    # Pair already cut; a raise stamped before that cutoff's second lands, then
    # a write that read the grant before it lowers it.
    org = unique_org("lowering-race")
    await grants_repo.insert_row(_grant(org, "gus", "viewer"))
    raise_row = _grant(org, "gus", "admin")
    await revocation.write_grant_cutoffs(org, ["team"], ["gus"], reason="test", revoked_by="t")
    await grants_repo.insert_row(raise_row)
    await grant_writes.write_row(_grant(org, "gus", "writer"))
    assert await _cutoff_reason(org, "gus") == revocation.RESTAMPED


async def test_namespace_grant_log_mv_keeps_every_row_the_grants_table_replaces(ch: Any) -> None:
    org = unique_org("lowering-race")
    for role in ("viewer", "admin", "writer"):
        await grants_repo.insert_row(_grant(org, "carol", role))
    rows = await ch.query_all(
        "SELECT role FROM loom.namespace_grant_log WHERE org = {org:String} ORDER BY version",
        params={"org": org},
    )
    assert [r["role"] for r in rows] == ["viewer", "admin", "writer"]
