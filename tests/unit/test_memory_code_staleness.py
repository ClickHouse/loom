"""memory.ask staleness oracle wiring: flags a memory whose code location has
moved, leaves fresh ones alone, skips homonyms, and no-ops when unbound."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from server.memory.app import code_grounding as ma


def _mem(key: str, text: str) -> SimpleNamespace:
    return SimpleNamespace(memory_key=key, memory_type="semantic", content_excerpt=text)


def _defs(*paths: str) -> SimpleNamespace:
    return SimpleNamespace(
        definitions=[
            SimpleNamespace(symbol_id=f"id{i}", unit_id=f"id{i}", path=p, start_line=10 + i)
            for i, p in enumerate(paths)
        ]
    )


class _In:
    def __init__(self, ns: str = "code-clickhouse") -> None:
        self.org = "o"
        self.code_namespace = ns
        self.code_org = ""
        self.query = "q"


class _S:
    memory_ask_code_namespace = ""
    memory_ask_code_org = ""
    code_org = "o"


def _patch_symbol(monkeypatch, table: dict[str, SimpleNamespace]) -> None:
    async def fake(inp):
        return table.get(inp.name, SimpleNamespace(definitions=[]))

    monkeypatch.setattr("server.code.app.symbol.handle_code_symbol", fake)


@pytest.mark.asyncio
async def test_flags_moved_location(monkeypatch) -> None:
    _patch_symbol(
        monkeypatch,
        {
            # memory claims computeThing is in Old.cpp; index says it's now in New.cpp
            "computeThing": _defs("src/x/New.cpp"),
        },
    )
    mems = [_mem("m1", "the fix for computeThing lives in src/x/Old.cpp near here")]
    note = await ma._annotate_stale_code_memories(mems, _In(), _S())
    assert note is not None
    assert "computeThing" in note.content_excerpt
    assert "New.cpp" in note.content_excerpt and "STALENESS" in note.content_excerpt


@pytest.mark.asyncio
async def test_fresh_location_not_flagged(monkeypatch) -> None:
    _patch_symbol(monkeypatch, {"computeThing": _defs("src/x/Old.cpp")})
    mems = [_mem("m1", "the fix for computeThing lives in src/x/Old.cpp near here")]
    assert await ma._annotate_stale_code_memories(mems, _In(), _S()) is None


@pytest.mark.asyncio
async def test_homonym_not_flagged(monkeypatch) -> None:
    # getValue resolves to many files, none the claimed one -> ambiguous -> skip
    _patch_symbol(
        monkeypatch,
        {
            "getValue": _defs("src/a/A.cpp", "src/b/B.cpp", "src/c/C.h"),
        },
    )
    mems = [_mem("m1", "getValue is used in src/x/resolveFunction.cpp somewhere")]
    assert await ma._annotate_stale_code_memories(mems, _In(), _S()) is None


@pytest.mark.asyncio
async def test_no_op_when_no_code_namespace_bound(monkeypatch) -> None:
    called = {"n": 0}

    async def fake(inp):
        called["n"] += 1
        return SimpleNamespace(definitions=[])

    monkeypatch.setattr("server.code.app.symbol.handle_code_symbol", fake)
    mems = [_mem("m1", "computeThing lives in src/x/Old.cpp")]
    # unbound: search_input.code_namespace empty AND settings binding empty
    note = await ma._annotate_stale_code_memories(mems, _In(ns=""), _S())
    assert note is None and called["n"] == 0  # never touches the index
