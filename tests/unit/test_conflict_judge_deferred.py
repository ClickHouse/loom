"""The conflict judge off the write's critical path.

`check_on_commit(defer_judge=True)` answers `pending` with a `PendingJudge`
exactly where the inline path would have called the LLM; every deterministic
tier (canonical merge, pinned 409, duplicate) still answers inline.
`settle_deferred_judgement` runs the same judge-and-fold the inline path runs,
so the two verdicts are identical for the same labels. `memory.set` inserts
the row active, returns `pending` / `verdict_at: deferred`, and a task lands
the settled verdict on the row; `await_verdict` and `dry_run` keep the
inline judge. The prefetch folds the namespace's write-time reads into one
concurrent round trip and the check issues none of them again.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace as NS
from typing import Any

import pytest

import server.memory.app.handlers as mem_handlers
from server.memory.api.models_crud import MemoryGetOutput, MemorySetInput, MemorySetOutput
from server.memory.app import conflict_deferred as cd
from server.memory.app import conflicts as cf
from server.memory.app import restore as _restore_mod
from server.memory.app import set_commit as set_commit
from server.memory.app import stack as _stack_mod
from server.memory.app import write as write_mod
from server.memory.domain import conflicts as conflict_rules
from server.memory.domain.conflicts import ConflictPeer, ConflictResult, PendingJudge
from server.settings import settings

_NOW = datetime(2026, 9, 16, 8, 0, 0)
_P1 = "11111111-1111-1111-1111-111111111111"


# --- check_on_commit: where the judge would run, `pending` is answered --------


def _row(mid: str, key: str, value: str, dist: float, pinned: int = 0, run_id: str = "-") -> dict:
    return {
        "memory_id": mid,
        "memory_key": key,
        "value": value,
        "pinned": pinned,
        "agent": "ag",
        "user_id": "u",
        "session_id": "s",
        "run_id": run_id,
        "dist": dist,
        "valid_at": None,
    }


class _RowsCH:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.sql: list[str] = []

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        self.sql.append(sql)
        if "loom.conflict_allowlist" in sql:
            return []
        return self.rows

    async def query_one(self, sql: str, params: Any = None) -> dict[str, Any] | None:
        self.sql.append(sql)
        return None


class _LLM:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0

    async def complete(self, system: str, user: str, **kw: Any) -> Any:
        self.calls += 1
        return NS(text=self.text)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> NS:
    import server.llm.client as llm_client

    e = NS(llm=_LLM(f'{{"labels": {{"{_P1}": "unrelated"}}}}'))
    e.rows = [_row(_P1, "old_key", "PR 1 has a bug in the hash join", dist=0.08)]
    e.ch = _RowsCH(e.rows)
    monkeypatch.setattr(cf, "get_client", lambda: e.ch)
    monkeypatch.setattr("server.core.ch.get_client", lambda: e.ch)

    async def thr(*a: Any, **k: Any) -> float:
        return 0.85

    async def dthr(*a: Any, **k: Any) -> float:
        return 0.97

    async def allow(*a: Any, **k: Any) -> set[str]:
        return set()

    async def policy(*a: Any, **k: Any) -> str:
        return "auto_reinforce"

    monkeypatch.setattr(cf, "_resolve_threshold", thr)
    monkeypatch.setattr(cf, "_resolve_duplicate_threshold", dthr)
    monkeypatch.setattr(cf, "_allowlisted_keys", allow)
    monkeypatch.setattr("server.memory.app.conflicts.namespace_conflict_policy", policy)
    monkeypatch.setattr(llm_client, "get_llm_client", lambda: e.llm)
    monkeypatch.setattr(llm_client, "get_llm_client_for_model", lambda m: e.llm)
    monkeypatch.setattr(settings, "memory_conflict_judge", True)
    monkeypatch.setattr(settings, "memory_conflict_judge_min_similarity", 0.70)
    cd._reset_for_tests()
    return e


async def _check(env: NS, *, defer: bool, value: str = "PR 1 is about the merge join", **kw: Any):
    return await cf.check_on_commit(
        org="o",
        namespace="n",
        agent="ag",
        memory_type="semantic",
        memory_key="new_key",
        new_value=value,
        new_embedding=[0.1, 0.2, 0.3],
        defer_judge=defer,
        **kw,
    )


@pytest.mark.asyncio
async def test_a_flagged_pair_defers_with_conflict_as_the_cosine_fallback(env: NS) -> None:
    res = await _check(env, defer=True)
    assert res.verdict == "pending" and res.pending_judge is not None
    assert env.llm.calls == 0, "the judge did not run on the write"
    pj = res.pending_judge
    assert pj.fallback_verdict == "conflict"
    assert [p.memory_id for p in pj.cosine_peers] == [_P1]
    assert [p.memory_id for p in pj.candidates] == [_P1]
    assert [p.memory_id for p in res.peers] == [_P1], "the response still names the peers"


@pytest.mark.asyncio
async def test_a_band_only_pair_defers_with_clear_as_the_cosine_fallback(env: NS) -> None:
    env.rows[0]["dist"] = 0.22  # similarity 0.78: in the judge's band, under the 0.85 flag
    res = await _check(env, defer=True)
    assert res.verdict == "pending"
    assert res.pending_judge.fallback_verdict == "clear"
    assert res.pending_judge.cosine_peers == []
    assert [p.memory_id for p in res.pending_judge.candidates] == [_P1]


@pytest.mark.asyncio
async def test_the_judge_switched_off_never_defers(env: NS, monkeypatch) -> None:
    monkeypatch.setattr(settings, "memory_conflict_judge", False)
    flagged = await _check(env, defer=True)
    assert flagged.verdict == "conflict" and flagged.pending_judge is None
    env.rows[0]["dist"] = 0.22
    band = await _check(env, defer=True)
    assert band.verdict == "clear" and band.pending_judge is None


@pytest.mark.asyncio
async def test_deterministic_tiers_answer_inline_even_when_deferring(env: NS) -> None:
    # canonical match → merge (no judge, no pending)
    res = await _check(env, defer=True, value="PR 1 has a bug in the hash join")
    assert res.verdict == "merge" and res.existing_memory_id == _P1
    # canonical match under another run → the run's duplicate
    res = await _check(env, defer=True, value="PR 1 has a bug in the hash join", new_run_id="r9")
    assert res.verdict == "duplicate" and res.duplicate_of_id == _P1
    # pinned close-but-different peer → rejected
    env.rows[0]["pinned"] = 1
    res = await _check(env, defer=True)
    assert res.verdict == "rejected" and res.pinned_blocker_id == _P1
    env.rows[0]["pinned"] = 0
    # above the duplicate threshold with the same content words → duplicate
    env.rows[0]["dist"] = 0.01
    res = await _check(env, defer=True, value="the PR 1 has a bug in the hash join")
    assert res.verdict == "duplicate" and res.duplicate_of_id == _P1
    assert env.llm.calls == 0


# --- parity: settling the deferred judge == the inline judge -----------------


def _shape(res: ConflictResult) -> tuple:
    return (
        res.verdict,
        [p.memory_id for p in res.peers],
        [p.memory_id for p in res.refined_peers],
        res.existing_memory_id,
        res.duplicate_of_id,
        res.refine_relation,
        dict(res.judged),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "label", ["contradiction", "elaboration", "restatement", "related", "unrelated"]
)
@pytest.mark.parametrize("dist", [0.08, 0.22])
@pytest.mark.parametrize("run_id", ["-", "r1"])
async def test_the_settled_verdict_equals_the_inline_verdict(
    env: NS, label: str, dist: float, run_id: str
) -> None:
    env.rows[0]["dist"] = dist
    env.llm.text = f'{{"labels": {{"{_P1}": "{label}"}}}}'
    inline = await _check(env, defer=False, new_run_id=run_id)
    assert env.llm.calls == 1
    deferred = await _check(env, defer=True, new_run_id=run_id)
    assert deferred.verdict == "pending"
    settled = await cf.settle_deferred_judgement(deferred.pending_judge)
    assert env.llm.calls == 2
    assert _shape(settled) == _shape(inline)
    expected = {
        "contradiction": "conflict",
        "elaboration": "refines",
        "related": "clear",
        "unrelated": "clear",
        "restatement": "merge" if run_id == "-" else "duplicate",
    }[label]
    assert settled.verdict == expected


@pytest.mark.asyncio
async def test_an_unparsable_judge_settles_to_the_cosine_fallback(env: NS) -> None:
    env.llm.text = "no json here"
    flagged = await _check(env, defer=True)
    settled = await cf.settle_deferred_judgement(flagged.pending_judge)
    assert settled.verdict == "conflict" and [p.memory_id for p in settled.peers] == [_P1]
    env.rows[0]["dist"] = 0.22
    band = await _check(env, defer=True)
    settled = await cf.settle_deferred_judgement(band.pending_judge)
    assert settled.verdict == "clear" and settled.peers == []


# --- prefetch: the namespace's write-time reads in one round trip ------------


@pytest.mark.asyncio
async def test_prefetch_reads_type_row_namespace_row_and_allowlist_once(monkeypatch) -> None:
    ch = _RowsCH([])
    monkeypatch.setattr(cf, "get_client", lambda: ch)
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)

    async def _thr(default: float, *a: Any, **k: Any) -> float:
        return default

    monkeypatch.setattr(
        "server.memory.app.embedder_calibration.effective_conflict_threshold_for_ns", _thr
    )
    monkeypatch.setattr(
        "server.memory.app.embedder_calibration.effective_duplicate_threshold_for_ns", _thr
    )
    cfg = await cf.prefetch_write_config("o", "n", "semantic", "k1")
    assert cfg.threshold == settings.conflict_threshold
    assert cfg.duplicate_threshold == settings.duplicate_threshold
    assert cfg.policy == "auto_reinforce" and cfg.allowlisted == set()
    assert cfg.memory_type == "semantic" and cfg.type_config is not None
    tables = [
        t
        for t in ("loom.namespace_types", "loom.namespaces", "loom.conflict_allowlist")
        if any(t in s for s in ch.sql)
    ]
    assert len(ch.sql) == 3 and len(tables) == 3, ch.sql
    ns_read = next(s for s in ch.sql if "loom.namespaces FINAL" in s)
    for col in ("conflict_threshold", "duplicate_threshold", "default_conflict_policy"):
        assert col in ns_read, "the three per-read columns come back in one statement"


@pytest.mark.asyncio
async def test_the_check_with_prefetched_config_issues_only_the_peer_statement(monkeypatch) -> None:
    ch = _RowsCH([_row(_P1, "old_key", "PR 1 has a bug in the hash join", dist=0.08)])
    monkeypatch.setattr(cf, "get_client", lambda: ch)
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    monkeypatch.setattr(settings, "memory_conflict_judge", True)
    cfg = conflict_rules.WriteConfig(0.85, 0.97, "auto_reinforce", set(), None, "semantic")
    res = await cf.check_on_commit(
        org="o",
        namespace="n",
        agent="ag",
        memory_type="semantic",
        memory_key="new_key",
        new_value="PR 1 is about the merge join",
        new_embedding=[0.1, 0.2, 0.3],
        defer_judge=True,
        **cfg.check_kwargs(),
    )
    assert res.verdict == "pending" and res.pending_judge.policy == "auto_reinforce"
    # The peer read is two statements — the vectors, then the
    # candidates' rows — and nothing else.
    assert len(ch.sql) == 2 and "loom.memory_embeddings" in ch.sql[0], ch.sql
    assert "{ids:Array(Tuple" in ch.sql[1], ch.sql


@pytest.mark.asyncio
async def test_a_failed_prefetch_falls_back_to_per_read(monkeypatch) -> None:
    class Boom:
        async def query_one(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("down")

        async def query_all(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("down")

    monkeypatch.setattr(cf, "get_client", lambda: Boom())
    monkeypatch.setattr("server.core.ch.get_client", lambda: Boom())
    cfg = await cf.prefetch_write_config("o", "n", "semantic", "k1")
    assert cfg == conflict_rules.WriteConfig()
    assert all(v is None for v in cfg.check_kwargs().values())


# --- memory.set: the row lands first, the verdict follows --------------------


class _CH:
    def __init__(self) -> None:
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []

    async def insert(self, table: str, rows: list[dict[str, Any]], **_: Any) -> None:
        """The graph-write marks a scheduled projection flushes are not the write under test."""
        if table not in ("loom.namespace_write_marks", "loom.graph_projections"):
            self.inserts.append((table, rows))

    async def query_one(self, *_: Any, **__: Any) -> dict[str, Any] | None:
        return None

    async def query_all(self, *_: Any, **__: Any) -> list[dict[str, Any]]:
        return []

    async def execute(self, *_: Any, **__: Any) -> None:
        return None


class _Embedder:
    model_name = "fake"
    dim = 4

    async def embed(self, _text: str) -> list[float]:
        return [0.1, 0.2, 0.3, 0.4]


def _peer(**kw: Any) -> ConflictPeer:
    base: dict[str, Any] = {
        "memory_id": _P1,
        "memory_key": "deploy_day",
        "similarity": 0.91,
        "pinned": False,
        "canonical": "deploys happen on tuesday",
        "value": '"Deploys happen on Tuesday"',
        "valid_at": _NOW - timedelta(days=10),
    }
    base.update(kw)
    return ConflictPeer(**base)


def _pending(peers: list[ConflictPeer]) -> ConflictResult:
    pj = PendingJudge("test", "personal", '"Deploys happen on Thursday"', peers, peers, "conflict")
    return ConflictResult(verdict="pending", peers=peers, pending_judge=pj)


def _wire(monkeypatch: pytest.MonkeyPatch, verdict: ConflictResult) -> tuple[_CH, NS]:
    ch, calls = (
        _CH(),
        NS(
            check=[],
            marked=[],
            refined=[],
            versions=[],
            usage=[],
            audit=[],
            stamped=[],
            stamp_sha=[],
            current=[],
            still_current=True,
            row_sha=None,  # the live row's hash; None = the write's own
            mark_answer="conflict",  # what the peer mark reports
        ),
    )
    monkeypatch.setattr(mem_handlers, "get_client", lambda: ch)
    monkeypatch.setattr(_stack_mod, "get_client", lambda: ch)
    monkeypatch.setattr(_restore_mod, "get_client", lambda: ch)
    monkeypatch.setattr("server.memory.app.write.get_client", mem_handlers.get_client)
    monkeypatch.setattr("server.memory.app.update.get_client", mem_handlers.get_client)
    monkeypatch.setattr(mem_handlers, "get_embedder", lambda: _Embedder())
    monkeypatch.setattr("server.memory.app.set_commit.get_embedder", mem_handlers.get_embedder)
    monkeypatch.setattr("server.memory.app.update.get_embedder", mem_handlers.get_embedder)
    monkeypatch.setattr("server.memory.app.lock.get_client", lambda: ch, raising=False)
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    import server.memory.app.blacklist_guard as _bl

    monkeypatch.setattr(_bl, "get_client", lambda: ch, raising=False)
    monkeypatch.setattr(cf, "get_client", lambda: ch, raising=False)

    async def _none(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr("server.memory.store.get_by_key", _none)
    monkeypatch.setattr("server.memory.store.get_by_id", _none)

    async def _check(**kw: Any) -> ConflictResult:
        calls.check.append(kw)
        return verdict

    async def _mark(**kw: Any) -> str:
        calls.marked.append(kw)
        return calls.mark_answer

    async def _refines(**kw: Any) -> int:
        calls.refined.append(kw)
        return 1

    async def _versions(org: str, ids: list[str], update: Any, skip: Any = None, **kw: Any) -> int:
        written = [r for t, rows in ch.inserts if t == "loom.memory" for r in rows]
        sha = calls.row_sha if calls.row_sha is not None else written[-1]["content_sha256"]
        row = NS(memory_id=ids[0], conflict_with=[], version=7, content_sha256=sha)
        if skip is not None and skip(row):
            return 0
        calls.versions.append((org, ids, update(row)))
        return 1

    async def _usage(**kw: Any) -> None:
        calls.usage.append(kw)

    async def _audit(event_type: str, **kw: Any) -> str:
        calls.audit.append({"event_type": event_type, **kw})
        return "hash"

    async def _stamp(ident: Any, **kw: Any) -> int:
        calls.stamped.append(ident.memory_key)
        calls.stamp_sha.append(kw.get("content_sha256"))
        return 1

    async def _current(ident: Any, sha: str) -> bool:
        calls.current.append(sha)
        return calls.still_current

    monkeypatch.setattr(cd, "stamp_checked", _stamp)
    monkeypatch.setattr(cd, "still_current", _current)
    monkeypatch.setattr(cf, "check_on_commit", _check)
    monkeypatch.setattr(cf, "mark_peers_conflicted", _mark)
    monkeypatch.setattr(cf, "apply_refines", _refines)
    monkeypatch.setattr("server.memory.store.write_versions_by_ids", _versions)
    monkeypatch.setattr("server.memory.store.record_usage_event", _usage)
    monkeypatch.setattr("server.observability.audit.emit", _audit)
    monkeypatch.setattr("server.memory.app.key_supersession.supersede_same_key_rows", _none)
    monkeypatch.setattr(set_commit, "_project_set_entities_fail_open", _none)
    monkeypatch.setattr(settings, "memory_conflict_judge", True)
    from server.memory.app import drops

    drops._reset_for_tests()
    cd._reset_for_tests()
    return ch, calls


def _input(**kw: Any) -> MemorySetInput:
    base: dict[str, Any] = {
        "org": "test",
        "namespace": "personal",
        "agent": "cursor",
        "memory_key": "release_day",
        "value": "Deploys happen on Thursday",
        "classify": False,
    }
    base.update(kw)
    return MemorySetInput(**base)


async def _settled() -> None:
    if cd._TASKS:
        await asyncio.gather(*list(cd._TASKS))


@pytest.mark.asyncio
async def test_a_pending_write_lands_active_and_says_so(monkeypatch) -> None:
    ch, calls = _wire(monkeypatch, _pending([_peer()]))
    gate = asyncio.Event()  # the judge takes ~1 s in production; hold it until asserted

    async def _settle(pj: PendingJudge) -> ConflictResult:
        await gate.wait()
        return ConflictResult(verdict="clear", peers=[])

    monkeypatch.setattr(cf, "settle_deferred_judgement", _settle)
    out = await write_mod.handle_memory_set(_input())
    assert out.conflict_verdict == "pending" and out.verdict_at == "deferred"
    assert out.noop is False and out.version > 0 and out.duplicate_of == ""
    assert [p.memory_key for p in out.peers] == ["deploy_day"]
    assert "memory.get" in out.next and "await_verdict" in out.next
    tables = sorted(t for t, _ in ch.inserts)
    assert tables == ["loom.memory", "loom.memory_embeddings"]
    assert all(r.get("status", "active") == "active" for _, rows in ch.inserts for r in rows)
    assert calls.check[0]["defer_judge"] is True
    assert calls.audit[0]["subject"]["verdict"] == "pending"
    assert cd.is_pending(out.memory_id), "the judge is in flight after the response"
    (emb,) = [rows[0] for t, rows in ch.inserts if t == "loom.memory_embeddings"]
    assert emb["conflict_checked_at"] == cd.conflict_marks.PENDING_MARK, "owed, on the row"
    assert calls.stamped == []
    gate.set()
    await _settled()
    assert not cd.is_pending(out.memory_id)
    assert calls.stamped == ["release_day"], "the settle stamps the row"
    assert calls.stamp_sha == [emb["content_sha256"]], "by the write's content hash"
    assert calls.marked == [] and calls.refined == [] and calls.versions == []
    from server.memory.app import drops

    rows = [r for r in drops._peek_for_tests() if r["stage"] == cd.DROP_STAGE]
    assert [(r["reason"], r["item"]) for r in rows] == [("clear", "release_day")]


@pytest.mark.asyncio
async def test_a_verdict_for_a_write_a_newer_revision_replaced_lands_nothing(monkeypatch) -> None:
    """The key's memory_id is shared by its revisions: a judge that settles after a
    newer write must not land its verdict on that write, nor stamp its mark."""
    ch, calls = _wire(monkeypatch, _pending([_peer()]))

    async def _settle(pj: PendingJudge) -> ConflictResult:
        calls.still_current = False  # memory.update_by_id landed while the judge ran
        return ConflictResult(verdict="conflict", peers=[_peer()], judged={_P1: "contradiction"})

    monkeypatch.setattr(cf, "settle_deferred_judgement", _settle)
    await write_mod.handle_memory_set(_input())
    await _settled()
    (emb,) = [rows[0] for t, rows in ch.inserts if t == "loom.memory_embeddings"]
    assert calls.current == [emb["content_sha256"]] and emb["content_sha256"]
    assert calls.marked == [] and calls.stamped == []
    from server.memory.app import drops

    rows = [r for r in drops._peek_for_tests() if r["stage"] == cd.DROP_STAGE]
    assert [r["reason"] for r in rows] == [cd.MOVED]


@pytest.mark.asyncio
async def test_a_settled_conflict_marks_the_new_row_and_its_peers(monkeypatch) -> None:
    ch, calls = _wire(monkeypatch, _pending([_peer()]))

    async def _settle(pj: PendingJudge) -> ConflictResult:
        return ConflictResult(verdict="conflict", peers=[_peer()], judged={_P1: "contradiction"})

    monkeypatch.setattr(cf, "settle_deferred_judgement", _settle)
    out = await write_mod.handle_memory_set(_input())
    await _settled()
    (emb,) = [rows[0] for t, rows in ch.inserts if t == "loom.memory_embeddings"]
    assert calls.marked == [
        {
            "org": "test",
            "new_memory_id": out.memory_id,
            "peer_ids": [_P1],
            "expected_sha256": emb["content_sha256"],
        }
    ]


@pytest.mark.asyncio
async def test_a_settled_conflict_with_every_peer_retired_since_is_clear(monkeypatch) -> None:
    # Another writer retired the peers: nothing is flagged, and the row stands active.
    ch, calls = _wire(monkeypatch, _pending([_peer()]))
    calls.mark_answer = "clear"

    async def _settle(pj: PendingJudge) -> ConflictResult:
        return ConflictResult(verdict="conflict", peers=[_peer()], judged={_P1: "contradiction"})

    monkeypatch.setattr(cf, "settle_deferred_judgement", _settle)
    await write_mod.handle_memory_set(_input())
    await _settled()
    from server.memory.app import drops

    rows = [r for r in drops._peek_for_tests() if r["stage"] == cd.DROP_STAGE]
    assert [r["reason"] for r in rows] == ["clear"]


@pytest.mark.asyncio
async def test_a_settled_refines_supersedes_the_peers(monkeypatch) -> None:
    ch, calls = _wire(monkeypatch, _pending([_peer()]))

    async def _settle(pj: PendingJudge) -> ConflictResult:
        return ConflictResult(
            verdict="refines",
            peers=[_peer()],
            refined_peers=[_peer()],
            refine_relation="supersedes",
        )

    monkeypatch.setattr(cf, "settle_deferred_judgement", _settle)
    out = await write_mod.handle_memory_set(_input(run_id="r1"))
    await _settled()
    assert len(calls.refined) == 1
    kw = calls.refined[0]
    assert kw["new_memory_id"] == out.memory_id and kw["new_memory_key"] == "release_day"
    assert kw["relation"] == "supersedes" and kw["run_id"] == "r1"
    assert [p.memory_id for p in kw["refined_peers"]] == [_P1]


@pytest.mark.asyncio
async def test_a_settled_merge_makes_this_row_the_duplicate_and_reinforces_the_peer(
    monkeypatch,
) -> None:
    ch, calls = _wire(monkeypatch, _pending([_peer()]))

    async def _settle(pj: PendingJudge) -> ConflictResult:
        return ConflictResult(verdict="merge", peers=[_peer()], existing_memory_id=_P1)

    monkeypatch.setattr(cf, "settle_deferred_judgement", _settle)
    out = await write_mod.handle_memory_set(_input())
    await _settled()
    assert calls.versions == [
        ("test", [out.memory_id], {"status": "duplicate", "duplicate_of": _P1, "version": 8})
    ]
    assert len(calls.usage) == 1 and calls.usage[0]["memory_id"] == _P1
    assert calls.usage[0]["event_type"] == "evidence"
    from server.memory.app import drops

    rows = [r for r in drops._peek_for_tests() if r["stage"] == cd.DROP_STAGE]
    assert [r["reason"] for r in rows] == ["merge"]


@pytest.mark.asyncio
async def test_a_settled_duplicate_points_at_the_peer_without_reinforcing(monkeypatch) -> None:
    ch, calls = _wire(monkeypatch, _pending([_peer()]))

    async def _settle(pj: PendingJudge) -> ConflictResult:
        return ConflictResult(verdict="duplicate", peers=[_peer()], duplicate_of_id=_P1)

    monkeypatch.setattr(cf, "settle_deferred_judgement", _settle)
    out = await write_mod.handle_memory_set(_input(run_id="r1"))
    await _settled()
    assert calls.versions == [
        ("test", [out.memory_id], {"status": "duplicate", "duplicate_of": _P1, "version": 8})
    ]
    assert calls.usage == []


@pytest.mark.asyncio
async def test_a_failing_settle_leaves_the_row_and_records_error(monkeypatch) -> None:
    ch, calls = _wire(monkeypatch, _pending([_peer()]))

    async def _settle(pj: PendingJudge) -> ConflictResult:
        raise RuntimeError("judge down")

    monkeypatch.setattr(cf, "settle_deferred_judgement", _settle)
    out = await write_mod.handle_memory_set(_input())
    await _settled()
    assert not cd.is_pending(out.memory_id)
    assert calls.stamped == [], "a failed settle leaves the mark for the conflict sweep"
    from server.memory.app import drops

    rows = [r for r in drops._peek_for_tests() if r["stage"] == cd.DROP_STAGE]
    assert [r["reason"] for r in rows] == ["error"]


@pytest.mark.asyncio
async def test_await_verdict_and_dry_run_keep_the_inline_judge(monkeypatch) -> None:
    ch, calls = _wire(monkeypatch, ConflictResult(verdict="conflict", peers=[_peer()]))
    out = await write_mod.handle_memory_set(_input(await_verdict=True))
    assert calls.check[-1]["defer_judge"] is False
    assert out.conflict_verdict == "conflict" and out.verdict_at == "inline"
    (emb,) = [rows[0] for t, rows in ch.inserts if t == "loom.memory_embeddings"]
    assert not cd.conflict_marks.is_mark(emb["conflict_checked_at"]), "settled inline"
    assert emb["conflict_checked_at"] is not None
    assert len(calls.marked) == 1, "the inline conflict still marks its peers on the write"
    out = await write_mod.handle_memory_set(_input(dry_run=True))
    assert calls.check[-1]["defer_judge"] is False and out.verdict_at == "inline"
    assert cd._TASKS == set()


@pytest.mark.asyncio
async def test_the_check_receives_the_prefetched_config_and_supersession_the_policy(
    monkeypatch,
) -> None:
    _, calls = _wire(monkeypatch, ConflictResult(verdict="clear", peers=[]))
    seen: list[dict[str, Any]] = []

    async def _supersede(**kw: Any) -> int:
        seen.append(kw)
        return 0

    monkeypatch.setattr("server.memory.app.key_supersession.supersede_same_key_rows", _supersede)
    await write_mod.handle_memory_set(_input())
    kw = calls.check[0]
    assert kw["resolved_policy"] == "auto_reinforce" and kw["allowlisted_keys"] == set()
    assert kw["resolved_threshold"] is not None and kw["resolved_duplicate_threshold"] is not None
    assert seen[0]["policy"] == "auto_reinforce", "same-key supersession does not re-read it"


@pytest.mark.asyncio
async def test_no_check_means_no_verdict_at(monkeypatch) -> None:
    _, calls = _wire(monkeypatch, ConflictResult(verdict="clear", peers=[]))
    out = await write_mod.handle_memory_set(_input(memory_type="episodic"))
    assert calls.check == [] and out.conflict_verdict == "" and out.verdict_at == ""


# --- memory.get and the wire defaults ----------------------------------------


def test_verdict_from_row_derives_from_status_and_pending() -> None:
    cd._reset_for_tests()
    assert cd.verdict_from_row("m", "active") == "clear"
    assert cd.verdict_from_row("m", "conflicted") == "conflict"
    assert cd.verdict_from_row("m", "duplicate", _P1) == "duplicate"
    assert cd.verdict_from_row("m", "active", _P1) == "duplicate"
    assert cd.verdict_from_row("m", "superseded") == "superseded"
    assert cd.verdict_from_row("m", None) == ""
    cd._PENDING["m"] = 0.0
    assert cd.verdict_from_row("m", "active") == "clear", "process memory decides nothing"
    assert cd.verdict_from_row("m", "active", pending=True) == "pending"
    cd._reset_for_tests()


def test_wanted_and_verdict_at() -> None:
    assert cd.wanted(_input()) is True
    assert cd.wanted(_input(await_verdict=True)) is False
    assert cd.wanted(_input(dry_run=True)) is False
    assert cd.verdict_at("") == "" and cd.verdict_at("pending") == "deferred"
    assert cd.verdict_at("clear") == "inline" and cd.verdict_at("merge") == "inline"


def test_new_fields_default_empty_so_unset_payloads_are_unchanged() -> None:
    assert _input().await_verdict is False
    assert "await_verdict" not in _input().model_dump(exclude_defaults=True)
    assert MemorySetOutput(version=1).verdict_at == ""
    got = MemoryGetOutput(found=False)
    assert got.duplicate_of == "" and got.conflict_verdict == ""


def test_the_ledger_projects_verdict_at() -> None:
    from server.observability.event_args import project_result

    out = project_result(
        MemorySetOutput(version=1, conflict_verdict="pending", verdict_at="deferred")
    )
    assert out.get("out_verdict_at") == "deferred" and out.get("out_conflict_verdict") == "pending"


@pytest.mark.asyncio
async def test_drain_waits_for_in_flight_judges() -> None:
    cd._reset_for_tests()
    gate = asyncio.Event()

    async def _slow() -> str:
        await gate.wait()
        return "clear"

    t = asyncio.create_task(_slow())
    cd._TASKS.add(t)
    gate.set()
    assert await cd.drain(timeout_s=1.0) == 1
    assert t.done() and await cd.drain(timeout_s=0.1) == 0
    cd._reset_for_tests()


@pytest.mark.asyncio
async def test_a_revision_committed_after_the_check_keeps_its_own_status(monkeypatch) -> None:
    """still_current() passes, then revision B commits before the verdict lands:
    the duplicate flip carries the judged write's hash and leaves B alone."""
    ch, calls = _wire(monkeypatch, _pending([_peer()]))

    async def _settle(pj: PendingJudge) -> ConflictResult:
        return ConflictResult(verdict="duplicate", peers=[_peer()], duplicate_of_id=_P1)

    async def _current(ident: Any, sha: str) -> bool:
        calls.current.append(sha)
        calls.row_sha = "b" * 64  # B commits right after the check
        return True

    monkeypatch.setattr(cf, "settle_deferred_judgement", _settle)
    monkeypatch.setattr(cd, "still_current", _current)
    await write_mod.handle_memory_set(_input())
    await _settled()
    assert calls.current and calls.versions == [] and calls.stamped == []
    from server.memory.app import drops

    rows = [r for r in drops._peek_for_tests() if r["stage"] == cd.DROP_STAGE]
    assert [r["reason"] for r in rows] == [cd.MOVED]


