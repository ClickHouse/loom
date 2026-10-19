"""A token name reused on ClickHouse: a new token under a name another host
revoked is the row FINAL keeps, and a pass that revokes what a person made
still finds the tokens a reused name's former holder made."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

from server.auth import revocation, service_accounts
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
    await _reuse(org, "parent", lee)  # the name, reused

    async with _deactivated(lee):
        assert (await _head(org, "child"))["revoked_at"] is not None
        assert (await _head(org, "parent"))["revoked_at"] is None  # the new holder's


@asynccontextmanager
async def _deactivated(subject: str) -> AsyncIterator[None]:
    """SCIM's revocation of `subject` and its token pass, removed on the way out."""
    await revocation.scim_revoke(subject, reason="test", revoked_by="scim")
    try:
        await service_accounts.revoke_created_by([subject], revoked_by="scim")
        yield
    finally:
        await get_client().execute(
            "ALTER TABLE loom.revoked_tokens DELETE WHERE jti = {j:String} "
            "SETTINGS mutations_sync = 2",
            params={"j": revocation.SCIM_PREFIX + subject},
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
    await _reuse(org, "parent", lee)  # the name, reused

    async with _deactivated(lee):
        assert (await _head(org, "child"))["revoked_at"] is not None


async def _reuse(org: str, name: str, by: str) -> None:
    """Revoke the token holding `name`, then list another under it."""
    await _revoke_by_name(org, name, by=by)
    await _save(org, name, f"sa:{name}-new", "admin", ["admin"])


async def test_two_reused_names_in_a_chain_listed_before_lineage_keep_its_root(org: str) -> None:
    # lee -> top -> mid -> leaf, all listed before 044; mid's name, then top's, reused.
    lee = f"lee-{uuid.uuid4().hex[:8]}"
    await _listed_before_lineage(org, "top", "sa:top-old", lee)
    await _listed_before_lineage(org, "mid", "sa:mid-old", "sa:top-old")
    await _listed_before_lineage(org, "leaf", "sa:leaf-1", "sa:mid-old")
    await _reuse(org, "mid", lee)
    await _reuse(org, "top", lee)

    async with _deactivated(lee):
        assert (await _head(org, "leaf"))["revoked_at"] is not None


def _lost_ancestry_query() -> str:
    """The SSO guide's query for tokens whose chain no longer leads to a person."""
    guide = (Path(__file__).parents[2] / "docs/operations/sso.md").read_text()
    query = guide.split("```sql\n")
    return next(q.split("```")[0] for q in query[1:] if "arrayConcat([created_by], lineage)" in q)


async def _lost(org: str) -> list[str]:
    rows = await get_client().query_all(
        f"SELECT name FROM ({_lost_ancestry_query()}) WHERE org = {{org:String}} ORDER BY name",
        params={"org": org},
    )
    return [str(r["name"]) for r in rows]


async def test_the_guide_lists_a_token_whose_root_was_lost_before_this_build(org: str) -> None:
    # root -> top -> mid -> leaf, all listed before 044. top's name is reused
    # before the upgrade (no hand-down), mid's after it: leaf's lineage holds
    # mid and top, never root.
    root = f"lee-{uuid.uuid4().hex[:8]}"
    await _listed_before_lineage(org, "top", "sa:top-old", root)
    await _listed_before_lineage(org, "mid", "sa:mid-old", "sa:top-old")
    await _listed_before_lineage(org, "leaf", "sa:leaf-1", "sa:mid-old")
    await _reused_by_the_previous_build(org, "top")
    await _reuse(org, "mid", root)
    assert await _lost(org) == ["leaf"]

    async with _deactivated(root):
        assert (await _head(org, "leaf"))["revoked_at"] is None  # what the guide's query is for


async def test_the_guide_lists_no_token_whose_chain_leads_to_a_person(org: str) -> None:
    lee = f"lee-{uuid.uuid4().hex[:8]}"
    await _listed_before_lineage(org, "top", "sa:top-old", lee)
    await _listed_before_lineage(org, "leaf", "sa:leaf-1", "sa:top-old")
    await _save(org, "agent-made", "sa:agent-made-1", "sa:fleet:a1", ["sa:fleet:a1"])
    await _reuse(org, "top", lee)  # handed down: leaf's lineage reaches lee
    # Listed before 044, and its parent's row still live: it leads to lee.
    await _listed_before_lineage(org, "p2", "sa:p2-old", lee)
    await _listed_before_lineage(org, "c2", "sa:c2-1", "sa:p2-old")
    assert await _lost(org) == []


async def _reused_by_the_previous_build(org: str, name: str) -> None:
    """`name` taken by another person's token, with no hand-down."""
    held = await get_client().query_one(
        "SELECT * FROM loom.service_accounts FINAL WHERE org = {org:String} AND name = {n:String}",
        params={"org": org, "n": name},
    )
    row = dict(held or {})
    new = {**row, "sa_subject": f"sa:{name}-new", "created_by": "kim", "lineage": []}
    await get_client().insert(
        "loom.service_accounts", [{**new, "version": int(row["version"]) + 1}]
    )


async def _revoke_by_name(org: str, name: str, *, by: str = "op") -> None:
    held = await get_client().query_one(
        "SELECT * FROM loom.service_accounts FINAL WHERE org = {org:String} AND name = {n:String}",
        params={"org": org, "n": name},
    )
    gone = service_accounts.revoked_row(
        dict(held or {}), revoked_at=datetime(2026, 1, 1), revoked_by=by
    )
    await get_client().insert("loom.service_accounts", [gone])


async def test_running_the_guides_query_again_reaches_down_the_chain(org: str) -> None:
    # root -> top -> mid -> x -> y, all listed before 044: top's name reused
    # before the upgrade, x's after it. y's lineage reaches mid, which is live.
    root = f"lee-{uuid.uuid4().hex[:8]}"
    await _listed_before_lineage(org, "top", "sa:top-old", root)
    await _listed_before_lineage(org, "mid", "sa:mid-1", "sa:top-old")
    await _listed_before_lineage(org, "x", "sa:x-old", "sa:mid-1")
    await _listed_before_lineage(org, "y", "sa:y-1", "sa:x-old")
    await _reused_by_the_previous_build(org, "top")
    await _reuse(org, "x", root)
    assert await _lost(org) == ["mid"]
    await _revoke_by_name(org, "mid")  # as the guide says, then again
    assert await _lost(org) == ["y"]
    await _revoke_by_name(org, "y")
    assert await _lost(org) == []


async def test_the_guide_lists_no_expired_token(org: str) -> None:
    await _listed_before_lineage(org, "old", "sa:old-1", "sa:gone-1")
    held = await get_client().query_one(
        "SELECT * FROM loom.service_accounts FINAL WHERE org = {org:String} AND name = 'old'",
        params={"org": org},
    )
    row = dict(held or {})
    expired = {**row, "expires_at": datetime(2026, 1, 1), "version": int(row["version"]) + 1}
    await get_client().insert("loom.service_accounts", [expired])
    assert await _lost(org) == []


async def test_the_guide_lists_a_token_made_from_an_operator_minted_subject(org: str) -> None:
    # The case the guide tells an operator to keep: `sa:ci` has no row of its own.
    await _save(org, "from-ci", "sa:from-ci-1", "sa:ci", ["sa:ci"])
    assert await _lost(org) == ["from-ci"]
