"""Three workers poll the work, not the table.

- the supervisor sleeps a spec's idle interval after a clean pass that
  marked itself idle, and the interval otherwise;
- code_embed_recovery scans every repo in one statement per table per
  round, keyed above a version watermark that the scan's own ORDER BY makes
  exact, reads the full rows by exact key only for what the scan returned,
  writes one insert per table per round, and scans from 0 on its slow clock;
- entity_extractor reads the cost cap once per pass for every namespace of
  its rows and writes the pass's rows in one batch (five statements, not
  five per row); the batch write emits the same rows as N single writes;
- the cost-cap verdict for N namespaces is one statement, fail-open;
- embedding_worker marks itself idle only when all three legs found nothing;
- every pass records its counts as outcome scalars.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import pytest

from server.code.store.index import CodeRepo as _RealCodeRepo
from server.settings import settings
from server.workers import pass_context
from server.workers import supervisor as sup
from server.workers.registry import WorkerSpec, enabled_workers

# ---------------------------------------------------------------- supervisor


def test_next_interval_is_the_idle_interval_only_for_a_clean_idle_pass_on_a_spec_that_has_one() -> (
    None
):
    async def _noop() -> None:
        pass

    with_idle = WorkerSpec("w", 5, _noop, idle_interval_seconds=30)
    without = WorkerSpec("w", 5, _noop)
    idle = pass_context.PassContext(worker="w", idle=True)
    busy = pass_context.PassContext(worker="w")
    assert sup.next_interval_seconds(with_idle, idle) == 30.0
    assert sup.next_interval_seconds(with_idle, busy) == 5.0
    assert (
        sup.next_interval_seconds(with_idle, idle, partial=True) == 5.0
    )  # a failed leg keeps the interval
    assert sup.next_interval_seconds(without, idle) == 5.0
    assert sup.next_interval_seconds(with_idle, None) == 5.0


def test_a_failed_leg_on_a_long_interval_worker_retries_within_the_hour_then_backs_off() -> None:
    async def _noop() -> None:
        pass

    weekly = WorkerSpec("w", 7 * 24 * 3600, _noop)
    ctx = pass_context.PassContext(worker="w")
    assert sup.next_interval_seconds(weekly, ctx, partial=True, streak=1) == 3600.0
    assert sup.next_interval_seconds(weekly, ctx, partial=True, streak=3) == 4 * 3600.0
    assert sup.next_interval_seconds(weekly, ctx, partial=True, streak=20) == 7 * 24 * 3600.0
    assert sup.next_interval_seconds(weekly, ctx) == 7 * 24 * 3600.0  # a clean pass waits the week


async def test_the_supervisor_sleeps_the_idle_interval_after_an_idle_pass_and_the_interval_after_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.observability import events as ev_mod
    from server.observability import genai

    class _Em:
        def emit(self, e: Any) -> None:
            pass

    monkeypatch.setattr(ev_mod, "get_emitter", lambda: _Em())
    slept: list[float] = []

    async def _idle() -> None:
        pass_context.mark_idle()

    async def _busy() -> None:
        pass

    def _one() -> sup.WorkerSupervisor:
        s = sup.WorkerSupervisor()

        async def _sleep(seconds: float) -> None:
            slept.append(seconds)
            s._stopping.set()

        s._sleep_or_drain = _sleep  # type: ignore[method-assign]
        return s

    for fn, expect in ((_idle, 30.0), (_busy, 5.0)):
        genai.set_identity()
        await asyncio.wait_for(
            _one()._supervise(WorkerSpec("w", 5.0, fn, idle_interval_seconds=30.0)),
            timeout=5,
        )
        assert slept[-1] == expect, (fn.__name__, slept)
    genai.set_identity()


def test_mark_idle_outside_a_pass_is_quiet_and_inside_sets_the_flag() -> None:
    pass_context.clear()
    pass_context.mark_idle()  # no context: a debug line, never an error
    ctx = pass_context.begin("w")
    try:
        assert ctx.idle is False
        pass_context.mark_idle()
        assert ctx.idle is True
    finally:
        pass_context.clear()


def test_the_three_workers_carry_their_idle_interval_in_the_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "embedding_worker_enabled", True)
    monkeypatch.setattr(settings, "entity_extract_enabled", True)
    monkeypatch.setattr(settings, "code_embed_recovery_enabled", True)
    by_name = {s.name: s for s in enabled_workers()}
    assert (
        by_name["embedding_worker"].idle_interval_seconds
        == settings.embedding_worker_idle_interval_seconds
    )
    assert (
        by_name["entity_extractor"].idle_interval_seconds
        == settings.entity_extract_idle_interval_seconds
    )
    assert (
        by_name["code_embed_recovery"].idle_interval_seconds
        == settings.code_embed_recovery_idle_interval_seconds
    )
    # the idle sleep is longer than the busy one on every spec that has it
    for name in ("embedding_worker", "entity_extractor", "code_embed_recovery"):
        assert by_name[name].idle_interval_seconds > by_name[name].interval_seconds
    # a worker without an idle interval keeps the interval
    assert by_name.get("audit_verifier", WorkerSpec("x", 1, None)).idle_interval_seconds is None  # type: ignore[arg-type]


# ------------------------------------------------------- code_embed_recovery


class _FakeCodeRepo:
    """Stub rows per table, keyed like the tables; records every statement."""

    def __init__(self, stubs: dict[str, list[dict[str, Any]]]) -> None:
        self.stubs = stubs  # table -> rows with org, namespace, key cols, version, embedding_dim
        self.calls: list[tuple[str, Any]] = []
        self.inserted: list[tuple[str, list[dict[str, Any]]]] = []

    async def pending_embed_keys(self, table, repos, model, max_attempts, limit, *, min_version=0):
        self.calls.append(("keys", table, min_version, limit))
        rows = [
            r
            for r in self.stubs.get(table, [])
            if (r["org"], r["namespace"]) in repos and r["version"] > min_version
        ]
        rows.sort(key=lambda r: r["version"])
        return [dict(r) for r in rows[:limit]]

    async def live_stub_keys(self, table, model, keys, max_attempts):
        self.calls.append(("live", table, len(keys)))
        key_cols = ("path", "chunk_id") if table == "code_chunks" else ("scope_type", "scope_path")
        wanted = {tuple(str(k[c]) for c in ("org", "namespace", *key_cols)) for k in keys}
        newest: dict[tuple[str, ...], dict[str, Any]] = {}
        for r in self.stubs.get(table, []) + self.embedded.get(table, []):
            k = tuple(str(r[c]) for c in ("org", "namespace", *key_cols))
            if k in wanted and (k not in newest or r["version"] > newest[k]["version"]):
                newest[k] = r
        return [dict(r) for r in newest.values() if r["embedding_dim"] == 0]

    async def pending_embed_rows_by_keys(self, table, model, keys, max_attempts):
        self.calls.append(("rows", table, len(keys)))
        key_cols = ("path", "chunk_id") if table == "code_chunks" else ("scope_type", "scope_path")
        wanted = {tuple(str(k[c]) for c in ("org", "namespace", *key_cols)) for k in keys}
        # FINAL: only rows whose NEWEST version is still a stub
        newest: dict[tuple[str, ...], dict[str, Any]] = {}
        for r in self.stubs.get(table, []) + self.embedded.get(table, []):
            k = tuple(str(r[c]) for c in ("org", "namespace", *key_cols))
            if k in wanted and (k not in newest or r["version"] > newest[k]["version"]):
                newest[k] = r
        return [dict(r) for r in newest.values() if r["embedding_dim"] == 0]

    embedded: dict[str, list[dict[str, Any]]] = {}

    async def insert_rows(self, table, rows):
        self.calls.append(("insert", table, len(rows)))
        self.inserted.append((table, list(rows)))

    @staticmethod
    def tombstone_of(table, live_row):
        return _RealCodeRepo.tombstone_of(table, live_row)


class _Embedder:
    model_name = "fake-ngram-8"
    dim = 8

    async def embed_many(self, texts):
        return [[0.5] * 8 for _ in texts]


def _chunk_stub(ns: str, i: int, version: int) -> dict[str, Any]:
    return {
        "org": "o",
        "namespace": ns,
        "embedding_model": "fake-ngram-8",
        "path": f"src/{i}.py",
        "chunk_id": f"00000000-0000-0000-0000-{i:012d}",
        "content": "x",
        "context_text": "ctx",
        "embedding_dim": 0,
        "embed_attempts": 0,
        "version": version,
        "created_at": None,
    }


def _summary_stub(ns: str, i: int, version: int) -> dict[str, Any]:
    return {
        "org": "o",
        "namespace": ns,
        "embedding_model": "fake-ngram-8",
        "scope_type": "file",
        "scope_path": f"src/{i}.py",
        "summary": "s",
        "embedding_dim": 0,
        "embed_attempts": 0,
        "version": version,
        "created_at": None,
    }


@pytest.fixture
def cer(monkeypatch: pytest.MonkeyPatch):
    from server.code.domain.index import repos as repos_mod
    from server.code.workers import embed_recovery as w

    class _Cfg:
        def __init__(self, ns: str) -> None:
            self.org, self.namespace = "o", ns

    monkeypatch.setattr(repos_mod, "configured_repos", lambda: [_Cfg("n1"), _Cfg("n2"), _Cfg("n3")])
    from server.embedding import embedder as emb_mod

    monkeypatch.setattr(emb_mod, "get_embedder", lambda: _Embedder())
    monkeypatch.setattr(settings, "code_embed_recovery_full_scan_seconds", 1800)
    monkeypatch.setattr(w, "_watermark", {})
    monkeypatch.setattr(w, "_full_scan_at", 0.0)
    return w


async def test_an_idle_pass_is_one_scan_per_table_over_every_repo_and_marks_idle(
    cer, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.code.store import index as cr

    repo = _FakeCodeRepo({})
    monkeypatch.setattr(cr, "CodeRepo", lambda: repo)
    pass_context.begin("code_embed_recovery")
    try:
        result = await cer.run_pass()
        assert pass_context.current().idle is True
    finally:
        pass_context.clear()
    assert [c[0] for c in repo.calls] == ["keys", "keys"], repo.calls
    assert {c[1] for c in repo.calls} == {"code_chunks", "code_summaries"}
    assert result.idle and result.rows_written == 0 and result.scans == 2 and result.full_scan


async def test_a_pass_that_only_met_stale_stub_versions_is_not_idle(
    cer, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.code.store import index as cr

    # the scan returns keys (old stub versions) but FINAL says every row is already embedded
    stale = _chunk_stub("n1", 1, 50)
    repo = _FakeCodeRepo({"code_chunks": [stale]})
    repo.embedded = {"code_chunks": [{**stale, "embedding_dim": 8, "version": 60}]}
    monkeypatch.setattr(cr, "CodeRepo", lambda: repo)
    pass_context.begin("code_embed_recovery")
    try:
        r = await cer.run_pass()
        assert pass_context.current().idle is False
    finally:
        pass_context.clear()
    assert r.keys_seen == 1 and r.rows_written == 0 and not r.idle
    assert [c[0] for c in repo.calls if c[1] == "code_chunks"] == [
        "keys",
        "live",
    ]  # stale page: no FINAL read, no insert
    assert cer._watermark["code_chunks"] == 50


async def test_the_watermark_is_the_newest_version_returned_and_the_next_scan_starts_above_it(
    cer, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.code.store import index as cr

    stubs = {
        "code_chunks": [
            _chunk_stub("n1", 1, 100),
            _chunk_stub("n2", 2, 300),
            _chunk_stub("n3", 3, 200),
        ]
    }
    repo = _FakeCodeRepo(stubs)
    monkeypatch.setattr(cr, "CodeRepo", lambda: repo)
    r1 = await cer.run_pass()
    assert r1.chunks_embedded == 3 and r1.rows_written == 3 and not r1.idle
    assert cer._watermark["code_chunks"] == 300
    # one scan, one keyed read, one insert for the three repos' rows — not per repo
    assert [c[0] for c in repo.calls if c[1] == "code_chunks"] == ["keys", "live", "rows", "insert"]
    ((table, rows),) = repo.inserted
    assert table == "code_chunks" and {r["namespace"] for r in rows} == {"n1", "n2", "n3"}
    assert all(r["embedding_dim"] == 8 and r["tombstoned_at"] is None for r in rows)
    # the stale stub versions stay in the table (RMT) — the second pass's scan
    # asks above the watermark and does not read them again
    repo.calls.clear()
    r2 = await cer.run_pass()
    assert r2.idle and r2.scans == 2
    scans = [c for c in repo.calls if c[0] == "keys" and c[1] == "code_chunks"]
    assert scans == [("keys", "code_chunks", 300, cer._batch() * 3)]  # 100 keys per configured repo
    assert not r2.full_scan


async def test_the_slow_clock_owes_a_scan_from_zero(cer, monkeypatch: pytest.MonkeyPatch) -> None:
    assert cer._full_scan_due(now=1000.0) is True  # first pass after boot
    assert cer._full_scan_due(now=1500.0) is False
    assert cer._full_scan_due(now=2800.0) is True  # 1800 s later
    monkeypatch.setattr(settings, "code_embed_recovery_full_scan_seconds", 0)
    assert cer._full_scan_due(now=2801.0) and cer._full_scan_due(
        now=2802.0
    )  # clock off: every pass from 0


async def test_one_scan_a_pass_takes_a_page_per_repo_reads_by_key_in_pages_and_embeds_in_batches(
    cer, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.code.store import index as cr

    monkeypatch.setattr(cer.settings, "code_embed_recovery_batch", 2)  # The knob is a setting
    monkeypatch.setattr(cer, "_KEYS_PER_READ", 4)
    stubs = {
        "code_summaries": [_summary_stub("n1", i, 10 + i) for i in range(10)]
    }  # 10 pending, 3 repos → 6 a pass
    repo = _FakeCodeRepo(stubs)
    monkeypatch.setattr(cr, "CodeRepo", lambda: repo)
    embed_calls: list[int] = []
    real = _Embedder()

    class _Counting:
        model_name, dim = real.model_name, real.dim

        async def embed_many(self, texts):
            embed_calls.append(len(texts))
            return await real.embed_many(texts)

    from server.embedding import embedder as emb_mod

    monkeypatch.setattr(emb_mod, "get_embedder", lambda: _Counting())
    r = await cer.run_pass()
    assert r.summaries_embedded == 6 and r.rows_written == 6 and r.keys_seen == 6 and r.scans == 2
    calls = [c for c in repo.calls if c[1] == "code_summaries"]
    assert calls[0] == ("keys", "code_summaries", 0, 6)  # one scan: BATCH × repos keys
    assert [c for c in calls if c[0] == "rows"] == [
        ("rows", "code_summaries", 4),
        ("rows", "code_summaries", 2),
    ]  # paged
    assert [c for c in calls if c[0] == "insert"] == [("insert", "code_summaries", 6)]  # one insert
    assert embed_calls == [2, 2, 2]  # BATCH texts an embed call (one namespace here)
    assert cer._watermark["code_summaries"] == 15  # the newest of the six returned
    # the next pass starts above it and finds the other four
    repo.calls.clear()
    r2 = await cer.run_pass()
    assert r2.summaries_embedded == 4 and cer._watermark["code_summaries"] == 19
    assert [c for c in repo.calls if c[0] == "keys" and c[1] == "code_summaries"][0][2] == 15


async def test_a_failed_embed_bumps_attempts_and_poisons_at_the_cap(
    cer, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.code.store import index as cr

    class _Down:
        model_name = "fake-ngram-8"
        dim = 8

        async def embed_many(self, texts):
            raise RuntimeError("provider down")

    async def _shrink(embedder, text):
        raise RuntimeError("provider down")

    from server.code.app.index import embed_text
    from server.embedding import embedder as emb_mod

    monkeypatch.setattr(emb_mod, "get_embedder", lambda: _Down())
    monkeypatch.setattr(embed_text, "embed_shrinking", _shrink)
    monkeypatch.setattr(settings, "code_embed_max_attempts", 2)
    stub = _chunk_stub("n1", 1, 5)
    stub["embed_attempts"] = 1
    repo = _FakeCodeRepo({"code_chunks": [stub]})
    monkeypatch.setattr(cr, "CodeRepo", lambda: repo)
    r = await cer.run_pass()
    assert r.failed == 1 and r.poisoned == 1 and r.rows_written == 1 and not r.idle
    ((_, rows),) = repo.inserted
    assert rows[0]["embed_attempts"] == 2 and rows[0]["embed_failed_at"] is not None
    assert rows[0]["embedding_dim"] == 0 and "provider down" in rows[0]["embed_last_error"]


def test_the_pending_scan_is_one_statement_over_the_repos_without_prewhere_or_final() -> None:
    from server.code.store.index import CodeRepo

    class _CH:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        async def query_all(self, sql, params=None):
            self.calls.append((" ".join(sql.split()), params or {}))
            return []

    ch = _CH()
    repo = CodeRepo.__new__(CodeRepo)
    repo._ch = ch  # type: ignore[attr-defined]
    asyncio.run(
        repo.pending_embed_keys(
            "code_chunks", [("o", "n1"), ("o", "n2")], "m", 8, 100, min_version=42
        )
    )
    asyncio.run(repo.pending_embed_keys("code_summaries", [("o", "n1")], "m", 8, 100))
    asyncio.run(
        repo.pending_embed_rows_by_keys(
            "code_chunks",
            "m",
            [{"org": "o", "namespace": "n1", "path": "p", "chunk_id": "c"}],
            8,
        )
    )
    scan, sscan, rows = ch.calls
    assert "PREWHERE" not in scan[0] and "FINAL" not in scan[0]
    assert "(org, namespace) IN {repos:Array(Tuple(String, String))}" in scan[0]
    assert "version > {min_version:UInt64}" in scan[0] and "ORDER BY version" in scan[0]
    assert "GROUP BY" not in scan[0]  # the candidate scan is the cheap flat one
    assert scan[1]["repos"] == [("o", "n1"), ("o", "n2")] and scan[1]["min_version"] == 42
    assert (
        "SELECT org, namespace, scope_type, scope_path, version FROM loom.code_summaries"
        in sscan[0]
    )
    assert "SELECT * EXCEPT (embedding) FROM loom.code_chunks FINAL" in rows[0]
    assert (
        "(org, namespace, embedding_model, path, chunk_id) IN" in rows[0]
        and "PREWHERE" not in rows[0]
    )
    assert rows[1]["keys"] == [("o", "n1", "m", "p", "c")]


# ---------------------------------------------------------- entity_extractor


@pytest.fixture
def ee_env(monkeypatch: pytest.MonkeyPatch):
    from server.memory import store as memory_repo
    from server.memory.workers import entity_extractor as ee

    rows = [
        {
            "org": "o",
            "namespace": f"ns{i % 2}",
            "agent": "a",
            "user_id": "-",
            "session_id": "-",
            "memory_key": f"k{i}",
            "memory_type": "semantic",
            "value": f"Alice {i}",
            "version": 1,
            "content_sha256": "",
        }
        for i in range(6)
    ]
    env: dict[str, Any] = {"rows": rows, "cap_calls": [], "batches": [], "over": set()}

    async def _find(**kw):
        return list(env["rows"])

    async def _caps(pairs):
        env["cap_calls"].append(sorted(pairs))
        return {p: (p in env["over"]) for p in pairs}

    async def _batch(items, **kw):
        env["batches"].append([dict(i) for i in items])
        return {"memory": len(items), "memory_embeddings": len(items)}

    async def _extract(v):
        return ["alice"]

    monkeypatch.setattr(memory_repo, "find_unentitied_memories", _find)
    monkeypatch.setattr(memory_repo, "apply_entities_batch", _batch)
    monkeypatch.setattr(ee, "namespaces_over_cap", _caps)
    monkeypatch.setattr(ee, "_extract", _extract)
    monkeypatch.setattr(ee, "emit_worker_cost_event_for", lambda **kw: None)
    monkeypatch.setattr(ee, "_heal_due", lambda now=None: False)
    monkeypatch.setattr(settings, "entity_extract_full_scan_seconds", 0)
    ee._parse_fail_streaks.clear()
    return env


async def test_the_cap_is_read_once_per_pass_for_every_namespace_and_the_rows_are_written_in_one_batch(
    ee_env,
) -> None:
    from server.memory.workers import entity_extractor as ee

    r = await ee.run_pass(ee.EntityExtractorConfig(batch_size=25))
    assert ee_env["cap_calls"] == [[("o", "ns0"), ("o", "ns1")]]
    assert len(ee_env["batches"]) == 1 and len(ee_env["batches"][0]) == 6
    assert ee_env["batches"][0][0]["entities"] == ["alice"]
    assert len(r.extracted) == 6 and r.rows_written == {"memory": 6, "memory_embeddings": 6}


async def test_rows_of_a_namespace_over_its_cap_are_skipped_without_a_call_per_row(ee_env) -> None:
    from server.memory.workers import entity_extractor as ee

    ee_env["over"] = {("o", "ns1")}
    r = await ee.run_pass(ee.EntityExtractorConfig(batch_size=25))
    assert r.skipped_over_budget == 3 and len(r.extracted) == 3
    assert {i["namespace"] for i in ee_env["batches"][0]} == {"ns0"}
    assert len(ee_env["cap_calls"]) == 1


async def test_an_empty_pass_reads_no_cap_writes_nothing_and_marks_idle(ee_env) -> None:
    from server.memory.workers import entity_extractor as ee

    ee_env["rows"] = []
    pass_context.begin("entity_extractor")
    try:
        r = await ee.run_pass(ee.EntityExtractorConfig())
        assert pass_context.current().idle is True
    finally:
        pass_context.clear()
    assert ee_env["cap_calls"] == [] and ee_env["batches"] == [] and r.rows_written == {}


async def test_a_busy_pass_does_not_mark_idle(ee_env) -> None:
    from server.memory.workers import entity_extractor as ee

    pass_context.begin("entity_extractor")
    try:
        await ee.run_pass(ee.EntityExtractorConfig())
        assert pass_context.current().idle is False
    finally:
        pass_context.clear()


# ------------------------------------------------ memory_repo.apply_entities_batch


class _CH:
    """query_all answers from a queue; inserts captured."""

    def __init__(self, answers: list[list[dict[str, Any]]]) -> None:
        self.answers = list(answers)
        self.queries: list[str] = []
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []

    async def query_all(self, sql, params=None):
        self.queries.append(" ".join(sql.split()))
        return self.answers.pop(0) if self.answers else []

    async def insert(self, table, rows, settings=None):
        self.inserts.append((table, list(rows)))


def _mem_row(key: str) -> dict[str, Any]:
    from datetime import UTC, datetime

    now = datetime.now(UTC).replace(tzinfo=None)
    return {
        "memory_id": "11111111-2222-3333-4444-555555555555",
        "org": "o",
        "namespace": "n",
        "agent": "a",
        "user_id": "-",
        "session_id": "-",
        "memory_key": key,
        "memory_type": "semantic",
        "value": '{"a": 1}',
        "value_json": "{}",
        "confidence": 1.0,
        "version": 1,
        "pinned": 0,
        "created_at": now,
        "updated_at": now,
        "expires_at": None,
        "tombstoned_at": None,
        "entities": [],
        "tags": [],
    }


def _emb_row(key: str, scope: str = "private") -> dict[str, Any]:
    from server.memory import store as mr

    return {
        **mr.EMBEDDING_PERSISTED_COLS,
        "org": "o",
        "namespace": "n",
        "agent": "a",
        "user_id": "-",
        "session_id": "-",
        "memory_key": key,
        "memory_type": "semantic",
        "scope": scope,
        "content_excerpt": "x",
        "embedding_model": "m",
        "embedding_dim": 3,
        "embedding": [0.1, 0.2, 0.3],
        "entities": ["old"],
        "version": 1,
    }


def _item(
    key: str, entities: list[str], version: int = 1, content_sha256: str = ""
) -> dict[str, Any]:
    return {
        "org": "o",
        "namespace": "n",
        "agent": "a",
        "user_id": "-",
        "session_id": "-",
        "memory_key": key,
        "version": version,
        "content_sha256": content_sha256,
        "entities": entities,
    }


def test_apply_entities_batch_is_five_statements_for_n_rows_and_emits_the_same_rows_as_n_single_writes() -> (
    None
):
    from server.memory import store as mr

    ch = _CH(
        answers=[
            [_mem_row("k1"), _mem_row("k2")],  # memory rows by exact key (FINAL)
            [
                {
                    "org": "o",
                    "namespace": "n",
                    "agent": "a",
                    "user_id": "-",
                    "session_id": "-",
                    "memory_key": "k1",
                    "scope": "private",
                    "embedding_model": "m",
                },
                {
                    "org": "o",
                    "namespace": "n",
                    "agent": "a",
                    "user_id": "-",
                    "session_id": "-",
                    "memory_key": "k2",
                    "scope": "private",
                    "embedding_model": "m",
                },
                {
                    "org": "o",
                    "namespace": "n",
                    "agent": "a",
                    "user_id": "-",
                    "session_id": "-",
                    "memory_key": "k2",
                    "scope": "shared",
                    "embedding_model": "m",
                },
            ],  # sibling discovery
            [_emb_row("k1"), _emb_row("k2"), _emb_row("k2", "shared")],  # keyed FINAL read
        ]
    )
    written = asyncio.run(
        mr.apply_entities_batch([_item("k1", ["alice"]), _item("k2", ["bob", "eve"])], ch=ch)
    )
    assert written == {"memory": 2, "memory_embeddings": 3}
    assert len(ch.queries) == 3 and [t for t, _ in ch.inserts] == [
        "loom.memory",
        "loom.memory_embeddings",
    ]
    assert (
        ch.queries[0].startswith("SELECT")
        and "FROM loom.memory FINAL PREWHERE (org, namespace, agent, user_id, session_id, memory_key) IN"
        in ch.queries[0]
    )
    assert ch.queries[1].startswith(
        "SELECT DISTINCT org, namespace, agent, user_id, session_id, memory_key, scope, embedding_model FROM loom.memory_embeddings"
    )
    assert ch.queries[2].startswith(
        "SELECT * FROM loom.memory_embeddings FINAL PREWHERE (org, namespace, agent, user_id, session_id, scope, embedding_model, memory_key) IN"
    )
    mem = {r["memory_key"]: r for r in ch.inserts[0][1]}
    assert mem["k1"]["entities"] == ["alice"] and mem["k2"]["entities"] == ["bob", "eve"]
    emb = ch.inserts[1][1]
    assert [(r["memory_key"], r["scope"], r["entities"]) for r in emb] == [
        ("k1", "private", ["alice"]),
        ("k2", "private", ["bob", "eve"]),
        ("k2", "shared", ["bob", "eve"]),
    ]
    versions = {r["version"] for r in ch.inserts[0][1]}
    assert len(versions) == 1 and versions.pop() > 1, "one bumped version for the memory batch"
    # The mirror copy sits one above the row it read, so a later write still wins.
    assert {r["version"] for r in emb} == {2}
    assert all(set(mr.EMBEDDING_PERSISTED_COLS) <= set(r) for r in emb), (
        "every persisted column mirrored"
    )
    assert all(r["embedding"] == [0.1, 0.2, 0.3] and r["content_excerpt"] == "x" for r in emb)


def test_apply_entities_batch_writes_only_onto_the_head_the_entities_came_from() -> None:
    """A value revised or redacted since the extractor read it keeps its own entities."""
    from datetime import UTC, datetime

    from server.memory import store as mr

    redacted = _mem_row("k3") | {"redacted_at": datetime.now(UTC).replace(tzinfo=None)}
    ch = _CH(answers=[[_mem_row("k1"), _mem_row("k2") | {"version": 2}, redacted]])
    items = [_item("k1", ["alice"]), _item("k2", ["bob"]), _item("k3", ["eve"])]
    written = asyncio.run(mr.apply_entities_batch(items, ch=ch))
    assert written["memory"] == 1
    assert [(r["memory_key"], r["entities"]) for r in ch.inserts[0][1]] == [("k1", ["alice"])]


def _sibling(key: str) -> dict[str, Any]:
    return {
        "org": "o",
        "namespace": "n",
        "agent": "a",
        "user_id": "-",
        "session_id": "-",
        "memory_key": key,
        "scope": "private",
        "embedding_model": "m",
    }


def test_apply_entities_batch_keeps_its_work_over_a_copy_of_the_same_text() -> None:
    """A head copied since the read (a lock, a touch) still holds the text extracted and
    takes the entities; one whose text changed does not."""
    from server.memory import store as mr

    read = "a" * 64
    ch = _CH(
        answers=[
            [
                _mem_row("k1") | {"version": 3, "content_sha256": read},
                _mem_row("k2") | {"version": 3, "content_sha256": "b" * 64},
            ]
        ]
    )
    items = [_item("k1", ["alice"], content_sha256=read), _item("k2", ["bob"], content_sha256=read)]
    written = asyncio.run(mr.apply_entities_batch(items, ch=ch))
    assert written["memory"] == 1
    assert [(r["memory_key"], r["entities"]) for r in ch.inserts[0][1]] == [("k1", ["alice"])]


@pytest.mark.parametrize(
    "since",
    [{"redacted_at": datetime(2026, 1, 1)}, {"sensitivity": "high"}],
    ids=["redacted", "high"],
)
def test_a_copy_of_the_same_text_redacted_or_made_high_since_keeps_no_entities(
    since: dict[str, Any],
) -> None:
    from server.memory import store as mr

    read = "a" * 64
    ch = _CH(answers=[[_mem_row("k1") | {"version": 3, "content_sha256": read} | since]])
    written = asyncio.run(
        mr.apply_entities_batch([_item("k1", ["alice"], content_sha256=read)], ch=ch)
    )
    assert written["memory"] == 0 and ch.inserts == []


def test_apply_entities_batch_copies_every_search_document_of_a_head_written() -> None:
    """A document above the head's version (an embedding worker's fill, a conflict stamp)
    takes the entities as the head's own does."""
    from server.memory import store as mr

    ch = _CH(
        answers=[
            [_mem_row("k1")],
            [_sibling("k1"), _sibling("k1") | {"scope": "shared"}],
            [_emb_row("k1"), _emb_row("k1", "shared") | {"version": 2}],
        ]
    )
    written = asyncio.run(mr.apply_entities_batch([_item("k1", ["alice"])], ch=ch))
    assert written == {"memory": 1, "memory_embeddings": 2}
    assert [(r["scope"], r["version"]) for r in ch.inserts[1][1]] == [("private", 2), ("shared", 3)]


