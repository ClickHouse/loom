"""code.symbol: the reads that need only the resolved definitions (bodies,
chunk hashes, enrichment, citing documents, neighbours) start together and
are joined where their result is used, instead of running one after another.
Each leg stays fail-open on its own."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, patch

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


async def test_named_examples_require_type_references() -> None:
    from server.code.app.symbol.named_examples import named_test_examples

    repo = AsyncMock()
    examples = await named_test_examples(repo, "o", "n", DEFS[0], 10)
    assert not examples
    repo.symbols_fuzzy.assert_not_awaited()


async def test_named_examples_skip_ambiguous_bare_types(monkeypatch) -> None:
    from server.code.app.symbol.named_examples import named_test_examples
    from server.embedding.fake import fake_model_name

    monkeypatch.setattr(settings, "embedding_model", fake_model_name(settings.embedding_dim))
    target = {**DEFS[0], "qualified_name": "modern::Cache", "name": "Cache", "kind": "class"}
    legacy = {**target, "qualified_name": "legacy::Cache", "path": "src/legacy.cpp"}
    repo = AsyncMock()
    repo.symbols_by_name.return_value = [target, legacy]
    repo.symbols_fuzzy.return_value = [{"name": "test_legacy_cache"}]
    assert not await named_test_examples(repo, "o", "n", target, 10)
    repo.source_chunks_for_span.assert_not_awaited()


@pytest.mark.parametrize("tier", ["scip", "treesitter"])
@pytest.mark.parametrize(("radius", "start"), [(2, 1), (1, 2)])
async def test_named_examples_include_attached_decorators(monkeypatch, tier, radius, start) -> None:
    from dataclasses import asdict

    from server.code.app.symbol.named_examples import named_test_examples
    from server.code.domain.extract.lang_python import parse_python
    from server.code.domain.extract.py_decorated_chunks import python_chunks
    from server.embedding.fake import fake_model_name

    revision = "a" * 40
    target = {**DEFS[0], "qualified_name": "modern::Cache", "name": "Cache", "kind": "class"}
    source = '@pytest.mark.parametrize("value", [1])\ndef test_cache(value):\n    Cache(value)\n'
    path = "tests/test_cache.py"
    parsed = parse_python(path, source.encode())
    (definition,) = (symbol for symbol in parsed.symbols if symbol.name == "test_cache")
    repo = AsyncMock()
    repo.symbols_by_name.return_value = [target]
    repo.symbols_fuzzy.return_value = [
        {
            "path": path,
            "name": definition.name,
            "qualified_name": definition.qualified_name,
            "kind": "function",
            "tier": tier,
            "start_line": definition.start_line,
            "end_line": definition.end_line,
            "indexed_commit": revision,
        }
    ]
    repo.source_chunks_for_span.return_value = [
        {**asdict(chunk), "indexed_commit": revision}
        for chunk in python_chunks(parsed, max_chars=6000)
    ]
    monkeypatch.setattr(settings, "embedding_model", fake_model_name(settings.embedding_dim))
    (example,) = await named_test_examples(repo, "o", "n", target, radius)
    assert example["context"] == {
        "commit": revision,
        "start_line": start,
        "end_line": 3,
        "text": "".join(source.splitlines(keepends=True)[start - 1 :]),
    }
    repo.source_chunks_for_span.assert_awaited_once()


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


def _run(clock: _Clock, *, lessons: Any, **fields: Any) -> tuple[Any, float]:
    from server.code.api.models_symbol import CodeSymbolInput
    from server.code.app import enrich as code_enrich
    from server.code.app.symbol import outline
    from server.code.app.symbol import symbol as h

    repo = _FakeRepo(clock)

    async def body(r, org, ns, sym):
        await clock.hold("body")
        return f"<body {sym['symbol_id']}>"

    async def lessons_for_symbol(org, ns, *, symbol_name, paths, extra_namespaces):
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
    assert d.lessons_unread  # and it says so, unlike a symbol with no lessons
    assert [p["pr_number"] for p in d.recent_prs] == [7]


def test_an_unread_lessons_leg_is_flagged_on_the_definition():
    # None is what the bounded read answers when cut off or failed (tested below)
    out, _wall = _run(_Clock(), lessons=None)
    d = out.definitions[0]
    assert d.lessons == [] and d.lessons_unread
    assert d.model_dump()["lessons_unread"] is True


def test_a_narrowed_token_gets_no_lessons_and_no_unread_flag():
    from server.code.app.symbol import symbol as h

    clock = _Clock()
    with patch.object(h, "request_narrowed", return_value=True):
        out, _wall = _run(clock, lessons=LESSON)
    d = out.definitions[0]
    assert "lessons" not in clock.calls  # the read is not made
    assert not d.lessons_unread
    assert "lessons_unread" not in d.model_dump() and "lessons" not in d.model_dump()


def test_a_symbol_with_no_lessons_says_nothing_about_them():
    out, _wall = _run(_Clock(), lessons=[])
    d = out.definitions[0]
    assert not d.lessons_unread
    assert "lessons_unread" not in d.model_dump() and "lessons" not in d.model_dump()


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
    assert not out.definitions[0].lessons_unread  # not asked is not unread


@pytest.mark.parametrize("outcome", ["slow", "failing"])
def test_the_lessons_read_answers_none_when_cut_off_or_failed(monkeypatch, outcome):
    from server.code.app import enrich as code_enrich
    from server.memory.app import code_plane_reads

    async def nss(org, ns, extra):
        return ["code-a"]

    async def read(*a, **kw):
        if outcome == "failing":
            raise RuntimeError("memory plane down")
        await asyncio.sleep(5)
        return [{"memory_key": "late"}]

    monkeypatch.setattr(code_plane_reads, "lesson_namespaces", nss)
    monkeypatch.setattr(code_plane_reads, "lessons_for_symbol", read)
    monkeypatch.setattr(settings, "code_enrich_timeout_s", 0.05)
    got = asyncio.run(
        code_enrich.lessons_for_symbol(
            "o", "code-a", symbol_name="f", paths=["a.py"], extra_namespaces=[]
        )
    )
    assert got is None


def test_a_member_whose_lessons_went_unread_keeps_the_others_and_flags_it():
    from server.code.api.models_symbol import CodeSymbolInput
    from server.code.app import enrich as code_enrich
    from server.code.app.symbol import symbol as h

    async def lessons_for_symbol(org, ns, *, symbol_name, paths, extra_namespaces):
        return None if symbol_name == "A::y" else [dict(LESSON[0])]

    head = [
        {"qualified_name": "A::x", "path": "a.py", "symbol_id": "s1"},
        {"qualified_name": "A::y", "path": "a.py", "symbol_id": "s2"},
    ]
    plain = CodeSymbolInput(name="A", include_history=False, include_examples=False)
    with patch.object(code_enrich, "lessons_for_symbol", new=lessons_for_symbol):
        out = asyncio.run(h._enrichment(None, "o", "n", plain, head, matched_members=True))
    # the member that was read still serves its lesson; the other may have had some
    assert [lesson["linked_symbols"] for lesson in out["lessons"]] == [["A::x"]]
    assert out["lessons_unread"] is True
