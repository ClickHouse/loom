"""Actual reseal writers preserve stable refusal keys and unfinished cleanup manifests."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from server.clients._vendor import VendorError
from server.core.secrets_at_rest import unseal
from server.knowledge.app import crawl_exclusions as exclusions
from server.knowledge.app import crawl_reseal
from server.knowledge.store import crawl_exclusions as store
from server.settings import settings
from tests.unit.test_crawl_exclusions import _PERSON
from tests.unit.test_crawl_exclusions import _World as _ExclusionWorld

pytestmark = pytest.mark.asyncio


class _World(_ExclusionWorld):
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        super().__init__(monkeypatch)
        monkeypatch.setattr(crawl_reseal, "get_client", lambda: self)
        monkeypatch.setattr(settings, "knowledge_crawl_lease_wait_s", 0)
        monkeypatch.setattr(settings, "knowledge_crawl_registry_page_size", 1)

    async def query_all(self, sql: str, *, params: dict[str, Any]):
        if sql.startswith(f"SELECT org FROM {store.KEYS} FINAL"):
            assert params["limit"] == 1
            return [{"org": org} for org in sorted(self.keys) if org > params["after"]][:1]
        return await super().query_all(sql, params=params)


def _rotate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "jwt_secret_previous", settings.jwt_secret)
    monkeypatch.setattr(settings, "jwt_secret", "second-root-sealing-secret-which-is-long-enough")


async def test_rotation_preserves_the_same_key_and_pending_blocked_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World(monkeypatch)
    index = await world.start()
    digests = index.person_digests(_PERSON)
    world.cleanup_fails = True
    with pytest.raises(RuntimeError, match="cleanup incomplete"):
        await world.change(index)
    before, fence = copy.deepcopy(world.keys["org"]), dict(world.keeper)
    _rotate(monkeypatch)
    assert await crawl_reseal.keys(dry_run=False, ch=world) == (1, [])
    after = world.keys["org"]
    assert after["key_enc"] != before["key_enc"] and after["version"] > before["version"]
    for field in ("key_id", "generation", "pending_id", "pending_digests", "pending_exclude"):
        assert after[field] == before[field]
    assert world.keeper == fence and after["pending_id"]
    assert exclusions._open_key(after) == exclusions._open_key(before)
    assert index.person_digests(_PERSON) == digests
    with pytest.raises(VendorError, match="cleanup incomplete"):
        await exclusions.Index.open("org", ch=world)
    monkeypatch.setattr(settings, "jwt_secret_previous", "")
    assert unseal(exclusions.KEY_PURPOSE, after["key_enc"], warn=False)


async def test_dry_run_does_not_rewrite_key_or_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _World(monkeypatch)
    await world.start()
    before, fence = copy.deepcopy(world.keys), dict(world.keeper)
    _rotate(monkeypatch)
    assert await crawl_reseal.keys(dry_run=True, ch=world) == (1, [])
    assert world.keys == before and world.keeper == fence


async def test_lost_key_is_reported_without_reinitializing_refusals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World(monkeypatch)
    index = await world.start()
    await world.change(index)
    world.keys["org"]["key_enc"] = "unreadable-sealed-value"
    before, rows, fence = copy.deepcopy(world.keys), dict(world.rows), dict(world.keeper)
    _rotate(monkeypatch)
    assert await crawl_reseal.keys(dry_run=False, ch=world) == (0, ["org"])
    assert world.keys == before and world.rows == rows and world.keeper == fence


async def test_reseal_refuses_a_different_database_client_before_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World(monkeypatch)
    with pytest.raises(RuntimeError, match="target_mismatch"):
        await crawl_reseal.keys(dry_run=False, ch=object())
    assert world.queries == 0 and not world.keys
