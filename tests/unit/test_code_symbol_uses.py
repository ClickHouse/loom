"""code.symbol(uses=true): the definition and the lines that
mention its name in one call. Flag off leaves the served fields untouched;
flag on greps the bare name the way code.grep does, attaches the enclosing
scope, drops the definition's own span, caps and says when it capped."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from server.settings import settings

DEFS = [
    # tree-sitter row: the full span
    {
        "symbol_id": "f1",
        "qualified_name": "DB::Foo::bar",
        "name": "bar",
        "signature": "void bar(int)",
        "path": "src/Foo.cpp",
        "start_line": 10,
        "end_line": 20,
        "tier": "treesitter",
        "kind": "method",
    },
    # compiler row for the same definition: single-line span at the declaration
    {
        "symbol_id": "f1s",
        "qualified_name": "DB::Foo::bar",
        "name": "bar",
        "signature": "void bar(int)",
        "path": "src/Foo.h",
        "start_line": 5,
        "end_line": 5,
        "tier": "scip",
        "kind": "method",
    },
]

CHUNKS = [
    # the definition itself: every mention inside it is NOT a use
    {
        "path": "src/Foo.cpp",
        "scope": "DB::Foo::bar",
        "symbol_ids": ["f1"],
        "start_line": 10,
        "end_line": 20,
        "content": "void Foo::bar(int x)\n{\n    // bar does things\n    bar_impl(x);\n}",
    },
    # the declaration line in the header (scip span 5..5) and a use two lines below it
    {
        "path": "src/Foo.h",
        "scope": "DB::Foo",
        "symbol_ids": ["c1"],
        "start_line": 3,
        "end_line": 8,
        "content": "class Foo {\n    int q;\n    void bar(int);\n    void baz();\n    void qux() { bar(1); }\n};",
    },
    # a caller in another file
    {
        "path": "src/Use.cpp",
        "scope": "DB::use",
        "symbol_ids": ["u1"],
        "start_line": 40,
        "end_line": 44,
        "content": "void use(Foo& f)\n{\n    f.bar(2);\n    // calls bar twice\n    f.bar(3);\n}",
    },
    # an include line and a comment-only mention
    {
        "path": "src/Other.cpp",
        "scope": "",
        "symbol_ids": [],
        "start_line": 1,
        "end_line": 3,
        "content": '#include "bar.h"\n/* bar is documented elsewhere */\nint z;',
    },
]


class _FakeRepo:
    def __init__(self, chunks: list[dict[str, Any]] | None = None) -> None:
        self.chunks = CHUNKS if chunks is None else chunks
        self.grep_calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_grep = False

    async def symbols_by_name(self, org, ns, name, limit=10):
        return list(DEFS)

    async def symbols_by_ids(self, org, ns, ids):
        return [r for r in DEFS if r["symbol_id"] in ids]

    async def symbols_fuzzy(self, org, ns, needle, *, limit=25):
        return []

    async def functions_for_paths(self, org, ns, paths, *, limit=400, **_kw):
        return [r for r in DEFS if r["path"] in paths]

    async def grep_chunks(self, org, ns, pattern, **kw):
        self.grep_calls.append((pattern, kw))
        if self.fail_grep:
            raise RuntimeError("chunk table unavailable")
        return [c for c in self.chunks if pattern in str(c["content"])][: kw.get("limit", 60)]

    async def all_state(self, org, ns):
        return {}


@pytest.fixture(autouse=True)
def _intel_on(monkeypatch):
    monkeypatch.setattr(settings, "code_intel_enabled", True)
    monkeypatch.setattr(settings, "code_org", "o")
    monkeypatch.setattr(settings, "code_namespace", "n")


def _run(repo: _FakeRepo, **fields: Any) -> Any:
    from server.code.api.models_symbol import CodeSymbolInput
    from server.code.app.symbol import outline
    from server.code.app.symbol import symbol as h

    async def body(r, org, ns, sym):
        return f"<body {sym['symbol_id']}>"

    async def go():
        with (
            patch.object(h, "CodeRepo", return_value=repo),
            patch.object(h, "symbol_body", new=body),
            patch.object(outline, "symbol_body", new=body),
            patch.object(h, "schedule_code_trace", new=lambda **k: None),
        ):
            return await h.handle_code_symbol(
                CodeSymbolInput(
                    include_history=False,
                    include_examples=False,
                    include_lessons=False,
                    **fields,
                )
            )

    return asyncio.run(go())


def _keys(uses: list[dict[str, Any]]) -> list[tuple[str, int]]:
    return [(u["path"], u["line"]) for u in uses]


def test_flag_off_serves_the_same_fields_and_never_greps():
    repo = _FakeRepo()
    out = _run(repo, name="DB::Foo::bar")
    assert repo.grep_calls == [], "the flag off must not read the chunk table"
    assert out.uses == [] and out.uses_truncated is False
    # every other field is unchanged: the same call without the two uses
    # fields in the model dump is the plain response
    dump = out.model_dump(mode="json")
    old_shape = {k: v for k, v in dump.items() if k not in {"uses", "uses_truncated"}}
    assert set(dump) - set(old_shape) == {"uses", "uses_truncated"}
    assert old_shape["definitions"] and old_shape["tokens_returned"] > 0


def test_flag_on_returns_uses_with_scope_minus_definition_spans():
    repo = _FakeRepo()
    out = _run(repo, name="DB::Foo::bar", uses=True)
    keys = _keys(out.uses)
    # the caller's two mentions, the header's qux() call, the include and the comment
    assert ("src/Use.cpp", 42) in keys and ("src/Use.cpp", 44) in keys
    assert ("src/Foo.h", 7) in keys
    # nothing from inside the tree-sitter span 10..20 (3 mentions there)
    assert not any(p == "src/Foo.cpp" for p, _ in keys)
    # the compiler row's single-line span drops the declaration line only
    assert ("src/Foo.h", 5) not in keys
    assert keys == sorted(keys), "path/line ordered"
    by_key = {(u["path"], u["line"]): u for u in out.uses}
    assert by_key[("src/Use.cpp", 42)]["scope"] == "DB::use"
    assert by_key[("src/Use.cpp", 42)]["scope_start_line"] == 40
    assert by_key[("src/Use.cpp", 42)]["scope_end_line"] == 44
    assert by_key[("src/Use.cpp", 42)]["text"] == "f.bar(2);"
    assert out.uses_truncated is False
    assert out.definitions, "the definition still ships beside the uses"


def test_line_kind_labels_include_comment_code():
    repo = _FakeRepo()
    out = _run(repo, name="DB::Foo::bar", uses=True)
    kinds = {(u["path"], u["line"]): u["kind"] for u in out.uses}
    assert kinds[("src/Other.cpp", 1)] == "include"
    assert kinds[("src/Other.cpp", 2)] == "comment"
    assert kinds[("src/Use.cpp", 43)] == "comment"
    assert kinds[("src/Use.cpp", 42)] == "code"


def test_qualified_name_greps_its_bare_tail_case_sensitive():
    repo = _FakeRepo()
    _run(repo, name="DB::Foo::bar", uses=True)
    assert [p for p, _ in repo.grep_calls] == ["bar"]
    kw = repo.grep_calls[0][1]
    assert kw["case_sensitive"] is True
    assert kw["limit"] == 150, "3x the default cap of 50, the grep handler's over-fetch shape"


def test_cap_and_truncated_flag():
    many = [
        {
            "path": f"src/U{i:03d}.cpp",
            "scope": f"DB::u{i}",
            "symbol_ids": [],
            "start_line": 1,
            "end_line": 2,
            "content": f"bar({i});\nbar({i} + 1);",
        }
        for i in range(10)
    ]
    repo = _FakeRepo(many)
    out = _run(repo, name="bar", uses=True, uses_limit=5)
    assert len(out.uses) == 5 and out.uses_truncated is True
    assert repo.grep_calls[0][1]["limit"] == 15
    # the cap holds at the first five lines in path/line order
    assert _keys(out.uses) == [
        ("src/U000.cpp", 1),
        ("src/U000.cpp", 2),
        ("src/U001.cpp", 1),
        ("src/U001.cpp", 2),
        ("src/U002.cpp", 1),
    ]
    # a fetch that filled the chunk over-fetch is truncated even under the line cap
    repo2 = _FakeRepo(many[:3])
    out2 = _run(repo2, name="bar", uses=True, uses_limit=1)
    assert out2.uses_truncated is True and len(out2.uses) == 1


def test_uses_match_what_a_separate_grep_returns():
    """The gate in unit form: every (path, line, text) a code.grep on the
    bare name serves is in `uses`, except the lines inside the definition's
    own span, which uses drops by design."""
    from server.code.api.models_grep import CodeGrepInput
    from server.code.app import grep as grep_mod

    class _GrepRepo:
        async def grep_chunks(self, org, ns, pattern, **kw):
            return [c for c in CHUNKS if pattern in str(c["content"])]

        async def symbols_by_ids(self, org, ns, ids):
            return [r for r in DEFS if r["symbol_id"] in ids]

        async def all_state(self, org, ns):
            return {}

    async def _empty(*a, **k):
        return {}

    async def grep():
        with (
            patch.object(grep_mod, "CodeRepo", return_value=_GrepRepo()),
            patch.object(grep_mod, "index_as_of", new=_empty),
        ):
            return await grep_mod.handle_code_grep(
                CodeGrepInput(org="o", namespace="n", pattern="bar", limit=200)
            )

    grep_out = asyncio.run(grep())
    uses_out = _run(_FakeRepo(), name="DB::Foo::bar", uses=True, uses_limit=200)
    spans = [(u.path, u.start_line, u.end_line) for u in uses_out.definitions]
    expected = {
        (m["path"], m["line"], m["text"], m["scope"], m["scope_start_line"], m["scope_end_line"])
        for m in grep_out.matches
        if not any(p == m["path"] and lo <= m["line"] <= hi for p, lo, hi in spans)
    }
    got = {
        (u["path"], u["line"], u["text"], u["scope"], u["scope_start_line"], u["scope_end_line"])
        for u in uses_out.uses
    }
    assert expected and got == expected


def test_path_line_lookup_greps_the_resolved_names_not_the_location():
    repo = _FakeRepo()
    out = _run(repo, name="src/Foo.cpp:12", uses=True)
    assert [p for p, _ in repo.grep_calls] == ["bar"]
    assert ("src/Use.cpp", 42) in _keys(out.uses)


def test_uses_leg_failure_is_never_fatal():
    repo = _FakeRepo()
    repo.fail_grep = True
    out = _run(repo, name="DB::Foo::bar", uses=True)
    assert out.definitions and out.uses == [] and out.uses_truncated is False


def test_bare_names_skip_lambdas_and_dedupe():
    from server.code.app.symbol.uses import _bare_names

    rows = [
        {"name": "bar", "qualified_name": "DB::Foo::bar"},
        {"name": "", "qualified_name": "DB::Foo::bar"},
        {"name": "$lambda", "qualified_name": "DB::Foo::bar::$lambda"},
        {"name": "x", "qualified_name": "x"},
        {"name": "", "qualified_name": "DB::Foo::baz"},
    ]
    assert _bare_names(rows) == ["bar", "baz"]


def test_sdk_sends_uses_fields_only_when_asked():
    from sdk.python.loom.code import CodeClient

    sent: list[dict[str, Any]] = []

    class _Transport:
        async def post(self, path, body):
            sent.append(body)
            return {}

    class _Agent:
        transport = _Transport()

    api = CodeClient.__new__(CodeClient)
    api._agent = _Agent()
    asyncio.run(api.symbol("DB::Foo::bar"))
    asyncio.run(api.symbol("DB::Foo::bar", uses=True, uses_limit=20))
    assert "uses" not in sent[0] and "uses_limit" not in sent[0]
    assert sent[1]["uses"] is True and sent[1]["uses_limit"] == 20
