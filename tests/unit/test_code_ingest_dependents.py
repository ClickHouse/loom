"""A changed file re-resolves the files that name it.

CH-free. The derivation of a commit's dependents set, the closure repair of
declined sites, the per-commit cap + pending queue, the edges-only ingest
path and the worker/reconcile wiring are each driven through stub repos.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import patch

import pytest

from server.code.app.index import dependents as d
from server.code.app.index import ingest, ingest_parse
from server.settings import settings
from tests.unit.code_fixtures import repo_config

SHA = "c" * 40


def _miss(
    name: str,
    path: str,
    line: int = 5,
    *,
    call_text: str = "",
    disposition: str = "unresolved",
    frm: str = "from1",
) -> dict[str, Any]:
    return {
        "callee_name": name,
        "path": path,
        "line": line,
        "from_symbol_id": frm,
        "from_qualified_name": f"{path}::caller",
        "call_text": call_text or name,
        "disposition": disposition,
        "candidates": 0,
        "indexed_commit": "a" * 40,
        "created_at": None,
        "max_version": 1,
        "live_tombstoned_at": None,
        "org": "o",
        "namespace": "n",
    }


def _edge(path: str, to: str, conf: float = 0.6, kind: str = "calls") -> dict[str, Any]:
    return {
        "tier": "treesitter",
        "edge_kind": kind,
        "to_symbol_id": to,
        "from_symbol_id": "f",
        "path": path,
        "line": 3,
        "to_qualified_name": "q",
        "confidence": conf,
    }


class _Stats:
    def __init__(self, added=(), removed=(), ids=()):
        self.symbol_names_added = list(added)
        self.symbol_names_removed = list(removed)
        self.symbols_tombstoned_ids = list(ids)


# ---- pure derivation --------------------------------------------------------


def test_commit_delta_gathers_names_and_ids_and_tolerates_none() -> None:
    delta = d.CommitDelta.from_stats(
        [_Stats(added=["foo", "", "bar"]), None, _Stats(removed=["baz"], ids=["s1", ""])],
        {"src/a.cpp"},
    )
    assert delta.names_added == {"foo", "bar"} and delta.names_removed == {"baz"}
    assert delta.symbols_tombstoned == {"s1"} and delta.touched_paths == {"src/a.cpp"}
    assert not delta.empty and d.CommitDelta(touched_paths={"x"}).empty


def test_plan_closure_repair_applies_rule_three_exactly() -> None:
    table = {
        "foo": [("idF", "DB::foo", "function")],
        "bar": [("idB1", "A::bar", "function"), ("idB2", "B::bar", "function")],
        "qux": [("idQ1", "X::qux", "function"), ("idQ2", "Y::qux", "function")],
    }
    misses = [
        _miss("foo", "src/1.cpp"),  # one def -> binds
        _miss("bar", "src/2.cpp"),  # two defs -> stays declined
        _miss("qux", "src/3.cpp", call_text="X::qux"),  # qualification pins one
        _miss("foo", "src/4.cpp", disposition="ambiguous"),  # not unresolved: untouched
        _miss("zap", "src/5.cpp"),  # not added this commit
    ]
    resolvable, targets, left = d.plan_closure_repair(misses, table, {"foo", "bar", "qux"})
    assert [(m["path"], t["symbol_id"]) for m, t in zip(resolvable, targets, strict=True)] == [
        ("src/1.cpp", "idF"),
        ("src/3.cpp", "idQ1"),
    ]
    assert left == 1  # bar


def test_derive_dependents_unions_sources_keeps_touched_paths_and_sorts() -> None:
    """A touched path whose own live miss or edge depends on the
    delta is a dependent (same-commit ordering — the caller was derived
    before its definer landed)."""
    delta = d.CommitDelta(touched_paths={"src/t.cpp", "src/i.cpp"})
    paths, by = d.derive_dependents(
        delta,
        miss_paths=["src/z.cpp", "src/t.cpp", "src/a.cpp"],
        tombstoned_edge_paths=["src/m.cpp", "src/a.cpp"],
        ambiguous_edge_paths=["src/q.cpp"],
    )
    assert paths == ["src/a.cpp", "src/m.cpp", "src/q.cpp", "src/t.cpp", "src/z.cpp"]
    assert by == {"a_declined_sites": 3, "b_tombstoned_targets": 2, "d_now_ambiguous": 1}
    assert d.split_by_cap(paths, 3) == (paths[:3], paths[3:])
    assert d.split_by_cap(paths, 0) == ([], paths)


# ---- per-commit run against a stub repo --------------------------------------


class _Repo:
    def __init__(self, *, misses=None, table=None, edges_to=None):
        self.misses = misses or []
        self.table = table or {}
        self.edges_to = edges_to or {}
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []
        self.state: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[Any, ...]] = []

    async def live_call_misses_for_names(self, org, ns, names, *, limit=2000):
        self.calls.append(("misses", list(names), limit))
        return [m for m in self.misses if m["callee_name"] in names][:limit]

    async def name_candidates(self, org, ns, names):
        self.calls.append(("names", list(names)))
        return {n: self.table[n] for n in names if n in self.table}

    async def live_edges_to_symbols(
        self, org, ns, ids, *, tier="treesitter", edge_kinds=None, max_confidence=None, limit=5000
    ):
        self.calls.append(("edges_to", list(ids), tuple(edge_kinds or ()), max_confidence))
        return [
            r
            for i in ids
            for r in self.edges_to.get(i, [])
            if (max_confidence is None or r["confidence"] <= max_confidence)
            and (edge_kinds is None or r["edge_kind"] in edge_kinds)
        ]

    async def insert_rows(self, table, rows):
        self.inserts.append((table, rows))

    async def all_state(self, org, ns):
        return self.state

    async def set_state(self, org, ns, key, value, detail=""):
        self.state[key] = {"value": value, "detail": detail}

    def rows(self, table: str) -> list[dict[str, Any]]:
        return [r for t, rows in self.inserts if t == table for r in rows]


def _fakes(missing: set[str] | None = None):
    seen: list[tuple[str, dict[str, Any]]] = []

    async def show_file(git_dir, sha, path, max_bytes=0):
        return None if path in (missing or set()) else b"int x;"

    class _S:
        skipped = False
        edges_new, edges_tombstoned, misses_new, misses_tombstoned = 2, 1, 0, 1

    async def ingest_file(repo, org, ns, *, path, source, commit_sha, **kw):
        seen.append((path, kw))
        if path == "src/boom.cpp":
            raise ValueError("parse")
        return _S()

    return seen, show_file, ingest_file


def _run(repo, delta, **over):
    seen, show_file, ingest_file = _fakes(over.pop("missing", None))
    kw = dict(
        delta=delta,
        commit_sha=SHA,
        git_dir="/git",
        show_file=show_file,
        ingest_file=ingest_file,
        embedding_model="m",
        dependents_enabled=True,
        closure_enabled=True,
        max_files=200,
        pending_max=5000,
        closure_max_names=500,
        closure_max_sites=2000,
    )
    kw.update(over)
    return asyncio.run(d.run_for_commit(repo, "o", "n", **kw)), seen


def test_disabled_or_empty_delta_does_nothing() -> None:
    repo = _Repo()
    (c, deps), seen = _run(repo, d.CommitDelta(touched_paths={"a"}))
    assert (c, deps, seen, repo.calls) == (None, None, [], [])
    (c, deps), seen = _run(
        repo, d.CommitDelta(names_added={"foo"}), dependents_enabled=False, closure_enabled=False
    )
    assert (c, deps, seen, repo.calls) == (None, None, [], [])


def test_closure_repair_binds_unique_names_and_batches_one_insert_per_table() -> None:
    repo = _Repo(
        misses=[
            _miss("foo", "src/1.cpp", 7),
            _miss("foo", "src/2.cpp", 9),
            _miss("bar", "src/3.cpp"),
        ],
        table={
            "foo": [("idF", "DB::foo", "function")],
            "bar": [("b1", "A::bar", "function"), ("b2", "B::bar", "function")],
        },
    )
    delta = d.CommitDelta(touched_paths={"src/def.cpp"}, names_added={"foo", "bar"})
    (closure, deps), seen = _run(repo, delta)

    assert closure.names == 2 and closure.names_unique == 1 and closure.sites_read == 3
    assert closure.resolved == 2 and closure.left_declined == 1
    edges = repo.rows("code_edges")
    assert [
        (e["path"], e["line"], e["to_symbol_id"], e["confidence"], e["indexed_commit"])
        for e in edges
    ] == [("src/1.cpp", 7, "idF", 0.6, SHA), ("src/2.cpp", 9, "idF", 0.6, SHA)]
    assert all(e["tier"] == "treesitter" and e["edge_kind"] == "calls" for e in edges)
    tombs = repo.rows("code_call_sites_unresolved")
    assert [(t["path"], t["line"]) for t in tombs] == [("src/1.cpp", 7), ("src/2.cpp", 9)]
    assert all(t["tombstoned_at"] is not None and t["callee_name"] == "foo" for t in tombs)
    # exactly one insert per table for the whole commit
    assert [t for t, _ in repo.inserts] == ["code_edges", "code_call_sites_unresolved"]

    # the bound sites are NOT re-extracted; bar's declined site (now
    # ambiguous, count stale) and bar's two now-ambiguous targets are
    assert sorted(p for p, _ in seen) == ["src/3.cpp"]
    assert deps.by_source["a_declined_sites"] == 1 and deps.reingested == 1
    assert all(kw["edges_only"] is True and kw["embedder"] is None for _, kw in seen)
    assert deps.edges_new == 2 and deps.edges_tombstoned == 1 and deps.misses_tombstoned == 1


def test_dependents_follow_tombstoned_targets_and_ambiguous_names() -> None:
    repo = _Repo(
        table={"dup": [("d1", "A::dup", "function"), ("d2", "B::dup", "function")]},
        edges_to={
            "gone": [
                _edge("src/caller.cpp", "gone", 0.6),
                _edge("src/def.cpp", "gone", 1.0),
                _edge("src/kid.cpp", "gone", 0.9, kind="inherits"),
            ],
            "d1": [_edge("src/amb.cpp", "d1", 0.6), _edge("src/local.cpp", "d1", 1.0)],
        },
    )
    delta = d.CommitDelta(
        touched_paths={"src/def.cpp", "src/Common/h.h"},
        names_added={"dup"},
        symbols_tombstoned={"gone"},
    )
    (closure, deps), seen = _run(repo, delta, closure_enabled=False)
    assert closure is None
    # src/def.cpp is touched AND holds a live edge into the id it tombstoned:
    # re-derived in the same commit
    assert deps.by_source == {
        "a_declined_sites": 0,
        "b_tombstoned_targets": 3,
        "d_now_ambiguous": 1,
    }
    assert sorted(p for p, _ in seen) == [
        "src/amb.cpp",
        "src/caller.cpp",
        "src/def.cpp",
        "src/kid.cpp",
    ]
    # (b) any grade, every dependent edge kind; (d) only the repo-wide grade
    kinds = {c[2]: c[3] for c in repo.calls if c[0] == "edges_to"}
    assert kinds[("calls", "inherits", "fixture_use")] is None and kinds[("calls",)] == 0.65
    assert not repo.state, "nothing queued under the cap"


def test_cap_queues_overflow_merges_and_bounds_the_pending_queue() -> None:
    misses = [_miss("foo", f"src/{i:02d}.cpp") for i in range(6)]
    repo = _Repo(misses=misses, table={})
    repo.state[d.PENDING_KEY] = {
        "value": "1",
        "detail": json.dumps({"paths": ["src/zz.cpp"], "dropped_total": 2}),
    }
    delta = d.CommitDelta(touched_paths=set(), names_removed={"foo"})
    (closure, deps), seen = _run(repo, delta, max_files=2, pending_max=4, closure_enabled=False)
    assert [p for p, _ in seen] == ["src/00.cpp", "src/01.cpp"], "deterministic head"
    assert deps.candidates == 6 and deps.reingested == 2
    detail = json.loads(repo.state[d.PENDING_KEY]["detail"])
    assert detail["paths"] == ["src/02.cpp", "src/03.cpp", "src/04.cpp", "src/05.cpp"]
    assert repo.state[d.PENDING_KEY]["value"] == "4"
    assert deps.queued == 4 and deps.pending_total == 4 and deps.dropped == 1
    assert detail["dropped_total"] == 3 and detail["head"] == SHA[:12]


def test_reextract_skips_missing_paths_and_counts_failures() -> None:
    repo = _Repo()
    seen, show_file, ingest_file = _fakes(missing={"src/gone.cpp"})
    res = asyncio.run(
        d.reextract_paths(
            repo,
            "o",
            "n",
            ["src/a.cpp", "src/gone.cpp", "src/boom.cpp"],
            commit_sha=SHA,
            git_dir="/g",
            show_file=show_file,
            ingest_file=ingest_file,
            embedding_model="m",
        )
    )
    assert (res.reingested, res.skipped, res.errors) == (1, 1, 1)


def test_drain_pending_respects_budget_and_writes_the_rest_back() -> None:
    repo = _Repo()
    repo.state[d.PENDING_KEY] = {
        "value": "3",
        "detail": json.dumps(
            {"paths": ["src/a.cpp", "src/b.cpp", "src/c.cpp"], "dropped_total": 1}
        ),
    }
    seen, show_file, ingest_file = _fakes()
    res = asyncio.run(
        d.drain_pending(
            repo,
            "o",
            "n",
            head=SHA,
            git_dir="/g",
            budget=2,
            show_file=show_file,
            ingest_file=ingest_file,
            embedding_model="m",
        )
    )
    assert [p for p, _ in seen] == ["src/a.cpp", "src/b.cpp"]
    assert (res.files, res.remaining) == (2, 1)
    detail = json.loads(repo.state[d.PENDING_KEY]["detail"])
    assert detail["paths"] == ["src/c.cpp"] and detail["dropped_total"] == 1

    res0 = asyncio.run(
        d.drain_pending(
            repo,
            "o",
            "n",
            head=SHA,
            git_dir="/g",
            budget=0,
            show_file=show_file,
            ingest_file=ingest_file,
            embedding_model="m",
        )
    )
    assert res0.files == 0 and res0.remaining == 1


# ---- ingest_file(edges_only=True) ------------------------------------------------


class _IngestRepo:
    def __init__(self, existing_edges=None, table=None):
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []
        self.reads: list[str] = []
        self.existing_edges = existing_edges or []
        self.table = table or {}

    async def live_chunk_index_for_path(self, *_):
        self.reads.append("chunks")
        return []

    async def chunks_full_by_ids(self, *_):
        return []

    async def live_symbols_for_path(self, *_):
        self.reads.append("symbols")
        return []

    async def live_edges_for_path(self, *_):
        self.reads.append("edges")
        return list(self.existing_edges)

    async def live_call_misses_for_path(self, *_):
        self.reads.append("misses")
        return []

    async def name_candidates(self, org, ns, names):
        return {n: self.table[n] for n in names if n in self.table}

    async def insert_rows(self, table, rows):
        self.inserts.append((table, rows))


_SRC = b"int f() { return 1; }\nint g() { return f() + h(); }\n"


@pytest.mark.asyncio
async def test_edges_only_touches_edges_and_misses_but_never_chunks_symbols_or_the_file_row() -> (
    None
):
    repo = _IngestRepo(table={"h": [("idH", "DB::h", "function")]})
    stats = await ingest.ingest_file(
        repo,
        "o",
        "n",
        path="src/a.cpp",
        source=_SRC,
        commit_sha=SHA,
        embedding_model="m",
        embedder=None,
        edges_only=True,
    )
    assert stats.edges_only and not stats.skipped
    assert set(repo.reads) == {"edges", "misses"}
    assert sorted(t for t, _ in repo.inserts) == ["code_call_sites_unresolved", "code_edges"]
    edges = [r for t, rows in repo.inserts if t == "code_edges" for r in rows]
    assert {(e["to_qualified_name"], e["confidence"]) for e in edges} == {
        ("f", 1.0),
        ("DB::h", 0.6),
    }
    assert stats.edges_new == 2 and stats.misses_new == 0


@pytest.mark.asyncio
async def test_edges_only_keeps_unchanged_edges_and_retires_the_stale_miss() -> None:
    """The dependents case itself: the site for `h` was declined at the last
    full ingest; `h` now has one definition. Re-extraction adds the edge and
    tombstones nothing else — `f`'s edge already exists under the same key."""
    from server.code.domain.extract.treesitter import parse_cpp

    pf = parse_cpp("src/a.cpp", _SRC)
    ids = {s.name: s.id for s in pf.symbols}
    existing = [
        {
            "tier": "treesitter",
            "edge_kind": "calls",
            "from_symbol_id": ids["g"],
            "to_symbol_id": ids["f"],
            "path": "src/a.cpp",
            "line": 2,
            "from_qualified_name": "g",
            "to_qualified_name": "f",
            "confidence": 1.0,
            "indexed_commit": "a" * 40,
            "created_at": None,
        }
    ]
    repo = _IngestRepo(existing_edges=existing, table={"h": [("idH", "DB::h", "function")]})

    async def misses(*_):
        return [_miss("h", "src/a.cpp", 2, frm=ids["g"])]

    repo.live_call_misses_for_path = misses  # type: ignore[method-assign]
    stats = await ingest.ingest_file(
        repo,
        "o",
        "n",
        path="src/a.cpp",
        source=_SRC,
        commit_sha=SHA,
        embedding_model="m",
        embedder=None,
        edges_only=True,
    )
    assert stats.edges_new == 1 and stats.edges_tombstoned == 0
    assert stats.misses_new == 0 and stats.misses_tombstoned == 1
    new_edge = [r for t, rows in repo.inserts if t == "code_edges" for r in rows]
    assert [(e["to_symbol_id"], e["confidence"], e["indexed_commit"]) for e in new_edge] == [
        ("idH", 0.6, SHA)
    ]


