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
    def __init__(
        self, org: str, namespace: str, source: str = "config", classification: str = "standard"
    ) -> None:
        self.org, self.namespace, self.repo, self.source = org, namespace, "It/ord", source
        self.classification = classification


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
    client: Any,
    org: str,
    namespace: str,
    role: str,
    at: datetime,
    status: str = "active",
    subject: str = "alice",
) -> None:
    row = {
        "org": org,
        "namespace": namespace,
        "subject": subject,
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


async def _state(client: Any, org: str, namespace: str, subject: str = "alice") -> str:
    row = await client.query_one(
        "SELECT concat(role, '/', toString(status), if(revoked_at IS NULL, '', '/revoked'))"
        " AS state FROM loom.namespace_grants FINAL"
        " WHERE org = {org:String} AND namespace = {ns:String} AND subject = {sub:String}",
        params={"org": org, "ns": namespace, "sub": subject},
    )
    return str(row["state"]) if row else "none"


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


async def test_a_page_added_branch_admin_never_reaches_the_operator_entry(
    ch: tuple[Any, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, org = ch
    entries = [_Entry(org, "code-ord"), _Entry(org, "code-ord-dev", source="registry")]
    monkeypatch.setattr(repos, "configured_repos", lambda: entries)
    second = datetime.now(UTC).replace(microsecond=0, tzinfo=None)
    await _grant(client, org, "code-ord", "admin", second, subject="ops")
    await _grant(client, org, "code-ord-dev", "admin", second, subject="mallory")
    await maintainers.sync_repo_maintainers(org, "code-ord-dev", ch=client)
    assert await _state(client, org, "code-ord", "mallory") == "none"
    assert await _state(client, org, "code-ord-dev", "ops") == "admin/active"

    async def _read(_ch: Any = None) -> bool:
        return True

    monkeypatch.setattr(repo_registry, "read_registry", _read)
    await maintainers.revoke_repo_maintainer(org, "code-ord", "ops", repo="It/ord", ch=client)
    assert await _state(client, org, "code-ord-dev", "ops") == "admin/active/revoked"


async def test_a_restricted_page_branch_drops_a_page_copy_and_takes_an_operator_grant_after(
    ch: tuple[Any, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """On ClickHouse: the sync's revoke replaces the page copy under FINAL, and an
    operator grant made afterwards is copied onto the branch, not held back by it."""
    client, org = ch
    entries = [
        _Entry(org, "code-ord"),
        _Entry(org, "code-ord-dev", source="registry"),
        _Entry(org, "code-ord-rc", source="registry", classification="restricted"),
    ]
    monkeypatch.setattr(repos, "configured_repos", lambda: entries)
    second = datetime.now(UTC).replace(microsecond=0, tzinfo=None)
    await _grant(client, org, "code-ord-dev", "admin", second, subject="mallory")
    copy = {
        "org": org,
        "namespace": "code-ord-rc",
        "subject": "mallory",
        "role": "admin",
        "status": "active",
        "granted_at": second,
        "granted_by": "repo-maintainers:code-ord-dev",
        "revoked_at": None,
        "version": time.time_ns(),
    }
    await client.insert(
        "loom.namespace_grants", [copy], settings={"async_insert": 0, "wait_for_async_insert": 1}
    )
    await maintainers.sync_repo_maintainers(org, "code-ord", ch=client)
    assert await _state(client, org, "code-ord-rc", "mallory") == "admin/active/revoked"
    await _grant(client, org, "code-ord", "admin", second, subject="mallory")
    await maintainers.sync_repo_maintainers(org, "code-ord", ch=client)
    assert await _state(client, org, "code-ord-rc", "mallory") == "admin/active"


async def test_a_page_branch_restricted_by_its_directory_row_alone_drops_a_page_copy(
    ch: tuple[Any, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """On ClickHouse: the snapshot says standard, the directory row says restricted;
    the sync reads the row and revokes the copy a sibling page branch gave it."""
    client, org = ch
    entries = [
        _Entry(org, "code-ord"),
        _Entry(org, "code-ord-dev", source="registry"),
        _Entry(org, "code-ord-rc", source="registry"),
    ]
    monkeypatch.setattr(repos, "configured_repos", lambda: entries)
    await client.insert(
        "loom.namespaces",
        [
            {
                "org": org,
                "namespace": "code-ord-rc",
                "owner_email": "",
                "classification": "restricted",
            }
        ],
        settings={"async_insert": 0, "wait_for_async_insert": 1},
    )
    try:
        second = datetime.now(UTC).replace(microsecond=0, tzinfo=None)
        await _grant(client, org, "code-ord-dev", "admin", second, subject="mallory")
        await maintainers.sync_repo_maintainers(org, "code-ord", ch=client)
        assert await _state(client, org, "code-ord-rc", "mallory") == "none"
        # A copy an earlier build left there, or one made while the branch was standard.
        copy = {
            "org": org,
            "namespace": "code-ord-rc",
            "subject": "mallory",
            "role": "admin",
            "status": "active",
            "granted_at": second,
            "granted_by": "repo-maintainers:code-ord-dev",
            "revoked_at": None,
            "version": time.time_ns(),
        }
        await client.insert(
            "loom.namespace_grants",
            [copy],
            settings={"async_insert": 0, "wait_for_async_insert": 1},
        )
        await maintainers.sync_repo_maintainers(org, "code-ord", ch=client)
        assert await _state(client, org, "code-ord-rc", "mallory") == "admin/active/revoked"
    finally:
        await client.execute(
            "ALTER TABLE loom.namespaces DROP PARTITION {p:String}", params={"p": org}
        )


@pytest.mark.parametrize(
    ("role", "status", "granted_by", "holds"),
    [
        ("admin", "active", "alice", True),
        ("admin", "pending", "alice", False),  # asked, not given
        ("viewer", "active", "alice", False),  # lowered
        ("admin", "active", "repo-maintainers:code-ord-dev", False),  # a page copy
        ("admin", "active", "repo-maintainers:code-ord", True),  # an operator copy
    ],
)
async def test_only_a_live_admin_grant_not_from_a_page_branch_counts_as_the_caller_s_own(
    ch: tuple[Any, str],
    monkeypatch: pytest.MonkeyPatch,
    role: str,
    status: str,
    granted_by: str,
    holds: bool,
) -> None:
    """On ClickHouse: the statement `holds_own_admin` runs refuses a pending or lowered
    grant, and the label check refuses a copy from a page branch."""
    client, org = ch
    entries = [
        _Entry(org, "code-ord"),
        _Entry(org, "code-ord-dev", source="registry"),
        _Entry(org, "code-ord-rc", source="registry", classification="restricted"),
    ]
    monkeypatch.setattr(repos, "configured_repos", lambda: entries)
    row = {
        "org": org,
        "namespace": "code-ord-rc",
        "subject": "mallory",
        "role": role,
        "status": status,
        "granted_at": datetime.now(UTC).replace(microsecond=0, tzinfo=None),
        "granted_by": granted_by,
        "revoked_at": None,
        "version": time.time_ns(),
    }
    await client.insert(
        "loom.namespace_grants", [row], settings={"async_insert": 0, "wait_for_async_insert": 1}
    )
    assert await maintainers.holds_own_admin(client, org, "code-ord-rc", "mallory") is holds


@pytest.mark.parametrize(
    ("namespace", "status", "granted_by", "holds"),
    [
        ("code-ord", "active", "alice", True),
        ("code-ord", "pending", "alice", False),
        ("code-ord", "active", "repo-maintainers:code-ord-26-5", False),  # a copy
        ("code-ord-dev", "active", "alice", False),  # not an operator entry
    ],
)
async def test_a_refused_rewrite_reads_the_subject_s_given_operator_admin_grants(
    ch: tuple[Any, str], namespace: str, status: str, granted_by: str, holds: bool
) -> None:
    """On ClickHouse: the statement behind a refused rewrite counts only live admin
    grants given on the operator entries it is passed."""
    client, org = ch
    row = {
        "org": org,
        "namespace": namespace,
        "subject": "ops",
        "role": "admin",
        "status": status,
        "granted_at": datetime.now(UTC).replace(microsecond=0, tzinfo=None),
        "granted_by": granted_by,
        "revoked_at": None,
        "version": time.time_ns(),
    }
    await client.insert(
        "loom.namespace_grants", [row], settings={"async_insert": 0, "wait_for_async_insert": 1}
    )
    config = ["code-ord", "code-ord-26-5"]
    assert await maintainers._holds_operator_admin(client, org, "ops", config) is holds
