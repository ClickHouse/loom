"""Actual exclusion rows, sealing, CAS fencing and crash repair with external I/O fakes."""

from __future__ import annotations

import copy
import json
from contextlib import asynccontextmanager
from typing import Any

import pytest

from server.auth import claims
from server.clients._vendor import VendorError
from server.core import leases
from server.core.secrets_at_rest import resealed
from server.knowledge.app import crawl_exclusions as exclusions
from server.knowledge.domain.source_crawl import Person
from server.knowledge.store import crawl_exclusions as store
from server.settings import settings

pytestmark = pytest.mark.asyncio
_PERSON = Person("workspace", "provider-user", "person@example.com", ("alias@example.com",))


class _World:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.keys: dict[str, dict[str, Any]] = {}
        self.rows: dict[tuple[str, str], bool] = {}
        self.keeper: dict[str, str] = {}
        self.held = False
        self.authorized = True
        self.cleanup_fails = False
        self.cleaned = 0
        self.queries = 0
        self.fail_key_write = False
        self.version = 0
        monkeypatch.setattr(
            settings, "jwt_secret", "first-root-sealing-secret-which-is-long-enough"
        )
        monkeypatch.setattr(settings, "jwt_secret_previous", "")
        monkeypatch.setattr(claims, "get", self.claim_get)
        monkeypatch.setattr(claims, "swap", self.claim_swap)
        monkeypatch.setattr(leases, "cluster_lease", self.lease)

    async def claim_get(self, ch: Any, key: str) -> str | None:
        assert ch is self and key.startswith("crawl-exclusions:")
        return self.keeper.get(key)

    async def claim_swap(self, ch: Any, key: str, old: str | None, new: str, expiry: int):
        assert ch is self and self.held and expiry == 2**32 - 1
        if self.keeper.get(key) == old:
            self.keeper[key] = new
        return self.keeper.get(key)

    @asynccontextmanager
    async def lease(self, name: str, *, ttl_s: float, wait_s: float, ch: Any):
        assert ch is self and name.startswith("crawl-exclusions:") and not self.held
        assert (ttl_s, wait_s) == (60, 0)
        self.held = True
        try:
            yield
        finally:
            self.held = False

    async def query_one(self, sql: str, *, params: dict[str, Any]):
        self.queries += 1
        if sql.startswith("SELECT greatest("):
            assert self.held and (f"FROM {store.KEYS} " in sql or f"FROM {store.REFUSALS} " in sql)
            self.version += 1
            return {"v": self.version}
        assert set(params) == {"org"}
        if f"FROM {store.KEYS} FINAL" in sql:
            return copy.deepcopy(self.keys.get(params["org"]))
        assert f"FROM {store.REFUSALS} FINAL" in sql and "LIMIT 1" in sql
        return next(({"digest": d} for org, d in self.rows if org == params["org"]), None)

    async def query_all(self, sql: str, *, params: dict[str, Any]):
        self.queries += 1
        if sql.startswith("SELECT digest, excluded FROM loom.knowledge_crawl_exclusions FINAL"):
            assert params["limit"] == len(set(params["digests"]))
            return [
                {"digest": d, "excluded": self.rows[params["org"], d]}
                for d in params["digests"]
                if (params["org"], d) in self.rows
            ]
        assert f"FROM {store.REFUSALS} FINAL" in sql and "AND excluded = 1" in sql
        assert params["limit"] == len(params["digests"])
        return [{"digest": d} for d in params["digests"] if self.rows.get((params["org"], d))]

    async def insert(self, table: str, rows: list[dict[str, Any]]) -> None:
        assert self.held
        for row in rows:
            if table == store.KEYS:
                assert set(row) == {
                    "org",
                    "key_id",
                    "key_enc",
                    "generation",
                    "pending_id",
                    "pending_digests",
                    "pending_exclude",
                    "version",
                }
                self.keys[row["org"]] = copy.deepcopy(row)
                if self.fail_key_write:
                    self.fail_key_write = False
                    raise RuntimeError("key write reply lost")
            else:
                assert table == store.REFUSALS and set(row) == {
                    "org",
                    "digest",
                    "excluded",
                    "version",
                }
                self.rows[row["org"], row["digest"]] = row["excluded"]

    async def validate(self, org: str) -> None:
        assert org in {"org", "other-org"}
        if not self.authorized:
            raise PermissionError("org authority ended")

    async def cleanup(self) -> None:
        assert self.held
        assert any(v.startswith("blocked:") for v in self.keeper.values())
        if self.cleanup_fails:
            raise RuntimeError("local cleanup incomplete")
        self.cleaned += 1

    async def start(self, org: str = "org") -> exclusions.Index:
        return await exclusions.initialize(org, authority=self, ttl_s=60, wait_s=0, ch=self)

    async def change(self, index: exclusions.Index, *, exclude: bool = True) -> exclusions.Index:
        return await index.change(
            (_PERSON,),
            exclude=exclude,
            ttl_s=60,
            wait_s=0,
            authority=self,
            cleanup=self.cleanup,
        )