@pytest.mark.asyncio
async def test_edges_only_refreshes_a_stale_verdict_under_the_same_site_key() -> None:
    """The site key is (callee, path, line, from); the
    verdict is not part of it. `h` was declined `unresolved` when it had no
    definition; two definitions appeared elsewhere, so the site is now
    `ambiguous` with 2 candidates. Re-extraction must rewrite the row (same
    key, newer version — no tombstone, no new key), and an unchanged verdict
    must write nothing."""
    from server.code.domain.extract.treesitter import parse_cpp

    pf = parse_cpp("src/a.cpp", _SRC)
    ids = {s.name: s.id for s in pf.symbols}
    two = {"h": [("idH1", "A::h", "function"), ("idH2", "B::h", "function")]}
    existing = [
        {
            "tier": "treesitter",
            "edge_kind": "calls",
            "from_symbol_id": ids["g"],
            "to_symbol_id": ids["f"],
            "path": "src/a.cpp",
            "line": 2,
            "from_qualified_name": "g",
            "to_qualified_name": "f",
            "confidence": 1.0,
            "indexed_commit": "a" * 40,
            "created_at": None,
        }
    ]
    repo = _IngestRepo(existing_edges=existing, table=two)
    stale = _miss("h", "src/a.cpp", 2, frm=ids["g"])  # disposition unresolved, candidates 0

    async def misses(*_):
        return [dict(stale)]

    repo.live_call_misses_for_path = misses  # type: ignore[method-assign]
    stats = await ingest.ingest_file(
        repo,
        "o",
        "n",
        path="src/a.cpp",
        source=_SRC,
        commit_sha=SHA,
        embedding_model="m",
        embedder=None,
        edges_only=True,
    )
    assert stats.edges_new == 0 and stats.edges_tombstoned == 0
    assert stats.misses_new == 0 and stats.misses_tombstoned == 0
    assert stats.misses_refreshed == 1
    rows = [r for t, rows in repo.inserts if t == "code_call_sites_unresolved" for r in rows]
    assert len(rows) == 1
    (row,) = rows
    assert (row["callee_name"], row["path"], row["line"], row["from_symbol_id"]) == (
        "h",
        "src/a.cpp",
        2,
        ids["g"],
    )
    assert row["disposition"] == "ambiguous" and row["candidates"] == 2
    assert row["indexed_commit"] == SHA and row["tombstoned_at"] is None
    assert row["version"] > stale["max_version"]

    # same verdict as stored -> nothing written for the site
    repo2 = _IngestRepo(existing_edges=existing, table=two)
    current = dict(stale, disposition="ambiguous", candidates=2)

    async def misses2(*_):
        return [current]

    repo2.live_call_misses_for_path = misses2  # type: ignore[method-assign]
    stats2 = await ingest.ingest_file(
        repo2,
        "o",
        "n",
        path="src/a.cpp",
        source=_SRC,
        commit_sha=SHA,
        embedding_model="m",
        embedder=None,
        edges_only=True,
    )
    assert stats2.misses_refreshed == 0 and stats2.misses_new == 0
    assert [r for t, rows in repo2.inserts if t == "code_call_sites_unresolved" for r in rows] == []


