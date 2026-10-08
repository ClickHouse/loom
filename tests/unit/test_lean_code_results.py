"""The MCP shape of a code.* result keeps what an agent acts
on (path, line, symbol, signature, body, next, every correctness signal)
and drops the bookkeeping of how it was found."""

from __future__ import annotations

from typing import Any

import server.transport.lean_results as lr
from server.transport.code_lean import lean_code

FRESH = {
    "branch": "main",
    "commit": "c15ee82f1020ed8261a2add694a46519457e2d2a",
    "indexed_at": "2026-09-24T23:19:56",
    "lag_commits": 0,
    "stale": False,
    "status": "fresh",
}


def _edge(frm: str, line: int, **kw: Any) -> dict[str, Any]:
    return {
        "edge_kind": "calls",
        "from_symbol": frm,
        "to_symbol": "m::seed",
        "from_symbol_id": f"{line:032x}",
        "to_symbol_id": "f" * 32,
        "path": f"{frm}.py",
        "line": line,
        "tier": "scip",
        "confidence": 1.0,
        **kw,
    }


def test_callers_name_the_seed_once_and_keep_what_locates_a_call_site() -> None:
    out = lean_code(
        "code.callers",
        {
            "edges": [
                _edge("a", 1),
                _edge("b", 2, via="registration"),
                _edge("c", 3, tier="treesitter", confidence=0.6),
            ],
            "seed_symbols": ["m::seed"],
            "epistemic": "lower_bound",
            "causes": ["dynamic_dispatch"],
            "unresolved_calls": 0,
            "ambiguous_calls": 2,
            "counts_floor": True,
            "candidate_sites": {"epistemic": "lower_bound", "total": 0, "groups": []},
            "by_hop_counts": {"1": 3},
            "truncated": False,
            "trace_run_id": "t1",
        },
    )
    assert out["seed"] == "m::seed" and "seed_symbols" not in out
    assert all(
        "to_symbol" not in e and "to_symbol_id" not in e and "from_symbol_id" not in e
        for e in out["edges"]
    )
    assert [e["path"] for e in out["edges"]] == ["a.py", "b.py", "c.py"]  # the plugin reads these
    assert out["edges"][1]["via"] == "registration"  # a real edge kind stays
    assert out["edges"][2]["confidence"] == 0.6 and out["edges"][2]["tier"] == "treesitter"
    assert out["defaults"]["tier"] == "scip" and "tier" not in out["edges"][0]
    # correctness signals survive; zero / false defaults do not
    assert out["epistemic"] == "lower_bound" and out["causes"] == ["dynamic_dispatch"]
    assert out["ambiguous_calls"] == 2 and out["counts_floor"] is True
    assert "unresolved_calls" not in out and "candidate_sites" not in out
    assert "by_hop_counts" not in out and out["trace_run_id"] == "t1"


def test_callers_keep_ids_when_a_caller_name_is_ambiguous_and_hop_totals_when_truncated() -> None:
    out = lean_code(
        "code.callers",
        {
            "edges": [_edge("dup", 1), _edge("dup", 2), _edge("x", 3)],
            "truncated": True,
            "by_hop_counts": {"1": 40},
            "candidate_sites": {"total": 4, "groups": [{"scope": "s"}]},
        },
    )
    ids = [e.get("from_symbol_id") for e in out["edges"]]
    assert ids[0] and ids[1] and ids[2] is None
    assert out["by_hop_counts"] == {"1": 40} and out["candidate_sites"]["total"] == 4


def test_outline_says_the_module_once_and_rewrites_anonymous_namespaces() -> None:
    path = "src/X.cpp"
    out = lean_code(
        "code.outline",
        {
            "path": path,
            "indexed": True,
            "as_of": FRESH,
            "entries": [
                {
                    "kind": "class",
                    "qualified_name": "DB::X",
                    "start_line": 1,
                    "end_line": 9,
                    "tier": "scip",
                },
                {
                    "kind": "function",
                    "qualified_name": "DB::X::add",
                    "signature": "(int)",
                    "start_line": 2,
                    "end_line": 3,
                    "tier": "scip",
                },
                {
                    "kind": "function",
                    "qualified_name": f"DB::$anonymous_namespace_{path}::helper",
                    "start_line": 5,
                    "end_line": 6,
                    "tier": "treesitter",
                },
                {
                    "kind": "function",
                    "qualified_name": "DB::X::remove",
                    "start_line": 7,
                    "end_line": 8,
                    "tier": "scip",
                },
            ],
        },
    )
    assert out["module"] == "DB"
    names = [e["name"] for e in out["entries"]]
    assert names == ["X", "X::add", "(anonymous namespace)::helper", "X::remove"]
    assert out["entries"][1]["signature"] == "(int)" and out["entries"][1]["start_line"] == 2
    assert out["defaults"] == {"tier": "scip", "kind": "function"}
    assert out["entries"][0]["kind"] == "class" and out["entries"][2]["tier"] == "treesitter"
    assert "indexed" not in out
    assert out["as_of"] == {"branch": "main", "status": "fresh", "commit": "c15ee82f1"}


