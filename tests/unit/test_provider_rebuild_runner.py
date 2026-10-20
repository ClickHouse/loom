"""Crash windows preserve checkpoints; no phase can jump straight to activation."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from server.insights.app import provider_rebuild_runner as runner
from server.insights.domain.provider_rebuild_manifest import RebuildManifest, RebuildTablePlan


def manifest(job_id, prepared=False):
    return RebuildManifest(
        job_id,
        "a" * 64,
        tuple(
            RebuildTablePlan(
                table,
                uuid4(),
                "MergeTree",
                ("content",),
                "b" * 64,
                uuid4(),
                uuid4(),
                uuid4() if prepared else None,
                0 if prepared else None,
            )
            for table in runner.REBUILD_TABLES
        ),
    )


@pytest.fixture
def backend(monkeypatch):
    backend = AsyncMock(spec=runner.RebuildBackend)
    backend.drain.return_value = True
    backend.rebuild.return_value = runner.BatchCheckpoint(0, 0, True)
    backend.snapshot.side_effect = lambda config_id: manifest(config_id)
    backend.verify.side_effect = lambda config_id, prior, held: replace(
        prior,
        tables=tuple(
            replace(table, prepared_uuid=uuid4(), verified_rows=0) for table in prior.tables
        ),
    )
    monkeypatch.setattr(runner, "write_configuration", AsyncMock())
    return backend


def row(phase="drain", state="rebuilding", **progress):
    config_id = uuid4()
    if phase in ("verify", "publish", "cleanup", "activate"):
        progress.setdefault("table_index", len(runner.REBUILD_TABLES))
    if phase not in ("drain", "snapshot"):
        progress.setdefault(
            "manifest", manifest(config_id, phase in ("publish", "cleanup", "activate")).to_dict()
        )
    return {
        "config_id": config_id,
        "parent_id": UUID(int=0),
        "state": state,
        "envelope": "encrypted-configuration",
        "progress": json.dumps({"phase": phase, **progress}),
        "error_code": "",
        "created_at": datetime.now(UTC),
        "version": 123,
    }


@pytest.mark.asyncio
async def test_complete_run_requires_all_phases_and_all_tables(backend):
    current = row(state="queued")
    original = dict(current)
    for _ in range(len(runner.REBUILD_TABLES) + 10):
        current = await runner.run_rebuild_step(current, backend)
        if current["state"] == "active":
            break
    assert current["state"] == "active"
    assert [call.args[1] for call in backend.rebuild.await_args_list] == list(runner.REBUILD_TABLES)
    calls = [call[0] for call in backend.mock_calls]
    assert (
        calls.index("snapshot")
        < calls.index("rebuild")
        < calls.index("verify")
        < calls.index("publish")
        < calls.index("cleanup")
    )
    assert current["envelope"] == original["envelope"]
    assert current["created_at"] == original["created_at"]
    assert set(current) == set(original)


@pytest.mark.asyncio
async def test_unacknowledged_drain_cannot_snapshot_or_advance(backend):
    backend.drain.return_value = False
    original = row()
    assert await runner.run_rebuild_step(original, backend) == original
    backend.snapshot.assert_not_awaited()
    runner.write_configuration.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["snapshot", "verify", "publish", "cleanup"])
async def test_backend_failure_retains_checkpoint_and_sanitizes_error(backend, phase):
    getattr(backend, phase).side_effect = RuntimeError("source text and API secret")
    original = row(phase, rows_done=47)
    failed = await runner.run_rebuild_step(original, backend)
    assert failed["state"] == "failed"
    assert failed["error_code"] == f"rebuild_{phase}_failed"
    assert json.loads(failed["progress"])["rows_done"] == 47
    assert "API secret" not in json.dumps(failed, default=str)
    assert original["state"] == "rebuilding"
    backend.reset_mock()
    assert await runner.run_rebuild_step(failed, backend) == failed
    backend.drain.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_lost_checkpoint_repeats_the_same_idempotent_page(backend):
    original = row("rebuild", cursor=20, rows_done=20)
    backend.rebuild.return_value = runner.BatchCheckpoint(30, 10, False)
    runner.write_configuration.side_effect = [RuntimeError("connection lost"), None]
    with pytest.raises(RuntimeError, match="connection lost"):
        await runner.run_rebuild_step(original, backend)
    updated = await runner.run_rebuild_step(original, backend)
    assert [call.args[2] for call in backend.rebuild.await_args_list] == [20, 20]
    progress = json.loads(updated["progress"])
    assert progress["cursor"] == progress["rows_done"] == 30
    assert runner.write_configuration.await_count == 2  # No ambiguous "failed" overwrite.


@pytest.mark.asyncio
async def test_rows_staged_without_a_vector_are_counted_in_the_progress(backend):
    backend.rebuild.return_value = runner.BatchCheckpoint(30, 10, False, rows_waiting=4)
    updated = await runner.run_rebuild_step(row("rebuild", cursor=20, rows_waiting=1), backend)
    assert json.loads(updated["progress"])["rows_waiting"] == 5


@pytest.mark.asyncio
async def test_held_rows_are_carried_per_table_to_verification(backend):
    table = runner.REBUILD_TABLES[0]
    backend.rebuild.return_value = runner.BatchCheckpoint(30, 10, False, held=(25, 21))
    updated = await runner.run_rebuild_step(
        row("rebuild", cursor=20, held={table: [3, 21]}), backend
    )
    held = json.loads(updated["progress"])["held"]
    assert held == {table: [3, 21, 25]}
    original = row("verify", held=held)
    await runner.run_rebuild_step(original, backend)
    assert backend.verify.await_args.args[2] == held


@pytest.mark.asyncio
@pytest.mark.parametrize("held", [[1], {"memory_chunks": [-1]}, {"memory_chunks": ["1"]}])
async def test_malformed_held_rows_fail_the_checkpoint(backend, held):
    with pytest.raises(ValueError, match="held"):
        runner._progress(row("rebuild", held=held))


@pytest.mark.asyncio
async def test_activation_write_failure_cannot_overwrite_a_possible_commit(backend):
    runner.write_configuration.side_effect = RuntimeError("response lost")
    with pytest.raises(RuntimeError):
        await runner.run_rebuild_step(row("activate"), backend)
    runner.write_configuration.assert_awaited_once()
    assert runner.write_configuration.await_args.args[0]["state"] == "active"


@pytest.mark.asyncio
async def test_cancellation_keeps_the_last_durable_checkpoint(backend):
    backend.snapshot.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await runner.run_rebuild_step(row("snapshot"), backend)
    runner.write_configuration.assert_not_awaited()


@pytest.mark.asyncio
async def test_environment_reset_preserves_its_target_through_activation(backend):
    updated = await runner.run_rebuild_step(
        row("activate", action="environment", environment="startup-settings"), backend
    )
    assert updated["state"] == "environment"
    assert json.loads(updated["progress"])["environment"] == "startup-settings"


@pytest.mark.asyncio
async def test_non_advancing_batch_fails_instead_of_looping_forever(backend):
    backend.rebuild.return_value = runner.BatchCheckpoint(10, 0, False)
    failed = await runner.run_rebuild_step(row("rebuild", cursor=10), backend)
    assert failed["state"] == "failed"
    assert json.loads(failed["progress"])["cursor"] == 10


@pytest.mark.asyncio
async def test_checkpoint_revision_advances_despite_clock_skew(backend, monkeypatch):
    monkeypatch.setattr(runner, "time_ns", lambda: 1)
    updated = await runner.run_rebuild_step(row(), backend)
    assert updated["version"] == 124


@pytest.mark.asyncio
async def test_incomplete_table_cursor_cannot_claim_activation(backend):
    with pytest.raises(ValueError, match="unfinished tables"):
        await runner.run_rebuild_step(row("activate", table_index=0), backend)
    backend.drain.assert_not_awaited()
    runner.write_configuration.assert_not_awaited()


@pytest.mark.asyncio
async def test_publish_receives_exact_durable_prepared_identities(backend):
    original = row("publish")
    saved = json.loads(original["progress"])["manifest"]
    updated = await runner.run_rebuild_step(original, backend)
    backend.publish.assert_awaited_once_with(
        original["config_id"], RebuildManifest.from_dict(saved)
    )
    assert json.loads(updated["progress"])["manifest"] == saved


@pytest.mark.asyncio
async def test_prepared_manifest_checkpoint_failure_never_publishes(backend):
    original = row("verify")
    runner.write_configuration.side_effect = RuntimeError("checkpoint response lost")
    with pytest.raises(RuntimeError):
        await runner.run_rebuild_step(original, backend)
    backend.publish.assert_not_awaited()
    written = runner.write_configuration.await_args.args[0]
    assert json.loads(written["progress"])["phase"] == "publish"
    RebuildManifest.from_dict(json.loads(written["progress"])["manifest"]).require_prepared()
    runner.write_configuration.assert_awaited_once()


@pytest.mark.asyncio
async def test_verification_cannot_replace_source_identities(backend):
    original = row("verify")
    backend.verify.side_effect = lambda config_id, prior, held: manifest(config_id, prepared=True)
    failed = await runner.run_rebuild_step(original, backend)
    assert failed["state"] == "failed"
    assert (
        json.loads(failed["progress"])["manifest"] == json.loads(original["progress"])["manifest"]
    )
    backend.publish.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["rebuild", "verify", "publish", "cleanup", "activate"])
async def test_recovery_without_a_manifest_never_performs_effects(backend, phase):
    original = row(phase, manifest=None)
    with pytest.raises(ValueError, match="manifest"):
        await runner.run_rebuild_step(original, backend)
    backend.drain.assert_not_awaited()
    runner.write_configuration.assert_not_awaited()
