"""The conflict resolver over one user's long run of sessions, against ClickHouse,
with a scripted judge.

Twelve sessions write about sixty facts into a namespace, once under `newest_wins`
and once under the default `auto_reinforce` (both retire on a confident update),
through `memory.set` (odd sessions) and `memory.bulk_set` (even sessions), each a
week apart. Six attributes change once or twice, each change under a new key and in
new words; other facts are restated, elaborated, or sit near a different fact;
one pinned fact is contradicted later; one fact is deleted and brought back
with Undo; the rest are unrelated (`bench/conflict_resolver_scenario.py`, the
scenario and its ground truth). A scenario embedder gives every text a fixed
direction, so the write path's own check decides which rows the resolver owes a
judgement, and the judge answers each (row, peer) pair from the scenario's
ground truth. The worker's pass runs after every session until its queue is
empty, as it would in the background.

At the end each attribute's current value is its only live row, every older
value is superseded behind the next one with an Undo-able record, the fact graph
holds only the current values open, nothing else was retired or left flagged,
low-confidence and malformed answers changed nothing, another pass changes
nothing, and search answers each attribute with its current value."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from bench.conflict_resolver_scenario import (
    ATTRIBUTES,
    BY_KEY,
    CURRENT,
    DELETE_IN,
    FACTS,
    FLAGGED_AT_WRITE,
    JUDGE,
    KEPT,
    LOW_CONFIDENCE,
    MALFORMED,
    OLDER,
    RESTORE_IN,
    SESSIONS,
    SOLO,
    relation_of,
)
from server.core.datetimes import naive_utc_now
from tests.integration._resources import unique_org

NS = "long-session"
USER = "u1"
AGENT = "bot"


def _topics() -> dict[str, int]:
    names = sorted({t for f in FACTS for t, _a, _w in f.at})
    return {t: 4 * i for i, t in enumerate(names)}


class ScenarioEmbedder:
    """Every scenario text and query in its own fixed direction: the longest scenario
    value a text contains decides it. Any other text (an entity name) lands on an
    axis of its own, far from every fact."""

    def __init__(self, dim: int, model_name: str) -> None:
        self.dim, self.model_name = dim, model_name
        base = _topics()
        self._by_text: list[tuple[str, list[float]]] = []
        for f in FACTS:
            self._by_text.append((f.value, self._vec([(base[t] + a, w) for t, a, w in f.at])))
        for a in ATTRIBUTES.values():
            self._by_text.append((a.query, self._vec([(base[a.topic], 1.0)])))
        self._by_text.sort(key=lambda p: -len(p[0]))
        self._spare = 4 * len(base)

    def _vec(self, parts: list[tuple[int, float]]) -> list[float]:
        v = [0.0] * self.dim
        for i, w in parts:
            v[i] = w
        norm = math.sqrt(sum(x * x for x in v))
        return [x / norm for x in v]

    async def embed(self, text: str) -> list[float]:
        for phrase, vec in self._by_text:
            if phrase in text:
                return vec
        h = int(hashlib.sha1(text.encode()).hexdigest(), 16)
        return self._vec([(self._spare + h % (self.dim - self._spare), 1.0)])

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [await self.embed(t) for t in texts]


class ScriptedJudge:
    """Answers each resolver call from `JUDGE`, by the keys the prompt names."""

    provider, model = "fake", "scripted"

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.unexpected: list[tuple[str, str]] = []

    async def complete(self, system: str, user: str, **kw: Any) -> Any:
        new = re.search(r"NEW note \(key ([^)]+)\)", user)
        if new is None:
            return SimpleNamespace(text="")  # not the resolver's prompt
        key = new.group(1)
        self.calls.append(key)
        if key in MALFORMED:
            return SimpleNamespace(text='{"peers": [{"peer_id": "p1", "reason": "the same')
        entries = []
        for pid, peer in re.findall(r"^\[(p\d+)\] \(key ([^,]+), cosine", user, re.M):
            truth = JUDGE.get(frozenset({key, peer}))
            if truth is None:
                self.unexpected.append((key, peer))
                truth = ("related", 0.5, False, False)
            verdict, confidence, same_entity, same_attribute = truth
            entries.append(
                {
                    "peer_id": pid,
                    "reason": "scripted",
                    "same_entity": same_entity,
                    "same_attribute": same_attribute,
                    "verdict": verdict,
                    "confidence": confidence,
                }
            )
        return SimpleNamespace(text=json.dumps({"peers": entries}))


class _OnlyOrg:
    """The queue reads, limited to this test's org (the server holds other tests')."""

    def __init__(self, ch: Any, org: str) -> None:
        self.ch, self.org = ch, org

    async def query_all(self, sql: str, params: Any = None) -> Any:
        sql = sql.replace("WHERE ", "WHERE org = {only_org:String} AND ", 1)
        return await self.ch.query_all(sql, params={**(params or {}), "only_org": self.org})


@pytest.fixture
async def ch() -> Any:
    from server.core.ch import get_client

    c = get_client()
    await c.connect()
    yield c
    await c.disconnect()


@dataclass
class Run:
    org: str
    judge: ScriptedJudge
    totals: dict[str, int] = field(default_factory=dict)
    written: dict[str, int] = field(default_factory=dict)  # key -> version at its write


@pytest.fixture(params=["newest_wins", "auto_reinforce"])
async def run(ch, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Any:
    import server.embedding.embedder as embedder_mod
    import server.llm.client as llm_client
    from server.memory.store import conflict_resolver as queue
    from server.settings import settings

    org = unique_org("long-session")
    real = embedder_mod.get_embedder()
    monkeypatch.setattr(embedder_mod, "_cached", ScenarioEmbedder(real.dim, real.model_name))
    judge = ScriptedJudge()
    monkeypatch.setattr(llm_client, "get_llm_client_without_fallback", lambda m, **kw: judge)
    monkeypatch.setattr(settings, "memory_conflict_resolver_enabled", True)
    owed, recheck = queue.owed_rows, queue.unjudged_conflicted_rows
    monkeypatch.setattr(queue, "owed_rows", lambda **kw: owed(**kw, ch=_OnlyOrg(ch, org)))
    monkeypatch.setattr(
        queue, "unjudged_conflicted_rows", lambda **kw: recheck(**kw, ch=_OnlyOrg(ch, org))
    )
    await _register(org, request.param)
    return Run(org, judge)


async def _register(org: str, policy: str) -> None:
    from server.auth.context import set_current_auth
    from server.auth.jwt import AuthInfo
    from server.identity.api.models import NamespaceRegisterInput
    from server.identity.app.namespace import handle_namespace_register
    from server.operations.base import Role

    set_current_auth(
        AuthInfo(
            subject="it@example.com",
            org=org,
            namespaces=(NS,),
            roles=(Role.ADMIN,),
            is_service_account=False,
        )
    )
    try:
        await handle_namespace_register(
            NamespaceRegisterInput(
                org=org,
                namespace=NS,
                owner_email="it@example.com",
                default_conflict_policy=policy,
            )
        )
    finally:
        set_current_auth(None)


def _identity(org: str, session: int) -> dict[str, Any]:
    return {
        "org": org,
        "namespace": NS,
        "agent": AGENT,
        "user_id": USER,
        "session_id": f"s{session}",
    }


def _when(session: int, i: int) -> Any:
    # A week per session, a minute per write: no two statements share a second.
    start = naive_utc_now().replace(microsecond=0) - timedelta(weeks=13)
    return start + timedelta(weeks=session, minutes=i)


async def _write_session(org: str, session: int) -> None:
    from server.memory.api.models_bulk import MemoryBulkSetInput
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app import after_write
    from server.memory.app.bulk_set import handle_memory_bulk_set
    from server.memory.app.write import handle_memory_set

    facts = [f for f in FACTS if f.session == session]
    entries = [
        {
            "memory_key": f.key,
            "value": f.value,
            "relations": [("user", relation_of(f.key), f.obj)] if f.obj else [],
            "valid_at": _when(session, i),
            "pinned": f.pinned,
        }
        for i, f in enumerate(facts)
    ]
    if session % 2 == 0:
        await handle_memory_bulk_set(
            MemoryBulkSetInput(**_identity(org, session), entries=entries, detect_updates=False)
        )
    else:
        for e in entries:
            await handle_memory_set(MemorySetInput(**_identity(org, session), **e))
    await after_write.drain()


async def _delete_or_restore(org: str, session: int) -> None:
    from server.memory.api.models_crud import MemoryDeleteInput, MemoryRestoreInput
    from server.memory.app.handlers import handle_memory_delete
    from server.memory.app.restore import handle_memory_restore

    if session in DELETE_IN:
        key = DELETE_IN[session]
        out = await handle_memory_delete(
            MemoryDeleteInput(**_identity(org, BY_KEY[key].session), memory_key=key)
        )
        assert out.tombstoned, key
    if session in RESTORE_IN:
        key = RESTORE_IN[session]
        back = await handle_memory_restore(
            MemoryRestoreInput(**_identity(org, BY_KEY[key].session), memory_key=key)
        )
        assert (back.restored, back.from_status) == (True, "tombstoned"), key


async def _drain(run: Run) -> None:
    """Run the worker's pass until nothing of this org is owed or left to re-check."""
    from server.memory.app import after_write
    from server.memory.store import conflict_resolver as queue
    from server.memory.workers.conflict_resolver import run_pass

    for _ in range(8):
        out = await run_pass()
        await after_write.drain()
        for k, v in out.items():
            run.totals[k] = run.totals.get(k, 0) + int(v)
        if not await queue.owed_rows(limit=1) and not await queue.unjudged_conflicted_rows(limit=1):
            return
    left = await queue.owed_rows(limit=20) + await queue.unjudged_conflicted_rows(limit=20)
    raise AssertionError(f"the queue did not drain: {[r['memory_key'] for r in left]}")


