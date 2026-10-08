"""The risk annotation runs beside the legs, not after them.

The annotation's five reads fall in two rounds. `RiskPrefetch.start()` runs
the PATH round (churn, symbol rows) for the ranked pool's head from inside
search(); the handler's `annotate()` runs the UNIT round (tests, callers,
callees) for exactly the served units and attaches. Pinned here: the served
annotation is byte-identical to the sequential `annotate_units` (fields,
values, callee order) whether the served set is the head, a subset of it,
or a re-ranked slice; a served path the head did not carry is read at the
join and counted; a failed prefetch degrades to the sequential read; each
round awaits its reads together; search() starts the prefetch once, after
the in-memory priors; the handler discloses only when something was missed.
"""

from __future__ import annotations

import asyncio
import copy
from typing import Any

import pytest

from server.code.app import risk as code_risk
from server.code.app.risk import RiskPrefetch, annotate_units, reserve_for
from server.code.domain.units import CodeUnit
from server.settings import settings


@pytest.fixture(autouse=True)
def _precomputed_table_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests pin the live overlap reads; the precomputed risk table holds no
    row, so every unit is read live."""
    from server.code.app import unit_risk

    async def _no_rows(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(unit_risk, "read_rows", _no_rows)


# ---- a stub repo with several files, symbols, tests and edges ---------------


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


class _Repo:
    """Deterministic stub: every read filters its rows by the keys asked
    for, keeps the statement's order and honours `limit`, like the table."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any, int | None]] = []
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

    async def pr_churn_for(self, org, ns, paths, *, days=365):
        self.calls.append(("churn", sorted(paths), None))
        return {p: n for p, n in self.churn.items() if p in paths}

    async def tested_paths(self, org, ns, paths=None):
        self.calls.append(("tests", sorted(paths or []), None))
        return {t["src_path"] for t in self.tests if paths is None or t["src_path"] in paths}

    async def functions_for_paths(self, org, ns, paths, *, limit=400, **_kw):
        self.calls.append(("symbols", sorted(paths), limit))
        return [s for s in self.symbols if s["path"] in paths][:limit]

    async def edges_for_symbols(
        self, org, ns, ids, *, direction, edge_kinds=None, limit=200, per_match=None
    ):
        self.calls.append((direction, sorted(ids), limit))
        rows = self.callers if direction == "callers" else self.callees
        key = "to_symbol_id" if direction == "callers" else "from_symbol_id"
        return [r for r in rows if r[key] in ids][:limit]


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
        _unit("symB", "src/B.cpp", 1, 50, kind="symbol"),
        _unit("symB", "src/B.cpp", 1, 50, kind="symbol"),  # the head dedupes by id
    ]


def _risk(units: list[CodeUnit]) -> list[tuple[Any, ...]]:
    return [(u.unit_id, u.churn, u.amp, u.tested, u.callees) for u in units]


async def _sequential(units: list[CodeUnit], reserve: int) -> tuple[int, list[tuple[Any, ...]]]:
    seq = copy.deepcopy(units)
    spent = await annotate_units(_Repo(), "o", "n", seq, reserve_tokens=reserve)
    return spent, _risk(seq)


# ---- identity with the sequential read ---------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "served",
    [
        [0, 1, 2],  # the head as ranked
        [2, 0, 1],  # re-ranked after the hook: order preserved from the served list
        [1],  # a subset
        [0, 3],  # two units of one file, different symbols
        [5, 0],  # a symbol-kind unit
    ],
)
async def test_prefetched_annotation_is_byte_identical_to_the_sequential_one(
    served: list[int],
) -> None:
    pool = _pool()
    units = [copy.deepcopy(pool[i]) for i in served]
    reserve = reserve_for(3)
    want_spent, want = await _sequential(units, reserve)

    repo = _Repo()
    pf = RiskPrefetch(repo, "o", "n", top_k=3)
    pf.start(pool)  # head = 6 distinct candidates over 4 paths, more than the 3 served
    spent = await pf.annotate(units, reserve_tokens=reserve)
    assert _risk(units) == want
    assert spent == want_spent
    assert pf.missed == 0
    # the path round ran once, for the head's paths; the unit round for the served set
    kinds = [c[0] for c in repo.calls]
    assert kinds[:2] == ["churn", "symbols"] and sorted(kinds[2:]) == [
        "callees",
        "callers",
        "tests",
    ]
    assert repo.calls[0][1] == ["src/A.cpp", "src/B.cpp", "src/C.cpp", "src/D.cpp"]
    tests_call = next(c for c in repo.calls if c[0] == "tests")
    assert tests_call[1] == sorted({units[i].path for i in range(len(units))})


