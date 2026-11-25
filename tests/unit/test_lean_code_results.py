"""The MCP shape of a code.* result keeps what an agent acts
on (path, line, symbol, signature, body, next, every correctness signal)
and drops the bookkeeping of how it was found."""

from __future__ import annotations

import json
from typing import Any

import server.transport.lean_results as lr
from server.transport.code_lean import lean_code


def test_symbol_drops_only_descriptions_repeated_in_the_returned_body() -> None:
    body = 'def f():\n    """Compute the total."""\n    return 1\n'
    out = lean_code(
        "code.symbol",
        {
            "definitions": [
                {"symbol": "f", "body": body, "description": "Compute the total."},
                {"symbol": "g", "body": body, "description": "Additional context."},
                {"symbol": "h", "description": "Compute the total."},
            ]
        },
    )
    f, g, h = out["definitions"]
    assert f["body"] == body and "description" not in f
    assert g["description"] == "Additional context."
    assert h["description"] == "Compute the total."


def test_scip_symbol_drops_rendered_description_already_in_source() -> None:
    body = '''class Group(Base):
    """Extend :class:`Base`.

    :param load_defaults: Load the high priority
        options on startup.
    """
'''
    rendered = (
        "```python\nclass Group(Base):\n```\nExtend `Base`.\n\n"
        r":param load\_defaults: Load the high priority"
        "  \n&nbsp;&nbsp;&nbsp;&nbsp;options on startup."
    )
    source = {"symbol": "Group", "tier": "scip", "body": body, "description": rendered}
    out = lean_code(
        "code.symbol",
        {
            "definitions": [
                source,
                {**source, "description": rendered + " Bypass startup on empty input."},
                {**source, "description": rendered.replace("class Group", "class Other")},
                {**source, "elided": True},
            ]
        },
    )
    repeated, extra, other, elided = out["definitions"]
    assert repeated["body"] == body and "description" not in repeated
    assert extra["description"] == rendered + " Bypass startup on empty input."
    assert other["description"] == rendered.replace("class Group", "class Other")
    assert elided["description"] == rendered


def test_scip_symbol_keeps_distinct_code_literals_in_docs() -> None:
    descriptions = (
        ('Send marker `"a  b"`.', 'Send marker `"a b"`.'),
        ('Send marker `"a\\tb"`.', 'Send marker `"a b"`.'),
        ('Send marker ``"a  b`x"``.', 'Send marker ``"a b`x"``.'),
        ('Example:\n\n        print("a  b")', 'Example:\n\n    print("a b")'),
    )
    for doc, different_doc in descriptions:
        body = f'def send():\n    """{doc}"""\n    pass\n'
        rendered = f"```python\ndef send():\n```\n{different_doc}"
        row = {"symbol": "send", "tier": "scip", "body": body, "description": rendered}
        out = lean_code("code.symbol", {"definitions": [row]})
        assert out["definitions"][0]["description"] == rendered


def test_scip_symbol_keeps_short_or_nested_descriptions() -> None:
    body = 'def send():\n    """Returns success=False after persisting."""\n    def resolve():\n        pass\n'
    excerpt = "```python\ndef send():\n```\nReturns success"
    nested = "```python\ndef resolve():\n```\nReturns success=False after persisting."
    row = {"symbol": "send", "tier": "scip", "body": body}
    for description in (excerpt, nested):
        out = lean_code("code.symbol", {"definitions": [{**row, "description": description}]})
        assert out["definitions"][0]["description"] == description


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


def test_related_grep_keeps_source_and_compacts_it_like_exact_hits() -> None:
    match = {
        "path": "server/a.py",
        "line": 3,
        "text": "item.value",
        "scope": "server.a::inspect",
        "scope_start_line": 2,
        "scope_end_line": 4,
    }
    related = {
        "pattern": "item.value",
        "matches": [match],
        "contexts": [{"path": "server/a.py", "start_line": 3, "text": "item.value"}],
        "definitions": [
            {"path": "server/a.py", "symbol_id": name, "kind": "function", "body": name}
            for name in ("inspect", "count", "update")
        ],
        "definitions_partial": True,
        "truncated": True,
    }
    out = lean_code("code.grep", {"matches": [], "related": related, "trace_run_id": "trace"})
    assert out["matches"] == [] and out["trace_run_id"] == "trace"
    assert out["related"] == lean_code("code.grep", related)
    assert out["related"]["matches"][0]["scope"] == "inspect"
    assert match["scope"] == "server.a::inspect"
    assert out["related"]["contexts"] == related["contexts"]
    assert out["related"]["definitions_partial"] is True and out["related"]["truncated"] is True
    assert out["related"]["defaults"] == {"kind": "function"}
    assert [row["body"] for row in out["related"]["definitions"]] == ["inspect", "count", "update"]
    assert [row["symbol_id"] for row in out["related"]["definitions"]] == [
        "inspect",
        "count",
        "update",
    ]


def test_lean_grep_omits_redundant_metadata_only_with_complete_source() -> None:
    body = "    def inspect(self, value: str):\n        return value"
    definition = {
        "path": "worker.py",
        "symbol": "inspect",
        "symbol_id": "i1",
        "qualified_name": "worker::Task::inspect",
        "signature": "(self, value: str)",
        "body": body,
        "body_elided": False,
        "lines": [7],
    }
    elided = {**definition, "body": "", "body_elided": True}
    different_signature = {**definition, "signature": "(self, value: str) -> str"}
    unrelated_short_name = {**definition, "symbol": "Task.inspect"}
    payload = {
        "definitions": [definition, elided, different_signature, unrelated_short_name],
        "related": {"definitions": [definition]},
    }
    lean = lean_code("code.grep", payload)
    expected = {
        "path": "worker.py",
        "symbol_id": "i1",
        "qualified_name": "worker::Task::inspect",
        "body": body,
        "lines": [7],
    }
    assert lean["definitions"] == [
        expected,
        elided,
        {**expected, "signature": "(self, value: str) -> str"},
        {**expected, "symbol": "Task.inspect"},
    ]
    assert lean["related"]["definitions"] == [expected]
    assert definition["signature"] == "(self, value: str)"
    assert definition["body_elided"] is False