def test_an_unindexed_outline_still_says_so() -> None:
    out = lean_code("code.outline", {"path": "a.py", "indexed": False, "entries": []})
    assert out["indexed"] is False


def test_a_stale_index_keeps_every_staleness_field() -> None:
    stale = {
        **FRESH,
        "stale": True,
        "status": "stale",
        "lag_commits": 23,
        "stale_reason": "mirror_not_fetched",
        "fetched_at": "2026-09-24T13:17:00",
    }
    out = lean_code("code.grep", {"matches": [], "as_of": stale})
    assert out["as_of"]["stale"] is True and out["as_of"]["lag_commits"] == 23
    assert out["as_of"]["stale_reason"] == "mirror_not_fetched" and out["as_of"]["fetched_at"]


def test_search_hits_drop_ranking_internals_and_keep_the_answer() -> None:
    out = lean_code(
        "code.search",
        {
            "results": [
                {
                    "unit_id": "u",
                    "path": "p.py",
                    "start_line": 1,
                    "end_line": 4,
                    "kind": "function",
                    "body": "def f(): ...",
                    "score": 0.04,
                    "lexical_rank": 3,
                    "vector_rank": 1,
                    "via": "both",
                    "elided": False,
                    "next": "code.symbol name=f",
                }
            ],
            "abstain": False,
            "retrieval_risk": "high",
            "next_cursor": "c",
            "has_more": True,
            "route": "hybrid",
            "tokens_returned": 12,
            "as_of": FRESH,
            "trace_run_id": "t",
        },
    )
    hit = out["results"][0]
    assert set(hit) == {"path", "start_line", "end_line", "kind", "body", "next"}
    assert out["retrieval_risk"] == "high" and out["next_cursor"] == "c" and out["has_more"] is True
    assert "route" not in out and "tokens_returned" not in out and out["trace_run_id"] == "t"


def test_grep_scope_drops_its_own_path_and_enclosing_drops_the_repeated_name() -> None:
    g = lean_code(
        "code.grep",
        {
            "matches": [
                {
                    "path": "server/a/b.py",
                    "line": 3,
                    "text": "x",
                    "scope": "server.a.b::Cls::fn",
                    "scope_start_line": 1,
                    "scope_end_line": 9,
                }
            ]
        },
    )
    assert g["matches"][0]["scope"] == "Cls::fn" and g["matches"][0]["scope_start_line"] == 1
    e = lean_code(
        "code.enclosing",
        {
            "results": [
                {
                    "path": "a.py",
                    "line": 3,
                    "symbol": "fn",
                    "symbol_id": "i" * 32,
                    "qualified_name": "a::fn",
                    "start_line": 1,
                    "end_line": 5,
                },
                {"path": "doc.md", "line": 11, "start_line": 0, "end_line": 0},
            ],
            "resolved": 1,
            "took_ms": 80,
            "bodies_returned": 0,
        },
    )
    assert e["results"][0] == {
        "path": "a.py",
        "line": 3,
        "qualified_name": "a::fn",
        "start_line": 1,
        "end_line": 5,
    }
    assert e["results"][1] == {"path": "doc.md", "line": 11}
    assert e["resolved"] == 1 and "took_ms" not in e


def test_symbol_keeps_ids_only_when_several_definitions_answer() -> None:
    one = lean_code(
        "code.symbol",
        {
            "definitions": [
                {
                    "unit_id": "u1",
                    "path": "a.py",
                    "symbol": "a::f",
                    "score": 0,
                    "elided": False,
                    "recent_prs": [
                        {
                            "pr_number": 7,
                            "title": "t",
                            "merged_at": "2026-09-24 17:21:15",
                            "is_revert": False,
                        }
                    ],
                }
            ],
            "candidates": [],
            "ambiguous": False,
        },
    )
    d = one["definitions"][0]
    assert "symbol_id" not in d and "unit_id" not in d and "score" not in d
    assert d["recent_prs"] == [{"pr_number": 7, "title": "t", "merged_at": "2026-09-24"}]
    assert "candidates" not in one and "ambiguous" not in one
    many = lean_code(
        "code.symbol",
        {
            "definitions": [{"unit_id": "u1", "path": "a.py"}, {"unit_id": "u2", "path": "b.py"}],
            "ambiguous": True,
        },
    )
    assert [x["symbol_id"] for x in many["definitions"]] == ["u1", "u2"] and many["ambiguous"]


