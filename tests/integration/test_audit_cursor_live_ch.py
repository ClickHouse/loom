"""audit.list pages across a rolling upgrade, on live ClickHouse: one write's
several audit rows (one millisecond, one request id) are never split by a page,
and a cursor this build writes pages the same on the previous build.

Skips cleanly when LOOM_CH_HOST is unset.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta
from typing import Any

import pytest

from server.core.cursors import decode_cursor, encode_cursor
from server.insights.api.models_audit import AuditListInput
from server.insights.app import audit as h

pytestmark = pytest.mark.skipif(
    not os.environ.get("LOOM_CH_HOST"),
    reason="LOOM_CH_HOST not set — integration tests require a live CH",
)

T0 = datetime(2026, 5, 16, 12, 0, 0, 250000)


@pytest.fixture
async def org(monkeypatch: pytest.MonkeyPatch) -> Any:
    from server.auth import context as ctx
    from server.core.ch import get_client

    monkeypatch.setattr(ctx, "current_auth", lambda: None)
    c = get_client()
    await c.connect()
    org = f"itest-audit-{uuid.uuid4().hex[:8]}"
    # One write of three rows under a request id ending in `|`, then two writes.
    rows = [(T0, "w|", c) for c in "abc"]
    rows += [(T0 - timedelta(seconds=1), "x", "d"), (T0 - timedelta(seconds=2), "y", "e")]
    rows += [(T0 - timedelta(seconds=3), "z", "f")]
    await c.insert(
        "loom.audit_log",
        [
            {
                "ts": ts,
                "event_type": "memory.set",
                "org": org,
                "namespace": "ns",
                "actor": "itest",
                "subject": "{}",
                "request_id": rid,
                "row_hash": ch * 64,
            }
            for ts, rid, ch in rows
        ],
    )
    yield org
    await c.execute(
        "ALTER TABLE loom.audit_log DELETE WHERE org = {org:String} SETTINGS mutations_sync = 2",
        params={"org": org},
    )
    await c.disconnect()


async def this_build(org: str, cursor: str | None) -> tuple[list[str], str | None]:
    out = await h.handle_audit_list(AuditListInput(org=org, namespace="ns", limit=2, cursor=cursor))
    return [r.row_hash[0] for r in out.rows], out.next_cursor


async def previous_build(org: str, cursor: str | None) -> tuple[list[str], str | None]:
    """origin/main's audit.list: `decode_cursor` (everything after the first bar is
    the request id), then a (ts, request_id) keyset (server/insights/store/audit.py)."""
    from server.core.ch import get_client

    where, params = "", {"org": org}
    if cursor:
        ts, rid = decode_cursor(cursor)
        lit = ts.strftime("%Y-%m-%d %H:%M:%S.") + f"{ts.microsecond // 1000:03d}"
        where = f" AND (ts, request_id) < (toDateTime64('{lit}', 3, 'UTC'), {{c_rid:String}})"
        params["c_rid"] = rid
    rows = await get_client().query_all(
        "SELECT ts, request_id, row_hash FROM loom.audit_log "
        f"WHERE org = {{org:String}} AND namespace = 'ns' {where} "
        "ORDER BY ts DESC, request_id DESC LIMIT 3",
        params=params,
    )
    page = rows[:2]
    nxt = encode_cursor(page[-1]["ts"], page[-1]["request_id"]) if len(rows) > 2 else None
    return [bytes(r["row_hash"]).decode()[0] for r in page], nxt


async def walk(org: str, builds: list[Any]) -> list[str]:
    seen, cursor = [], None
    for i in range(10):
        page, cursor = await builds[i % len(builds)](org, cursor)
        seen += page
        if cursor is None:
            return seen
    raise AssertionError("did not end")


@pytest.mark.asyncio
async def test_pages_of_this_build_return_every_row_once(org: str) -> None:
    assert await walk(org, [this_build]) == list("cbadef")


@pytest.mark.asyncio
async def test_pages_alternating_with_the_previous_build_return_every_row_once(
    org: str,
) -> None:
    assert await walk(org, [this_build, previous_build]) == list("cbadef")


@pytest.mark.asyncio
async def test_a_write_of_unhashed_rows_is_paged_whole(monkeypatch: pytest.MonkeyPatch) -> None:
    # Legacy rows carry no row hash: five rows of one write tie on (ts,
    # request_id, row_hash), so the hash cannot order them.
    from server.auth import context as ctx
    from server.core.ch import get_client

    monkeypatch.setattr(ctx, "current_auth", lambda: None)
    c = get_client()
    await c.connect()
    org = f"itest-audit-{uuid.uuid4().hex[:8]}"
    rows = [(T0, "w", str(i)) for i in range(5)] + [(T0 - timedelta(seconds=1), "x", "older")]
    await c.insert(
        "loom.audit_log",
        [
            {
                "ts": ts,
                "event_type": "memory.set",
                "org": org,
                "namespace": "ns",
                "actor": "itest",
                "subject": subject,
                "request_id": rid,
            }
            for ts, rid, subject in rows
        ],
    )
    try:
        seen, cursor = [], None
        for _ in range(10):
            out = await h.handle_audit_list(
                AuditListInput(org=org, namespace="ns", limit=2, cursor=cursor)
            )
            seen += [r.subject for r in out.rows]
            cursor = out.next_cursor
            if cursor is None:
                break
        assert sorted(seen) == ["0", "1", "2", "3", "4", "older"]
    finally:
        await c.execute(
            "ALTER TABLE loom.audit_log DELETE WHERE org = {org:String} "
            "SETTINGS mutations_sync = 2",
            params={"org": org},
        )
        await c.disconnect()
