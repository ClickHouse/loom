"""The MCP shape of a code.* result keeps what an agent acts
on (path, line, symbol, signature, body, next, every correctness signal)
and drops the bookkeeping of how it was found."""

from __future__ import annotations

import json
from typing import Any

import server.transport.lean_results as lr
from server.code.domain.scope import reset_code_scope, set_code_scope
from server.settings import settings
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


def test_edit_context_shares_identical_test_source_and_preserves_evidence() -> None:
    context = {
        "commit": "a" * 40,
        "start_line": 1,
        "end_line": 1,
        "text": "test source " * 32,
    }
    verified = {
        "path": "tests/test_use.py",
        "line": 1,
        "caller": "test_use",
        "context": context,
        "tier": "scip",
    }
    duplicate = {**verified, "evidence": "indirect_call"}
    earlier = {**verified, "context": {**context, "commit": "b" * 40}}
    short = {**verified, "context": {**context, "text": "ok"}}
    payload = {
        "definitions": [
            {"symbol": "entry", "path": "src/entry.py", "examples": [verified, short]},
            {"symbol": "impl", "path": "src/impl.py", "examples": [duplicate, earlier, short]},
        ],
        "edit_targets": [{"name": "entry"}, {"name": "impl"}],
    }

    result = lr.mcp_op_result("code.edit_context", payload, {}, None)
    original, repeated = result["definitions"]
    first, previous, small = repeated["examples"]
    assert next(iter(result)) == "test_sources"
    assert result["test_sources"] == [
        {
            "excerpts": [
                {
                    "path": "tests/test_use.py",
                    "callers": ["test_use"],
                    "definitions": [
                        {"symbol": "entry", "path": "src/entry.py"},
                        {"symbol": "impl", "path": "src/impl.py"},
                    ],
                    "text": context["text"],
                }
            ]
        }
    ]
    assert first["evidence"] == "indirect_call"
    assert first["context"] == {
        "commit": context["commit"],
        "start_line": 1,
        "end_line": 1,
        "text_ref": "#/test_sources/0/excerpts/0/text",
    }
    assert original["examples"][0] == {**verified, "context": first["context"]}
    assert previous == earlier and small == short
    assert payload["definitions"][1]["examples"][0] == duplicate
    assert (
        "text_ref"
        not in lr.mcp_op_result("code.symbol", payload, {}, None)["definitions"][1]["examples"][0][
            "context"
        ]
    )


def test_edit_context_keeps_source_when_shared_row_would_exceed_an_original_row() -> None:
    context = {"commit": "a" * 40, "start_line": 1, "end_line": 1}
    short = {"path": "tests/test_short.py", "context": {**context, "text": "alpha " * 45}}
    long = {"path": "tests/test_long.py", "context": {**context, "text": "feature " * 100}}
    payload = {
        "definitions": [
            {"symbol": "first", "examples": [short, long]},
            {"symbol": "second", "examples": [short]},
            {"symbol": "third", "examples": [long]},
        ],
        "edit_targets": [{"name": name} for name in ("first", "second", "third")],
    }

    result = lr.mcp_op_result("code.edit_context", payload, {}, None)
    assert "test_sources" not in result
    assert result["definitions"][1]["examples"] == [short]