def test_other_code_ops_are_not_reshaped() -> None:
    payload = {
        "items": [{"pr_number": 1, "score": 0.47, "relation": "related"}],
        "verdict": "related",
    }
    assert lean_code("code.pr_check", payload) is payload


def test_mcp_op_result_applies_it_and_explain_skips_it() -> None:
    payload = {"edges": [_edge("a", 1), _edge("b", 2)], "trace_run_id": "t"}
    lean = lr.mcp_op_result("code.callers", payload, {"name": "seed"}, None)
    assert lean["seed"] == "m::seed" and "from_symbol_id" not in lean["edges"][0]
    full = lr.mcp_op_result("code.callers", payload, {"name": "seed", "explain": True}, None)
    assert full["edges"][0]["from_symbol_id"] and full["edges"][0]["to_symbol"] == "m::seed"


def test_a_search_hit_keeps_what_it_does_and_the_repositorys_own_callees() -> None:
    from server.transport.code_lean import lean_code

    out = lean_code(
        "code.search",
        {
            "results": [
                {
                    "path": "src/Access/EnabledQuota.cpp",
                    "description": "Tracks quota usage per query. Answers: How is usage tracked?",
                    "callees": [
                        {"name": "std::chrono::system_clock::now", "path": "x", "line": 1},
                        {"name": "DB::EnabledQuota::Impl::used", "path": "x", "line": 2},
                    ],
                },
                {"path": "b.cpp", "callees": [{"name": "boost::shared_ptr::operator*"}]},
            ]
        },
    )
    first, second = out["results"]
    assert first["description"] == "Tracks quota usage per query."
    assert first["callees"] == ["DB::EnabledQuota::Impl::used"]
    assert "callees" not in second


def test_a_class_answer_carries_its_skeleton_once_and_points_at_its_members() -> None:
    from server.transport.code_lean import lean_code

    out = lean_code(
        "code.symbol",
        {
            "definitions": [
                {
                    "path": "src/Coordination/Changelog.h",
                    "symbol": "DB::Changelog",
                    "granularity": "skeleton",
                    "body": "class Changelog { ... };",
                    "outline": [{"qualified_name": "DB::Changelog::flush"}],
                },
                {
                    "path": "src/Coordination/Changelog.cpp",
                    "symbol": "DB::Changelog::Changelog",
                    "body": "Changelog::Changelog(...) { ... }",
                },
                {
                    "path": "src/Other.h",
                    "symbol": "DB::Other::Changelog",
                    "body": "struct Changelog {};",
                },
            ]
        },
    )
    cls, ctor, other = out["definitions"]
    assert cls["body"] and "outline" not in cls
    assert "body" not in ctor and ctor["next"] == "code.symbol name=DB::Changelog::Changelog"
    assert other["body"] == "struct Changelog {};"  # another class's definition keeps its body


def test_overloads_of_a_function_keep_every_body() -> None:
    from server.transport.code_lean import lean_code

    defs = [
        {"path": "a.cpp", "symbol": "DB::f", "body": "void f(int) {}"},
        {"path": "a.cpp", "symbol": "DB::f", "body": "void f(char) {}"},
    ]
    out = lean_code("code.symbol", {"definitions": defs})
    assert [d["body"] for d in out["definitions"]] == ["void f(int) {}", "void f(char) {}"]


def test_a_long_body_becomes_a_ranged_read_and_late_hits_keep_only_where_they_are() -> None:
    from server.transport.code_lean import _BODY_MAX_CHARS, lean_code

    long_body = "x" * (_BODY_MAX_CHARS + 1)
    out = lean_code(
        "code.symbol",
        {
            "definitions": [
                {
                    "path": "a.cpp",
                    "symbol": "DB::f",
                    "start_line": 10,
                    "end_line": 59,
                    "body": long_body,
                },
                {
                    "path": "b.cpp",
                    "symbol": "DB::g",
                    "start_line": 3,
                    "end_line": 4,
                    "body": "int g();",
                    "description": "No documentation available.",
                },
            ]
        },
    )
    f, g = out["definitions"]
    assert "body" not in f and f["next"] == "Read a.cpp offset=10 limit=50"
    assert g["body"] == "int g();" and "description" not in g
    hits = [{"path": f"p{i}.cpp", "description": f"d{i}"} for i in range(5)]
    got = lean_code("code.search", {"results": hits})["results"]
    assert [h.get("description") for h in got] == ["d0", "d1", "d2", None, None]
