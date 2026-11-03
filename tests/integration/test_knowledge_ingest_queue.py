"""The ingestion queue on a real ClickHouse: one namespace's backlog takes turns
with another's. Each namespace offers its oldest due job, and the one whose last
job started longest ago (or never) comes first."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tests.integration._resources import unique_org

pytestmark = pytest.mark.usefixtures("ch_target_required")


@pytest.mark.asyncio
async def test_a_backlog_takes_turns_with_another_namespaces_job(ch_client: Any) -> None:
    from server.core.ch import get_client
    from server.knowledge.store import ingestions

    busy, quiet = unique_org("ingest-busy"), unique_org("ingest-quiet")
    older = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=10)

    def job(org: str, state: str, at: datetime) -> dict[str, Any]:
        return ingestions.ingestion_row(
            org=org,
            namespace="docs",
            ingestion_id=str(uuid.uuid4()),
            source_uri="repo://a",
            upload_id=str(uuid.uuid4()),
            sha256="a" * 64,
            state=state,
            next_attempt_at=at,
        )

    backlog = [job(busy, "pending", older + timedelta(seconds=i)) for i in range(3)]
    rows = [
        *backlog,
        job(busy, "ready", older),
        job(quiet, "pending", older + timedelta(minutes=5)),
    ]
    ch = get_client()
    await ch.connect()
    try:
        for row in rows:
            await ingestions.insert_ingestion(row)
        due = [
            d for d in await ingestions.due(1000, stale_after_s=120) if d["org"] in (busy, quiet)
        ]
    finally:
        await ch.disconnect()
    # The busy namespace just ran a job, so the quiet one's later job goes first,
    # and the backlog offers only its oldest.
    assert [(d["org"], d["ingestion_id"]) for d in due] == [
        (quiet, rows[-1]["ingestion_id"]),
        (busy, backlog[0]["ingestion_id"]),
    ]
