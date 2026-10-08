"""The risk block of a served unit is precomputed.

`loom.code_unit_risk` holds, per unit, what the five live risk reads
compute; code.search reads it in one keyed statement for the ranked
pool's head and keeps the live reads for units without a fresh row. Pinned
here: a built row equals the live annotation of its unit served alone; the
prefetch's attach from rows is byte-identical to the live attach (fields,
callee order, the reserve fit) and makes no path/edge read when every
served unit has a row; a missing unit, a stale row and a failed table read
each fall back to the live reads for exactly those units with the source
disclosed (table | live | mixed) and the count; the table off leaves the
payload byte-identical to the live reads; the ingest's refresh set is the changed
files plus the files their calls reach, capped, and never fatal; the weekly
pass writes every live file's units at one version; the fan-out merge keeps
a label when the namespaces agree.
"""

from __future__ import annotations

import asyncio
import copy
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from server.code.app import risk as code_risk
from server.code.app import unit_risk as code_unit_risk
from server.code.app.risk import RiskPrefetch, annotate_units, reserve_for
from server.code.domain.units import CodeUnit
from server.settings import settings

# ---- a stub repo: files, chunks, symbols, tests, edges, and the table ----------


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


def _edge(
    frm: str, to: str, path: str, line: int, tier: str = "scip", conf: float = 1.0
) -> dict[str, Any]:
    return {
        "from_symbol_id": frm,
        "to_symbol_id": to,
        "from_qualified_name": f"DB::{frm}",
        "to_qualified_name": f"DB::{to}",
        "path": path,
        "line": line,
        "tier": tier,
        "edge_kind": "calls",
        "confidence": conf,
    }


def _chunk(cid: str, path: str, s: int, e: int, kind: str = "function") -> dict[str, Any]:
    return {"chunk_id": cid, "path": path, "kind": kind, "start_line": s, "end_line": e}