async def _rows(ch: Any, org: str) -> dict[str, dict[str, Any]]:
    rows = await ch.query_all(
        "SELECT memory_key, toString(memory_id) AS id, session_id, status, superseded_by,"
        " arrayMap(x -> toString(x), conflict_with) AS conflict_with, pinned, version"
        " FROM loom.memory FINAL WHERE org = {org:String} AND namespace = {ns:String}"
        " AND tombstoned_at IS NULL SETTINGS select_sequential_consistency = 1",
        params={"org": org, "ns": NS},
    )
    return {str(r["memory_key"]): dict(r) for r in rows}


async def _vector_status(ch: Any, org: str) -> dict[str, str]:
    rows = await ch.query_all(
        "SELECT memory_key, status FROM loom.memory_embeddings FINAL WHERE org = {org:String}"
        " AND namespace = {ns:String} AND tombstoned_at IS NULL"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org, "ns": NS},
    )
    return {str(r["memory_key"]): str(r["status"]) for r in rows}


async def _triples(ch: Any, org: str, closed: set[str]) -> dict[str, bool]:
    """{object: open} of the user's facts. The graph closes facts with mutations:
    poll up to ten seconds for every object in `closed` to be closed."""
    got: dict[str, bool] = {}
    for _ in range(50):
        rows = await ch.query_all(
            "SELECT object, countIf(valid_to IS NULL) > 0 AS open FROM loom.entity_triples"
            " WHERE org = {org:String} AND namespace = {ns:String} AND subject = 'user'"
            " GROUP BY object"
            " SETTINGS select_sequential_consistency = 1, apply_mutations_on_fly = 1",
            params={"org": org, "ns": NS},
        )
        got = {str(r["object"]): bool(r["open"]) for r in rows}
        if not any(got.get(o, True) for o in closed):
            break
        await asyncio.sleep(0.2)
    return got


