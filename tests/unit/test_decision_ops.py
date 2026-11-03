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
from server.memory.domain import decision_statement as stmt
from server.memory.domain.decision_schema import SCHEMA_FIELDS
from server.memory.domain.edge_relations import canonical_link_relation
from server.memory.domain.files import detect_value_files
from server.memory.domain.sensitivity import detect_sensitivity
from tests.unit.clickhouse_local import ddl
from tests.unit.decision_inputs import record_input


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
    # A long alternative is cut at a space and marked; one first word longer than the
    # limit leaves the mark alone.
    words = "w " * 200
    (cut,) = svc.clean_alternatives([words])
    assert cut == "w " * 149 + "w…" and len(cut) <= svc.ALTERNATIVE_CHARS
    assert svc.clean_alternatives(["wait", "z" * 400]) == ["wait", "…"]
    # A path straddling the cut is never kept in half (`LruCache.c` of `LruCache.cpp`), so
    # neither the record's files nor the statement's name a file the decision never names.
    long = "y" * 279 + " src/cache/LruCache.cpp is slow"
    assert long[: svc.ALTERNATIVE_CHARS].endswith(" src/cache/LruCache.c")
    (cut,) = svc.clean_alternatives([long])
    assert cut == "y" * 279 + "…"
    assert svc.decision_files({"alternatives": [cut]}) is None
    assert detect_value_files(stmt.memory_value({"outcome": "o", "alternatives": [cut]})) == []
    # Unspaced CJK is cut between two wide characters, so two options that differ after
    # the limit's first 299 characters are one, and two that differ before are two.
    cjk = "使用哈希集合存储去重结果" * 30
    (cut,) = svc.clean_alternatives([cjk])
    assert cut == cjk[: svc.ALTERNATIVE_CHARS - 1] + "…"
    assert len(svc.clean_alternatives([cjk + "甲", cjk + "乙"])) == 1
    assert len(svc.clean_alternatives(["甲" + cjk, "乙" + cjk])) == 2
    # Unspaced Thai has no wide characters and no space: one token past the limit, it
    # leaves the mark alone, as a long path does.
    assert svc.clean_alternatives(["ใช้ตารางแฮช" * 40]) == ["…"]
    # An alternative that fits is kept as written.
    fits = "y" * 270 + " src/cache/LruCache.cpp"
    assert svc.clean_alternatives([fits]) == [fits]
    assert svc.decision_files({"alternatives": [fits]}) == ["src/cache/LruCache.cpp"]


