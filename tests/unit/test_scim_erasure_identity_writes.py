"""Erasure directory rewrites discard the complete person decision memo."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest

from server.auth import delegation, revocation, scim, scim_people, scim_routes
from server.auth.delegation import delegate
from server.auth.jwt import AuthInfo
from server.core import ch
from server.core.principals import USER, personal_name, principal_id
from server.erasure.app import saga_steps
from server.identity.api.models import NamespaceRegisterInput
from server.identity.app import me_service_accounts
from server.identity.app.namespace import registry_row
from server.operations.base import Role
from server.settings import settings
from tests.unit.test_scim_people import People
from tests.unit.test_scim_people import people as people

ORG = "acme"
USER_ID = "alice@example.test"
OWNER = USER + USER_ID
SA = "sa:erasure-child-123abc"
# Capture at collection, before the function-scoped autouse parent-policy stub.
_REFUSE_A_REVOKED_PARENT = delegation._refuse_a_revoked_parent


@asynccontextmanager
async def claim_edge(*args, **kwargs):
    yield


class Directory(People):
    """The I/O edges share FINAL directory rows, not independent policy answers."""

    def __init__(self, rows, observed):
        super().__init__()
        self.rows = {row["namespace"]: dict(row) for row in rows}
        self.tree = {row["namespace"]: OWNER for row in rows if row["kind"] == "collection"}
        self.observed = observed
        self.failure = None
        self.auth = None
        self.writes = []
        self.query_parameters = []
        self.expected_revocation_keys = []
        self.revocation_reads = []

    def selected(self, table, params):
        if table == "loom.collections":
            return [ns for ns, owner in self.tree.items() if owner == params["p"]]
        if "ns" in params:
            return [
                ns
                for ns, row in self.rows.items()
                if ns == params["ns"] and row["org"] == params["org"] and row["active"]
            ]
        return [
            ns
            for ns, row in self.rows.items()
            if row["org"] == params["org"]
            and row["kind"] == "collection"
            and row["owner_email"] == params["p"]
        ]

    def sync_accounts(self):
        self.accounts = [
            (row["org"], row["owner_email"], row["contact_email"], not row["password_hash"])
            for row in self.rows.values()
            if row["active"]
        ]

    async def query_all(self, sql, params=None):
        params = params or {}
        self.query_parameters.append(params)
        if "FROM loom.revoked_tokens" in sql:
            assert "WHERE jti IN {keys:Array(String)} AND expires_at > now()" in sql
            assert self.expected_revocation_keys
            assert params == {"keys": self.expected_revocation_keys}
            self.revocation_reads.append(list(params["keys"]))
            return []  # the store holds no unexpired revocation for these exact keys
        if "UNION DISTINCT" in sql and "loom.collections" in sql:
            assert params == {"org": ORG, "p": OWNER}
            names = set(self.selected("loom.namespaces", params)) | set(self.tree)
            return [{"namespace": ns} for ns in sorted(names)]
        self.sync_accounts()
        return await super().query_all(sql, params=params)

    async def query_one(self, sql, params=None):
        params = params or {}
        self.query_parameters.append(params)
        if sql.startswith("SELECT count() AS n FROM "):
            table = sql.split()[5]
            assert table in ("loom.namespaces", "loom.collections")
            return {"n": len(self.selected(table, params))}
        if "max(version) + 1" in sql:
            assert "loom.service_accounts" in sql
            return {"v": 1}
        if "SELECT sa_subject, created_by, lineage" in sql:
            assert params == {"org": ORG, "name": "erasure-child"}
            return None
        self.sync_accounts()
        return await super().query_one(sql, params=params)

    async def insert(self, table, rows, **kwargs):
        assert table == "loom.service_accounts"
        for row in rows:
            self.services[row["sa_subject"]] = dict(row)

    async def execute(self, sql, params=None, **kwargs):
        params = params or {}
        assert sql.startswith("INSERT INTO ") and "SELECT * REPLACE" in sql
        table = sql.split()[2]
        assert table in ("loom.namespaces", "loom.collections")
        selected = self.selected(table, params)
        assert selected
        self.writes.append((table, sql, dict(params), tuple(selected)))
        if table == "loom.collections":
            assert "'' AS owner" in sql
            for ns in selected:
                self.tree[ns] = ""
            return
        # Observe the actual cache path during the actual writer's I/O call.
        # Nothing clears or edits the memo between any of these checks.
        reads = len(self.reads)
        self.observed["during_refused"] = await scim.refuses_auth(self.auth)
        self.observed["during_read_delta"] = len(self.reads) - reads
        assert "'' AS owner_email" in sql
        for ns in selected:
            row = self.rows[ns]
            row["owner_email"] = ""
            if "false AS active" in sql:
                row["active"] = False
                row["description"] = ""
                row["title"] = ""
            row["updated_at"] = row["updated_at"].replace(year=2099)
        self.observed["rewritten"] = len(selected)
        if self.failure is not None:
            raise self.failure


def directory_rows(kind, count):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    rows = []
    for i in range(count):
        ns = personal_name(ORG, OWNER) if kind == "personal" and i == 0 else f"col-audit-{i}"
        # The same builder and inputs used by collection.create and first-write
        # personal auto-registration: contacts and passwords are empty.
        row = registry_row(
            NamespaceRegisterInput(org=ORG, namespace=ns, owner_email=OWNER), None, now
        )
        assert row["kind"] == ("personal" if ns.startswith("col-me-") else "collection")
        assert row["contact_email"] == row["password_hash"] == ""
        rows.append(row)
    return rows


async def setup_delegated_world(people, monkeypatch, kind, count):
    assert principal_id(OWNER, USER) == USER_ID
    monkeypatch.setattr(settings, "grant_cache_ttl_seconds", 240)
    observed = {}
    world = Directory(directory_rows(kind, count), observed)
    _, email, _ = scim_routes.user_name_and_email(
        {"userName": OWNER, "emails": [{"value": OWNER, "type": "work", "primary": True}]}
    )
    assert email == OWNER
    world.provision(email)
    monkeypatch.setattr(ch, "get_client", lambda: world)
    monkeypatch.setattr(saga_steps, "account_change", claim_edge)
    monkeypatch.setattr(saga_steps, "collections_change", claim_edge)
    # This is the AuthInfo shape returned by the verified end-user issuer path.
    # Its normal delegate/save path creates a nonexempt native SA with the user
    # in creator, parent_subjects and persisted lineage.
    monkeypatch.setattr(delegation, "_refuse_a_revoked_parent", _REFUSE_A_REVOKED_PARENT)
    assert delegation._refuse_a_revoked_parent is _REFUSE_A_REVOKED_PARENT
    assert _REFUSE_A_REVOKED_PARENT.__module__ == "server.auth.delegation"
    assert _REFUSE_A_REVOKED_PARENT.__name__ == "_refuse_a_revoked_parent"
    parent = AuthInfo(
        subject=OWNER,
        org=ORG,
        namespaces=(),
        roles=(Role.VIEWER,),
        is_service_account=False,
        impersonated_by="https://issuer.example.test",
        read_scope="own_user",
        read_user_id=USER_ID,
        token_id="unit-end-user-token",
        issued_at=int(time.time()),
    )
    world.expected_revocation_keys = sorted(
        key
        for subject in (OWNER, parent.token_id)
        for key in (
            subject,
            revocation.CUTOFF_PREFIX + subject,
            revocation.SCIM_PREFIX + subject,
        )
    )
    child = await delegate(
        parent,
        org=ORG,
        requested={"*": None},
        scoped=False,
        ttl_seconds=60,
        is_service_account=True,
    )
    assert world.revocation_reads == [world.expected_revocation_keys]
    await me_service_accounts.save_token(
        "erasure-child", SA, child, created_by=parent.subject, now=datetime.now(UTC)
    )
    world.auth = child.as_auth(SA)
    assert world.auth.subject == SA and world.auth.creator == OWNER
    assert OWNER in world.auth.parent_subjects and OWNER in world.services[SA]["lineage"]
    assert world.services[SA]["revoked_at"] is None
    assert world.auth.impersonated_by == ""
    assert len(world.rows) == count
    assert sum(len(row["contact_email"].encode()) for row in world.rows.values()) == 0
    return world, observed


async def cold_decision(world):
    auth = world.auth
    roots = (auth.subject, *sorted(set((*auth.parent_subjects, auth.creator))))
    decision = scim_people._Decision(ORG, auth.subject, "", scim_people._now() + 30)
    try:
        result = await decision.refuses(roots)
    except scim_people._Refused:
        result = True  # the public whole-decision wrapper maps supported-size overflow to refusal
    assert decision.transferred < 65_536
    assert all(
        len(json.dumps(params, ensure_ascii=False, separators=(",", ":")).encode()) < 65_536
        for params in world.query_parameters
    )
    return result


async def rewrite(kind):
    if kind == "personal":
        return await saga_steps._step_retire_personal_collection_by_user(ORG, USER_ID)
    return await saga_steps._step_forget_collection_owner_by_user(ORG, USER_ID)


@pytest.mark.parametrize("kind", ["personal", "collection"])
@pytest.mark.parametrize("outcome", ["success", "lost-reply", "cancel"])
async def test_erasure_rechecks_native_ancestry_after_account_budget_shrinks(
    people, monkeypatch, kind, outcome
):
    world, observed = await setup_delegated_world(people, monkeypatch, kind, 1025)
    assert await scim.refuses_auth(world.auth) is True
    assert await cold_decision(world) is True
    # Capture the same exception object through the app and store layers.
    if outcome != "success":
        world.failure = (
            ConnectionError("directory rewrite reply lost")
            if outcome == "lost-reply"
            else asyncio.CancelledError()
        )
        with pytest.raises(type(world.failure)) as caught:
            await rewrite(kind)
        assert caught.value is world.failure
    else:
        await rewrite(kind)
    assert observed["rewritten"] == (1 if kind == "personal" else 1025)
    assert await cold_decision(world) is False, (
        "the actual rewrite must leave a qualifying bounded graph"
    )
    assert await scim.refuses_auth(world.auth) is False, (
        "the old account-overflow refusal must not survive the local rewrite"
    )
    assert observed["during_refused"] is True
    assert observed["during_read_delta"] > 0, (
        "pre-attempt invalidation must prevent a warm answer during the write"
    )
    assert len(world.writes) == (1 if kind == "personal" else 2)


@pytest.mark.parametrize("kind", ["personal", "collection"])
async def test_erasure_preserves_a_qualifying_native_ancestry_control(people, monkeypatch, kind):
    world, observed = await setup_delegated_world(people, monkeypatch, kind, 1024)
    assert await cold_decision(world) is False
    assert await scim.refuses_auth(world.auth) is False
    await rewrite(kind)
    assert await cold_decision(world) is False
    assert await scim.refuses_auth(world.auth) is False
    assert observed["rewritten"] == (1 if kind == "personal" else 1024)
