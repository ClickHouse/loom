"""The ingest pass stops starting work before its deadline,
and says so.

The pass is budgeted (`code_ingest_pass_budget_fraction` of the worker's
deadline, checked between commits, between rounds of the bootstrap slice,
the re-chunk and the heal, and between namespaces); its legs run in order —
every entry's commits, the bootstrap slices, the walk, the cheap
post-steps; a pass the budget cut returns a success with `partial: true`
and the resume point, its cursors advanced by what it attempted; and a
round's per-file resolution reads (live edges, the name table) are two
round-wide statements answered per file from memory, with the same rows.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from server.code.app.index import ingest, ingest_headers, reconcile
from server.code.app.index import ingest_pass as w
from server.code.app.index import pass_budget as ingest_budget
from server.code.app.index import pass_commit as ingest_commit_mod
from server.code.app.index import pass_fetch as ingest_fetch
from server.code.app.index import pass_stream as ingest_stream
from server.code.app.index.round_reads import RoundReads, rounds
from server.settings import settings
from server.workers import pass_context, status_store
from tests.unit.code_fixtures import repo_config


class _Clock:
    """A monotonic clock the test advances; stands in for `time` in a module."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return 1_700_000_000.0 + self.now

    def perf_counter(self) -> float:
        return self.now


def _cfg(ns: str = "code-a", **kw: Any) -> Any:
    fields: dict[str, Any] = {
        "org": "o",
        "namespace": ns,
        "git_dir": "/nowhere.git",
        "embed_reuse_from": "",
        "path_prefixes": ("src/",),
        "source": "config",
    }
    fields.update(kw)
    return repo_config(**fields)


# ---------------------------------------------------------------------------
# the budget and the order of the legs
# ---------------------------------------------------------------------------


def test_the_budget_is_three_quarters_of_the_deadline_by_default() -> None:
    assert settings.code_ingest_max_pass_seconds == 900
    assert settings.code_ingest_pass_budget_fraction == 0.75
    b = ingest_budget.pass_budget(started=10.0)
    assert b.seconds == 675.0 and b.stop_after == 685.0


def _wire_run(
    monkeypatch: pytest.MonkeyPatch,
    cfgs: list[Any],
    plans: dict[str, ingest_budget.RepoPass | None],
    clock: _Clock,
    *,
    cost: float = 0.0,
) -> list[str]:
    """`_run` over fakes: `_run_repo` returns the plan named per namespace
    and advances the clock by `cost`; the legs record their order."""
    import server.code.app.index.repo_registry as rr

    order: list[str] = []

    async def refresh() -> None:
        return None

    async def inactive() -> set[tuple[str, str]]:
        return set()

    async def run_repo(cfg: Any, result: Any, budget: Any = None) -> Any:
        order.append(f"commits:{cfg.namespace}")
        clock.now += cost
        return plans.get(cfg.namespace)

    async def bootstrap(plan: Any, result: Any, budget: Any) -> None:
        order.append(f"bootstrap:{plan.cfg.namespace}")
        clock.now += cost

    async def walk(plan: Any, result: Any, budget: Any) -> None:
        order.append(f"walk:{plan.cfg.namespace}")
        clock.now += cost

    async def post(plan: Any, result: Any) -> None:
        order.append(f"post:{plan.cfg.namespace}")

    monkeypatch.setattr(rr, "refresh_db_repos", refresh)
    monkeypatch.setattr(w, "_inactive_namespaces", inactive)
    monkeypatch.setattr(w, "selected_repos", lambda org="", namespace="": list(cfgs))
    monkeypatch.setattr(w, "_run_repo", run_repo)
    monkeypatch.setattr(w, "_bootstrap_leg", bootstrap)
    monkeypatch.setattr(w, "_walk_leg", walk)
    monkeypatch.setattr(w, "_post_steps", post)
    monkeypatch.setattr(ingest_budget, "time", clock)
    monkeypatch.setattr(w, "time", clock)
    monkeypatch.setattr(ingest_commit_mod, "time", clock)
    monkeypatch.setattr(ingest_stream, "time", clock)
    monkeypatch.setattr(ingest_fetch, "time", clock)
    return order


