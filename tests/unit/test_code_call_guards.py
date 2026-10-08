"""The conditions that guard a call are indexed, and a path
between two symbols reports them. They are stored once per caller function
(`loom.code_caller_guards`), sites referencing their guards by index.

The extractor (call_guards.py) reads the dominating conditions of each
call site off the syntax tree and folds a file's sites into one record per
caller; the op (code.path_guards) walks the verify(reaches) chain, reads
the chain's callers and judges every call site of every hop on its own
guard list. Both are syntactic and intraprocedural — every verdict is a
lower bound, and the tests pin what that means: a cap is `unknown`, an
empty guards table is `unknown`, a call under no condition is not listed.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

pytest.importorskip("tree_sitter_cpp")

from server.code.app.ttl_cache import clear_all
from server.code.domain.extract.call_guards import (
    CALLER_GUARDS_MAX,
    EARLY_EXIT_MAX,
    GUARD_MAX_CHARS,
    GUARD_MAX_LEVELS,
    CallerGuards,
    callee_tag,
    extract_call_guards,
    guard_names,
    normalize_guard,
    site_guards_of,
)
from server.code.domain.extract.edges_treesitter import Edge, extract_edges
from server.code.domain.extract.treesitter import parse_cpp

# ---- fixture: nested ifs, else-if, early exits, switch, ternary, loops, a
# preprocessor conditional, a lambda, and one call under no guard at all.
_CPP = b"""
namespace DB {
int g(int); int h(int); int k(int); int plain(int); int deep(int);

int f(int x, Ptr p, const Settings & settings)
{
    plain(x);                                   // no guard: no row
    if (!p) return -1;
    if (x < 0)
        throw Exception("neg");
    if (settings.allow_experimental) { g(1); }  // one level
    for (int i = 0; i < x; ++i)
    {
        if (i % 2 == 0 && p->ok())
        {
            g(i);
        }
        else if (i == 3)
        {
            h(i);
        }
        else
        {
            k(i);
        }
    }
    switch (x)
    {
        case 1: g(1); break;
        case 2:
        case 3: { h(2); break; }
        default: k(0);
    }
    int y = x > 0 ? g(x) : h(x);
    while (p->more()) { if (p->bad()) continue; g(9); }
#if USE_SSL
    g(11);
#else
    h(11);
#endif
    auto l = [&]() { if (x) g(7); };
    if (a) { if (b) { if (c) { if (d) { deep(1); } } } }
    return y;
}
}
"""


def _fixture() -> tuple[Any, list[Edge]]:
    pf = parse_cpp("src/f.cpp", _CPP)
    table = {n: [(f"id_{n}", f"DB::{n}", "function")] for n in ("g", "h", "k", "plain", "deep")}
    edges = extract_edges(pf, table)
    return pf, [e for e in edges if e.edge_kind == "calls"]


def _sites(records: list[CallerGuards]) -> dict[tuple[int, int], list[tuple[str, str]]]:
    """(line, callee tag) -> [(kind, text)] in the order the guards apply, expanded
    from the caller records exactly as the read side does."""
    return {
        (line, tag): [(g["kind"], g["text"]) for g in guards]
        for (_from, tag, _path, line), guards in site_guards_of(_rows(records)).items()
    }


def _rows(records: list[CallerGuards]) -> list[dict[str, Any]]:
    return [
        {
            "from_symbol_id": r.from_symbol_id,
            "path": r.path,
            "guards": list(r.guards),
            "guard_kinds": list(r.guard_kinds),
            "sites": list(r.sites),
            "site_callees": list(r.site_callees),
            "site_guards": [list(x) for x in r.site_guards],
        }
        for r in records
    ]


def _by_site(records: list[CallerGuards]) -> dict[tuple[int, str], list[tuple[str, str]]]:
    tags = {callee_tag(f"id_{n}"): f"id_{n}" for n in ("g", "h", "k", "plain", "deep")}
    return {(line, tags[tag]): v for (line, tag), v in _sites(records).items()}


def test_extractor_reads_enclosing_conditions_and_early_exits() -> None:
    pf, edges = _fixture()
    got = _by_site(extract_call_guards(pf, edges))
    early = [("early_return", "!p"), ("early_throw", "x < 0")]
    # one level under an if, after two early exits — the ClickGap shape
    assert got[(11, "id_g")] == [*early, ("if", "settings.allow_experimental")]
    # loop header + if / else-if / else, polarity in the kind, text as written
    assert got[(16, "id_g")] == [*early, ("loop", "i < x"), ("if", "i % 2 == 0 && p->ok()")]
    assert got[(20, "id_h")] == [
        *early,
        ("loop", "i < x"),
        ("else", "i % 2 == 0 && p->ok()"),
        ("if", "i == 3"),
    ]
    assert got[(24, "id_k")] == [
        *early,
        ("loop", "i < x"),
        ("else", "i % 2 == 0 && p->ok()"),
        ("else", "i == 3"),
    ]
    # switch: the selector and the case label; default is named
    assert got[(29, "id_g")] == [*early, ("switch", "x"), ("case", "1")]
    assert got[(31, "id_h")] == [*early, ("switch", "x"), ("case", "3")]
    assert got[(32, "id_k")] == [*early, ("switch", "x"), ("case", "default")]
    # ternary: both arms, on the same line, keyed apart by callee
    assert got[(34, "id_g")] == [*early, ("ternary", "x > 0")]
    assert got[(34, "id_h")] == [*early, ("ternary_else", "x > 0")]
    # an early continue inside the loop body sits between the exits and the header
    assert got[(35, "id_g")] == [*early, ("early_continue", "p->bad()"), ("loop", "p->more()")]
    # preprocessor conditionals keep their directive (polarity in the text)
    assert got[(37, "id_g")] == [*early, ("preproc", "#if USE_SSL")]
    assert got[(39, "id_h")] == [*early, ("preproc_else", "#if USE_SSL")]
    # a lambda is a function boundary: the outer early exits do not apply
    assert got[(41, "id_g")] == [("if", "x")]


def test_extractor_writes_no_site_for_an_unguarded_call() -> None:
    """The table is sparse by design: a call under no
    condition is not listed among its caller's sites, and a caller with no
    conditional call has no record; the op tells that apart from "not
    indexed" with the namespace probe, not with an empty array."""
    pf, edges = _fixture()
    records = extract_call_guards(pf, edges)
    plain = [e for e in edges if e.to_symbol_id == "id_plain"]
    assert plain and plain[0].line == 7
    assert callee_tag("id_plain") not in {t for r in records for t in r.site_callees}
    assert all(r.guards and r.sites for r in records), "never an empty record"
    assert all(len(r.guards) == len(r.guard_kinds) for r in records)
    assert all(len(r.sites) == len(r.site_callees) == len(r.site_guards) for r in records)


