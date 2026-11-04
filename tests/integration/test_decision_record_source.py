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


@pytest.mark.asyncio
async def test_precedents_find_a_cyrillic_decision_past_its_embedded_excerpt(agent) -> None:
    # Stored as JSON, each Cyrillic letter is a six-character escape: a 250-character
    # scenario fills the excerpt, and the decision is found through its chunks.
    scenario = "MergeTree: " + "как ночное слияние планирует свои части " * 6
    assert len(scenario) < 1200 < len(json.dumps(scenario))
    distractor = await agent.decision.record(
        category="design:scheduler-ru",
        scenario=scenario,
        outcome="сливать части по порядку ключа",
        reasoning="порядок ключа сохраняет части отсортированными",
    )
    out = await agent.decision.record(
        category="design:arena-ru",
        scenario=scenario,
        outcome="держать отдельную арену для каждого потока",
        reasoning="общая арена фрагментируется при одновременных сбросах",
    )
    found = await agent.decision.precedents(
        "отдельная арена для каждого потока фрагментируется при сбросах", top_k=5
    )
    keys = [p["memory_key"] for p in found["precedents"]]
    assert keys and keys[0] == out["memory_key"], found
    assert distractor["memory_key"] in keys[1:], found


@pytest.mark.asyncio
async def test_a_scenario_without_latin_letters_records_and_punctuation_is_refused(
    agent, ch_client
) -> None:
    from loom.errors import LoomError

    base = dict(reasoning="журналы нужны для разбора инцидентов", outcome="хранить 30 дней")
    first = await agent.decision.record(
        category="хранение", scenario="Срок хранения журналов", **base
    )
    again = await agent.decision.record(
        category="Хранение", scenario="срок хранения журналов!", **base
    )
    other = await agent.decision.record(
        category="хранение", scenario="Срок хранения отчётов", **base
    )
    key = first["memory_key"]
    assert again["memory_key"] == key and again["superseded_prior"] is True
    assert other["memory_key"] != key
    assert _memory_row(ch_client, agent, key)["memory_type"] == "decision"
    with pytest.raises(LoomError) as excinfo:
        await agent.decision.record(category="хранение", scenario="?! …", **base)
    assert "MISSING_IDENTITY_FIELD" in str(excinfo.value), excinfo.value


@pytest.mark.asyncio
async def test_a_proposed_decision_is_tagged_worded_and_served_as_proposed(
    agent, ch_client
) -> None:
    body = dict(
        category="design:join",
        scenario="how the hash join spills when the right side outgrows memory",
        reasoning="partitions bound the memory a build holds",
        outcome="spill the build side by hash partitions",
    )
    key = (await agent.decision.record(**body, stage="proposed"))["memory_key"]
    said = (
        "how the hash join spills when the right side outgrows memory. {} spill the build "
        "side by hash partitions because partitions bound the memory a build holds."
    )
    tags = ["decision", "decision:category:design:join"]
    expected = {
        "value": json.dumps(said.format("Proposed")),
        "files": [],
        "source_resource_id": "",
        "source_class": "asserted",
        "memory_type": "decision",
        "tags": sorted([*tags, "decision:proposed"]),
        "sensitivity": "",
        "confidence": 1.0,
        "agent": agent.agent,
        "user_id": "-",
        "session_id": "-",
    }
    assert _memory_row(ch_client, agent, key) == expected
    found = await agent.decision.precedents("hash join spills by partitions")
    assert {p["memory_key"]: p["stage"] for p in found["precedents"]}.get(key) == "proposed"
    assert (await agent.decision.chain(key))["seed_decision"]["stage"] == "proposed"
    pack = await agent.transport.post(
        "/v1/context.pack",
        {
            "org": agent.org,
            "namespace": agent.namespace,
            "agent": agent.agent,
            "query": "how the hash join spills",
        },
    )
    assert "→ proposed: spill the build side by hash partitions" in pack["sections"]["decisions"]

    # Recorded again as decided: tag and wording go.
    await agent.decision.record(**body)
    decided = expected | {"value": json.dumps(said.format("Decided")), "tags": sorted(tags)}
    assert _memory_row(ch_client, agent, key) == decided
    assert (await agent.decision.chain(key))["seed_decision"]["stage"] == "decided"

    # Proposed again, then an outcome: the decision was acted on, so the stage goes.
    await agent.decision.record(**body, stage="proposed")
    await agent.decision.outcome(key, "a partitioned build shipped", success=True)
    realized = said.format("Decided") + " Realized: a partitioned build shipped (succeeded)."
    assert _memory_row(ch_client, agent, key) == decided | {
        "value": json.dumps(realized),
        "tags": sorted([*tags, "decision:outcome-known", "decision:success"]),
    }