async def test_exclusion_persists_only_keyed_refusal_and_stable_provider_and_alias_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World(monkeypatch)
    old = await world.start()
    current = await world.change(old)
    assert await current.contains(_PERSON)
    assert await current.contains(
        Person("workspace", "provider-user", "new-address@example.com", ())
    )
    assert await current.contains(Person("other-workspace", "different-user", _PERSON.email, ()))
    assert await current.excluded_addresses((_PERSON.email, "other@example.com")) == {_PERSON.email}
    assert world.cleaned == 1 and not world.keys["org"]["pending_id"]
    encoded = json.dumps([world.keys, list(world.rows), world.keeper])
    assert all(
        value not in encoded
        for value in (_PERSON.email, _PERSON.id, _PERSON.customer, _PERSON.aliases[0])
    )
    assert all(len(digest) == 64 for org, digest in world.rows if org == "org")
    with pytest.raises(VendorError, match="changed"):
        await old.contains(_PERSON)


async def test_cleanup_failure_blocks_old_and_new_readers_and_restart_repairs_real_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World(monkeypatch)
    index = await world.start()
    world.cleanup_fails = True
    with pytest.raises(RuntimeError, match="cleanup incomplete"):
        await world.change(index)
    assert world.keys["org"]["pending_id"] and all(world.rows.values())
    assert next(iter(world.keeper.values())).startswith("blocked:")
    with pytest.raises(VendorError):
        await index.contains(_PERSON)
    with pytest.raises(VendorError):
        await exclusions.Index.open("org", ch=world)
    world.cleanup_fails = False
    repaired = await exclusions.repair(
        "org",
        ttl_s=60,
        wait_s=0,
        authority=world,
        cleanup=world.cleanup,
        ch=world,
    )
    assert await repaired.contains(_PERSON) and world.cleaned == 1
    assert world.keys["org"]["pending_id"] == ""


async def test_rescan_cannot_readmit_but_explicit_checked_readmission_changes_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World(monkeypatch)
    current = await world.change(await world.start())
    rescanned = await exclusions.Index.open("org", ch=world)
    assert await rescanned.contains(_PERSON)
    prior_version = await current.version()
    readmitted = await world.change(current, exclude=False)
    assert not await readmitted.contains(_PERSON)
    assert await readmitted.version() != prior_version
    assert world.cleaned == 2
    with pytest.raises(VendorError):
        await rescanned.version()


async def test_same_address_in_other_org_does_not_inherit_person_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World(monkeypatch)
    current = await world.change(await world.start())
    other = await world.start("other-org")
    assert await current.contains(_PERSON) and not await other.contains(_PERSON)
    assert set(current.person_digests(_PERSON)).isdisjoint(other.person_digests(_PERSON))


async def test_root_reseal_preserves_stable_exclusion_key_and_refusal_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World(monkeypatch)
    current = await world.change(await world.start())
    before = current.person_digests(_PERSON)
    previous = str(settings.jwt_secret)
    monkeypatch.setattr(settings, "jwt_secret_previous", previous)
    monkeypatch.setattr(settings, "jwt_secret", "second-root-sealing-secret-which-is-long-enough")
    ciphertext = resealed(exclusions.KEY_PURPOSE, world.keys["org"]["key_enc"])
    assert ciphertext is not None
    world.keys["org"]["key_enc"] = (
        ciphertext  # External maintenance I/O; actual seal/open remain real.
    )
    monkeypatch.setattr(settings, "jwt_secret_previous", "")
    reopened = await exclusions.Index.open("org", ch=world)
    assert reopened.person_digests(_PERSON) == before and await reopened.contains(_PERSON)