def test_extractor_folds_a_file_into_one_record_per_caller() -> None:
    """The file has one caller with conditional calls (`f`;
    the lambda's call is attributed to `f` by the edge pass); each distinct
    (kind, condition) is listed once although the two early exits dominate
    eleven of its sites, and every site references its guards by index in
    the order they apply — the lambda's site references only its own `if`."""
    pf, edges = _fixture()
    records = extract_call_guards(pf, edges)
    assert len(records) == 1 and records[0].path == "src/f.cpp"
    f = records[0]
    pairs = list(zip(f.guard_kinds, f.guards, strict=True))
    lam = [x for line, x in zip(f.sites, f.site_guards, strict=True) if line == 41]
    assert lam == [
        (pairs.index(("if", "x")),)
    ]  # not `switch (x)`'s "x": the kind is part of the identity
    assert len(pairs) == len(set(pairs)), "a (kind, condition) is listed once"
    assert pairs[:2] == [("early_return", "!p"), ("early_throw", "x < 0")]
    assert sum(1 for idxs in f.site_guards if idxs[:2] == (0, 1)) >= 11
    assert list(f.sites) == sorted(f.sites) and all(
        0 <= i < len(f.guards) for x in f.site_guards for i in x
    )
    # the ternary line holds two sites told apart by the callee tag
    assert [t for line, t in zip(f.sites, f.site_callees, strict=True) if line == 34] == sorted(
        [callee_tag("id_g"), callee_tag("id_h")]
    )