async def test_a_refines_verdict_retires_no_peer_once_a_newer_revision_took_the_row(
    monkeypatch,
) -> None:
    """B commits between still_current() and apply_refines(): its re-read right
    before the supersede sees B's hash, so no peer is retired or linked."""
    from types import SimpleNamespace

    import server.memory.store as memory_repo
    from server.memory.app import ops

    reads = iter([SimpleNamespace(status="active", content_sha256="a" * 64)])

    async def _get(*_: Any, **__: Any) -> Any:
        return next(reads, SimpleNamespace(status="active", content_sha256="b" * 64))

    retired: list[Any] = []
    linked: list[Any] = []

    async def _supersede(org: str, victims: list[Any], **_: Any) -> int:
        retired.extend(victims)
        return len(victims)

    async def _no_truncation(*_: Any, **__: Any) -> set[str]:
        return set()

    async def _link(inp: Any) -> None:
        linked.append(inp)

    monkeypatch.setattr(memory_repo, "get_by_id", _get)
    monkeypatch.setattr(memory_repo, "supersede_duplicates", _supersede)
    monkeypatch.setattr(cf, "_truncating_refines", _no_truncation)
    monkeypatch.setattr(ops, "handle_memory_link", _link)
    n = await cf.apply_refines(
        org="o",
        namespace="n",
        new_memory_id="new",
        new_memory_key="k-new",
        refined_peers=[_peer()],
        expected_sha256="a" * 64,
    )
    assert n == 0 and retired == [] and linked == []
