"""`loom erasure-re-verify --completed-since` against ClickHouse: a saga a previous
build completed without erasing a table this build added (rolling-upgrades rule 14)
is counted again by this build's census and finished."""

from __future__ import annotations

import argparse
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tests.integration._resources import unique_org

NS = "re-verify"


@pytest.fixture
async def ch() -> Any:
    from server.core.ch import get_client

    c = get_client()
    await c.connect()
    yield c
    await c.disconnect()


async def _triple_rows(ch: Any, org: str) -> int:
    row = await ch.query_one(
        "SELECT count() AS n FROM loom.entity_triple_rows WHERE org = {org:String}",
        params={"org": org},
    )
    return int(row["n"])


async def test_a_completed_saga_with_a_left_row_is_finished(ch: Any) -> None:
    from server.cli import hygiene
    from server.erasure.api.erasure import ErasureRequestInput
    from server.erasure.app.saga import checkpoint, request_row_template
    from server.erasure.app.saga_steps import NAMESPACE_STEPS

    org = unique_org("re-verify")
    # The row an older build's saga did not know to delete.
    await ch.insert(
        "loom.entity_triple_rows",
        [
            {
                "org": org,
                "namespace": NS,
                "memory_id": str(uuid.uuid4()),
                "user_id": "u1",
                "content_sha256": "0" * 64,
                "subject": "user",
                "relation": "live_in",
                "object": "rome",
                "inferred": False,
            }
        ],
    )
    # Its saga, completed with every step checkpointed and a clean census.
    now = datetime.now(UTC).replace(tzinfo=None)
    request = ErasureRequestInput(
        org=org,
        scope_type="namespace_list",
        scope_value=json.dumps({"namespaces": [NS], "purge": False, "external": False}),
        requester="itest",
        external=False,
    )
    erasure_id = str(uuid.uuid4())
    done = [name for name, _ in NAMESPACE_STEPS] + ["verify.residue"]
    await checkpoint(
        row=request_row_template(
            erasure_id=erasure_id, input=request, requested_at=now, attempts=1
        ),
        status="completed",
        steps_completed=done,
        current_step=len(done),
        completed_at=now,
        extra={"residue_count": 0, "residue": "{}", "verified_at": now},
    )
    assert await _triple_rows(ch, org) == 1

    args = argparse.Namespace(
        org=org,
        id=[erasure_id],
        resume=False,
        skip_external=False,
        orphan_graph=False,
        erase=False,
        batch=200,
        show=20,
        requester="itest",
        completed_since=now - timedelta(minutes=1),
    )
    # Read-only: the census finds the row, and nothing is erased.
    assert await hygiene._re_verify_run(ch, args) == 1
    assert await _triple_rows(ch, org) == 1

    args.resume = True
    assert await hygiene._re_verify_run(ch, args) == 0
    assert await _triple_rows(ch, org) == 0

    # A saga verified before the cut is not taken.
    args.completed_since = now + timedelta(minutes=1)
    assert await hygiene._re_verify_run(ch, args) == 0
