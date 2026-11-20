"""Actual erasure rewrites release a native child's cached account-size refusal."""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest

from server.auth import delegation, revocation, scim, scim_people, scim_routes
from server.auth.grant_claims import service_account_change
from server.auth.jwt import AuthInfo
from server.core import ch
from server.core.principals import USER, personal_name, principal_id
from server.erasure.app import saga_steps
from server.identity.api.models import NamespaceRegisterInput
from server.identity.app import me_service_accounts
from server.identity.app.namespace import registry_row
from server.identity.store import collections as collections_repo
from server.operations.base import Role
from server.settings import settings
from tests.integration._resources import unique_org
from tests.integration.test_scim_person_live_ch import People
from tests.integration.test_scim_person_live_ch import people as people

pytestmark = pytest.mark.usefixtures("ch_target_required")

# Integration conftests do not replace this policy. Check that before delegation.
_PARENT_POLICY = delegation._refuse_a_revoked_parent


def directory_row(org: str, namespace: str, owner: str, *, ordinary: bool) -> dict[str, Any]:
    row = registry_row(
        NamespaceRegisterInput(
            org=org,
            namespace=namespace,
            owner_email=owner,
            kind="memory" if ordinary else None,
            description="directory description",
        ),
        None,
        datetime.now(UTC).replace(microsecond=0),
    )
    assert row["contact_email"] == row["password_hash"] == ""
    return row


async def stored_row(client: ch.CHClient, table: str, org: str, namespace: str) -> dict[str, Any]:
    assert table in ("loom.namespaces", "loom.collections")
    with ch.own_write_reads():
        row = await client.query_one(
            f"SELECT * FROM {table} FINAL WHERE org = {{org:String}} "
            "AND namespace = {ns:String} LIMIT 1",
            params={"org": org, "ns": namespace},
        )
    assert row is not None
    return row


async def cold_decision(auth: AuthInfo, expected_accounts: int, refused: bool) -> None:
    extra = scim_people._roots(auth.subject, "", (*auth.parent_subjects, auth.creator), auth.org)
    seconds = scim_people._decision_seconds()
    decision = scim_people._Decision(auth.org, auth.subject, "", scim_people._now() + seconds)
    with ch.own_write_reads(), ch.query_tag("test.scim_erasure.cold"):
        try:
            result = await decision.refuses((auth.subject, *extra))
        except scim_people._Refused:
            result = True
    assert result is refused
    assert decision.accounts == expected_accounts
    assert decision.transferred < scim_people.MAX_BYTES