@pytest.mark.asyncio
async def test_edges_only_writes_nothing_when_the_parse_degrades(monkeypatch) -> None:
    def _boom(path, source):
        raise RuntimeError("grammar exploded")

    monkeypatch.setitem(ingest_parse._PARSERS, "cpp", _boom)
    repo = _IngestRepo()
    stats = await ingest.ingest_file(
        repo,
        "o",
        "n",
        path="src/a.cpp",
        source=_SRC,
        commit_sha=SHA,
        embedding_model="m",
        embedder=None,
        edges_only=True,
    )
    assert stats.skipped and repo.inserts == [] and repo.reads == []


@pytest.mark.asyncio
async def test_full_ingest_reports_added_and_removed_names() -> None:
    from server.code.domain.extract.treesitter import parse_cpp

    pf = parse_cpp("src/a.cpp", _SRC)
    ids = {s.name: s.id for s in pf.symbols}
    repo = _IngestRepo()

    async def symbols(*_):
        return [
            {
                "qualified_name": "old",
                "symbol_id": "idOld",
                "name": "old",
                "kind": "function",
                "start_line": 1,
                "end_line": 1,
                "signature": "",
                "path": "src/a.cpp",
                "tier": "treesitter",
                "created_at": None,
            }
        ]

    repo.live_symbols_for_path = symbols  # type: ignore[method-assign]
    stats = await ingest.ingest_file(
        repo,
        "o",
        "n",
        path="src/a.cpp",
        source=_SRC,
        commit_sha=SHA,
        embedding_model="m",
        embedder=None,
    )
    assert sorted(stats.symbol_names_added) == ["f", "g"]
    assert stats.symbol_names_removed == ["old"] and stats.symbols_tombstoned_ids == ["idOld"]
    assert ids["f"] != "idOld"