class _Repo:
    """Every read filters by the keys asked for, keeps the statement's order
    and honours `limit`; `table` is loom.code_unit_risk, `inserted` what the
    writers sent."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.churn = {"src/A.cpp": 12, "src/B.cpp": 3, "src/C.cpp": 7, "src/D.cpp": 1}
        self.tests = [
            {"src_path": "src/A.cpp", "test_path": "tests/a1", "confidence": 1.0},
            {"src_path": "src/C.cpp", "test_path": "tests/c1", "confidence": 0.9},
            {"src_path": "src/A.cpp", "test_path": "tests/a2", "confidence": 0.8},
            {"src_path": "src/D.cpp", "test_path": "tests/d1", "confidence": 0.7},
        ]
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
            _chunk("chH", "src/A.cpp", 1, 5, "file_header"),
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
            _edge("a1", "z3", "src/Z.cpp", 81),
            _edge("a1", "z4", "src/Z.cpp", 90),
            _edge("b1", "z1", "src/Z.cpp", 77),
            _edge("c1", "$anonymous::q", "src/Q.cpp", 1),
            _edge("c1", "z9", "src/Z.cpp", 99),
            _edge("d1", "z1", "src/Z.cpp", 77),
        ]
        self.table: dict[str, dict[str, Any]] = {}
        self.table_error: Exception | None = None
        self.inserted: list[tuple[str, list[dict[str, Any]]]] = []

    # -- the live reads --
    async def pr_churn_for(self, org, ns, paths, *, days=365):
        self.calls.append(("churn", sorted(paths)))
        return {p: n for p, n in self.churn.items() if p in paths}

    async def pr_churn_ranked(self, org, ns, *, days=365, prefixes=(), exclude=(), limit=2000):
        self.calls.append(("churn_ranked", limit))
        return sorted(self.churn.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]

    async def test_map_for(self, org, ns, paths, *, limit=30):
        self.calls.append(("tests", sorted(paths)))
        return [t for t in self.tests if t["src_path"] in paths][:limit]

    async def functions_for_paths(self, org, ns, paths, *, limit=400, per_statement=20, **_kw):
        self.calls.append(("symbols", sorted(paths)))
        return [s for s in self.symbols if s["path"] in paths][:limit]

    async def edges_for_symbols(
        self, org, ns, ids, *, direction, edge_kinds=None, limit=200, per_match=None
    ):
        self.calls.append((direction, sorted(ids)))
        rows = self.callers if direction == "callers" else self.callees
        key = "to_symbol_id" if direction == "callers" else "from_symbol_id"
        return [r for r in rows if r[key] in ids][:limit]

    # -- the table and the builder's reads --
    async def unit_risk_rows(self, org, ns, unit_ids):
        self.calls.append(("table", sorted(unit_ids)))
        if self.table_error is not None:
            raise self.table_error
        return {u: dict(self.table[u]) for u in unit_ids if u in self.table}

    async def tested_paths(self, org, ns, paths=None):
        self.calls.append(("tested_paths", None if paths is None else sorted(paths)))
        rows = {t["src_path"] for t in self.tests}
        return rows if paths is None else {p for p in rows if p in paths}

    async def chunk_units_for_paths(self, org, ns, model, paths, *, per_statement=50):
        self.calls.append(("chunk_units", sorted(paths)))
        return [c for c in self.chunks if c["path"] in paths]

    async def symbols_by_ids(self, org, ns, ids):
        self.calls.append(("symbols_by_ids", sorted(ids)))
        return [s for s in self.symbols if s["symbol_id"] in ids]

    async def symbol_paths_by_ids(self, org, ns, ids):
        # The commit refresh's reached set, path-only
        self.calls.append(("symbol_paths", sorted(ids)))
        return {s["path"] for s in self.symbols if s["symbol_id"] in ids}

    async def live_file_hashes(self, org, ns):
        return {c["path"]: "h" for c in self.chunks}

    async def unit_risk_path_stamps(self, org, ns):
        # The weekly leg's resume read, from the rows written
        self.calls.append(("path_stamps", None))
        out: dict[str, Any] = {}
        for r in self.table.values():
            p = str(r["path"])
            if p not in out or r["computed_at"] > out[p]:
                out[p] = r["computed_at"]
        return out

    async def symbol_stats(self, org, ns, ids):
        self.calls.append(("stand_in", sorted(ids)))
        return {}

    async def insert_rows(self, table, rows):
        self.inserted.append((table, list(rows)))
        if table == "code_unit_risk":
            for r in rows:
                self.table[str(r["unit_id"])] = dict(r)


def _unit(uid: str, path: str, s: int, e: int, **kw: Any) -> CodeUnit:
    base = dict(unit_id=uid, path=path, start_line=s, end_line=e, symbol="", kind="function")
    base.update(kw)
    return CodeUnit(**base)


def _pool() -> list[CodeUnit]:
    return [
        _unit("chA", "src/A.cpp", 12, 35),
        _unit("chC", "src/C.cpp", 6, 50),
        _unit("chB", "src/B.cpp", 2, 40),
        _unit("chA2", "src/A.cpp", 101, 120),
        _unit("chD", "src/D.cpp", 1, 15),
        _unit("b1", "src/B.cpp", 1, 50, kind="symbol"),
    ]


def _risk(units: list[CodeUnit]) -> list[tuple[Any, ...]]:
    return [(u.unit_id, u.churn, u.amp, u.tested, u.callees) for u in units]


@pytest.fixture
def table_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "code_unit_risk_max_age_s", 10 * 24 * 3600)


async def _live(
    units: list[CodeUnit], reserve: int, monkeypatch: pytest.MonkeyPatch
) -> tuple[int, list[tuple[Any, ...]]]:
    """The live sequential read over an empty table — the reference."""
    seq = copy.deepcopy(units)
    spent = await annotate_units(_Repo(), "o", "n", seq, reserve_tokens=reserve)
    return spent, _risk(seq)


async def _filled_repo() -> _Repo:
    """A repo whose table holds the weekly pass's rows for every file."""
    repo = _Repo()
    await code_unit_risk.refresh_namespace(repo, "o", "n", "m")
    repo.calls.clear()
    return repo


