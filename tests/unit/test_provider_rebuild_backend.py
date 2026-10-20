"""Concrete backend/runner sequencing with mocked database effects, not Docker E2E."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from server.insights.app import provider_rebuild_backend as storage
from server.insights.app import provider_rebuild_runner as runner
from server.insights.domain.provider_rebuild import REBUILD_TABLES
from server.insights.domain.provider_rebuild_manifest import RebuildManifest, RebuildTablePlan


@pytest.fixture
def setup(monkeypatch):
    job = uuid4()
    manifest = RebuildManifest(
        job,
        "a" * 64,
        tuple(
            RebuildTablePlan(
                table, uuid4(), "MergeTree", ("embedding",), "b" * 64, uuid4(), uuid4()
            )
            for table in REBUILD_TABLES
        ),
    )
    barrier = AsyncMock(return_value=True)
    backend = storage.ClickHouseRebuildBackend(
        job,
        "a" * 64,
        SimpleNamespace(model_name="new", dim=3),
        prefix_dimensions=(),
        barrier=barrier,
    )
    effects = {
        "snapshot_rebuild_manifest": AsyncMock(return_value=manifest),
        "rebuild_snapshot_page": AsyncMock(return_value=runner.BatchCheckpoint(0, 0, True)),
        "verify_reembedded_target": AsyncMock(return_value=2),
        "verify_staged_cache": AsyncMock(return_value=0),
        "verify_staged_prefixes": AsyncMock(return_value=0),
        "prepare_live_table": AsyncMock(side_effect=lambda *args, **kwargs: uuid4()),
        "publish_prepared_table": AsyncMock(),
        "cleanup_rebuild_copies": AsyncMock(),
    }
    for name, effect in effects.items():
        monkeypatch.setattr(storage, name, effect)
    writes = []
    monkeypatch.setattr(
        runner, "write_configuration", AsyncMock(side_effect=lambda row: writes.append(row))
    )
    row = {
        "config_id": job,
        "parent_id": UUID(int=0),
        "state": "queued",
        "envelope": "ciphertext",
        "progress": "{}",
        "error_code": "",
        "created_at": datetime.now(UTC),
        "version": 1,
    }
    return backend, manifest, effects, barrier, writes, row


@pytest.mark.asyncio
async def test_runner_connects_storage_actions_and_journals_before_publication(setup):
    backend, manifest, effects, barrier, writes, current = setup

    async def publish(names, original, prepared, **kwargs):
        checkpoint = json.loads(writes[-1]["progress"])
        assert checkpoint["phase"] == "publish"
        saved = RebuildManifest.from_dict(checkpoint["manifest"])
        saved.require_prepared()
        table = next(table for table in saved.tables if table.table == names.table)
        assert (original, prepared) == (table.source_uuid, table.prepared_uuid)

    effects["publish_prepared_table"].side_effect = publish
    for _ in range(len(REBUILD_TABLES) + 10):
        current = await runner.run_rebuild_step(current, backend)
        if current["state"] == "active":
            break
    assert current["state"] == "active"
    assert effects["rebuild_snapshot_page"].await_count == len(REBUILD_TABLES)
    assert effects["verify_reembedded_target"].await_count == len(REBUILD_TABLES) - 2
    # A raw-passage table is verified with its text column, so a credential row it staged
    # empty is accepted; every other table without one.
    passage_text = {
        call.args[0].table: call.kwargs["passage_text"]
        for call in effects["verify_reembedded_target"].await_args_list
    }
    assert passage_text["memory_chunks"] == "content"
    assert passage_text["conversation_embeddings"] == "content_excerpt"
    assert passage_text["memory_embeddings"] is None
    effects["verify_staged_cache"].assert_awaited_once()
    effects["verify_staged_prefixes"].assert_awaited_once()
    assert effects["prepare_live_table"].await_count == len(REBUILD_TABLES)
    assert effects["publish_prepared_table"].await_count == len(REBUILD_TABLES)
    effects["cleanup_rebuild_copies"].assert_awaited_once()
    assert barrier.await_count > len(REBUILD_TABLES)


@pytest.mark.asyncio
async def test_unacknowledged_barrier_prevents_snapshot_effects(setup):
    backend, _, effects, barrier, writes, current = setup
    current.update(state="rebuilding", progress=json.dumps({"phase": "snapshot"}))
    barrier.return_value = False
    assert await runner.run_rebuild_step(current, backend) == current
    effects["snapshot_rebuild_manifest"].assert_not_awaited()
    assert writes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("ack", [None, 1, "yes"])
async def test_truthy_non_boolean_is_not_a_barrier_ack(setup, ack):
    backend, manifest, _, barrier, *_ = setup
    barrier.return_value = ack
    with pytest.raises(ValueError, match="acknowledgement"):
        await backend.drain(manifest.job_id)


@pytest.mark.asyncio
async def test_backend_cannot_operate_on_another_job(setup):
    backend, manifest, effects, barrier, *_ = setup
    with pytest.raises(ValueError, match="does not own"):
        await backend.snapshot(uuid4())
    with pytest.raises(ValueError, match="does not own"):
        await backend.cleanup(uuid4(), manifest)
    barrier.assert_not_awaited()
    effects["snapshot_rebuild_manifest"].assert_not_awaited()
    effects["cleanup_rebuild_copies"].assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_cache_verification_never_publishes(setup):
    backend, manifest, effects, _, writes, current = setup
    current.update(
        state="validating",
        progress=json.dumps(
            {"phase": "verify", "table_index": len(REBUILD_TABLES), "manifest": manifest.to_dict()}
        ),
    )
    effects["verify_staged_cache"].side_effect = ValueError("not empty")
    failed = await runner.run_rebuild_step(current, backend)
    assert failed["state"] == "failed"
    assert failed["error_code"] == "rebuild_verify_failed"
    effects["publish_prepared_table"].assert_not_awaited()
    assert json.loads(writes[-1]["progress"])["manifest"] == manifest.to_dict()


@pytest.mark.asyncio
async def test_an_unread_switch_leaves_the_step_to_run_again_not_failed(setup):
    from server.llm import customer

    backend, manifest, effects, _, writes, current = setup
    current.update(
        state="validating",
        progress=json.dumps(
            {"phase": "verify", "table_index": len(REBUILD_TABLES), "manifest": manifest.to_dict()}
        ),
    )
    effects["verify_staged_cache"].side_effect = customer.SwitchUnread("stale")
    assert await runner.run_rebuild_step(current, backend) == current
    assert writes == []  # no checkpoint: the job stays where it was


@pytest.mark.asyncio
async def test_an_unread_switch_in_the_rebuild_phase_leaves_the_page_to_run_again(setup):
    from server.llm import customer

    backend, _, effects, _, writes, current = setup
    for _ in range(10):  # queued → snapshot → the first rebuild page
        if json.loads(current["progress"] or "{}").get("phase") == "rebuild":
            break
        current = await runner.run_rebuild_step(current, backend)
    assert json.loads(current["progress"])["phase"] == "rebuild"
    written = len(writes)
    effects["rebuild_snapshot_page"].side_effect = customer.SwitchUnread("stale")
    assert await runner.run_rebuild_step(current, backend) == current
    assert len(writes) == written  # no checkpoint: the page runs again next tick
    effects["rebuild_snapshot_page"].side_effect = None
    assert (await runner.run_rebuild_step(current, backend))["state"] != "failed"
