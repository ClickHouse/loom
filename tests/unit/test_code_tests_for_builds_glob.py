"""`code.tests_for` kind=builds asked with a file path matches
the CMake lane's source globs at read time — which CMake list (and target
line) builds this file."""

from __future__ import annotations

import asyncio
from typing import Any

import server.code.app.coverage as cc
from server.code.app.coverage import _facet_pairs, glob_regex

GLOBS = [
    {
        "path": "src/client/CMakeLists.txt",
        "name": "src/client/*.cpp",
        "ordinal": 1,
        "line": 3,
        "confidence": 0.9,
    },
    {
        "path": "src/client/tests/CMakeLists.txt",
        "name": "src/client/tests/*.cpp",
        "ordinal": 1,
        "line": 5,
        "confidence": 0.9,
    },
    {
        "path": "src/CMakeLists.txt",
        "name": "src/**/*.cpp",
        "ordinal": 2,
        "line": 12,
        "confidence": 0.9,
    },
]


class _Repo:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def facet_kinds_present(self, org, ns, kinds):
        return set(kinds)

    async def tests_for_facets(self, org, ns, facets, *, limit=50):
        self.calls.append(("exact", list(facets)))
        return [], 0

    async def live_facets_of_kind(self, org, ns, kind, *, name_like="", limit=5000):
        self.calls.append(("kind", kind, name_like))
        return GLOBS


class _Input:
    org = "clickhouse"
    namespace = "code-lumen"
    agent = "-"
    user_id = "-"
    consumer = "test"
    facets = [{"kind": "builds", "name": "src/client/row-block.cpp"}]
    limit = 10
    closest = False


def _patch(monkeypatch, repo) -> None:
    monkeypatch.setattr(cc, "CodeRepo", lambda: repo)
    monkeypatch.setattr(cc, "require_code_intel", lambda: None)
    monkeypatch.setattr(cc, "resolve_code_identity", lambda o, n: (o, n))
    monkeypatch.setattr(cc, "schedule_code_trace", lambda **kw: None)
    monkeypatch.setattr(cc, "set_code_span_attributes", lambda **kw: None)

    async def _as_of(repo, org, ns):
        return {"commit": "abc"}

    import server.code.app.common as common

    monkeypatch.setattr(common, "index_as_of", _as_of)


def test_glob_regex_semantics() -> None:
    assert glob_regex("src/client/*.cpp").match("src/client/row-block.cpp")
    assert not glob_regex("src/client/*.cpp").match("src/client/tests/x.cpp"), "* stays in one dir"
    assert glob_regex("src/**/*.cpp").match("src/client/tests/x.cpp")
    assert glob_regex("src/**/*.cpp").match("src/x.cpp"), "**/ matches zero directories"
    assert glob_regex("src/a?.h").match("src/ab.h") and not glob_regex("src/a?.h").match(
        "src/a/b.h"
    )
    assert not glob_regex("src/*.cpp").match("src/a.cpp.bak")


def test_a_file_path_finds_the_lists_whose_globs_cover_it(monkeypatch) -> None:
    repo = _Repo()
    _patch(monkeypatch, repo)
    out = asyncio.run(cc.handle_code_tests_for(_Input()))
    assert [(t["path"], t["via"]) for t in out.tests] == [
        ("src/CMakeLists.txt", "glob:src/**/*.cpp"),
        ("src/client/CMakeLists.txt", "glob:src/client/*.cpp"),
    ]
    assert out.total == 2 and out.verdict == "" and out.abstain is False
    assert out.tests[0]["lines"] == [12] and out.tests[0]["confidence"] == 0.9
    assert ("kind", "builds", "%*%") in repo.calls, "one keyed read of the glob rows"


def test_the_leg_stays_out_of_other_questions(monkeypatch) -> None:
    class _Glob(_Input):
        facets = [
            {"kind": "builds", "name": "src/client/*.cpp"}
        ]  # a glob asked by name: exact path

    class _Mixed(_Input):
        facets = [
            {"kind": "builds", "name": "src/client/row-block.cpp"},
            {"kind": "setting", "name": "x"},
        ]

    for inp in (_Glob(), _Mixed()):
        repo = _Repo()
        _patch(monkeypatch, repo)
        out = asyncio.run(cc.handle_code_tests_for(inp))
        assert out.tests == [] and not any(c[0] == "kind" for c in repo.calls)


def test_lane_kinds_are_accepted() -> None:
    pairs = _facet_pairs(
        [
            {"kind": "builds", "name": "src/a.cpp"},
            {"kind": "checks", "name": "TypeOK"},
            {"kind": "root_type", "name": "Header"},
            {"kind": "tests", "name": "lumen-test"},
        ]
    )
    assert [k for k, _ in pairs] == ["builds", "checks", "root_type", "tests"]