# ---- the builder --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_built_row_equals_the_live_annotation_of_the_unit_served_alone(
    table_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = await code_unit_risk.build_rows(
        _Repo(), "o", "n", "m", ["src/A.cpp", "src/B.cpp", "src/C.cpp", "src/D.cpp"]
    )
    by_id = {r["unit_id"]: r for r in rows}
    # every live chunk and every function symbol of the files has a row
    assert set(by_id) == {"chA", "chA2", "chB", "chC", "chD", "chH", "a1", "a2", "b1", "c1", "d1"}
    for u in _pool():
        _, want = await _live([copy.deepcopy(u)], 10_000, monkeypatch)
        r = by_id[u.unit_id]
        got = (
            u.unit_id,
            r["churn"],
            r["amp"],
            bool(r["tested"]),
            code_unit_risk.parse_callees(r["callees"]),
        )
        assert got == want[0], u.unit_id
    # a file-header chunk overlapping no symbol: churn and tested only
    assert (by_id["chH"]["churn"], by_id["chH"]["amp"], by_id["chH"]["callees"]) == (12, 0, "[]")
    # one version and one stamp for the build; every persisted column present
    assert len({r["version"] for r in rows}) == 1 and len({r["computed_at"] for r in rows}) == 1
    assert set(rows[0]) == {
        "org",
        "namespace",
        "unit_id",
        "path",
        "churn",
        "amp",
        "tested",
        "callees",
        "computed_at",
        "version",
    }


def test_callees_json_is_canonical_and_round_trips() -> None:
    hop = [
        {"name": "DB::z1", "path": "src/Z.cpp", "line": 77},
        {"name": "DB::z2", "path": "src/Z.cpp", "line": 80},
    ]
    text = code_unit_risk.callees_json(hop)
    assert (
        text == '[{"line":77,"name":"DB::z1","path":"src/Z.cpp"},'
        '{"line":80,"name":"DB::z2","path":"src/Z.cpp"}]'
    )
    assert code_unit_risk.parse_callees(text) == hop  # the live attach's key order
    assert code_unit_risk.parse_callees("not json") == []
    assert code_unit_risk.parse_callees("[]") == []


def test_freshness_is_the_max_age_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "code_unit_risk_max_age_s", 3600)
    now = datetime(2026, 9, 16, 12, 0, 0)
    assert code_unit_risk.is_fresh(now - timedelta(minutes=59), now=now)
    assert code_unit_risk.is_fresh(now - timedelta(hours=1), now=now)
    assert not code_unit_risk.is_fresh(now - timedelta(hours=1, seconds=1), now=now)
    assert not code_unit_risk.is_fresh(None, now=now)
    # a tz-aware stamp (the file export) compares like the naive UTC the table returns
    aware = (now - timedelta(minutes=5)).replace(tzinfo=UTC)
    assert code_unit_risk.row_from({"computed_at": aware}).computed_at == now - timedelta(minutes=5)


