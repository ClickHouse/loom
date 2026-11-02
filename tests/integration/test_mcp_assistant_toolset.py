"""`?toolset=assistant` end to end over MCP, against a live Loom + ClickHouse.

With a writer's token: the listing carries no write tool, `loom_memory_set` is refused
and writes nothing, `loom_memory_search` answers the full hit (session included) and
records no recall usage even when the call asks for it, while the same search over HTTP
does; namespace.audit counts the assistant's calls as inspector calls, not as the
namespace's own reads; and the assistant's MCP session opens no run.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))

from tests.integration._eventually import eventually  # noqa: E402
from tests.integration._resources import unique_org  # noqa: E402

ORG = unique_org("assistant")
CONTROL_ORG = unique_org("assistant-control")


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Mcp-Session-Id": uuid.uuid4().hex,
        "Accept": "application/json, text/event-stream",
    }


async def _rpc(c: httpx.AsyncClient, token: str, method: str, params: dict[str, Any]) -> Any:
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    r = await c.post("/mcp?toolset=assistant", json=body, headers=_headers(token))
    assert r.status_code == 200, r.text[:400]
    return r.json()["result"]


@pytest.mark.asyncio
async def test_the_assistant_reads_without_writing_or_recording_usage(
    control_plane_url: str, mint_test_token: Any, ch_client: Any
) -> None:
    ns = f"as-{uuid.uuid4().hex[:6]}"
    token = mint_test_token(org=ORG, namespaces=[ns], roles=["writer"], subject="assistant")
    auth = {"Authorization": f"Bearer {token}"}
    ident = {"org": ORG, "namespace": ns, "agent": "writer"}
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=30) as c:
        r = await c.post(
            "/v1/memory.set",
            json={**ident, "session_id": "s-1", "memory_key": "deploy/window", "value": "Tuesdays"},
            headers=auth,
        )
        assert r.status_code == 200, r.text[:400]

        tools = {t["name"] for t in (await _rpc(c, token, "tools/list", {}))["tools"]}
        assert "loom_memory_search" in tools and "loom_namespace_audit" in tools
        assert not {"loom_memory_set", "loom_op", "loom_search"} & tools

        refused = await _rpc(
            c,
            token,
            "tools/call",
            {
                "name": "loom_memory_set",
                "arguments": {"namespace": ns, "memory_key": "planted", "value": "x"},
            },
        )
        assert refused["isError"] is True
        r = await c.post("/v1/memory.get", json={**ident, "memory_key": "planted"}, headers=auth)
        assert r.status_code == 200 and not r.json().get("found"), r.text[:400]

        async def _assistant_hit() -> Any:
            res = await _rpc(
                c,
                token,
                "tools/call",
                {
                    "name": "loom_memory_search",
                    "arguments": {
                        "namespace": ns,
                        "session_id": "s-1",
                        "query": "Tuesdays",
                        "auto_touch": True,
                    },
                },
            )
            hits = res["structuredContent"].get("results") or []
            return next((h for h in hits if h["memory_key"] == "deploy/window"), None)

        hit = await eventually(_assistant_hit, description="the embedded row is searchable")
        # The full output: the identity the trimmed shape drops is there to cite.
        assert hit["session_id"] == "s-1" and hit["version"]

        # An ask that names no agent reads the writer's row, not the client's own (empty).
        asked = await _rpc(
            c,
            token,
            "tools/call",
            {
                "name": "loom_memory_ask",
                "arguments": {
                    "namespace": ns,
                    "session_id": "s-1",
                    "query": "Tuesdays",
                    "allow_synthesis": False,
                },
            },
        )
        assert "deploy/window" in asked["structuredContent"]["memory_keys"], asked

        # A read of one writer's row answers once the call names the writer it listed.
        traced = await _rpc(
            c,
            token,
            "tools/call",
            {
                "name": "loom_memory_provenance",
                "arguments": {
                    "namespace": ns,
                    "session_id": "s-1",
                    "memory_key": "deploy/window",
                    "agent": "writer",
                },
            },
        )
        assert traced["structuredContent"]["found"] is True, traced

        # The same search over HTTP, from a named reader in a session (a read recall credit
        # counts), records recall usage; once it shows, the assistant's earlier search
        # would have too.
        control = {"consumer": "reader", "session_id": "s-1", "query": "Tuesdays"}
        r = await c.post("/v1/memory.search", json={**ident, "agent": "*", **control}, headers=auth)
        assert r.status_code == 200, r.text[:400]

        def _usage() -> list[str]:
            rows = ch_client.query(
                "SELECT consumer FROM loom.memory_usage_events "
                "WHERE org = {org:String} AND namespace = {ns:String} AND event_type = 'recall'",
                parameters={"org": ORG, "ns": ns},
            ).result_rows
            return [str(row[0]) for row in rows]

        async def _http_recall() -> list[str]:
            return _usage()

        consumers = await eventually(_http_recall, description="the HTTP recall is recorded")
        assert "loom-assistant" not in consumers

        # A dashboard read, as the dashboard sends it (`consumer: loom-ui` on every call).
        r = await c.post(
            "/v1/memory.get",
            json={**ident, "memory_key": "deploy/window", "consumer": "loom-ui"},
            headers=auth,
        )
        assert r.status_code == 200, r.text[:400]

        async def _dashboard_get_ledgered() -> bool:
            rows = ch_client.query(
                "SELECT count() FROM loom.agent_events WHERE org = {org:String} "
                "AND namespace = {ns:String} AND operation = 'memory.get' "
                "AND consumer = 'loom-ui'",
                parameters={"org": ORG, "ns": ns},
            ).result_rows
            return int(rows[0][0]) >= 1

        # The audit is read once the dashboard's own row has landed, so `gets` below proves
        # it was left out rather than not yet written.
        await eventually(_dashboard_get_ledgered, description="the dashboard get is ledgered")

        async def _inspected() -> Any:
            r = await c.post(
                "/v1/namespace.audit", json={"org": ORG, "namespace": ns}, headers=auth
            )
            summary = r.json()["summary"]
            # Every ledger row landed: the assistant's, the dashboard's and the HTTP search's.
            landed = summary["inspector_calls"] >= 2 and summary["searches"] >= 1
            return summary if landed else None

        summary = await eventually(_inspected, description="the ledger has the assistant calls")
        # The HTTP search is the namespace's one counted search; the assistant's are not.
        assert summary["searches"] == 1
        # The one counted get is the test's own check for the refused write; the
        # dashboard's is not counted.
        assert summary["gets"] == 1

    # Every assistant call carried an MCP session id, and none opened a session run. A
    # control call on the core toolset, in an org of its own, opens one; once its run
    # shows, the assistant's would have too.
    def _runs(org: str) -> int:
        rows = ch_client.query(
            "SELECT count() FROM loom.agent_runs WHERE org = {org:String}",
            parameters={"org": org},
        ).result_rows
        return int(rows[0][0])

    control_ns = f"as-{uuid.uuid4().hex[:6]}"
    control = mint_test_token(org=CONTROL_ORG, namespaces=[control_ns], roles=["writer"])
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=30) as c:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "loom_memory_set",
                "arguments": {"namespace": control_ns, "memory_key": "k", "value": "v"},
            },
        }
        r = await c.post("/mcp?toolset=core", json=body, headers=_headers(control))
        assert r.status_code == 200 and not r.json()["result"].get("isError"), r.text[:400]

    async def _control_run() -> int:
        return _runs(CONTROL_ORG)

    await eventually(_control_run, description="the core session opens a run")
    assert _runs(ORG) == 0
