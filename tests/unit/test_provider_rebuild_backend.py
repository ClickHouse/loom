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
    text_column = {
        call.args[0].table: call.kwargs["text_column"]
        for call in effects["verify_reembedded_target"].await_args_list
    }
    assert text_column["memory_chunks"] == "content"
    assert text_column["conversation_embeddings"] == "content_excerpt"
    assert text_column["memory_embeddings"] is None
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


@pytest.mark.asyncio
async def test_a_raw_identity_hold_survives_a_worker_change_and_recovers(setup, monkeypatch):
    from dataclasses import replace

    from server.core.errors import ModelCallDeferred
    from server.insights.app import provider_rebuild_page as page
    from server.insights.app.provider_rebuild_target import RebuildEmbedder
    from server.insights.store import provider_verify as verifier
    from server.insights.store.provider_staging import completion_marker
    from server.insights.store.provider_target import target_marker
    from server.llm import customer
    from server.memory.app import model_attribution
    from server.settings import settings
    from tests.unit.clock_fake import ManualClock
    from tests.unit.switch_state import switch_off

    backend, manifest, effects, barrier, writes, current = setup
    clock = ManualClock(t=1000)
    monkeypatch.setattr(runner, "time", clock.time)
    monkeypatch.setattr(settings, "inference_identity_headers_json", '{"X-Subject":"{subject}"}')
    monkeypatch.setattr(settings, "inference_gateway_hosts", "gateway.example")
    monkeypatch.setattr(settings, "inference_customer_header", "")
    switch_off()
    source = dict(
        org="o",
        namespace="n",
        agent="a",
        user_id="u",
        session_id="s",
        memory_key="waiting",
        embedding_model="old",
        embedding_dim=2,
        embedding=[1.0, 0.0],
        memory_key_embedding_dim=2,
        memory_key_embedding=[1.0, 0.0],
        content_excerpt="a plain fact",
        tombstoned_at=None,
        version=1,
        _loom_rebuild_ordinal=1,
    )
    sources = [source, {**source, "memory_key": "healthy", "_loom_rebuild_ordinal": 2}]
    columns = tuple(k for k in source if k != "_loom_rebuild_ordinal")
    manifest = replace(
        manifest,
        tables=tuple(
            replace(p, columns=columns) if p.table == "memory_embeddings" else p
            for p in manifest.tables
        ),
    )
    index = REBUILD_TABLES.index("memory_embeddings")
    current.update(
        state="rebuilding",
        progress=json.dumps(
            dict(
                phase="rebuild",
                table_index=index,
                cursor=0,
                rows_done=40,
                manifest=manifest.to_dict(),
            )
        ),
    )
    staged, calls, writer_reads, verified = [], [], [], []
    deferred = True

    async def fetch(text):
        subject = customer.attribution().subject
        calls.append((subject, text))
        if subject == "waiting" and deferred:
            raise ModelCallDeferred("wait", hold_s=600, retry_after_s=600)
        return [1.0, 0.0]

    async def writers(org, keys, **kwargs):
        writer_reads.append(keys)
        return {
            ("n", key): [dict(agent="a", user_id="u", session_id="s", actor="human:" + key)]
            for _, key in keys
        }

    monkeypatch.setattr(model_attribution.memory_repo, "actors_by_key", writers)
    read = AsyncMock(return_value=sources)
    stage = AsyncMock(side_effect=lambda *args, **kwargs: staged.__setitem__(slice(None), args[5]))
    monkeypatch.setattr(page, "snapshot_page", read)
    monkeypatch.setattr(page, "write_target_page", stage)

    async def rebuild(*args, **kwargs):
        if args[0].table == "memory_embeddings":
            return await page.rebuild_snapshot_page(*args, **kwargs)
        return runner.BatchCheckpoint(0, 0, True)

    effects["rebuild_snapshot_page"].side_effect = rebuild
    plan = next(p for p in manifest.tables if p.table == "memory_embeddings")
    schema = storage._schema(plan)
    from server.insights.store.provider_staging import StagingNames

    names = StagingNames(manifest.job_id, plan.table)
    monkeypatch.setattr(
        verifier,
        "_owned_snapshot",
        AsyncMock(
            return_value={"uuid": plan.snapshot_uuid, "comment": completion_marker(names, schema)}
        ),
    )
    monkeypatch.setattr(
        verifier,
        "_target",
        AsyncMock(
            return_value={
                "uuid": plan.target_uuid,
                "engine": "ReplacingMergeTree",
                "comment": target_marker(names, plan.snapshot_uuid, manifest.embedding_fingerprint),
            }
        ),
    )
    monkeypatch.setattr(verifier, "verify_live_key_uniqueness", AsyncMock())

    class StagedRows:
        async def query_one(self, sql, params):
            # Evaluate the strict vector-loss predicate against the rows the real page staged.
            lost = sum(
                any(not t[c] and any(s[c]) for c in ("embedding", "memory_key_embedding"))
                for s, t in zip(sources, staged, strict=True)
            )
            return dict(
                source_rows=2,
                target_rows=len(staged),
                missing=0,
                metadata_errors=0,
                vector_errors=0,
                emptied=lost,
            )

    async def verify(*args, **kwargs):
        if args[0].table != "memory_embeddings":
            return 0
        count = await verifier.verify_reembedded_target(*args, **{**kwargs, "ch": StagedRows()})
        verified.append(count)
        return count

    effects["verify_reembedded_target"].side_effect = verify
    backend.target = RebuildEmbedder("target", 2, fetch)
    held = await runner.run_rebuild_step(current, backend)
    progress = json.loads(held["progress"])
    assert held["state"] == "rebuilding" and held["error_code"] == ""
    assert (
        progress["phase"],
        progress["table_index"],
        progress["cursor"],
        progress["rows_done"],
    ) == ("rebuild", index, 0, 40)
    assert progress["rows_waiting"] == 1 and progress["retry_at"] == 1600
    assert staged[0]["embedding"] == [] and staged[1]["embedding"] == [1.0, 0.0]
    assert writer_reads == [[("n", "healthy"), ("n", "waiting")]]
    paid, saved = list(calls), len(writes)
    fresh = storage.ClickHouseRebuildBackend(
        manifest.job_id,
        manifest.embedding_fingerprint,
        RebuildEmbedder("target", 2, fetch),
        prefix_dimensions=(),
        barrier=barrier,
    )
    clock.advance(599)
    assert await runner.run_rebuild_step(dict(held), fresh) == held
    assert calls == paid and len(writes) == saved and not verified
    assert read.await_count == stage.await_count == 1
    clock.advance(1)
    deferred = False
    current = held
    for _ in range(len(REBUILD_TABLES) + 8):
        current = await runner.run_rebuild_step(current, fresh)
        if current["state"] in ("active", "failed"):
            break
    assert current["state"] == "active" and current["error_code"] == ""
    final = json.loads(current["progress"])
    assert final["rows_done"] == 42 and final["rows_waiting"] == 0
    assert verified == [2]
    assert all(r["embedding"] == r["memory_key_embedding"] == [1.0, 0.0] for r in staged)