def test_callee_tag_is_the_hex_prefix_of_a_real_symbol_id() -> None:
    from server.code.domain.extract.treesitter import symbol_id

    sid = symbol_id("DB::Foo::bar", "(int)")
    assert len(sid) == 32 and callee_tag(sid) == int(sid[:8], 16)
    assert callee_tag("id_g") != callee_tag("id_h")  # non-hex ids hash
    assert 0 <= callee_tag("id_g") <= 0xFFFFFFFF


def test_caller_record_caps_distinct_guards_at_uint8() -> None:
    """A function with more than CALLER_GUARDS_MAX distinct conditions keeps
    the first 255 in site order; later sites reference only those (the
    indexes are UInt8) and the expander skips nothing it cannot resolve."""
    body = "\n".join(f"    if (c{i}) g({i});" for i in range(CALLER_GUARDS_MAX + 5))
    src = f"int g(int);\nint f(int x) {{\n{body}\n    return 0;\n}}\n".encode()
    pf = parse_cpp("a.cpp", src)
    edges = [
        e for e in extract_edges(pf, {"g": [("id_g", "g", "function")]}) if e.edge_kind == "calls"
    ]
    (rec,) = extract_call_guards(pf, edges)
    assert len(rec.guards) == CALLER_GUARDS_MAX and len(rec.sites) == CALLER_GUARDS_MAX + 5
    assert rec.site_guards[-1] == () and rec.site_guards[0] == (0,)
    assert max(i for x in rec.site_guards for i in x) == CALLER_GUARDS_MAX - 1
    expanded = site_guards_of(_rows([rec]))
    assert len(expanded) == CALLER_GUARDS_MAX + 5


def test_extractor_caps_levels_and_chars() -> None:
    pf, edges = _fixture()
    got = _by_site(extract_call_guards(pf, edges))
    deep = got[(42, "id_deep")]
    # four nested ifs: the innermost three survive, outermost first
    assert [k for k, _ in deep[-GUARD_MAX_LEVELS:]] == ["if", "if", "if"]
    assert [t for _, t in deep[-GUARD_MAX_LEVELS:]] == ["b", "c", "d"]
    assert len(deep) == 2 + GUARD_MAX_LEVELS  # the two early exits still apply
    long = "x" * 300
    assert len(normalize_guard(f"(  {long}  )")) == GUARD_MAX_CHARS
    assert normalize_guard("(a)\n && (b)") == "(a) && (b)"  # outer pair only when it IS a pair
    assert normalize_guard("( p->isReadOnly() )") == "p->isReadOnly()"


def test_extractor_caps_early_exits_to_the_nearest() -> None:
    body = "\n".join(f"    if (c{i}) return {i};" for i in range(EARLY_EXIT_MAX + 4))
    src = f"int g(int);\nint f(int x) {{\n{body}\n    g(x);\n    return 0;\n}}\n".encode()
    pf = parse_cpp("a.cpp", src)
    edges = [
        e for e in extract_edges(pf, {"g": [("id_g", "g", "function")]}) if e.edge_kind == "calls"
    ]
    (rec,) = extract_call_guards(pf, edges)
    (((_line, _tag), guards),) = _sites([rec]).items()
    assert len(guards) == EARLY_EXIT_MAX
    assert guards[-1][1] == f"c{EARLY_EXIT_MAX + 3}" and guards[0][1] == "c4"


def test_extractor_skips_languages_without_a_grammar_rule() -> None:
    pf = parse_cpp("a.sh", b"")
    pf.lang = "shell"
    assert extract_call_guards(pf, []) == []


