"""Decisions as first-class records: key derivation, field
shaping, the causal walk, the four handlers over stubbed planes, and memory.delete
retiring a decision's record on clickhouse-local."""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from server.memory.app import decisions as svc
from server.memory.domain.decision_schema import SCHEMA_FIELDS
from server.memory.domain.edge_relations import canonical_link_relation
from tests.unit.clickhouse_local import CLICKHOUSE, KeptCH, ddl, view


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
                {"memory_key": k, "record_json": json.dumps(v), "created_at": None}
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

    async def fake_record_write(inp, **_kw):
        calls["record"].append(inp)
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
    monkeypatch.setattr(h, "tombstone_decision_record", fake_tombstone)
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


def test_the_writer_read_and_the_tombstone_are_sequential() -> None:
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


# ------------------------------------------- memory.delete retires the record

_KEY = svc.decision_key("deploy", "Roll back or hotfix the image?")
_RECORD_ID = "00000000-0000-0000-0000-0000000000d1"


@pytest.fixture
def local(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> KeptCH:
    if CLICKHOUSE is None:
        pytest.skip("clickhouse binary not installed")
    from server.core import ch as ch_mod
    from server.memory.store import key_write_lock

    ch = KeptCH(
        tmp_path,
        *(ddl(t) for t in ("memory", "memory_records", "memory_record_versions")),
        view("memory_record_versions_mv"),
        *(ddl(t) for t in ("memory_record_fields", "memory_record_conflicts")),
        *(ddl(t) for t in ("memory_schemas", "memory_schema_fields")),
        ddl("operator_edges"),
    )

    @asynccontextmanager
    async def _no_lease(*_key: str) -> AsyncIterator[None]:
        yield

    # memory.delete_version holds the key's write lock, a lease this database does not keep.
    monkeypatch.setattr(key_write_lock.key_write_locks, "hold", _no_lease)
    monkeypatch.setattr(ch_mod, "_ch", ch)
    return ch


def _memory_row(agent: str, memory_type: str, version: int) -> dict[str, Any]:
    from server.memory.store.rows import build_first_version_row

    return build_first_version_row(
        {
            "org": "o",
            "namespace": "n",
            "agent": agent,
            "user_id": "-",
            "session_id": "-",
            "memory_key": _KEY,
            "memory_type": memory_type,
            "value": json.dumps({"outcome": "rollback"}),
            "value_json": "{}",
            "content_sha256": "",
            "actor": f"process:{agent}",
            "source_class": "asserted",
            "confidence": 1.0,
            "version": version,
        },
        now=datetime.now(UTC).replace(tzinfo=None, microsecond=0),
    )


def _set_into(ch: KeptCH, monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """memory.set reduced to its insert: the row lands under the input's identity with its
    value, above any version written so far. Returns the inputs it received."""
    seen: list[Any] = []

    async def _set(inp: Any) -> Any:
        seen.append(inp)
        row = _memory_row(inp.agent, inp.memory_type, time.time_ns())
        row.update(user_id=inp.user_id, session_id=inp.session_id, value=json.dumps(inp.value))
        await ch.insert("loom.memory", [row])
        return SimpleNamespace(version=row["version"])

    monkeypatch.setattr("server.memory.app.write.handle_memory_set", _set)
    return seen


async def _write_decision_rows(
    ch: KeptCH, *agents: str, memory_type: str = "decision", updated_at: datetime | None = None
) -> int:
    """One active record under _KEY, written by the first agent, and one live memory row
    under _KEY per agent. Returns the rows' version."""
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    version = time.time_ns() - 10_000_000_000
    await ch.insert(
        "loom.memory_records",
        [
            {
                "record_id": _RECORD_ID,
                "org": "o",
                "namespace": "n",
                "schema_id": _RECORD_ID,
                "schema_name": "decision",
                "memory_key": _KEY,
                "identity_agent": agents[0],
                "record_json": json.dumps({"scenario": "Roll back?", "outcome": "rollback"}),
                "status": "active",
                "version": version,
                "created_at": now,
                "updated_at": updated_at or now,
            }
        ],
    )
    for agent in agents:
        await ch.insert("loom.memory", [_memory_row(agent, memory_type, version)])
    return version


async def _delete(agent: str, *, version: int | None = None, user_id: str = "-") -> None:
    from server.memory.api.models_crud import MemoryDeleteInput, MemoryDeleteVersionInput
    from server.memory.app import handlers as hm

    key = {"org": "o", "namespace": "n", "agent": agent, "user_id": user_id, "memory_key": _KEY}
    if version is None:
        out: Any = await hm.handle_memory_delete(MemoryDeleteInput(**key))
    else:
        out = await hm.handle_memory_delete_version(
            MemoryDeleteVersionInput(**key, version=version)
        )
    assert out.tombstoned is True


async def _history() -> list[str]:
    from server.memory.api.models_records import MemoryRecordHistoryInput
    from server.memory.app.records import handle_memory_record_history

    out = await handle_memory_record_history(
        MemoryRecordHistoryInput(org="o", namespace="n", record_id=_RECORD_ID)
    )
    return [r.status for r in out.rows]


async def _seed_decision() -> Any:
    from server.memory.api.models_decision import DecisionChainInput
    from server.memory.app import decision as h

    out = await h.handle_decision_chain(DecisionChainInput(org="o", namespace="n", memory_key=_KEY))
    return out.seed_decision


@pytest.mark.asyncio
@pytest.mark.parametrize("op", ["memory.delete", "memory.delete_version"])
async def test_deleting_a_decision_row_tombstones_its_record(
    local: KeptCH, monkeypatch: pytest.MonkeyPatch, op: str
) -> None:
    import server.memory.app.search as msearch
    from server.core.errors import NotFoundError
    from server.memory.api.models_decision import DecisionOutcomeInput, DecisionPrecedentsInput
    from server.memory.app import decision as h

    # The search leg is stubbed to hit the key, so precedents answers from the record alone.
    async def _hit(_inp: Any) -> Any:
        return SimpleNamespace(
            results=[SimpleNamespace(memory_key=_KEY, similarity=0.9)], embedding_model="m"
        )

    monkeypatch.setattr(msearch, "handle_memory_search", _hit)
    precedents_in = DecisionPrecedentsInput(org="o", namespace="n", query="image rollback")

    version = await _write_decision_rows(local, "claude")
    assert (await _seed_decision()).outcome == "rollback"
    served = await h.handle_decision_precedents(precedents_in)
    assert [p.memory_key for p in served.precedents] == [_KEY]

    await _delete("claude", version=version if op == "memory.delete_version" else None)

    assert await _seed_decision() is None
    assert (await h.handle_decision_precedents(precedents_in)).precedents == []
    with pytest.raises(NotFoundError):
        await h.handle_decision_outcome(
            DecisionOutcomeInput(org="o", namespace="n", memory_key=_KEY, realized_outcome="x")
        )
    assert await _history() == ["tombstoned", "active"]

    # Deciding the scenario again records it afresh: nothing active was superseded.
    _set_into(local, monkeypatch)
    again = await h.handle_decision_record(
        _record_input(
            category="deploy",
            scenario="Roll back or hotfix the image?",
            outcome="hotfix",
            caused_by=[],
            influenced_by=[],
        )
    )
    assert again.memory_key == _KEY and again.superseded_prior is False
    assert (await _seed_decision()).outcome == "hotfix"


@pytest.mark.asyncio
async def test_a_delete_by_another_identity_leaves_the_writers_record(
    local: KeptCH,
) -> None:
    # claude wrote the record; the maintainer's row under the key is not its writer's.
    await _write_decision_rows(local, "claude", "maintainer")
    await _delete("maintainer")
    assert await _seed_decision() is not None
    assert await _history() == ["active"]
    await _delete("claude")
    assert await _seed_decision() is None
    assert await _history() == ["tombstoned", "active"]


@pytest.mark.asyncio
async def test_deleting_a_row_of_another_type_leaves_the_record(local: KeptCH) -> None:
    await _write_decision_rows(local, "claude", memory_type="semantic")
    await _delete("claude")
    assert await _history() == ["active"]


@pytest.mark.asyncio
async def test_a_failed_record_tombstone_leaves_the_delete_standing(
    local: KeptCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.identity.domain.read_scope import WHOLE_NAMESPACE
    from server.memory import store as memory_repo

    async def _fails(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("timeout")

    monkeypatch.setattr(memory_repo, "tombstone_decision_record", _fails)
    await _write_decision_rows(local, "claude")
    await _delete("claude")
    identity = memory_repo.Identity(org="o", namespace="n", agent="claude")
    assert await memory_repo.get_by_key(identity, _KEY, strong=True, scope=WHOLE_NAMESPACE) is None
    assert await _history() == ["active"]


@pytest.mark.asyncio
async def test_a_decision_row_written_above_the_tombstone_keeps_the_record(
    local: KeptCH,
) -> None:
    # The same identity wrote the key again after the delete's tombstone, before the
    # delete reached its record: the key holds a live decision, so the record stays.
    from server.memory import store as memory_repo

    version = await _write_decision_rows(local, "claude")
    tombstone = _memory_row("claude", "decision", version + 1)
    tombstone["tombstoned_at"] = tombstone["updated_at"]
    await local.insert("loom.memory", [tombstone, _memory_row("claude", "decision", version + 2)])
    await memory_repo.tombstone_decision_record("o", "n", _KEY, version=version + 1)
    assert await _history() == ["active"]


@pytest.mark.asyncio
async def test_a_refused_memory_row_leaves_no_active_record(
    local: KeptCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The memory row is written first: when memory.set refuses (a pinned peer), the
    # record write is never reached, so no record says what the memory plane refused.
    from fastapi import HTTPException

    from server.memory.app import decision as h

    async def _pinned_conflict(_inp: Any) -> Any:
        raise HTTPException(status_code=409, detail={"code": "PINNED_CONFLICT"})

    monkeypatch.setattr("server.memory.app.write.handle_memory_set", _pinned_conflict)
    with pytest.raises(HTTPException):
        await h.handle_decision_record(_record_input(caused_by=[], influenced_by=[]))
    key = svc.decision_key("Deploy", "Roll back or hotfix the 26.5 image?")
    assert await h._decision_records(local, "o", "n", [key]) == {}
    assert await local.query_all("SELECT 1 FROM loom.memory_records") == []


@pytest.mark.asyncio
async def test_a_supersede_drawn_below_the_active_version_still_wins(local: KeptCH) -> None:
    # A slow clock on the superseding replica: its version is under the active row's.
    from server.memory import store as memory_repo

    version = await _write_decision_rows(local, "claude")
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    await memory_repo.supersede_active_record(
        "o", "n", "decision", _KEY, close_at=now, version=version - 1_000, now=now
    )
    rows = await local.query_all(
        "SELECT status FROM loom.memory_records FINAL WHERE record_id = {rid:UUID}",
        {"rid": _RECORD_ID},
    )
    assert rows == [{"status": "superseded"}]


@pytest.mark.asyncio
async def test_the_writers_delete_retires_the_record_while_another_row_holds_the_key(
    local: KeptCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The record is the maintainer's text; claude's row under the key was superseded in
    # the record plane. The maintainer's delete retires the record, and no outcome copies
    # it into claude's row.
    import server.memory.app.search as msearch
    from server.core.errors import NotFoundError
    from server.memory.api.models_decision import DecisionOutcomeInput, DecisionPrecedentsInput
    from server.memory.app import decision as h

    async def _hit(_inp: Any) -> Any:
        return SimpleNamespace(
            results=[SimpleNamespace(memory_key=_KEY, similarity=0.9)], embedding_model="m"
        )

    monkeypatch.setattr(msearch, "handle_memory_search", _hit)
    await _write_decision_rows(local, "maintainer", "claude")
    await _delete("maintainer")
    assert await _seed_decision() is None
    precedents = await h.handle_decision_precedents(
        DecisionPrecedentsInput(org="o", namespace="n", query="image rollback")
    )
    assert precedents.precedents == []
    seen = _set_into(local, monkeypatch)
    with pytest.raises(NotFoundError):
        await h.handle_decision_outcome(
            DecisionOutcomeInput(org="o", namespace="n", memory_key=_KEY, realized_outcome="ok")
        )
    assert seen == []
    assert await _history() == ["tombstoned", "active"]


@pytest.mark.asyncio
async def test_the_writers_identity_includes_its_user(local: KeptCH) -> None:
    # Same agent, another user: that row is not the writer's, so its delete keeps the record.
    version = await _write_decision_rows(local, "claude")
    other_user = _memory_row("claude", "decision", version)
    other_user["user_id"] = "u2"
    await local.insert("loom.memory", [other_user])
    await _delete("claude", user_id="u2")
    assert await _history() == ["active"]
    await _delete("claude")
    assert await _history() == ["tombstoned", "active"]


@pytest.mark.asyncio
async def test_the_tombstone_copies_the_active_record(local: KeptCH) -> None:
    version = await _write_decision_rows(local, "claude")
    await _delete("claude")
    active, tombstoned = await local.query_all(
        "SELECT * FROM loom.memory_record_versions WHERE record_id = {rid:UUID} ORDER BY version",
        {"rid": _RECORD_ID},
    )
    changed = {"status", "tombstoned_at", "valid_to", "version", "updated_at"}
    for column in active.keys() - changed:
        assert tombstoned[column] == active[column], column
    assert (active["status"], tombstoned["status"]) == ("active", "tombstoned")
    assert active["tombstoned_at"] is None and tombstoned["tombstoned_at"] is not None
    assert active["valid_to"] is None and tombstoned["valid_to"] is not None
    assert int(tombstoned["version"]) > int(active["version"]) == version


@pytest.mark.asyncio
async def test_a_tombstone_from_a_slow_clock_still_reads_newest(local: KeptCH) -> None:
    # The active version was written an hour ahead of the deleting replica's clock.
    ahead = datetime.now(UTC).replace(tzinfo=None, microsecond=0) + timedelta(hours=1)
    await _write_decision_rows(local, "claude", updated_at=ahead)
    await _delete("claude")
    assert await _history() == ["tombstoned", "active"]


@pytest.mark.asyncio
async def test_an_outcome_with_no_live_decision_row_is_not_found(
    local: KeptCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The record is still active (its tombstone never ran) but no row holds the key.
    from server.core.errors import NotFoundError
    from server.memory.api.models_decision import DecisionOutcomeInput
    from server.memory.app import decision as h

    version = await _write_decision_rows(local, "claude")
    tombstone = _memory_row("claude", "decision", version + 1)
    tombstone["tombstoned_at"] = tombstone["updated_at"]
    await local.insert("loom.memory", [tombstone])
    seen = _set_into(local, monkeypatch)
    with pytest.raises(NotFoundError):
        await h.handle_decision_outcome(
            DecisionOutcomeInput(org="o", namespace="n", memory_key=_KEY, realized_outcome="x")
        )
    assert seen == []
    assert await _history() == ["active"]


@pytest.mark.asyncio
async def test_an_outcome_revises_the_record_writers_row_while_it_is_live(
    local: KeptCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # claude wrote the key after the maintainer, but the record is the maintainer's.
    from server.memory.api.models_decision import DecisionOutcomeInput
    from server.memory.app import decision as h

    await h.ensure_schema("o", "n")
    await _write_decision_rows(local, "maintainer")
    await local.insert("loom.memory", [_memory_row("claude", "decision", time.time_ns())])
    seen = _set_into(local, monkeypatch)
    await h.handle_decision_outcome(
        DecisionOutcomeInput(org="o", namespace="n", memory_key=_KEY, realized_outcome="ok")
    )
    assert [i.agent for i in seen] == ["maintainer"]
    # The revision is the key's active record, and the record it replaced is superseded.
    rows = await local.query_all(
        "SELECT record_json FROM loom.memory_records FINAL "
        "WHERE memory_key = {key:String} AND status = 'active'",
        {"key": _KEY},
    )
    assert [json.loads(r["record_json"])["realized_outcome"] for r in rows] == ["ok"]
    assert await _history() == ["superseded", "active"]


@pytest.mark.asyncio
async def test_a_delete_between_the_row_and_the_record_leaves_no_active_record(
    local: KeptCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # decision.record writes its row, the same identity deletes it before the record
    # lands, so that delete finds no record to retire; the write retires it itself.
    from server.memory.app import decision as h

    async def _set_then_delete(inp: Any) -> Any:
        await local.insert("loom.memory", [_memory_row(inp.agent, inp.memory_type, time.time_ns())])
        await _delete(inp.agent)
        return SimpleNamespace(version=0)

    monkeypatch.setattr("server.memory.app.write.handle_memory_set", _set_then_delete)
    out = await h.handle_decision_record(
        _record_input(
            category="deploy",
            scenario="Roll back or hotfix the image?",
            caused_by=[],
            influenced_by=[],
        )
    )
    assert await _seed_decision() is None
    rows = await local.query_all(
        "SELECT status FROM loom.memory_records FINAL WHERE record_id = {rid:UUID}",
        {"rid": out.record_id},
    )
    assert rows == [{"status": "tombstoned"}]


def test_a_failing_closing_check_still_records_the_decision(env, monkeypatch) -> None:
    _, calls, h = env

    async def _fails(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("timeout")

    monkeypatch.setattr(h, "tombstone_decision_record", _fails)
    out = asyncio.run(h.handle_decision_record(_record_input()))
    assert out.record_id == "rec-1" and len(calls["record"]) == 1 and len(calls["set"]) == 1


@pytest.mark.asyncio
async def test_history_hides_active_and_superseded_rows_with_tombstoned_at_set(
    local: KeptCH,
) -> None:
    # A mirror row with tombstoned_at set and another status stays out of history; the
    # decision delete's tombstoned version is served.
    await _write_decision_rows(local, "claude")
    await _delete("claude")
    (active,) = await local.query_all(
        "SELECT * FROM loom.memory_record_versions WHERE record_id = {rid:UUID} "
        "AND status = 'active'",
        {"rid": _RECORD_ID},
    )
    erased = []
    for status, bump in (("active", 10), ("superseded", 20)):
        row = {k: v for k, v in active.items() if v is not None}
        row.update(status=status, version=int(active["version"]) + bump)
        row["tombstoned_at"] = row["updated_at"]
        erased.append(row)
    await local.insert("loom.memory_record_versions", erased)
    assert await _history() == ["tombstoned", "active"]


@pytest.mark.asyncio
async def test_an_outcome_goes_on_the_writers_exact_identity(
    local: KeptCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A newer live row differs from the record's writer only by session: not the writer's.
    from server.memory.api.models_decision import DecisionOutcomeInput
    from server.memory.app import decision as h

    await h.ensure_schema("o", "n")
    version = await _write_decision_rows(local, "claude")
    other_session = _memory_row("claude", "decision", version + 1)
    other_session["session_id"] = "s2"
    await local.insert("loom.memory", [other_session])
    seen = _set_into(local, monkeypatch)
    await h.handle_decision_outcome(
        DecisionOutcomeInput(org="o", namespace="n", memory_key=_KEY, realized_outcome="ok")
    )
    assert [(i.agent, i.session_id) for i in seen] == [("claude", "-")]


@pytest.mark.asyncio
async def test_record_query_rows_carry_their_status(local: KeptCH) -> None:
    from server.memory.api.models_records import MemoryRecordQueryInput
    from server.memory.app.records import handle_memory_record_query

    await _write_decision_rows(local, "claude")
    out = await handle_memory_record_query(
        MemoryRecordQueryInput(org="o", namespace="n", schema_name="decision")
    )
    assert [(r.record_id, r.status) for r in out.rows] == [(_RECORD_ID, "active")]


def test_decision_ops_refuse_a_token_bounded_below_the_namespace() -> None:
    # decision.outcome reads the writer's identity across every user of the key, which a
    # user- or session-scoped token must not see; the transport gate refuses such tokens.
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


@pytest.mark.asyncio
async def test_a_second_closing_write_keeps_the_record_tombstoned(local: KeptCH) -> None:
    from server.memory import store as memory_repo

    version = await _write_decision_rows(local, "claude")
    await _delete("claude")
    await memory_repo.tombstone_decision_record("o", "n", _KEY, version=version + 1)
    rows = await local.query_all(
        "SELECT status FROM loom.memory_records FINAL WHERE record_id = {rid:UUID}",
        {"rid": _RECORD_ID},
    )
    assert rows == [{"status": "tombstoned"}]
    assert await _history() == ["tombstoned", "active"]  # the second call wrote nothing


@pytest.mark.asyncio
async def test_a_new_writers_decision_keeps_nothing_of_the_earlier_writers(
    local: KeptCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # claude records and learns the outcome; the maintainer then decides the same scenario
    # with no alternatives, confidence or outcome. None of claude's fields may live on in
    # the maintainer's record, where claude's delete or erasure would not reach them.
    from server.memory.api.models_decision import DecisionOutcomeInput
    from server.memory.app import decision as h

    _set_into(local, monkeypatch)
    scenario = {"category": "deploy", "scenario": "Roll back or hotfix the image?"}
    await h.handle_decision_record(_record_input(**scenario, caused_by=[], influenced_by=[]))
    await h.handle_decision_outcome(
        DecisionOutcomeInput(
            org="o", namespace="n", memory_key=_KEY, realized_outcome="took 90s", success=True
        )
    )
    await h.handle_decision_record(
        _record_input(
            **scenario,
            agent="maintainer",
            run_id="-",
            outcome="hotfix",
            alternatives=[],
            confidence=0.0,
            caused_by=[],
            influenced_by=[],
        )
    )
    rows = await local.query_all(
        "SELECT record_json FROM loom.memory_records FINAL "
        "WHERE memory_key = {key:String} AND status = 'active'",
        {"key": _KEY},
    )
    (record,) = [json.loads(r["record_json"]) for r in rows]
    for field in ("realized_outcome", "success", "alternatives", "confidence"):
        assert record[field] in (None, ""), field
    assert record["run_id"] in (None, "") and record["outcome"] == "hotfix"

    # The changed scalar is still recorded as a conflict between the two records.
    conflicts = await local.query_all(
        "SELECT prior_value, incoming_value FROM loom.memory_record_conflicts FINAL "
        "WHERE memory_key = {key:String} AND field_name = 'outcome'",
        {"key": _KEY},
    )
    assert conflicts == [{"prior_value": '"rollback"', "incoming_value": '"hotfix"'}]

    await _delete("claude")
    seed = await _seed_decision()
    assert seed is not None and seed.outcome == "hotfix"
    assert seed.realized_outcome == "" and seed.success is None and seed.alternatives == []


@pytest.mark.asyncio
async def test_an_outcome_keeps_nothing_of_another_active_record(
    local: KeptCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The record write's own read of the active record answers with another writer's
    # record (a lagging replica, or a concurrent decision.record). The revision is built
    # from the writer's record alone, so none of the other record's fields reach it.
    import server.memory.app.records as hrec
    from server.memory.api.models_decision import DecisionOutcomeInput
    from server.memory.app import decision as h

    await h.ensure_schema("o", "n")
    await _write_decision_rows(local, "claude")
    other = {"reasoning": "the other writer's reason", "alternatives": "wait", "run_id": "r9"}

    async def _other_record(*_a: Any, **_k: Any) -> Any:
        from server.memory.store.records import ActiveRecord

        return ActiveRecord("00000000-0000-0000-0000-0000000000e1", "", json.dumps(other))

    monkeypatch.setattr(hrec, "active_record", _other_record)
    _set_into(local, monkeypatch)
    await h.handle_decision_outcome(
        DecisionOutcomeInput(org="o", namespace="n", memory_key=_KEY, realized_outcome="ok")
    )
    rows = await local.query_all(
        "SELECT record_json FROM loom.memory_records FINAL "
        "WHERE memory_key = {key:String} AND status = 'active'",
        {"key": _KEY},
    )
    (record,) = [json.loads(r["record_json"]) for r in rows]
    assert record["realized_outcome"] == "ok"
    for field, value in other.items():
        assert record[field] != value, field


@pytest.mark.asyncio
async def test_a_whole_record_write_reports_nothing_kept(local: KeptCH) -> None:
    from server.memory.api.models_records import MemoryRecordWriteInput
    from server.memory.app import decision as h
    from server.memory.app.records import handle_memory_record_write

    await h.ensure_schema("o", "n")

    def _write(**fields: Any) -> MemoryRecordWriteInput:
        values = {"category": "deploy", "scenario": "Roll back?", "outcome": "rollback", **fields}
        return MemoryRecordWriteInput(
            org="o",
            namespace="n",
            schema_name="decision",
            memory_key=_KEY,
            agent="claude",
            fields=svc.record_fields(values),  # type: ignore[arg-type]
        )

    await handle_memory_record_write(_write(reasoning="fast"))
    out = await handle_memory_record_write(_write(), carry_prior=False)
    assert out.superseded_record_id
    assert out.fold["filled_from_prior"] == [] and out.fold["unioned"] == []
