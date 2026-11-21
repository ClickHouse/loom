"""Restore and recovery preserve a mirror receipt's privacy in both ledgers."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tests.integration._resources import unique_org
from tests.integration.test_erasure_ledger_actor_ch import ch as ch


@pytest.mark.destructive
async def test_promoting_privacy_changes_no_receipt_or_restore_fence(ch: Any) -> None:
    from server.erasure.api.erasure import ErasureRequestInput
    from server.erasure.app.saga import checkpoint, request_row_template
    from server.erasure.domain.failure_reason import REPLAY_PENDING_REASON
    from server.erasure.store import ledger

    org, erasure_id = unique_org("mirror-receipt"), str(uuid.uuid4())
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    row = request_row_template(
        erasure_id=erasure_id,
        input=ErasureRequestInput(
            org=org, scope_type="namespace", scope_value="team", requester="ops"
        ),
        actor="human:admin",
        requested_at=now,
        attempts=3,
    )
    await checkpoint(
        row=row,
        status="failed",
        steps_completed=["memory.tombstone"],
        current_step=1,
        failure_reason=REPLAY_PENDING_REASON,
        extra={
            "residue_count": 47,
            "residue": '{"loom.documents":47}',
            "census": '{"loom.documents":47}',
        },
    )
    sql = "SELECT * FROM loom.erasure_requests FINAL WHERE org = {org:String} AND id = {id:UUID}"
    params = {"org": org, "id": erasure_id}
    before = await ch.query_one(sql, params=params)
    await ledger.promote_mirror_mark(org + "-other", erasure_id, ch=ch)
    assert await ch.query_one(sql, params=params) == before
    await ledger.promote_mirror_mark(org, erasure_id, ch=ch)
    after = await ch.query_one(sql, params=params)
    assert after["mirror_marked"] and after["version"] > before["version"]
    assert {k: v for k, v in after.items() if k not in {"mirror_marked", "version"}} == {
        k: v for k, v in before.items() if k not in {"mirror_marked", "version"}
    }
    await ledger.promote_mirror_mark(org, erasure_id, ch=ch)
    assert await ch.query_one(sql, params=params) == after
    # A stale recovery snapshot must not clear privacy, even after physical merges.
    await checkpoint(row=row, status="completed", steps_completed=[], current_step=99)
    assert await ch.query_one(sql, params=params) == after
    await ch.execute("OPTIMIZE TABLE loom.erasure_requests FINAL")
    assert await ch.query_one(sql, params=params) == after
    # Privacy survives even when this checkpoint is too recent to trust for deletes.
    assert (
        await ledger.trusted_row(
            org, erasure_id, before=now - timedelta(seconds=1), actor="other", ch=ch
        )
        is None
    )
    assert await ledger.mirror_mark(org, erasure_id, ch=ch)
    assert not await ledger.mirror_mark(org + "-other", erasure_id, ch=ch)


async def test_postgres_promotions_never_clear_privacy_or_replace_a_terminal_state() -> None:
    os.environ.get("LOOM_PG_DSN") or pytest.skip("LOOM_PG_DSN not set")
    from server.core import pg
    from server.erasure.store import pg_ledger

    org, erasure_id = unique_org("pg-mirror-receipt"), uuid.uuid4()
    now = datetime.now(UTC).replace(microsecond=0)
    row = {
        "org": org,
        "id": erasure_id,
        "scope_type": "namespace",
        "scope_value": "team",
        "purge": True,
        "requester": "ops",
        "urgency": "normal",
        "requested_at": now,
        "mirror_marked": False,
    }
    try:
        await pg_ledger.record(row)
        await pg_ledger.complete(org, str(erasure_id), now)
        sql = "SELECT * FROM loom.erasure_ledger WHERE org = $1 AND id = $2"
        before = dict(await pg.fetchrow(sql, org, erasure_id))
        await pg_ledger.record({**row, "mirror_marked": True, "scope_value": "other"})
        await pg_ledger.record(row)  # an older writer never clears the mark
        await pg_ledger._upsert(
            [
                {
                    **row,
                    "completed_at": None,
                    "abandoned_at": now + timedelta(seconds=1),
                }
            ]
        )
        after = dict(await pg.fetchrow(sql, org, erasure_id))
        assert after == {**before, "mirror_marked": True}
        # A copy from ClickHouse also promotes a row already terminal in Postgres.
        second = {**row, "id": uuid.uuid4()}
        await pg_ledger.record(second)
        await pg_ledger.abandon(org, str(second["id"]), now)
        await pg_ledger._upsert(
            [
                {
                    **second,
                    "mirror_marked": True,
                    "completed_at": now + timedelta(seconds=1),
                    "abandoned_at": None,
                }
            ]
        )
        copied = dict(await pg.fetchrow(sql, org, second["id"]))
        assert copied["mirror_marked"] and copied["abandoned_at"] == now
        assert copied["completed_at"] is None
    finally:
        await pg.execute("DELETE FROM loom.erasure_ledger WHERE org = $1", org)
        await pg.close_pool()
