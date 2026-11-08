"""A repository decides how large its files are, so every reader of a mirror blob that
it parses, indexes or counts asks git for the size first: a blob over
`code_ingest_max_file_bytes` is never read, and the ingest records its verdict from
the size alone, the same row an oversize source has always produced."""

from __future__ import annotations

import asyncio
import fcntl
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from server.code.app.index import denylist as dl
from server.code.app.index import dependents, gitrepo, ingest, pass_commit, reconcile
from server.code.app.index.gitrepo import BlobTooLarge, blob_sha, show_file
from server.code.app.index.pass_budget import CodeIngestResult
from server.settings import settings
from tests.unit.code_fixtures import repo_config
from tests.unit.test_code_ingest_errors import _StubRepo

_BIG = b"{" + b'"k": 1, ' * 200 + b"}"


def _commit(tmp_path: Path, files: dict[str, bytes]) -> tuple[str, str]:
    work = tmp_path / "w"
    for rel, data in files.items():
        (work / rel).parent.mkdir(parents=True, exist_ok=True)
        (work / rel).write_bytes(data)
    run = lambda *a: subprocess.run(  # noqa: E731
        ["git", "-C", str(work), *a], check=True, capture_output=True, text=True
    )
    run("init", "-q")
    run("add", "-A")
    run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "c")
    return str(work / ".git"), run("rev-parse", "HEAD").stdout.strip()


# --- show_file ---


def test_a_blob_over_the_cap_raises_with_its_size_and_id_unread(tmp_path: Path) -> None:
    git_dir, sha = _commit(tmp_path, {"big.json": _BIG, "a.py": b"x = 1\n"})
    with pytest.raises(BlobTooLarge) as e:
        asyncio.run(show_file(git_dir, sha, "big.json", max_bytes=100))
    assert (e.value.size, e.value.blob) == (len(_BIG), blob_sha(_BIG))
    assert e.value.head == _BIG[: gitrepo.OVERSIZE_HEAD_BYTES]
    assert str(e.value) == f"big.json is {len(_BIG)} bytes; max 100"
    assert asyncio.run(show_file(git_dir, sha, "a.py", max_bytes=6)) == b"x = 1\n"
    assert asyncio.run(show_file(git_dir, sha, "big.json", max_bytes=0)) == _BIG  # 0: no cap


def test_a_capped_read_of_a_missing_path_or_a_directory_is_none(tmp_path: Path) -> None:
    files = {f"src/f{i:03d}.py": b"x = 1\n" for i in range(50)}  # a tree larger than the cap
    git_dir, sha = _commit(tmp_path, files)
    assert asyncio.run(show_file(git_dir, sha, "nope.py", max_bytes=10)) is None
    assert asyncio.run(show_file(git_dir, sha, "src", max_bytes=10)) is None
    assert asyncio.run(show_file(git_dir, "f" * 40, "src/f000.py", max_bytes=10)) is None


def _pipe_capacity() -> int:
    r, w = os.pipe()
    try:
        return int(fcntl.fcntl(w, fcntl.F_GETPIPE_SZ))  # a page size's multiple
    finally:
        os.close(r)
        os.close(w)


def test_a_capped_read_of_a_large_directory_reads_not_its_tree(tmp_path, monkeypatch) -> None:
    # Once git is stopped, what is read is two of the reader's 64 KiB limits, the one
    # pipe read (up to 256 KiB) that passes them, and what the pipe still holds.
    cap = _pipe_capacity()
    bound = 2 * 65_536 + min(cap, 262_144) + cap
    entries = 4 * bound // 38  # a tree entry is about 38 bytes: the tree is 4 bounds
    git_dir, sha = _commit(tmp_path, {f"d/f{i:06d}.py": b"" for i in range(entries)})
    got: list[int] = []
    read = asyncio.StreamReader.read

    async def counted(self: asyncio.StreamReader, n: int = -1) -> bytes:
        data = await read(self, n)
        if n >= 0:  # read(-1) loops over read(limit), which is counted: not both
            got.append(len(data))
        return data

    monkeypatch.setattr(asyncio.StreamReader, "read", counted)
    assert asyncio.run(show_file(git_dir, sha, "d", max_bytes=10)) is None
    assert sum(got) < bound