@pytest.mark.asyncio
async def test_tombstone_file_returns_what_it_retired() -> None:
    class _R(_IngestRepo):
        async def live_chunks_for_path(self, *a, **k):
            return []

        async def live_symbols_for_path(self, *_):
            return [
                {
                    "qualified_name": "DB::f",
                    "symbol_id": "idF",
                    "name": "f",
                    "kind": "function",
                    "path": "src/a.cpp",
                    "created_at": None,
                },
                {
                    "qualified_name": "DB::g",
                    "symbol_id": "idG",
                    "name": "",
                    "kind": "function",
                    "path": "src/a.cpp",
                    "created_at": None,
                },
            ]

        async def live_files_for_path(self, *_):
            return []

    stats = await ingest.tombstone_file(_R(), "o", "n", path="src/a.cpp", embedding_model="m")
    assert stats.symbol_names_removed == ["f", "g"], "bare name falls back to the qualified tail"
    assert stats.symbols_tombstoned_ids == ["idF", "idG"] and stats.symbols_tombstoned == 2


# ---- worker + reconcile wiring -------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_skips_dependents_when_both_settings_are_off(monkeypatch) -> None:
    from server.code.app.index import pass_budget as ingest_budget
    from server.code.app.index import pass_commit as ingest_commit

    monkeypatch.setattr(settings, "code_ingest_dependents_enabled", False)
    monkeypatch.setattr(settings, "code_closure_repair_enabled", False)
    result = ingest_budget.CodeIngestResult()
    called = []

    async def boom(*a, **k):
        called.append(1)

    with patch("server.code.app.index.dependents.run_for_commit", new=boom):
        await ingest_commit._resolve_dependents(
            object(),
            "o",
            "n",
            "/g",
            SHA,
            [_Stats(added=["x"])],
            set(),
            embedding_model="m",
            gitrepo=object(),
            ingest_file=object(),
            result=result,
        )
    assert called == [] and result.errors == []


