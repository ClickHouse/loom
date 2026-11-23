"""Hosted real-store lifecycle checks; no generic source execution is admitted."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from server.auth import account_lifecycle, claims, revocation, scim
from server.core import ch as ch_module
from server.core.ch import CHClient, set_query_tag
from server.core.errors import OverloadedError, ValidationError
from server.erasure.app.connected_accounts import require_erased, require_sources_erased
from server.erasure.store.census import connected_account_residue, connected_source_residue
from server.knowledge.app import connected_accounts, connected_restore
from server.knowledge.app.sources import sources_lease
from server.knowledge.domain.connected_accounts import ConnectedAccount
from server.knowledge.domain.sources import mirror_synced, served
from server.knowledge.store import connected_accounts as account_store
from server.knowledge.store import mirrors as mirror_store
from server.knowledge.store import source_retirements
from server.knowledge.store import sources as source_store
from tests.integration._resources import unique_org

pytestmark = pytest.mark.usefixtures("ch_target_required")

OWNER = "first@example.test"
OTHER_OWNER = "second@example.test"
SLOT = "mcp:" + "a" * 32
OTHER_SLOT = "mcp:" + "b" * 32
# Fixed generations belong only to the four inactive metadata cases; never create their claims.
GENERATION = "1" * 32 + "/0" + "a" * 31
LATER_GENERATION = "1" * 32 + "/1" + "b" * 31
OTHER_GENERATION = "2" * 32 + "/1" + "c" * 31
SECOND_OWNER_GENERATION = "3" * 32 + "/0" + "d" * 31
NATIVE_GENERATION = "4" * 32 + "/0" + "e" * 31
CONFIG = '{ "profile": "fixture", "future": {"marker": "opaque-config"} }'
ITEMS = '[ {"resource_id": "fixture://selected", "future": "opaque-selection"} ]'


@dataclass
class _World:
    ch: CHClient
    org: str
    rows: list[dict[str, Any]] = field(default_factory=list)
    accounts: list[ConnectedAccount] = field(default_factory=list)
    mirrors: dict[tuple[str, str], str] = field(default_factory=dict)

    @asynccontextmanager
    async def locked(self, row: dict[str, Any]) -> AsyncIterator[None]:
        kind = row["connection_slot"] or row["source_kind"]
        async with connected_accounts.account_lease(
            row["org"], row["owner_subject"], kind, ch=self.ch
        ):
            async with sources_lease(row["org"], row["namespace"], ch=self.ch):
                yield

    def source_row(
        self,
        name: str,
        source_id: str,
        *,
        namespace: str = "selected",
        owner: str = OWNER,
        slot: str = SLOT,
        generation: str = GENERATION,
        config: str = CONFIG,
        items: str = ITEMS,
        native: bool = False,
        org: str | None = None,
        active: bool = False,
        contract_version: int = 1,
        version: int = 1,
    ) -> dict[str, Any]:
        return {
            "org": org or self.org,
            "namespace": namespace,
            "name": name,
            "source_id": str(UUID(source_id)),
            "kind": "gdrive" if native else "",
            "source_kind": "gdrive" if native else "mcp",
            "credential_mode": "connected" if native else "mcp",
            "owner_subject": owner,
            "connection_slot": "" if native else slot,
            "connection_id": generation,
            "source_contract_version": 0 if native else contract_version,
            "access": "mirrored",
            "config_json": config,
            "items_json": items,
            "credential_enc": "fixture-unreadable-envelope",
            "secret_enc": "fixture-unreadable-secret",
            "created_by": owner,
            "active": active,
            "pending_delete": False,
            "version": version,
        }

    async def seed(self, name: str, source_id: str, **kwargs: Any) -> dict[str, Any]:
        row = self.source_row(name, source_id, **kwargs)
        await self.insert_rows([row])
        return row

    async def insert_rows(self, rows: list[dict[str, Any]]) -> None:
        self.rows.extend(rows)  # Record exact identities before an insert can partially land.
        accounts = sorted(
            {(r["org"], r["owner_subject"], r["connection_slot"] or r["source_kind"]) for r in rows}
        )
        namespaces = sorted({(r["org"], r["namespace"]) for r in rows})
        set_query_tag("test.mcp-source-lifecycle.seed")
        async with AsyncExitStack() as stack:
            for org, owner, kind in accounts:
                await stack.enter_async_context(
                    connected_accounts.account_lease(org, owner, kind, ch=self.ch)
                )
            for org, namespace in namespaces:
                await stack.enter_async_context(sources_lease(org, namespace, ch=self.ch))
            await self.ch.insert("loom.knowledge_sources", rows)

    async def raw(self, row: dict[str, Any]) -> dict[str, Any] | None:
        return await self.ch.query_one(
            "SELECT org, namespace, name, source_id, kind, source_kind, credential_mode, "
            "owner_subject, connection_slot, connection_id, source_contract_version, access, "
            "config_json, items_json, credential_enc, secret_enc, created_by, active, "
            "pending_delete, created_at FROM loom.knowledge_sources FINAL "
            "WHERE org = {org:String} AND namespace = {namespace:String} "
            "AND name = {name:String} AND source_id = {source_id:UUID} LIMIT 1",
            params={key: row[key] for key in ("org", "namespace", "name", "source_id")},
        )

    async def source_versions(
        self, row: dict[str, Any], *, final: bool = False
    ) -> list[dict[str, Any]]:
        suffix = " FINAL" if final else ""
        return await self.ch.query_all(
            f"SELECT * FROM loom.knowledge_sources{suffix} "
            "WHERE org = {org:String} AND namespace = {namespace:String} "
            "AND name = {name:String} ORDER BY version LIMIT 10",
            params={key: row[key] for key in ("org", "namespace", "name")},
        )

    async def seed_account(
        self, kind: str = SLOT, *, org: str | None = None, owner: str = OWNER
    ) -> ConnectedAccount:
        account = ConnectedAccount(
            org or self.org,
            owner,
            kind,
            uuid4().hex + "/1" + uuid4().hex[1:],
            credential_enc="fixture-unreadable-account-envelope",
            active=True,
            connected_at=datetime.now(UTC),
        )
        async with connected_accounts.account_lease(
            account.org, account.subject, account.kind, ch=self.ch
        ):
            key = connected_accounts._fence_key(account.connection_id)
            assert await claims.get(self.ch, key) is None
            self.accounts.append(account)
            await connected_accounts._move_fence(None, account.connection_id, ch=self.ch)
            await account_store.put(account, ch=self.ch)
        return account

    async def account_versions(self, account: ConnectedAccount) -> list[dict[str, Any]]:
        return await self.ch.query_all(
            "SELECT * FROM loom.connected_accounts WHERE org = {org:String} "
            "AND subject = {owner:String} AND kind = {kind:String} ORDER BY version LIMIT 10",
            params={"org": account.org, "owner": account.subject, "kind": account.kind},
        )

    async def add_mirror(self, org: str, namespace: str) -> None:
        mirror = "acl-" + uuid4().hex[:24]
        self.mirrors[org, namespace] = mirror
        async with sources_lease(org, namespace, ch=self.ch):
            await mirror_store.add_mirror(org, namespace, mirror, ch=self.ch)

    def retirement(self, row: dict[str, Any]) -> source_retirements.Retirement:
        return source_retirements.Retirement(
            row["org"],
            row["namespace"],
            row["name"],
            row["source_id"],
            row["source_kind"],
            self.mirrors[row["org"], row["namespace"]],
        )

    async def retirement_row(self, row: dict[str, Any]) -> dict[str, Any] | None:
        item = self.retirement(row)
        return await self.ch.query_one(
            "SELECT * FROM loom.code_index_state FINAL WHERE org = {org:String} "
            "AND namespace = {mirror:String} AND state_key = {key:String} LIMIT 1",
            params={"org": item.org, "mirror": item.mirror, "key": item.key},
        )

    async def cleanup_account(self, account: ConnectedAccount) -> None:
        async with connected_accounts.account_lease(
            account.org, account.subject, account.kind, ch=self.ch
        ):
            key = connected_accounts._fence_key(account.connection_id)
            observed = await claims.get(self.ch, key)
            if observed is not None:
                assert connected_accounts._fence_key(observed) == key
                if not observed.partition("/")[2].startswith("0"):
                    stopped = connected_accounts._next_connection(observed, inactive=True)
                    await connected_accounts._move_fence(observed, stopped, ch=self.ch)
                    observed = stopped
            await account_store.erase(account.org, [account.subject], kind=account.kind, ch=self.ch)
            await require_erased(account.org, account.subject, account.kind, ch=self.ch)
            if observed is not None:
                await claims._drop(self.ch, key, observed)
                assert await claims.get(self.ch, key) is None

    async def cleanup_source(self, row: dict[str, Any]) -> None:
        async with self.locked(row):
            await self.ch.execute(
                "DELETE FROM loom.knowledge_sources WHERE org = {org:String} "
                "AND namespace = {namespace:String} AND name = {name:String} "
                "AND source_id = {source_id:UUID}",
                params={key: row[key] for key in ("org", "namespace", "name", "source_id")},
                settings={"lightweight_deletes_sync": 2},
            )
            if (row["org"], row["namespace"]) in self.mirrors:
                item = self.retirement(row)
                await self.ch.execute(
                    "DELETE FROM loom.code_index_state WHERE org = {org:String} "
                    "AND namespace = {mirror:String} AND state_key = {key:String}",
                    params={"org": item.org, "mirror": item.mirror, "key": item.key},
                    settings={"lightweight_deletes_sync": 2},
                )

    async def cleanup_mirror(self, org: str, namespace: str, mirror: str) -> None:
        async with sources_lease(org, namespace, ch=self.ch):
            await self.ch.execute(
                "DELETE FROM loom.knowledge_mirrors WHERE org = {org:String} "
                "AND namespace = {namespace:String} AND mirror = {mirror:String}",
                params={"org": org, "namespace": namespace, "mirror": mirror},
                settings={"lightweight_deletes_sync": 2},
            )

    async def cleanup(self) -> None:
        failures = []
        work = [(self.cleanup_account, (a,)) for a in self.accounts]
        work += [(self.cleanup_source, (r,)) for r in self.rows]
        work += [(self.cleanup_mirror, (o, n, m)) for (o, n), m in self.mirrors.items()]
        for operation, arguments in work:
            set_query_tag("test.mcp-source-lifecycle.cleanup")
            try:
                await operation(*arguments)
            except Exception as error:  # noqa: BLE001 — attempt cleanup of every owned identity
                failures.append(error)
        if failures:
            raise ExceptionGroup("MCP lifecycle fixture cleanup failed", failures)


@pytest.fixture
async def world(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_World]:
    ch = CHClient()
    await ch.connect()
    state = _World(ch, unique_org("mcp-source-lifecycle"))
    # Store/lease defaults resolve the first factory; account lifecycle captures the second.
    # Both bindings use this same real client, without replacing any SQL or authority result.
    monkeypatch.setattr(ch_module, "get_client", lambda: ch)
    monkeypatch.setattr(connected_accounts, "get_client", lambda: ch)
    try:
        yield state
    finally:
        try:
            await state.cleanup()
        finally:
            await ch.disconnect()


@pytest.mark.parametrize(
    ("config", "items"),
    [(CONFIG, ITEMS), ('{"marker":"opaque-config",', '[{"marker":"opaque-selection"},')],
    ids=["unknown-fields", "corrupt-json"],
)
async def test_raw_metadata_roundtrips_without_native_admission(
    world: _World, config: str, items: str
) -> None:
    row = await world.seed("opaque", "a" * 32, config=config, items=items)
    native = await world.seed(
        "native",
        "d" * 32,
        native=True,
        generation=NATIVE_GENERATION,
        config='{"folder_ids":["fixture-folder"]}',
        items='[{"type":"folder","id":"fixture-folder"}]',
    )
    before, native_before = await world.raw(row), await world.raw(native)
    assert before is not None and native_before is not None
    async with world.locked(row):
        source = await source_store.latest_source(
            world.org, row["namespace"], row["name"], ch=world.ch
        )
        assert source is not None
        assert (source.kind, source.credential_mode, source.access) == ("mcp", "mcp", "mirrored")
        assert source.connection_slot == SLOT
        assert source.connection_id == GENERATION
        assert source.source_contract_version == 1
        assert source.source_id == row["source_id"]
        assert not served(source) and not mirror_synced(source)
        assert "opaque-config" not in repr(source)
        assert "opaque-selection" not in repr(source)
        await source_store.insert_source(
            replace(source, created_by="roundtrip-writer"), ch=world.ch
        )
    assert await world.raw(row) == before | {"created_by": "roundtrip-writer"}
    assert await world.raw(native) == native_before
    neighbor = await source_store.latest_source(
        world.org, native["namespace"], native["name"], ch=world.ch
    )
    assert neighbor is not None and mirror_synced(neighbor)


async def test_slot_selectors_and_erasure_preserve_other_bindings(world: _World) -> None:
    first = await world.seed("first", "a" * 32)
    historical = await world.seed(
        "historical", "e" * 32, namespace="historical", generation=LATER_GENERATION
    )
    second = await world.seed("second", "b" * 32, slot=OTHER_SLOT, generation=OTHER_GENERATION)
    other_owner = await world.seed(
        "other-owner", "c" * 32, owner=OTHER_OWNER, generation=SECOND_OWNER_GENERATION
    )
    native = await world.seed(
        "native",
        "d" * 32,
        native=True,
        generation=NATIVE_GENERATION,
        config='{"folder_ids":["fixture-folder"]}',
        items='[{"type":"folder","id":"fixture-folder"}]',
    )
    retained = [second, other_owner, native]
    snapshots = [await world.raw(row) for row in retained]
    owned = await source_store.connected_sources(world.org, OWNER, ch=world.ch)
    assert {source.source_id for source in owned} == {
        row["source_id"] for row in (first, historical, second, native)
    }
    assert await source_store.connected_owner_namespaces(world.org, OWNER, SLOT, ch=world.ch) == [
        "historical",
        "selected",
    ]
    assert await source_store.connected_owner_namespaces(
        world.org, OWNER, OTHER_SLOT, ch=world.ch
    ) == ["selected"]
    history = await source_store.connected_history(owner=(world.org, OWNER, SLOT), ch=world.ch)
    assert {source.source_id for source in history} == {first["source_id"], historical["source_id"]}
    async with connected_accounts.account_lease(world.org, OWNER, SLOT, ch=world.ch):
        # Absence is checked while holding the account lease; no active account can race this erase.
        assert await account_store.get(world.org, OWNER, SLOT, ch=world.ch) is None
        for row in (first, historical):
            set_query_tag("test.mcp-source-lifecycle.erase")
            async with sources_lease(world.org, row["namespace"], ch=world.ch):
                assert (
                    await connected_source_residue(
                        world.org, row["namespace"], OWNER, SLOT, ch=world.ch
                    )
                    == 1
                )
                with pytest.raises(OverloadedError):
                    await require_sources_erased(
                        world.org, row["namespace"], OWNER, SLOT, ch=world.ch
                    )
                await source_store.erase_connected_owner(
                    world.org, row["namespace"], OWNER, SLOT, ch=world.ch
                )
                await require_sources_erased(world.org, row["namespace"], OWNER, SLOT, ch=world.ch)
                assert await world.raw(row) is None
            assert [await world.raw(kept) for kept in retained] == snapshots
    assert await source_store.connected_history(owner=(world.org, OWNER, SLOT), ch=world.ch) == []
    assert await source_store.connected_owner_namespaces(world.org, OWNER, SLOT, ch=world.ch) == []
    for row in retained:
        set_query_tag("test.mcp-source-lifecycle.neighbor")
        slot = row["connection_slot"] or row["source_kind"]
        assert (
            await connected_source_residue(
                world.org, row["namespace"], row["owner_subject"], slot, ch=world.ch
            )
            == 1
        )


async def test_source_only_restore_inventory_validates_slot_and_generation(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = await world.seed("first", "a" * 32)
    past = await world.seed("historical", "e" * 32, generation=LATER_GENERATION)
    second = await world.seed("second", "b" * 32, slot=OTHER_SLOT, generation=OTHER_GENERATION)
    await world.seed("other-owner", "c" * 32, owner=OTHER_OWNER, generation=SECOND_OWNER_GENERATION)

    def refuse_decryption(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Restore inventory tried to decrypt an envelope")

    monkeypatch.setattr(connected_restore, "unseal", refuse_decryption)
    owner = (world.org, OWNER, SLOT)
    assert await account_store.restore_keys(owner=owner, ch=world.ch) == []
    keys, sources = await connected_restore._metadata(world.ch, owner)
    assert set(keys) == {(*owner, GENERATION), (*owner, LATER_GENERATION)}
    assert {source.source_id for source in sources} == {first["source_id"], past["source_id"]}
    other_keys, other_sources = await connected_restore._metadata(
        world.ch, (world.org, OWNER, OTHER_SLOT)
    )
    assert other_keys == [(world.org, OWNER, OTHER_SLOT, OTHER_GENERATION)]
    assert [source.source_id for source in other_sources] == [second["source_id"]]
    groups: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    prefixes: dict[str, tuple[str, str, str]] = {}
    connected_restore._include(groups, prefixes, keys + other_keys)
    assert dict(groups) == {
        owner: {GENERATION, LATER_GENERATION},
        (world.org, OWNER, OTHER_SLOT): {OTHER_GENERATION},
    }
    for collision in [
        (world.org, OTHER_OWNER, SLOT, GENERATION),
        (world.org, OWNER, OTHER_SLOT, GENERATION),
        (world.org + "-other", OWNER, SLOT, GENERATION),
    ]:
        with pytest.raises(ValidationError):
            connected_restore._include(defaultdict(set), dict(prefixes), [collision])
    invalid_slots = (
        "mcp",
        "mcp:",
        "mcp:" + "A" * 32,
        "mcp:" + "a" * 31,
        "mcp:" + "g" * 32,
        SLOT + " ",
    )
    for invalid in invalid_slots:
        with pytest.raises(ValidationError):
            connected_restore._include(
                defaultdict(set), {}, [(world.org, OWNER, invalid, GENERATION)]
            )
    for invalid_generation in (
        "1" * 32 + "/2" + "a" * 31,
        GENERATION[:-1],
        "F" * 32 + "/0" + "a" * 31,
    ):
        with pytest.raises(ValidationError):
            connected_restore._include(defaultdict(set), {}, [(*owner, invalid_generation)])


async def test_superseded_same_key_binding_is_erased_without_touching_final_owner(
    world: _World,
) -> None:
    other_org = unique_org("mcp-source-history-neighbor")
    old = world.source_row(
        "replaced", str(uuid4()), generation=uuid4().hex + "/1" + uuid4().hex[1:]
    )
    newer = world.source_row(
        old["name"],
        str(uuid4()),
        owner=OTHER_OWNER,
        slot=OTHER_SLOT,
        generation=uuid4().hex + "/1" + uuid4().hex[1:],
        version=2,
    )
    other_namespace = await world.seed("replaced", str(uuid4()), namespace="untouched")
    other_tenant = await world.seed("replaced", str(uuid4()), org=other_org)
    # One real batch keeps both versions at the actual (org, namespace, name) key.
    await world.insert_rows([old, newer])
    raw = await world.source_versions(old)
    assert {str(r["source_id"]) for r in raw} == {old["source_id"], newer["source_id"]}, (
        "both superseded raw versions must exist before history/erasure qualification"
    )
    [latest] = await world.source_versions(old, final=True)
    assert str(latest["source_id"]) == newer["source_id"]
    assert (latest["owner_subject"], latest["connection_slot"]) == (OTHER_OWNER, OTHER_SLOT)
    neighbors = [newer, other_namespace, other_tenant]
    before = [await world.source_versions(row, final=True) for row in neighbors]
    history = await source_store.connected_history(owner=(world.org, OWNER, SLOT), ch=world.ch)
    assert {s.source_id for s in history} == {old["source_id"], other_namespace["source_id"]}
    assert await source_store.connected_owner_namespaces(world.org, OWNER, SLOT, ch=world.ch) == [
        "selected",
        "untouched",
    ]
    async with world.locked(old):
        assert await account_store.get(world.org, OWNER, SLOT, ch=world.ch) is None
        assert (
            await connected_source_residue(world.org, old["namespace"], OWNER, SLOT, ch=world.ch)
            == 1
        )
        with pytest.raises(OverloadedError):
            await require_sources_erased(world.org, old["namespace"], OWNER, SLOT, ch=world.ch)
        await source_store.erase_connected_owner(
            world.org, old["namespace"], OWNER, SLOT, ch=world.ch
        )
        await require_sources_erased(world.org, old["namespace"], OWNER, SLOT, ch=world.ch)
    remaining = await world.source_versions(old)
    assert len(remaining) == 1 and str(remaining[0]["source_id"]) == newer["source_id"]
    assert [await world.source_versions(row, final=True) for row in neighbors] == before
    remaining_history = await source_store.connected_history(
        owner=(world.org, OWNER, SLOT), ch=world.ch
    )
    assert [s.source_id for s in remaining_history] == [other_namespace["source_id"]]
    assert await connected_source_residue(world.org, "untouched", OWNER, SLOT, ch=world.ch) == 1
    assert await connected_source_residue(other_org, "selected", OWNER, SLOT, ch=world.ch) == 1


async def _seed_lifecycle(
    world: _World,
) -> tuple[ConnectedAccount, list[dict[str, Any]], list[ConnectedAccount], list[dict[str, Any]]]:
    target = await world.seed_account()
    neighbors = [
        await world.seed_account(OTHER_SLOT),
        await world.seed_account("gdrive"),
        await world.seed_account(owner=OTHER_OWNER),
        await world.seed_account(org=unique_org("mcp-source-stop-neighbor")),
    ]
    for org, namespace in {(a.org, "selected") for a in [target, *neighbors]} | {
        (target.org, "historical")
    }:
        await world.add_mirror(org, namespace)
    selected = []
    for name, namespace in [("unknown-contract", "selected"), ("corrupt-contract", "historical")]:
        selected.append(
            await world.seed(
                name,
                str(uuid4()),
                namespace=namespace,
                generation=target.connection_id,
                config='{"future":',
                items='[{"future":',
                contract_version=65535,
                active=True,
            )
        )
    other_rows = []
    for index, account in enumerate(neighbors):
        other_rows.append(
            await world.seed(
                f"neighbor-{index}",
                str(uuid4()),
                org=account.org,
                owner=account.subject,
                slot=account.kind,
                generation=account.connection_id,
                native=account.kind == "gdrive",
            )
        )
    return target, selected, neighbors, other_rows


async def _neighbor_snapshot(
    world: _World, accounts: list[ConnectedAccount], rows: list[dict[str, Any]]
) -> tuple[Any, ...]:
    return (
        [await world.account_versions(account) for account in accounts],
        [
            await claims.get(world.ch, connected_accounts._fence_key(a.connection_id))
            for a in accounts
        ],
        [await world.source_versions(row, final=True) for row in rows],
        [await world.retirement_row(row) for row in rows],
    )


def _forbid_decryption(*args: Any, **kwargs: Any) -> None:
    pytest.fail("local stop/erasure tried to decrypt a generic credential")


async def _assert_stopped(
    world: _World,
    original: ConnectedAccount,
    ended: ConnectedAccount,
    rows: list[dict[str, Any]],
    before: list[dict[str, Any]],
) -> None:
    key = connected_accounts._fence_key(original.connection_id)
    assert ended.connection_id != original.connection_id
    assert connected_accounts._fence_key(ended.connection_id) == key
    assert ended.connection_id.partition("/")[2].startswith("0")
    assert await claims.get(world.ch, key) == ended.connection_id
    marker = await account_store.get(original.org, original.subject, original.kind, ch=world.ch)
    assert marker is not None and marker.connection_id == ended.connection_id
    [raw] = await world.account_versions(original)
    assert not raw["active"] and raw["connected_at"] is None
    assert raw["disconnected_at"] is not None
    for column in (
        "credential_enc",
        "account_email_enc",
        "account_id",
        "workspace_id",
        "refresh_id",
    ):
        assert raw[column] == ""
    assert not raw["scopes"]
    assert (
        await connected_account_residue(
            original.org,
            original.subject,
            original.kind,
            keep_connection=ended.connection_id,
            ch=world.ch,
        )
        == 0
    )
    assert (
        await connected_account_residue(
            original.org,
            original.subject,
            original.kind,
            ch=world.ch,
        )
        == 1
    )
    await require_erased(
        original.org,
        original.subject,
        original.kind,
        keep_connection=ended.connection_id,
        ch=world.ch,
    )
    for row, initial in zip(rows, before, strict=True):
        [current] = await world.source_versions(row, final=True)
        assert current == initial | {
            "active": False,
            "credential_enc": "",
            "secret_enc": "",
            "pending_delete": True,
            "updated_at": current["updated_at"],
            "version": current["version"],
        }
        item = world.retirement(row)
        assert await source_retirements.pending(item, ch=world.ch)
        retirement = await world.retirement_row(row)
        assert retirement is not None and retirement["state_value"] == "pending"
        assert json.loads(retirement["detail"]) == {
            "namespace": row["namespace"],
            "name": row["name"],
            "source_id": row["source_id"],
            "kind": "mcp",
        }
        assert (
            await connected_source_residue(
                original.org,
                row["namespace"],
                original.subject,
                original.kind,
                ch=world.ch,
            )
            > 0
        )


async def _erase_stopped(
    world: _World, ended: ConnectedAccount, rows: list[dict[str, Any]]
) -> None:
    async with connected_accounts.account_lease(ended.org, ended.subject, ended.kind, ch=world.ch):
        await connected_accounts._erase_locked(ended, keep_marker=False)
    assert await world.account_versions(ended) == []
    assert await account_store.get(ended.org, ended.subject, ended.kind, ch=world.ch) is None
    await require_erased(ended.org, ended.subject, ended.kind, ch=world.ch)
    for row in rows:
        assert await world.source_versions(row) == []
        await require_sources_erased(
            ended.org, row["namespace"], ended.subject, ended.kind, ch=world.ch
        )
        # Removing account history must not discard the durable copy-retirement request.
        assert await source_retirements.pending(world.retirement(row), ch=world.ch)
    assert (
        await claims.get(world.ch, connected_accounts._fence_key(ended.connection_id))
        == ended.connection_id
    )


async def test_stop_and_owner_erase_use_real_fence_retirement_and_census(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    account, selected, neighbors, other_rows = await _seed_lifecycle(world)
    before = [(await world.source_versions(row, final=True))[0] for row in selected]
    neighbors_before = await _neighbor_snapshot(world, neighbors, other_rows)
    monkeypatch.setattr(connected_accounts, "unseal", _forbid_decryption)
    async with connected_accounts.account_lease(
        account.org, account.subject, account.kind, ch=world.ch
    ):
        ended = await connected_accounts._stop_locked(account)
    await _assert_stopped(world, account, ended, selected, before)
    assert await _neighbor_snapshot(world, neighbors, other_rows) == neighbors_before
    await _erase_stopped(world, ended, selected)
    assert await _neighbor_snapshot(world, neighbors, other_rows) == neighbors_before


class _AfterFenceFailure(RuntimeError):
    pass


async def test_stop_recovers_after_fence_lands_before_account_marker(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    account, selected, neighbors, other_rows = await _seed_lifecycle(world)
    before = [(await world.source_versions(row, final=True))[0] for row in selected]
    neighbors_before = await _neighbor_snapshot(world, neighbors, other_rows)
    real_put = account_store.put
    failed = False

    async def fail_marker_once(value: ConnectedAccount, **kwargs: Any) -> None:
        nonlocal failed
        if (
            not failed
            and (value.org, value.subject, value.kind)
            == (account.org, account.subject, account.kind)
            and not value.active
        ):
            failed = True
            raise _AfterFenceFailure("injected failure after durable stop fence")
        await real_put(value, **kwargs)

    monkeypatch.setattr(connected_accounts, "unseal", _forbid_decryption)
    async with connected_accounts.account_lease(
        account.org, account.subject, account.kind, ch=world.ch
    ):
        with monkeypatch.context() as injection:
            injection.setattr(account_store, "put", fail_marker_once)
            with pytest.raises(_AfterFenceFailure, match="after durable stop fence"):
                await connected_accounts._stop_locked(account)
        assert failed
        stopped_fence = await claims.get(
            world.ch, connected_accounts._fence_key(account.connection_id)
        )
        assert stopped_fence is not None and stopped_fence != account.connection_id
        assert stopped_fence.partition("/")[2].startswith("0")
        still_live = await account_store.get(
            account.org, account.subject, account.kind, ch=world.ch
        )
        assert still_live is not None and still_live.active
        assert still_live.connection_id == account.connection_id
        assert still_live.credential_enc == account.credential_enc
        assert [(await world.source_versions(row, final=True))[0] for row in selected] == before
        assert [await world.retirement_row(row) for row in selected] == [None, None]
        ended = await connected_accounts._stop_locked(still_live)
        assert ended.connection_id == stopped_fence
    await _assert_stopped(world, account, ended, selected, before)
    assert await _neighbor_snapshot(world, neighbors, other_rows) == neighbors_before
    await _erase_stopped(world, ended, selected)
    assert await _neighbor_snapshot(world, neighbors, other_rows) == neighbors_before


@asynccontextmanager
async def _scim_erasure_ready(
    world: _World, monkeypatch: pytest.MonkeyPatch, owner: str
) -> AsyncIterator[None]:
    """The real post-deactivation caller reads an actual, uniquely owned SCIM cutoff."""
    key = revocation.SCIM_PREFIX + owner
    params = {"key": key}

    async def stored() -> dict[str, Any] | None:
        return await world.ch.query_one(
            "SELECT jti FROM loom.revoked_tokens FINAL WHERE jti = {key:String} LIMIT 1",
            params=params,
        )

    def no_provider(*args: Any, **kwargs: Any) -> None:
        pytest.fail("canonical generic owner erasure consulted a native provider")

    with monkeypatch.context() as wiring:
        # Direct auth calls use the same real composition as HTTP/CLI roots. No SQL is faked.
        wiring.setattr(account_lifecycle, "_stopper", connected_accounts.stop_subjects)
        wiring.setattr(connected_accounts, "client", no_provider)
        async with scim.subject_leases([owner]):
            assert await stored() is None
        try:
            async with scim.subject_leases([owner]):
                await world.ch.insert(
                    "loom.revoked_tokens",
                    [
                        {
                            "jti": key,
                            "revoked_at": datetime.now(UTC),
                            "expires_at": revocation.revocation_expiry(None),
                            "reason": "synthetic owner erasure",
                            "revoked_by": "scim",
                            "issued_before": None,
                            "version": 1,
                        }
                    ],
                )
            # Read the same real cutoff authority used by stop_subjects, not a cache stub.
            assert await revocation.cutoffs([owner], permanent=True) == {owner: None}
            yield
        finally:
            async with scim.subject_leases([owner]):
                await world.ch.execute(
                    "DELETE FROM loom.revoked_tokens WHERE jti = {key:String}",
                    params=params,
                    settings={"lightweight_deletes_sync": 2},
                )
                assert await stored() is None


async def _seed_canonical_owner_erasure(
    world: _World, *, with_accounts: bool
) -> tuple[
    str,
    list[ConnectedAccount],
    list[dict[str, Any]],
    list[ConnectedAccount],
    list[dict[str, Any]],
]:
    # The SCIM handoff intentionally spans orgs: fixed shared subjects would be unsafe.
    owner = f"mcp-erased-{uuid4().hex}@example.test"
    neighbor_owner = f"mcp-retained-{uuid4().hex}@example.test"
    orgs = [world.org, unique_org("mcp-source-owner-neighbor")]
    accounts: list[ConnectedAccount] = []
    selected: list[dict[str, Any]] = []
    neighbors: list[ConnectedAccount] = []
    other_rows: list[dict[str, Any]] = []
    for org, account_slot in zip(orgs, (SLOT, OTHER_SLOT), strict=True):
        set_query_tag("test.mcp-source-lifecycle.owner-seed")
        await world.add_mirror(org, "owner-erasure")
        for slot in (SLOT, OTHER_SLOT):
            account = None
            if with_accounts and slot == account_slot:
                account = await world.seed_account(slot, org=org, owner=owner)
                accounts.append(account)
            generation = account.connection_id if account else uuid4().hex + "/1" + uuid4().hex[1:]
            if account is None:
                # Cleanup owns this historical prefix even if a later implementation fences it.
                # This is bookkeeping only: no account row or claim is invented for the history.
                async with connected_accounts.account_lease(org, owner, slot, ch=world.ch):
                    assert (
                        await claims.get(world.ch, connected_accounts._fence_key(generation))
                        is None
                    )
                    world.accounts.append(
                        ConnectedAccount(org, owner, slot, generation, active=False)
                    )
                    assert await account_store.get(org, owner, slot, ch=world.ch) is None
            selected.append(
                await world.seed(
                    ("registered" if account else "source-only")
                    + ("-first" if slot == SLOT else "-second"),
                    str(uuid4()),
                    org=org,
                    namespace="owner-erasure",
                    owner=owner,
                    slot=slot,
                    generation=generation,
                    active=account is not None,
                    contract_version=65535,
                    config='{"unknown-owner-contract":',
                    items='[{"unknown-selection":',
                )
            )
        neighbor = await world.seed_account(SLOT, org=org, owner=neighbor_owner)
        neighbors.append(neighbor)
        other_rows.append(
            await world.seed(
                "other-owner",
                str(uuid4()),
                org=org,
                namespace="owner-erasure",
                owner=neighbor_owner,
                slot=neighbor.kind,
                generation=neighbor.connection_id,
                active=True,
            )
        )
    native = await world.seed_account("gdrive", owner=neighbor_owner)
    neighbors.append(native)
    other_rows.append(
        await world.seed(
            "native-owner-neighbor",
            str(uuid4()),
            namespace="owner-erasure",
            owner=neighbor_owner,
            generation=native.connection_id,
            native=True,
            active=True,
            config='{"folder_ids":["fixture-folder"]}',
            items='[{"type":"folder","id":"fixture-folder"}]',
        )
    )
    return owner, accounts, selected, neighbors, other_rows


@pytest.mark.parametrize(
    "with_accounts", [True, False], ids=["mixed-account-history", "source-only-owner"]
)
async def test_scim_owner_erasure_handoff_erases_source_only_history_across_orgs(
    world: _World, monkeypatch: pytest.MonkeyPatch, with_accounts: bool
) -> None:
    owner, accounts, selected, neighbors, other_rows = await _seed_canonical_owner_erasure(
        world, with_accounts=with_accounts
    )
    before = await _neighbor_snapshot(world, neighbors, other_rows)
    assert len(await account_store.owned([owner], ch=world.ch)) == len(accounts)
    for row in selected:
        set_query_tag("test.mcp-source-lifecycle.owner-before")
        assert await world.source_versions(row)
        assert (
            await connected_source_residue(
                row["org"], row["namespace"], owner, row["connection_slot"], ch=world.ch
            )
            == 1
        )
        assert await world.retirement_row(row) is None
    async with _scim_erasure_ready(world, monkeypatch, owner):
        # This is SCIM delete's real post-deactivation caller, including its current/previous
        # subject union, respect_cutoff, erase_owner and service-account cleanup. Not _erase_locked.
        subjects = [owner] if with_accounts else []
        previous_subjects = [] if with_accounts else [owner]
        await scim._revoke_tokens(
            {"subjects": subjects, "deleted": True}, previous_subjects=previous_subjects
        )
        assert await _neighbor_snapshot(world, neighbors, other_rows) == before
        assert await account_store.owned([owner], ch=world.ch) == []
        for account in accounts:
            set_query_tag("test.mcp-source-lifecycle.owner-account-erased")
            await require_erased(account.org, owner, account.kind, ch=world.ch)
        for row in selected:
            set_query_tag("test.mcp-source-lifecycle.owner-after")
            key = connected_accounts._fence_key(row["connection_id"])
            stopped = await claims.get(world.ch, key)
            assert stopped is not None, "canonical owner erasure left source-only history unfenced"
            assert stopped != row["connection_id"]
            assert connected_accounts._fence_key(stopped) == key
            assert stopped.partition("/")[2].startswith("0")
            assert await world.source_versions(row) == [], (
                "canonical owner erasure returned with generic source-only history still stored"
            )
            await require_sources_erased(
                row["org"], row["namespace"], owner, row["connection_slot"], ch=world.ch
            )
            assert (
                await source_store.connected_history(
                    owner=(row["org"], owner, row["connection_slot"]), ch=world.ch
                )
                == []
            )
            assert await source_retirements.pending(world.retirement(row), ch=world.ch)
        # A repeated offboarding tail must neither revive work nor touch another owner's rows.
        await scim._revoke_tokens(
            {"subjects": subjects, "deleted": True}, previous_subjects=previous_subjects
        )
        assert await _neighbor_snapshot(world, neighbors, other_rows) == before