def test_grep_excerpts_offer_a_batch_for_unread_declarations(monkeypatch) -> None:
    long = {
        "path": "src/a.py",
        "line": 5,
        "text": "def first(value):",
        "scope": "first",
        "scope_start_line": 5,
        "scope_end_line": 16,
    }
    short = {
        "path": "src/b.py",
        "line": 2,
        "scope": "quick",
        "scope_start_line": 2,
        "scope_end_line": 3,
    }
    unusual = ["src/plugins v2/routes.py", "scripts/bootstrap", "docs/overview.md"]
    more = [{**long, "path": path} for path in (*unusual, "examples/demo.IPYNB", "a" * 1025)]
    async_def = {**long, "path": "src/support.py", "text": "async def first(value):"}
    payload = {
        "matches": [long, {**long, "line": 7}, long, short, async_def, *more],
        "contexts": [
            {"path": "src/a.py", "start_line": 4, "end_line": 8, "text": "partial"},
            {"path": "src/b.py", "start_line": 1, "end_line": 4, "text": "complete"},
        ],
    }
    options = {
        "org": "secondary",
        "namespace": "repo",
        "include_lessons": False,
        "lesson_namespaces": ["history"],
    }
    arguments = {"include_body": False, **options}
    edit_payload = {
        **payload,
        "matches": [hit for hit in payload["matches"] if hit["path"] not in unusual],
    }
    result = lr.mcp_op_result("code.grep", edit_payload, arguments, None, toolset="coding")
    edit_step = result.pop("edit_context_next")
    assert edit_step == {
        "tool": "loom_code_edit_context",
        "arguments": {"names": ["src/a.py:5", "src/support.py:5"], **options},
    }
    suggestion = result.pop("next")
    label, _, params = suggestion.partition(" with ")
    assert label == "For the full source, call loom_code_read"
    assert json.loads(params) == {
        "ranges": [
            {"path": path, "start_line": 5, "end_line": 16}
            for path in ("src/a.py", "src/support.py")
        ],
        **options,
    }
    with_read = lr.mcp_op_result("code.grep", payload, arguments, None, toolset="coding")
    assert "edit_context_next" not in with_read
    label, _, params = with_read.pop("next").partition(" with ")
    assert label == "For the full source, call loom_code_read"
    assert json.loads(params) == {
        "ranges": [
            {"path": path, "start_line": 5, "end_line": 16}
            for path in ("src/a.py", "src/support.py", *unusual)
        ],
        **options,
    }
    assert with_read == lr.mcp_op_result("code.grep", payload, arguments, None, toolset="all")
    assert result == lr.mcp_op_result("code.grep", edit_payload, {"include_body": True}, None)
    assert "next" not in lr.mcp_op_result(
        "code.grep", {**payload, "contexts": []}, arguments, None, toolset="coding"
    )
    assert "next" not in lr.mcp_op_result("code.grep", payload, arguments, None, toolset="core")
    monkeypatch.setattr(settings, "mcp_toolset", "coding")
    for toolset in (None, "Coding", "coding+tracker"):
        assert lr.mcp_op_result("code.grep", edit_payload, arguments, None, toolset=toolset) == {
            **result,
            "edit_context_next": edit_step,
            "next": suggestion,
        }


def test_grep_keeps_exact_ranges_for_decorators_and_batches_that_exceed_named_lookup() -> None:
    declaration = {
        "path": "src/a.py",
        "line": 1,
        "text": "def worker():",
        "scope": "worker",
        "scope_start_line": 1,
        "scope_end_line": 12,
    }
    for matches in (
        [{**declaration, "text": "@marker"}],
        [{**declaration, "line": 2, "text": "@marker", "scope": "src.a::worker"}],
        [{**declaration, "path": f"src/module{index}.py"} for index in range(9)],
    ):
        payload = {
            "matches": matches,
            "contexts": [{"path": "src/a.py", "start_line": 1, "end_line": 2, "text": "partial"}],
        }
        result = lr.mcp_op_result(
            "code.grep", payload, {"include_body": False}, None, toolset="coding"
        )
        assert "edit_context_next" not in result
        label, _, params = result["next"].partition(" with ")
        assert label == "For the full source, call loom_code_read"
        assert json.loads(params) == {
            "ranges": [{"path": hit["path"], "start_line": 1, "end_line": 12} for hit in matches]
        }


def test_grep_offers_edit_context_for_declarations_without_context_windows(monkeypatch) -> None:
    declaration = {
        "path": "src/a.py",
        "line": 5,
        "text": "def worker():",
        "scope": "worker",
        "scope_start_line": 5,
        "scope_end_line": 12,
    }
    payload = {"matches": [declaration, {**declaration, "line": 8, "text": "    worker()"}]}
    result = lr.mcp_op_result("code.grep", payload, {"include_body": False}, None, toolset="coding")
    assert result["matches"] == payload["matches"]
    assert result["edit_context_next"] == {
        "tool": "loom_code_edit_context",
        "arguments": {"names": ["src/a.py:5"]},
    }
    assert json.loads(result["next"].partition(" with ")[2]) == {
        "ranges": [{"path": "src/a.py", "start_line": 5, "end_line": 12}],
    }
    token = set_code_scope("attached/repo")
    try:
        arguments = {"include_body": False, "org": "attached", "namespace": "repo"}
        monkeypatch.setattr(settings, "cell", "")
        assert lr.mcp_op_result("code.grep", payload, arguments, None, toolset="coding") == result
        override = lr.mcp_op_result(
            "code.grep", payload, {**arguments, "namespace": "other"}, None, toolset="coding"
        )
        assert override["edit_context_next"]["arguments"] == {
            "names": ["src/a.py:5"],
            "org": "attached",
            "namespace": "other",
        }
        assert json.loads(override["next"].partition(" with ")[2])["namespace"] == "other"
        monkeypatch.setattr(settings, "cell", "local-cell")
        on_cell = lr.mcp_op_result("code.grep", payload, arguments, None, toolset="coding")
        assert on_cell["edit_context_next"]["arguments"] == {
            "names": ["src/a.py:5"],
            "org": "attached",
            "namespace": "repo",
        }
        assert json.loads(on_cell["next"].partition(" with ")[2])["org"] == "attached"
    finally:
        reset_code_scope(token)


