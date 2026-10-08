"""Server-side idempotency cache (loom.idempotency, ReplacingMergeTree with TTL).

Walks `Idempotency-Key` through the transport middleware:
  - same key + same body → replay (no double-execution)
  - same key + different body → 409 IDEMPOTENT_CONFLICT
  - different keys → both execute
  - no key → bypass (always executes)
  - cache hit short-circuits handler (verified by deleting underlying state)
  - cross-operation isolation (same key different op = miss; different body)
  - expired entry → miss
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))

from tests.integration._eventually import eventually  # noqa: E402

# ch_client fixture comes from tests/conftest.py (env-aware so Cloud runs work)

# No flaky marker: loom.idempotency is ReplacingMergeTree FINAL, so the read-side
# point-read can briefly miss a just-written cache row under load. Every cache
# read-after-write below is bridged by _wait_cached()/eventually() — polling a
# same-body replay (idempotent: a hit replays, a miss harmlessly re-runs the same
# set) until the row is visible, then asserting once.


async def _wait_cached(base_url, path, body, idem_key):
    """Poll until the idempotency row for (key, body) is visible to check().

    Probe with the SAME body so a hit replays and a miss harmlessly re-runs the
    same idempotent operation — never poll a different body (that would cache the
    wrong row permanently). Returns the confirmed replay response."""

    async def probe():
        r = await _post(base_url, path, body, idem_key)
        return r if r.headers.get("X-Idempotent-Replay") == "true" else None

    return await eventually(probe, description=f"idempotency row for {idem_key} to be cached")


async def _post(
    base_url: str, path: str, body: dict[str, Any], idem_key: str | None = None
) -> httpx.Response:
    headers: dict[str, str] = {}
    if idem_key:
        headers["Idempotency-Key"] = idem_key
    tok = os.environ.get("LOOM_API_KEY")
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    async with httpx.AsyncClient(base_url=base_url, timeout=5.0) as c:
        return await c.post(path, json=body, headers=headers)


@pytest.mark.asyncio
async def test_same_key_same_body_replays(control_plane_url: str) -> None:
    """Two POSTs with identical key + body — second returns cached response."""
    ns = f"idem-{uuid.uuid4().hex[:8]}"
    body = {
        "org": "test",
        "namespace": ns,
        "agent": "t",
        "memory_key": "k",
        "memory_type": "semantic",
        "value": {"x": 1},
    }
    k = str(uuid.uuid4())

    r1 = await _post(control_plane_url, "/v1/memory.set", body, k)
    assert r1.status_code == 200
    v1 = r1.json()["version"]
    assert r1.headers.get("X-Idempotent-Replay") != "true"  # first call ran the handler

    r2 = await _wait_cached(control_plane_url, "/v1/memory.set", body, k)
    assert r2.status_code == 200
    assert r2.headers.get("X-Idempotent-Replay") == "true"  # second call replayed
    assert r2.json()["version"] == v1, "replay must return the same version"


@pytest.mark.asyncio
async def test_same_key_different_body_returns_409(control_plane_url: str) -> None:
    """Reuse of an idempotency key with different body is a contract violation."""
    ns = f"idem-{uuid.uuid4().hex[:8]}"
    body_a = {
        "org": "test",
        "namespace": ns,
        "agent": "t",
        "memory_key": "k",
        "memory_type": "semantic",
        "value": 1,
    }
    body_b = {**body_a, "value": 42}
    k = str(uuid.uuid4())

    r1 = await _post(control_plane_url, "/v1/memory.set", body_a, k)
    assert r1.status_code == 200

    # Precondition (not the assertion): body_a must be the visible cached row
    # before we send body_b. If body_a's row hasn't landed, body_b's handler
    # runs and stores body_b, which then wins under ReplacingMergeTree FINAL and
    # replays 200 forever — the 409 never recovers. Confirm body_a cached, THEN
    # fire body_b exactly once.
    await _wait_cached(control_plane_url, "/v1/memory.set", body_a, k)
    r2 = await _post(control_plane_url, "/v1/memory.set", body_b, k)
    assert r2.status_code == 409
    assert r2.json()["detail"]["code"] == "IDEMPOTENT_CONFLICT"


@pytest.mark.asyncio
async def test_different_keys_both_execute(control_plane_url: str) -> None:
    """Distinct idempotency keys → independent executions, distinct versions."""
    ns = f"idem-{uuid.uuid4().hex[:8]}"
    body = {
        "org": "test",
        "namespace": ns,
        "agent": "t",
        "memory_key": "k",
        "memory_type": "semantic",
        "value": 1,
    }

    r1 = await _post(control_plane_url, "/v1/memory.set", body, str(uuid.uuid4()))
    r2 = await _post(control_plane_url, "/v1/memory.set", body, str(uuid.uuid4()))
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["version"] != r2.json()["version"]


@pytest.mark.asyncio
async def test_missing_key_bypasses_cache(control_plane_url: str) -> None:
    """No Idempotency-Key → handler always runs; no replay header."""
    ns = f"idem-{uuid.uuid4().hex[:8]}"
    body = {
        "org": "test",
        "namespace": ns,
        "agent": "t",
        "memory_key": "k",
        "memory_type": "semantic",
        "value": 1,
    }
    r1 = await _post(control_plane_url, "/v1/memory.set", body)
    r2 = await _post(control_plane_url, "/v1/memory.set", body)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.headers.get("X-Idempotent-Replay") != "true"
    assert r2.headers.get("X-Idempotent-Replay") != "true"
    assert r1.json()["version"] != r2.json()["version"]  # handler ran twice


@pytest.mark.asyncio
async def test_cache_hit_short_circuits_handler(control_plane_url: str, ch_client) -> None:
    """If the cache replays, the underlying memory row is irrelevant —
    deleting it doesn't change the replayed response."""
    ns = f"idem-{uuid.uuid4().hex[:8]}"
    body = {
        "org": "test",
        "namespace": ns,
        "agent": "t",
        "memory_key": "k",
        "memory_type": "semantic",
        "value": 1,
    }
    k = str(uuid.uuid4())

    r1 = await _post(control_plane_url, "/v1/memory.set", body, k)
    assert r1.status_code == 200
    v1 = r1.json()["version"]

    # Confirm the replay is cached & visible BEFORE wiping the underlying row.
    # If the cache read missed while the memory row is gone, the handler would
    # re-execute on an absent row → new (higher) version → store() overwrites the
    # cache with the wrong version, permanently corrupting the replay.
    await _wait_cached(control_plane_url, "/v1/memory.set", body, k)

    # Wipe the underlying memory row
    ch_client.command(
        "ALTER TABLE loom.memory DELETE WHERE org='test' AND namespace=%(ns)s",
        parameters={"ns": ns},
    )

    # Cache should still replay
    r2 = await _post(control_plane_url, "/v1/memory.set", body, k)
    assert r2.status_code == 200
    assert r2.headers.get("X-Idempotent-Replay") == "true"
    assert r2.json()["version"] == v1