@pytest.mark.asyncio
async def test_a_tight_reserve_drops_callees_the_same_way() -> None:
    pool = _pool()
    units = [copy.deepcopy(pool[i]) for i in (0, 1, 2)]
    want_spent, want = await _sequential(units, 20)
    pf = RiskPrefetch(_Repo(), "o", "n", top_k=3)
    pf.start(pool)
    spent = await pf.annotate(units, reserve_tokens=20)
    assert _risk(units) == want and spent == want_spent
    assert units[2].callees == [] and units[0].churn == 12  # ints always land


@pytest.mark.asyncio
async def test_a_wide_head_reads_the_same_facts_as_the_served_set_would() -> None:
    """The head reads MORE symbol rows (four paths); filtered to the two
    served units' paths the attribution is what those two units' own read
    gives — the callee cap and the anonymous-name filter apply as before."""
    pool = _pool()
    units = [copy.deepcopy(pool[0]), copy.deepcopy(pool[1])]
    _, want = await _sequential(units, 400)
    pf = RiskPrefetch(_Repo(), "o", "n", top_k=1)  # head of 4 units, 3 paths
    pf.start(pool)
    await pf.annotate(units, reserve_tokens=400)
    assert _risk(units) == want
    assert units[0].amp == 2 and [c["name"] for c in units[0].callees] == [
        "DB::z1",
        "DB::z2",
        "DB::z3",
    ]
    assert units[1].amp == 3 and [c["name"] for c in units[1].callees] == ["DB::z9"]


@pytest.mark.asyncio
async def test_tested_is_every_served_path_a_mapped_test_reaches() -> None:
    """The test read is a DISTINCT over the served paths, not a flat top-30:
    a path with 40 mapped tests does not hide the other served path's one
    test, in the sequential and the prefetched annotation alike, and both
    read exactly the served paths."""
    repo = _Repo()
    repo.tests = [
        {"src_path": "src/A.cpp", "test_path": f"tests/a{i}", "confidence": 1.0} for i in range(40)
    ] + [{"src_path": "src/C.cpp", "test_path": "tests/c1", "confidence": 0.9}]
    pool = _pool()
    served = [copy.deepcopy(pool[0]), copy.deepcopy(pool[1])]
    seq = copy.deepcopy(served)
    await annotate_units(repo, "o", "n", seq, reserve_tokens=400)
    assert [u.tested for u in seq] == [True, True]
    pf = RiskPrefetch(repo, "o", "n", top_k=2)
    pf.start(pool)
    await pf.annotate(served, reserve_tokens=400)
    assert _risk(served) == _risk(seq)
    tests_calls = [c for c in repo.calls if c[0] == "tests"]
    assert all(c[1] == ["src/A.cpp", "src/C.cpp"] for c in tests_calls)


# ---- what the head did not carry ---------------------------------------------


@pytest.mark.asyncio
async def test_a_served_path_outside_the_head_is_read_at_the_join_and_counted() -> None:
    pool = _pool()
    repo = _Repo()
    pf = RiskPrefetch(repo, "o", "n", top_k=1)  # head = chA, chC, chB, chA2 -> paths A, C, B
    pf.start(pool)
    served = [copy.deepcopy(pool[0]), copy.deepcopy(pool[4])]  # D was not in the head
    _, want = await _sequential(served, 400)
    await pf.annotate(served, reserve_tokens=400)
    assert _risk(served) == want
    assert pf.missed == 1
    after = repo.calls[2:]  # the join: the missed path's round, then the unit round
    assert [c[0] for c in after[:2]] == ["churn", "symbols"] and after[0][1] == ["src/D.cpp"]
    assert sorted(c[0] for c in after[2:]) == ["callees", "callers", "tests"]
    assert next(c for c in after if c[0] == "callers")[1] == ["a1", "d1"]