def test_the_record_holds_long_alternatives_cut_at_their_cap(env) -> None:
    _, calls, h = env
    words = "w " * 200
    asyncio.run(h.handle_decision_record(record_input(alternatives=[words, "z" * 400])))
    stored = calls["record"][-1].fields["alternatives"].value.split("\n")
    assert stored == ["w " * 149 + "w…", "…"]


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
        self.blocked: list[str] = []  # blacklisted phrases
        self.identities: dict[str, tuple[str, str, str]] = {}
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
            (key,) = (params or {})["keys"]
            rec = self.records.get(key)
            if rec is None:
                return None
            agent, user_id, session_id = self.identities[key]
            return {
                "identity_agent": agent,
                "user_id": user_id,
                "session_id": session_id,
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
        ch.identities[inp.memory_key] = (inp.agent, inp.user_id, inp.session_id)
        ch.records[inp.memory_key] = {
            k: (v.value if hasattr(v, "value") else v.get("value")) for k, v in inp.fields.items()
        }
        return SimpleNamespace(record_id="rec-1")

    async def fake_set(inp):
        calls["set"].append(inp)
        return SimpleNamespace(version=1)

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

    async def fake_get_by_key(identity, memory_key, **kw):
        # The writer's live row: the last memory.set under the key.
        sets = [c for c in calls["set"] if c.memory_key == memory_key]
        if not sets:
            return None
        last = sets[-1]
        # The outcome must read the row under the identity the decision was recorded with.
        assert (identity.agent, identity.user_id, identity.session_id) == (
            last.agent,
            last.user_id,
            last.session_id,
        )
        return SimpleNamespace(
            value=last.value,
            pinned=False,
            files=list(last.files or []),
            source_class=last.source_class or "asserted",
            source_resource_id=last.source_resource_id,
            tags=list(last.tags),
        )

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
    import server.memory.app.blacklist_guard as blacklist

    async def fake_find_match(*, org, namespace, value):
        hit = any(re.search(p, value) for p in ch.blocked)
        return SimpleNamespace(pattern_id="bl-1", scope="namespace", reason="r") if hit else None

    async def fake_note_hit(pattern_id):
        return None

    monkeypatch.setattr(blacklist, "find_match", fake_find_match)
    monkeypatch.setattr(blacklist, "note_hit", fake_note_hit)
    monkeypatch.setattr(h, "tombstone_decision_records", fake_tombstone)
    monkeypatch.setattr(h, "get_by_key", fake_get_by_key)
    with patch.object(h, "get_client", return_value=ch):
        yield ch, calls, h


def test_record_writes_schema_once_record_memory_and_edges(env) -> None:
    ch, calls, h = env
    out = asyncio.run(h.handle_decision_record(record_input()))
    key = svc.decision_key("Deploy", "Roll back or hotfix the 26.5 image?")
    assert out.memory_key == key and out.record_id == "rec-1" and out.superseded_prior is False
    assert len(calls["schema"]) == 1 and calls["schema"][0].name == "decision"
    assert [f.name for f in calls["schema"][0].fields] == [s["name"] for s in SCHEMA_FIELDS]
    rec = calls["record"][0]
    assert rec.schema_name == "decision" and rec.memory_key == key
    assert rec.fields["alternatives"].value == "hotfix forward\nwait"
    mem = calls["set"][0]
    assert mem.memory_type == "decision" and mem.memory_key == key and mem.run_id == "r1"
    assert mem.value.startswith(
        "Roll back or hotfix the 26.5 image? Decided rollback because Rollback is"
    )
    assert "Rejected: hotfix forward; wait." in mem.value
    assert "decision:category:deploy" in mem.tags and mem.source_trace == "run:r1"
    assert out.edges_written == 2
    assert [(ln.from_key, ln.relation) for ln in calls["link"]] == [
        ("ci:flaky-runner", "caused"),
        ("policy:no-friday-deploys", "influenced"),
    ]
    assert all(ln.to_key == key and ln.run_id == "r1" for ln in calls["link"])
    assert "decision.outcome" in out.next
    # second write for the same scenario: schema not re-registered, prior reported
    again = asyncio.run(h.handle_decision_record(record_input(outcome="hotfix forward")))
    assert again.superseded_prior is True and len(calls["schema"]) == 1


def test_precedents_search_decisions_and_join_records(env) -> None:
    from server.memory.api.models_decision import DecisionPrecedentsInput

    ch, calls, h = env
    asyncio.run(h.handle_decision_record(record_input()))
    asyncio.run(
        h.handle_decision_record(
            record_input(scenario="Pin or float the runner image?", outcome="pin")
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


def test_a_decision_served_in_two_sessions_is_one_precedent(env, monkeypatch) -> None:
    import server.memory.app.search as msearch
    from server.memory.api.models_decision import DecisionPrecedentsInput

    ch, calls, h = env
    asyncio.run(h.handle_decision_record(record_input()))
    (key,) = ch.records

    asked: list[int] = []

    async def two_rows(inp):
        asked.append(inp.top_k)
        hits = [SimpleNamespace(memory_key=key, similarity=s) for s in (0.9, 0.8)]
        return SimpleNamespace(results=hits, embedding_model="fake")

    monkeypatch.setattr(msearch, "handle_memory_search", two_rows)
    out = asyncio.run(
        h.handle_decision_precedents(DecisionPrecedentsInput(org="o", namespace="n", query="q"))
    )
    assert [p.similarity for p in out.precedents] == [pytest.approx(0.9)]
    assert asked == [30]  # ten precedents at three rows each


def test_chain_walks_authored_edges_and_attaches_decisions(env) -> None:
    from server.memory.api.models_decision import DecisionChainInput

    ch, calls, h = env
    d = asyncio.run(h.handle_decision_record(record_input()))
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
    d = asyncio.run(h.handle_decision_record(record_input()))
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
    assert mem.value.endswith("Realized: rollback took 90s; no user impact (succeeded).")
    assert "decision:success" in mem.tags and "decision:outcome-known" in mem.tags
    assert mem.source_class is None  # the live row is asserted: nothing to carry
    run_call = calls["outcome_run"][0]
    assert run_call.outcome == "positive" and run_call.memory_keys == [
        "ci:flaky-runner",
        "runbook:rollback",
    ]
    assert run_call.run_id == "r1"


def test_the_writer_read_and_the_tombstone_are_sequential() -> None:
    # Pinned by text: a single-node engine treats the setting as a no-op, so no local
    # test can show a replica missing the delete's tombstone.
    from server.memory.store import records

    sequential = "SETTINGS select_sequential_consistency = 1"
    assert records._LIVE_WRITER_RECORD.rstrip().endswith(sequential)
    assert records._TOMBSTONE_DECISION_RECORDS.rstrip().endswith(sequential)


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

    monkeypatch.setattr(h, "tombstone_decision_records", _fails)
    out = asyncio.run(h.handle_decision_record(record_input()))
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


def test_record_passes_files_source_and_class_to_the_memory_row(env) -> None:
    from server.memory.api.models_decision import DecisionOutcomeInput

    ch, calls, h = env
    files = ["src/Processors/Transforms/ExternalDistinctTransform.h"]
    source = "knowledge:code-repo/docs/spilling.md#Spilling DISTINCT"
    d = asyncio.run(
        h.handle_decision_record(
            record_input(
                files=files,
                source_resource_id=source,
                source_class="derived",
                tags=["team:db", "decision:owner:db", "decision:category:legacy"],
            )
        )
    )
    mem = calls["set"][0]
    assert "team:db" in mem.tags
    assert mem.files == files
    assert mem.source_resource_id == source
    assert mem.source_class == "derived"
    # An outcome rewrites the memory row; it keeps the files, even when the realized
    # outcome names another one, the citation and the declared class.
    asyncio.run(
        h.handle_decision_outcome(
            DecisionOutcomeInput(
                org="o",
                namespace="n",
                memory_key=d.memory_key,
                realized_outcome="the fix landed in src/Interpreters/Aggregator.cpp",
            )
        )
    )
    after = calls["set"][-1]
    assert after.files == files
    assert after.source_resource_id == source and after.source_class == "derived"
    # The caller's tags stay beside the rebuilt `decision` ones, each once, including a
    # caller tag that happens to start with `decision:`.
    assert {"team:db", "decision:owner:db", "decision:outcome-known"} <= set(after.tags)
    assert "decision:category:legacy" in after.tags
    assert after.tags.count("decision") == 1


def test_record_input_refuses_a_class_the_body_may_not_declare() -> None:
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        record_input(source_class="asserted")


def test_outcome_reads_and_rewrites_the_row_of_the_recording_identity(env) -> None:
    from server.memory.api.models_decision import DecisionOutcomeInput

    _, calls, h = env
    d = asyncio.run(
        h.handle_decision_record(
            record_input(user_id="u7", session_id="s9", source_resource_id="doc:1")
        )
    )
    asyncio.run(
        h.handle_decision_outcome(
            DecisionOutcomeInput(
                org="o", namespace="n", memory_key=d.memory_key, realized_outcome="done"
            )
        )
    )
    after = calls["set"][-1]
    assert (after.agent, after.user_id, after.session_id) == ("claude", "u7", "s9")
    assert after.source_resource_id == "doc:1"


def test_files_are_detected_from_the_decisions_fields(env) -> None:
    _, calls, h = env
    path = "src/Interpreters/GraceHashJoin.cpp"
    # The statement holds the whole reasoning, path included; the row's files name it.
    asyncio.run(h.handle_decision_record(record_input(reasoning="x " * 200 + f"see {path}")))
    assert path in calls["set"][-1].value
    assert calls["set"][-1].files == [path]
    # A name the detector does not take (`.mdx`) names no file.
    asyncio.run(
        h.handle_decision_record(record_input(scenario="fourth", reasoning="docs/guide.mdx"))
    )
    written = calls["set"][-1]
    assert written.files is None and detect_value_files(written.value) == []
    # Text naming no file passes None, so a re-record keeps the row's files.
    asyncio.run(h.handle_decision_record(record_input(scenario="third")))
    assert calls["set"][-1].files is None


def _commit_verdict(first: str, second: str, *, policy: str = "", pinned: bool = False) -> str:
    """The conflict check's verdict on the decision `second` against `first` as its
    closest possible peer (identical embedding), with no run: the case in which a
    free-text fact with the same words merges."""
    from server.memory.app.conflicts import DEFAULT_CONFLICT_POLICY, check_on_commit

    peer = {
        "memory_id": "m1",
        "memory_key": "decision:first",
        "dist": 0.0,
        "pinned": pinned,
        "value": first,
        "agent": "claude",
        "user_id": "-",
        "session_id": "-",
        "run_id": "-",
    }
    out = asyncio.run(
        check_on_commit(
            org="o",
            namespace="n",
            agent="claude",
            memory_type="decision",
            memory_key="decision:second",
            new_value=second,
            new_embedding=[1.0],
            candidate_rows=[peer],
            resolved_threshold=0.8,
            resolved_duplicate_threshold=0.9,
            resolved_policy=policy or DEFAULT_CONFLICT_POLICY,
            allowlisted_keys=set(),
        )
    )
    return out.verdict


def _statement(category: str, scenario: str) -> str:
    return stmt.memory_value(
        {
            "category": category,
            "scenario": scenario,
            "outcome": "roll back",
            "reasoning": "it takes two minutes",
            "alternatives": ["wait"],
        }
    )


def test_decisions_under_other_keys_only_ever_conflict() -> None:
    # Two scenarios alike in every character the embedding's excerpt reads.
    head = "how the nightly compaction schedules its merges " * 30
    pairs = [
        (("ci", "a bad image"), ("db", "a bad image")),
        (("deploy", "a bad image"), ("deploys", "a bad image")),
        (("deploy", "a bad image"), ("deploy", "the bad image")),
        (("deploy", "a bad image"), ("deploy", "a bad image in production")),
        (("deploy", f"{head} on monday"), ("deploy", f"{head} on friday")),
        (("c" * 119 + "1", "a bad image"), ("c" * 119 + "2", "a bad image")),
    ]
    for first, second in pairs:
        verdict = _commit_verdict(_statement(*first), _statement(*second))
        assert verdict == "conflict", (first, second, verdict)
    # newest_wins refines a templated substitution of a free-text fact; never a decision.
    deploy, storage = _statement("deploy", "a bad image"), _statement("storage", "a bad image")
    assert _commit_verdict(deploy, storage, policy="newest_wins") == "conflict"
    # A pinned decision under another key does not refuse this one.
    assert _commit_verdict(deploy, storage, pinned=True) == "conflict"


def test_outcome_keeps_an_empty_files_list_from_the_live_row(env, monkeypatch) -> None:
    from server.memory.api.models_decision import DecisionOutcomeInput

    _, calls, h = env
    d = asyncio.run(h.handle_decision_record(record_input(files=[])))

    async def row_without_files(identity, memory_key, **kw):
        return SimpleNamespace(
            value="",
            pinned=False,
            files=[],
            source_class="asserted",
            source_resource_id="",
            tags=[],
        )

    monkeypatch.setattr(h, "get_by_key", row_without_files)
    asyncio.run(
        h.handle_decision_outcome(
            DecisionOutcomeInput(
                org="o",
                namespace="n",
                memory_key=d.memory_key,
                realized_outcome="the fix landed in src/Interpreters/Aggregator.cpp",
            )
        )
    )
    # [] is sent as the caller's list, so the realized text's path is not detected.
    assert calls["set"][-1].files == []


def test_a_foreign_pinned_decision_leaves_a_clear_write_marked_for_review(monkeypatch) -> None:
    """A pinned decision outside the writer's read scope, with a peer below the conflict
    threshold: the write is clear, and the peer still asks the resolver to look."""
    from server.identity.domain.read_scope import ReadFilters
    from server.memory.app import conflicts
    from server.memory.app.conflicts import DEFAULT_CONFLICT_POLICY, check_on_commit

    row = {"agent": "claude", "session_id": "-", "run_id": "-"}
    pinned_elsewhere = {
        **row,
        "memory_id": "m1",
        "memory_key": "decision:first",
        "dist": 0.0,
        "pinned": True,
        "value": _statement("deploy", "a bad image"),
        "user_id": "someone-else",
    }
    below_threshold = {
        **row,
        "memory_id": "m2",
        "memory_key": "decision:third",
        "dist": 0.5,
        "pinned": False,
        "value": _statement("ci", "a flaky runner"),
        "user_id": "u1",
    }
    monkeypatch.setattr(
        conflicts,
        "_scope_or_none",
        lambda: ReadFilters(scope="own_user", user_id="u1", session_id=None),
    )
    out = asyncio.run(
        check_on_commit(
            org="o",
            namespace="n",
            agent="claude",
            memory_type="decision",
            memory_key="decision:second",
            new_value=_statement("storage", "a bad image"),
            new_embedding=[1.0],
            candidate_rows=[pinned_elsewhere, below_threshold],
            resolved_threshold=0.8,
            resolved_duplicate_threshold=0.9,
            resolved_policy=DEFAULT_CONFLICT_POLICY,
            allowlisted_keys=set(),
        )
    )
    assert (out.verdict, out.peers, out.review) == ("clear", [], True)


def test_a_pinned_decision_peer_is_marked_conflicted_and_stays_pinned(monkeypatch) -> None:
    """A close pinned decision under another key does not refuse the write; the marking
    step flags it `conflicted` beside the new row and keeps its pin."""
    from datetime import datetime

    import server.memory.store as memory_repo
    from server.memory.app import conflicts
    from server.memory.store import MemoryRow, lifecycle

    def row(memory_id: str, key: str, pinned: bool) -> MemoryRow:
        return MemoryRow(
            memory_id=memory_id,
            org="o",
            namespace="n",
            agent="claude",
            user_id="-",
            session_id="-",
            memory_key=key,
            memory_type="decision",
            value=_statement("deploy", key),
            confidence=1.0,
            version=1,
            pinned=pinned,
            created_at=datetime(2026, 1, 1),
            expires_at=None,
            entities=[],
            status="active",
            conflict_with=[],
        )

    rows = {"new": row("new", "decision:new", False), "pin": row("pin", "decision:pin", True)}
    written: list[dict[str, Any]] = []

    async def get_by_id(memory_id, **kw):
        return rows.get(memory_id)

    async def insert(versions, ch=None):
        written.extend(versions)

    async def mirror(versions, ch=None, **_):
        return None

    monkeypatch.setattr(memory_repo, "get_by_id", get_by_id)
    monkeypatch.setattr(lifecycle, "get_by_id", get_by_id)
    monkeypatch.setattr(lifecycle, "insert_memory_versions", insert)
    monkeypatch.setattr(lifecycle, "mirror_status_to_embeddings", mirror)
    monkeypatch.setattr(lifecycle, "_default_client", lambda: object())
    out = asyncio.run(
        conflicts.mark_peers_conflicted(org="o", new_memory_id="new", peer_ids=["pin"])
    )
    assert out == "conflict"
    by_id = {str(r["memory_id"]): r for r in written}
    assert by_id["pin"]["status"] == "conflicted" and by_id["pin"]["pinned"] == 1
    assert by_id["pin"]["conflict_with"] == ["new"] and by_id["new"]["conflict_with"] == ["pin"]


def test_decision_outcome_does_not_honour_read_scope() -> None:
    """A token bound to one user or session is refused every `decision.*` op at the
    transport."""
    from server.identity.domain.read_scope import honours_read_scope

    assert not honours_read_scope("decision.outcome")


def test_a_secret_in_a_long_reasoning_makes_the_row_high(env) -> None:
    """The sensitivity is read from every field, so a credential keeps the row out of the
    embeddings and the pre-edit block."""
    _, calls, h = env
    token = "ghp_" + "Ab1" * 12
    reasoning = "the runner needs a token " * 20 + token
    asyncio.run(h.handle_decision_record(record_input(reasoning=reasoning)))
    assert calls["set"][-1].sensitivity == "high"


def test_a_blacklisted_phrase_in_a_long_reasoning_refuses_the_whole_decision(env) -> None:
    from fastapi import HTTPException

    ch, calls, h = env
    ch.blocked = ["do-not-store-this-phrase"]
    reasoning = "the mirror was slow " * 30 + "so use do-not-store-this-phrase"
    with pytest.raises(HTTPException) as e:
        asyncio.run(h.handle_decision_record(record_input(reasoning=reasoning)))
    assert e.value.detail["code"] == "BLACKLISTED"
    assert calls["record"] == [] and calls["set"] == []


def test_a_non_ascii_blacklisted_phrase_refuses_the_decision(env) -> None:
    from fastapi import HTTPException

    ch, calls, h = env
    ch.blocked = ["배포 금지"]
    reasoning = "the mirror was slow " * 30 + "배포 금지"
    with pytest.raises(HTTPException) as e:
        asyncio.run(h.handle_decision_record(record_input(reasoning=reasoning)))
    assert e.value.detail["code"] == "BLACKLISTED"
    assert calls["record"] == [] and calls["set"] == []


def test_alternatives_that_read_as_a_phone_number_together_stay_unlabelled(env) -> None:
    """Each field is read on its own, so `256`, `512` and `1024` in three alternatives do
    not read as one phone number."""
    _, calls, h = env
    asyncio.run(h.handle_decision_record(record_input(alternatives=["256", "512", "1024"])))
    assert calls["set"][-1].sensitivity == ""
    # memory.set reads the statement too; it labels nothing either.
    assert detect_sensitivity(calls["set"][-1].value) == ""


def test_the_decision_maker_is_checked_too(env) -> None:
    from fastapi import HTTPException

    ch, calls, h = env
    asyncio.run(h.handle_decision_record(record_input(decision_maker="ana@example.com")))
    # The row never holds the decision maker, so it does not label the row.
    assert calls["set"][-1].sensitivity == ""
    ch.blocked = ["do-not-store-this-phrase"]
    calls["record"].clear()
    calls["set"].clear()
    with pytest.raises(HTTPException) as e:
        asyncio.run(
            h.handle_decision_record(record_input(decision_maker="do-not-store-this-phrase"))
        )
    assert e.value.detail["code"] == "BLACKLISTED"
    assert calls["record"] == [] and calls["set"] == []


def test_an_outcome_works_the_server_labels_out_again(monkeypatch, env) -> None:
    """The live row's `attribute:` labels are rebuilt from the text, not carried: one the
    text still supports comes back once, one it does not is dropped."""
    from server.memory.api.models_decision import DecisionOutcomeInput
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_attribute_tagging", True)
    _, calls, h = env
    d = asyncio.run(h.handle_decision_record(record_input(reasoning="she is pregnant")))
    labels = [t for t in calls["set"][-1].tags if t.startswith("attribute:")]
    assert labels
    calls["set"][-1].tags.append("attribute:stale")
    asyncio.run(
        h.handle_decision_outcome(
            DecisionOutcomeInput(
                org="o", namespace="n", memory_key=d.memory_key, realized_outcome="ok"
            )
        )
    )
    after = calls["set"][-1].tags
    assert [t for t in after if t.startswith("attribute:")] == labels


def test_a_decision_recorded_again_word_for_word_writes_the_same_statement(env) -> None:
    """The statement carries neither `made_at` nor `run_id`, so memory.set takes the second
    write as a restatement."""
    _, calls, h = env
    asyncio.run(h.handle_decision_record(record_input()))
    asyncio.run(h.handle_decision_record(record_input(run_id="r2")))
    first, second = calls["set"][-2:]
    assert first.value == second.value


def test_an_attribute_in_a_long_reasoning_is_labelled(monkeypatch, env) -> None:
    from server.memory.domain.sensitivity import ATTRIBUTE_TAG_PREFIX, attribute_classes
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_attribute_tagging", True)
    _, calls, h = env
    term = "she is pregnant"
    assert attribute_classes(term), "the fixture term must label an attribute"
    reasoning = "the rota changes next month " * 15 + term
    asyncio.run(h.handle_decision_record(record_input(reasoning=reasoning)))
    assert any(t.startswith(ATTRIBUTE_TAG_PREFIX) for t in calls["set"][-1].tags)


def test_a_blacklisted_realized_outcome_refuses_the_outcome(env) -> None:
    from fastapi import HTTPException

    from server.memory.api.models_decision import DecisionOutcomeInput

    ch, calls, h = env
    d = asyncio.run(h.handle_decision_record(record_input()))
    ch.blocked = ["do-not-store-this-phrase"]
    written = (len(calls["record"]), len(calls["set"]))
    with pytest.raises(HTTPException) as e:
        asyncio.run(
            h.handle_decision_outcome(
                DecisionOutcomeInput(
                    org="o",
                    namespace="n",
                    memory_key=d.memory_key,
                    realized_outcome="rolled back to do-not-store-this-phrase",
                )
            )
        )
    assert e.value.detail["code"] == "BLACKLISTED"
    assert (len(calls["record"]), len(calls["set"])) == written


def test_an_attribute_anywhere_in_a_long_decision_is_labelled(monkeypatch, env) -> None:
    from server.memory.domain.sensitivity import ATTRIBUTE_TAG_PREFIX
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_attribute_tagging", True)
    _, calls, h = env
    term = "she is pregnant"
    long = {"scenario": "s " * 1000, "outcome": "o " * 500}
    asyncio.run(h.handle_decision_record(record_input(**long, reasoning="r " * 550 + term)))
    assert any(t.startswith(ATTRIBUTE_TAG_PREFIX) for t in calls["set"][-1].tags)
    asyncio.run(
        h.handle_decision_record(record_input(scenario="another", alternatives=["wait", term]))
    )
    assert any(t.startswith(ATTRIBUTE_TAG_PREFIX) for t in calls["set"][-1].tags)


def test_a_refused_decision_is_counted_as_blacklisted(monkeypatch, env) -> None:
    from fastapi import HTTPException

    ch, _, h = env
    counted: list[tuple[str, str]] = []
    monkeypatch.setattr(h, "count_write", lambda ns, outcome: counted.append((ns, outcome)))
    ch.blocked = ["do-not-store-this-phrase"]
    with pytest.raises(HTTPException):
        asyncio.run(h.handle_decision_record(record_input(reasoning="do-not-store-this-phrase")))
    assert counted == [("n", "blacklisted")]


def test_decision_attribute_labels_are_counted(monkeypatch, env) -> None:
    import server.observability.metrics as metrics
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_attribute_tagging", True)
    counted: list[str] = []
    monkeypatch.setattr(
        metrics,
        "record_attribute_tagged",
        lambda *, namespace, attribute: counted.append(attribute),
    )
    _, _, h = env
    asyncio.run(h.handle_decision_record(record_input(reasoning="she is pregnant")))
    assert counted

    # A row memory.set refuses was never labelled, so nothing is counted for it.
    from fastapi import HTTPException

    import server.memory.app.write as write_mod

    async def refused(inp):
        raise HTTPException(status_code=409, detail={"code": "LOCKED"})

    counted.clear()
    monkeypatch.setattr(write_mod, "handle_memory_set", refused)
    with pytest.raises(HTTPException):
        asyncio.run(h.handle_decision_record(record_input(reasoning="she is pregnant")))
    assert counted == []


def test_a_failed_blacklist_read_is_counted_as_an_error(monkeypatch, env) -> None:
    import server.memory.app.blacklist_guard as blacklist

    _, calls, h = env
    counted: list[str] = []
    monkeypatch.setattr(h, "count_write", lambda ns, outcome: counted.append(outcome))

    async def unreadable(*, org, namespace, value):
        raise RuntimeError("ClickHouse is down")

    monkeypatch.setattr(blacklist, "find_match", unreadable)
    with pytest.raises(RuntimeError):
        asyncio.run(h.handle_decision_record(record_input()))
    assert counted == ["error"] and calls["record"] == []


def test_files_come_from_the_stored_alternatives_only() -> None:
    """A path past the cut is never stored, so the blacklist never read it: no file."""
    path = "src/Interpreters/GraceHashJoin.cpp"
    near = "y " * 100 + path
    far = "y " * svc.ALTERNATIVE_CHARS + path
    stored = {"alternatives": svc.clean_alternatives([near, far])}
    assert svc.decision_files(stored) == [path]
    stored = {"alternatives": svc.clean_alternatives([far])}
    assert svc.decision_files(stored) is None
