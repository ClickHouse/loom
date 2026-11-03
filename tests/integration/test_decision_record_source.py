"""decision.record writes the files it governs, its source and its class on the
decision's memory row, whose value reads as one statement; decision.outcome keeps
the source."""

from __future__ import annotations

import json
from typing import Any

import pytest

FILES = ["src/Processors/Transforms/ExternalDistinctTransform.h"]
SOURCE = "knowledge:code-repo/docs/spilling.md#Spilling DISTINCT"


_COLUMNS = (
    "value, files, source_resource_id, source_class, memory_type, tags, sensitivity,"
    " confidence, agent, user_id, session_id"
)


def _memory_row(ch: Any, agent: Any, key: str) -> dict[str, Any]:
    result = ch.query(
        f"""
        SELECT {_COLUMNS}
        FROM loom.memory FINAL
        WHERE org = {{org:String}} AND namespace = {{ns:String}} AND memory_key = {{key:String}}
          AND tombstoned_at IS NULL
        """,
        parameters={"org": agent.org, "ns": agent.namespace, "key": key},
    )
    assert len(result.result_rows) == 1, result.result_rows
    row = dict(zip(result.column_names, result.result_rows[0], strict=True))
    row["files"], row["tags"] = list(row["files"]), sorted(row["tags"])
    return row


@pytest.mark.asyncio
async def test_decision_row_carries_files_source_and_class(agent, ch_client) -> None:
    out = await agent.decision.record(
        category="design:spilling",
        scenario="how the final DISTINCT spills to disk",
        reasoning="sorted runs reuse the ORDER BY merge",
        outcome="spill into sorted runs and merge them",
        alternatives=["a hash set on disk — rejected: random reads"],
        files=FILES,
        source_resource_id=SOURCE,
        source_class="derived",
        tags=["team:db", "decision:owner:db"],
    )
    key = out["memory_key"]
    statement = (
        "how the final DISTINCT spills to disk. "
        "Decided spill into sorted runs and merge them because sorted runs reuse the "
        "ORDER BY merge. Rejected: a hash set on disk — rejected: random reads."
    )
    tags = ["decision", "decision:category:design:spilling", "decision:owner:db", "team:db"]
    expected = {
        "value": json.dumps(statement),
        "files": FILES,
        "source_resource_id": SOURCE,
        "source_class": "derived",
        "memory_type": "decision",
        "tags": sorted(tags),
        "sensitivity": "",
        "confidence": 1.0,
        "agent": agent.agent,
        "user_id": "-",
        "session_id": "-",
    }
    assert _memory_row(ch_client, agent, key) == expected

    # The realized outcome names another file; the rewrite keeps the files, the source, the
    # class, the caller's tags and the recording identity.
    realized = "spills stayed under the limit after src/Interpreters/Aggregator.cpp changed"
    await agent.decision.outcome(key, realized, success=True)
    statement += f" Realized: {realized} (succeeded)."
    tags += ["decision:outcome-known", "decision:success"]
    assert _memory_row(ch_client, agent, key) == expected | {
        "value": json.dumps(statement),
        "tags": sorted(tags),
    }


@pytest.mark.asyncio
async def test_decisions_that_read_alike_each_keep_a_live_row(agent, ch_client) -> None:
    # The write-time verdicts only: the conflict resolver is off by default
    # (memory_conflict_resolver_enabled) and the CI server runs no workers, so nothing
    # judges these rows after they are written.
    head = "how the nightly compaction schedules its merges " * 30
    # A reason long enough that the shared statement, not the key each embedded text
    # ends with, decides how close two of these decisions are.
    reasoning = "a rollback takes two minutes and the previous image is known to be good"
    base = dict(reasoning=reasoning, outcome="roll back")
    keys = [
        (await agent.decision.record(category=c, scenario=s, **base))["memory_key"]
        for c, s in (
            # The same statement: the category is in the key, not the text.
            ("deploy", "a bad image"),
            ("storage", "a bad image"),
            # These two differ only past the 1,200 characters the embedding reads.
            ("deploy", f"{head} on monday"),
            ("deploy", f"{head} on friday"),
            ("ci", "a flaky runner"),
            ("db", "a flaky runner"),
            ("deploy", "a bad image in production"),
        )
    ]
    rows = ch_client.query(
        """
        SELECT memory_key, status, toString(memory_id), arrayMap(x -> toString(x), conflict_with)
        FROM loom.memory FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
          AND memory_key IN {keys:Array(String)} AND tombstoned_at IS NULL
        """,
        parameters={"org": agent.org, "ns": agent.namespace, "keys": keys},
    ).result_rows
    status = {key: st for key, st, _, _ in rows}
    assert len(set(keys)) == 7 and set(status) == set(keys), rows
    # Each decision keeps a live row: active, or conflicted beside a close peer.
    assert set(status.values()) <= {"active", "conflicted"}, rows
    # The rule fired: a conflicted row is cross-linked to another of these decisions.
    ids = {mid for _, _, mid, _ in rows}
    linked = [(mid, peers) for _, st, mid, peers in rows if st == "conflicted"]
    assert linked, rows
    assert all(peers and set(peers) <= ids - {mid} for mid, peers in linked), rows


@pytest.mark.asyncio
async def test_precedents_find_a_decision_past_its_embedded_excerpt(agent) -> None:
    # The scenario alone fills the first 1,200 stored characters, so the outcome and the
    # reasoning are found through the statement's chunks, not its head. A distractor with
    # the same scenario, so the same head, decides something else.
    scenario = "how the nightly compaction schedules its merges " * 30
    assert len(scenario) > 1200
    distractor = await agent.decision.record(
        category="design:scheduler",
        scenario=scenario,
        outcome="run the merges in key order",
        reasoning="key order keeps the parts sorted for the next read",
    )
    out = await agent.decision.record(
        category="design:allocator",
        scenario=scenario,
        outcome="keep a per-thread arena for spilled blocks",
        reasoning="a shared arena fragments under concurrent spills and stalls the merge",
    )
    found = await agent.decision.precedents(
        "per-thread arena for spilled blocks fragments under concurrent spills", top_k=5
    )
    keys = [p["memory_key"] for p in found["precedents"]]
    assert keys and keys[0] == out["memory_key"], found
    # The distractor shares the head, so it is found too, below the target.
    assert distractor["memory_key"] in keys[1:], found