@pytest.mark.asyncio
async def test_a_unit_on_a_covered_path_with_other_symbols_is_not_a_miss() -> None:
    """The path round reads a file's every function, so a served unit from
    a covered file is attributed from the head's rows even when no
    candidate carried its span (why the round does not filter by span)."""
    pool = _pool()
    repo = _Repo()
    pf = RiskPrefetch(repo, "o", "n", top_k=1)  # head's paths: A, C, B
    pf.start(pool[:2] + pool[4:])  # chA, chC, chD, symB: chA2's symbol a2 is in no candidate
    served = [copy.deepcopy(pool[3])]  # chA2: same file as chA, symbol a2 — the path round has it
    _, want = await _sequential(served, 400)
    await pf.annotate(served, reserve_tokens=400)
    assert _risk(served) == want and pf.missed == 0
    assert next(c for c in repo.calls if c[0] == "callers")[1] == ["a2"]


@pytest.mark.asyncio
async def test_a_failed_prefetch_degrades_to_the_sequential_read() -> None:
    class _Broken(_Repo):
        def __init__(self) -> None:
            super().__init__()
            self.first = True

        async def functions_for_paths(self, org, ns, paths, *, limit=400, **_kw):
            if self.first:  # the prefetch's read; the sequential one is fine
                self.first = False
                raise RuntimeError("boom")
            return await super().functions_for_paths(org, ns, paths, limit=limit)

    pool = _pool()
    pf = RiskPrefetch(_Broken(), "o", "n", top_k=2)
    pf.start(pool)
    served = [copy.deepcopy(pool[0])]
    _, want = await _sequential(served, 400)
    spent = await pf.annotate(served, reserve_tokens=400)
    assert _risk(served) == want and spent > 0 and pf.missed == 0


@pytest.mark.asyncio
async def test_never_started_is_the_sequential_read_with_nothing_counted() -> None:
    repo = _Repo()
    pf = RiskPrefetch(repo, "o", "n", top_k=2)
    assert not pf.started
    served = [copy.deepcopy(_pool()[0])]
    _, want = await _sequential(served, 400)
    await pf.annotate(served, reserve_tokens=400)
    assert _risk(served) == want and pf.missed == 0
    assert [c[0] for c in repo.calls[:2]] == ["churn", "symbols"]


def test_the_head_is_top_k_times_four_distinct_candidates() -> None:
    pf = RiskPrefetch(_Repo(), "o", "n", top_k=2)
    assert pf._top_n == 8
    assert code_risk.PREFETCH_PER_SLOT == 4


@pytest.mark.asyncio
async def test_start_takes_the_head_once() -> None:
    repo = _Repo()
    pf = RiskPrefetch(repo, "o", "n", top_k=1)
    pool = _pool()
    pf.start(pool[:2])
    pf.start(pool[3:])  # a second start is ignored
    await pf.annotate([copy.deepcopy(pool[0])], reserve_tokens=400)
    assert repo.calls[0] == ("churn", ["src/A.cpp", "src/C.cpp"], None)


# ---- each round awaits its reads together --------------------------------------


class _Gate(_Repo):
    """Reads in a group complete only once every read of the group has
    started: a sequential await of the group would time out here."""

    def __init__(self, groups: list[set[str]]) -> None:
        super().__init__()
        self.groups = groups
        self.started = {k: asyncio.Event() for g in groups for k in g}

    async def _wait(self, me: str) -> None:
        self.started[me].set()
        for g in self.groups:
            if me in g:
                for k in g:
                    await asyncio.wait_for(self.started[k].wait(), 1.0)

    async def pr_churn_for(self, org, ns, paths, *, days=365):
        await self._wait("churn")
        return await super().pr_churn_for(org, ns, paths, days=days)

    async def functions_for_paths(self, org, ns, paths, *, limit=400, **_kw):
        await self._wait("symbols")
        return await super().functions_for_paths(org, ns, paths, limit=limit)

    async def tested_paths(self, org, ns, paths=None):
        await self._wait("tests")
        return await super().tested_paths(org, ns, paths)

    async def edges_for_symbols(self, org, ns, ids, *, direction, **kw):
        await self._wait(direction)
        return await super().edges_for_symbols(org, ns, ids, direction=direction, **kw)


