"""A grant write that lowers a raise it never read still ends the raise's claims.

`grants.create` decides whether it lowers from a read of the current grant; a
concurrent admin grant can land after that read. The grant log on ClickHouse
is what lets the write see it lowered admin and write the cutoff.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Any

import pytest

from server.auth import revocation
from server.core.repo_base import _default_client
from server.identity.api.models_grants import GrantsCreateInput
from server.identity.app import grants as grants_app
from server.identity.store import grants as grants_repo
from tests.integration._resources import unique_org

pytestmark = pytest.mark.grant_log_check


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


async def _cutoff_written(org: str, subject: str) -> bool:
    key = revocation.grant_claim_key(org, "team", subject)
    return bool(await revocation._read_revocations([key]))


async def test_a_lowering_that_read_before_a_concurrent_raise_cuts_it(
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

    await grants_app.handle_grants_create(
        GrantsCreateInput(org=org, namespace="team", subject="alice", role="writer")
    )

    assert (await grants_repo.find_active_grant(org, "team", "alice")).role == "writer"
    assert await _cutoff_written(org, "alice")


async def test_a_raise_writes_no_cutoff(ch: Any) -> None:
    org = unique_org("lowering-race")
    await grants_repo.insert_row(_grant(org, "bob", "viewer"))

    await grants_app.handle_grants_create(
        GrantsCreateInput(org=org, namespace="team", subject="bob", role="writer")
    )

    assert not await _cutoff_written(org, "bob")


async def test_namespace_grant_log_mv_keeps_every_row_the_grants_table_replaces(ch: Any) -> None:
    org = unique_org("lowering-race")
    for role in ("viewer", "admin", "writer"):
        await grants_repo.insert_row(_grant(org, "carol", role))
    rows = await ch.query_all(
        "SELECT role FROM loom.namespace_grant_log WHERE org = {org:String} ORDER BY version",
        params={"org": org},
    )
    assert [r["role"] for r in rows] == ["viewer", "admin", "writer"]