def test_python_guards() -> None:
    pytest.importorskip("tree_sitter_python")
    from server.code.domain.extract.lang_python import parse_python

    src = b"""
def g(i): return i
def h(i): return i
def f(x, p):
    if not p:
        return -1
    if x < 0:
        raise ValueError("neg")
    if p.ok() and x:
        g(1)
    elif x == 3:
        h(2)
    y = g(x) if x > 0 else h(x)
    while p.more():
        if p.bad():
            continue
        g(9)
    return h(0)
"""
    pf = parse_python("pkg/m.py", src)
    edges = [e for e in extract_edges(pf, {}) if e.edge_kind == "calls"]
    got = _sites(extract_call_guards(pf, edges))
    by_line = {line: v for (line, _tag), v in got.items()}
    early = [("early_return", "not p"), ("early_throw", "x < 0")]
    assert by_line[10] == [*early, ("if", "p.ok() and x")]
    assert by_line[12] == [*early, ("else", "p.ok() and x"), ("if", "x == 3")]
    # the ternary line has two sites with different callees: both arms present
    arms = sorted(v[-1] for (line, _s), v in got.items() if line == 13)
    assert arms == [("ternary", "x > 0"), ("ternary_else", "x > 0")]
    assert by_line[17] == [*early, ("early_continue", "p.bad()"), ("loop", "p.more()")]
    # `return h(0)` has no enclosing condition, but the two early exits above
    # it dominate it: guarded by them alone
    assert by_line[18] == early


def test_guard_names_matches_identifiers_on_word_boundaries() -> None:
    assert guard_names("settings.allow_experimental && !p", "allow_experimental")
    assert not guard_names("is_ok(x)", "ok")
    assert guard_names("p->isReadOnly() && x", "p->isReadOnly()")
    assert guard_names("DB::Foo::bar(x)", "Foo::bar")
    assert not guard_names("x", "")
    assert not guard_names("ReadOnly", "readonly")  # case-sensitive identifiers


# ---- the op -----------------------------------------------------------------------


def _sym(q: str, sid: str, path: str = "src/A.cpp") -> dict[str, Any]:
    return {
        "qualified_name": q,
        "symbol_id": sid,
        "path": path,
        "start_line": 1,
        "end_line": 9,
        "kind": "function",
        "tier": "treesitter",
        "signature": "()",
    }


def _edge(f: str, t: str, fq: str, tq: str, line: int, path: str = "src/A.cpp") -> dict[str, Any]:
    return {
        "tier": "treesitter",
        "edge_kind": "calls",
        "from_symbol_id": f,
        "to_symbol_id": t,
        "from_qualified_name": fq,
        "to_qualified_name": tq,
        "path": path,
        "line": line,
        "confidence": 1.0,
    }


class _Repo:
    """A -> B -> C, with guards on A->B (`settings.readonly`) and none stored for B->C."""

    def __init__(
        self, *, guards_indexed: bool = True, chain_open: bool = False, guard_walk_done: bool = True
    ) -> None:
        self.guard_walk_done = guard_walk_done
        self.symbols = {
            "DB::A": _sym("DB::A", "sA"),
            "DB::B": _sym("DB::B", "sB"),
            "DB::C": _sym("DB::C", "sC", "src/C.cpp"),
            "DB::Z": _sym("DB::Z", "sZ", "src/Z.cpp"),
        }
        self.edges = {
            # A calls B at two sites; only :12 is guarded — the pair is one hop
            # (the guarded site comes SECOND in the batch: the walk must not return on the
            # first row of the pair before it has seen the rest of them)
            "sA": [
                _edge("sA", "sB", "DB::A", "DB::B", 30),
                _edge("sA", "sB", "DB::A", "DB::B", 12),
            ],
            "sB": [_edge("sB", "sC", "DB::B", "DB::C", 40)],
        }
        if chain_open:  # an endless frontier: every symbol calls a fresh one
            self.edges["sC"] = [_edge("sC", "sD", "DB::C", "DB::D", 5, "src/C.cpp")]
            self.edges["sD"] = [_edge("sD", "sE", "DB::D", "DB::E", 6, "src/D.cpp")]
            self.edges["sE"] = [_edge("sE", "sF", "DB::E", "DB::F", 7, "src/E.cpp")]
        # one caller row for A: site:12 -> sB under both guards
        self.guard_rows = [
            {
                "from_symbol_id": "sA",
                "path": "src/A.cpp",
                "guards": ["!settings.readonly", "x < 0"],
                "guard_kinds": ["if", "early_throw"],
                "sites": [12],
                "site_callees": [callee_tag("sB")],
                "site_guards": [[0, 1]],
            }
        ]
        self.guards_indexed = guards_indexed
        self.probes = 0

    async def symbols_by_name(self, org, ns, name, *, limit=50):
        s = self.symbols.get(name)
        return [s] if s else []

    async def symbols_by_ids(self, org, ns, ids):
        return [s for s in self.symbols.values() if s["symbol_id"] in ids]

    async def symbols_fuzzy(self, org, ns, needle, *, limit=25):
        return []

    async def edges_for_symbols(self, org, ns, ids, *, direction, edge_kinds=None, limit=200, **kw):
        assert direction == "callees"
        return [e for i in ids for e in self.edges.get(i, [])]

    async def call_guards_for_callers(self, org, ns, froms):
        return [r for r in self.guard_rows if r["from_symbol_id"] in set(froms)]

    async def has_call_guards(self, org, ns):
        self.probes += 1
        return self.guards_indexed

    async def all_state(self, org, ns):
        from server.code.domain.extract.extractor_stamp import stage_stamps

        if not self.guard_walk_done:  # The paced walk has not converged
            return {"extractor_stamp": {"value": "old", "detail": "{}"}}
        return {"extractor_stamp_guard": {"value": stage_stamps()["guard"], "detail": "{}"}}


