"""Decisions as first-class records: key derivation, field shaping, the causal walk and
the four handlers over stubbed planes. The clickhouse-local tests of the record's
retirement and the outcome's write are in test_decision_retirement.py."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from server.memory.app import decisions as svc
from server.memory.domain.decision_schema import SCHEMA_FIELDS
from server.memory.domain.edge_relations import canonical_link_relation
from tests.unit.clickhouse_local import ddl


def test_decision_key_is_the_record_plane_identity_key() -> None:
    k1 = svc.decision_key("deploy", "Roll back or hotfix the 26.5 image?")
    k2 = svc.decision_key("Deploy", "roll back or hotfix the 26.5 image")
    assert k1.startswith("decision:") and k1 == k2  # canonicalised identity
    assert svc.decision_key("deploy", "another scenario") != k1


def test_record_fields_mark_absent_optionals_and_join_alternatives() -> None:
    f = svc.record_fields(
        {
            "category": "deploy",
            "scenario": "s",
            "reasoning": "r",
            "outcome": "rollback",
            "alternatives": ["hotfix", "wait"],
            "confidence": 0.8,
        }
    )
    assert f["alternatives"] == {"value": "hotfix\nwait", "state": "value"}
    assert f["realized_outcome"] == {"state": "absent"} and f["success"] == {"state": "absent"}
    assert f["confidence"]["value"] == 0.8
    assert set(f) == {s["name"] for s in SCHEMA_FIELDS}


def test_clean_alternatives_dedupes_and_caps() -> None:
    assert svc.clean_alternatives([" Hotfix ", "hotfix", "", "wait"]) == ["Hotfix", "wait"]
    assert len(svc.clean_alternatives([f"a{i}" for i in range(20)])) == svc.MAX_ALTERNATIVES


def test_influenced_is_a_canonical_verb_and_influenced_by_flips() -> None:
    assert canonical_link_relation("influences") == ("influenced", False)
    assert canonical_link_relation("influenced_by") == ("influenced", True)


@pytest.mark.asyncio
async def test_walk_chain_follows_direction_and_dedupes() -> None:
    # a -caused-> d ; b -influenced-> d ; d -caused-> e ; e -caused-> f ; a -caused-> e
    edges = [
        ("a", "d", "caused"),
        ("b", "d", "influenced"),
        ("d", "e", "caused"),
        ("e", "f", "caused"),
        ("a", "e", "caused"),
    ]

    async def fetch(frontier, direction):
        if direction == "upstream":
            return [e for e in edges if e[1] in frontier]
        return [e for e in edges if e[0] in frontier]

    up = await svc.walk_chain("d", fetch=fetch, direction="upstream", max_hops=3)
    assert [(n.memory_key, n.hop, n.relation) for n in up] == [
        ("a", 1, "caused"),
        ("b", 1, "influenced"),
    ]
    down = await svc.walk_chain("d", fetch=fetch, direction="downstream", max_hops=3)
    assert [(n.memory_key, n.hop) for n in down] == [("e", 1), ("f", 2)]
    both = await svc.walk_chain("d", fetch=fetch, direction="both", max_hops=1)
    assert {n.memory_key for n in both} == {"a", "b", "e"}
    capped = await svc.walk_chain("d", fetch=fetch, direction="downstream", max_hops=3, max_nodes=1)
    assert len(capped) == 1


class _CH:
    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}
        self.edges: list[tuple[str, str, str]] = []
        self.run_used: list[str] = []
        self.queries: list[str] = []

    async def query_all(self, sql, params=None):
        self.queries.append(sql)
        if "FROM loom.memory_records" in sql:
            return [
                {"memory_key": k, "record_json": json.dumps(v), "created_at": datetime(2026, 1, 1)}
                for k, v in self.records.items()
                if k in (params or {}).get("keys", [])
            ]
        if "FROM loom.operator_edges" in sql:
            col_to = "to_key IN" in sql
            keys = set((params or {}).get("keys", []))
            return [
                {"from_key": a, "to_key": b, "relation": r}
                for a, b, r in self.edges
                if (b in keys if col_to else a in keys)
            ]
        return []

    async def query_one(self, sql, params=None):
        self.queries.append(sql)
        if "FROM loom.agent_runs" in sql:
            return {"memories_used": list(self.run_used)}
        if "FROM loom.memory FINAL" in sql and "memory_type = 'decision'" in sql:
            rec = self.records.get((params or {}).get("key"))
            if rec is None:
                return None
            return {
                "identity_agent": "claude",
                "user_id": "-",
                "session_id": "s1",
                "record_json": json.dumps(rec),
                "created_at": datetime(2026, 1, 1),
            }
        return None


@pytest.fixture
def env(monkeypatch):
    from server.memory.app import decision as h

    ch = _CH()
    calls: dict[str, list[Any]] = {
        "record": [],
        "set": [],
        "link": [],
        "search": [],
        "outcome_run": [],
        "schema": [],
    }

    async def fake_schema_by_name(org, ns, name):
        return {"status": "active"} if calls["schema"] else None

    async def fake_register(inp):
        calls["schema"].append(inp)
        return SimpleNamespace(schema_id="sid")

    async def fake_record_write(inp):
        calls["record"].append(inp)
        ch.records[inp.memory_key] = {
            k: (v.value if hasattr(v, "value") else v.get("value")) for k, v in inp.fields.items()
        }
        return SimpleNamespace(record_id="rec-1")

    async def fake_set(inp):
        calls["set"].append(inp)
        return SimpleNamespace(version=1, conflict_verdict="clear")

    async def fake_link(inp):
        calls["link"].append(inp)
        ch.edges.append((inp.from_key, inp.to_key, inp.relation))
        return SimpleNamespace(linked=True)

    async def fake_search(inp):
        calls["search"].append(inp)
        hits = [
            SimpleNamespace(memory_key=k, similarity=0.9 - 0.1 * i)
            for i, k in enumerate(ch.records)
        ]
        return SimpleNamespace(results=hits, embedding_model="fake")

    async def fake_tombstone(*_a, **_k):
        return None

    async def fake_outcome_run(inp):
        calls["outcome_run"].append(inp)
        return SimpleNamespace(applied=len(inp.memory_keys))

    import server.memory.app.ops as mops
    import server.memory.app.records as hrec
    import server.memory.app.search as msearch
    from server.memory.app import write as write_mod

    monkeypatch.setattr(hrec, "_schema_by_name", fake_schema_by_name)
    monkeypatch.setattr(hrec, "handle_memory_schema_register", fake_register)
    monkeypatch.setattr(hrec, "handle_memory_record_write", fake_record_write)
    monkeypatch.setattr(write_mod, "handle_memory_set", fake_set)
    monkeypatch.setattr("server.memory.app.write.handle_memory_set", write_mod.handle_memory_set)
    monkeypatch.setattr(mops, "handle_memory_link", fake_link)
    monkeypatch.setattr(mops, "handle_memory_outcome_run", fake_outcome_run)
    monkeypatch.setattr(msearch, "handle_memory_search", fake_search)

    async def fake_get_by_key(identity, memory_key, **_kw):
        # The writer's live row: the last memory.set under the key.
        sets = [c for c in calls["set"] if c.memory_key == memory_key]
        return SimpleNamespace(value=sets[-1].value) if sets else None

    monkeypatch.setattr(h, "tombstone_decision_record", fake_tombstone)
    monkeypatch.setattr(h, "get_by_key", fake_get_by_key)
    with patch.object(h, "get_client", return_value=ch):
        yield ch, calls, h


def _record_input(**over: Any):
    from server.memory.api.models_decision import DecisionRecordInput

    base: dict[str, Any] = dict(
        org="o",
        namespace="n",
        agent="claude",
        run_id="r1",
        category="Deploy",
        scenario="Roll back or hotfix the 26.5 image?",
        reasoning="Rollback is 2 minutes; the fix is untested.",
        outcome="rollback",
        alternatives=["hotfix forward", "wait"],
        confidence=0.8,
        caused_by=["ci:flaky-runner"],
        influenced_by=["policy:no-friday-deploys", ""],
    )
    base.update(over)
    return DecisionRecordInput(**base)


def test_record_writes_schema_once_record_memory_and_edges(env) -> None:
    ch, calls, h = env
    out = asyncio.run(h.handle_decision_record(_record_input()))
    key = svc.decision_key("Deploy", "Roll back or hotfix the 26.5 image?")
    assert out.memory_key == key and out.record_id == "rec-1" and out.superseded_prior is False
    assert len(calls["schema"]) == 1 and calls["schema"][0].name == "decision"
    assert [f.name for f in calls["schema"][0].fields] == [s["name"] for s in SCHEMA_FIELDS]
    rec = calls["record"][0]
    assert rec.schema_name == "decision" and rec.memory_key == key
    assert rec.fields["alternatives"].value == "hotfix forward\nwait"
    mem = calls["set"][0]
    assert mem.memory_type == "decision" and mem.memory_key == key and mem.run_id == "r1"
    assert mem.value["outcome"] == "rollback" and mem.value["alternatives"] == [
        "hotfix forward",
        "wait",
    ]
    assert "decision:category:deploy" in mem.tags and mem.source_trace == "run:r1"
    assert out.edges_written == 2
    assert [(ln.from_key, ln.relation) for ln in calls["link"]] == [
        ("ci:flaky-runner", "caused"),
        ("policy:no-friday-deploys", "influenced"),
    ]
    assert all(ln.to_key == key and ln.run_id == "r1" for ln in calls["link"])
    assert "decision.outcome" in out.next
    # second write for the same scenario: schema not re-registered, prior reported
    again = asyncio.run(h.handle_decision_record(_record_input(outcome="hotfix forward")))
    assert again.superseded_prior is True and len(calls["schema"]) == 1


def test_precedents_search_decisions_and_join_records(env) -> None:
    from server.memory.api.models_decision import DecisionPrecedentsInput

    ch, calls, h = env
    asyncio.run(h.handle_decision_record(_record_input()))
    asyncio.run(
        h.handle_decision_record(
            _record_input(scenario="Pin or float the runner image?", outcome="pin")
        )
    )
    out = asyncio.run(
        h.handle_decision_precedents(
            DecisionPrecedentsInput(
                org="o", namespace="n", query="bad image in production", category="deploy", top_k=5
            )
        )
    )
    assert calls["search"][0].memory_type == "decision"
    assert calls["search"][0].tags == ["decision:category:deploy"]
    assert [p.outcome for p in out.precedents] == ["rollback", "pin"]
    assert out.precedents[0].alternatives == ["hotfix forward", "wait"]
    assert out.precedents[0].similarity == pytest.approx(0.9)
    none = asyncio.run(
        h.handle_decision_precedents(
            DecisionPrecedentsInput(org="o", namespace="n", query="x", with_outcome_only=True)
        )
    )
    assert none.precedents == []  # no realized outcomes yet


def test_chain_walks_authored_edges_and_attaches_decisions(env) -> None:
    from server.memory.api.models_decision import DecisionChainInput

    ch, calls, h = env
    d = asyncio.run(h.handle_decision_record(_record_input()))
    ch.edges.append((d.memory_key, "incident:2026-09-11", "caused"))
    out = asyncio.run(
        h.handle_decision_chain(
            DecisionChainInput(
                org="o", namespace="n", memory_key=d.memory_key, direction="both", max_hops=2
            )
        )
    )
    assert out.seed_decision is not None and out.seed_decision.outcome == "rollback"
    got = {(n.memory_key, n.direction, n.relation) for n in out.nodes}
    assert got == {
        ("ci:flaky-runner", "upstream", "caused"),
        ("policy:no-friday-deploys", "upstream", "influenced"),
        ("incident:2026-09-11", "downstream", "caused"),
    }
    assert all(n.decision is None for n in out.nodes)  # none of those are decisions


def test_outcome_revises_the_record_and_credits_the_run(env) -> None:
    from server.memory.api.models_decision import DecisionOutcomeInput

    ch, calls, h = env
    d = asyncio.run(h.handle_decision_record(_record_input()))
    ch.run_used = ["ci:flaky-runner", "runbook:rollback"]
    out = asyncio.run(
        h.handle_decision_outcome(
            DecisionOutcomeInput(
                org="o",
                namespace="n",
                memory_key=d.memory_key,
                realized_outcome="rollback took 90s; no user impact",
                success=True,
            )
        )
    )
    assert out.updated and out.memories_credited == 2
    rec = calls["record"][-1]
    assert rec.fields["realized_outcome"].value.startswith("rollback took")
    assert rec.fields["success"].value is True and rec.fields["outcome"].value == "rollback"
    mem = calls["set"][-1]
    assert (
        mem.value["realized_outcome"].startswith("rollback took") and mem.value["success"] is True
    )
    assert "decision:success" in mem.tags and "decision:outcome-known" in mem.tags
    run_call = calls["outcome_run"][0]
    assert run_call.outcome == "positive" and run_call.memory_keys == [
        "ci:flaky-runner",
        "runbook:rollback",
    ]
    assert run_call.run_id == "r1"


def test_a_merge_hint_names_the_key_or_asks_for_the_op_again() -> None:
    from server.memory.app.decision import _merged_hint

    assert "decision:peer" in _merged_hint("decision:peer", "decision.outcome")
    assert "Use that key" in _merged_hint("decision:peer", "decision.outcome")
    assert "Call decision.outcome again" in _merged_hint("", "decision.outcome")


def test_the_writer_read_and_the_tombstone_are_sequential() -> None:
    # Pinned by text: a single-node engine treats the setting as a no-op, so no local
    # test can show a replica missing the delete's tombstone.
    from server.memory.store import records

    sequential = "SETTINGS select_sequential_consistency = 1"
    assert records._LIVE_WRITER_RECORD.rstrip().endswith(sequential)
    assert records._TOMBSTONE_DECISION_RECORD.rstrip().endswith(sequential)


def test_outcome_on_unknown_decision_is_not_found(env) -> None:
    from server.core.errors import NotFoundError
    from server.memory.api.models_decision import DecisionOutcomeInput

    _, _, h = env
    with pytest.raises(NotFoundError):
        asyncio.run(
            h.handle_decision_outcome(
                DecisionOutcomeInput(
                    org="o", namespace="n", memory_key="decision:nope", realized_outcome="x"
                )
            )
        )


def test_ops_registered_and_search_accepts_decision_type() -> None:
    from server.memory.api.models_search import MemorySearchInput
    from server.operations.base import Role
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.operations.toolsets import _MCP_HIDDEN_OPS

    for name in ("decision.record", "decision.precedents", "decision.chain", "decision.outcome"):
        assert name in OPERATIONS_BY_NAME and name not in _MCP_HIDDEN_OPS
    assert OPERATIONS_BY_NAME["decision.record"].required_role == Role.WRITER
    assert (
        MemorySearchInput(org="o", namespace="n", query="q", memory_type="decision").memory_type
        == "decision"
    )


def test_a_failing_closing_check_still_records_the_decision(env, monkeypatch) -> None:
    _, calls, h = env

    async def _fails(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("timeout")

    monkeypatch.setattr(h, "tombstone_decision_record", _fails)
    out = asyncio.run(h.handle_decision_record(_record_input()))
    assert out.record_id == "rec-1" and len(calls["record"]) == 1 and len(calls["set"]) == 1


def test_decision_ops_do_not_honour_a_read_scope() -> None:
    # decision.outcome reads the writer's identity across every user of the key, which a
    # user- or session-scoped token must not see; the transport gate refuses a token bound
    # below the namespace for every op that does not honour a read scope.
    from server.identity.domain.read_scope import honours_read_scope

    for name in ("decision.record", "decision.precedents", "decision.chain", "decision.outcome"):
        assert not honours_read_scope(name), name


def test_record_status_values_match_the_table_enum() -> None:
    from typing import get_args

    from server.memory.api.models_records import MemoryRecordRow

    enum = re.search(r"`status` Enum8\(([^)]*)\)", ddl("memory_records"))
    assert enum is not None
    in_table = re.findall(r"'(\w+)' = \d+", enum.group(1))
    in_model = get_args(MemoryRecordRow.model_fields["status"].annotation)
    assert tuple(in_table) == in_model == ("active", "superseded", "rejected", "tombstoned")
