"""mentor.brief's evidence ladder over real calls: each rung counts what the ledger and
the stores show, a write that stored nothing is not committed, a read that found nothing
is not a returned hit, and a task outcome is never observed.

Skips without LOOM_BASE_URL (tests/integration/conftest.py).
"""

from __future__ import annotations

from typing import Any

import httpx

from tests.integration._eventually import wait_until
from tests.integration._resources import unique_org

AGENT = "evidence-bot"


def _call(c: httpx.Client, op: str, body: dict[str, Any]) -> Any:
    r = c.post(f"/v1/{op}", json=body)
    assert r.status_code == 200, f"{op}: {r.status_code} {r.text[:400]}"
    return r.json()


def test_the_ladder_counts_what_the_calls_show(control_plane_url: str) -> None:
    org, ns = unique_org("evidence"), "evidence"
    ident = {"org": org, "namespace": ns, "agent": AGENT}
    fact = {**ident, "memory_key": "pref/report", "value": "Reports go out on Fridays"}
    with httpx.Client(base_url=control_plane_url, timeout=60.0) as c:
        _call(c, "memory.set", fact)
        # A dry run stores nothing.
        _call(c, "memory.set", {**fact, "value": "Reports go out on Mondays", "dry_run": True})
        _call(c, "memory.get", {**ident, "memory_key": "pref/report"})
        _call(c, "memory.get", {**ident, "memory_key": "pref/absent"})
        # The agent reports what it relied on when its run completes.
        run = _call(c, "run.begin", {**ident, "goal": "send the report"})
        done = {**ident, "run_id": run["run_id"], "status": "completed"}
        _call(c, "run.complete", {**done, "memories_used": ["pref/report"]})

        def brief() -> Any:
            out = _call(c, "mentor.brief", {"org": org, "namespace": ns, "agent": AGENT})
            ladder = {e["rung"]: e for e in out["evidence"]}
            return (out, ladder) if ladder["attempt"]["count"] == 6 else None

        out, ladder = wait_until(brief, max_wait=30.0, description="six calls in the ledger")
    counts = {rung: e["count"] for rung, e in ladder.items()}
    assert counts["success"] == 6
    # The write stored a row; the dry run did not.
    assert counts["committed_write"] == 1
    assert counts["indexed_write"] == 1
    # The get that found its key answered; the miss did not.
    assert counts["returned_hit"] == 1
    # One key reported relied on, as the client's own word.
    assert (counts["client_acknowledged"], ladder["client_acknowledged"]["source"]) == (
        1,
        "client",
    )
    assert (counts["task_outcome"], ladder["task_outcome"]["source"]) == (None, "none")
    assert out["ledger_from"] is not None and out["ledger_retention_days"] == 0
    assert not [s for s in out["skipped"] if s.split(":")[0] in ("evidence", "indexed_writes")]
