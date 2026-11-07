"""A user erasure's ingestion step on clickhouse-local: the user's rows go, and a
cancel the user asked for on another writer's open job still cancels it."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from server.erasure.store import steps_memory
from server.knowledge.store import ingestions
from tests.unit.clickhouse_local import CLICKHOUSE, ddl, run

pytestmark = pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")

_TABLES = (
    "knowledge_uploads",
    "knowledge_ingestions",
    "knowledge_ingest_cancels",
    "datasets",
    "dataset_rows",
)
OTHERS_OPEN, OTHERS_DONE, OWN_OPEN, AGENTS_OPEN = (
    f"00000000-0000-0000-0000-00000000000{n}" for n in (1, 2, 3, 4)
)
# a day ago, inside the table's 30-day TTL, and never now64(3)
ASKED = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S.123")


class _LocalCH:
    """A client over one clickhouse-local database kept in `path`."""

    def __init__(self, path) -> None:
        self.path = path
        run([ddl(t) for t in _TABLES], "SELECT 1", path=path)

    def write(self, table: str, rows: list[dict[str, Any]]) -> None:
        run(
            [
                f"INSERT INTO loom.{table} ({', '.join(rows[0])})"
                " SELECT * FROM format(JSONEachRow, {rows:String})"
            ],
            "SELECT 1",
            {"rows": "\n".join(json.dumps(r) for r in rows)},
            path=self.path,
        )

    async def query_one(self, sql, params=None):
        rows = await self.query_all(sql, params)
        return rows[0] if rows else None

    async def query_all(self, sql, params=None):
        if "system.mutations" in sql:  # execute ran each mutation to the end
            return [{"mutation_id": "m", "is_done": 1, "parts_to_do": 0, "latest_fail_reason": ""}]
        return run([], sql, params, path=self.path)

    async def execute(self, sql, params=None):
        run(
            [sql.replace("mutations_sync = 0", "mutations_sync = 2")],
            "SELECT 1",
            params,
            path=self.path,
        )


def _job(job_id: str, user_id: str, state: str) -> dict[str, Any]:
    return {
        "org": "o",
        "namespace": "ns",
        "ingestion_id": job_id,
        "source_uri": f"files/{job_id[-1]}.md",
        "upload_id": job_id,
        "sha256": "a" * 64,
        "state": state,
        "user_id": user_id,
        "version": 1,
    }


def _cancel(job_id: str) -> dict[str, Any]:
    return {
        "org": "o",
        "namespace": "ns",
        "ingestion_id": job_id,
        "agent": "bot",
        "user_id": "alice",
        "session_id": "s",
        "actor": "user:alice",
        "requested_at": ASKED,
    }


def test_a_cancel_on_another_writers_open_job_outlives_its_canceller(tmp_path) -> None:
    ch = _LocalCH(tmp_path)
    ch.write(
        "knowledge_ingestions",
        [_job(OTHERS_OPEN, "bob", "pending"), _job(OTHERS_DONE, "bob", "cancelled")]
        + [_job(OWN_OPEN, "alice", "processing"), _job(AGENTS_OPEN, "-", "processing")],
    )
    ch.write(
        "knowledge_ingest_cancels",
        [_cancel(j) for j in (OTHERS_OPEN, OTHERS_DONE, OWN_OPEN, AGENTS_OPEN)],
    )

    asyncio.run(steps_memory.delete_ingestions_by_user("o", "alice", ch=ch))

    def cancelled(job_id: str) -> bool:
        return asyncio.run(ingestions.cancel_requested("o", "ns", job_id, ch=ch))

    assert cancelled(OTHERS_OPEN) and cancelled(AGENTS_OPEN)
    assert not cancelled(OTHERS_DONE) and not cancelled(OWN_OPEN)
    left = run(
        [], "SELECT * FROM loom.knowledge_ingest_cancels ORDER BY ingestion_id", path=tmp_path
    )
    anonymous = {"org": "o", "namespace": "ns", "agent": "-", "user_id": "-", "session_id": "-"}
    assert left == [
        {**anonymous, "ingestion_id": job, "actor": "", "requested_at": ASKED}
        for job in (OTHERS_OPEN, AGENTS_OPEN)
    ]
    assert run(
        [],
        "SELECT ingestion_id FROM loom.knowledge_ingestions ORDER BY ingestion_id",
        path=tmp_path,
    ) == [
        {"ingestion_id": OTHERS_OPEN},
        {"ingestion_id": OTHERS_DONE},
        {"ingestion_id": AGENTS_OPEN},
    ]
