"""Update reads a scratch participant census and waits for actual stored successors."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from server.cli import cell_control
from server.cli import cell_update_participants as update
from server.core import cell, instances, provider_process
from server.core.ch import CHClient, query_tag
from tests.unit.cell_control_owner import owner
from tests.unit.test_cell_attach import FakeConn


@pytest.mark.asyncio
async def test_postcutoff_census_waits_for_certified_old_row_and_serving_successor(monkeypatch):
    if not os.environ.get("LOOM_CH_HOST"):
        pytest.skip("Set LOOM_CH_HOST to the owned disposable ClickHouse")
    from server.settings import settings

    database = "update_" + uuid4().hex
    client = CHClient()
    await client.connect()
    with query_tag("test.cell_update.participants"):
        async with contextlib.AsyncExitStack() as cleanup:
            cleanup.push_async_callback(client.disconnect)
            try:
                await client.execute("CREATE DATABASE {db:Identifier}", {"db": database})
                cleanup.push_async_callback(
                    client.execute, "DROP DATABASE {db:Identifier} SYNC", {"db": database}
                )
                await client.execute(
                    "CREATE TABLE {db:Identifier}._provider_participants AS loom._provider_participants",
                    {"db": database},
                )
                for table in ("instances", "cell_heartbeats"):
                    await client.execute(
                        f"CREATE TABLE {{db:Identifier}}.{table} AS loom.{table} ENGINE=MergeTree ORDER BY instance_id",
                        {"db": database},
                    )
                monkeypatch.setattr(settings, "ch_database", database)
                monkeypatch.setattr(settings, "ch_cluster", "")
                old, new = uuid4(), uuid4()
                proof = provider_process.certified(provider_process.host_proof())
                await client.insert(
                    "loom._provider_participants",
                    [
                        {
                            "participant_id": old,
                            "state": "running",
                            "version": 1,
                            "host_proof": proof,
                        }
                    ],
                )
                await client.execute(
                    "ALTER TABLE loom._provider_participants UPDATE heartbeat_at=now64(3)-toIntervalHour(25) WHERE 1 SETTINGS mutations_sync=2"
                )
                initial = await update.census(client)
                assert set(update.capture(list(initial.values()))) == {str(old)}
                target = cell.as_value({"deployment_id": "target"})
                captured = asyncio.Event()
                actual_census = update.census
                reads = 0

                async def census(ch):
                    nonlocal reads
                    result = await actual_census(ch)
                    reads += 1
                    if reads == 1:
                        captured.set()
                    return result

                async def successor():
                    await captured.wait()
                    await client.insert(
                        "loom._provider_participants",
                        [
                            {
                                "participant_id": old,
                                "state": "retired",
                                "version": 2,
                                "host_proof": proof,
                            },
                            {
                                "participant_id": new,
                                "state": "running",
                                "version": 1,
                                "host_proof": proof,
                            },
                        ],
                    )
                    instance = instances.row() | {
                        "instance_id": "successor",
                        "serving": 1,
                        "stopped": 0,
                    }
                    await client.insert("loom.instances", [instance])
                    await client.insert(
                        "loom.cell_heartbeats",
                        [
                            {
                                "instance_id": "successor",
                                "cell": "first",
                                "tenancy_mode": "enforced",
                                "fingerprint": json.dumps(
                                    target
                                    | {
                                        "participant_id": str(new),
                                        "serves": True,
                                        "marked": True,
                                        "router": True,
                                    }
                                ),
                            }
                        ],
                    )

                monkeypatch.setattr(update, "census", census)
                monkeypatch.setattr(cell_control, "check_lock", AsyncMock())
                run = SimpleNamespace(
                    ch=client,
                    args=SimpleNamespace(cell="first", wait=5),
                    row={"is_first": True},
                    owner=owner(FakeConn()),
                )
                task = asyncio.create_task(successor())
                cleanup.push_async_callback(asyncio.gather, task, return_exceptions=True)
                cleanup.callback(task.cancel)
                await update.settle(run, target)
                await task
                assert reads >= 2
                assert len(await actual_census(client)) == 2
                # A workers-only heartbeat cannot replace the serving target.
                await client.execute("TRUNCATE TABLE loom.instances")
                await client.insert(
                    "loom.instances", [instances.row() | {"instance_id": "successor", "serving": 0}]
                )
                assert not await update.replacement(
                    client, run, await actual_census(client), {str(old)}, target
                )
            except BaseException:  # Owned cleanup must not replace setup or body failure.
                with contextlib.suppress(BaseException):
                    await cleanup.aclose()
                raise
