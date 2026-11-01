"""mentor.brief's evidence ladder over real calls: each rung counts what the ledger and
the stores show, a write that stored nothing is not committed, a read that found nothing
or abstained is not a returned hit, the dashboard's and the mentor's own calls are not
the agent's, and a task outcome is never observed.

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


def test_the_ladder_counts_what_the_calls_show(control_plane_url: str, ch_client: Any) -> None:
    org, ns = unique_org("evidence"), "evidence"
    ident = {"org": org, "namespace": ns, "agent": AGENT}
    fact = {**ident, "memory_key": "pref/report", "value": "Reports go out on Fridays"}
    with httpx.Client(base_url=control_plane_url, timeout=60.0) as c:
        _call(c, "memory.set", fact)
        # Restating the fact under another key stores nothing (a merge, `noop`).
        restated = _call(c, "memory.set", {**fact, "memory_key": "pref/again"})
        assert restated["noop"] is True, restated
        # A dry run stores nothing.
        _call(c, "memory.set", {**fact, "value": "Reports go out on Mondays", "dry_run": True})
        _call(c, "memory.get", {**ident, "memory_key": "pref/report"})
        _call(c, "memory.get", {**ident, "memory_key": "pref/absent"})
        # A bulk read with one key found answered; a read by id that found its row did too.
        bulk = {**ident, "memory_keys": ["pref/report", "pref/absent"]}
        _call(c, "memory.bulk_get", bulk)
        stored = _call(c, "memory.get", {**ident, "memory_key": "pref/report"})
        # The read by id names no namespace or agent of its own; the ledger takes them
        # from the body.
        by_id = {**ident, "memory_id": stored["memory_id"]}
        assert _call(c, "memory.get_by_id", by_id)["found"] is True
        abstained = _call(c, "memory.search", {**ident, "query": "zebra migration in Kenya"})
        assert abstained["abstain"] is True, abstained
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

        def brief() -> Any:
            # A brief is ledgered under the agent it names; the ladder leaves it out.
            body = {"org": org, "namespace": ns, "agent": AGENT}
            out = _call(c, "mentor.brief", body)
            ladder = {e["rung"]: e for e in out["evidence"]}
            return (out, ladder) if ladder["attempt"]["count"] == 15 else None

        out, ladder = wait_until(brief, max_wait=30.0, description="fifteen calls in the ledger")
    counts = {rung: e["count"] for rung, e in ladder.items()}
    assert counts["success"] == 15
    # Two writes stored a row; the restatement and the dry run did not.
    assert counts["committed_write"] == 2
    # The deleted row is no longer one a search can serve.
    assert counts["indexed_write"] == 1
    # The two gets that found their key, the bulk read with one hit, the read by id and the
    # search with a hit answered, and the ask when it did not abstain; the miss and the
    # abstaining search did not.
    assert counts["returned_hit"] == 5 + (0 if asked["abstain"] else 1)
    # One key reported relied on, as the client's own word.
    assert (counts["client_acknowledged"], ladder["client_acknowledged"]["source"]) == (
        1,
        "client",
    )
    assert (counts["task_outcome"], ladder["task_outcome"]["source"]) == (None, "none")
    # The retention is the server's (0 unless it sets one): known, whatever its value.
    assert out["ledger_from"] is not None and out["ledger_retention_days"] is not None
    assert not [s for s in out["skipped"] if s.split(":")[0] in ("evidence", "indexed_writes")]
