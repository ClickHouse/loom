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


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@pytest.mark.asyncio
async def test_each_run_in_a_session_counts_only_its_own_turns(
    control_plane_url: str, ch_client: Any
) -> None:
    from loom import Agent

    ns = f"run-ledger-{uuid.uuid4().hex[:6]}"
    async with Agent(org="run-test", namespace=ns, agent="w", base_url=control_plane_url) as a:
        # Budgets in units of one turn's price, so the test outlives a price change:
        # the first run's budget holds one turn but not two, the second's not one.
        from server.observability.cost_accumulator import turn_cost_usd

        turn = turn_cost_usd("claude-opus-5-5", input_tokens=10_000, output_tokens=1_000)
        # The Stop hook posts a prompt's turns after the prompt ends, so after the
        # agent has completed a run it opened during that prompt.
        first = await a.run.begin(goal="first", session_id="sess-b", budget_usd=1.5 * turn)
        await asyncio.sleep(0.2)
        first_turn_at = _now_iso()
        await asyncio.sleep(0.2)
        await a.run.complete(first["run_id"], status="completed")
        first_cost = await _record_turn(a, "sess-b", "m1", turn_at=first_turn_at)

        await asyncio.sleep(0.2)
        second = await a.run.begin(goal="second", session_id="sess-b", budget_usd=0.5 * turn)
        await asyncio.sleep(0.2)
        second_cost = await _record_turn(a, "sess-b", "m2", turn_at=_now_iso())
        assert first_cost == pytest.approx(turn) and second_cost == pytest.approx(turn)

        # A re-sent turn keeps its time, and so its run.
        await _record_turn(a, "sess-b", "m1", turn_at=first_turn_at)
        ch_client.command("SYSTEM FLUSH ASYNC INSERT QUEUE")

        got_first = await a.run.get(first["run_id"])
        assert got_first["turns"] == 1 and got_first["cost_usd"] == first_cost
        assert got_first["over_budget"] == []
        got_second = await a.run.get(second["run_id"])
        assert got_second["turns"] == 1 and got_second["cost_usd"] == second_cost
        assert got_second["over_budget"] == ["cost"]


@pytest.mark.asyncio
async def test_a_replayed_turn_does_not_move_between_runs(
    control_plane_url: str, ch_client: Any
) -> None:
    from loom import Agent

    ns = f"run-ledger-{uuid.uuid4().hex[:6]}"
    async with Agent(org="run-test", namespace=ns, agent="w", base_url=control_plane_url) as a:
        # A client clock an hour fast dates its turn ahead of every run window.
        ahead = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        turn = {
            "org": a.org,
            "namespace": a.namespace,
            "agent": a.agent,
            "session_id": "sess-r",
            "turns": [{"message_id": "m1", "model": "claude-opus-5-5", "turn_at": ahead}],
        }
        first = await a.run.begin(goal="first", session_id="sess-r", budget_usd=1000.0)
        out = await a.transport.post("/v1/cost.record_turns", turn)
        assert out["turns_dated_ahead"] == 1
        ch_client.command("SYSTEM FLUSH ASYNC INSERT QUEUE")
        before = await a.run.get(first["run_id"])
        await a.run.complete(first["run_id"], status="completed")
        second = await a.run.begin(goal="second", session_id="sess-r", budget_usd=1000.0)

        # The hook delivers the identical usage again while the second run is open.
        await a.transport.post("/v1/cost.record_turns", turn)
        ch_client.command("SYSTEM FLUSH ASYNC INSERT QUEUE")

        after = await a.run.get(first["run_id"])
        assert (after["turns"], after["cost_usd"]) == (before["turns"], before["cost_usd"])
        got_second = await a.run.get(second["run_id"])
        assert got_second["turns"] == 0 and got_second["cost_usd"] == 0.0


@pytest.mark.asyncio
async def test_workers_in_their_own_sessions_count_only_their_own_turns(
    control_plane_url: str, ch_client: Any
) -> None:
    from loom import Agent

    ns = f"run-ledger-{uuid.uuid4().hex[:6]}"
    async with Agent(org="run-test", namespace=ns, agent="a", base_url=control_plane_url) as a:
        run_a = await a.run.begin(goal="worker a", session_id="worker-a", budget_usd=1.0)
        run_b = await a.run.begin(goal="worker b", session_id="worker-b", budget_usd=1.0)
        cost = await _record_turn(a, "worker-a", "m1", turn_at=_now_iso())
        ch_client.command("SYSTEM FLUSH ASYNC INSERT QUEUE")
        got_a = await a.run.get(run_a["run_id"])
        assert got_a["turns"] == 1 and got_a["cost_usd"] == cost
        got_b = await a.run.get(run_b["run_id"])
        assert got_b["turns"] == 0 and got_b["cost_usd"] == 0.0 and got_b["over_budget"] == []


@pytest.mark.asyncio
async def test_a_cost_budget_without_a_session_is_refused(control_plane_url: str) -> None:
    from loom import Agent
    from loom.errors import ValidationError

    ns = f"run-ledger-{uuid.uuid4().hex[:6]}"
    async with Agent(org="run-test", namespace=ns, agent="w", base_url=control_plane_url) as a:
        with pytest.raises(ValidationError) as e:
            await a.run.begin(goal="no session", budget_usd=1.0)
        assert "session_id" in str(e.value.detail)
        timed = await a.run.begin(goal="time only", budget_seconds=60)
        assert timed["status"] == "active"