async def _audit(ch: Any, org: str, event: str) -> list[dict[str, Any]]:
    rows = await ch.query_all(
        "SELECT subject FROM loom.audit_log WHERE org = {org:String} AND event_type = {e:String}"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org, "e": event},
    )
    return [json.loads(r["subject"]) for r in rows]


def _snapshot(rows: dict[str, dict[str, Any]]) -> dict[str, tuple[Any, ...]]:
    return {
        k: (r["status"], r["version"], r["superseded_by"], sorted(r["conflict_with"]))
        for k, r in rows.items()
    }


def test_the_scenario_is_consistent() -> None:
    from server.memory.domain.graph_quality import is_single_valued_relation

    keys = [f.key for f in FACTS]
    assert len(keys) == len(set(keys)) and len(keys) >= 55
    assert {f.session for f in FACTS} == set(range(1, SESSIONS + 1))
    for a in ATTRIBUTES.values():
        assert not is_single_valued_relation(a.relation), a  # the write would close it itself
        sessions = [BY_KEY[k].session for k in a.keys]
        assert sessions == sorted(set(sessions)), a
    assert MALFORMED | LOW_CONFIDENCE <= KEPT
    assert all(pair <= KEPT and pair in JUDGE for pair in FLAGGED_AT_WRITE)