@pytest.mark.asyncio
async def test_worker_counts_dependents_and_records_failures(monkeypatch) -> None:
    from server.code.app.index import pass_budget as ingest_budget
    from server.code.app.index import pass_commit as ingest_commit

    monkeypatch.setattr(settings, "code_ingest_dependents_enabled", True)
    monkeypatch.setattr(settings, "code_closure_repair_enabled", True)
    monkeypatch.setattr(settings, "code_ingest_dependents_max_files", 7)
    result = ingest_budget.CodeIngestResult()
    got: dict[str, Any] = {}

    async def fake_run(repo, org, ns, **kw):
        got.update(kw)
        return (
            d.ClosureRepairResult(resolved=3, ms=4),
            d.DependentsResult(candidates=9, reingested=7, queued=2, edges_new=5, ms=11),
        )

    class _Git:
        show_file = object()

    with patch("server.code.app.index.dependents.run_for_commit", new=fake_run):
        await ingest_commit._resolve_dependents(
            object(),
            "o",
            "n",
            "/g",
            SHA,
            [_Stats(added=["x"]), None],
            {"src/t.cpp"},
            embedding_model="m",
            gitrepo=_Git,
            ingest_file=object(),
            result=result,
        )
    assert got["max_files"] == 7 and got["delta"].names_added == {"x"}
    assert got["delta"].touched_paths == {"src/t.cpp"} and got["show_file"] is _Git.show_file
    assert (
        result.closure_resolved,
        result.dependents_reingested,
        result.dependents_queued,
        result.edges_new,
    ) == (3, 7, 2, 5)
    assert result.dependents_ms == 11 and result.closure_ms == 4 and result.errors == []

    async def fail(*a, **k):
        raise RuntimeError("ch down")

    with patch("server.code.app.index.dependents.run_for_commit", new=fail):
        await ingest_commit._resolve_dependents(
            object(),
            "o",
            "n",
            "/g",
            SHA,
            [_Stats(added=["x"])],
            set(),
            embedding_model="m",
            gitrepo=_Git,
            ingest_file=object(),
            result=result,
        )
    assert result.errors == [f"{SHA[:12]} dependents: RuntimeError: ch down"]


