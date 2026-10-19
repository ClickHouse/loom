"""A token name reused on ClickHouse: a new token under a name another host
revoked is the row FINAL keeps, and a pass that revokes what a person made
still finds the tokens a reused name's former holder made."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest

from server.auth import service_accounts
from server.auth.delegation import Delegated
from server.core.ch import get_client
from server.identity.app.me_service_accounts import save_token
from tests.integration._resources import unique_org

pytestmark = pytest.mark.usefixtures("ch_target_required")

_AHEAD = 2**62  # written by a host whose clock runs far ahead


def _delegated(org: str, *, parents: list[str]) -> Delegated:
    """What `delegate` hands a mint made down `parents`, the first its root."""
    return Delegated(
        org=org,
        namespaces=["ns"],
        roles=["writer"],
        ns_roles=None,
        write_namespaces=None,
        read_scope="",
        read_user_id="",
        ttl_seconds=None,
        parents=[],
        parent_subjects=parents,
        parent_iats=[0] * len(parents),
        creator=parents[0],
        is_service_account=True,
    )


@pytest.fixture
async def org() -> AsyncIterator[str]:
    ch = get_client()
    await ch.connect()
    made = unique_org("sa-names")
    try:
        yield made
    finally:
        await ch.execute(
            "ALTER TABLE loom.service_accounts DELETE WHERE org = {org:String} "
            "SETTINGS mutations_sync = 2",
            params={"org": made},
        )
        await ch.disconnect()


async def _head(org: str, name: str) -> dict:
    row = await get_client().query_one(
        "SELECT sa_subject, revoked_at FROM loom.service_accounts FINAL "
        "WHERE org = {org:String} AND name = {name:String}",
        params={"org": org, "name": name},
    )
    assert row is not None
    return dict(row)


async def _save(org: str, name: str, subject: str, created_by: str, parents: list[str]) -> None:
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    await save_token(
        name, subject, _delegated(org, parents=parents), created_by=created_by, now=now
    )


async def test_a_new_token_under_a_name_revoked_by_a_host_ahead_is_the_head(org: str) -> None:
    await _save(org, "bot", "sa:bot-old", "lee", ["lee"])
    old = await get_client().query_one(
        "SELECT * FROM loom.service_accounts FINAL WHERE org = {org:String} AND name = 'bot'",
        params={"org": org},
    )
    revoked = service_accounts.revoked_row(
        {**dict(old or {}), "version": _AHEAD}, revoked_at=datetime(2026, 1, 1), revoked_by="lee"
    )
    await get_client().insert("loom.service_accounts", [revoked])
    await _save(org, "bot", "sa:bot-new", "lee", ["lee"])
    assert await _head(org, "bot") == {"sa_subject": "sa:bot-new", "revoked_at": None}


async def test_the_tokens_a_reused_names_former_holder_made_are_revoked(org: str) -> None:
    lee = f"lee-{uuid.uuid4().hex[:8]}"
    await _save(org, "parent", "sa:parent-old", lee, [lee])
    await _save(org, "child", "sa:child-1", "sa:parent-old", [lee, "sa:parent-old"])
    old = await get_client().query_one(
        "SELECT * FROM loom.service_accounts FINAL WHERE org = {org:String} AND name = 'parent'",
        params={"org": org},
    )
    gone = service_accounts.revoked_row(
        dict(old or {}), revoked_at=datetime(2026, 1, 1), revoked_by="lee"
    )
    await get_client().insert("loom.service_accounts", [gone])
    await _save(org, "parent", "sa:parent-new", "admin", ["admin"])  # the name, reused

    from server.auth import revocation

    await revocation.scim_revoke(lee, reason="test", revoked_by="scim")
    try:
        await service_accounts.revoke_created_by([lee], revoked_by="scim")
        assert (await _head(org, "child"))["revoked_at"] is not None
        assert (await _head(org, "parent"))["revoked_at"] is None  # the new holder's
    finally:
        await get_client().execute(
            "ALTER TABLE loom.revoked_tokens DELETE WHERE jti = {j:String} "
            "SETTINGS mutations_sync = 2",
            params={"j": revocation.SCIM_PREFIX + lee},
        )


async def _listed_before_lineage(org: str, name: str, subject: str, created_by: str) -> None:
    """A token row as a build before migration 044 wrote it: no lineage."""
    await _save(org, name, subject, created_by, [created_by])
    row = await get_client().query_one(
        "SELECT * FROM loom.service_accounts FINAL WHERE org = {org:String} AND name = {n:String}",
        params={"org": org, "n": name},
    )
    old = {**dict(row or {}), "lineage": [], "version": int((row or {})["version"]) + 1}
    await get_client().insert("loom.service_accounts", [old])


async def test_a_reused_name_hands_its_ancestry_to_tokens_listed_before_lineage(org: str) -> None:
    lee = f"lee-{uuid.uuid4().hex[:8]}"
    await _listed_before_lineage(org, "parent", "sa:parent-old", lee)
    await _listed_before_lineage(org, "child", "sa:child-1", "sa:parent-old")
    old = await get_client().query_one(
        "SELECT * FROM loom.service_accounts FINAL WHERE org = {org:String} AND name = 'parent'",
        params={"org": org},
    )
    gone = service_accounts.revoked_row(
        dict(old or {}), revoked_at=datetime(2026, 1, 1), revoked_by=lee
    )
    await get_client().insert("loom.service_accounts", [gone])
    await _save(org, "parent", "sa:parent-new", "admin", ["admin"])  # the name, reused

    from server.auth import revocation

    await revocation.scim_revoke(lee, reason="test", revoked_by="scim")
    try:
        await service_accounts.revoke_created_by([lee], revoked_by="scim")
        assert (await _head(org, "child"))["revoked_at"] is not None
    finally:
        await get_client().execute(
            "ALTER TABLE loom.revoked_tokens DELETE WHERE jti = {j:String} "
            "SETTINGS mutations_sync = 2",
            params={"j": revocation.SCIM_PREFIX + lee},
        )


async def _reuse(org: str, name: str, by: str) -> None:
    """Revoke the token holding `name`, then list another under it."""
    held = await get_client().query_one(
        "SELECT * FROM loom.service_accounts FINAL WHERE org = {org:String} AND name = {n:String}",
        params={"org": org, "n": name},
    )
    gone = service_accounts.revoked_row(
        dict(held or {}), revoked_at=datetime(2026, 1, 1), revoked_by=by
    )
    await get_client().insert("loom.service_accounts", [gone])
    await _save(org, name, f"sa:{name}-new", "admin", ["admin"])


async def test_two_reused_names_in_a_chain_listed_before_lineage_keep_its_root(org: str) -> None:
    # lee -> top -> mid -> leaf, all listed before 044; mid's name, then top's, reused.
    lee = f"lee-{uuid.uuid4().hex[:8]}"
    await _listed_before_lineage(org, "top", "sa:top-old", lee)
    await _listed_before_lineage(org, "mid", "sa:mid-old", "sa:top-old")
    await _listed_before_lineage(org, "leaf", "sa:leaf-1", "sa:mid-old")
    await _reuse(org, "mid", lee)
    await _reuse(org, "top", lee)

    from server.auth import revocation

    await revocation.scim_revoke(lee, reason="test", revoked_by="scim")
    try:
        await service_accounts.revoke_created_by([lee], revoked_by="scim")
        assert (await _head(org, "leaf"))["revoked_at"] is not None
    finally:
        await get_client().execute(
            "ALTER TABLE loom.revoked_tokens DELETE WHERE jti = {j:String} "
            "SETTINGS mutations_sync = 2",
            params={"j": revocation.SCIM_PREFIX + lee},
        )
