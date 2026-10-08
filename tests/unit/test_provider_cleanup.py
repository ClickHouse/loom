"""Cleanup never treats publication intent or table names as ownership proof."""

from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from server.insights.domain.provider_rebuild import REBUILD_TABLES
from server.insights.domain.provider_rebuild_manifest import RebuildManifest, RebuildTablePlan
from server.insights.store.provider_cleanup import _identities, cleanup_rebuild_copies


@pytest.fixture
def setup():
    manifest = RebuildManifest(
        uuid4(),
        "a" * 64,
        tuple(
            RebuildTablePlan(
                table, uuid4(), "MergeTree", ("value",), "b" * 64, uuid4(), uuid4(), uuid4(), 1
            )
            for table in REBUILD_TABLES
        ),
    )
    live, copies = _identities(manifest)

    def rows(mapping):
        return [{"name": name, "uuid": identity} for name, identity in mapping.items()]

    client = AsyncMock()
    client.query_all.side_effect = [rows({**live, **copies}), rows(live)]
    client.query_one.return_value = {"running": 0}
    return manifest, client, live, copies, rows


@pytest.mark.asyncio
async def test_cleanup_drops_only_journaled_private_names(setup):
    manifest, client, live, copies, _ = setup
    assert await cleanup_rebuild_copies(manifest, ch=client) == len(copies)
    assert [call.args[0] for call in client.execute.await_args_list] == [
        f"DROP TABLE IF EXISTS `loom`.`{name}` SYNC" for name in copies
    ]
    assert not set(live).intersection(copies)


@pytest.mark.asyncio
async def test_already_clean_is_success_without_ddl(setup):
    manifest, client, live, _, rows = setup
    client.query_all.side_effect = [rows(live), rows(live)]
    assert await cleanup_rebuild_copies(manifest, ch=client) == 0
    client.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["live", "private", "unknown", "missing_live"])
async def test_identity_preflight_fails_before_any_drop(setup, kind):
    manifest, client, live, copies, rows = setup
    current = {**live, **copies}
    if kind == "live":
        current[next(iter(live))] = uuid4()
    elif kind == "private":
        current[next(iter(copies))] = uuid4()
    elif kind == "missing_live":
        del current[next(iter(live))]
    else:
        current[f"_provider_rebuild_{manifest.job_id.hex}_unexpected"] = uuid4()
    client.query_all.side_effect = [rows(current)]
    with pytest.raises(ValueError):
        await cleanup_rebuild_copies(manifest, ch=client)
    client.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [None, {}, {"running": True}, {"running": 1}, {"running": -1}])
@pytest.mark.parametrize("kind", ["query", "mutation"])
async def test_active_or_unknown_work_prevents_cleanup(setup, result, kind):
    manifest, client, *_ = setup
    client.query_one.side_effect = (
        [result, {"running": 0}] if kind == "query" else [{"running": 0}, result]
    )
    with pytest.raises(ValueError, match="running or its status"):
        await cleanup_rebuild_copies(manifest, ch=client)
    client.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_unprepared_plan_never_reads_or_deletes(setup):
    manifest, client, *_ = setup
    manifest = replace(manifest, tables=tuple(table.unprepared() for table in manifest.tables))
    with pytest.raises(ValueError, match="unprepared"):
        await cleanup_rebuild_copies(manifest, ch=client)
    client.query_all.assert_not_awaited()
    client.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_final_census_cannot_claim_success_with_remaining_copies(setup):
    manifest, client, live, copies, rows = setup
    client.query_all.side_effect = [rows({**live, **copies}), rows({**live, **copies})]
    with pytest.raises(ValueError, match="left private copies"):
        await cleanup_rebuild_copies(manifest, ch=client)