@pytest.mark.asyncio
async def test_reconcile_drains_the_queue_inside_the_heal_budget(monkeypatch) -> None:
    from server.code.app.index import ingest_pass as w
    from server.code.app.index import pass_budget as ingest_budget

    monkeypatch.setattr(settings, "code_reconcile_enabled", True)
    monkeypatch.setattr(settings, "code_reconcile_interval_seconds", 3600)
    monkeypatch.setattr(settings, "code_reconcile_heal_per_pass", 5)
    monkeypatch.setattr(settings, "code_ingest_dependents_enabled", True)

    def _Cfg(*_a: Any, **_k: Any) -> Any:
        return repo_config(
            org="o", namespace="n", git_dir="/git", embed_reuse_from="", path_prefixes=("",)
        )

    repo = _Repo()
    result = ingest_budget.CodeIngestResult()

    class _Found:
        stale = [f"src/{i}.cpp" for i in range(6)]
        stale_count, paths_total, commits_distinct, commits_checked = 6, 10, 3, 3
        commits_failed, truncated, took_ms = 0, False, 12
        missing: dict[str, str] = {}

    class _Healed:
        files, chunks_moved, symbols_moved, errors = 2, 0, 0, 0

    healed_paths: list[str] = []
    drain_kw: dict[str, Any] = {}

    async def find(*a, **k):
        return _Found()

    async def do_heal(repo_, cfg, head, paths, **k):
        healed_paths.extend(paths)
        return _Healed()

    async def drain(repo_, cfg, head, *, budget, embedding_model):
        drain_kw.update(budget=budget, model=embedding_model)
        return d.DrainResult(files=3, remaining=4)

    class _Emb:
        model_name = "m"

    with (
        patch("server.code.app.index.reconcile.find_stale_paths", new=find),
        patch("server.code.app.index.reconcile.heal", new=do_heal),
        patch("server.code.app.index.reconcile.drain_dependents_pending", new=drain),
        patch("server.embedding.embedder.get_embedder", new=lambda: _Emb()),
    ):
        await w._maybe_reconcile(repo, _Cfg(), SHA, result)
    assert drain_kw == {"budget": 5, "model": "m"}
    assert healed_paths == ["src/0.cpp", "src/1.cpp"], "heal gets the budget the drain left"
    assert result.dependents_drained == 3
    detail = json.loads(repo.state["reconcile"]["detail"])
    assert detail["dependents_drained"] == 3 and detail["dependents_pending"] == 4
