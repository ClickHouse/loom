"""Named-user budget policy, warnings and erasure through the authenticated API."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from tests.integration._resources import unique_org

pytestmark = pytest.mark.usefixtures("ch_target_required")


async def test_budget_policy_status_warning_stream_and_user_erasure(
    control_plane_url: str, ch_client: Any, mint_test_token: Any
) -> None:
    org = unique_org("user-budgets")
    namespace, other = "budget-main", "budget-other"
    inbox, other_inbox = "budget-warnings", "other-warnings"
    token = mint_test_token(org=org, namespaces=["*"], roles=["org_admin", "admin", "writer"])
    async with httpx.AsyncClient(
        base_url=control_plane_url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=90,
        trust_env=False,
    ) as client:

        async def post(op: str, **body: Any) -> dict[str, Any]:
            response = await client.post(f"/v1/{op}", json={"org": org, **body})
            assert response.status_code == 200, response.text
            return response.json()

        for ns, target in [(namespace, inbox), (other, other_inbox)]:
            await post("namespace.register", namespace=ns, owner_email="owner@example.com")
            await post("inbox.register", namespace=ns, inbox=target, owner_agent="budget-worker")
        refused = await client.post(
            "/v1/namespace.register",
            json={
                "org": org,
                "namespace": namespace,
                "owner_email": "owner@example.com",
                "budget_alert_inbox": other_inbox,
            },
        )
        assert refused.status_code == 422, refused.text
        await post(
            "namespace.register",
            namespace=namespace,
            owner_email="owner@example.com",
            daily_cost_cap_usd=5,
            daily_user_cost_cap_usd=2,
            budget_alert_inbox=inbox,
        )
        info = await post("namespace.get", namespace=namespace)
        assert info["daily_user_cost_cap_usd"] == 2 and info["budget_alert_inbox"] == inbox
        listing = await post("namespace.list")
        listed = next(row for row in listing["rows"] if row["namespace"] == namespace)
        assert listed["daily_user_cost_cap_usd"] == 2 and listed["budget_alert_inbox"] == inbox

        def spend(user_id: str, amount: float) -> None:
            ch_client.insert(
                "loom.agent_events",
                [
                    [
                        datetime.now(UTC),
                        org,
                        namespace,
                        "budget-agent",
                        user_id,
                        "-",
                        "memory.ask",
                        uuid.uuid4().hex,
                        1,
                        True,
                        amount,
                    ]
                ],
                column_names=[
                    "ts",
                    "org",
                    "namespace",
                    "agent",
                    "user_id",
                    "session_id",
                    "operation",
                    "request_id",
                    "latency_ms",
                    "success",
                    "upstream_cost_usd",
                ],
            )

        def warnings() -> list[dict[str, Any]]:
            return list(
                ch_client.query(
                    "SELECT user_id, task_id, seq, patch, actor, source_class FROM loom.task_events "
                    "WHERE org = {org:String} AND inbox = {inbox:String} AND event = 'budget.warning'",
                    parameters={"org": org, "inbox": inbox},
                ).named_results()
            )

        async def wait_warnings(expected: set[str]) -> list[dict[str, Any]]:
            for _ in range(100):
                rows = warnings()
                if expected <= {row["user_id"] for row in rows}:
                    return rows
                await asyncio.sleep(0.1)
            raise AssertionError(f"missing warning users {expected}: {warnings()}")

        spend("alice", 1.7)
        spend("bob", 0.1)
        alice = await post("cost.budget_status", namespace=namespace, user_id="alice")
        assert alice["cap_source"] == "namespace_user" and alice["spent_24h_usd"] is None
        assert alice["pct_of_cap"] is None
        assert not alice["over_cap"] and not alice["namespace_over_cap"]
        await wait_warnings({"alice"})
        for _ in range(3):
            await post("cost.budget_status", namespace=namespace, user_id="alice")
        await asyncio.sleep(0.2)
        rows = warnings()
        assert len(rows) == 1
        assert rows[0]["actor"] == "process:budget_warning" and rows[0]["source_class"] == "derived"
        assert json.loads(rows[0]["patch"]) == {
            "namespace": namespace,
            "user_id": "alice",
            "cap_usd": 2,
            "spent_24h_usd": None,
            "share": 0.8,
        }

        spend("alice", 0.5)
        alice = await post("cost.budget_status", namespace=namespace, user_id="alice")
        assert alice["over_cap"] and not alice["namespace_over_cap"]
        pinned = mint_test_token(
            org=org, namespaces=[namespace], roles=["viewer"], read_user_id="alice"
        )
        denied = await client.post(
            "/v1/cost.budget_status",
            headers={"Authorization": f"Bearer {pinned}"},
            json={"org": org, "namespace": namespace, "user_id": "bob"},
        )
        assert denied.status_code == 403, denied.text
        await post(
            "namespace.register",
            namespace=namespace,
            owner_email="owner@example.com",
            daily_cost_cap_usd=2,
        )
        bob = await post("cost.budget_status", namespace=namespace, user_id="bob")
        assert not bob["over_cap"] and bob["namespace_over_cap"]
        await wait_warnings({"alice", "-"})
        await post(
            "namespace.register",
            namespace=namespace,
            owner_email="owner@example.com",
            daily_user_cost_cap_usd=0.05,
        )
        await post("cost.budget_status", namespace=namespace, user_id="bob")
        await wait_warnings({"alice", "bob", "-"})
        erased = await post(
            "erasure.request",
            scope_type="user_id",
            scope_value="alice",
            requester="test-suite",
        )
        assert erased["status"] == "completed", erased
        kept = warnings()
        assert "alice" not in {row["user_id"] for row in kept}
        assert {"bob", "-"} <= {row["user_id"] for row in kept}
        assert all("alice" not in row["patch"] for row in kept)


async def test_warning_insert_start_deadline_is_enforced_by_clickhouse(ch_client: Any) -> None:
    import time

    from server.core import rmt_carry
    from server.core.ch import get_client, set_query_tag
    from server.core.errors import BackendUnavailableError
    from server.tasks.store import budget_notifications

    ch = get_client()
    await ch.connect()
    set_query_tag("test.budget_expired_send")
    org = unique_org("budget-expired")
    event_id = str(uuid.uuid4())
    row = {
        "org": org,
        "namespace": "budget",
        "inbox": "warnings",
        "task_id": event_id,
        "seq": 1,
        "event": "budget.warning",
        "from_status": "",
        "to_status": "",
        "actor": "process:budget_warning",
        "agent": "-",
        "user_id": "alice",
        "session_id": "-",
        "source_class": "derived",
        "from_subject": "",
        "fence": 0,
        "version": 1,
        "reason": "",
        "patch": "{}",
        "note": "User budget warning",
        "ts": datetime.now(UTC),
    }
    try:
        with rmt_carry.under_lease(time.monotonic() - 100):
            with pytest.raises(BackendUnavailableError):
                await budget_notifications.insert_event(row)
        rows = ch_client.query(
            "SELECT count() FROM loom.task_events WHERE org = {org:String} AND task_id = {id:UUID}",
            parameters={"org": org, "id": event_id},
        ).result_rows
        assert rows == [(0,)]
    finally:
        await ch.disconnect()