# ---- the read path ----------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("served", [[0, 1, 2], [2, 0, 1], [1], [0, 3], [5, 0], [0, 1, 2, 3, 4, 5]])
async def test_prefetch_from_rows_is_byte_identical_and_makes_no_live_read(
    served: list[int],
    table_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _pool()
    units = [copy.deepcopy(pool[i]) for i in served]
    reserve = reserve_for(3)
    want_spent, want = await _live(units, reserve, monkeypatch)

    repo = await _filled_repo()
    pf = RiskPrefetch(repo, "o", "n", top_k=3)
    pf.start(pool)
    spent = await pf.annotate(units, reserve_tokens=reserve)
    assert _risk(units) == want
    assert spent == want_spent
    assert (pf.source, pf.stale, pf.missed) == ("table", 0, 0)
    # one keyed read for the head; no path round, no unit round
    kinds = [c[0] for c in repo.calls]
    assert kinds == ["table"]
    assert repo.calls[0][1] == sorted(u.unit_id for u in pool)


@pytest.mark.asyncio
async def test_reserve_fit_drops_callees_tail_first_from_rows_too(
    table_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    units = [copy.deepcopy(u) for u in _pool()[:3]]
    tight = 40  # ints for three units fit, not their callees
    want_spent, want = await _live(units, tight, monkeypatch)
    repo = await _filled_repo()
    pf = RiskPrefetch(repo, "o", "n", top_k=3)
    pf.start(_pool())
    spent = await pf.annotate(units, reserve_tokens=tight)
    assert _risk(units) == want and spent == want_spent
    assert units[-1].callees == [] and units[0].amp == 2


@pytest.mark.asyncio
async def test_a_unit_without_a_row_is_read_live_and_the_source_is_mixed(
    table_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _pool()
    units = [copy.deepcopy(pool[i]) for i in (0, 1, 2)]
    reserve = reserve_for(3)
    want_spent, want = await _live(units, reserve, monkeypatch)
    repo = await _filled_repo()
    del repo.table["chC"]  # an edited unit: a new chunk id, no row yet
    pf = RiskPrefetch(repo, "o", "n", top_k=3)
    pf.start(pool)
    spent = await pf.annotate(units, reserve_tokens=reserve)
    assert _risk(units) == want and spent == want_spent
    assert (pf.source, pf.stale, pf.missed) == ("mixed", 1, 0)
    # the path round ran at the hook for the missing head unit's file only,
    # the unit round at the join for that unit only
    by_kind = {c[0]: c[1] for c in repo.calls}
    assert by_kind["churn"] == ["src/C.cpp"] and by_kind["symbols"] == ["src/C.cpp"]
    assert by_kind["tested_paths"] == ["src/C.cpp"] and by_kind["callers"] == ["c1"]


@pytest.mark.asyncio
async def test_a_stale_row_reads_live(table_on: None, monkeypatch: pytest.MonkeyPatch) -> None:
    units = [copy.deepcopy(_pool()[0])]
    reserve = reserve_for(1)
    want_spent, want = await _live(units, reserve, monkeypatch)
    repo = await _filled_repo()
    repo.table["chA"]["computed_at"] = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=11)
    repo.table["chA"]["amp"] = 999  # a stale number must never reach the wire
    pf = RiskPrefetch(repo, "o", "n", top_k=1)
    pf.start(_pool())
    spent = await pf.annotate(units, reserve_tokens=reserve)
    assert _risk(units) == want and spent == want_spent and units[0].amp == 2
    assert (pf.source, pf.stale) == ("live", 1)


@pytest.mark.asyncio
async def test_a_failed_table_read_degrades_to_the_live_reads(
    table_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    units = [copy.deepcopy(u) for u in _pool()[:2]]
    reserve = reserve_for(2)
    want_spent, want = await _live(units, reserve, monkeypatch)
    repo = await _filled_repo()
    repo.table_error = RuntimeError("table not there yet")
    pf = RiskPrefetch(repo, "o", "n", top_k=2)
    pf.start(_pool())
    spent = await pf.annotate(units, reserve_tokens=reserve)
    assert _risk(units) == want and spent == want_spent
    assert (pf.source, pf.stale) == ("live", 2)
    kinds = [c[0] for c in repo.calls]
    assert kinds[:3] == ["table", "churn", "symbols"]


@pytest.mark.asyncio
async def test_a_served_unit_outside_the_head_is_still_a_miss_only_when_read_live(
    table_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _pool()
    repo = await _filled_repo()
    # served: a unit the head did not carry, WITH a row -> from the table, no miss
    lifted = _unit("chD", "src/D.cpp", 1, 15)
    pf = RiskPrefetch(repo, "o", "n", top_k=1)
    pf.start(pool[:1])
    await pf.annotate([copy.deepcopy(pool[0]), lifted], reserve_tokens=reserve_for(2))
    assert (pf.source, pf.missed) == ("table", 0)
    # the same unit without a row -> read at the join and counted
    del repo.table["chD"]
    pf = RiskPrefetch(repo, "o", "n", top_k=1)
    pf.start(pool[:1])
    units = [copy.deepcopy(pool[0]), copy.deepcopy(lifted)]
    await pf.annotate(units, reserve_tokens=reserve_for(2))
    assert (pf.source, pf.stale, pf.missed) == ("mixed", 1, 1)
    assert (units[1].churn, units[1].amp, units[1].tested) == (1, 0, True)


@pytest.mark.asyncio
async def test_sequential_annotate_units_reads_the_table_first(
    table_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    units = [copy.deepcopy(u) for u in _pool()[:3]]
    reserve = reserve_for(3)
    want_spent, want = await _live(units, reserve, monkeypatch)
    repo = await _filled_repo()
    del repo.table["chB"]
    spent = await annotate_units(repo, "o", "n", units, reserve_tokens=reserve)
    assert _risk(units) == want and spent == want_spent
    by_kind = {c[0]: c[1] for c in repo.calls}
    assert by_kind["table"] == ["chA", "chB", "chC"]
    assert by_kind["churn"] == ["src/B.cpp"] and by_kind["callers"] == ["b1"]


def test_handler_discloses_the_source_only_when_the_table_is_on() -> None:
    import server.operations  # noqa: F401
    from server.code.app.search import handler as h

    class _Res:
        def __init__(self) -> None:
            self.units = [_pool()[0]]
            self.retrieval_work: dict[str, Any] = {}

    class _PF:
        missed, source, stale = 0, "", 0

        async def annotate(self, units, *, reserve_tokens):
            return 7

    res = _Res()
    asyncio.run(h._annotate_served(res, _PF(), reserve=100))
    assert res.retrieval_work == {}  # table off: the live reads' payload
    pf = _PF()
    pf.source, pf.stale = "mixed", 2
    asyncio.run(h._annotate_served(res, pf, reserve=100))
    assert res.retrieval_work == {"risk_source": "mixed", "risk_stale_units": 2}
    from server.code.api.models_search import CodeSearchOutput

    out = CodeSearchOutput(
        results=[], route="hybrid", retrieval_work={"risk_source": "table", "stage_ms_total": 3}
    )
    assert out.retrieval_work["risk_source"] == "table"


# ---- the writes ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_commit_paths_are_the_changed_files_plus_the_files_their_calls_reach() -> None:
    repo = _Repo()
    paths = await code_unit_risk.commit_paths(
        repo, "o", "n", ["src/X.cpp", "src/A.cpp"], ["a1", "c1", "d1", "b1"], max_callee_paths=2
    )
    # changed first in their order; reached files sorted, minus the changed, capped
    assert paths == ["src/X.cpp", "src/A.cpp", "src/B.cpp", "src/C.cpp"]


@pytest.mark.asyncio
async def test_commit_paths_reach_every_file_defining_a_called_symbol() -> None:
    """A symbol id is (qualified name, signature), so a
    header's declaration and the source's definition share one; both files'
    units carry it and both are reached, from one path-only read."""
    repo = _Repo()
    repo.symbols.append(_sym("a1", "src/A.h", 3, 3))
    paths = await code_unit_risk.commit_paths(repo, "o", "n", ["src/X.cpp"], ["a1", "", "a1"])
    assert paths == ["src/X.cpp", "src/A.cpp", "src/A.h"]
    assert repo.calls == [("symbol_paths", ["a1"])]
    assert await code_unit_risk.commit_paths(repo, "o", "n", ["src/X.cpp"], []) == ["src/X.cpp"]
    assert len(repo.calls) == 1  # no ids, no read


@pytest.mark.asyncio
async def test_refresh_for_commit_writes_rows_for_the_reached_files(table_on: None) -> None:
    repo = _Repo()
    n = await code_unit_risk.refresh_for_commit(repo, "o", "n", "m", ["src/B.cpp"], ["a1"])
    written = {r["unit_id"] for _, rows in repo.inserted for r in rows}
    assert written == {"chB", "b1", "chA", "chA2", "chH", "a1", "a2"} and n == 7
    # one round: the round-wide reads ran once each over the commit's set
    kinds = [c[0] for c in repo.calls]
    assert kinds.count("chunk_units") == kinds.count("symbols") == kinds.count("callers") == 1
    assert kinds.count("churn") == kinds.count("tested_paths") == 1


@pytest.mark.asyncio
async def test_a_large_commit_is_built_in_the_weekly_rounds_at_one_version(
    table_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A set past `BUILD_PATHS_PER_ROUND` goes through the weekly pass's rounds
    (the symbol read of one `build_rows` call stops at 200 paths); every row
    shares the commit's version and stamp."""
    batches: list[tuple[list[str], Any, Any]] = []

    async def fake_build(
        repo: Any, org: str, ns: str, model: str, paths: list[str], **kw: Any
    ) -> list[dict[str, Any]]:
        batches.append((list(paths), kw.get("computed_at"), kw.get("version")))
        return [{"unit_id": p, "path": p} for p in paths]

    monkeypatch.setattr(code_unit_risk, "build_rows", fake_build)
    repo = _Repo()
    changed = [f"src/F{i:03d}.cpp" for i in range(450)]
    n = await code_unit_risk.refresh_for_commit(repo, "o", "n", "m", changed, [])
    assert n == 450 and [len(b[0]) for b in batches] == [200, 200, 50]
    assert [p for b in batches for p in b[0]] == changed
    assert len({(b[1], b[2]) for b in batches}) == 1 and batches[0][1] is not None
    assert sum(len(rows) for _, rows in repo.inserted) == 450


@pytest.mark.asyncio
async def test_ingest_hook_counts_rows_and_never_fails_the_commit(
    table_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.app.index import pass_budget as ingest_budget
    from server.code.app.index import pass_commit as ingest_commit

    class _Stats:
        def __init__(self, ids: list[str]) -> None:
            self.callee_symbol_ids = ids

    result = ingest_budget.CodeIngestResult()
    repo = _Repo()
    await ingest_commit._refresh_unit_risk(
        repo, "o", "n", "m", "abc123def456", ["src/B.cpp"], [_Stats(["a1"]), object()], result
    )
    assert result.unit_risk_rows == 7 and result.unit_risk_failed == 0

    async def boom(*a: Any, **k: Any) -> int:
        raise RuntimeError("edges read timed out")

    monkeypatch.setattr(code_unit_risk, "refresh_for_commit", boom)
    await ingest_commit._refresh_unit_risk(
        repo, "o", "n", "m", "abc123def456", ["src/B.cpp"], [], result
    )
    assert result.unit_risk_failed == 1 and result.errors == [
        "abc123def456 unit_risk: RuntimeError: edges read timed out"
    ]


@pytest.mark.asyncio
async def test_ingest_stats_carry_the_calls_targets_and_the_retired_edges() -> None:
    from server.code.app.index.ingest_stats import FileIngestStats

    stats = FileIngestStats(path="src/X.cpp")
    assert stats.callee_symbol_ids == []


@pytest.mark.asyncio
async def test_weekly_pass_writes_every_live_units_row_at_one_version(table_on: None) -> None:
    from server.code.workers import graph_stats as g

    repo = _Repo()
    result = g.CodeGraphStatsResult()
    await g._write_unit_risk(repo, "o", "n", "m", result)
    rows = [r for t, rs in repo.inserted for r in rs if t == "code_unit_risk"]
    assert result.unit_risk_rows == len(rows) == 11
    assert len({r["version"] for r in rows}) == 1 and len({r["computed_at"] for r in rows}) == 1
    # churn and the tested set were read once for the namespace, not per file
    kinds = [c[0] for c in repo.calls]
    assert kinds.count("churn_ranked") == 1 and kinds.count("tested_paths") == 1
    assert "churn" not in kinds and "tests" not in kinds


# ---- the fan-out merge and the settings -------------------------------------------------


def test_fanout_merge_keeps_a_label_the_namespaces_agree_on() -> None:
    from server.code.api.models_search import CodeSearchOutput
    from server.code.app.search_fanout import _merge_work

    def out(**work: Any) -> tuple[Any, int]:
        return CodeSearchOutput(results=[], route="hybrid", retrieval_work=work), 1

    same = {
        "a": out(risk_source="table", risk_stale_units=0, stage_ms_total=5),
        "b": out(risk_source="table", risk_stale_units=1, stage_ms_total=9),
    }
    assert _merge_work(same, ["a", "b"], []) == {
        "risk_source": "table",
        "risk_stale_units": 1,
        "stage_ms_total": 9,
        "fanout_namespaces": 2,
        "fanout_dropped": 0,
    }
    differ = {"a": out(risk_source="table"), "b": out(risk_source="live")}
    assert _merge_work(differ, ["a", "b"], [])["risk_source"] == "mixed"


def test_settings_defaults() -> None:
    from server.settings import Settings

    s = Settings()
    assert s.code_unit_risk_max_age_s == 10 * 24 * 3600
    assert code_unit_risk.COMMIT_CALLEE_PATHS_MAX == 200
    assert code_risk.CALLERS_LIMIT == 1000  # the builder's per-target cap is the live flat limit