def test_a_capped_read_names_a_path_holding_a_newline(tmp_path: Path) -> None:
    git_dir, sha = _commit(tmp_path, {"a\nb.py": b"y = 2\n", "c.py": b"1", "c.py\r": b"2"})
    assert asyncio.run(show_file(git_dir, sha, "a\nb.py", max_bytes=100)) == b"y = 2\n"
    assert asyncio.run(show_file(git_dir, sha, "c.py\r", max_bytes=100)) == b"2"
    with pytest.raises(BlobTooLarge):
        asyncio.run(show_file(git_dir, sha, "a\nb.py", max_bytes=3))


# --- the ingest's verdict from the size ---


def _oversize(path: str, data: bytes = _BIG) -> BlobTooLarge:
    return BlobTooLarge(path, len(data), blob_sha(data), 100, data[:8192])


def test_the_head_covers_the_denylist_content_probes() -> None:
    assert gitrepo.OVERSIZE_HEAD_BYTES >= max(dl._BINARY_PROBE_BYTES, dl._GENERATED_PROBE_BYTES)


def test_a_head_past_the_cap_is_all_that_is_read(tmp_path: Path) -> None:
    big = b"x" * 5_000_000
    git_dir, sha = _commit(tmp_path, {"big.bin": big})
    with pytest.raises(BlobTooLarge) as e:
        asyncio.run(show_file(git_dir, sha, "big.bin", max_bytes=1000))
    assert len(e.value.head) == gitrepo.OVERSIZE_HEAD_BYTES and e.value.size == len(big)


async def _record(repo: Any, path: str, data: bytes = _BIG, **kw: Any) -> Any:
    return await ingest.record_oversize(
        repo,
        "o",
        "code-ns",
        path=path,
        too_large=_oversize(path, data),
        commit_sha="c" * 40,
        embedding_model="m",
        **kw,
    )


@pytest.mark.asyncio
async def test_an_unread_oversize_file_gets_the_row_its_bytes_would(monkeypatch) -> None:
    monkeypatch.setattr(settings, "code_ingest_max_file_bytes", 100)
    read, unread = _StubRepo(), _StubRepo()
    stats = await ingest.ingest_file(
        read,
        "o",
        "code-ns",
        path="data/big.json",
        source=_BIG,
        commit_sha="c" * 40,
        embedding_model="m",
    )
    got = await _record(unread, "data/big.json")
    assert (
        got.error
        == stats.error
        == (f"skipped: {len(_BIG)} bytes > code_ingest_max_file_bytes (100)")
    )
    assert [t for t, _ in unread.inserts] == ["code_files"]
    (want,), (row,) = read.file_rows(), unread.file_rows()
    # The bytes were never read: no content hash, no line count. Every other field
    # is the row the read file got, its blob id from git included.
    unread_only = {"content_hash": "", "loc": 0}
    for key, value in want.items():
        if key in ("version", "created_at"):
            continue
        assert row[key] == unread_only.get(key, value), key


@pytest.mark.asyncio
async def test_an_unread_oversize_file_the_denylist_refuses_is_tombstoned(monkeypatch) -> None:
    gone: list[str] = []

    async def tombstone(repo: Any, org: str, ns: str, *, path: str, embedding_model: str) -> None:
        gone.append(path)

    monkeypatch.setattr(ingest, "tombstone_file", tombstone)
    repo = _StubRepo()
    stats = await _record(repo, "node_modules/x/big.json", denylist=dl.Denylist(builtin=True))
    assert stats.excluded == "excluded: dir node_modules/" and stats.skipped
    assert gone == ["node_modules/x/big.json"] and repo.inserts == []


def test_a_commit_records_an_oversize_file_without_reading_it(tmp_path, monkeypatch) -> None:
    git_dir, sha = _commit(tmp_path, {"big.json": _BIG, "a.py": b"x = 1\n"})
    monkeypatch.setattr(settings, "code_ingest_max_file_bytes", 100)
    monkeypatch.setattr(settings, "code_ingest_round_files", 0)
    reads: list[tuple[str, int]] = []
    real = gitrepo.show_file

    async def show(git_dir: str, sha: str, path: str, max_bytes: int = 0) -> bytes | None:
        reads.append((path, max_bytes))
        return await real(git_dir, sha, path, max_bytes=max_bytes)

    ingested: list[str] = []

    async def ingest_file(repo: Any, org: str, ns: str, *, path: str, **kw: Any) -> Any:
        ingested.append(path)
        return SimpleNamespace(excluded="", skipped=True)

    repo = _StubRepo()
    result = CodeIngestResult()
    entries = [SimpleNamespace(path=p, status="M") for p in ("big.json", "a.py")]
    asyncio.run(
        pass_commit._ingest_commit_files(
            repo,
            "o",
            "code-ns",
            git_dir,
            sha,
            entries,
            embedder=SimpleNamespace(model_name="m"),
            llm=None,
            describe=False,
            embed_reuse_from="",
            denylist=None,
            result=result,
            gitrepo=SimpleNamespace(show_file=show),
            ingest_file=ingest_file,
            tombstone_file=None,
        )
    )
    assert reads == [("big.json", 100), ("a.py", 100)]
    assert ingested == ["a.py"]
    (row,) = repo.file_rows()
    assert row["path"] == "big.json" and row["blob_sha"] == blob_sha(_BIG)
    assert result.files_errored == 1


