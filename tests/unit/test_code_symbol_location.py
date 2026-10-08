"""code.symbol accepts `path:line`: a grep hit or review comment resolves to
the innermost function containing the line, body included, in one call."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

from server.settings import settings

FUNCS = [
    {
        "symbol_id": "a",
        "qualified_name": "DB::Foo::bar",
        "name": "bar",
        "signature": "void bar(int)",
        "path": "src/Foo.cpp",
        "start_line": 10,
        "end_line": 40,
        "tier": "treesitter",
        "kind": "method",
    },
    {
        "symbol_id": "b",
        "qualified_name": "DB::Foo::bar::$lambda",
        "name": "$lambda",
        "signature": "",
        "path": "src/Foo.cpp",
        "start_line": 20,
        "end_line": 25,
        "tier": "treesitter",
        "kind": "function",
    },
    {
        "symbol_id": "d",
        "qualified_name": "DB::Foo::qux",
        "name": "qux",
        "signature": "int qux()",
        "path": "src/Foo.cpp",
        "start_line": 60,
        "end_line": 70,
        "tier": "treesitter",
        "kind": "method",
    },
]
# the compiler row for bar: single-line span (scip-clang emits no enclosing_range)
SCIP_BAR = {
    "symbol_id": "a2",
    "qualified_name": "DB::Foo::bar",
    "name": "bar",
    "signature": "void bar(int)",
    "path": "src/Foo.cpp",
    "start_line": 10,
    "end_line": 10,
    "tier": "scip",
    "kind": "method",
}


class _FakeRepo:
    def __init__(self) -> None:
        self.by_name: list[str] = []
        self.fuzzy: list[str] = []

    async def functions_for_paths(self, org, ns, paths, *, limit=400, **_kw):
        return [r for r in FUNCS if r["path"] in paths]

    async def symbols_by_name(self, org, ns, name, limit=10):
        self.by_name.append(name)
        rows = [r for r in FUNCS if r["qualified_name"] == name]
        if name == "DB::Foo::bar":
            rows = [SCIP_BAR, *rows]
        return rows

    async def symbols_fuzzy(self, org, ns, needle, *, limit=25):
        self.fuzzy.append(needle)
        return []

    async def symbols_by_ids(self, org, ns, ids):
        return [r for r in FUNCS if r["symbol_id"] in ids]


def _run(name: str) -> tuple[Any, _FakeRepo]:
    from server.code.api.models_symbol import CodeSymbolInput
    from server.code.app.symbol import outline
    from server.code.app.symbol import symbol as h

    fake = _FakeRepo()

    async def body(repo, org, ns, sym):
        return f"<body {sym['symbol_id']}>"

    async def go():
        with (
            patch.object(h, "CodeRepo", return_value=fake),
            patch.object(h, "symbol_body", new=body),
            patch.object(outline, "symbol_body", new=body),
            patch.object(h, "schedule_code_trace", new=lambda **k: None),
        ):
            return await h.handle_code_symbol(
                CodeSymbolInput(
                    name=name,
                    include_history=False,
                    include_examples=False,
                    include_lessons=False,
                )
            )

    return asyncio.run(go()), fake


def _settings(monkeypatch) -> None:
    monkeypatch.setattr(settings, "code_intel_enabled", True)
    monkeypatch.setattr(settings, "code_org", "o")
    monkeypatch.setattr(settings, "code_namespace", "n")


def test_path_line_resolves_innermost_function_with_body(monkeypatch):
    _settings(monkeypatch)
    out, fake = _run("./src/Foo.cpp:22")
    got = [(u.symbol, u.tier, u.body) for u in out.definitions]
    # innermost (lambda) first, then bar's tree-sitter row, then bar's compiler row by name
    assert got[0] == ("DB::Foo::bar::$lambda", "treesitter", "<body b>")
    assert got[1] == ("DB::Foo::bar", "treesitter", "<body a>")
    assert ("DB::Foo::bar", "scip", "<body a2>") in got
    assert "DB::Foo::bar" in fake.by_name and fake.fuzzy == []
    assert out.candidates == []


def test_path_line_at_file_scope_lists_nearest_functions(monkeypatch):
    _settings(monkeypatch)
    out, fake = _run("src/Foo.cpp:50")
    assert out.definitions == []
    # line 50 is 10 from bar's end (40) and 10 from qux's start (60); the lambda is 25 away
    assert [c["qualified_name"] for c in out.candidates][:2] == ["DB::Foo::bar", "DB::Foo::qux"]
    assert out.candidates[0]["start_line"] == 10 and out.candidates[0]["end_line"] == 40
    assert fake.fuzzy == [], "location misses do not fall through to fuzzy name search"


def test_plain_names_still_take_the_name_path(monkeypatch):
    _settings(monkeypatch)
    out, fake = _run("DB::Foo::bar")
    assert fake.by_name == ["DB::Foo::bar"]
    assert {u.tier for u in out.definitions} == {"scip", "treesitter"}


def test_parse_location_rejects_symbol_shapes():
    from server.code.app.symbol.resolve import _parse_location

    assert _parse_location("src/Foo.cpp:12") == ("src/Foo.cpp", 12)
    assert _parse_location("./src/Foo.cpp:12") == ("src/Foo.cpp", 12)
    assert _parse_location("DB::Foo::bar") is None
    assert _parse_location("Foo:12") is None, "no extension -> not a location"
    assert _parse_location("src/Foo.cpp") is None
