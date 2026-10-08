"""Operation trajectories are exportable per run."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from bench.op_trajectories import (
    attach_runs,
    choose_grouping,
    decile_profile,
    fold_trajectories,
    summarize,
)
from server.observability.events import AgentEvent
from server.observability.ledger import run_id_of


def _rows() -> list[dict[str, Any]]:
    t0 = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
    seq = [
        ("run-a", "s1", "memory.search", "10"),
        ("run-a", "s1", "memory.get", None),
        ("run-a", "s1", "memory.set", None),
        ("run-a", "s1", "memory.update", None),
        ("run-a", "s1", "working_memory.append", None),
        ("run-b", "s2", "memory.search", "30"),
        ("run-b", "s2", "memory.search", "30"),
        ("", "s3", "memory.get", None),  # no run: dropped under by=run
        ("", "s3", "memory.set", None),
        ("", "s3", "knowledge.search", None),  # not memory plane: always dropped
    ]
    out = []
    for i, (run, sid, op, k) in enumerate(seq):
        out.append(
            {
                "ts": t0 + timedelta(seconds=i),
                "run_id": run,
                "session_id": sid,
                "agent": "bot",
                "operation": op,
                "success": op != "memory.update",
                "latency_ms": 5,
                "top_k": k,
                "memory_key": "",
            }
        )
    return out


def test_fold_by_run_drops_runless_rows_and_orders_steps() -> None:
    ts = fold_trajectories(_rows(), by="run", min_ops=1)
    assert [t.key for t in ts] == ["run-a", "run-b"]
    a, b = ts
    assert a.sequence == "R R C U S"
    assert a.counts["read"] == 2 and a.counts["update"] == 1 and a.counts["scratchpad"] == 1
    assert a.reads_per_write == 1.0 and a.update_share == 0.5
    assert a.reads_before_first_write == 2 and a.modal_top_k == 10 and a.duration_s == 4.0
    assert a.row()["errors"] == 1
    assert b.writes == 0 and b.reads_per_write is None and b.sequence == "R R"


def test_fold_by_session_keeps_every_session_and_min_ops_filters() -> None:
    ts = fold_trajectories(_rows(), by="session", min_ops=2)
    assert [t.key for t in ts] == ["bot/s1", "bot/s2", "bot/s3"]
    assert ts[2].sequence == "R C"  # knowledge.search excluded
    assert [t.key for t in fold_trajectories(_rows(), by="session", min_ops=3)] == ["bot/s1"]


def test_attach_runs_joins_outcome_and_credit() -> None:
    ts = fold_trajectories(_rows(), by="run", min_ops=1)
    runs = [
        {
            "run_id": "run-a",
            "status": "completed",
            "memories_used": ["m1"],
            "memories_written": ["m2", "m3"],
        },
        {"run_id": "run-zzz", "status": "failed", "memories_used": [], "memories_written": []},
    ]
    usage = [
        {"memory_key": "m2", "event_type": "used", "n": 3},
        {"memory_key": "m3", "event_type": "harm", "n": 1},
        {"memory_key": "m1", "event_type": "evidence", "n": 2},  # read, not written
        {"memory_key": "m9", "event_type": "used", "n": 7},  # neither
    ]
    attach_runs(ts, runs, usage)
    a, b = ts
    assert a.outcome == "completed" and a.memories_used == 1 and a.memories_written == 2
    assert a.credit == {"used": 3, "harm": 1}
    assert a.credit_used == {"evidence": 2}
    assert a.row()["credit_used"] == {"evidence": 2}
    assert b.outcome == "unknown" and b.credit == {} and b.credit_used == {}


def test_summary_and_decile_profile() -> None:
    ts = fold_trajectories(_rows(), by="run", min_ops=1)
    attach_runs(
        ts,
        [{"run_id": "run-a", "status": "completed", "memories_used": [], "memories_written": []}],
        [],
    )
    s = summarize(ts)
    assert s["trajectories"] == 2 and s["grouping"] == "run" and s["ops_total"] == 7
    assert s["share_with_update"] == 0.5 and s["share_with_scratchpad"] == 0.5
    assert s["share_read_only"] == 0.5 and s["share_with_delete"] == 0.0
    assert s["outcomes"] == {"completed": 1, "unknown": 1}
    assert s["by_outcome"]["completed"]["share_with_update"] == 1.0
    prof = decile_profile(ts)
    assert len(prof) == 10
    assert prof[0] == {"read": 1.0}  # both trajectories open with a read
    assert prof[8] == {"scratchpad": 1.0}  # run-a's 5th of 5 steps lands in decile 8
    assert prof[9] == {}  # no trajectory has a step that late
    assert summarize([]) == {"trajectories": 0, "dropped_under_min_ops": 0}


def test_run_id_reaches_the_event_row() -> None:
    assert run_id_of({"run_id": "cc-abc"}) == "cc-abc"
    assert run_id_of({"run_id": 42}) == "42"
    assert run_id_of({"run_id": "-"}) == "" and run_id_of({"run_id": "  "}) == ""
    assert run_id_of({"run_id": True}) == "" and run_id_of({"run_id": ["x"]}) == ""
    assert run_id_of(None) == "" and run_id_of({}) == ""
    assert len(run_id_of({"run_id": "x" * 500})) == 128
    ev = AgentEvent(
        ts=datetime.now(UTC),
        org="o",
        namespace="n",
        agent="a",
        user_id="u",
        session_id="s",
        operation="memory.get",
        request_id="r",
        latency_ms=1,
        success=True,
        run_id="cc-abc",
    )
    row = ev.to_row()
    assert row["run_id"] == "cc-abc" and row["session_id"] == "s"


def test_gap_grouping_splits_one_identityless_caller_into_bursts() -> None:
    t0 = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
    rows = []
    # burst 1: three calls 1 s apart; pause 120 s; burst 2: two calls; another
    # agent interleaved must not split either burst.
    for off, agent, op in [
        (0, "clickgap", "memory.search"),
        (1, "clickgap", "memory.get"),
        (2, "clickgap", "memory.set"),
        (50, "other", "memory.search"),
        (122, "clickgap", "memory.search"),
        (123, "clickgap", "memory.get"),
    ]:
        rows.append(
            {
                "ts": t0 + timedelta(seconds=off),
                "run_id": "",
                "session_id": "-",
                "user_id": "-",
                "agent": agent,
                "operation": op,
                "success": True,
                "latency_ms": 1,
                "top_k": None,
                "memory_key": "",
            }
        )
    ts = fold_trajectories(rows, by="gap", min_ops=1, gap_seconds=60)
    assert [(t.agent, t.sequence) for t in ts] == [
        ("clickgap", "R R C"),
        ("other", "R"),
        ("clickgap", "R R"),
    ]
    assert ts[0].key.endswith("#1") and ts[2].key.endswith("#2")
    # a wider gap merges the two clickgap bursts
    assert [t.sequence for t in fold_trajectories(rows, by="gap", gap_seconds=600)] == [
        "R R C R R",
        "R",
    ]
    # session grouping drops the sentinel rows entirely
    assert fold_trajectories(rows, by="session") == []


def test_choose_grouping_prefers_runs_then_sessions_then_gaps() -> None:
    base = {"operation": "memory.get", "run_id": "", "session_id": "-"}
    assert choose_grouping([{**base, "run_id": "r1"}, base]) == "run"
    assert (
        choose_grouping([{**base, "session_id": "s"}, {**base, "session_id": "s"}, base])
        == "session"
    )
    assert choose_grouping([{**base, "session_id": "s"}, base, base]) == "gap"
    assert choose_grouping([{"operation": "knowledge.search", "run_id": "r1"}]) == "session"


def test_dropped_groups_are_counted_and_rendered() -> None:
    from bench.op_trajectories import render_markdown

    ts = fold_trajectories(_rows(), by="session", min_ops=3)  # s1 only; s2, s3 dropped
    assert len(ts) == 1 and fold_trajectories.dropped == 2  # type: ignore[attr-defined]
    s = summarize(ts, dropped=2)
    assert s["dropped_under_min_ops"] == 2
    md = render_markdown(s, ts, 5)
    assert "groups under --min-ops" in md and "| 1.00 |" in md
    empty = summarize([], dropped=60)
    assert empty == {"trajectories": 0, "dropped_under_min_ops": 60}
    assert "60 groups" in render_markdown(empty, [], 5)
