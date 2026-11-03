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


async def _saga_row(
    org: str, status: str, attempts: int, erasure_id: str | None = None
) -> dict[str, Any]:
    """Write one checkpoint and return the columns it wrote."""
    from server.erasure.api.erasure import ErasureRequestInput
    from server.erasure.app.saga import checkpoint, request_row_template

    request = ErasureRequestInput(
        org=org, scope_type="namespace", scope_value="ns-a", requester="dpo@y"
    )
    row = request_row_template(
        erasure_id=erasure_id or str(uuid.uuid4()),
        input=request,
        actor="human:admin@o",
        # The column is DateTime: whole seconds.
        requested_at=datetime.now(UTC).replace(tzinfo=None, microsecond=0),
        attempts=attempts,
    )
    failure_reason = "step broke" if status == "failed" else ""
    await checkpoint(
        row=row,
        status=status,
        steps_completed=["memory.tombstone"],
        current_step=1,
        failure_reason=failure_reason,
    )
    return {**row, "steps_completed": ["memory.tombstone"], "failure_reason": failure_reason}


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
    # Both sagas end completed, so no recovery worker resumes or abandons them.
    for written in (stuck, exhausted):
        await _saga_row(org, "completed", attempts=1, erasure_id=str(written["id"]))
    for name, rows in reads.items():
        written = wanted[name]
        (row,) = [r for r in rows if str(r["id"]) == written["id"]]
        # Every column the read returns, compared with what the checkpoint wrote.
        got = {k: (str(v) if k == "id" else v) for k, v in row.items() if k in written}
        assert got == {k: written[k] for k in got}, name
        assert {"actor", "requester", "purge", "attempts", "steps_completed"} <= got.keys(), name
