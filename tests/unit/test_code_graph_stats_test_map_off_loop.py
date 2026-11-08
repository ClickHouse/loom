"""The graph-stats test-map leg pairs test files with source files by stem in a
thread. On a large repository the pairing takes seconds of pure Python; on the
event loop it blocks the supervisor lease's renewal past its deadline, and the
workers stop before the pass can finish."""

from __future__ import annotations

import asyncio
import threading
from pathlib import PurePosixPath
from types import SimpleNamespace
from typing import Any

import pytest

from server.code.workers import graph_stats as w

_FILES = {
    "src/Common/HashTable.h": "h",
    "src/Common/hash_table.cpp": "h",  # same stem as HashTable.h
    "src/IO/ReadBuffer.cpp": "h",
    "src/IO/Read.cpp": "h",
    "src/a/util.cpp": "h",
    "src/b/util.cpp": "h",
    "src/c/util.cpp": "h",
    "src/d/util.cpp": "h",  # four files: the stem maps none of them
    "src/x/tests/gtest_read.cpp": "h",  # a test under src/: not a source file
    "tests/queries/0_stateless/01234_hash_table_read_buffer.sql": "h",
    "tests/integration/test_util/test.py": "h",
    "base/tests/read.cpp": "h",
    "docs/readme.md": "h",
}


def _reference(known: set[str]) -> set[tuple[str, str, float, str]]:
    """Every source stem tested against every test stem: the pairing's spec."""

    def stem(p: str) -> str:
        return PurePosixPath(p).stem.lower().replace("_", "")

    src: dict[str, list[str]] = {}
    for p in known:
        if p.startswith("src/") and "/tests/" not in p:
            src.setdefault(stem(p), []).append(p)
    out = set()
    for t in known:
        if not (t.startswith("tests/") or "/tests/" in t):
            continue
        for s, paths in src.items():
            if s and s in stem(t) and len(paths) <= 3:
                for p in paths:
                    out.add((p, t, 0.7 if len(paths) == 1 else 0.4, f"stem '{s}'"))
    return out


def test_the_pairs_are_every_source_stem_inside_a_test_stem() -> None:
    rows = w._path_convention_rows("o", "n", set(_FILES))
    got = {(r["src_path"], r["test_path"], r["confidence"], r["evidence"]) for r in rows}
    assert got == _reference(set(_FILES))
    assert ("src/IO/Read.cpp", "base/tests/read.cpp", 0.7, "stem 'read'") in got
    assert not any(src.endswith("util.cpp") for src, *_ in got)
    assert all(r["method"] == "path_convention" and r["namespace"] == "n" for r in rows)
    assert len(rows) == len(got)


def test_no_source_files_pairs_nothing() -> None:
    assert w._path_convention_rows("o", "n", {"tests/a.py", "docs/b.md"}) == []


class _Repo:
    def __init__(self) -> None:
        self.test_map: list[dict[str, Any]] = []

    async def live_file_hashes(self, org: str, ns: str) -> dict[str, str]:
        return dict(_FILES)

    async def insert_rows(self, table: str, rows: list[dict[str, Any]]) -> None:
        if table == "code_test_map":
            self.test_map.extend(rows)

    def __getattr__(self, name: str) -> Any:  # every other leg: nothing to do
        async def empty(*a: Any, **k: Any) -> Any:
            return []

        return empty


def test_the_leg_pairs_off_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.code.store import index as code_repo

    repo = _Repo()

    class Cls:
        def __new__(cls, *a: Any, **k: Any) -> _Repo:
            return repo

    async def no_head(*a: Any, **k: Any) -> str:
        return ""

    on_loop: list[str] = []
    stem = w._stem

    def watched(path: str) -> str:
        if threading.current_thread() is threading.main_thread():
            on_loop.append(path)
        return stem(path)

    monkeypatch.setattr(code_repo, "CodeRepo", Cls)
    monkeypatch.setattr(w, "_git_head", no_head)
    monkeypatch.setattr(w, "_stem", watched)
    monkeypatch.setattr(
        "server.embedding.embedder.get_embedder", lambda: SimpleNamespace(model_name="m")
    )
    result = w.CodeGraphStatsResult()
    asyncio.run(w._run_repo("o", "n", result))
    assert on_loop == [], "the stem pairing ran on the event loop"
    assert len(repo.test_map) == len(_reference(set(_FILES))) == result.test_map_rows
