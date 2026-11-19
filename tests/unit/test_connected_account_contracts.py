"""Canonical-owner persistence, cleanup, own-list and configured lifecycle bounds."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from types import SimpleNamespace
from typing import Any

import pytest

from server.auth import account_lifecycle
from server.auth.context import current_auth_var
from server.core.errors import BackendUnavailableError, ValidationError
from server.core.secrets_at_rest import unseal
from server.erasure.store import census
from server.knowledge.api.models_connections import ConnectionListInput
from server.knowledge.app import connected_accounts as accounts
from server.knowledge.app.connections import handle_connection_list
from server.knowledge.domain.connected_accounts import CREDENTIAL_PURPOSE, ConnectedAccount
from server.knowledge.store import connected_accounts as store
from tests.unit.test_connected_account_callbacks import (
    _auth,
    _World,
)
from tests.unit.test_connected_account_callbacks import (
    world as world,
)


class _Rows:
    def __init__(self) -> None:
        self.inserted: list[tuple[str, list[dict[str, Any]]]] = []
        self.queries: list[tuple[str, dict[str, Any]]] = []
        self.rows: list[dict[str, Any]] = []

    async def insert(self, table: str, rows: list[dict[str, Any]]) -> None:
        self.inserted.append((table, rows))

    async def execute(self, sql: str, *, params: dict[str, Any], **_kw: Any) -> None:
        self.queries.append((sql, params))

    async def query_all(self, sql: str, *, params: dict[str, Any]) -> list[dict[str, Any]]:
        self.queries.append((sql, params))
        return self.rows

    async def query_one(self, sql: str, *, params: dict[str, Any]) -> dict[str, int]:
        self.queries.append((sql, params))
        return {"n": 0}


async def test_connected_account_row_builder_preserves_every_field_and_monotonic_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _Rows()
    account = ConnectedAccount(
        "team",
        "canonical",
        "gdrive",
        "connection",
        account_id="provider-id",
        workspace_id="workspace",
        account_email_enc="sealed-email",
        credential_enc="sealed-refresh",
        scopes=("read",),
        active=True,
        refresh_id="reserved",
        version=40,
    )

    async def version(ch: Any, table: str, key: dict[str, str]) -> int:
        assert ch is rows and table == "loom.connected_accounts"
        assert key == {"org": "team", "subject": "canonical", "kind": "gdrive"}
        return 41

    monkeypatch.setattr(store, "next_version", version)
    await store.put(account, ch=rows)
    expected = asdict(account)
    expected.update(scopes=["read"], version=41)
    assert rows.inserted == [("loom.connected_accounts", [expected])]


async def test_canonical_owner_cleanup_step_matches_exact_store_and_census_axes() -> None:
    rows = _Rows()
    assert census.CANONICAL_OWNER_TABLES == {"loom.connected_accounts": "connected_accounts.erase"}
    await store.erase("team", ["canonical"], kind="gdrive", ch=rows)
    sql, params = rows.queries.pop()
    assert sql == (
        "DELETE FROM loom.connected_accounts WHERE org = {org:String} "
        "AND subject IN {subjects:Array(String)} AND kind = {kind:String}"
    )
    assert params == {"org": "team", "subjects": ["canonical"], "kind": "gdrive"}
    assert await census.connected_account_residue("team", "canonical", "gdrive", ch=rows) == 0
    sql, params = rows.queries.pop()
    assert "org = {org:String}" in sql and "subject = {subject:String}" in sql
    assert "kind = {kind:String}" in sql and "FINAL" not in sql
    assert params == {"org": "team", "subject": "canonical", "kind": "gdrive"}


async def test_owner_scan_refuses_truncation_and_reseal_page_uses_configured_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _Rows()
    monkeypatch.setattr(accounts.settings, "knowledge_connected_registry_scan_limit", 1)
    rows.rows = [{}, {}]
    with pytest.raises(RuntimeError, match="owner_bound"):
        await store.owned(["canonical"], org="team", ch=rows)
    assert rows.queries[-1][1] == {"subjects": ["canonical"], "org": "team", "limit": 2}
    rows.rows = []
    monkeypatch.setattr(accounts.settings, "knowledge_connected_registry_page_size", 3)
    assert await store.page(("team", "canonical", "gdrive"), ch=rows) == []
    assert rows.queries[-1][1]["limit"] == 3


async def test_account_lease_forwards_configured_ttl_and_wait_and_stop_bound(
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = []
    timeouts = []
    lease = accounts.leases.cluster_lease
    timeout = accounts.asyncio.timeout

    @asynccontextmanager
    async def tracked_lease(key: str, **kw: Any):
        held.append((key, kw))
        async with lease(key, **kw):
            yield

    def tracked_timeout(seconds: float):
        timeouts.append(seconds)
        return timeout(seconds)

    monkeypatch.setattr(accounts.settings, "knowledge_connected_lease_ttl_s", 41.0)
    monkeypatch.setattr(accounts.settings, "knowledge_connected_lease_wait_s", 1.25)
    monkeypatch.setattr(accounts.settings, "knowledge_connected_stop_timeout_s", 7.0)
    monkeypatch.setattr(accounts.leases, "cluster_lease", tracked_lease)
    monkeypatch.setattr(accounts, "asyncio", SimpleNamespace(timeout=tracked_timeout))
    await accounts.disconnect("team", "person", "gdrive")
    assert timeouts == [7.0]
    assert len(held) == 1 and held[0][0].startswith("connected-account:")
    assert held[0][1]["ttl_s"] == 41.0 and held[0][1]["wait_s"] == 1.25
    assert not world.requests
    assert set(world.rows) == {("team", "person", "gdrive")}
    ended = world.rows[("team", "person", "gdrive")]
    assert asdict(ended) == {
        "org": "team",
        "subject": "person",
        "kind": "gdrive",
        "connection_id": ended.connection_id,
        "account_id": "",
        "workspace_id": "",
        "account_email_enc": "",
        "credential_enc": "",
        "scopes": (),
        "active": False,
        "refresh_id": "",
        "connected_at": None,
        "disconnected_at": ended.disconnected_at,
        "version": ended.version,
    }
    assert ended.disconnected_at is not None and ended.version > 0
    prefix, generation = ended.connection_id.split("/")
    assert len(prefix) == len(generation) == 32 and generation.startswith("0")
    assert set(prefix + generation) <= set("0123456789abcdef")
    assert world.keeper == {
        "connected-account-generation:" + prefix: (ended.connection_id, accounts._FENCE_EXPIRY)
    }


async def test_provider_revoke_uses_its_own_bound_after_local_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = []

    async def revoke(token: str, **kw: Any):
        captured.append((token, kw))

    monkeypatch.setattr(accounts.settings, "knowledge_connected_revoke_timeout_s", 2.5)
    monkeypatch.setattr(accounts, "client", lambda _kind: SimpleNamespace(revoke=revoke))
    before = time.monotonic()
    await accounts._revoke(
        ConnectedAccount("team", "person", "gdrive", "stopped"), credential="old"
    )
    after = time.monotonic()
    assert len(captured) == 1 and captured[0][0] == "old"
    assert captured[0][1]["refresh"] is True
    assert before + 2.5 <= captured[0][1]["deadline"] <= after + 2.5


async def test_connection_list_uses_own_subject_and_hides_a_stopped_generation(
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _url, state, binding = await accounts.start(_auth(), "gdrive")
    await accounts.callback("gdrive", state, binding, "older-code")
    account = world.account()

    async def owned(subjects: list[str], *, org: str):
        assert subjects == ["person"] and org == "team"
        return [account]

    monkeypatch.setattr(store, "owned", owned)
    token = current_auth_var.set(_auth())
    try:
        result = await handle_connection_list(ConnectionListInput(org="team"))
        entry = next(x for x in result.connections if x.kind == "gdrive")
        assert entry.connected and entry.account_email == "display@example.com"
        with pytest.raises(ValidationError):
            await handle_connection_list(ConnectionListInput(org="other"))
        world.keeper[accounts._fence_key(account.connection_id)] = (
            "stopped",
            int(time.time()) + 60,
        )
        result = await handle_connection_list(ConnectionListInput(org="team"))
        entry = next(x for x in result.connections if x.kind == "gdrive")
        assert not entry.connected and entry.account_email is None and entry.scopes == []
    finally:
        current_auth_var.reset(token)
    assert unseal(CREDENTIAL_PURPOSE, account.credential_enc)


async def test_uncomposed_auth_retirement_refuses_instead_of_skipping_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(account_lifecycle, "_stopper", None)
    with pytest.raises(BackendUnavailableError):
        await account_lifecycle.stop_connections(["person"], erase_owner=True)


async def test_http_composition_binds_real_revocation_cleanup_before_serving(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi import FastAPI

    from server.knowledge.app import connection_routes

    scans = []

    async def identities(*, org=None):
        scans.append(org)
        return []

    monkeypatch.setattr(account_lifecycle, "_stopper", None)

    async def owned(subjects, *, org=None):
        assert subjects == ["person"] and org is None
        return []

    monkeypatch.setattr(store, "owner_identities", identities)
    monkeypatch.setattr(store, "owned", owned)
    connection_routes.attach(FastAPI())
    assert account_lifecycle._stopper is accounts.stop_subjects
    await account_lifecycle.stop_connections(["person"], respect_cutoff=True)
    assert scans == [None] and not world.requests
