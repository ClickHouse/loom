"""The ingest's import resolution reads what one file's imports need and no more: the
probing read's candidates are built only without the live path set and only up to the
probe cut, and the Go listing rung reads a bounded number of package directories."""

from __future__ import annotations

import asyncio
from typing import Any

from server.code.app.index import ingest_resolve
from server.code.app.index.ingest_parse import parse_for_ingest
from server.code.app.ttl_cache import TtlCache
from server.code.domain.extract import lang_python, lang_rust


class _ProbingRepo:
    """A repository without a live path set: every lookup is a probing read."""

    def __init__(self) -> None:
        self.probes: list[list[str]] = []
        self.listings: list[str] = []

    async def live_paths_present(self, org: str, ns: str, paths: list[str]) -> set[str]:
        self.probes.append(list(paths))
        return set()

    async def live_paths_under(self, org: str, ns: str, prefix: str) -> list[str]:
        self.listings.append(prefix)
        return []


class _LiveRepo(_ProbingRepo):
    async def live_path_set(self, org: str, ns: str) -> set[str]:
        return {"src/a.rs"}


def test_a_python_probe_builds_the_candidates_up_to_the_cut(monkeypatch: Any) -> None:
    specs = [f"m{i}.n" for i in range(20_000)]
    pf, _ = parse_for_ingest("a.py", "".join(f"import {s}\n" for s in specs).encode())
    assert pf is not None
    expected = lang_python.wanted_paths(pf.path, list(pf.includes))[
        : ingest_resolve._MAX_INCLUDE_PROBES
    ]
    built: list[str] = []
    real = lang_python.import_candidates

    def counted(from_path: str, spec: str, roots: Any = ()) -> list[str]:
        built.append(spec)
        return real(from_path, spec, roots)

    monkeypatch.setattr(lang_python, "import_candidates", counted)
    repo = _ProbingRepo()
    asyncio.run(ingest_resolve._resolve_py_includes(repo, "o", "probe-cut", pf))  # type: ignore[arg-type]
    assert repo.probes == [expected]
    assert len(built) < len(specs) // 10


def test_a_lane_under_the_live_set_builds_no_probe(monkeypatch: Any) -> None:
    def unread(from_path: str, uses: list[str]) -> list[str]:
        raise AssertionError("the live set answers; the probe's candidates go unread")

    monkeypatch.setattr(lang_rust, "wanted_paths", unread)
    pf, _ = parse_for_ingest("src/b.rs", b"use crate::a;\n")
    assert pf is not None
    repo = _LiveRepo()
    try:
        asyncio.run(ingest_resolve._resolve_lane_includes(repo, "o", "live-set", pf))  # type: ignore[arg-type]
    finally:
        ingest_resolve._LIVE_PATHS_CACHE.pop(("o", "live-set"), None)
    assert repo.probes == [] and pf.includes == ["src/a.rs"]


def test_the_go_listing_rung_reads_a_bounded_number_of_directories(monkeypatch: Any) -> None:
    async def index(repo: Any, org: str, ns: str) -> list[tuple[str, str]]:
        return [("m", "")]

    monkeypatch.setattr(ingest_resolve, "_go_module_index", index)
    monkeypatch.setattr(ingest_resolve, "_GO_LISTING_CACHE", TtlCache(lambda: 60.0))
    dirs = ingest_resolve._MAX_GO_LISTINGS + 50
    imports = "".join(f'\t"m/p{i}"\n' for i in range(dirs))
    pf, _ = parse_for_ingest("a.go", f"package a\n\nimport (\n{imports})\n".encode())
    assert pf is not None
    repo = _ProbingRepo()
    asyncio.run(ingest_resolve._resolve_go_imports(repo, "o", "go-listings", pf))  # type: ignore[arg-type]
    assert len(repo.listings) == ingest_resolve._MAX_GO_LISTINGS
    assert len(set(repo.listings)) == len(repo.listings)
