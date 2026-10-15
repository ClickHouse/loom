"""run.begin → memory.set(run_id) → run.complete(memories_used) → run.get,
end to end through the SDK against a running control plane.

Pins: the terminal row replaces the active one under FINAL; the memory
written under the run is derived (not sent) at completion; `used` and
`evidence` usage events land for the key the agent relied on.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))


async def _usage_kinds(ch_client: Any, namespace: str, timeout_s: float = 6.0) -> set[str]:
    """Usage events are buffered and flushed every ~0.5 s; poll for them."""
    deadline = asyncio.get_event_loop().time() + timeout_s
    kinds: set[str] = set()
    while asyncio.get_event_loop().time() < deadline:
        rows = ch_client.query(
            "SELECT DISTINCT event_type FROM loom.memory_usage_events "
            "WHERE namespace = {ns:String}",
            parameters={"ns": namespace},
        ).result_rows
        kinds = {str(r[0]) for r in rows}
        if {"used", "evidence"} <= kinds:
            break
        await asyncio.sleep(0.25)
    return kinds


@pytest.mark.asyncio
async def test_run_ledger_roundtrip(control_plane_url: str, ch_client) -> None:
    from loom import Agent

    ns = f"run-ledger-{uuid.uuid4().hex[:6]}"
    async with Agent(org="run-test", namespace=ns, agent="w", base_url=control_plane_url) as a:
        begun = await a.run.begin(
            goal="Pin the CI runner image",
            user_request="CI is red again",
            session_id="sess-1",
            expected_outcome="green main",
        )
        run_id = begun["run_id"]
        assert begun["status"] == "active" and run_id in begun["next"]

        # a memory the run relies on (written earlier, outside the run) and
        # one the run writes (tagged with the run_id)
        await a.memory.set("ci:flaky-runner", {"v": "runner image drifted"}, memory_type="semantic")
        await a.memory.set(
            "ci:fix:pin-image", {"v": "pinned 26.5.1"}, memory_type="semantic", run_id=run_id
        )

        done = await a.run.complete(
            run_id,
            status="completed",
            final_response="Pinned the runner image.",
            confidence=0.9,
            memories_used=["ci:flaky-runner", "does-not-exist"],
            reflection={
                "what_worked": ["pinning the runner image to 26.5.1"],
                "what_failed": ["retrying the flaky job"],
                "lessons": ["pin runner images; retries hide drift"],
                # A lesson names the memories it rests on
                "evidence": ["ci:flaky-runner"],
            },
        )
        assert done["status"] == "completed"
        assert done["reflection_key"] == f"reflection:{run_id}"
        assert done["used_applied"] == 1 and done["evidence_applied"] == 1
        assert done["memories_written"] == ["ci:fix:pin-image"]
        assert done["duration_ms"] >= 0 and done["observations"] == 0

        got = await a.run.get(run_id)
        assert got["found"] is True
        run = got["run"]
        assert run["status"] == "completed" and run["goal"] == "Pin the CI runner image"
        assert run["memories_used"] == ["ci:flaky-runner"]
        assert run["memories_written"] == ["ci:fix:pin-image"]
        assert run["session_id"] == "sess-1" and run["completed_at"]
        assert got["memories_written"] == ["ci:fix:pin-image"]

        listed = await a.run.list(status="completed")
        assert [r["run_id"] for r in listed["runs"]] == [run_id]

        # The reflection is a memory (readable by key) that plain recall never
        # returns; memory_type="reflection" reaches it.
        # written under the run's identity (session sess-1), so read it there
        got_ref = await a.memory.get(f"reflection:{run_id}", session_id="sess-1")
        assert got_ref is not None and got_ref["memory_type"] == "reflection"
        assert got_ref["value"]["lessons"] == ["pin runner images; retries hide drift"]
        plain = await a.memory.search(
            "pin runner images retries hide drift", top_k=10, namespace_scope="this"
        )
        assert all(h.memory_key != f"reflection:{run_id}" for h in plain)
        only_ref = await a.memory.search(
            "pin runner images retries hide drift",
            top_k=10,
            namespace_scope="this",
            memory_type="reflection",
        )
        assert [h.memory_key for h in only_ref] == [f"reflection:{run_id}"]

        # exactly one row survives FINAL for the run
        rows = ch_client.query(
            "SELECT count(), any(status) FROM loom.agent_runs FINAL "
            "WHERE namespace = {ns:String} AND run_id = {rid:String}",
            parameters={"ns": ns, "rid": run_id},
        ).result_rows
        assert rows[0][0] == 1 and rows[0][1] == "completed"

        kinds = await _usage_kinds(ch_client, ns)
        assert {"used", "evidence"} <= kinds, kinds


@pytest.mark.asyncio
async def test_complete_unknown_run_is_404(control_plane_url: str) -> None:
    from loom import Agent
    from loom.errors import NotFound

    ns = f"run-ledger-{uuid.uuid4().hex[:6]}"
    async with Agent(org="run-test", namespace=ns, agent="w", base_url=control_plane_url) as a:
        with pytest.raises(NotFound):
            await a.run.complete("never-begun", status="failed")


async def _record_turn(a: Any, session_id: str, message_id: str, turn_at: str = "") -> float:
    """One priced turn (10k input + 1k output tokens of claude-opus-5-5) under the session."""
    out = await a.transport.post(
        "/v1/cost.record_turns",
        {
            "org": a.org,
            "namespace": a.namespace,
            "agent": a.agent,
            "session_id": session_id,
            "turns": [
                {
                    "message_id": message_id,
                    "model": "claude-opus-5-5",
                    "input_tokens": 10_000,
                    "output_tokens": 1_000,
                    "turn_at": turn_at,
                }
            ],
        },
    )
    return float(out["cost_usd"])


async def _get_with_turns(a: Any, run_id: str, turns: int, timeout_s: float = 6.0) -> dict:
    """run.get once the run's turns are visible (turn rows are inserted asynchronously)."""
    deadline = asyncio.get_event_loop().time() + timeout_s
    while True:
        got = await a.run.get(run_id)
        if got["turns"] >= turns or asyncio.get_event_loop().time() > deadline:
            return got
        await asyncio.sleep(0.25)


