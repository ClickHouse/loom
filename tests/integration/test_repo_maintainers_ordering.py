"""Maintainer sync on ClickHouse orders grant changes by row version.

`granted_at` keeps whole seconds, so a lowering on one branch and a promotion
on another within the same second compare equal by time; the row version
tells which came last. Written in order, as two admins would.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest

from server.code.app.index import maintainers, repo_registry
from server.code.domain.index import repos
from server.core.repo_base import _default_client

BRANCHES = ("code-ord", "code-ord-26-5")


class _Entry:
    def __init__(self, org: str, namespace: str) -> None:
        self.org, self.namespace, self.repo, self.source = org, namespace, "It/ord", "config"


@pytest.fixture
async def ch(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple[Any, str]]:
    client = _default_client()
    await client.connect()
    monkeypatch.setattr("server.core.ch.get_client", lambda: client)
    org = f"it-ordering-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(repos, "configured_repos", lambda: [_Entry(org, b) for b in BRANCHES])
    monkeypatch.setattr(repo_registry, "registry_read", lambda: True)
    try:
        yield client, org
    finally:
        await client.execute(
            "ALTER TABLE loom.namespace_grants DROP PARTITION {p:String}", params={"p": org}
        )
        await client.disconnect()


async def _grant(
    client: Any, org: str, namespace: str, role: str, at: datetime, status: str = "active"
) -> None:
    row = {
        "org": org,
        "namespace": namespace,
        "subject": "alice",
        "role": role,
        "status": status,
        "granted_at": at,
        "granted_by": "it",
        "revoked_at": None,
        "version": time.time_ns(),
    }
    await client.insert(
        "loom.namespace_grants", [row], settings={"async_insert": 0, "wait_for_async_insert": 1}
    )


async def _state(client: Any, org: str, namespace: str) -> str:
    row = await client.query_one(
        "SELECT concat(role, '/', toString(status)) AS state FROM loom.namespace_grants FINAL"
        " WHERE org = {org:String} AND namespace = {ns:String} AND subject = 'alice'",
        params={"org": org, "ns": namespace},
    )
    return str(row["state"])


async def test_a_promotion_in_the_same_second_as_a_lowering_reaches_that_branch(
    ch: tuple[Any, str],
) -> None:
    client, org = ch
    second = datetime.now(UTC).replace(microsecond=0, tzinfo=None)
    await _grant(client, org, "code-ord-26-5", "viewer", second)  # lowered here first
    await _grant(client, org, "code-ord", "admin", second)  # then promoted there
    await maintainers.sync_repo_maintainers(org, "code-ord", ch=client)
    assert await _state(client, org, "code-ord-26-5") == "admin/active"
    await maintainers.sync_repo_maintainers(org, "code-ord", ch=client)
    assert await _state(client, org, "code-ord-26-5") == "admin/active", "a second sync keeps it"


async def test_a_lowering_in_the_same_second_after_a_promotion_stands(
    ch: tuple[Any, str],
) -> None:
    client, org = ch
    second = datetime.now(UTC).replace(microsecond=0, tzinfo=None)
    await _grant(client, org, "code-ord", "admin", second)  # promoted there first
    await _grant(client, org, "code-ord-26-5", "viewer", second)  # then lowered here
    await maintainers.sync_repo_maintainers(org, "code-ord", ch=client)
    assert await _state(client, org, "code-ord-26-5") == "viewer/active"


async def test_an_older_join_request_on_a_branch_is_answered_by_the_mirror(
    ch: tuple[Any, str],
) -> None:
    client, org = ch
    second = datetime.now(UTC).replace(microsecond=0, tzinfo=None)
    await _grant(client, org, "code-ord-26-5", "viewer", second, status="pending")
    await _grant(client, org, "code-ord", "admin", second)
    assert await maintainers.sync_repo_maintainers(org, "code-ord", ch=client) == 1
    assert await _state(client, org, "code-ord-26-5") == "admin/active"
    assert await maintainers.sync_repo_maintainers(org, "code-ord", ch=client) == 0


async def test_a_request_made_after_the_admin_grant_is_left_for_its_approval(
    ch: tuple[Any, str],
) -> None:
    client, org = ch
    second = datetime.now(UTC).replace(microsecond=0, tzinfo=None)
    await _grant(client, org, "code-ord", "admin", second)
    await _grant(client, org, "code-ord-26-5", "viewer", second, status="pending")
    assert await maintainers.sync_repo_maintainers(org, "code-ord", ch=client) == 0
    assert await _state(client, org, "code-ord-26-5") == "viewer/pending"
