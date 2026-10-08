"""The weekly graph-stats pass finishes, and restarts nothing
it already did.

Pinned here: the cheap legs run for every namespace before any unit-risk
row is written, so the first-pass due conditions are met whatever the
second loop manages; the unit-risk leg reads the table's own stamps and
builds only the paths without a row or with one that would go stale before
the next pass; a pass-level budget checked between rounds and namespaces
stops the leg cleanly, names the resume point and leaves the pass a
success with `unit_risk_partial`; the two edge reads run once per round
with rows identical to the per-file build; a cancelled leg issues no
further statement; the worker carries its own deadline while the 900 s cap
stays for everyone else; a pass cut at its deadline is held one interval,
not the 1 s backoff; `graph_stats_state` and `code.index_status` show the
leg's progress and fail open without the table.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from server.code.api.models_status import CodeIndexStatusOutput
from server.code.app import index_status
from server.code.app import unit_risk as cur
from server.code.workers import graph_stats as cgs
from server.settings import settings
from server.workers import pass_context
from server.workers import supervisor as sup
from server.workers.registry import (
    DEADLINE_CAP_SECONDS,
    WorkerSpec,
    default_max_pass_seconds,
    enabled_workers,
)
from server.workers.status import get_registry

WEEK = 7 * 24 * 3600
PATHS = ["src/A.cpp", "src/B.cpp", "src/C.cpp", "src/D.cpp"]


# ---- a stub repo over several namespaces ----------------------------------------


def _sym(sid: str, path: str, s: int, e: int) -> dict[str, Any]:
    return {
        "symbol_id": sid,
        "qualified_name": f"DB::{sid}",
        "name": sid,
        "signature": "",
        "path": path,
        "start_line": s,
        "end_line": e,
        "tier": "scip",
        "kind": "function",
    }


def _edge(frm: str, to: str, path: str, line: int) -> dict[str, Any]:
    return {
        "from_symbol_id": frm,
        "to_symbol_id": to,
        "from_qualified_name": f"DB::{frm}",
        "to_qualified_name": f"DB::{to}",
        "path": path,
        "line": line,
        "tier": "scip",
        "edge_kind": "calls",
        "confidence": 1.0,
    }


def _chunk(cid: str, path: str, s: int, e: int) -> dict[str, Any]:
    return {"chunk_id": cid, "path": path, "kind": "function", "start_line": s, "end_line": e}


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def monotonic(self) -> float:
        return self.t


class _Repo:
    """Every code namespace holds the same four files; reads filter by the
    keys asked for. `inserted` keeps (namespace, table, rows) in order,
    `statements` counts every read and write, `table` is code_unit_risk."""

    def __init__(self, namespaces: list[str], *, clock: _Clock | None = None) -> None:
        self.namespaces = namespaces
        self.clock = clock
        self.statements = 0
        self.calls: list[tuple[str, str, Any]] = []
        self.inserted: list[tuple[str, str, list[dict[str, Any]]]] = []
        self.table: dict[tuple[str, str], dict[str, Any]] = {}
        self.symbols = [
            _sym("a1", "src/A.cpp", 10, 40),
            _sym("a2", "src/A.cpp", 100, 130),
            _sym("b1", "src/B.cpp", 1, 50),
            _sym("c1", "src/C.cpp", 5, 60),
            _sym("d1", "src/D.cpp", 1, 20),
        ]
        self.chunks = [
            _chunk("chA", "src/A.cpp", 12, 35),
            _chunk("chA2", "src/A.cpp", 101, 120),
            _chunk("chB", "src/B.cpp", 2, 40),
            _chunk("chC", "src/C.cpp", 6, 50),
            _chunk("chD", "src/D.cpp", 1, 15),
        ]
        self.callers = [
            _edge("x1", "a1", "src/X.cpp", 5),
            _edge("x2", "a1", "src/X.cpp", 9),
            _edge("x3", "b1", "src/X.cpp", 12),
            _edge("x1", "c1", "src/X.cpp", 15),
            _edge("x4", "c1", "src/Y.cpp", 2),
            _edge("x5", "c1", "src/Y.cpp", 4),
        ]
        self.callees = [
            _edge("a1", "z1", "src/Z.cpp", 77),
            _edge("a1", "z2", "src/Z.cpp", 80),
            _edge("b1", "z1", "src/Z.cpp", 77),
            _edge("c1", "z9", "src/Z.cpp", 99),
            _edge("d1", "z1", "src/Z.cpp", 77),
        ]
        self.gate: asyncio.Event | None = None  # when set, edge reads wait on it
        self.in_flight = asyncio.Event()
        self.state_over: dict[str, dict[str, Any]] = {}  # per-namespace overrides of the state

    def _note(self, kind: str, ns: str, arg: Any = None) -> None:
        self.statements += 1
        self.calls.append((kind, ns, arg))

    # -- the state the due check reads --
    async def graph_stats_state(self, org: str, ns: str) -> dict[str, Any]:
        self._note("state", ns)
        stats = self.rows("code_symbol_stats", ns)
        return {
            "files": True,
            "ranks": True,
            "test_map": True,
            "symbol_stats_rows": len(stats),
            "symbol_stats_at": stats[-1]["computed_at"] if stats else None,
            "unit_risk_rows": len(self.rows("code_unit_risk", ns)),
            "unit_risk_at": None,
            **self.state_over.get(ns, {}),
        }

    # -- the cheap legs --
    async def live_file_hashes(self, org: str, ns: str) -> dict[str, str]:
        self._note("files", ns)
        return {p: "h" for p in PATHS}

    async def include_edges(self, org: str, ns: str) -> list[tuple[str, str]]:
        self._note("includes", ns)
        return []

    async def file_card_ranks(self, org: str, ns: str, model: str) -> dict[str, Any]:
        self._note("summaries", ns)
        return {}

    async def live_file_cards(
        self, org: str, ns: str, model: str, paths: list[str]
    ) -> list[dict[str, Any]]:
        return []

    async def pr_file_pairs(self, org: str, ns: str) -> list[dict[str, Any]]:
        self._note("pr_pairs", ns)
        return []

    async def symbol_fan_in(self, org: str, ns: str) -> list[dict[str, Any]]:
        self._note("fan_in", ns)
        return [
            {
                "symbol_id": "a1",
                "qualified_name": "DB::a1",
                "fan_in_calls": 2,
                "fan_in_nontest": 2,
                "caller_files": 1,
            }
        ]

    async def symbol_definition_sites(self, org: str, ns: str) -> dict[str, Any]:
        self._note("sites", ns)
        return {"a1": (1, 0, "src/A.cpp")}

    async def live_symbol_stats_ids(self, org: str, ns: str) -> set[str]:
        self._note("stats_ids", ns)
        return set()

    # -- the unit-risk leg --
    async def pr_churn_ranked(
        self, org: str, ns: str, *, days: int = 365, limit: int = 2000
    ) -> list[tuple[str, int]]:
        self._note("churn_ranked", ns)
        return [("src/A.cpp", 12), ("src/C.cpp", 7)]

    async def tested_paths(self, org: str, ns: str, paths: list[str] | None = None) -> set[str]:
        self._note("tested_paths", ns)
        return {"src/A.cpp", "src/C.cpp"}

    async def chunk_units_for_paths(
        self, org: str, ns: str, model: str, paths: list[str], *, per_statement: int = 50
    ) -> list[dict[str, Any]]:
        self._note("chunk_units", ns, sorted(paths))
        return [c for c in self.chunks if c["path"] in paths]

    async def functions_for_paths(
        self,
        org: str,
        ns: str,
        paths: list[str],
        *,
        limit: int = 400,
        per_statement: int = 20,
        **_kw,
    ) -> list[dict[str, Any]]:
        self._note("symbols", ns, sorted(paths))
        return [s for s in self.symbols if s["path"] in paths]

    async def edges_for_symbols(
        self,
        org: str,
        ns: str,
        ids: list[str],
        *,
        direction: str,
        edge_kinds: list[str] | None = None,
        limit: int = 200,
        per_match: int | None = None,
    ) -> list[dict[str, Any]]:
        self._note(direction, ns, sorted(ids))
        if self.gate is not None:
            self.in_flight.set()
            await self.gate.wait()
        rows = self.callers if direction == "callers" else self.callees
        key = "to_symbol_id" if direction == "callers" else "from_symbol_id"
        return [r for r in rows if r[key] in ids][:limit]

    async def unit_risk_path_stamps(self, org: str, ns: str) -> dict[str, datetime]:
        self._note("path_stamps", ns)
        out: dict[str, datetime] = {}
        for (row_ns, _uid), r in self.table.items():
            if row_ns != ns:
                continue
            p = str(r["path"])
            if p not in out or r["computed_at"] > out[p]:
                out[p] = r["computed_at"]
        return out

    async def insert_rows(self, table: str, rows: list[dict[str, Any]]) -> None:
        self._note(f"insert:{table}", str(rows[0]["namespace"]) if rows else "")
        if not rows:
            return
        ns = str(rows[0]["namespace"])
        self.inserted.append((ns, table, list(rows)))
        if table == "code_unit_risk":
            for r in rows:
                self.table[(ns, str(r["unit_id"]))] = dict(r)
        if self.clock is not None:
            self.clock.t += 10.0  # every insert costs ten seconds of budget

    def rows(self, table: str, ns: str | None = None) -> list[dict[str, Any]]:
        return [
            r for n, t, rs in self.inserted for r in rs if t == table and (ns is None or n == ns)
        ]


class _Cfg:
    def __init__(self, ns: str) -> None:
        self.org, self.namespace, self.label = "o", ns, f"o/{ns}"


class _Embedder:
    model_name = "m"


def _wire(monkeypatch: pytest.MonkeyPatch, repo: _Repo) -> None:
    import server.code.domain.index.repos as repos_mod
    import server.code.store.index as cr
    import server.embedding.embedder as emb

    monkeypatch.setattr(cr, "CodeRepo", lambda: repo)
    monkeypatch.setattr(emb, "get_embedder", lambda: _Embedder())
    monkeypatch.setattr(repos_mod, "configured_repos", lambda: [_Cfg(ns) for ns in repo.namespaces])
    monkeypatch.setattr(settings, "code_unit_risk_max_age_s", 10 * 24 * 3600)
    monkeypatch.setattr(settings, "code_graph_stats_interval_seconds", WEEK)
    get_registry().reset()


def _first_index(repo: _Repo, table: str) -> int:
    return next(i for i, (_n, t, _r) in enumerate(repo.inserted) if t == table)


# ---- the two loops ---------------------------------------------------------------


async def test_every_namespaces_cheap_legs_run_before_any_unit_risk_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _Repo(["code-a", "code-b", "code-c"])
    _wire(monkeypatch, repo)
    monkeypatch.setattr(settings, "code_graph_stats_unit_risk_budget_s", 3600)
    result = await cgs.run_pass()
    assert result.errors == []
    for ns in repo.namespaces:
        assert repo.rows("code_symbol_stats", ns) and repo.rows("code_unit_risk", ns)
    stats_inserts = [i for i, (_n, t, _r) in enumerate(repo.inserted) if t == "code_symbol_stats"]
    assert max(stats_inserts) < _first_index(repo, "code_unit_risk")
    assert result.unit_risk_partial is False and result.unit_risk_resume == ""
    assert result.unit_risk_rows == len(repo.rows("code_unit_risk")) == 3 * 10
    assert result.namespaces_refreshed == 3
    # Items are namespace-legs (3 cheap + 3 unit-risk), rows every table's rows,
    # stated first on the outcome so the status row reads them exactly
    assert (result.legs_planned, result.legs_done) == (6, 6)
    assert (
        result.rows_written()
        == (
            result.files_ranked
            + result.test_map_rows
            + result.symbol_stats_rows
            + result.unit_risk_rows
        )
        > 0
    )
    outcome = get_registry().snapshot()["code_graph_stats"]["last_outcome"]
    assert list(outcome)[:3] == ["items_planned", "items_done", "rows_written"]
    assert (outcome["items_planned"], outcome["items_done"], outcome["rows_written"]) == (
        6,
        6,
        result.rows_written(),
    )


async def test_only_the_namespaces_owed_their_cheap_legs_get_them_the_risk_leg_runs_for_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _Repo(["code-a", "code-b", "code-c"])
    _wire(monkeypatch, repo)
    monkeypatch.setattr(settings, "code_graph_stats_unit_risk_budget_s", 3600)
    fresh = {"symbol_stats_rows": 5, "symbol_stats_at": datetime.now() - timedelta(days=1)}
    stale = {"symbol_stats_rows": 5, "symbol_stats_at": datetime.now() - timedelta(days=8)}
    repo.state_over = {"code-a": fresh, "code-b": stale}  # code-c: empty table
    assert await cgs.first_pass_due() is True
    assert set(await cgs.due_namespaces()) == {"o/code-b", "o/code-c"}
    result = await cgs.run_pass()
    assert result.namespaces_refreshed == 2
    assert repo.rows("code_symbol_stats", "code-a") == []  # fresh: nothing owed
    assert repo.rows("code_symbol_stats", "code-b") and repo.rows("code_symbol_stats", "code-c")
    for ns in repo.namespaces:  # the resumable leg looks at every namespace
        assert repo.rows("code_unit_risk", ns)
    # every namespace fresh: no cheap leg runs, the pass is not due at boot
    repo.state_over = {ns: fresh for ns in repo.namespaces}
    assert await cgs.first_pass_due() is False
    before = len(repo.rows("code_symbol_stats"))
    result = await cgs.run_pass()
    assert result.namespaces_refreshed == 0 and len(repo.rows("code_symbol_stats")) == before


async def test_a_failed_state_read_reads_as_due_and_stats_off_rewrites_every_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _Repo(["code-a", "code-b"])
    _wire(monkeypatch, repo)
    monkeypatch.setattr(settings, "code_graph_stats_unit_risk_budget_s", 0)

    async def _boom(org: str, ns: str) -> dict[str, Any]:
        raise RuntimeError("clickhouse away")

    repo.graph_stats_state = _boom  # type: ignore[method-assign]
    assert await cgs.due_namespaces() == {"o/code-a": "state_unknown", "o/code-b": "state_unknown"}
    result = await cgs.run_pass()
    assert result.namespaces_refreshed == 2 and result.errors == []


async def test_a_budget_already_spent_leaves_the_cheap_legs_done_and_the_pass_a_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _Repo(["code-a", "code-b", "code-c"])
    _wire(monkeypatch, repo)
    monkeypatch.setattr(settings, "code_graph_stats_unit_risk_budget_s", 0)
    ctx = pass_context.begin("code_graph_stats")
    try:
        result = await cgs.run_pass()  # no exception: a partial leg is not a failed pass
    finally:
        pass_context.clear()
    assert ctx.count == 0
    for ns in repo.namespaces:
        # the first-pass due conditions are met by loop one alone
        assert repo.rows("code_symbol_stats", ns)
        assert (
            cgs.pass_due(
                {
                    "files": True,
                    "ranks": True,
                    "test_map": True,
                    "symbol_stats_rows": len(repo.rows("code_symbol_stats", ns)),
                    "symbol_stats_at": datetime.now(),
                },
                interval_seconds=WEEK,
            )
            == ""
        )
    assert repo.rows("code_unit_risk") == []
    assert result.unit_risk_partial is True
    assert result.unit_risk_resume == "code-a,code-b,code-c"
    snap = get_registry().snapshot()["code_graph_stats"]
    assert snap["last_error"] is None and snap["consecutive_failures"] == 0
    assert snap["last_outcome"]["unit_risk_partial"] is True
    assert snap["last_outcome"]["unit_risk_resume"] == "code-a,code-b,code-c"


async def test_the_budget_is_checked_between_namespaces(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock()
    repo = _Repo(["code-a", "code-b", "code-c"], clock=clock)
    _wire(monkeypatch, repo)
    monkeypatch.setattr(cgs, "time", clock)
    monkeypatch.setattr(cur, "time", clock)
    # loop one: 3 namespaces x 1 stats insert = 30 s; a 35 s budget leaves one
    # namespace's unit-risk insert (10 s) before the check between namespaces
    # fails at 40 s — code-b and code-c are named for the next pass
    monkeypatch.setattr(settings, "code_graph_stats_unit_risk_budget_s", 35)
    result = await cgs.run_pass()
    assert [ns for ns in repo.namespaces if repo.rows("code_unit_risk", ns)] == ["code-a"]
    assert result.unit_risk_partial is True
    assert result.unit_risk_resume == "code-b,code-c"


# ---- the resumable, budgeted leg ------------------------------------------------------


def test_rewrite_threshold_keeps_no_row_that_would_be_stale_before_the_next_pass() -> None:
    assert cur.rewrite_threshold_s(10 * 24 * 3600, WEEK) == 3 * 24 * 3600
    assert cur.rewrite_threshold_s(WEEK, WEEK) == 0  # max age at the interval: rewrite all
    assert cur.rewrite_threshold_s(3600, WEEK) == 0


def test_paths_to_build_are_the_missing_and_the_ageing_in_sorted_order() -> None:
    now = datetime(2026, 9, 16, 12, 0)
    stamps = {
        "src/B.cpp": now - timedelta(days=1),  # fresh: kept
        "src/A.cpp": now - timedelta(days=5),  # would be 12 days old at the next pass: rebuilt
        "src/D.cpp": now - timedelta(days=3, seconds=1),  # just over the line: rebuilt
        "src/gone.cpp": now,  # not live any more: ignored
    }
    due = cur.paths_to_build(PATHS, stamps, threshold_s=3 * 24 * 3600, now=now)
    assert due == ["src/A.cpp", "src/C.cpp", "src/D.cpp"]
    assert cur.paths_to_build(PATHS, {}, threshold_s=3 * 24 * 3600, now=now) == PATHS
    assert cur.paths_to_build(PATHS, stamps, threshold_s=0, now=now) == PATHS  # rewrite all


async def test_the_leg_stops_at_the_budget_between_rounds_and_the_next_pass_resumes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    repo = _Repo(["code-a"], clock=clock)
    _wire(monkeypatch, repo)
    monkeypatch.setattr(cur, "time", clock)
    monkeypatch.setattr(cur, "BUILD_PATHS_PER_ROUND", 1)
    # two inserts fit (t = 1000 -> 1010 -> 1020 >= 1015 at the third check)
    first = await cur.refresh_namespace(repo, "o", "code-a", "m", deadline=clock.t + 15)
    assert (first.paths_live, first.paths_due, first.paths_built) == (4, 4, 2)
    assert first.partial is True and first.resume_at == "src/C.cpp"
    assert sorted({r["path"] for r in repo.rows("code_unit_risk")}) == ["src/A.cpp", "src/B.cpp"]
    assert first.rows_written == len(repo.rows("code_unit_risk"))

    # the next pass: one stamp read, then only the remainder is built
    repo.calls.clear()
    second = await cur.refresh_namespace(repo, "o", "code-a", "m")
    assert (second.paths_due, second.paths_built, second.partial) == (2, 2, False)
    kinds = [c[0] for c in repo.calls]
    assert kinds.count("path_stamps") == 1
    assert [c[2] for c in repo.calls if c[0] == "chunk_units"] == [["src/C.cpp"], ["src/D.cpp"]]
    assert sorted({r["path"] for r in repo.rows("code_unit_risk")}) == PATHS

    # a third pass the same day has nothing to do and reads nothing per file
    repo.calls.clear()
    third = await cur.refresh_namespace(repo, "o", "code-a", "m")
    assert (third.paths_due, third.paths_built, third.rows_written) == (0, 0, 0)
    assert {c[0] for c in repo.calls} == {"files", "path_stamps"}


async def test_a_pass_rewrites_the_rows_that_would_age_out_and_keeps_the_fresh_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _Repo(["code-a"])
    _wire(monkeypatch, repo)
    now = cur._now()
    repo.table[("code-a", "old")] = {"path": "src/A.cpp", "computed_at": now - timedelta(days=5)}
    repo.table[("code-a", "new")] = {"path": "src/B.cpp", "computed_at": now - timedelta(days=1)}
    done = await cur.refresh_namespace(repo, "o", "code-a", "m")
    assert (done.paths_due, done.paths_built) == (3, 3)
    built = {p for c in repo.calls if c[0] == "chunk_units" for p in c[2]}
    assert built == {"src/A.cpp", "src/C.cpp", "src/D.cpp"}
    # every row of the pass carries one version and one stamp
    rows = repo.rows("code_unit_risk")
    assert len({r["version"] for r in rows}) == 1 and len({r["computed_at"] for r in rows}) == 1


# ---- the per-round edge reads ---------------------------------------------------------


async def test_edge_reads_run_once_per_round_and_rows_equal_the_per_file_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _Repo(["code-a"])
    _wire(monkeypatch, repo)
    stamp = datetime(2026, 9, 16, 12, 0)
    rows = await cur.build_rows(
        repo,
        "o",
        "code-a",
        "m",
        PATHS,
        churn={"src/A.cpp": 12, "src/C.cpp": 7},
        tested={"src/A.cpp", "src/C.cpp"},
        computed_at=stamp,
        version=7,
    )
    kinds = [c[0] for c in repo.calls]
    assert kinds.count("callers") == 1 and kinds.count("callees") == 1  # not 2 per file
    assert kinds.count("chunk_units") == 1 and kinds.count("symbols") == 1
    assert [c[2] for c in repo.calls if c[0] == "callers"] == [["a1", "a2", "b1", "c1", "d1"]]
    # the reference: each file built alone, the per-file semantics
    ref: list[dict[str, Any]] = []
    for p in PATHS:
        ref += await cur.build_rows(
            repo,
            "o",
            "code-a",
            "m",
            [p],
            churn={"src/A.cpp": 12, "src/C.cpp": 7},
            tested={"src/A.cpp", "src/C.cpp"},
            computed_at=stamp,
            version=7,
        )
    key = lambda r: r["unit_id"]  # noqa: E731
    assert sorted(rows, key=key) == sorted(ref, key=key)
    assert len(rows) == 10  # 5 chunks + 5 function symbols
    by_id = {r["unit_id"]: r for r in rows}
    assert by_id["chA"]["amp"] == 2 and by_id["chC"]["amp"] == 3 and by_id["chB"]["amp"] == 1
    assert by_id["chA"]["tested"] == 1 and by_id["chB"]["tested"] == 0


def test_edge_rows_are_routed_to_every_file_defining_the_symbol() -> None:
    syms = [_sym("s", "src/A.h", 1, 2), _sym("s", "src/A.cpp", 1, 2), _sym("t", "src/B.cpp", 1, 2)]
    callers = [_edge("x", "s", "src/X.cpp", 1), _edge("y", "t", "src/X.cpp", 2)]
    callees = [_edge("s", "z", "src/Z.cpp", 1), _edge("q", "z", "src/Z.cpp", 1)]
    by_callers, by_callees = cur._edges_by_path(syms, callers, callees)
    assert set(by_callers) == {"src/A.h", "src/A.cpp", "src/B.cpp"}
    assert by_callers["src/A.h"] == by_callers["src/A.cpp"] == [callers[0]]
    assert by_callees == {"src/A.h": [callees[0]], "src/A.cpp": [callees[0]]}  # `q` owns no file


async def test_the_round_wide_edge_statement_keeps_the_per_target_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.app import risk as code_risk

    seen: list[dict[str, Any]] = []

    class _R:
        async def edges_for_symbols(
            self, org: str, ns: str, ids: list[str], **kw: Any
        ) -> list[dict[str, Any]]:
            seen.append({"ids": list(ids), **kw})
            return []

    ids = [f"s{i}" for i in range(1200)]
    await cur._edge_facts(_R(), "o", "n", ids)  # type: ignore[arg-type]
    assert len(seen) == 6  # 3 batches of <= 500 x 2 directions
    for call in seen:
        assert len(call["ids"]) <= 500
        cap = (
            code_risk.CALLERS_LIMIT
            if call["direction"] == "callers"
            else code_risk.CALLEES_PER_SOURCE_READ
        )
        assert call["per_match"] == cap and call["limit"] == cap * len(call["ids"])


# ---- cancellation -----------------------------------------------------------------------


async def test_a_cancelled_leg_issues_no_further_statements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _Repo(["code-a"])
    _wire(monkeypatch, repo)
    monkeypatch.setattr(cur, "BUILD_PATHS_PER_ROUND", 1)
    repo.gate = asyncio.Event()
    task = asyncio.create_task(cur.refresh_namespace(repo, "o", "code-a", "m"))
    await asyncio.wait_for(repo.in_flight.wait(), timeout=2)  # the first edge read is in flight
    frozen = repo.statements
    task.cancel()
    repo.gate.set()  # the in-flight statement returns — nothing must be scheduled after it
    with pytest.raises(asyncio.CancelledError):
        await task
    for _ in range(5):
        await asyncio.sleep(0)
    assert task.cancelled()
    assert repo.statements == frozen
    assert repo.rows("code_unit_risk") == []


async def test_a_pass_past_its_deadline_raises_timeout_and_freezes_the_statement_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _Repo(["code-a", "code-b"])
    _wire(monkeypatch, repo)
    monkeypatch.setattr(settings, "code_graph_stats_unit_risk_budget_s", 3600)
    repo.gate = asyncio.Event()

    async def _pass() -> None:
        async with asyncio.timeout(0.2):
            await cgs.run_pass()

    task = asyncio.create_task(_pass())
    await asyncio.wait_for(repo.in_flight.wait(), timeout=2)
    with pytest.raises(TimeoutError):
        await task
    frozen = repo.statements
    repo.gate.set()
    for _ in range(5):
        await asyncio.sleep(0.01)
    assert repo.statements == frozen
    # the cheap legs of both namespaces were written before the cut
    assert repo.rows("code_symbol_stats", "code-a") and repo.rows("code_symbol_stats", "code-b")


# ---- the supervisor and the registry --------------------------------------------------


def test_the_worker_carries_its_own_deadline_and_the_cap_stays_for_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "code_graph_stats_enabled", True)
    spec = next(w for w in enabled_workers() if w.name == "code_graph_stats")
    assert spec.deadline_seconds() == float(settings.code_graph_stats_max_pass_seconds) == 3600.0
    assert DEADLINE_CAP_SECONDS == 900.0
    assert default_max_pass_seconds(WEEK) == 900.0  # a weekly worker without its own is capped
    assert settings.code_graph_stats_unit_risk_budget_s == 2400
    assert settings.code_graph_stats_unit_risk_budget_s < settings.code_graph_stats_max_pass_seconds


def test_a_timed_out_or_long_interval_failure_holds_the_interval_others_keep_the_backoff() -> None:
    async def _noop() -> None:
        pass

    weekly = WorkerSpec("code_graph_stats", WEEK, _noop, max_pass_seconds=3600.0)
    ingest = WorkerSpec("code_ingest", 120, _noop, max_pass_seconds=900.0)
    quick = WorkerSpec("quick", 5, _noop)  # deadline 60 (the floor) > interval
    timeout, error = TimeoutError(), RuntimeError("x")
    # a pass cut at its deadline waits the interval, never less than the backoff
    assert sup.failure_hold_seconds(weekly, timeout, 1.0) == float(WEEK)
    assert sup.failure_hold_seconds(ingest, timeout, 1.0) == 120.0
    assert sup.failure_hold_seconds(quick, timeout, 1.0) == 5.0
    assert sup.failure_hold_seconds(quick, timeout, 8.0) == 8.0  # the backoff had grown past it
    # a worker whose interval is longer than its deadline waits the interval on any failure
    assert sup.failure_hold_seconds(weekly, error, 4.0) == float(WEEK)
    # a frequent worker's other failures keep the ordinary backoff
    assert sup.failure_hold_seconds(ingest, error, 4.0) == 4.0
    assert sup.failure_hold_seconds(quick, error, 1.0) == 1.0


async def test_the_supervisor_sleeps_the_hold_after_a_timeout_and_the_backoff_after_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.observability import events as ev_mod
    from server.observability import genai

    class _Em:
        def emit(self, e: Any) -> None:
            pass

    monkeypatch.setattr(ev_mod, "get_emitter", lambda: _Em())
    monkeypatch.setattr(
        sup,
        "failure_hold_seconds",
        lambda spec, exc, backoff: 123.0 if isinstance(exc, TimeoutError) else backoff,
    )
    slept: list[float] = []

    async def _slow() -> None:
        await asyncio.sleep(5)

    async def _boom() -> None:
        raise RuntimeError("x")

    def _one(fn: Any) -> sup.WorkerSupervisor:
        s = sup.WorkerSupervisor()

        async def _sleep(seconds: float) -> None:
            slept.append(seconds)
            s._stopping.set()

        s._sleep_or_drain = _sleep  # type: ignore[method-assign]
        return s

    for fn, expect in ((_slow, 123.0), (_boom, 1.0)):
        genai.set_identity()
        await asyncio.wait_for(
            _one(fn)._supervise(WorkerSpec("w", 1.0, fn, max_pass_seconds=0.05)), timeout=5
        )
        assert slept[-1] == expect
    genai.set_identity()


async def test_the_pass_row_carries_the_outcome_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.observability import cost_accumulator, genai
    from server.observability import events as ev_mod
    from server.workers.status import record_pass

    rows: list[Any] = []

    class _Em:
        def emit(self, e: Any) -> None:
            rows.append(e)

    monkeypatch.setattr(ev_mod, "get_emitter", lambda: _Em())
    get_registry().reset()

    async def _pass() -> None:
        # An idle, successful pass writes no ledger row; spend one embed
        # token so the row this test reads is written.
        cost_accumulator.report_embed("text-embedding-3-large", 1)
        with record_pass("w") as rec:
            rec.outcome = {
                "unit_risk_rows": 389_915,
                "unit_risk_partial": True,
                "unit_risk_resume": "code-clickhouse-26-5:src/A.cpp",
                "errors": 0,
                "nested": {"not": "a scalar"},
            }

    s = sup.WorkerSupervisor()

    async def _sleep(seconds: float) -> None:
        s._stopping.set()

    s._sleep_or_drain = _sleep  # type: ignore[method-assign]
    genai.set_identity()
    await asyncio.wait_for(s._supervise(WorkerSpec("w", 1.0, _pass)), timeout=5)
    genai.set_identity()
    (row,) = rows
    assert row.success is True
    assert row.args["outcome_unit_risk_rows"] == "389915"
    assert row.args["outcome_unit_risk_partial"] == "True"
    assert row.args["outcome_unit_risk_resume"] == "code-clickhouse-26-5:src/A.cpp"
    assert "outcome_nested" not in row.args and row.args["worker"] == "w"


# ---- progress for operators ---------------------------------------------------------


def test_index_status_output_defaults_keep_old_clients_whole() -> None:
    out = CodeIndexStatusOutput(repo="r", branch="b", namespace="n")
    assert out.unit_risk_rows == 0 and out.unit_risk_at == ""


class _StatusRepo:
    def __init__(self, answer: Any) -> None:
        self.answer, self.reads = answer, 0

    async def unit_risk_summary(self, org: str, ns: str) -> Any:
        self.reads += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


async def test_status_fields_read_the_risk_table_once_per_ten_minutes_and_fail_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index_status._UNIT_RISK_CACHE.clear()
    repo = _StatusRepo((389_915, datetime(2026, 9, 16, 9, 53, 28)))
    monkeypatch.setattr(index_status, "CodeRepo", lambda: repo)
    want = (389_915, "2026-09-16T09:53:28+00:00")
    assert await index_status.unit_risk_summary_cached("o", "code-x") == want
    assert await index_status.unit_risk_summary_cached("o", "code-x") == want
    assert repo.reads == 1
    monkeypatch.setattr(index_status, "CodeRepo", lambda: _StatusRepo((0, None)))
    assert await index_status.unit_risk_summary_cached("o", "code-empty") == (0, "")
    monkeypatch.setattr(index_status, "CodeRepo", lambda: _StatusRepo(RuntimeError("no table")))
    assert await index_status.unit_risk_summary_cached("o", "code-old") == (0, "")
    index_status._UNIT_RISK_CACHE.clear()


class _CH:
    """query_one answers the existence reads; query_all the published
    summaries, where the risk summary can be missing."""

    def __init__(self, *, risk_rows: bool) -> None:
        self.risk_rows = risk_rows

    async def query_one(self, sql: str, params: dict[str, Any]) -> dict[str, Any] | None:
        assert "code_unit_risk" not in sql and "code_symbol_stats" not in sql
        return {"present": 1}

    async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        assert "code_index_state" in sql
        rows = [
            {"state_key": "symbol_stats_summary", "value": '{"n": 5, "at": "2026-09-16T09:53:22"}'}
        ]
        if self.risk_rows:
            rows.append(
                {
                    "state_key": "unit_risk_summary",
                    "value": '{"n": 12, "at": "2026-09-16T09:53:28"}',
                }
            )
        return rows


async def test_graph_stats_state_shows_unit_risk_progress() -> None:
    from server.code.store.index import CodeRepo

    state = await CodeRepo(_CH(risk_rows=True)).graph_stats_state("o", "n")  # type: ignore[arg-type]
    assert state["unit_risk_rows"] == 12
    assert state["unit_risk_at"] == datetime(2026, 9, 16, 9, 53, 28)
    assert state["symbol_stats_rows"] == 5
    without = await CodeRepo(_CH(risk_rows=False)).graph_stats_state("o", "n")  # type: ignore[arg-type]
    assert (without["unit_risk_rows"], without["unit_risk_at"]) == (0, None)
    assert without["symbol_stats_rows"] == 5  # the first-pass check still has its answer
    # the due check ignores the new keys (measured against the fixture's own
    # day, not the wall clock)
    assert (
        cgs.pass_due(
            without,
            interval_seconds=WEEK,
            now=datetime(2026, 9, 16, 12, 0, tzinfo=UTC),
        )
        == ""
    )