@pytest.mark.parametrize("kind", ["personal", "collection"])
async def test_real_erasure_rewrite_rechecks_a_native_child_after_account_count_shrinks(
    people: People, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    client, org = people.client, people.org
    user_id = f"erasure-{people.run}@example.test"
    owner = USER + user_id
    assert principal_id(owner, USER) == user_id
    target = personal_name(org, owner) if kind == "personal" else "col-erasure-target"
    other_org = unique_org("scim-erasure-other")
    monkeypatch.setattr(settings, "grant_cache_ttl_seconds", 240)

    # Ordinary operator-registerable memory rows do not add collection claims.
    ordinary = [
        directory_row(org, f"memory-erasure-{i}", owner, ordinary=True) for i in range(1024)
    ]
    assert len({r["namespace"] for r in ordinary}) == 1024
    assert all(r["kind"] == "memory" and r["active"] for r in ordinary)
    target_row = directory_row(org, target, owner, ordinary=False)
    assert target_row["kind"] == kind
    if kind == "collection":
        # collection.create fills the title after the same registry builder.
        target_row["title"] = "Erasure collection"
    # Same namespace and owner ensure the collection rewrite needs its org filter.
    # The foreign personal-name row is an operator-registerable memory row.
    foreign_row = directory_row(other_org, target, owner, ordinary=kind == "personal")
    await client.insert(
        "loom.namespaces",
        [*ordinary, target_row, foreign_row],
        settings={"async_insert": 0, "wait_for_async_insert": 1},
    )
    if kind == "collection":
        await collections_repo.write(collections_repo.collection_row(org, target, owner=owner))
        await collections_repo.write(
            collections_repo.collection_row(other_org, target, owner=owner)
        )
    before = await stored_row(client, "loom.namespaces", org, target)
    foreign_before = await stored_row(client, "loom.namespaces", other_org, target)
    tree_before = (
        await stored_row(client, "loom.collections", org, target) if kind == "collection" else None
    )
    foreign_tree_before = (
        await stored_row(client, "loom.collections", other_org, target)
        if kind == "collection"
        else None
    )
    _, parsed, _ = scim_routes.user_name_and_email(
        {"userName": owner, "emails": [{"value": owner, "type": "work", "primary": True}]}
    )
    assert parsed == owner
    # The SCIM table has no org; use the existing fixture's exact-id cleanup.
    people.emails.append(owner)
    await people.provision(owner)

    revocation_reads: list[tuple[dict[str, Any], Any]] = []
    account_counts: list[int] = []
    original_all, original_one = client.query_all, client.query_one

    async def read_all(sql: str, params: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        if params is not None:
            assert scim_people._encoded(params) < scim_people.MAX_BYTES
        answer = await original_all(sql, params=params, **kwargs)
        if "FROM loom.revoked_tokens FINAL" in sql and "jti IN {keys:Array(String)}" in sql:
            revocation_reads.append((dict(params or {}), answer))
        return answer

    async def read_one(sql: str, params: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        if params is not None:
            assert scim_people._encoded(params) < scim_people.MAX_BYTES
        answer = await original_one(sql, params=params, **kwargs)
        if "AS account_count" in sql or "AS bounded_creator" in sql:
            applied = ch.statement_settings(sql)
            assert 0 < applied["max_execution_time"] <= scim_people.SCIM_DECISION_S
        if "AS account_count" in sql:
            assert answer is not None and int(answer["account_bytes"]) == 0
            count = int(answer["account_count"])
            if count > 1024:
                assert answer["accounts"] == []
            account_counts.append(count)
        return answer

    monkeypatch.setattr(client, "query_all", read_all)
    monkeypatch.setattr(client, "query_one", read_one)
    assert delegation._refuse_a_revoked_parent is _PARENT_POLICY
    assert _PARENT_POLICY.__module__ == "server.auth.delegation"
    assert _PARENT_POLICY.__name__ == "_refuse_a_revoked_parent"
    parent = AuthInfo(
        subject=owner,
        org=org,
        namespaces=(),
        roles=(Role.VIEWER,),
        is_service_account=False,
        impersonated_by="https://issuer.example.test",
        read_scope="own_user",
        read_user_id=user_id,
        token_id="parent-" + people.run,
        issued_at=int(time.time()),
    )
    child = await delegation.delegate(
        parent,
        org=org,
        requested={"*": None},
        scoped=False,
        ttl_seconds=300,
        is_service_account=True,
    )
    expected_keys = sorted(
        key
        for subject in (owner, parent.token_id)
        for key in (subject, revocation.CUTOFF_PREFIX + subject, revocation.SCIM_PREFIX + subject)
    )
    assert revocation_reads == [({"keys": expected_keys}, [])]
    name = "erasure-child-" + people.run
    subject = me_service_accounts.new_token_subject(org, name, owner)
    async with service_account_change(org, name):
        await me_service_accounts.refuse_taken_name(org, name, owner)
        await me_service_accounts.save_token(
            name, subject, child, created_by=owner, now=datetime.now(UTC)
        )
    auth = child.as_auth(subject)
    assert auth.creator == owner and owner in auth.parent_subjects
    assert auth.impersonated_by == "" and auth.is_service_account
    persisted = await client.query_one(
        "SELECT created_by, lineage, revoked_at FROM loom.service_accounts FINAL "
        "WHERE org = {org:String} AND name = {name:String} LIMIT 1",
        params={"org": org, "name": name},
    )
    assert persisted is not None and persisted["created_by"] == owner
    assert owner in persisted["lineage"] and persisted["revoked_at"] is None

    await cold_decision(auth, 1025, True)
    assert account_counts[-1] == 1025
    warmed_at = time.monotonic()
    assert await scim.refuses_auth(auth) is True
    reads = len(people.results)
    assert await scim.refuses_auth(auth) is True
    assert len(people.results) == reads, "the pre-write denial must actually be warm"

    # Observe real claims and real SQL; each wrapper forwards to its original.
    account_claims: list[tuple[str, str]] = []
    tree_claims: list[str] = []
    held_accounts: set[tuple[str, str]] = set()
    held_trees: set[str] = set()
    writes: list[str] = []
    original_account, original_tree = saga_steps.account_change, saga_steps.collections_change
    original_execute = client.execute

    @asynccontextmanager
    async def account_claim(claim_org: str, namespace: str):
        async with original_account(claim_org, namespace):
            account_claims.append((claim_org, namespace))
            held_accounts.add((claim_org, namespace))
            try:
                yield
            finally:
                held_accounts.remove((claim_org, namespace))

    @asynccontextmanager
    async def tree_claim(claim_org: str):
        async with original_tree(claim_org):
            tree_claims.append(claim_org)
            held_trees.add(claim_org)
            try:
                yield
            finally:
                held_trees.remove(claim_org)

    async def execute(sql: str, params: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        if sql.startswith("INSERT INTO loom.") and "SELECT * REPLACE" in sql:
            assert held_accounts == {(org, target)}
            assert held_trees == ({org} if kind == "collection" else set())
            assert "FROM (SELECT * FROM " in sql and " FINAL WHERE " in sql
            assert params is not None and params["org"] == org
            writes.append(sql)
        return await original_execute(sql, params=params, **kwargs)

    monkeypatch.setattr(saga_steps, "account_change", account_claim)
    monkeypatch.setattr(saga_steps, "collections_change", tree_claim)
    monkeypatch.setattr(client, "execute", execute)
    if kind == "personal":
        assert await saga_steps._step_retire_personal_collection_by_user(org, user_id) == 1
    else:
        assert await saga_steps._step_forget_collection_owner_by_user(org, user_id) == 2
    assert account_claims == [(org, target)]
    assert tree_claims == ([org] if kind == "collection" else [])
    assert len(writes) == (1 if kind == "personal" else 2)
    assert not held_accounts and not held_trees

    after = await stored_row(client, "loom.namespaces", org, target)
    expected = dict(before, owner_email="", updated_at=after["updated_at"])
    if kind == "personal":
        expected.update(active=False, description="", title="")
    assert after["updated_at"] > before["updated_at"]
    assert after == expected, "the namespace rewrite must carry every other persisted field"
    assert await stored_row(client, "loom.namespaces", other_org, target) == foreign_before
    if kind == "collection":
        assert tree_before is not None
        tree_after = await stored_row(client, "loom.collections", org, target)
        assert tree_after["version"] > tree_before["version"]
        assert tree_after == dict(tree_before, owner="", version=tree_after["version"])
        assert (
            await stored_row(client, "loom.collections", other_org, target)
        ) == foreign_tree_before
    owner_rows = await client.query_one(
        "SELECT count() AS n, countIf(kind = 'memory') AS ordinary "
        "FROM loom.namespaces FINAL WHERE org = {org:String} AND owner_email = {owner:String} "
        "AND ifNull(active, true) = true",
        params={"org": org, "owner": owner},
    )
    assert owner_rows == {"n": 1024, "ordinary": 1024}
    await cold_decision(auth, 1024, False)
    assert account_counts[-1] == 1024
    assert time.monotonic() - warmed_at < settings.grant_cache_ttl_seconds
    assert await scim.refuses_auth(auth) is False, (
        "the same warm memo must observe the actual rewrite"
    )
    reads = len(people.results)
    assert await scim.refuses_auth(auth) is False
    assert len(people.results) == reads, "the new qualifying decision must also be memoized"
    claim_keys = [
        "service_account:" + json.dumps([org, name]),
        "account:" + json.dumps([org, target]),
    ]
    if kind == "collection":
        claim_keys.append("collections:" + json.dumps(org))
    assert (
        await client.query_all(
            "SELECT name FROM loom._migration_lock WHERE name IN {names:Array(String)}",
            params={"names": claim_keys},
        )
        == []
    )