def test_lean_grep_omits_verbatim_multiline_signatures_at_the_declaration() -> None:
    signature = "(\n        self,\n        value: str,\n    )"
    body = f"    def inspect{signature}:\n        return value"
    declaration = {"signature": signature, "body": body}
    inside_body = {
        "signature": signature,
        "body": f"    def inspect(self):\n        call{signature}",
    }
    elided = {**declaration, "body_elided": True}
    result = lean_code("code.grep", {"definitions": [declaration, inside_body, elided]})
    assert result["definitions"] == [{"body": body}, inside_body, elided]


def test_lean_grep_refs_exact_nested_source_without_losing_a_body() -> None:
    method = {
        "path": "a.py",
        "symbol_id": "method",
        "start_line": 3,
        "end_line": 4,
        "body": "        def read(self):\r\n            return 'source\u2028data'\r\n",
    }
    inner = {
        "path": "a.py",
        "symbol_id": "inner",
        "start_line": 2,
        "end_line": 4,
        "body": "    class Inner:\r\n" + method["body"],
    }
    outer = {
        "path": "a.py",
        "symbol_id": "outer",
        "start_line": 1,
        "end_line": 4,
        "body": "class Outer:\r\n" + inner["body"],
    }
    other_file = {**method, "symbol_id": "other", "path": "b.py"}
    altered = {**method, "symbol_id": "changed", "body": method["body"].replace("data", "code")}
    elided = {**method, "symbol_id": "elided", "body_elided": True}
    payload = {
        "definitions": [method, inner, outer, other_file, altered, elided],
        "definitions_note": "One definition was elided.",
    }

    result = lean_code("code.grep", payload)

    rows = {row["symbol_id"]: row for row in result["definitions"]}
    assert rows["method"]["body_in_symbol_id"] == rows["inner"]["body_in_symbol_id"] == "outer"
    assert "body" not in rows["method"] and "body" not in rows["inner"]
    assert [rows[r["symbol_id"]]["body"] for r in (outer, other_file, altered, elided)] == [
        r["body"] for r in (outer, other_file, altered, elided)
    ]
    assert result["definitions_note"] == payload["definitions_note"]
    assert method["body"] and inner["body"]


def test_lean_grep_near_output_boundary_only_shrinks_the_page() -> None:
    cases = (
        ("    def nested(self):\n        return ''\n", 3, False),
        ("    def nested(self):\n        return 'é'\n", 3, False),
        ('    def f(): return "界界界界界界界界"\n', 2, False),
        (f"    def nested(self):\n        return '{'x' * 21}'\n", 3, True),
    )
    for body, end_line, smaller in cases:
        child = {
            "path": "sample.py",
            "symbol_id": "a" * 32,
            "start_line": 2,
            "end_line": end_line,
            "body": body,
        }
        parent = {
            **child,
            "symbol_id": "b" * 32,
            "start_line": 1,
            "body": "class Outer:\n" + body,
        }
        payload = {"definitions": [parent, child], "definitions_note": ""}
        payload["definitions_note"] = "x" * (49_970 - len(json.dumps(payload)))

        result = lean_code("code.grep", payload)

        for ascii_only, encoding in ((True, "utf-8"), (False, "utf-8"), (False, "utf-16-le")):
            before = json.dumps(payload, ensure_ascii=ascii_only).encode(encoding)
            after = json.dumps(result, ensure_ascii=ascii_only).encode(encoding)
            assert len(after) <= len(before)
        assert result["definitions_note"] == payload["definitions_note"]
        if smaller:
            assert result["definitions"][1]["body_in_symbol_id"] == parent["symbol_id"]
        else:
            assert result["definitions"][1] == child


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


def test_edit_context_batch_keeps_an_explicit_member_body() -> None:
    from server.transport.code_lean import lean_code

    member = {"symbol": "DB::Log::flush", "body": "void Log::flush() {}"}
    result = lean_code(
        "code.edit_context",
        {
            "definitions": [
                {"symbol": "DB::Log", "granularity": "skeleton", "body": "class Log {};"},
                member,
            ],
            "edit_targets": [
                {"name": "DB::Log", "definition_indices": [0]},
                {"name": "DB::Log::flush", "definition_indices": [1]},
            ],
        },
    )
    assert result["definitions"][1] == member


def test_overloads_of_a_function_keep_every_body() -> None:
    from server.transport.code_lean import lean_code

    defs = [
        {"path": "a.cpp", "symbol": "DB::f", "body": "void f(int) {}"},
        {"path": "a.cpp", "symbol": "DB::f", "body": "void f(char) {}"},
    ]
    out = lean_code("code.symbol", {"definitions": defs})
    assert [d["body"] for d in out["definitions"]] == ["void f(int) {}", "void f(char) {}"]


def test_a_long_body_is_preserved_and_late_hits_keep_only_where_they_are() -> None:
    from server.transport.code_lean import lean_code

    long_body = "x" * 12000
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
    assert f["body"] == long_body
    assert not f.get("elided") and not f.get("next")
    assert g["body"] == "int g();" and "description" not in g
    hits = [{"path": f"p{i}.cpp", "description": f"d{i}"} for i in range(5)]
    got = lean_code("code.search", {"results": hits})["results"]
    assert [h.get("description") for h in got] == ["d0", "d1", "d2", None, None]