def test_a_dependents_batch_skips_an_oversize_file(tmp_path, monkeypatch) -> None:
    git_dir, sha = _commit(tmp_path, {"big.py": _BIG, "a.py": b"x = 1\n"})
    monkeypatch.setattr(settings, "code_ingest_max_file_bytes", 100)
    out = dependents.DependentsResult()
    got = asyncio.run(
        dependents._batch_sources(["big.py", "a.py", "gone.py"], git_dir, sha, show_file, out)
    )
    assert got == {"a.py": b"x = 1\n"} and out.skipped == 2


def test_a_heal_records_an_oversize_file_and_names_it_skipped(tmp_path, monkeypatch) -> None:
    git_dir, sha = _commit(tmp_path, {"big.json": _BIG, "a.py": b"x = 1\n"})
    monkeypatch.setattr(settings, "code_ingest_max_file_bytes", 100)
    monkeypatch.setattr(settings, "code_ingest_round_files", 0)
    ingested: list[str] = []

    async def ingest_file(repo: Any, org: str, ns: str, *, path: str, **kw: Any) -> Any:
        ingested.append(path)
        return SimpleNamespace(chunks_new=0, chunks_moved=0, symbols_moved=0)

    repo = _StubRepo()
    res = asyncio.run(
        reconcile.heal(
            repo,
            repo_config(org="o", namespace="code-ns", git_dir=git_dir, embed_reuse_from=""),
            sha,
            ["big.json", "a.py"],
            embedder=SimpleNamespace(model_name="m"),
            ingest_file=ingest_file,
            show_file=show_file,
        )
    )
    assert ingested == ["a.py"] and res.skipped_paths == ["big.json"] and res.errors == 0
    (row,) = repo.file_rows()
    assert row["path"] == "big.json" and row["index_error"].startswith("skipped: ")


# --- the repository's own ignore and attribute files ---


def test_an_oversize_attributes_file_gives_no_patterns_and_is_logged(tmp_path, caplog) -> None:
    from server.code.app.index.untrusted_files import PACKAGE_JSON_MAX_BYTES

    huge = b"*.gen linguist-generated\n" * (PACKAGE_JSON_MAX_BYTES // 25 + 1)
    git_dir, sha = _commit(tmp_path, {".gitattributes": huge, "a.py": b"x = 1\n"})
    cfg = repo_config(git_dir=git_dir, ignore_file="")
    with caplog.at_level("WARNING"):
        rules = asyncio.run(dl.load_denylist(cfg, sha, show_file))
    assert rules.patterns == ()
    assert ".gitattributes not read" in caplog.text


def test_the_repository_patterns_are_capped_and_the_cap_logged(caplog) -> None:
    ignore = "\n".join(f"gen{i}/" for i in range(dl.REPO_PATTERNS_MAX + 500))
    cfg = repo_config(exclude=("vendor/",))
    with caplog.at_level("WARNING"):
        rules = dl.denylist_for(cfg, ignore_text=ignore, attributes_text="x.c linguist-vendored")
    assert rules.patterns[0] == "vendor/"  # the operator's own excludes are never cut
    assert len(rules.patterns) == 1 + dl.REPO_PATTERNS_MAX
    assert f"{dl.REPO_PATTERNS_MAX + 501} ignore and attribute patterns" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data,reason",
    [
        (b"\x7fELF\x00\x00" + b"x" * 500, "excluded: binary"),
        (b"// Code generated by protoc. DO NOT EDIT.\n" + b"x" * 500, "excluded: generated"),
    ],
)
async def test_an_oversize_file_is_refused_by_its_first_bytes_as_before(
    monkeypatch, data: bytes, reason: str
) -> None:
    gone: list[str] = []

    async def tombstone(repo: Any, org: str, ns: str, *, path: str, embedding_model: str) -> None:
        gone.append(path)

    monkeypatch.setattr(ingest, "tombstone_file", tombstone)
    monkeypatch.setattr(settings, "code_ingest_max_file_bytes", 100)
    rules = dl.Denylist(exclude_generated=True)
    read, unread = _StubRepo(), _StubRepo()
    want = await ingest.ingest_file(
        read,
        "o",
        "code-ns",
        path="lib/x.go",
        source=data,
        commit_sha="c" * 40,
        embedding_model="m",
        denylist=rules,
    )
    got = await _record(unread, "lib/x.go", data, denylist=rules)
    assert got.excluded.startswith(reason)
    assert (got.excluded, got.declared_only, got.error) == (
        want.excluded,
        want.declared_only,
        want.error,
    )
    if want.declared_only:  # a generated file keeps its row with the size reason
        assert [r["index_error"] for r in unread.file_rows()] == [want.error]
        assert [r["index_error"] for r in read.file_rows()] == [want.error] and gone == []
    else:  # a binary is tombstoned, both ways
        assert gone == ["lib/x.go", "lib/x.go"] and unread.inserts == read.inserts == []


