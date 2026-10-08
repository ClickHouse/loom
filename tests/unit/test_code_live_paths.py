"""Import specifiers resolve against the namespace's live
path set, read once per process and TTL, not against a capped probe."""

from __future__ import annotations

import asyncio

from server.code.app.index import ingest, ingest_resolve
from server.code.app.index.ingest_parse import parse_file


class _Repo:
    def __init__(self, paths: set[str]) -> None:
        self.paths, self.set_reads, self.probes = paths, 0, 0

    async def live_path_set(self, org, ns):
        self.set_reads += 1
        return set(self.paths)

    async def live_paths_present(self, org, ns, paths):
        self.probes += 1
        return {p for p in paths if p in self.paths}

    async def live_text_for_path(self, org, ns, path):
        return ""


def _file(n_imports: int, path: str = "web/src/features/big/index.tsx") -> ingest.ParsedFile:
    lines = [f"import {{ x{i} }} from './mod{i}';" for i in range(n_imports)]
    lines += [f"import {{ a{i} }} from '@/src/lib/a{i}';" for i in range(n_imports)]
    return parse_file(path, "\n".join(lines).encode())


def test_a_file_with_forty_imports_binds_every_one_through_one_set_read() -> None:
    ingest_resolve._LIVE_PATHS_CACHE.clear()
    paths = {f"web/src/features/big/mod{i}.ts" for i in range(40)}
    paths |= {f"web/src/lib/a{i}/index.ts" for i in range(40)}
    repo = _Repo(paths)
    pf = _file(40)
    asyncio.run(ingest_resolve._resolve_lane_includes(repo, "o", "n", pf))  # type: ignore[arg-type]
    assert len(pf.includes) == 80, len(pf.includes)
    assert repo.set_reads == 1 and repo.probes == 0
    # a second file in the window: the cached set, no read at all
    pf2 = _file(3, "web/src/features/small/index.tsx")
    asyncio.run(ingest_resolve._resolve_lane_includes(repo, "o", "n", pf2))  # type: ignore[arg-type]
    assert (
        repo.set_reads == 1 and len(pf2.includes) == 3
    )  # its aliases; its relatives name no indexed file
    ingest_resolve._LIVE_PATHS_CACHE.clear()


def test_the_file_being_ingested_joins_the_set_for_the_files_after_it() -> None:
    ingest_resolve._LIVE_PATHS_CACHE.clear()
    repo = _Repo(set())  # a fresh namespace: nothing written yet
    first = parse_file("web/src/a.ts", b"export const a = 1;\n")
    asyncio.run(ingest_resolve._resolve_lane_includes(repo, "o", "n", first))  # type: ignore[arg-type]
    second = parse_file("web/src/b.ts", b"import { a } from './a';\nexport const b = a;\n")
    asyncio.run(ingest_resolve._resolve_lane_includes(repo, "o", "n", second))  # type: ignore[arg-type]
    assert second.includes == ["web/src/a.ts"]
    ingest_resolve._LIVE_PATHS_CACHE.clear()


def test_a_repository_without_the_set_read_probes_as_before() -> None:
    ingest_resolve._LIVE_PATHS_CACHE.clear()

    class _Old:
        async def live_paths_present(self, org, ns, paths):
            return {p for p in paths if p == "web/src/mod0.ts"}

    pf = parse_file("web/src/index.ts", b"import { x } from './mod0';\n")
    asyncio.run(ingest_resolve._resolve_lane_includes(_Old(), "o", "n", pf))  # type: ignore[arg-type]
    assert pf.includes == ["web/src/mod0.ts"]