def test_apply_entities_batch_leaves_a_newer_writes_search_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A document showing other content than the head written (a newer write's) keeps its
    own entities, without a retry for it."""
    from server.memory import store as mr
    from server.memory.store import entities

    slept: list[float] = []

    async def sleep(s: float) -> None:
        slept.append(s)

    monkeypatch.setattr(entities.asyncio, "sleep", sleep)
    head, newer = "a" * 64, "b" * 64
    ch = _CH(
        answers=[
            [_mem_row("k1") | {"content_sha256": head}],
            [_sibling("k1"), _sibling("k1") | {"scope": "shared"}],
            [
                _emb_row("k1") | {"content_sha256": head},
                _emb_row("k1", "shared") | {"version": 3, "content_sha256": newer},
            ],
        ]
    )
    written = asyncio.run(mr.apply_entities_batch([_item("k1", ["alice"])], ch=ch))
    assert written == {"memory": 1, "memory_embeddings": 1}
    assert [r["scope"] for r in ch.inserts[1][1]] == ["private"]
    assert slept == []


def test_a_failed_mirror_copy_fails_the_pass_on_clickhouse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The memory rows are in; the search documents' copy failing is the pass's failure,
    for the worker's status, and a later pass's heal closes the mirror."""
    from server.memory import store as mr
    from server.memory.store import entities

    async def refused(*_: Any, **__: Any) -> None:
        raise RuntimeError("memory_embeddings is read-only")

    monkeypatch.setattr(entities, "insert_embedding_rows", refused)
    ch = _CH(answers=[[_mem_row("k1")], [_sibling("k1")], [_emb_row("k1")]])
    with pytest.raises(RuntimeError, match="read-only"):
        asyncio.run(mr.apply_entities_batch([_item("k1", ["alice"])], ch=ch))
    assert [t for t, _ in ch.inserts] == ["loom.memory"]


def test_a_restatement_with_its_own_entities_keeps_them() -> None:
    """The same text written again with the caller's entities since the read is not the
    pass's to tag."""
    from server.memory import store as mr

    read = "a" * 64
    restated = _mem_row("k1") | {"version": 3, "content_sha256": read, "entities": ["acme"]}
    ch = _CH(answers=[[restated]])
    written = asyncio.run(
        mr.apply_entities_batch([_item("k1", ["alice"], content_sha256=read)], ch=ch)
    )
    assert written["memory"] == 0 and ch.inserts == []


def test_apply_entities_batch_writes_above_a_copy_newer_than_its_draw() -> None:
    """A copy of the same text written after the version was drawn (a lock on a replica
    whose clock is ahead) is the head the entities land over, so they go above it."""
    from server.memory import store as mr
    from server.memory.store.rows import next_version

    read = "a" * 64
    far = next_version() + 10**9  # a lock from a replica whose clock is a second ahead
    ch = _CH(answers=[[_mem_row("k1") | {"version": far, "content_sha256": read}]])
    asyncio.run(mr.apply_entities_batch([_item("k1", ["alice"], content_sha256=read)], ch=ch))
    assert [r["version"] for r in ch.inserts[0][1]] == [far + 2]


def test_apply_entities_batch_draws_its_version_before_reading_the_heads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On ClickHouse the copy is a plain insert: drawn before the read, a write drawn after
    it outranks the copy. One drawn earlier that lands after the read is overwritten;
    Postgres refuses it."""
    from server.memory import store as mr
    from server.memory.store import entities

    ch = _CH(answers=[[_mem_row("k1")]])
    reads_before_draw: list[int] = []
    real = entities.next_version

    def next_version() -> int:
        reads_before_draw.append(len(ch.queries))
        return real()

    monkeypatch.setattr(entities, "next_version", next_version)
    monkeypatch.setattr(entities, "ENTITY_MIRROR_RETRY_DELAY_S", 0)
    asyncio.run(mr.apply_entities_batch([_item("k1", ["alice"])], ch=ch))
    assert reads_before_draw == [0]


def test_apply_entities_batch_reads_again_without_a_key_erased_since_its_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An erasure between the read and the commit refuses the commit; the next read no
    longer holds the erased key, and the rest land."""
    from server.memory import store as mr
    from server.memory.store import entities, pg_memory

    reads = iter([[_mem_row("k1"), _mem_row("k2")], [_mem_row("k1")]])

    async def heads(keys: Any, *, ch: Any, sequential: bool) -> dict[Any, Any]:
        return {mr.identity_key(r): mr.MemoryRow.from_snapshot(r) for r in next(reads)}

    committed: list[list[str]] = []

    async def commit(rows: list[dict[str, Any]], read: Any, *, ch: Any, then: Any) -> None:
        committed.append([r["memory_key"] for r in rows])
        if len(committed) == 1:
            raise pg_memory.MemoryErased()

    monkeypatch.setattr(entities, "memory_rows_by_identity_keys", heads)
    monkeypatch.setattr(entities, "insert_versions_over_heads", commit)
    monkeypatch.setattr(entities, "ENTITY_MIRROR_RETRY_DELAY_S", 0)
    written = asyncio.run(
        mr.apply_entities_batch([_item("k1", ["alice"]), _item("k2", ["bob"])], ch=_CH([]))
    )
    assert committed == [["k1", "k2"], ["k1"]]
    assert written["memory"] == 1


def test_apply_entities_batch_answers_contended_when_every_round_moves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory import store as mr
    from server.memory.store import entities, pg_memory

    async def heads(keys: Any, *, ch: Any, sequential: bool) -> dict[Any, Any]:
        return {mr.identity_key(r): mr.MemoryRow.from_snapshot(r) for r in [_mem_row("k1")]}

    commits: list[int] = []

    async def commit(rows: list[dict[str, Any]], read: Any, *, ch: Any, then: Any) -> None:
        commits.append(len(rows))
        raise pg_memory.AdmissionContendedError("moved")

    monkeypatch.setattr(entities, "memory_rows_by_identity_keys", heads)
    monkeypatch.setattr(entities, "insert_versions_over_heads", commit)
    with pytest.raises(pg_memory.AdmissionContendedError):
        asyncio.run(mr.apply_entities_batch([_item("k1", ["alice"])], ch=_CH([])))
    assert len(commits) == settings.pg_admission_rounds


def test_a_redacted_head_is_no_entity_candidate() -> None:
    from server.memory import store as mr

    ch = _CH(answers=[[{**_item("k1", []), "memory_type": "semantic", "value": "x"}]])
    asyncio.run(mr.find_unentitied_memories(batch_size=5, ch=ch))
    keys, rows = ch.queries
    assert "argMax(m.redacted_at IS NULL, version) = 1" in keys
    assert "AND redacted_at IS NULL" in rows


def test_apply_entities_batch_reads_again_without_the_heads_that_moved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under Postgres one head written between the read and the commit refuses the whole
    commit; the batch reads again and lands the keys that did not move."""
    from server.memory import store as mr
    from server.memory.store import entities, pg_memory

    reads = iter(
        [
            [_mem_row("k1"), _mem_row("k2")],
            [_mem_row("k1"), _mem_row("k2") | {"version": 2}],
        ]
    )

    async def heads(keys: Any, *, ch: Any, sequential: bool) -> dict[Any, Any]:
        assert sequential, "a replica that has not seen a write would copy over it"
        return {mr.identity_key(r): mr.MemoryRow.from_snapshot(r) for r in next(reads)}

    committed: list[list[str]] = []

    async def commit(rows: list[dict[str, Any]], read: Any, *, ch: Any, then: Any) -> None:
        committed.append([r["memory_key"] for r in rows])
        if len(committed) == 1:
            raise pg_memory.AdmissionContendedError("moved")

    monkeypatch.setattr(entities, "memory_rows_by_identity_keys", heads)
    monkeypatch.setattr(entities, "insert_versions_over_heads", commit)
    monkeypatch.setattr(entities, "ENTITY_MIRROR_RETRY_DELAY_S", 0)
    written = asyncio.run(
        mr.apply_entities_batch([_item("k1", ["alice"]), _item("k2", ["bob"])], ch=_CH([]))
    )
    assert committed == [["k1", "k2"], ["k1"]]
    assert written["memory"] == 1


def test_apply_entities_batch_retries_the_mirror_once_for_the_keys_whose_memory_exists_but_mirror_is_not_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory import store as mr
    from server.memory.store import entities

    slept: list[float] = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(entities.asyncio, "sleep", fake_sleep)
    sib1 = {
        "org": "o",
        "namespace": "n",
        "agent": "a",
        "user_id": "-",
        "session_id": "-",
        "memory_key": "k1",
        "scope": "private",
        "embedding_model": "m",
    }
    sib2 = {**sib1, "memory_key": "k2"}
    ch = _CH(
        answers=[
            [_mem_row("k1"), _mem_row("k2")],
            [sib1],
            [_emb_row("k1")],  # first look: only k1's mirror is visible
            [sib2],
            [_emb_row("k2")],  # the retry, for k2 alone
        ]
    )
    written = asyncio.run(mr.apply_entities_batch([_item("k1", ["a"]), _item("k2", ["b"])], ch=ch))
    assert slept == [mr.ENTITY_MIRROR_RETRY_DELAY_S], "exactly one bounded retry for the batch"
    assert written == {"memory": 2, "memory_embeddings": 2}
    assert len(ch.queries) == 5 and len(ch.inserts) == 2
    assert [r["memory_key"] for r in ch.inserts[1][1]] == ["k1", "k2"]


def test_apply_entities_batch_with_no_live_memory_rows_writes_nothing_and_does_not_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory import store as mr
    from server.memory.store import entities

    slept: list[float] = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(entities.asyncio, "sleep", fake_sleep)
    ch = _CH(answers=[[], []])
    written = asyncio.run(mr.apply_entities_batch([_item("k1", ["a"])], ch=ch))
    assert written == {"memory": 0, "memory_embeddings": 0} and ch.inserts == [] and slept == []
    assert asyncio.run(mr.apply_entities_batch([], ch=ch)) == {"memory": 0, "memory_embeddings": 0}


def test_heal_reads_every_pair_in_one_keyed_final_read_and_one_insert() -> None:
    from server.memory import store as mr

    pairs = [
        {
            "org": "o",
            "namespace": "n",
            "agent": "a",
            "user_id": "-",
            "session_id": "-",
            "memory_key": "k1",
            "entities": ["x"],
        },
        {
            "org": "o",
            "namespace": "n",
            "agent": "a",
            "user_id": "-",
            "session_id": "-",
            "memory_key": "k2",
            "entities": ["y"],
        },
    ]
    ch = _CH(answers=[pairs, [_emb_row("k1"), _emb_row("k2"), _emb_row("k2", "shared")]])
    healed = asyncio.run(mr.heal_divergent_entities(limit=50, ch=ch))
    assert healed == 3 and len(ch.queries) == 2 and len(ch.inserts) == 1
    assert "IN {keys:Array(Tuple(String, String, String, String, String, String))}" in ch.queries[1]
    assert [(r["memory_key"], r["entities"]) for r in ch.inserts[0][1]] == [
        ("k1", ["x"]),
        ("k2", ["y"]),
        ("k2", ["y"]),
    ]


# ---------------------------------------------------------------- budget


def test_namespaces_over_cap_is_one_statement_with_the_default_cap_fallback_and_absent_pairs_under_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.observability import budget

    monkeypatch.setattr(settings, "default_daily_cost_cap_usd", 10.0)
    ch = _CH(
        answers=[
            [
                {"org": "o", "namespace": "a", "spent": 12.0, "cap": 0.0},  # default cap 10 → over
                {"org": "o", "namespace": "b", "spent": 12.0, "cap": 50.0},  # own cap 50 → under
                {"org": "o", "namespace": "c", "spent": 5.0, "cap": 0.0},  # default cap → under
            ]
        ]
    )
    out = asyncio.run(
        budget.namespaces_over_cap([("o", "a"), ("o", "b"), ("o", "c"), ("o", "d")], ch=ch)
    )
    assert out == {("o", "a"): True, ("o", "b"): False, ("o", "c"): False, ("o", "d"): False}
    assert len(ch.queries) == 1
    assert (
        "sum(upstream_cost_usd) AS spent" in ch.queries[0]
        and "FROM loom.namespaces FINAL" in ch.queries[0]
    )
    assert "(org, namespace) IN {pairs:Array(Tuple(String, String))}" in ch.queries[0]
    assert "ts > now() - INTERVAL 1 DAY" in ch.queries[0]


def test_namespaces_over_cap_is_uncapped_when_both_tiers_are_zero_and_fail_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.observability import budget

    monkeypatch.setattr(settings, "default_daily_cost_cap_usd", 0.0)
    ch = _CH(answers=[[{"org": "o", "namespace": "a", "spent": 1e9, "cap": 0.0}]])
    assert asyncio.run(budget.namespaces_over_cap([("o", "a")], ch=ch)) == {("o", "a"): False}

    class _Boom:
        async def query_all(self, sql, params=None):
            raise RuntimeError("down")

    assert asyncio.run(budget.namespaces_over_cap([("o", "a")], ch=_Boom())) == {("o", "a"): False}
    assert asyncio.run(budget.namespaces_over_cap([], ch=_Boom())) == {}


# --------------------------------------------------------- embedding_worker


@pytest.fixture
def emb_env(monkeypatch: pytest.MonkeyPatch):
    from server.memory import store as memory_repo
    from server.memory.workers import embedding as w

    env: dict[str, Any] = {"stubs": [], "orphans": [], "chunks": [], "inserted": []}

    async def find_stubs(**kw):
        return [dict(s) for s in env["stubs"]]

    async def insert_rows(rows, *a, **kw):
        env["inserted"].append(list(rows))

    async def orphans(**kw):
        return [dict(o) for o in env["orphans"]]

    async def no_refresh(rows):
        return None

    class _E:
        model_name, dim = "fake-ngram-3", 3

        async def embed_many(self, texts):
            return [[1.0, 2.0, 3.0] for _ in texts]

        async def embed(self, text):
            return [1.0, 2.0, 3.0]

    monkeypatch.setattr(memory_repo, "find_embedding_stubs", find_stubs)
    monkeypatch.setattr(memory_repo, "insert_embedding_rows", insert_rows)
    monkeypatch.setattr(memory_repo, "find_memories_without_embedding_rows", orphans)
    monkeypatch.setattr(w, "_refresh_entities", no_refresh)
    monkeypatch.setattr(w, "get_embedder", lambda: _E())
    return env


def _stub_row() -> dict[str, Any]:
    return {
        "org": "o",
        "namespace": "n",
        "agent": "a",
        "user_id": "-",
        "session_id": "-",
        "memory_key": "k",
        "memory_type": "semantic",
        "scope": "private",
        "content_excerpt": "x",
        "embedding_model": "fake-ngram-3",
        "created_at": None,
        "embed_attempts": 0,
        "run_id": "-",
        "entities": [],
        "tags": [],
        "version": 10,
    }


async def test_embedding_worker_marks_idle_only_when_both_legs_found_nothing(emb_env) -> None:
    from server.memory.workers import embedding as w

    pass_context.begin("embedding_worker")
    try:
        r = await w.run_pass(w.EmbeddingWorkerConfig(batch_size=10))
        assert pass_context.current().idle is True and r.embedded_count == 0
    finally:
        pass_context.clear()
    emb_env["stubs"] = [_stub_row()]
    pass_context.begin("embedding_worker")
    try:
        r = await w.run_pass(w.EmbeddingWorkerConfig(batch_size=10))
        assert pass_context.current().idle is False and r.embedded_count == 1
    finally:
        pass_context.clear()


# ------------------------------------------------------- outcome scalars


async def test_every_pass_records_its_counts_as_outcome_scalars(
    emb_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.memory.workers import embedding as w
    from server.workers import status

    emb_env["stubs"] = [_stub_row()]
    await w.run_pass(w.EmbeddingWorkerConfig(batch_size=10))
    outcome = status.get_registry().snapshot()["embedding_worker"]["last_outcome"]
    assert (
        outcome["rows_written"] == 1 and outcome["idle"] is False and outcome["embedded_count"] == 1
    )
    # the supervisor's pass row carries every scalar of that outcome as outcome_<key>
    args = sup._pass_args("embedding_worker", spent=False, error=None, outcome=outcome)
    assert args["outcome_rows_written"] == "1" and args["outcome_idle"] == "False"


async def test_a_pass_chains_scans_past_stale_stub_versions_until_live_work(
    cer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A heal leaves the superseded stub version of every row it
    re-chunked; the flat scan returns those first. A pass that meets a full
    page of stale versions scans again above its watermark (up to
    `_MAX_SCANS_PER_TABLE`) instead of ending with nothing embedded."""
    from server.code.store import index as cr

    monkeypatch.setattr(settings, "code_embed_recovery_batch", 2)  # 2 keys × 3 repos = 6 a page
    stale = [
        _chunk_stub("n1", i, 10 + i) for i in range(14)
    ]  # 14 stale versions: two full pages + 2
    live = [_chunk_stub("n2", 100 + i, 200 + i) for i in range(3)]
    repo = _FakeCodeRepo({"code_chunks": stale + live})
    repo.embedded = {
        "code_chunks": [{**r, "embedding_dim": 8, "version": r["version"] + 50} for r in stale]
    }
    monkeypatch.setattr(cr, "CodeRepo", lambda: repo)
    r = await cer.run_pass()
    scans = [c for c in repo.calls if c[0] == "keys" and c[1] == "code_chunks"]
    # page 1 (6 stale) → chain; page 2 (6 stale) → chain; page 3 (2 stale + 3 live, then keys < limit) → embed
    assert [c[2] for c in scans] == [0, 15, 21]
    assert r.chunks_embedded == 3 and r.rows_written == 3 and not r.idle
    assert cer._watermark["code_chunks"] == 202
    # the chain is bounded: a backlog longer than the cap leaves the rest to the next pass
    monkeypatch.setattr(cer, "_MAX_SCANS_PER_TABLE", 2)
    cer._watermark.clear()
    repo.calls.clear()
    r2 = await cer.run_pass()
    assert (
        r2.chunks_embedded == 0 and r2.scans == 3 and not r2.idle
    )  # 2 chunk scans + 1 summaries scan
    assert cer._watermark["code_chunks"] == 21


async def test_the_chain_stops_at_its_wall_time_budget_and_embeds_what_it_has(
    cer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The chain is bounded by wall time as well as by scans — a
    pass must finish inside the supervisor's deadline, and whatever the
    chain fetched is embedded, not dropped."""
    from server.code.store import index as cr

    monkeypatch.setattr(settings, "code_embed_recovery_batch", 2)  # pages of 6
    stale = [_chunk_stub("n1", i, 10 + i) for i in range(12)]  # two full stale pages
    live = [_chunk_stub("n2", 100 + i, 200 + i) for i in range(3)]
    repo = _FakeCodeRepo({"code_chunks": stale + live})
    repo.embedded = {
        "code_chunks": [{**r, "embedding_dim": 8, "version": r["version"] + 50} for r in stale]
    }
    monkeypatch.setattr(cr, "CodeRepo", lambda: repo)
    monkeypatch.setattr(
        settings, "code_embed_recovery_chain_budget_s", 0.0
    )  # budget spent after the first scan
    r = await cer.run_pass()
    scans = [c for c in repo.calls if c[0] == "keys" and c[1] == "code_chunks"]
    assert len(scans) == 1 and r.chunks_embedded == 0 and not r.idle
    assert cer._watermark["code_chunks"] == 15  # the next pass continues above the page it read
    monkeypatch.setattr(settings, "code_embed_recovery_chain_budget_s", 20.0)
    repo.calls.clear()
    r2 = await cer.run_pass()
    assert r2.chunks_embedded == 3 and cer._watermark["code_chunks"] == 202


def test_the_liveness_filter_is_one_keyed_aggregate_over_the_page() -> None:
    """`live_stub_keys` judges a candidate page by each key's
    newest version with one keyed aggregate (IN on the full sort key, flag
    columns only, no FINAL); superseded versions never reach the FINAL read."""
    from server.code.store.index import CodeRepo

    class _CH:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        async def query_all(self, sql, params=None):
            self.calls.append((" ".join(sql.split()), params or {}))
            return [{"org": "o", "namespace": "n1", "path": "p", "chunk_id": "c", "newest": 7}]

    ch = _CH()
    repo = CodeRepo.__new__(CodeRepo)
    repo._ch = ch  # type: ignore[attr-defined]
    out = asyncio.run(
        repo.live_stub_keys(
            "code_chunks",
            "m",
            [{"org": "o", "namespace": "n1", "path": "p", "chunk_id": "c", "version": 3}],
            8,
        )
    )
    ((sql, params),) = ch.calls
    assert (
        "(org, namespace, embedding_model, path, chunk_id) IN {keys:Array(Tuple(String, String, String, String, UUID))}"
        in sql
    )
    assert (
        "GROUP BY org, namespace, path, chunk_id" in sql
        and "HAVING argMax(embedding_dim, version) = 0" in sql
    )
    assert (
        "argMax(tombstoned_at IS NULL, version) = 1" in sql
        and "FINAL" not in sql
        and "PREWHERE" not in sql
    )
    assert params["keys"] == [("o", "n1", "m", "p", "c")]
    assert out == [{"org": "o", "namespace": "n1", "path": "p", "chunk_id": "c", "version": 7}]