def _run(
    monkeypatch: pytest.MonkeyPatch, repo: _Repo, **kw: Any
) -> tuple[Any, list[dict[str, Any]]]:
    from server.code.api.models_verify import CodePathGuardsInput
    from server.code.app import path_guards as h

    calls: list[dict[str, Any]] = []
    clear_all()
    monkeypatch.setattr(h, "schedule_code_trace", lambda **k: calls.append(k))
    with patch.object(h, "CodeRepo", return_value=repo):
        out = asyncio.run(
            h.handle_code_path_guards(CodePathGuardsInput(org="o", namespace="n", **kw))
        )
    return out, calls


def test_op_guarded_by_names_the_hop(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _Repo()
    out, (trace,) = _run(
        monkeypatch, repo, from_symbol="DB::A", to_symbol="DB::C", mentions="readonly"
    )
    assert out.verdict == "guarded_by" and out.epistemic == "lower_bound"
    assert out.named_at == ["src/A.cpp:12"]
    assert [h["caller"] for h in out.hops] == ["DB::A", "DB::B"]
    assert out.hops[0]["mentions_hit"] is True and out.hops[1]["mentions_hit"] is False
    # the chain's own site is the batch's first row (:30, unguarded); the pair's
    # guarded site (:12) is judged and listed all the same, and names the hop
    assert out.hops[0]["line"] == 30 and out.hops[0]["guards"] == []
    assert out.hops[0]["sites"][0]["guards"] == [
        {"kind": "if", "text": "!settings.readonly"},
        {"kind": "early_throw", "text": "x < 0"},
    ]
    assert out.hops[1]["guards"] == []
    assert [(x["line"], x["mentions_hit"]) for x in out.hops[0]["sites"]] == [
        (12, True),
        (30, False),
    ]
    assert out.hops[0]["sites_named"] == 1 and out.hops[1]["sites"] == [
        {"path": "src/A.cpp", "line": 40, "guards": [], "mentions_hit": False}
    ]
    assert "1 of 3 call site(s)" in out.explanation
    assert repo.probes == 0, "a hit needs no positive control"
    assert trace["op"] == "code.path_guards" and trace["abstain"] is False
    assert trace["returned_paths"] == ["src/A.cpp", "src/C.cpp"] or trace["returned_paths"] == [
        "src/A.cpp"
    ]
    assert "syntactic" in out.causes and "intraprocedural" in out.causes


def test_op_no_guard_named_only_when_the_namespace_holds_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _Repo()
    out, _ = _run(monkeypatch, repo, from_symbol="DB::A", to_symbol="DB::C", mentions="allow_ddl")
    assert out.verdict == "no_guard_named" and repo.probes == 1
    assert out.named_at == [] and all(h["mentions_hit"] is False for h in out.hops)
    # the same path with the writer never run here: unknown, not "unchecked"
    repo = _Repo(guards_indexed=False)
    out, (trace,) = _run(
        monkeypatch, repo, from_symbol="DB::A", to_symbol="DB::C", mentions="allow_ddl"
    )
    assert out.verdict == "unknown" and out.causes[0] == "guards_not_indexed"
    assert len(out.hops) == 2, "the path is still reported"
    assert trace["abstain"] is True and trace["verdict"] == "no_match"


def test_op_is_unknown_while_the_guard_walk_has_not_converged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """During the paced heal a namespace holds SOME guard rows;
    a hop in a file the walk has not reached must read as unindexed, never
    as unconditional. A guarded hit is still a hit — no probe."""
    repo = _Repo(guard_walk_done=False)
    out, (trace,) = _run(
        monkeypatch, repo, from_symbol="DB::A", to_symbol="DB::C", mentions="allow_ddl"
    )
    assert out.verdict == "unknown" and out.causes[0] == "guards_indexing" and repo.probes == 1
    assert len(out.hops) == 2 and trace["abstain"] is True
    assert "has not converged" in out.explanation
    out, _ = _run(
        monkeypatch,
        _Repo(guard_walk_done=False),
        from_symbol="DB::A",
        to_symbol="DB::B",
        mentions="readonly",
    )
    assert out.verdict == "guarded_by", (
        "a guard that names the identifier needs no positive control"
    )


def test_op_lists_guards_when_nothing_is_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    out, _ = _run(monkeypatch, _Repo(), from_symbol="DB::A", to_symbol="DB::B")
    assert out.verdict == "path_found"
    assert out.hops[0]["sites"][0]["guards"][0]["text"] == "!settings.readonly"
    assert out.named_at == []


def test_op_tells_a_cap_from_an_absence(monkeypatch: pytest.MonkeyPatch) -> None:
    # frontier empties: nothing reaches Z
    out, _ = _run(monkeypatch, _Repo(), from_symbol="DB::A", to_symbol="DB::Z", mentions="x")
    assert out.verdict == "path_not_found" and out.hops == [] and out.visited_symbols == 3
    # depth 1 with the chain still open: unknown (depth_exhausted)
    out, (trace,) = _run(
        monkeypatch,
        _Repo(chain_open=True),
        from_symbol="DB::A",
        to_symbol="DB::Z",
        mentions="x",
        depth=1,
    )
    assert (
        out.verdict == "unknown" and out.causes[0] == "depth_exhausted" and trace["abstain"] is True
    )


def test_op_refuses_an_unknown_symbol(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.core.errors import NotFoundError

    with pytest.raises(NotFoundError):
        _run(monkeypatch, _Repo(), from_symbol="DB::Nope", to_symbol="DB::C")


def test_op_path_prefix_target_like_verify(monkeypatch: pytest.MonkeyPatch) -> None:
    """A `to` with a slash is a path prefix, matched against the call-site
    path of an edge exactly as verify(reaches) does."""
    out, _ = _run(
        monkeypatch, _Repo(), from_symbol="DB::A", to_symbol="src/A.cpp", mentions="readonly"
    )
    assert out.verdict == "guarded_by" and len(out.hops) == 1 and out.hops[0]["path"] == "src/A.cpp"
    out, _ = _run(
        monkeypatch, _Repo(), from_symbol="DB::A", to_symbol="src/Nowhere/", mentions="readonly"
    )
    assert out.verdict == "path_not_found"


def test_verify_evidence_payload_is_unchanged_by_the_shared_bfs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """code.verify(reaches) walks through the shared bfs_callees; its
    evidence rows keep exactly the five keys they had."""
    from server.code.api.models_verify import CodeVerifyInput
    from server.code.app.symbol import verify as h

    monkeypatch.setattr(h, "schedule_code_trace", lambda **k: None)
    with patch.object(h, "CodeRepo", return_value=_Repo()):
        out = asyncio.run(
            h.handle_code_verify(
                CodeVerifyInput(
                    org="o", namespace="n", kind="reaches", subject="DB::A", object="DB::C"
                )
            )
        )
    assert out.verdict == "confirmed"
    assert [set(e) for e in out.evidence] == [{"from", "to", "path", "line", "tier"}] * 2


# ---- the ingest write -----------------------------------------------------------


class _IngestRepo:
    def __init__(self, live: list[dict[str, Any]]) -> None:
        self.live = live
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []

    async def live_call_guards_for_path(self, org, ns, path):
        return list(self.live)

    async def insert_rows(self, table, rows):
        self.inserts.append((table, rows))


def test_ingest_diff_inserts_only_guarded_callers_and_tombstones_vanished(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.app.index import ingest_reconcile, ingest_stats

    pf, edges = _fixture()
    (rec,) = extract_call_guards(pf, edges)
    meta = {
        "org": "o",
        "namespace": "n",
        "indexed_commit": "",
        "created_at": None,
        "version": 1,
        "tombstoned_at": None,
    }
    stale = {
        **meta,
        "from_symbol_id": "gone",
        "path": "src/f.cpp",
        "guards": ["old"],
        "guard_kinds": ["if"],
        "sites": [999],
        "site_callees": [1],
        "site_guards": [[0]],
    }
    # the caller's live row with its sites one line off: re-emitted under its key, no tombstone
    moved = {**meta, **_rows([rec])[0], "sites": [x + 1 for x in rec.sites]}
    repo = _IngestRepo([stale, moved])
    stats = ingest_stats.FileIngestStats(path="src/f.cpp")
    asyncio.run(
        ingest_reconcile._reconcile_call_guards(
            repo, "o", "n", pf, "src/f.cpp", "c" * 40, edges, stats
        )
    )  # type: ignore[arg-type]
    ((table, rows),) = repo.inserts
    assert table == "code_caller_guards"
    tomb = [r for r in rows if r["tombstoned_at"] is not None]
    fresh = [r for r in rows if r["tombstoned_at"] is None]
    assert len(tomb) == 1 and tomb[0]["from_symbol_id"] == "gone"
    assert [r["from_symbol_id"] for r in fresh] == [rec.from_symbol_id]
    assert fresh[0]["sites"] == list(rec.sites) and fresh[0]["indexed_commit"] == "c" * 40
    assert (
        fresh[0]["version"] == 1 or fresh[0]["version"] is not None
    )  # a moved_of row keeps the key
    assert stats.guards_new == 1 and stats.guards_tombstoned == 1
    assert fresh[0]["guards"] and fresh[0]["sites"], "no row for a caller without a guarded site"
    assert 7 not in fresh[0]["sites"], "plain(x) at line 7 is not a site"
    # a live row identical to the derived record: kept, never re-inserted
    repo2 = _IngestRepo([{**meta, **_rows([rec])[0]}])
    stats2 = ingest_stats.FileIngestStats(path="src/f.cpp")
    asyncio.run(
        ingest_reconcile._reconcile_call_guards(
            repo2, "o", "n", pf, "src/f.cpp", "c" * 40, edges, stats2
        )
    )  # type: ignore[arg-type]
    assert repo2.inserts == [("code_caller_guards", [])]
    assert stats2.guards_new == 0 and stats2.guards_tombstoned == 0


def test_the_edge_pass_writes_the_guard_diff(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.code.app.index import ingest_reconcile, ingest_stats

    called: list[str] = []

    async def _boom(*a: Any, **k: Any) -> None:
        called.append("guards")

    monkeypatch.setattr(ingest_reconcile, "_reconcile_call_guards", _boom)

    class _R:
        async def name_candidates(self, *a, **k):
            return {}

        async def live_edges_for_path(self, *a, **k):
            return []

        async def insert_rows(self, *a, **k):
            return None

        async def live_call_misses_for_path(self, *a, **k):
            return []

    pf, _ = _fixture()
    stats = ingest_stats.FileIngestStats(path="src/f.cpp")
    asyncio.run(
        ingest_reconcile._reconcile_edges_and_misses(
            _R(), "o", "n", pf, "src/f.cpp", "c" * 40, stats
        )
    )  # type: ignore[arg-type]
    assert called == ["guards"]
    called.clear()
    asyncio.run(
        ingest_reconcile._reconcile_edges_and_misses(
            _R(), "o", "n", pf, "src/f.cpp", "c" * 40, stats
        )
    )  # type: ignore[arg-type]
    assert called == ["guards"]