def test_grep_follows_matches_inside_an_indexed_scope() -> None:
    path = "src/config.py"
    inside = {
        "path": path,
        "line": 13,
        "text": "value = read_config()",
        "scope": "src.config::Config::load",
        "scope_start_line": 9,
        "scope_end_line": 16,
    }
    declaration = {
        "path": path,
        "line": 22,
        "text": "def validate():",
        "scope": "src.config::validate",
        "scope_start_line": 22,
        "scope_end_line": 26,
    }
    payload = {
        "matches": [inside, {**inside, "line": 14}, declaration],
        "contexts": [{"path": path, "start_line": 12, "end_line": 15, "text": "excerpt"}],
    }
    result = lr.mcp_op_result("code.grep", payload, {"include_body": False}, None, toolset="coding")
    assert [hit["scope"] for hit in result["matches"]] == [
        "Config::load",
        "Config::load",
        "validate",
    ]
    assert result["edit_context_next"]["arguments"] == {
        "names": [f"{path}:22", f"{path}:src.config::Config::load"],
    }
    assert json.loads(result["next"].partition(" with ")[2]) == {
        "ranges": [
            {"path": path, "start_line": 22, "end_line": 26},
            {"path": path, "start_line": 9, "end_line": 16},
        ],
    }
    body_hits = [
        {**inside, "path": f"src/file{i}.py", "scope": f"src.file{i}::Config::load"}
        for i in range(9)
    ]
    wide = {"matches": [declaration, *body_hits]}
    result = lr.mcp_op_result("code.grep", wide, {"include_body": False}, None, toolset="coding")
    assert result["edit_context_next"]["arguments"] == {"names": [f"{path}:22"]}
    assert json.loads(result["next"].partition(" with ")[2]) == {
        "ranges": [{"path": path, "start_line": 22, "end_line": 26}],
    }


def test_grep_edit_followup_keeps_the_indexed_python_module() -> None:
    for path, scope, display in (
        ("src/demo.py", "src.demo::run", "run"),
        ("src/demo.pyi", "src.demo.__stub__::run", "src.demo.__stub__::run"),
        ("src/build-doc.py", "src.build-doc::run", "run"),
    ):
        payload = {
            "matches": [
                {
                    "path": path,
                    "line": 7,
                    "text": "return value",
                    "scope": scope,
                    "scope_start_line": 5,
                    "scope_end_line": 8,
                },
            ],
        }
        result = lr.mcp_op_result(
            "code.grep", payload, {"include_body": False}, None, toolset="coding"
        )
        assert result["matches"][0]["scope"] == display
        assert result["edit_context_next"]["arguments"] == {"names": [f"{path}:{scope}"]}


def test_grep_offers_the_exact_range_when_the_scope_cannot_be_named() -> None:
    for path, scope, text in (
        ("demo.py", "demo::run", "return value"),
        ("src/demo.py", "src.demo::run", "@marker"),
    ):
        payload = {
            "matches": [
                {
                    "path": path,
                    "line": 7,
                    "text": text,
                    "scope": scope,
                    "scope_start_line": 5,
                    "scope_end_line": 8,
                },
            ],
        }
        result = lr.mcp_op_result(
            "code.grep", payload, {"include_body": False}, None, toolset="coding"
        )
        assert "edit_context_next" not in result
        assert json.loads(result["next"].partition(" with ")[2]) == {
            "ranges": [{"path": path, "start_line": 5, "end_line": 8}],
        }


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
