"""Inside a worker pass, sibling texts come from the mirror."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

import pytest

from server.code.app.index import gitrepo, ingest_resolve
from server.settings import settings


@pytest.fixture(autouse=True)
def _fresh_caches() -> Any:
    caches = (
        ingest_resolve._OVERSIZE,
        ingest_resolve._PACKAGE_JSON_CACHE,
        ingest_resolve._BARREL_CACHE,
        ingest_resolve._WORKSPACE_CACHE,
    )
    for cache in caches:
        cache.clear()
    yield
    for cache in caches:
        cache.clear()


def _mirror(tmp_path: Path, extra: dict[str, bytes] | None = None) -> tuple[str, str]:
    work = tmp_path / "w"
    work.mkdir()
    (work / "packages").mkdir()
    (work / "packages" / "package.json").write_text('{"name": "@x/pkg"}')
    for rel, data in (extra or {}).items():
        (work / rel).parent.mkdir(parents=True, exist_ok=True)
        (work / rel).write_bytes(data)
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
    read = lambda p: asyncio.run(ingest_resolve._live_text(repo, "o", "n", p, 1024))  # type: ignore[arg-type]  # noqa: E731
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


def test_a_manifest_over_its_cap_is_skipped_unread_with_a_logged_reason(
    tmp_path: Path, caplog
) -> None:
    git_dir, sha = _mirror(tmp_path)
    work = Path(git_dir).parent
    # Minimal JSON values parse at about 30 times their bytes.
    (work / "package.json").write_bytes(b"[" + b",".join([b"{}"] * 400_000) + b"]")
    subprocess.run(["git", "-C", str(work), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(work),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-qm",
            "big",
        ],
        check=True,
    )
    head = subprocess.run(
        ["git", "-C", str(work), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    repo = _Repo()
    ingest_resolve._PACKAGE_JSON_CACHE.clear()
    with ingest_resolve.git_text_source(git_dir, head), caplog.at_level("WARNING"):
        got = asyncio.run(ingest_resolve._package_json_text(repo, "o", "n", "package.json"))  # type: ignore[arg-type]
        small = asyncio.run(
            ingest_resolve._package_json_text(repo, "o", "n", "packages/package.json")
        )  # type: ignore[arg-type]
    assert got == "" and repo.reads == []  # neither parsed nor read from the chunks
    assert small == '{"name": "@x/pkg"}'
    assert caplog.text.count("o/n: sibling file skipped: package.json is 1200001 bytes") == 1
    with ingest_resolve.git_text_source(git_dir, head), caplog.at_level("WARNING"):
        again = asyncio.run(ingest_resolve._package_json_text(repo, "o", "n", "package.json"))  # type: ignore[arg-type]
    assert again == "" and caplog.text.count("package.json is 1200001 bytes") == 1  # logged once
    ingest_resolve._PACKAGE_JSON_CACHE.clear()


def _counting_show(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    asked: list[str] = []
    real = gitrepo.show_file

    async def show(git_dir: str, sha: str, path: str, max_bytes: int = 0) -> bytes | None:
        asked.append(path)
        return await real(git_dir, sha, path, max_bytes=max_bytes)

    monkeypatch.setattr(gitrepo, "show_file", show)
    return asked


def test_a_file_found_over_its_cap_is_not_asked_for_again(tmp_path, monkeypatch) -> None:
    git_dir, sha = _mirror(tmp_path, {"big.json": b"x" * 2000})
    asked = _counting_show(monkeypatch)
    repo = _Repo()
    with ingest_resolve.git_text_source(git_dir, sha):
        for _ in range(3):
            assert asyncio.run(ingest_resolve._live_text(repo, "o", "n", "big.json", 1000)) == ""  # type: ignore[arg-type]
    assert asked == ["big.json"] and repo.reads == []


def test_a_barrel_over_the_file_cap_forwards_nothing_and_is_read_once(
    tmp_path, monkeypatch
) -> None:
    barrel = b"export * from './a'\n" + b"//" * 600
    git_dir, sha = _mirror(tmp_path, {"src/index.ts": barrel, "src/a.ts": b"export const a = 1\n"})
    monkeypatch.setattr(settings, "code_ingest_max_file_bytes", 1000)
    asked = _counting_show(monkeypatch)

    class _Present(_Repo):
        async def live_paths_present(self, org: str, ns: str, paths: list[str]) -> set[str]:
            return {"src/a.ts"}

    repo = _Present()
    with ingest_resolve.git_text_source(git_dir, sha):
        for _ in range(2):
            got = asyncio.run(ingest_resolve._barrel_targets_of(repo, "o", "n", "src/index.ts"))  # type: ignore[arg-type]
            assert got == ()
    assert asked == ["src/index.ts"]
    monkeypatch.setattr(settings, "code_ingest_max_file_bytes", 0)  # no cap: it forwards
    ingest_resolve._BARREL_CACHE.clear()
    ingest_resolve._OVERSIZE.clear()
    with ingest_resolve.git_text_source(git_dir, sha):
        got = asyncio.run(ingest_resolve._barrel_targets_of(repo, "o", "n", "src/index.ts"))  # type: ignore[arg-type]
    assert got == ("src/a.ts",)


def test_a_registry_empty_for_an_oversize_root_manifest_is_cached(tmp_path) -> None:
    from server.code.app.index.untrusted_files import PACKAGE_JSON_MAX_BYTES

    big = b'{"workspaces": ["packages"], "pad": "' + b"x" * PACKAGE_JSON_MAX_BYTES + b'"}'
    git_dir, sha = _mirror(tmp_path, {"package.json": big})
    present = {"package.json", "packages/package.json"}
    ingest_resolve._LIVE_PATHS_CACHE[("o", "n")] = (0.0, present)
    try:
        with ingest_resolve.git_text_source(git_dir, sha):
            got = asyncio.run(ingest_resolve._workspace_registry(_Repo(), "o", "n", present))  # type: ignore[arg-type]
        assert got == {} and ingest_resolve._WORKSPACE_CACHE.get(("o", "n")) == {}
    finally:
        ingest_resolve._LIVE_PATHS_CACHE.pop(("o", "n"), None)


def test_the_manifest_cache_is_bounded_by_the_characters_it_holds() -> None:
    from server.code.app.ttl_cache import TtlCache

    cache: TtlCache[str, str] = TtlCache(60, maxsize=100, weigh=len, maxweight=10)
    for key in "abcd":
        cache.put(key, "xxxx")
    assert cache.weight == 8 and [k for k in "abcd" if k in cache] == ["c", "d"]
    cache.put("d", "x")  # a replaced value's weight leaves with it
    assert cache.weight == 5
    cache.put("big", "y" * 50)  # one value over the bound is kept alone
    assert cache.weight == 50 and "big" in cache and "c" not in cache
    cache.pop("big")
    assert cache.weight == 0
    bounded = ingest_resolve._PACKAGE_JSON_CACHE
    assert bounded.maxweight == ingest_resolve.PACKAGE_JSON_CACHE_BYTES
    bounded.put(("o", "n", "package.json", 1), "\U0001f600" * 10)  # its UTF-8 bytes count
    assert bounded.weight == 40


def test_go_mods_are_read_capped_and_counted(tmp_path, monkeypatch) -> None:
    git_dir, sha = _mirror(tmp_path)
    caps: list[tuple[str, int]] = []

    async def show(git_dir: str, sha: str, path: str, max_bytes: int = 0) -> bytes | None:
        caps.append((path, max_bytes))
        return f"module example.com/{path.rsplit('/', 1)[0]}\n".encode()

    monkeypatch.setattr(gitrepo, "show_file", show)
    many = [f"m{i:03d}/sub/go.mod" for i in range(300)] + ["go.mod"]

    class _Mods(_Repo):
        async def live_paths_named(self, org: str, ns: str, name: str) -> list[str]:
            return many

    ingest_resolve._GO_MODULE_INDEX_CACHE.clear()
    with ingest_resolve.git_text_source(git_dir, sha):
        index = asyncio.run(ingest_resolve._go_module_index(_Mods(), "o", "n"))  # type: ignore[arg-type]
    ingest_resolve._GO_MODULE_INDEX_CACHE.clear()
    assert len(caps) == ingest_resolve.GO_MODS_MAX
    assert caps[0] == ("go.mod", ingest_resolve.GO_MOD_MAX_BYTES)  # the root first
    assert len(index) == ingest_resolve.GO_MODS_MAX


def test_a_file_over_one_cap_is_still_read_under_a_larger_one(tmp_path: Path) -> None:
    git_dir, sha = _mirror(tmp_path, {"big.ts": b"export const a = 1;\n" * 100})
    repo: Any = _Repo()
    with ingest_resolve.git_text_source(git_dir, sha):
        small = asyncio.run(ingest_resolve._live_text(repo, "o", "n", "big.ts", 100))
        large = asyncio.run(ingest_resolve._live_text(repo, "o", "n", "big.ts", 1 << 20))
    assert small == "" and large.startswith("export const a")


def test_the_chunk_fallback_holds_the_callers_cap_and_records_the_cut() -> None:
    class Repo:
        def __init__(self) -> None:
            self.reads = 0

        async def live_text_for_path(self, org: str, ns: str, path: str) -> str:
            self.reads += 1
            return "x" * 100

    repo: Any = Repo()
    read = ingest_resolve._live_text
    assert asyncio.run(read(repo, "o", "n", "go.mod", 100)) == "x" * 100
    assert asyncio.run(read(repo, "o", "n", "go.mod", 50)) == ""
    assert asyncio.run(read(repo, "o", "n", "go.mod", 50)) == ""
    assert repo.reads == 2  # the cut is known: the second read under 50 asks no chunks
    assert ingest_resolve._oversize("o", "n", "go.mod", 50)
    assert not ingest_resolve._oversize("o2", "n", "go.mod", 50)


def test_a_cut_the_chunks_made_does_not_stop_a_recovered_mirror(
    tmp_path: Path, monkeypatch
) -> None:
    git_dir, sha = _mirror(tmp_path, {"go.mod": b"module x\n"})

    class Repo:
        async def live_text_for_path(self, org: str, ns: str, path: str) -> str:
            return "y" * 100  # an older, larger version in the chunks

    repo: Any = Repo()
    show = gitrepo.show_file

    async def broken(*a: Any, **k: Any) -> bytes:
        raise gitrepo.GitError("timed out")

    with ingest_resolve.git_text_source(git_dir, sha):
        monkeypatch.setattr(gitrepo, "show_file", broken)
        assert asyncio.run(ingest_resolve._live_text(repo, "o", "n", "go.mod", 50)) == ""
        monkeypatch.setattr(gitrepo, "show_file", show)
        assert asyncio.run(ingest_resolve._live_text(repo, "o", "n", "go.mod", 50)) == "module x\n"


def test_a_manifest_verdict_outlasts_a_pass_of_oversize_barrels() -> None:
    ingest_resolve._over_cap(("o", "n", "package.json", 1 << 20), "o", "n", "big")
    for i in range(2_000):
        ingest_resolve._over_cap(("o", "n", f"src/b{i}.ts", 1_000_000), "o", "n", "big")
    assert ingest_resolve._oversize("o", "n", "package.json", 1 << 20)