async def test_unreadable_or_lost_key_never_creates_a_new_key_over_existing_refusals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World(monkeypatch)
    await world.change(await world.start())
    world.keys.pop("org")
    before = copy.deepcopy(world.rows)
    with pytest.raises(VendorError, match="key unavailable"):
        await world.start()
    assert world.rows == before and "org" not in world.keys


async def test_authority_loss_before_mutation_writes_no_exclusion_or_new_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World(monkeypatch)
    index = await world.start()
    before = copy.deepcopy((world.keys, world.rows, world.keeper))
    world.authorized = False
    with pytest.raises(PermissionError):
        await world.change(index)
    assert (world.keys, world.rows, world.keeper) == before and world.cleaned == 0


async def test_lost_initial_key_write_reply_reuses_the_persisted_key_on_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World(monkeypatch)
    world.fail_key_write = True
    with pytest.raises(RuntimeError, match="reply lost"):
        await world.start()
    key_id = world.keys["org"]["key_id"]
    assert next(iter(world.keeper.values())).startswith("initializing:")
    current = await world.start()
    assert world.keys["org"]["key_id"] == key_id
    assert not await current.contains(_PERSON) and not world.rows


async def test_late_old_manifest_cannot_overwrite_a_newer_explicit_readmission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World(monkeypatch)
    index = await world.start()
    saved: dict[str, Any] = {}
    actual_cleanup = world.cleanup

    async def record_cleanup() -> None:
        saved.update(copy.deepcopy(world.keys["org"]))
        await actual_cleanup()

    current = await index.change(
        (_PERSON,),
        exclude=True,
        ttl_s=60,
        wait_s=0,
        authority=world,
        cleanup=record_cleanup,
    )
    current = await world.change(current, exclude=False)
    assert not await current.contains(_PERSON)
    before = copy.deepcopy((world.rows, world.keeper))
    world.keys["org"] = saved  # An old ClickHouse write is visible after the newer mutation.
    with pytest.raises(VendorError, match="repair unavailable"):
        await exclusions.repair(
            "org",
            ttl_s=60,
            wait_s=0,
            authority=world,
            cleanup=world.cleanup,
            ch=world,
        )
    assert (world.rows, world.keeper) == before and world.cleaned == 2
    with pytest.raises(VendorError):
        await current.version()


async def test_lost_refusal_write_cannot_release_the_completed_cleanup_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.core.errors import BackendUnavailableError

    world = _World(monkeypatch)
    index = await world.start()

    async def cleanup() -> None:
        await world.cleanup()
        # The external store lost the just-written refusal while cleanup completed.
        world.rows.clear()

    with pytest.raises(BackendUnavailableError):
        await index.change(
            (_PERSON,), exclude=True, ttl_s=60, wait_s=0, authority=world, cleanup=cleanup
        )
    assert world.cleaned == 1 and world.keys["org"]["pending_id"]
    assert next(iter(world.keeper.values())).startswith("blocked:")
    with pytest.raises(VendorError):
        await exclusions.Index.open("org", ch=world)


@pytest.mark.parametrize("generation", ["active", "blocked", "initializing"])
async def test_missing_registry_cannot_replace_a_surviving_keeper_generation(
    monkeypatch: pytest.MonkeyPatch, generation: str
) -> None:
    world = _World(monkeypatch)
    fence = exclusions._fence("org")
    world.keeper[fence] = generation + ":retained-key:retained-generation"
    before = copy.deepcopy((world.keys, world.rows, world.keeper, world.version))
    with pytest.raises(VendorError, match="key unavailable"):
        await world.start()
    assert (world.keys, world.rows, world.keeper, world.version) == before
    assert not world.cleaned and not world.held
