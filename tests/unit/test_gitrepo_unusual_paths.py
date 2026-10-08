"""The git readers the ingest walks by (ls_tree, ls_tree_blobs, diff_files,
diff_name_only) name a file the way show_file reads it. Git C-quotes a path
with non-ASCII bytes or a control character unless told otherwise, and a
quoted name reads back as a missing file: the file would never be indexed
and its deletion would retire nothing."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

import pytest

from server.code.app.index import gitrepo
from server.code.app.index.gitrepo import DiffEntry

PLAIN = "src/plain.py"
UNUSUAL = {
    "non_ascii": "src/café.py",
    "cjk": "docs/説明.md",
    "space": "src/with space.py",
    "tab": "src/tab\tname.py",
    "quote": 'src/say "hi".py',
}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture(autouse=True)
def _no_user_git_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A developer's core.quotePath=false must not be what makes these pass.
    empty = tmp_path / "gitconfig"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str, str]:
    wt = tmp_path / "w"
    wt.mkdir()
    _git(wt, "init", "-q", "-b", "main")
    _git(wt, "config", "user.email", "t@example.invalid")
    _git(wt, "config", "user.name", "t")
    (wt / "src").mkdir()
    (wt / "docs").mkdir()
    (wt / PLAIN).write_text("a = 1\n")
    (wt / "src/old é.py").write_text("gone = True\n")
    _git(wt, "add", ".")
    _git(wt, "commit", "-q", "-m", "base")
    base = _git(wt, "rev-parse", "HEAD")
    for path in UNUSUAL.values():
        (wt / path).write_text("b = 2\n")
    _git(wt, "mv", "src/old é.py", "src/new é.py")
    _git(wt, "add", ".")
    _git(wt, "commit", "-q", "-m", "unusual names and a rename")
    return wt / ".git", base, _git(wt, "rev-parse", "HEAD")


@pytest.mark.parametrize("path", [PLAIN, *UNUSUAL.values()])
def test_every_listed_path_is_one_show_file_reads(repo: tuple[Path, str, str], path: str) -> None:
    git_dir, _base, head = str(repo[0]), repo[1], repo[2]
    assert asyncio.run(gitrepo.show_file(git_dir, head, path)) is not None
    assert path in asyncio.run(gitrepo.ls_tree(git_dir, head))
    blobs = asyncio.run(gitrepo.ls_tree_blobs(git_dir, head))
    assert blobs[path] == _git(repo[0].parent, "rev-parse", f"{head}:{path}")


def test_ls_tree_lists_exactly_the_tree(repo: tuple[Path, str, str]) -> None:
    git_dir, _base, head = str(repo[0]), repo[1], repo[2]
    expected = sorted([PLAIN, "src/new é.py", *UNUSUAL.values()])
    assert sorted(asyncio.run(gitrepo.ls_tree(git_dir, head))) == expected
    assert sorted(asyncio.run(gitrepo.ls_tree_blobs(git_dir, head))) == expected
    assert asyncio.run(gitrepo.ls_tree(git_dir, head, ("docs/",))) == [UNUSUAL["cjk"]]


def test_the_diff_readers_name_the_real_paths_and_split_a_rename(
    repo: tuple[Path, str, str],
) -> None:
    git_dir, base, head = str(repo[0]), repo[1], repo[2]
    entries = asyncio.run(gitrepo.diff_files(git_dir, base, head))
    assert sorted(entries, key=lambda e: e.path) == sorted(
        [
            DiffEntry(status="D", path="src/old é.py"),
            DiffEntry(status="A", path="src/new é.py"),
            *(DiffEntry(status="A", path=p) for p in UNUSUAL.values()),
        ],
        key=lambda e: e.path,
    )
    assert sorted(asyncio.run(gitrepo.diff_name_only(git_dir, base, head))) == sorted(
        e.path for e in entries
    )
    assert len(asyncio.run(gitrepo.diff_name_only(git_dir, base, head, limit=2))) == 2


def test_name_status_records_with_two_paths_are_split() -> None:
    # --no-renames keeps R and C records out of the ingest's diff, and the parser
    # still reads them field by field: old path then new path
    out = "M\0src/a.py\0R087\0src/old\tx.py\0src/new\tx.py\0C100\0src/b.py\0src/c.py\0D\0z.py\0"
    assert gitrepo.parse_name_status_z(out) == [
        DiffEntry(status="M", path="src/a.py"),
        DiffEntry(status="D", path="src/old\tx.py"),
        DiffEntry(status="A", path="src/new\tx.py"),
        DiffEntry(status="A", path="src/c.py"),
        DiffEntry(status="D", path="z.py"),
    ]
    assert gitrepo.parse_name_status_z("") == []


@pytest.mark.parametrize(
    ("call", "rev_args"),
    [
        (lambda: gitrepo.ls_tree("/g", "a" * 40), 1),
        (lambda: gitrepo.ls_tree_blobs("/g", "a" * 40), 1),
        (lambda: gitrepo.diff_files("/g", "a" * 40, "b" * 40), 2),
        (lambda: gitrepo.diff_name_only("/g", "a" * 40, "b" * 40), 2),
    ],
)
def test_the_listings_ask_for_verbatim_nul_records_and_end_options_before_revs(
    monkeypatch: pytest.MonkeyPatch, call: Any, rev_args: int
) -> None:
    argv: list[tuple[str, ...]] = []

    class _Done:
        returncode = 0

        async def communicate(self, *_a: Any) -> tuple[bytes, bytes]:
            return b"", b""

    async def spawn(*args: str, **_kw: Any) -> _Done:
        argv.append(args)
        return _Done()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    asyncio.run(call())
    (args,) = argv
    assert args[:3] == ("git", "-c", "core.quotePath=false")
    assert "-z" in args
    end = args.index("--end-of-options")
    assert args[end + 1 :] == args[-rev_args:]
