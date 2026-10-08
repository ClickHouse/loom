"""code.symbol: the reads that need only the resolved definitions (bodies,
chunk hashes, enrichment, citing documents, neighbours) start together and
are joined where their result is used, instead of running one after another.
Each leg stays fail-open on its own."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import patch

import pytest

from server.settings import settings

DEFS = [
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
]
DELAY = 0.05


class _Clock:
    """Counts the reads in flight at once; the proof the legs overlap."""

    def __init__(self) -> None:
        self.in_flight = 0
        self.peak = 0
        self.calls: list[str] = []

    async def hold(self, what: str) -> None:
        self.calls.append(what)
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(DELAY)
        finally:
            self.in_flight -= 1


class _FakeRepo:
    def __init__(self, clock: _Clock) -> None:
        self.clock = clock

    async def symbols_by_name(self, org, ns, name, limit=10):
        return list(DEFS)

    async def symbols_by_ids(self, org, ns, ids):
        return [r for r in DEFS if r["symbol_id"] in ids]

    async def symbols_fuzzy(self, org, ns, needle, *, limit=25):
        return []

    async def functions_for_paths(self, org, ns, paths, *, limit=400, **_kw):
        return [r for r in DEFS if r["path"] in paths]

    async def grep_chunks(self, org, ns, pattern, **kw):
        return []

    async def all_state(self, org, ns):
        return {}

    async def chunk_hashes_for_symbols(self, org, ns, ids, *, rows=None):
        await self.clock.hold("hashes")
        return {"f1": "sha-f1"}

    async def pr_history_for(self, org, ns, *, paths=None, symbol_names=None, limit=5):
        await self.clock.hold("pr_history")
        return [
            {
                "pr_number": 7,
                "title": "touch Foo",
                "author": "a",
                "merged_at": "2026-09-01",
                "is_revert": False,
            }
        ]

    async def edges_for_symbols(self, org, ns, ids, *, direction, limit=200, **kw):
        await self.clock.hold("edges:" + ",".join(kw.get("edge_kinds") or ["calls"]))
        if kw.get("edge_kinds") == ["reads"]:
            return [
                {
                    "path": "docs/foo.md",
                    "line": 3,
                    "from_qualified_name": "Foo § bar",
                    "to_symbol_id": "f1",
                    "tier": "markdown",
                }
            ]
        return [
            {
                "path": "src/Use.cpp",
                "line": 42,
                "from_qualified_name": "DB::use",
                "to_symbol_id": "f1",
                "tier": "treesitter",
            }
        ]

    async def test_map_for(self, org, ns, paths):
        await self.clock.hold("test_map")
        return [{"src_path": "src/Use.cpp", "test_path": "tests/use_test.cpp"}]


@pytest.fixture(autouse=True)
def _intel_on(monkeypatch):
    monkeypatch.setattr(settings, "code_intel_enabled", True)
    monkeypatch.setattr(settings, "code_org", "o")
    monkeypatch.setattr(settings, "code_namespace", "n")
    monkeypatch.setattr(settings, "code_lesson_namespaces", "lessons-public")


def _run(clock: _Clock, *, lessons: Any, **fields: Any) -> tuple[Any, float]:
    from server.code.api.models_symbol import CodeSymbolInput
    from server.code.app import enrich as code_enrich
    from server.code.app.symbol import outline
    from server.code.app.symbol import symbol as h

    repo = _FakeRepo(clock)

    async def body(r, org, ns, sym):
        await clock.hold("body")
        return f"<body {sym['symbol_id']}>"

    async def lessons_for_symbol(org, nss, *, symbol_name, paths, limit=3):
        await clock.hold("lessons")
        if isinstance(lessons, Exception):
            raise lessons
        return lessons

    async def go():
        with (
            patch.object(h, "CodeRepo", return_value=repo),
            patch.object(h, "symbol_body", new=body),
            patch.object(outline, "symbol_body", new=body),
            patch.object(code_enrich, "lessons_for_symbol", new=lessons_for_symbol),
            patch.object(h, "schedule_code_trace", new=lambda **k: None),
        ):
            started = time.monotonic()
            out = await h.handle_code_symbol(CodeSymbolInput(name="DB::Foo::bar", **fields))
            return out, time.monotonic() - started

    return asyncio.run(go())


LESSON = [{"memory_key": "k1", "namespace": "lessons-public", "excerpt": "bar bites"}]


def test_reads_overlap_and_every_block_still_lands():
    clock = _Clock()
    out, _wall = _run(clock, lessons=LESSON)
    # seven timed reads: body, hashes, pr_history, edges(calls), test_map, edges(reads), lessons
    assert set(clock.calls) >= {
        "body",
        "hashes",
        "pr_history",
        "edges:calls",
        "test_map",
        "edges:reads",
        "lessons",
    }
    # they ran side by side, not one after another
    assert clock.peak >= 4, clock.calls
    # The peak proves the overlap; a wall-clock bound would measure the runner's load.
    d = out.definitions[0]
    assert d.body == "<body f1>"
    assert d.body_hash == "sha-f1"
    assert [p["pr_number"] for p in d.recent_prs] == [7]
    assert d.examples and d.examples[0]["path"] == "src/Use.cpp" and d.examples[0]["test_covered"]
    assert d.lessons == LESSON
    assert out.documented_in and out.documented_in[0]["section"] == "Foo § bar"


def test_a_failing_leg_leaves_the_definition_served():
    clock = _Clock()
    out, _wall = _run(clock, lessons=RuntimeError("memory plane down"))
    d = out.definitions[0]
    assert d.body == "<body f1>"
    assert d.lessons == []  # the leg degraded to nothing; the answer did not
    assert [p["pr_number"] for p in d.recent_prs] == [7]


def test_enrichment_not_asked_reads_nothing_extra():
    clock = _Clock()
    out, _wall = _run(
        clock,
        lessons=LESSON,
        include_history=False,
        include_examples=False,
        include_lessons=False,
    )
    assert not {"pr_history", "test_map", "lessons"} & set(clock.calls), clock.calls
    assert "edges:calls" not in clock.calls  # usage examples were not asked for
    assert out.definitions[0].recent_prs == [] and out.definitions[0].lessons == []
