"""The Go gold harness in `bench/` — the comparison keys,
the scoring on a synthetic pair, and the derivation over an in-memory module."""

from __future__ import annotations

from bench.go_scip_gold import compare, derive, fast_key, gold_key, report


def test_keys_fold_the_package_clause_and_skip_libraries() -> None:
    assert fast_key("proto.proto::ColDate32::Row") == "proto::ColDate32::Row"
    assert fast_key("root.ch::Client::Do") == "root::Client::Do"
    assert (
        fast_key("proto.proto_test::helper") == "proto::helper"
    )  # the external test package folds
    assert gold_key("proto.proto::ColDate32::Row") == "proto::ColDate32::Row"
    assert gold_key("strconv::Itoa") is None  # stdlib
    assert gold_key("github.com/go-faster/errors::New") is None  # a dependency
    assert gold_key("sync::Mutex::Lock") is None
    assert gold_key("noscope") is None


def test_compare_scores_exact_wrong_missed_and_extra() -> None:
    gold = {
        "files": [
            {
                "path": "a.go",
                "edges": [
                    {"edge_kind": "calls", "line": 3, "to_qualified_name": "root.ch::Client::Do"},
                    {
                        "edge_kind": "calls",
                        "line": 4,
                        "to_qualified_name": "proto.proto::ColDate32::Row",
                    },
                    {
                        "edge_kind": "calls",
                        "line": 5,
                        "to_qualified_name": "root.ch::Client::Close",
                    },
                    {
                        "edge_kind": "calls",
                        "line": 6,
                        "to_qualified_name": "strconv::Itoa",
                    },  # not in-repo
                    {
                        "edge_kind": "inherits",
                        "from_qualified_name": "root.ch::Conn",
                        "to_qualified_name": "root.ch::Base",
                    },
                    {
                        "edge_kind": "inherits",
                        "from_qualified_name": "root.ch::Other",
                        "to_qualified_name": "root.ch::Base",
                    },
                ],
                "symbols": [
                    {"kind": "function", "line_start": 3, "name": "Do"},
                    {"kind": "function", "line_start": 9, "name": "gone"},
                ],
            },
        ]
    }
    fast = {
        "symbols": [
            {"path": "a.go", "line": 3, "name": "Do", "kind": "function"},
            {"path": "a.go", "line": 11, "name": "extra", "kind": "function"},
        ],
        "edges": [
            {
                "kind": "calls",
                "path": "a.go",
                "line": 3,
                "to": "root.ch::Client::Do",
                "conf": 0.9,
            },  # exact
            {
                "kind": "calls",
                "path": "a.go",
                "line": 4,
                "to": "proto.proto::Other::Row",
                "conf": 1.0,
            },  # wrong type
            {
                "kind": "calls",
                "path": "a.go",
                "line": 7,
                "to": "root.ch::Client::Ping",
                "conf": 0.6,
            },  # no gold
            {
                "kind": "inherits",
                "path": "a.go",
                "line": 1,
                "from": "root.ch::Conn",
                "to": "root.ch::Base",
                "conf": 1.0,
            },
            {
                "kind": "inherits",
                "path": "a.go",
                "line": 2,
                "from": "root.ch::Conn",
                "to": "root.ch::Nope",
                "conf": 0.6,
            },
        ],
    }
    scores = compare(fast, gold)
    assert scores["calls"] == {
        "gold": 3,
        "exact": 1,
        "exact_rate": 0.333,
        "wrong_type": 1,
        "missed": 1,
        "extra_lines": 1,
        "exact_by_confidence": {0.9: 1},
        "wrong_by_confidence": {1.0: 1},
    }
    inh = scores["inherits"]
    assert (inh["gold"], inh["ours"], inh["both"], inh["recall"], inh["precision"]) == (
        2,
        2,
        1,
        0.5,
        0.5,
    )
    fn = scores["functions"]
    assert (fn["gold"], fn["ours"], fn["both"], fn["ours_only"], fn["gold_only"]) == (2, 2, 1, 1, 1)
    text = report(scores)
    assert "exact 1 (0.333)" in text and "same name, other type 1" in text and "recall 0.5" in text


def test_derive_runs_the_fast_tier_over_a_checkout_in_memory() -> None:
    files = {
        "go.mod": b"module example.com/m\n",
        "core/base.go": b'package core\ntype Base struct{}\nfunc (b *Base) Describe() string { return "" }\n',
        "core/t.go": b"package core\ntype T struct{ *Base }\nfunc NewT() *T { return &T{} }\n",
        "core/other.go": b'package core\ntype Other struct{}\nfunc (o Other) Describe() string { return "o" }\n',
        "app/main.go": (
            b'package main\nimport "example.com/m/core"\n'
            b"func run() {\n\tt := core.NewT()\n\tt.Describe()\n}\n"
        ),
    }
    out = derive(files)
    assert out["files"] == 4
    calls = {
        (e["path"], e["line"], e["to"], e["conf"]) for e in out["edges"] if e["kind"] == "calls"
    }
    assert ("app/main.go", 4, "core.core::NewT", 1.0) in calls  # import-bound
    assert (
        "app/main.go",
        5,
        "core.core::Base::Describe",
        0.9,
    ) in calls  # typed by the return, promoted
    assert ("core.core::T", "core.core::Base") in {
        (e["from"], e["to"]) for e in out["edges"] if e["kind"] == "inherits"
    }
    assert out["misses"] == []
    assert {s["kind"] for s in out["symbols"]} >= {"struct", "function", "field"}
