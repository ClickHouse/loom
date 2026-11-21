"""Completed deletion clears raw capture PII before its durable done receipt."""

from __future__ import annotations

import asyncio
import copy
import json
from typing import Any

import pytest

from server.auth import person_erasure as erasure
from server.auth import scim
from server.clients._vendor import CredentialRefused
from server.core.errors import BackendUnavailableError
from server.core.secrets_at_rest import seal
from server.erasure.app import crawl_person
from server.erasure.store import crawl_controls
from server.knowledge.app import crawl_person_erasure
from server.settings import settings
from tests.unit.test_crawl_authority import crawl_world as crawl_world
from tests.unit.test_crawl_person_workspace import captured_world as captured_world
from tests.unit.test_crawl_signing_authority import key_view as key_view
from tests.unit.test_google_workspace import account as account

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def finalizing(captured_world, monkeypatch: pytest.MonkeyPatch):
    world, canonical = captured_world
    prepared = await erasure.prepare("scim-person-id", canonical)
    world.pending.update(
        id="scim-person-id", active=False, deleted=True, crawl_erasure_enc=prepared
    )
    world.history_caps = [
        {"id": "scim-person-id", "crawl_erasure_enc": canonical, "done": False},
        {"id": "scim-person-id", "crawl_erasure_enc": prepared, "done": False},
        {"id": "other-id", "crawl_erasure_enc": "other-owner-encrypted-metadata", "done": False},
    ]
    world.capsule_mutations = 0
    world.ignore_capsule_mutation = False
    world.lose_reply = False
    query_one, query_all = world.query_one, world.query_all

    async def one(sql: str, params: dict[str, Any] | None = None, **kwargs: Any):
        if "SELECT count() AS n FROM loom.scim_users" in sql:
            assert params == {"id": "scim-person-id"} and "FINAL" not in sql
            return {
                "n": sum(
                    r["id"] == params["id"] and (bool(r["crawl_erasure_enc"]) or not r["done"])
                    for r in world.history_caps
                )
            }
        return await query_one(sql, params, **kwargs)

    async def all_rows(sql: str, params: dict[str, Any], **kwargs: Any):
        if "FROM system.mutations" in sql:
            return [
                {"is_done": 1, "parts_to_do": 0, "latest_fail_reason": "", "mutation_id": "owned"}
            ]
        if sql.startswith("SELECT DISTINCT crawl_erasure_enc FROM loom.scim_users"):
            assert "FINAL" not in sql and params["id"] == "scim-person-id"
            return [
                {"crawl_erasure_enc": v}
                for v in sorted(
                    {
                        r["crawl_erasure_enc"]
                        for r in world.history_caps
                        if r["id"] == params["id"] and r["crawl_erasure_enc"]
                    }
                )
            ][: params["limit"]]
        return await query_all(sql, params, **kwargs)

    async def execute(sql: str, *, params: dict[str, Any]):
        assert "ALTER TABLE loom.scim_users" in sql and "UPDATE crawl_erasure_enc = ''" in sql
        assert "crawl_erasure_done = true" in sql and "crawl_erasure_required = true" in sql
        assert params["id"] == "scim-person-id" and "erasure_mutation_tag" in params
        world.capsule_mutations += 1
        if not world.ignore_capsule_mutation:
            for row in world.history_caps:
                if row["id"] == params["id"]:
                    row.update(crawl_erasure_enc="", done=True)
            world.pending.update(crawl_erasure_enc="", crawl_erasure_done=True)
        if world.lose_reply:
            raise RuntimeError("lost mutation reply")

    monkeypatch.setattr(world, "query_one", one)
    monkeypatch.setattr(world, "query_all", all_rows)
    monkeypatch.setattr(world, "execute", execute, raising=False)
    monkeypatch.setattr(crawl_controls, "_default_client", lambda: world)
    monkeypatch.setattr(erasure, "_finalizer", crawl_person.finalize)
    yield world, canonical, prepared


async def test_finalization_erases_canonical_and_provider_capsule_versions_but_not_another_id(
    finalizing,
):
    world, _canonical, prepared = finalizing
    await erasure.finalize("scim-person-id", prepared)
    assert world.capsule_mutations == 1
    assert all(r["crawl_erasure_enc"] == "" and r["done"] for r in world.history_caps[:2])
    assert world.history_caps[2] == {
        "id": "other-id",
        "crawl_erasure_enc": "other-owner-encrypted-metadata",
        "done": False,
    }
    assert world.pending["crawl_erasure_done"] and world.pending["crawl_erasure_enc"] == ""


