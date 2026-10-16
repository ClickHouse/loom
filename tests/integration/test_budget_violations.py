"""Per-operation budget-violation tracker.

When a handler takes longer than its `budget_p99_ms`, the HTTP transport
emits a `budget.violated` event on loom.events (kind `budget_violation`).
Ops alerts hang off these rows.

Tests use the budget-override admin endpoint to force violations
deterministically (budget=0 → every call overages) and to verify clearing
the override stops them.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))

from tests.integration._eventually import eventually, overrides_adopted  # noqa: E402
from tests.integration._resources import unique_org  # noqa: E402

ORG = unique_org("budget")


# ch_client fixture comes from tests/conftest.py (env-aware so Cloud runs work)


async def _set_budget(c: httpx.AsyncClient, op: str, budget_ms: int) -> None:
    r = await c.post("/v1/admin/budget.set", json={"op": op, "budget_ms": budget_ms})
    assert r.status_code == 200, r.text
    await overrides_adopted()


async def _clear_budget(c: httpx.AsyncClient, op: str) -> None:
    r = await c.post("/v1/admin/budget.set", json={"op": op, "clear": True})
    assert r.status_code == 200, r.text
    await overrides_adopted()


def _violations(ch_client, ns: str, op: str = "") -> int:
    rows = ch_client.query(
        """
        SELECT count() FROM loom.events
        WHERE org = {org:String} AND namespace = {ns:String}
          AND kind = 'budget_violation'
          AND position(payload, {op:String}) > 0
        """,
        parameters={"org": ORG, "ns": ns, "op": op},
    ).result_rows
    return int(rows[0][0])


async def _wait_for_violation(ch_client, ns: str, op: str, timeout_s: float = 10.0) -> int:
    async def _seen() -> int:
        return _violations(ch_client, ns, op)

    return await eventually(_seen, max_wait=timeout_s, description=f"a {op} violation in {ns}")


async def _flush_barrier(control_plane_url: str, ch_client) -> None:
    """Emit one violation that must follow every event already buffered, and
    wait for it: once it is visible, an absent row is absent, not unflushed."""
    from loom import Agent

    ns = f"bv-barrier-{uuid.uuid4().hex[:6]}"
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=5.0) as c:
        await _set_budget(c, "memory.get", 0)
    try:
        async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
            await a.memory.get("absent")
        await _wait_for_violation(ch_client, ns, "memory.get")
    finally:
        async with httpx.AsyncClient(base_url=control_plane_url, timeout=5.0) as c:
            await _clear_budget(c, "memory.get")


@pytest.mark.asyncio
async def test_budget_override_to_zero_forces_violation(control_plane_url: str, ch_client) -> None:
    """budget=0 → any handler latency overages → audit row lands."""
    from loom import Agent

    ns = f"bv-{uuid.uuid4().hex[:6]}"
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=5.0) as c:
        await _set_budget(c, "memory.set", 0)

    try:
        async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
            await a.memory.set("k", {"v": 1}, memory_type="semantic")

        count = await _wait_for_violation(ch_client, ns, "memory.set")
        assert count >= 1, "expected a budget.violated row for memory.set"

        # Inspect subject payload
        rows = ch_client.query(
            """
            SELECT payload, agent FROM loom.events
            WHERE org = {org:String} AND namespace = {ns:String}
              AND kind = 'budget_violation'
            ORDER BY ts DESC LIMIT 1
            """,
            parameters={"org": ORG, "ns": ns},
        ).result_rows
        subject = json.loads(rows[0][0])
        assert subject["operation"] == "memory.set"
        assert subject["budget_ms"] == 0
        assert subject["elapsed_ms"] >= 0
        assert "overage_ratio" in subject
        # `agent` is an identity column of loom.events, not a payload key
        # (events_stream.to_event consumes it), so it is read as a column.
        assert rows[0][1] == "w"
    finally:
        async with httpx.AsyncClient(base_url=control_plane_url, timeout=5.0) as c:
            await _clear_budget(c, "memory.set")


@pytest.mark.asyncio
async def test_clearing_override_stops_violations(control_plane_url: str, ch_client) -> None:
    """After clearing the override, the op's real budget applies and a normal
    call no longer fires a violation."""
    from loom import Agent

    ns_violate = f"bv-on-{uuid.uuid4().hex[:6]}"
    ns_clean = f"bv-off-{uuid.uuid4().hex[:6]}"

    async with httpx.AsyncClient(base_url=control_plane_url, timeout=5.0) as c:
        await _set_budget(c, "memory.set", 0)
    async with Agent(org=ORG, namespace=ns_violate, agent="w", base_url=control_plane_url) as a:
        await a.memory.set("k", {"v": 1}, memory_type="semantic")
    await _wait_for_violation(ch_client, ns_violate, "memory.set")

    async with httpx.AsyncClient(base_url=control_plane_url, timeout=5.0) as c:
        await _clear_budget(c, "memory.set")
    async with Agent(org=ORG, namespace=ns_clean, agent="w", base_url=control_plane_url) as a:
        await a.memory.set("k", {"v": 1}, memory_type="semantic")

    await _flush_barrier(control_plane_url, ch_client)
    assert _violations(ch_client, ns_clean) == 0, "cleared budget should not emit violations"


@pytest.mark.asyncio
async def test_default_budget_not_violated_by_normal_call(
    control_plane_url: str, ch_client
) -> None:
    """memory.set's real budget is 30ms; a single local-CH call should
    finish well under it. No violation rows for this op in this ns."""
    from loom import Agent

    ns = f"bv-norm-{uuid.uuid4().hex[:6]}"
    async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
        await a.memory.set("k", {"v": 1}, memory_type="semantic")

    await _flush_barrier(control_plane_url, ch_client)
    # A single local call stays under the default budget (times the CI
    # multiplier); a violation here is the same signal the real SLO would give.
    assert _violations(ch_client, ns, "memory.set") == 0, "no violations expected at default budget"


@pytest.mark.asyncio
async def test_violations_are_not_audit_rows_but_reach_the_admin_report(
    control_plane_url: str, ch_client, mint_test_token
) -> None:
    """A budget violation is a latency reading on loom.events,
    not a memory-changing write in the tamper-evident audit log. The admin
    report reads the stream; audit.list does not list the violation."""
    from loom import Agent

    ns = f"bv-aud-{uuid.uuid4().hex[:6]}"
    admin_tok = mint_test_token(org=ORG, namespaces=["*"], roles=["admin", "writer"])
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=5.0) as c:
        await _set_budget(c, "memory.set", 0)
    try:
        async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
            await a.memory.set("k", {"v": 1}, memory_type="semantic")
        assert await _wait_for_violation(ch_client, ns, "memory.set") >= 1

        async with httpx.AsyncClient(base_url=control_plane_url, timeout=5.0) as c:
            r = await c.post("/v1/admin/budget.violations", json={"since_seconds": 600, "org": ORG})
        assert r.status_code == 200, r.text
        assert any(row["namespace"] == ns for row in r.json()["rows"])

        audit_rows = ch_client.query(
            """
            SELECT count() FROM loom.audit_log
            WHERE org = {org:String} AND namespace = {ns:String}
              AND event_type = 'budget.violated'
            """,
            parameters={"org": ORG, "ns": ns},
        ).result_rows
        assert int(audit_rows[0][0]) == 0, "no budget.violated audit rows any more"

        async with Agent(
            org=ORG,
            namespace=ns,
            agent="r",
            base_url=control_plane_url,
            api_key=admin_tok,
        ) as rd:
            page = await rd.audit.list(event_type="budget.violated", limit=10)
        assert not [row for row in page["rows"] if row.get("namespace") == ns]
    finally:
        async with httpx.AsyncClient(base_url=control_plane_url, timeout=5.0) as c:
            await _clear_budget(c, "memory.set")