async def test_a_long_run_of_sessions_ends_with_one_live_value_per_attribute(ch, run) -> None:
    from server.memory.api.models_crud import MemoryRestoreInput
    from server.memory.api.models_search import MemorySearchInput
    from server.memory.app.restore import handle_memory_restore
    from server.memory.app.search.handler import handle_memory_search
    from server.memory.workers.conflict_resolver import run_pass

    org = run.org
    for session in range(1, SESSIONS + 1):
        await _write_session(org, session)
        await _delete_or_restore(org, session)
        rows = await _rows(ch, org)
        for f in FACTS:
            if f.session == session:
                run.written[f.key] = int(rows[f.key]["version"])
        await _drain(run)

    rows = await _rows(ch, org)
    assert set(rows) == {f.key for f in FACTS}
    assert run.judge.unexpected == [], "a cluster held a peer the scenario did not place"

    # Each attribute: its current value is live and unflagged; every older value is
    # superseded behind the next one.
    for name, a in ATTRIBUTES.items():
        head = rows[a.keys[-1]]
        assert (head["status"], head["conflict_with"]) == ("active", []), (name, head)
        for older, newer in zip(a.keys, a.keys[1:], strict=False):
            assert rows[older]["status"] == "superseded", (name, older)
            assert rows[older]["superseded_by"] == rows[newer]["id"], (name, older)
    # Nothing else retired or left flagged; the vector rows say the same.
    for k in KEPT:
        assert (rows[k]["status"], rows[k]["conflict_with"]) == ("active", []), (k, rows[k])
    vectors = await _vector_status(ch, org)
    assert {k for k, s in vectors.items() if s == "superseded"} == OLDER
    assert {k for k, s in vectors.items() if s != "superseded"} == CURRENT | KEPT
    # The pinned row, the unsure pairs and the malformed answer's pair: not rewritten.
    for k in {"code_freeze"} | LOW_CONFIDENCE | {"language", "language_again"}:
        assert int(rows[k]["version"]) == run.written[k], k
    assert rows["code_freeze"]["pinned"] and rows["freeze_change"]["status"] == "active"

    # One retirement record per older value, each naming the row it retired behind.
    records = await _audit(ch, org, "memory.auto_supersede")
    retired = {
        (e["memory_id"], e["superseded_by"])
        for r in records
        if r.get("reason") == "conflict_resolver"
        for e in r["retired"]
    }
    assert retired == {(rows[k]["id"], rows[k]["superseded_by"]) for k in OLDER}
    assert all(r.get("reason") == "conflict_resolver" for r in records)

    # The fact graph: the current values are open, every older one is closed.
    graph = await _triples(ch, org, {f.obj for f in FACTS if f.key in OLDER})
    assert {f.obj: graph.get(f.obj) for f in FACTS if f.obj} == {
        f.obj: f.key in CURRENT for f in FACTS if f.obj
    }

    t = run.totals
    assert t["retired"] == len(OLDER) and t["flagged"] == 0, t
    assert t["cleared"] == len(FLAGGED_AT_WRITE), t
    assert t["errored"] == 1 and t["skipped_pinned"] >= 1, t
    assert t["skipped_low_confidence"] >= len(LOW_CONFIDENCE) // 2, t
    assert not set(run.judge.calls) & SOLO

    # Another drain: nothing owed, nothing changes, the judge is not called.
    calls = len(run.judge.calls)
    again = await run_pass()
    assert len(run.judge.calls) == calls and again["judged"] == 0, again
    assert _snapshot(await _rows(ch, org)) == _snapshot(rows)

    # Search answers each attribute with its current value, though the first value
    # sits nearest the question.
    for name, a in ATTRIBUTES.items():
        found = await handle_memory_search(
            MemorySearchInput(org=org, namespace=NS, agent="*", query=a.query)
        )
        top = [h.memory_key for h in found.results]
        assert top and top[0] == a.keys[-1], (name, top)

    # Every retirement can be undone.
    for k in sorted(OLDER):
        undone = await handle_memory_restore(
            MemoryRestoreInput(
                **_identity(org, BY_KEY[k].session),
                memory_key=k,
                if_superseded_by=rows[k]["superseded_by"],
            )
        )
        assert undone.restored, k
    after = await _rows(ch, org)
    # Each is active again. `ci` was flagged at its write, but the retirement cleared the
    # winner's side of that flag, so no peer lists it back and nothing is left to review.
    assert {k: after[k]["status"] for k in OLDER} == dict.fromkeys(OLDER, "active")