@pytest.mark.asyncio
async def test_the_path_round_and_the_unit_round_each_await_together() -> None:
    u = _pool()[0]
    repo = _Gate([{"churn", "symbols"}, {"tests", "callers", "callees"}])
    await annotate_units(repo, "o", "n", [u], reserve_tokens=400)
    assert u.churn == 12 and u.amp == 2 and u.tested is True
    assert [c["name"] for c in u.callees] == ["DB::z1", "DB::z2", "DB::z3"]


# ---- search() starts it after the in-memory priors; the handler discloses ---

_CHUNK = dict(
    scope="f",
    kind="function",
    start_line=1,
    end_line=5,
    content="int f();",
    symbol_ids=[],
    context_description="",
)


class _SearchRepo:
    """Two legs that agree on six chunks; every other read is empty."""

    async def code_corpus_stats(self, org, ns, tokens):
        return {t: 3 for t in tokens}, 100, 56.0

    async def lexical_search_chunks(self, *a, **k):
        return [
            {"chunk_id": f"c{i}", "lex_score": 40.0 - i, "path": f"src/F{i}.cpp", **_CHUNK}
            for i in range(6)
        ]

    async def vector_search_chunks(self, *a, **k):
        return [
            {"chunk_id": f"c{i}", "distance": 0.1 + i / 100, "path": f"src/F{i}.cpp", **_CHUNK}
            for i in range(6)
        ]

    async def symbols_fuzzy(self, *a, **k):
        return []

    async def unit_signals(self, *a, **k):
        return {}


class _Embedder:
    model_name = "fake-embed"

    async def embed(self, text: str) -> list[float]:
        return [0.1, 0.2, 0.3]


def _quiet(monkeypatch) -> None:
    monkeypatch.setattr(settings, "code_fusion_signals", False)
    monkeypatch.setattr(settings, "code_search_pr_leg", "off", raising=False)
    monkeypatch.setattr(settings, "code_search_history_route", "off", raising=False)


@pytest.mark.asyncio
async def test_search_starts_the_prefetch_once_on_the_ranked_pool(monkeypatch) -> None:
    from server.code.app import search as cs

    class _Spy:
        def __init__(self) -> None:
            self.starts: list[list[str]] = []

        def start(self, candidates: list[CodeUnit]) -> None:
            self.starts.append([u.unit_id for u in candidates])

    _quiet(monkeypatch)
    spy = _Spy()
    res = await cs.search(
        _SearchRepo(),
        "o",
        "n",
        query="where do we retry uploads",
        top_k=2,
        embedder=_Embedder(),
        risk_prefetch=spy,
    )
    assert res.units and not res.abstain, res
    assert len(spy.starts) == 1, "started once"
    # the ranked pool is offered in rank order; the served page is its head
    assert spy.starts[0][:2] == [u.unit_id for u in res.units]
    assert len(spy.starts[0]) == 6


@pytest.mark.asyncio
async def test_search_without_a_prefetch_is_unchanged(monkeypatch) -> None:
    from server.code.app import search as cs

    _quiet(monkeypatch)
    res = await cs.search(
        _SearchRepo(),
        "o",
        "n",
        query="where do we retry uploads",
        top_k=2,
        embedder=_Embedder(),
    )
    assert [u.unit_id for u in res.units] == ["c0", "c1"]
    assert all(u.churn == 0 and u.amp == 0 and u.callees == [] for u in res.units)


def test_handler_discloses_only_when_something_was_missed() -> None:
    import server.operations  # noqa: F401
    from server.code.app.search import handler as h

    class _Res:
        def __init__(self) -> None:
            self.units = [_pool()[0]]
            self.retrieval_work: dict[str, int] = {}

    class _PF:
        missed = 0
        source = ""  # "" while the precomputed table is off
        stale = 0

        async def annotate(self, units, *, reserve_tokens):
            return 7

    res = _Res()
    assert asyncio.run(h._annotate_served(res, _PF(), reserve=100)) == 7
    assert res.retrieval_work == {}
    pf = _PF()
    pf.missed = 2
    asyncio.run(h._annotate_served(res, pf, reserve=100))
    assert res.retrieval_work == {"risk_annotate_missed": 2}