def test_a_heal_counts_a_failed_oversize_record_and_goes_on(tmp_path, monkeypatch) -> None:
    git_dir, sha = _commit(tmp_path, {"big.json": _BIG, "a.py": b"x = 1\n"})
    monkeypatch.setattr(settings, "code_ingest_max_file_bytes", 100)
    monkeypatch.setattr(settings, "code_ingest_round_files", 0)

    async def boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("insert failed")

    monkeypatch.setattr(ingest, "record_oversize", boom)
    ingested: list[str] = []

    async def ingest_file(repo: Any, org: str, ns: str, *, path: str, **kw: Any) -> Any:
        ingested.append(path)
        return SimpleNamespace(chunks_new=0, chunks_moved=0, symbols_moved=0)

    res = asyncio.run(
        reconcile.heal(
            _StubRepo(),
            repo_config(org="o", namespace="code-ns", git_dir=git_dir, embed_reuse_from=""),
            sha,
            ["big.json", "a.py"],
            embedder=SimpleNamespace(model_name="m"),
            ingest_file=ingest_file,
            show_file=show_file,
        )
    )
    assert ingested == ["a.py"] and res.errors == 1


def test_an_instruction_file_over_the_cap_is_reported_not_linted(tmp_path, monkeypatch) -> None:
    from server.code.app.index import instructions_lint as il

    git_dir, _sha = _commit(tmp_path, {"CLAUDE.md": b"# x\n" * 100, "AGENTS.md": b"see `a.py`\n"})
    monkeypatch.setattr(settings, "code_ingest_max_file_bytes", 100)
    branch = subprocess.run(
        ["git", "--git-dir", git_dir, "symbolic-ref", "--short", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    cfg = repo_config(git_dir=git_dir, git_branch=branch)
    report = asyncio.run(il.lint_repo(cfg))
    by_path = {f.path: f for f in report.files}
    assert by_path["CLAUDE.md"].skipped.startswith("over code_ingest_max_file_bytes")
    assert by_path["AGENTS.md"].skipped == ""
    assert "CLAUDE.md  (not linted: over code_ingest_max_file_bytes" in il.render_report(
        report, stale_only=True
    )


def test_a_memory_link_read_of_an_oversize_file_is_a_parse_failure(tmp_path, monkeypatch) -> None:
    from server.code.app import memory_links

    git_dir, sha = _commit(tmp_path, {"big.py": _BIG, "a.py": b"def f():\n    return 1\n"})
    monkeypatch.setattr(settings, "code_ingest_max_file_bytes", 100)
    monkeypatch.setattr(
        memory_links, "repo_for_namespace", lambda org, ns: SimpleNamespace(git_dir=git_dir)
    )
    with pytest.raises(memory_links.ParseFailed):
        asyncio.run(memory_links.parsed("o", "code-ns", sha, "big.py"))
    with pytest.raises(memory_links.ParseFailed):
        asyncio.run(memory_links.names_at("o", "code-ns", sha, "big.py"))
    assert asyncio.run(memory_links.names_at("o", "code-ns", sha, "a.py"))
