"""The erasure ledger keeps the request's audit actor: a checkpoint writes it and
every read that feeds a resume (the recovery worker's and the CLI's) returns it,
so a resumed saga names the principal that asked."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from tests.integration._resources import unique_org


@pytest.fixture
async def ch() -> Any:
    from server.core.ch import get_client

    c = get_client()
    await c.connect()
    yield c
    await c.disconnect()


async def _saga_row(org: str, status: str, attempts: int) -> str:
    from server.erasure.api.erasure import ErasureRequestInput
    from server.erasure.app.saga import checkpoint, request_row_template

    request = ErasureRequestInput(
        org=org, scope_type="namespace", scope_value="ns-a", requester="dpo@y"
    )
    erasure_id = str(uuid.uuid4())
    await checkpoint(
        row=request_row_template(
            erasure_id=erasure_id,
            input=request,
            actor="human:admin@o",
            requested_at=datetime.now(UTC).replace(tzinfo=None),
            attempts=attempts,
        ),
        status=status,
        steps_completed=[],
        current_step=0,
        failure_reason="step broke" if status == "failed" else "",
    )
    return erasure_id


@pytest.mark.asyncio
async def test_every_resume_read_returns_the_actor(ch: Any) -> None:
    from server.erasure.store import ledger

    org = unique_org("erasure-actor")
    stuck = await _saga_row(org, "in_progress", attempts=1)
    exhausted = await _saga_row(org, "failed", attempts=9)
    await asyncio.sleep(1.1)  # past a zero-second stale threshold

    reads = {
        "stuck": await ledger.list_stuck_in_progress(
            stale_threshold_seconds=0, max_attempts=5, batch_size=100_000, ch=ch
        ),
        "exhausted": await ledger.list_unabandoned_exhausted(
            stale_threshold_seconds=0, max_attempts=5, batch_size=100_000, ch=ch
        ),
        "failed": await ledger.list_failed_requests(org=org, ch=ch),
    }
    wanted = {"stuck": stuck, "exhausted": exhausted, "failed": exhausted}
    for name, rows in reads.items():
        (row,) = [r for r in rows if str(r["id"]) == wanted[name]]
        assert (row["actor"], row["requester"]) == ("human:admin@o", "dpo@y"), name