@pytest.mark.asyncio
async def test_each_run_in_a_session_counts_only_its_own_turns(control_plane_url: str) -> None:
    from loom import Agent

    ns = f"run-ledger-{uuid.uuid4().hex[:6]}"
    async with Agent(org="run-test", namespace=ns, agent="w", base_url=control_plane_url) as a:
        first = await a.run.begin(goal="first", session_id="sess-b", budget_usd=0.1)
        first_cost = await _record_turn(a, "sess-b", "m1")
        await _get_with_turns(a, first["run_id"], 1)
        await a.run.complete(first["run_id"], status="completed")
        await asyncio.sleep(0.2)

        second = await a.run.begin(goal="second", session_id="sess-b", budget_usd=0.05)
        second_cost = await _record_turn(a, "sess-b", "m2")
        assert first_cost > 0.05 and first_cost + second_cost > 0.1

        # The second run's turn does not reach the closed first run.
        got_second = await _get_with_turns(a, second["run_id"], 1)
        got_first = await a.run.get(first["run_id"])
        assert got_first["turns"] == 1 and got_first["cost_usd"] == first_cost
        assert got_first["over_budget"] == []
        # The first run's turn does not count against the second.
        assert got_second["turns"] == 1 and got_second["cost_usd"] == second_cost
        assert got_second["over_budget"] == ["cost"]


@pytest.mark.asyncio
async def test_a_turn_stamped_by_a_slow_client_clock_still_counts(control_plane_url: str) -> None:
    from loom import Agent

    ns = f"run-ledger-{uuid.uuid4().hex[:6]}"
    async with Agent(org="run-test", namespace=ns, agent="w", base_url=control_plane_url) as a:
        run = await a.run.begin(goal="slow clock", session_id="sess-c", budget_usd=0.01)
        # A client clock five minutes behind the server stamps the turn before the run began.
        behind = datetime.now(UTC) - timedelta(minutes=5)
        cost = await _record_turn(a, "sess-c", "m1", turn_at=behind.isoformat())
        got = await _get_with_turns(a, run["run_id"], 1)
        assert got["turns"] == 1 and got["cost_usd"] == cost
        assert got["over_budget"] == ["cost"]


@pytest.mark.asyncio
async def test_a_cost_budget_without_a_session_is_refused(control_plane_url: str) -> None:
    from loom import Agent
    from loom.errors import ValidationError

    ns = f"run-ledger-{uuid.uuid4().hex[:6]}"
    async with Agent(org="run-test", namespace=ns, agent="w", base_url=control_plane_url) as a:
        with pytest.raises(ValidationError) as e:
            await a.run.begin(goal="no session", budget_usd=1.0)
        assert "session_id" in str(e.value.detail["errors"])
        timed = await a.run.begin(goal="time only", budget_seconds=60)
        assert timed["status"] == "active"