def test_every_entrys_commits_run_before_any_bootstrap_slice_before_any_walk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.app.index import sandbox

    monkeypatch.setattr(sandbox, "_available", True)  # a self-registered entry is served
    clock = _Clock()
    a, b, c = _cfg("code-a"), _cfg("code-b", source="registry"), _cfg("code-c")
    plans = {
        "code-a": ingest_budget.RepoPass(a, object(), "h", "w", False, walk=True),
        "code-b": ingest_budget.RepoPass(b, object(), "h", "", True, bootstrap=True),
        "code-c": ingest_budget.RepoPass(c, object(), "h", "w", False, walk=True),
    }
    order = _wire_run(monkeypatch, [a, b, c], plans, clock)
    result = asyncio.run(w.run_pass())
    assert order == [
        "commits:code-a",
        "commits:code-b",
        "commits:code-c",
        "bootstrap:code-b",
        "walk:code-a",
        "walk:code-c",
        "post:code-a",
        "post:code-b",
        "post:code-c",
    ]
    assert result.partial is False and result.resume == ""
    assert result.budget_s == 675.0


def test_a_spent_budget_stops_starting_units_the_pass_succeeds_partial_with_the_resume_point(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three entries, each leg costing 100 s of a 250 s budget: the commits
    of all three run (checked before each: 0, 100, 200 < 250), the walk of
    the first starts at 300 — past the budget — so no walk runs, the pass
    is a success with `partial` and names where it stopped, and the cursor
    on the pass context is that point."""
    clock = _Clock()
    monkeypatch.setattr(settings, "code_ingest_max_pass_seconds", 1000)
    monkeypatch.setattr(settings, "code_ingest_pass_budget_fraction", 0.25)
    monkeypatch.setattr(settings, "code_ingest_walk_budget_share", 0.0)  # one budget
    a, b, c = _cfg("code-a"), _cfg("code-b"), _cfg("code-c")
    plans = {
        ns: ingest_budget.RepoPass(cfg, object(), "h", "w", False, walk=True)
        for ns, cfg in (("code-a", a), ("code-b", b), ("code-c", c))
    }
    order = _wire_run(monkeypatch, [a, b, c], plans, clock, cost=100.0)
    ctx = pass_context.begin("code_ingest")
    try:
        result = asyncio.run(w.run_pass())  # no exception: a cut pass is not a failed pass
    finally:
        pass_context.clear()
    assert order == [
        "commits:code-a",
        "commits:code-b",
        "commits:code-c",
        "post:code-a",
        "post:code-b",
        "post:code-c",
    ], "post-steps still run"
    assert result.partial is True and result.resume == "code-a:walk"
    assert ctx.cursor == "code-a:walk" and ctx.count == 0, "not a failed leg"
    outcome = w.pass_outcome(result)
    assert outcome["partial"] is True and outcome["resume"] == "code-a:walk"
    assert status_store.classify(success=True, outcome=outcome) in ("ok", "idle")


def test_the_budget_is_checked_before_each_entrys_commits_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    monkeypatch.setattr(settings, "code_ingest_max_pass_seconds", 1000)
    monkeypatch.setattr(settings, "code_ingest_pass_budget_fraction", 0.15)  # 150 s
    monkeypatch.setattr(settings, "code_ingest_walk_budget_share", 0.0)  # one budget
    a, b = _cfg("code-a"), _cfg("code-b")
    plans = {
        ns: ingest_budget.RepoPass(cfg, object(), "h", "w", False, walk=True)
        for ns, cfg in (("code-a", a), ("code-b", b))
    }
    order = _wire_run(monkeypatch, [a, b], plans, clock, cost=200.0)
    result = asyncio.run(w.run_pass())
    assert order == ["commits:code-a", "post:code-a"]
    assert result.partial and result.resume == "code-b:commits; code-a:walk"  # both phases


# ---------------------------------------------------------------------------
# between commits: the watermark is the cursor
# ---------------------------------------------------------------------------


class _StateRepo:
    def __init__(self) -> None:
        self.state: dict[str, str] = {}
        self.writes: list[tuple[str, str]] = []

    async def get_state(self, org: str, ns: str, key: str) -> str:
        return self.state.get(key, "")

    async def set_state(self, org: str, ns: str, key: str, value: str, detail: str = "") -> None:
        self.state[key] = value
        self.writes.append((key, value))


def _wire_commits(monkeypatch: pytest.MonkeyPatch, clock: _Clock, cost: float) -> list[str]:
    import server.code.app.index.denylist as dl
    import server.embedding.embedder as emb
    import server.workers.cost_event as cs

    ingested: list[str] = []

    async def ingest_commit(
        repo: Any, org: str, ns: str, git_dir: str, base: str, sha: str, **kw: Any
    ) -> None:
        ingested.append(sha)
        clock.now += cost

    async def load_denylist(cfg: Any, head: str, show: Any) -> None:
        return None

    monkeypatch.setattr(ingest_commit_mod, "_ingest_commit", ingest_commit)
    monkeypatch.setattr(ingest_stream, "_ingest_commit", ingest_commit)
    monkeypatch.setattr(ingest_commit_mod, "time", clock)
    monkeypatch.setattr(w, "time", clock)
    monkeypatch.setattr(ingest_budget, "time", clock)
    monkeypatch.setattr(ingest_stream, "time", clock)
    monkeypatch.setattr(ingest_fetch, "time", clock)
    monkeypatch.setattr(emb, "get_embedder", lambda: SimpleNamespace(model_name="m"))
    monkeypatch.setattr(dl, "load_denylist", load_denylist)
    monkeypatch.setattr(cs, "emit_worker_cost_event_for", lambda **kw: None)
    monkeypatch.setattr(settings, "code_context_description_enabled", False)
    return ingested


# ---------------------------------------------------------------------------
# the walk: rounds, the budget between them, the cursor by attempted
# ---------------------------------------------------------------------------


def test_rounds_are_contiguous_slices_and_zero_means_one_round() -> None:
    assert rounds(["a", "b", "c", "d", "e"], 2) == [["a", "b"], ["c", "d"], ["e"]]
    assert rounds(["a", "b"], 0) == [["a", "b"]]
    assert rounds([], 3) == []


def test_heal_stops_starting_rounds_at_the_budget_and_reports_what_it_attempted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    monkeypatch.setattr(reconcile, "time", clock)
    seen: list[str] = []

    async def show_file(git_dir: str, head: str, path: str, max_bytes: int = 0) -> bytes:
        return b"int x;"

    async def ingest_file(repo: Any, org: str, ns: str, *, path: str, **kw: Any) -> Any:
        seen.append(path)
        clock.now += 10.0
        return SimpleNamespace(chunks_new=0, chunks_moved=0, symbols_moved=0)

    async def load_denylist(cfg: Any, head: str, show: Any) -> None:
        return None

    import server.code.app.index.denylist as dl

    monkeypatch.setattr(dl, "load_denylist", load_denylist)
    paths = [f"src/{i}.cpp" for i in range(6)]
    res = asyncio.run(
        reconcile.heal(
            object(),
            _cfg(),
            "h" * 40,
            paths,
            embedder=SimpleNamespace(model_name="m"),
            ingest_file=ingest_file,
            show_file=show_file,
            stop_after=clock.now + 25.0,
            round_files=2,
        )
    )
    # round one at t=0 (→20), round two at t=20 < 25 (→40), round three not started
    assert seen == paths[:4]
    assert (res.attempted, res.partial, res.rounds, res.files) == (4, True, 2, 4)

    seen.clear()
    res = asyncio.run(
        reconcile.heal(
            object(),
            _cfg(),
            "h" * 40,
            paths[4:],
            embedder=SimpleNamespace(model_name="m"),
            ingest_file=ingest_file,
            show_file=show_file,
            stop_after=None,
            round_files=2,
        )
    )
    assert seen == paths[4:] and res.attempted == 2 and res.partial is False


class _WalkRepo:
    def __init__(self, paths: list[str], state: dict[str, dict[str, str]]) -> None:
        self.paths, self.state = paths, state

    async def all_state(self, org: str, ns: str) -> dict[str, dict[str, str]]:
        return self.state

    async def set_state(self, org: str, ns: str, key: str, value: str, detail: str = "") -> None:
        self.state[key] = {"value": value, "detail": detail}

    async def live_file_hashes(self, org: str, ns: str) -> dict[str, str]:
        return {p: "h" for p in self.paths}


def test_a_cut_rechunk_advances_its_cursor_by_the_rounds_it_started_and_the_next_pass_redoes_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.domain.extract import extractor_stamp as es

    monkeypatch.setattr(settings, "code_extractor_rechunk_enabled", True)
    monkeypatch.setattr(settings, "code_extractor_rechunk_per_pass", 4)
    cur, stages = {"chunk": "c1", "symbol": "s1", "edge": "e1", "guard": "g1"}, es.STAGES
    monkeypatch.setattr(es, "extractor_stamp", lambda: "NEW")
    monkeypatch.setattr(es, "stage_stamps", lambda: dict(cur))
    paths = [f"src/{i}.cpp" for i in range(6)]
    state = {"extractor_stamp": {"value": "OLD", "detail": "{}"}}
    state.update({f"extractor_stamp_{s}": {"value": "x", "detail": "{}"} for s in stages})
    repo = _WalkRepo(paths, state)
    healed_batches: list[list[str]] = []
    cut = {"attempted": 2, "partial": True}

    async def fake_heal(repo_: Any, cfg: Any, head: str, batch: list[str], **kw: Any) -> Any:
        healed_batches.append(list(batch))
        assert kw["stop_after"] == 42.0, "the pass budget reaches the heal"
        return SimpleNamespace(files=cut["attempted"], identical=0, **cut)

    import server.embedding.embedder as emb

    monkeypatch.setattr(reconcile, "heal", fake_heal)
    monkeypatch.setattr(emb, "get_embedder", lambda: object())
    budget = ingest_budget.PassBudget(started=0.0, stop_after=42.0)

    result = ingest_budget.CodeIngestResult()
    asyncio.run(w._maybe_rechunk(repo, _cfg(), "head", result, budget=budget))
    detail = json.loads(state["extractor_stamp"]["detail"])
    assert healed_batches == [paths[:4]] and detail["cursor"] == 2, "cursor = attempted, not batch"
    assert result.partial and result.resume == "code-a:rechunk@2/6"
    assert (result.files_planned, result.files_done, result.rechunked) == (4, 2, 2)

    cut.update(attempted=4, partial=False)
    nxt = ingest_budget.CodeIngestResult()
    asyncio.run(w._maybe_rechunk(repo, _cfg(), "head", nxt, budget=budget))
    assert healed_batches[-1] == paths[2:6], "continues at the cursor: no path redone"
    assert json.loads(state["extractor_stamp"]["detail"]).get("completed") is True
    assert nxt.partial is False


def test_a_walk_whose_budget_is_spent_after_the_reconcile_does_not_start_the_rechunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def reconcile_(*a: Any, **k: Any) -> None:
        calls.append("reconcile")

    async def rechunk(*a: Any, **k: Any) -> None:
        calls.append("rechunk")

    monkeypatch.setattr(w, "_maybe_reconcile", reconcile_)
    monkeypatch.setattr(w, "_maybe_rechunk", rechunk)
    result = ingest_budget.CodeIngestResult()
    spent = ingest_budget.PassBudget(started=0.0, stop_after=0.0)
    asyncio.run(w._steady_state(object(), _cfg(), "h", result, budget=spent))
    assert calls == ["reconcile"] and result.resume == "code-a:rechunk"


# ---------------------------------------------------------------------------
# the bootstrap slice under the same budget
# ---------------------------------------------------------------------------


def test_the_bootstrap_slice_stops_starting_rounds_at_the_budget_and_its_cursor_advances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.code.app.index.bootstrap as bs
    import server.code.app.index.denylist as dl
    import server.embedding.embedder as emb
    from server.code.app.index import gitrepo

    clock = _Clock()
    monkeypatch.setattr(bs, "time", clock)
    monkeypatch.setattr(settings, "code_ingest_round_files", 1)
    paths = ["a.cpp", "b.cpp", "c.cpp"]
    calls: list[str] = []

    async def ls_tree(git_dir: str, head: str, prefixes: Any) -> list[str]:
        return list(paths)

    async def show_file(git_dir: str, head: str, path: str, max_bytes: int = 0) -> bytes:
        return b"int x;"

    async def load_denylist(cfg: Any, head: str, show: Any) -> None:
        return None

    async def ingest_file(repo: Any, org: str, ns: str, **kw: Any) -> Any:
        calls.append(kw["path"])
        clock.now += 10.0
        return SimpleNamespace(embedded=0, embed_stubs=0)

    class _Repo:
        def __init__(self) -> None:
            self.state: dict[str, str] = {}

        async def get_state(self, org: str, ns: str, key: str) -> str:
            return self.state.get(key, "")

        async def set_state(
            self, org: str, ns: str, key: str, value: str, detail: str = ""
        ) -> None:
            self.state[key] = value

    monkeypatch.setattr(gitrepo, "ls_tree", ls_tree)
    monkeypatch.setattr(gitrepo, "show_file", show_file)
    monkeypatch.setattr(dl, "load_denylist", load_denylist)
    monkeypatch.setattr(ingest, "ingest_file", ingest_file)
    monkeypatch.setattr(ingest_headers, "headers_first", lambda ps: list(ps))
    monkeypatch.setattr(emb, "get_embedder", lambda: SimpleNamespace(model_name="m"))
    monkeypatch.setattr(bs, "bootstrap_embedder", lambda: None)
    repo = _Repo()
    cfg = SimpleNamespace(
        org="o", namespace="n", git_dir="/nowhere.git", path_prefixes=(), label="o/n"
    )
    progress = asyncio.run(bs.index_slice(cfg, repo, "f" * 40, stop_after=clock.now + 15.0))
    # round one at t=0 (→10), round two at t=10 < 15 (→20), round three not started
    assert calls == ["a.cpp", "b.cpp"]
    assert progress == {"done": 2, "total": 3, "stage": "indexing", "partial": True}
    assert repo.state[bs.CURSOR_KEY] == "2"


# ---------------------------------------------------------------------------
# the round-wide reads: two statements, the same rows
# ---------------------------------------------------------------------------

_A = b"int f() { return 1; }\nint h() { return 2; }\n"
_B = b"int g() { return f(); }\nint k() { return h() + f(); }\n"
# calls names no file of the round defines: the prefetch alone answers them
_C = b"int m() { return q(); }\nint n() { return r() + q(); }\n"


class _ReadRepo:
    """Records every read and write; the name table resolves `f` and `h`."""

    def __init__(self) -> None:
        self.reads: list[str] = []
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []

    async def live_chunk_index_for_path(self, *_: Any) -> list[dict[str, Any]]:
        self.reads.append("code_chunks")
        return []

    async def chunks_full_by_ids(self, *_: Any) -> list[dict[str, Any]]:
        return []

    async def live_symbols_for_path(self, *_: Any) -> list[dict[str, Any]]:
        self.reads.append("code_symbols")
        return []

    async def live_symbols_for_paths(
        self, org: str, ns: str, paths: list[str], tier: str
    ) -> dict[str, Any]:
        self.reads.append(f"code_symbols:batch:{','.join(sorted(paths))}")
        return {p: [] for p in paths}

    async def live_call_misses_for_paths(
        self, org: str, ns: str, paths: list[str]
    ) -> dict[str, Any]:
        self.reads.append(f"code_call_sites_unresolved:batch:{','.join(sorted(paths))}")
        return {p: [] for p in paths}

    async def live_call_guards_for_paths(
        self, org: str, ns: str, paths: list[str]
    ) -> dict[str, Any]:
        self.reads.append(f"code_caller_guards:batch:{','.join(sorted(paths))}")
        return {p: [] for p in paths}

    async def name_candidates(self, org: str, ns: str, names: list[str]) -> dict[str, Any]:
        self.reads.append(f"code_symbol_names:{','.join(sorted(names))}")
        table = {
            "f": [("sid-f", "f", "function")],
            "h": [("sid-h", "h", "function")],
            "q": [("sid-q", "q", "function")],
            "r": [("sid-r", "r", "function")],
        }
        return {n: table[n] for n in names if n in table}

    async def live_edges_for_path(
        self, org: str, ns: str, path: str, tier: str
    ) -> list[dict[str, Any]]:
        self.reads.append(f"code_edges:{path}")
        return []

    async def live_edges_for_paths(
        self, org: str, ns: str, paths: list[str], tier: str
    ) -> dict[str, Any]:
        self.reads.append(f"code_edges:batch:{','.join(sorted(paths))}")
        return {p: [] for p in paths}

    async def live_call_misses_for_path(self, *_: Any) -> list[dict[str, Any]]:
        self.reads.append("code_call_sites_unresolved")
        return []

    async def live_call_guards_for_path(self, *_: Any) -> list[dict[str, Any]]:
        self.reads.append("code_caller_guards")
        return []

    async def insert_rows(self, table: str, rows: list[dict[str, Any]]) -> None:
        self.inserts.append((table, rows))


def _rows(repo: _ReadRepo) -> list[tuple[str, str]]:
    out = []
    for table, rows in repo.inserts:
        for r in rows:
            r = {k: v for k, v in r.items() if k not in ("version", "created_at", "updated_at")}
            out.append((table, json.dumps(r, sort_keys=True, default=str)))
    return sorted(out)


async def _ingest_pair(
    repo: Any,
    *,
    parsed: dict[str, Any] | None = None,
    second: bytes = _B,
) -> None:
    for path, src in (("src/a.cpp", _A), ("src/b.cpp", second)):
        await ingest.ingest_file(
            repo,
            "o",
            "code-ns",
            path=path,
            source=src,
            commit_sha="c" * 40,
            embedding_model="m",
            embedder=None,
            parsed=(parsed or {}).get(path),
        )


def _batched_pair(second: bytes) -> tuple[_ReadRepo, RoundReads]:
    batched = _ReadRepo()
    reads = RoundReads(batched, "o", "code-ns")

    async def run() -> None:
        await reads.prefetch({"src/a.cpp": _A, "src/b.cpp": second})
        await _ingest_pair(reads, parsed=reads.parsed, second=second)
        await reads.flush()  # The round loop writes the buffer at its end

    asyncio.run(run())
    return batched, reads


def test_a_round_answers_the_edge_and_name_reads_from_two_statements_with_the_same_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    per_file = _ReadRepo()
    asyncio.run(_ingest_pair(per_file, second=_C))
    name_reads = [r for r in per_file.reads if r.startswith("code_symbol_names")]
    edge_reads = [r for r in per_file.reads if r.startswith("code_edges")]
    assert len(name_reads) == 1 and len(edge_reads) == 2, "b.cpp resolves names; both read edges"

    batched, reads = _batched_pair(_C)
    name_reads = [r for r in batched.reads if r.startswith("code_symbol_names")]
    edge_reads = [r for r in batched.reads if r.startswith("code_edges")]
    assert name_reads == ["code_symbol_names:q,r"], "one statement for the round's names"
    assert edge_reads == ["code_edges:batch:src/a.cpp,src/b.cpp"], "one statement for the paths"
    per_path = [
        r
        for r in batched.reads
        if r.split(":")[0] in ("code_symbols", "code_call_sites_unresolved", "code_caller_guards")
    ]
    assert per_path == [
        "code_symbols:batch:src/a.cpp,src/b.cpp",
        "code_call_sites_unresolved:batch:src/a.cpp,src/b.cpp",
        "code_caller_guards:batch:src/a.cpp,src/b.cpp",
    ], "symbols, declined sites and guards: one round-wide statement each"
    assert reads.misses == 0 and reads.hits >= 9
    assert set(reads.parsed) == {"src/a.cpp", "src/b.cpp"}
    assert _rows(batched) == _rows(per_file), "the rows written are the same"
    assert any(t == "code_edges" and rows for t, rows in batched.inserts), "edges were derived"


def test_a_file_that_defines_a_name_a_later_file_of_the_round_calls_forces_its_reread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Headers before sources: `a.cpp` inserts symbols `f`
    and `h`, so `b.cpp`'s resolution of them must not come from the
    prefetch — the insert drops them and the read goes to the table; the
    rows equal the per-file ingest's."""
    per_file = _ReadRepo()
    asyncio.run(_ingest_pair(per_file, second=_B))
    batched, reads = _batched_pair(_B)
    name_reads = [r for r in batched.reads if r.startswith("code_symbol_names")]
    assert name_reads == ["code_symbol_names:f,h", "code_symbol_names:f,h"], (
        "the prefetch, then the re-read after a.cpp's symbol insert"
    )
    assert reads.misses == 1
    assert _rows(batched) == _rows(per_file), "the rows written are the same"


def test_a_name_the_round_did_not_prefetch_is_read_on_the_miss_and_a_symbol_insert_invalidates_it() -> (
    None
):
    repo = _ReadRepo()
    reads = RoundReads(repo, "o", "code-ns")

    async def run() -> dict[str, Any]:
        await reads.prefetch({"src/a.cpp": _A})  # names: none (a.cpp calls nothing)
        first = await reads.name_candidates("o", "code-ns", ["f", "zz"])
        again = await reads.name_candidates("o", "code-ns", ["f"])
        await reads.insert_rows("code_symbols", [{"name": "f", "qualified_name": "f"}])
        after = await reads.name_candidates("o", "code-ns", ["f"])
        return {"first": first, "again": again, "after": after}

    out = asyncio.run(run())
    assert out["first"] == {"f": [("sid-f", "f", "function")]} and out["again"] == out["first"]
    assert out["after"] == out["first"]
    name_reads = [r for r in repo.reads if r.startswith("code_symbol_names")]
    assert name_reads == ["code_symbol_names:f,zz", "code_symbol_names:f"], (
        "the miss read once (zz absent stays known-absent); the insert forced a re-read of f"
    )
    assert [t for t, _ in repo.inserts] == ["code_symbols"], "inserts pass through"
    assert reads.hits == 1 and reads.misses == 2


def test_a_repository_without_the_batched_read_fails_open_to_the_per_file_reads() -> None:
    class _Plain:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def live_edges_for_path(self, org: str, ns: str, path: str, tier: str) -> list[Any]:
            self.calls.append(path)
            return []

        async def name_candidates(self, org: str, ns: str, names: list[str]) -> dict[str, Any]:
            return {}

    plain = _Plain()
    reads = RoundReads(plain, "o", "n")

    async def run() -> list[Any]:
        await reads.prefetch({"src/b.cpp": _B})
        return await reads.live_edges_for_path("o", "n", "src/b.cpp", "treesitter")

    assert asyncio.run(run()) == [] and plain.calls == ["src/b.cpp"]
    assert reads.misses == 1 and reads.hits == 0


def test_round_reads_off_keeps_the_per_file_path(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.code.app.index.round_reads import round_size

    monkeypatch.setattr(settings, "code_ingest_round_files", 0)
    assert round_size() == 0
    assert rounds(["a", "b", "c"], round_size()) == [["a", "b", "c"]]


# ---------------------------------------------------------------------------
# the outcome on the pass row
# ---------------------------------------------------------------------------


def test_the_outcome_states_the_rows_three_counts_and_the_first_sixteen_carry_partial_and_resume() -> (
    None
):
    from server.workers.supervisor import _OUTCOME_ARGS_MAX, _pass_args

    result = ingest_budget.CodeIngestResult()
    result.files_planned, result.files_done = 640, 425
    result.commits_planned, result.commits_done = 20, 7
    result.chunks_new, result.symbols_new, result.edges_new = 10, 5, 30
    result.cut("code-clickhouse-26-3:rechunk@3600/22014", cfg=_cfg("code-clickhouse-26-3"))
    outcome = w.pass_outcome(result)
    assert list(outcome)[:5] == ["items_planned", "items_done", "rows_written", "partial", "resume"]
    assert status_store.counts(outcome) == (640, 425, 45)
    assert status_store.classify(success=True, outcome=outcome) == "ok"
    args = _pass_args("code_ingest", spent=False, error=None, outcome=outcome)
    assert args["outcome_partial"] == "True"
    assert args["outcome_resume"] == "code-clickhouse-26-3:rechunk@3600/22014"
    assert args["outcome_commits_done"] == "7"
    assert len([k for k in args if k.startswith("outcome_")]) == _OUTCOME_ARGS_MAX
    idle = w.pass_outcome(ingest_budget.CodeIngestResult())
    assert status_store.classify(success=True, outcome=idle) == "idle"


def test_the_registry_deadline_is_the_setting_and_the_default_is_the_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.workers.registry import enabled_workers

    monkeypatch.setattr(settings, "code_ingest_enabled", True)
    monkeypatch.setattr(settings, "code_ingest_max_pass_seconds", 900)
    spec = next(s for s in enabled_workers() if s.name == "code_ingest")
    assert spec.deadline_seconds() == 900.0
    assert ingest_budget.pass_budget(started=0.0).seconds == 675.0
