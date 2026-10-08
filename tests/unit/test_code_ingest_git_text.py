"""Inside a worker pass, sibling texts come from the mirror."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from server.code.app.index import ingest_resolve


def _mirror(tmp_path: Path) -> tuple[str, str]:
    work = tmp_path / "w"
    work.mkdir()
    (work / "packages").mkdir()
    (work / "packages" / "package.json").write_text('{"name": "@x/pkg"}')
    run = lambda *a: subprocess.run(  # noqa: E731
        ["git", "-C", str(work), *a], check=True, capture_output=True, text=True
    )
    run("init", "-q")
    run("add", "-A")
    run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "c")
    return str(work / ".git"), run("rev-parse", "HEAD").stdout.strip()


class _Repo:
    def __init__(self) -> None:
        self.reads: list[str] = []

    async def live_text_for_path(self, org: str, ns: str, path: str) -> str:
        self.reads.append(path)
        return "from-chunks"


def test_the_mirror_answers_inside_a_pass_and_the_chunks_outside(tmp_path: Path) -> None:
    git_dir, sha = _mirror(tmp_path)
    repo = _Repo()
    read = lambda p: asyncio.run(ingest_resolve._live_text(repo, "o", "n", p))  # type: ignore[arg-type]  # noqa: E731
    with ingest_resolve.git_text_source(git_dir, sha):
        assert read("packages/package.json") == '{"name": "@x/pkg"}'
        assert repo.reads == []  # no code_chunks FINAL read
        # a path the commit does not hold falls back to the indexed chunks
        assert read("missing/package.json") == "from-chunks"
    assert repo.reads == ["missing/package.json"]
    assert ingest_resolve._GIT_TEXT_SOURCE.get() is None
    assert read("packages/package.json") == "from-chunks"  # outside a pass: unchanged


def test_the_package_json_cache_reads_through_the_source(tmp_path: Path) -> None:
    git_dir, sha = _mirror(tmp_path)
    repo = _Repo()
    ingest_resolve._PACKAGE_JSON_CACHE.clear()
    with ingest_resolve.git_text_source(git_dir, sha):
        got = asyncio.run(
            ingest_resolve._package_json_text(repo, "o", "n", "packages/package.json")
        )  # type: ignore[arg-type]
    assert got == '{"name": "@x/pkg"}' and repo.reads == []
    ingest_resolve._PACKAGE_JSON_CACHE.clear()