async def test_lost_reply_after_atomic_redaction_is_completed_on_actual_scim_retry(finalizing):
    world, _canonical, prepared = finalizing
    world.lose_reply = True
    with pytest.raises(RuntimeError, match="lost mutation reply"):
        await erasure.finalize("scim-person-id", prepared)
    assert world.pending["crawl_erasure_done"] and world.pending["crawl_erasure_enc"] == ""
    before = len(world.provider_calls)
    await scim._finish_person_erasure(dict(world.pending))
    assert world.capsule_mutations == 1 and len(world.provider_calls) == before


async def test_done_mutation_status_does_not_override_nonzero_raw_capsule_residue(finalizing):
    world, _canonical, prepared = finalizing
    world.ignore_capsule_mutation = True
    with pytest.raises(RuntimeError, match="capture_history_remains"):
        await erasure.finalize("scim-person-id", prepared)
    assert (
        not world.pending["crawl_erasure_done"] and world.pending["crawl_erasure_enc"] == prepared
    )


async def test_uncovered_historical_canonical_person_is_not_silently_destroyed(finalizing):
    world, _canonical, prepared = finalizing
    foreign = seal(
        erasure.PURPOSE,
        json.dumps(
            {
                "format": 2,
                "id": "scim-person-id",
                "prepared": False,
                "workspaces": [],
                "people": [
                    {
                        "org": "other-org",
                        "subject": "another-person",
                        "spellings": ["another-person"],
                        "emails": ["other@example.com"],
                    }
                ],
            }
        ),
    )
    world.history_caps.append({"id": "scim-person-id", "crawl_erasure_enc": foreign, "done": False})
    with pytest.raises(CredentialRefused, match="verified repair"):
        await erasure.finalize("scim-person-id", prepared)
    assert world.capsule_mutations == 0 and not world.pending["crawl_erasure_done"]


async def test_replaced_current_capture_cannot_finalize_old_cleanup(finalizing):
    world, _canonical, prepared = finalizing
    world.pending["crawl_erasure_enc"] = "replacement"
    with pytest.raises(BackendUnavailableError):
        await erasure.finalize("scim-person-id", prepared)
    assert world.capsule_mutations == 0


async def test_missing_raw_current_capture_refuses_before_erasure_receipt(finalizing):
    world, canonical, prepared = finalizing
    world.history_caps = [{"id": "scim-person-id", "crawl_erasure_enc": canonical, "done": False}]
    with pytest.raises(CredentialRefused, match="history unavailable"):
        await erasure.finalize("scim-person-id", prepared)
    assert world.capsule_mutations == 0 and not world.pending["crawl_erasure_done"]


async def test_configured_cleanup_deadline_cancels_external_io_without_completing_capsule(
    finalizing,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world, _canonical, prepared = finalizing
    monkeypatch.setattr(settings, "knowledge_crawl_person_cleanup_timeout_s", 1.0)
    monkeypatch.setattr(erasure, "_cleanup", crawl_person_erasure.finish)
    monkeypatch.setattr(crawl_person_erasure, "get_client", lambda: world)
    original = world.query_one
    before = copy.deepcopy(world.exclusion_keys), world.source
    entered = False
    cancelled = False

    async def blocked(sql: str, params: dict[str, Any] | None = None, **kwargs: Any):
        nonlocal entered, cancelled
        if "FROM loom.knowledge_crawl_exclusion_keys FINAL" in sql:
            assert params == {"org": "team"}
            entered = True
            try:
                await asyncio.Event().wait()
            finally:
                cancelled = True
        return await original(sql, params, **kwargs)

    monkeypatch.setattr(world, "query_one", blocked)
    async with asyncio.timeout(3):
        with pytest.raises(TimeoutError):
            await erasure.finish("scim-person-id", prepared)
    assert entered and cancelled
    assert (world.exclusion_keys, world.source) == before
    assert world.pending["crawl_erasure_enc"] == prepared
    assert not world.pending["crawl_erasure_done"] and not world.capsule_mutations