@pytest.mark.asyncio
async def test_replay_works_for_reads(control_plane_url: str) -> None:
    """Idempotency applies to read ops too — memory.get with same key replays."""
    ns = f"idem-{uuid.uuid4().hex[:8]}"
    set_body = {
        "org": "test",
        "namespace": ns,
        "agent": "t",
        "memory_key": "k",
        "memory_type": "semantic",
        "value": 1,
    }
    await _post(control_plane_url, "/v1/memory.set", set_body)

    get_body = {"org": "test", "namespace": ns, "agent": "t", "memory_key": "k"}

    # The seeded row must be readable before the first get caches found=True.
    async def _found():
        r = await _post(control_plane_url, "/v1/memory.get", get_body)
        return r if (r.status_code == 200 and r.json().get("found") is True) else None

    await eventually(_found, description="seeded memory row to be readable")

    k = str(uuid.uuid4())
    r1 = await _post(control_plane_url, "/v1/memory.get", get_body, k)
    assert r1.status_code == 200 and r1.json()["found"] is True

    r2 = await _wait_cached(control_plane_url, "/v1/memory.get", get_body, k)
    assert r2.status_code == 200
    assert r2.headers.get("X-Idempotent-Replay") == "true"


@pytest.mark.asyncio
async def test_expired_entry_treated_as_miss(control_plane_url: str, ch_client) -> None:
    """Idempotency rows past `expires_at` are treated as cache miss
    (sweep job will eventually delete them)."""
    body = {
        "org": "test",
        "namespace": f"idem-{uuid.uuid4().hex[:8]}",
        "agent": "t",
        "memory_key": "k",
        "memory_type": "semantic",
        "value": 1,
    }
    k = str(uuid.uuid4())

    # Pre-seed an EXPIRED cache row for this key (intentionally past expires_at)
    past = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)
    from server.core.idempotency import hash_body

    h = hash_body(body)
    ch_client.insert(
        "loom.idempotency",
        [[k, h, "memory.set", 200, '{"version": 999}', "{}", past, past]],
        column_names=[
            "idempotency_key",
            "body_hash",
            "operation",
            "status_code",
            "response_body",
            "response_headers",
            "created_at",
            "expires_at",
        ],
    )

    # Request should bypass the expired row and re-execute
    r = await _post(control_plane_url, "/v1/memory.set", body, k)
    assert r.status_code == 200
    assert r.headers.get("X-Idempotent-Replay") != "true"
    # The new response's version is real (not the bogus 999 we seeded)
    assert r.json()["version"] != 999


@pytest.mark.asyncio
async def test_loom_idempotency_uses_cloud_compatible_engine(ch_client) -> None:
    """CH Cloud's Shared database rejects on-disk engines such as
    EmbeddedRocksDB, so the table is ReplacingMergeTree(created_at) with
    native TTL. On Cloud, this gets
    auto-rewritten to SharedReplacingMergeTree behind the scenes."""
    result = ch_client.query(
        "SELECT engine FROM system.tables WHERE database='loom' AND name='idempotency'"
    )
    engine = result.result_rows[0][0]
    # Local CH returns ReplacingMergeTree; CH Cloud returns SharedReplacingMergeTree
    assert engine in (
        "ReplacingMergeTree",
        "SharedReplacingMergeTree",
    ), f"unexpected idempotency engine: {engine!r}"
