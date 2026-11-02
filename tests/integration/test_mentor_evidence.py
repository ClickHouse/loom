"""mentor.brief's evidence ladder over real calls: each rung counts what the ledger and
the stores show, a write that stored nothing is not committed, a read that found nothing
or abstained is not a returned hit, the dashboard's and the mentor's own calls are not
the agent's, and a graded run is the task outcome, as its grader reported it.

Skips without LOOM_BASE_URL (tests/integration/conftest.py).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from tests.integration._eventually import wait_until
from tests.integration._resources import unique_org

AGENT = "evidence-bot"


def _call(c: httpx.Client, op: str, body: dict[str, Any]) -> Any:
    r = c.post(f"/v1/{op}", json=body)
    assert r.status_code == 200, f"{op}: {r.status_code} {r.text[:400]}"
    return r.json()


def test_the_ladder_counts_what_the_calls_show(
    control_plane_url: str, ch_client: Any, mint_test_token: Any
) -> None:
    org, ns = unique_org("evidence"), "evidence"
    ident = {"org": org, "namespace": ns, "agent": AGENT}
    fact = {**ident, "memory_key": "pref/report", "value": "Reports go out on Fridays"}
    with httpx.Client(base_url=control_plane_url, timeout=60.0) as c:
        # A retried write with the same idempotency key is answered from the first one:
        # one call stored a row, the replay none.
        retried = {"Idempotency-Key": f"evidence-{org}"}
        for _ in range(2):
            r = c.post("/v1/memory.set", json=fact, headers=retried)
            assert r.status_code == 200, r.text[:400]
        assert r.headers.get("X-Idempotent-Replay") == "true", dict(r.headers)
        # Restating the fact under another key stores nothing (a merge, `noop`).
        restated = _call(c, "memory.set", {**fact, "memory_key": "pref/again"})
        assert restated["noop"] is True, restated
        # A dry run stores nothing.
        _call(c, "memory.set", {**fact, "value": "Reports go out on Mondays", "dry_run": True})
        _call(c, "memory.get", {**ident, "memory_key": "pref/report"})
        _call(c, "memory.get", {**ident, "memory_key": "pref/absent"})
        # A bulk read with one key found answered.
        bulk = {**ident, "memory_keys": ["pref/report", "pref/absent"]}
        _call(c, "memory.bulk_get", bulk)
        stored = _call(c, "memory.get", {**ident, "memory_key": "pref/report"})
        # A read by id names no agent, so the ledger records it under none: it is not this
        # agent's call, nor a returned hit.
        by_id = {"org": org, "memory_id": stored["memory_id"]}
        assert _call(c, "memory.get_by_id", by_id)["found"] is True
        # A search across agents (`agent: *`, the SDKs' default) is recorded under `*`:
        # not this agent's call.
        _call(c, "memory.search", {**ident, "agent": "*", "query": "when do reports go out"})
        abstained = _call(c, "memory.search", {**ident, "query": "zebra migration in Kenya"})
        assert abstained["abstain"] is True, abstained
        # Over MCP: a get that found its key, and `loom_search` (search.query) with a hit.
        mcp_results = []
        for n, (tool, args) in enumerate(
            (
                ("loom_memory_get", {**ident, "memory_key": "pref/report"}),
                ("loom_search", {**ident, "query": "when do reports go out"}),
            )
        ):
            rpc = {"jsonrpc": "2.0", "id": n, "method": "tools/call"}
            rpc["params"] = {"name": tool, "arguments": args}
            mcp = c.post("/mcp", json=rpc)
            assert mcp.status_code == 200 and mcp.json()["result"]["isError"] is False, mcp.text
            mcp_results.append(mcp.json()["result"]["structuredContent"])
        assert mcp_results[1]["memory"]["results"], mcp_results[1]
        # With a token, an MCP update that names no agent resolves its key namespace-wide,
        # so the ledger records it under no agent: not this agent's call.
        token = mint_test_token(org=org, namespaces=[ns], roles=["writer"], subject=AGENT)
        rpc = {"jsonrpc": "2.0", "id": 9, "method": "tools/call"}
        update = {"namespace": ns, "memory_key": "pref/report", "value": "Reports go out Fridays"}
        rpc["params"] = {"name": "loom_memory_update", "arguments": update}
        mcp = c.post("/mcp", json=rpc, headers={"Authorization": f"Bearer {token}"})
        assert mcp.status_code == 200 and mcp.json()["result"]["isError"] is False, mcp.text

        # `loom_search` (search.query) answers with hits per plane: one here.
        searched = _call(c, "search.query", {**ident, "query": "when do reports go out"})
        assert searched["memory"]["results"], searched
        asked = _call(c, "memory.ask", {**ident, "query": "When do reports go out?"})
        # A deferred consistency verdict lands under the asking agent; it is not a call the
        # agent made (here one whose judge failed).
        ch_client.insert(
            "loom.agent_events",
            [[datetime.now(UTC), org, ns, AGENT, "memory.ask.consistency", "verdict", 0]],
            column_names=["ts", "org", "namespace", "agent", "operation", "kind", "success"],
        )
        # A delete answers `found` too; it is not a read that answered.
        _call(c, "memory.set", {**ident, "memory_key": "tmp/scratch", "value": "Draft note"})
        deleted = _call(c, "memory.delete", {**ident, "memory_key": "tmp/scratch"})
        assert deleted["found"] is True, deleted
        # The dashboard reading under the agent's name is not the agent's use.
        _call(c, "memory.get", {**ident, "memory_key": "pref/report", "consumer": "loom-ui"})
        # The agent reports what it relied on when its run completes.
        run = _call(c, "run.begin", {**ident, "goal": "send the report"})
        done = {**ident, "run_id": run["run_id"], "status": "completed"}
        _call(c, "run.complete", {**done, "memories_used": ["pref/report"]})
        # A grader's verdict on the run is its outcome, as the grader reports it.
        grade = {"org": org, "namespace": ns, "run_id": run["run_id"], "grade": "pass"}
        _call(c, "run.grade", {**grade, "source": "ci"})
        # An outcome label is a second way to report reliance (on another fact, by id); a
        # label on a key that holds no row lands nowhere and is not counted.
        day = {**ident, "memory_key": "pref/day", "value": "Standups are at nine"}
        day_id = _call(c, "memory.set", day)["memory_id"]
        used = {**ident, "memory_id": day_id, "outcome": "used"}
        assert _call(c, "memory.outcome", used)["recorded"] is True
        # By key, on a third fact: the label names the row writer's agent, so the ledger
        # records it under the writer, whoever gave it; it is not counted.
        tz = {**ident, "memory_key": "pref/tz", "value": "The team works in UTC"}
        _call(c, "memory.set", tz)
        by_key = {**ident, "memory_key": "pref/tz", "outcome": "positive"}
        assert _call(c, "memory.outcome", by_key)["recorded"] is True
        missing = {**ident, "memory_key": "pref/nowhere", "outcome": "used"}
        assert _call(c, "memory.outcome", missing)["recorded"] is False

        def brief() -> Any:
            # A brief is ledgered under the agent it names; the ladder leaves it out.
            body = {"org": org, "namespace": ns, "agent": AGENT}
            out = _call(c, "mentor.brief", body)
            ladder = {e["rung"]: e for e in out["usage_evidence"]}
            return (out, ladder) if ladder["attempt"]["count"] == 22 else None

        out, ladder = wait_until(brief, max_wait=30.0, description="22 calls in the ledger")
    counts = {rung: e["count"] for rung, e in ladder.items()}
    assert counts["success"] == 22
    # Four writes stored a row; the replay, the restatement and the dry run did not.
    assert counts["committed_write"] == 4
    # The deleted row no longer counts.
    assert counts["indexed_write"] == 3
    # The two gets and the MCP get that found their key, the bulk read with one hit and the
    # two search.query calls with a hit answered, and the ask when it did not abstain; the
    # miss and the abstaining search did not.
    assert counts["returned_hit"] == 6 + (0 if asked["abstain"] else 1)
    # Two keys reported relied on (one by the run, one by an outcome by id), as the
    # client's word; the label by key and the one that landed nowhere are not counted.
    assert (counts["client_acknowledged"], ladder["client_acknowledged"]["source"]) == (
        2,
        "client",
    )
    # One graded run, as its grader reported it.
    assert (counts["task_outcome"], ladder["task_outcome"]["source"]) == (1, "client")
    # The retention is the server's (0 unless it sets one): known, whatever its value.
    assert out["ledger_from"] is not None and out["ledger_retention_days"] is not None
    assert out["skipped"] == [], out["skipped"]
