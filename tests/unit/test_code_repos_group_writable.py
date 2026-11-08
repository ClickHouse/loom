"""With `code_repos_shared_gid` naming the group that owns the code repos root, what
Loom creates there is writable by that group and closed to other users, so the host
operator and the containers (uid 999) can each fetch into and check out what the other
made. Unset, mirrors and checkouts stay the owner's alone."""

from __future__ import annotations

import os
import stat
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from server.code.app.index.repo_modes import REPO_UMASK, repo_umask
from tests.unit.code_fixtures import repo_config


@pytest.fixture
def under_repo_umask(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The process umask a server or a `loom code` command runs with once
    `code_repos_shared_gid` names a group."""
    _shared_gid(monkeypatch, os.getegid())
    with repo_umask():
        yield


def _shared_gid(monkeypatch: pytest.MonkeyPatch, gid: int | None) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "code_repos_shared_gid", gid)


def _share(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """Prepare `root` as the runbook does: its group named as the shared one, and the
    setgid bit so what is created under it keeps that group."""
    os.chmod(root, 0o2775)
    _shared_gid(monkeypatch, root.stat().st_gid)


def _git(*argv: str, cwd: Path | None = None) -> None:
    env = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    subprocess.run(["git", *argv], cwd=cwd, env=env, check=True, capture_output=True)


def _bare_with_a_commit(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    _git("init", "-q", "-b", "main", str(work))
    (work / "a.txt").write_text("a\n")
    _git("add", "a.txt", cwd=work)
    _git("commit", "-q", "-m", "a", cwd=work)
    bare = tmp_path / "r.git"
    _git("clone", "-q", "--bare", str(work), str(bare))
    return bare


def _not_shared(root: Path) -> list[str]:
    """Every path under `root` whose group bits differ from its owner's (git keeps its
    objects read-only for both), or that others may use at all."""
    bad = []
    for path in [root, *root.rglob("*")]:
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            continue
        if (mode >> 3) & 0o7 != (mode >> 6) & 0o7 or mode & 0o007:
            bad.append(f"{path.relative_to(root)} {stat.filemode(mode)}")
    return bad


def test_a_new_checkout_and_what_git_writes_in_it_are_group_writable(
    tmp_path: Path, under_repo_umask: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.code.app.index.compiler_tier import resolve_checkout

    bare = _bare_with_a_commit(tmp_path)
    _share(monkeypatch, tmp_path)
    cfg = repo_config(
        git_dir=str(bare), scip_checkout_dir=str(tmp_path / "checkout"), scip_producer="go"
    )
    checkout, _bare, missing = resolve_checkout(cfg, "go")
    assert missing is None
    assert stat.S_IMODE(checkout.stat().st_mode) == 0o2770  # setgid kept
    _git("fetch", "-q", str(bare), "main", cwd=checkout)
    _git("checkout", "-q", "FETCH_HEAD", cwd=checkout)
    assert (checkout / ".git" / "FETCH_HEAD").is_file()
    assert _not_shared(checkout) == []


@pytest.mark.asyncio
async def test_a_cloned_mirror_and_its_lock_file_are_group_writable(
    tmp_path: Path, under_repo_umask: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real clone from a local remote, run as a repository job (sandboxed where the
    host has bubblewrap; `test_repo_job_sandbox_hostile` shows the sandbox keeps the
    umask)."""
    from server.code.app.index import bootstrap

    source = _bare_with_a_commit(tmp_path)
    root = tmp_path / "code_repos"
    root.mkdir()
    _share(monkeypatch, root)
    git_dir = root / "mirror.git"
    cfg = repo_config(git_dir=str(git_dir), git_remote=str(source))
    assert await bootstrap.clone_mirror(cfg) == ""
    assert stat.S_IMODE(git_dir.stat().st_mode) == 0o2770
    assert _not_shared(git_dir) == []
    lock = root / "mirror.git.clone-lock"
    assert stat.S_IMODE(lock.stat().st_mode) == 0o660


@pytest.mark.parametrize("shared", [True, False], ids=["shared", "unset"])
def test_a_loom_code_command_runs_under_the_repo_umask_and_restores_it(
    monkeypatch: pytest.MonkeyPatch, shared: bool
) -> None:
    """Every `loom code` subcommand runs through `code_run._run`; with no shared gid it
    keeps the caller's umask."""
    from server.cli import code_run

    _shared_gid(monkeypatch, os.getegid() if shared else None)
    seen: list[int] = []

    async def sub() -> int:
        seen.append(os.umask(0o022))
        os.umask(seen[-1])
        return 0

    async def no_disconnect(coro: Any) -> int:
        return await coro

    monkeypatch.setattr(code_run, "_then_disconnect", no_disconnect)
    before = os.umask(0o022)
    try:
        assert code_run._run("t", sub()) == 0
        assert seen == [REPO_UMASK if shared else 0o022]
        assert os.umask(0o022) == 0o022
    finally:
        os.umask(before)


@pytest.mark.asyncio
async def test_the_server_lifespan_runs_under_the_repo_umask_and_restores_it_on_a_failed_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import asynccontextmanager

    from server import main

    seen: list[int] = []

    @asynccontextmanager
    async def failing(_app: Any) -> Any:
        seen.append(os.umask(0o022))
        os.umask(seen[-1])
        raise RuntimeError("startup failed")
        yield

    monkeypatch.setattr(main, "lifespan", failing)
    _shared_gid(monkeypatch, os.getegid())
    before = os.umask(0o022)
    try:
        with pytest.raises(RuntimeError):
            async with main.app.router.lifespan_context(main.app):
                pass
        assert seen == [REPO_UMASK]
        assert os.umask(0o022) == 0o022
    finally:
        os.umask(before)


@pytest.mark.asyncio
async def test_the_server_lifespan_restores_the_umask_after_a_normal_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import asynccontextmanager

    from server import main

    seen: list[int] = []

    @asynccontextmanager
    async def serving(_app: Any) -> Any:
        yield
        seen.append(os.umask(0o022))  # the inline workers' last writes
        os.umask(seen[-1])

    monkeypatch.setattr(main, "lifespan", serving)
    _shared_gid(monkeypatch, os.getegid())
    before = os.umask(0o022)
    try:
        async with main.app.router.lifespan_context(main.app):
            pass
        assert seen == [REPO_UMASK]
        assert os.umask(0o022) == 0o022
    finally:
        os.umask(before)


@pytest.mark.parametrize(
    ("shared", "before", "after", "level"),
    [
        (True, 0o755, 0o2770, "WARNING"),  # other users shut out
        (True, 0o700, 0o2770, "DEBUG"),  # only the group's bits added
        (False, 0o2770, 0o700, "WARNING"),  # not shared: the group shut out
    ],
    ids=["others", "group", "unshared"],
)
def test_restrict_dir_warns_only_when_it_takes_access_away(
    tmp_path: Path,
    shared: bool,
    before: int,
    after: int,
    level: str,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adding the group's bits is logged at debug: an older build sharing the root
    undoes it every round, and a warning each time would only be noise."""
    import logging

    from server.code.app.index.bootstrap import restrict_dir

    os.chmod(tmp_path, 0o2775)  # a setgid parent alone does not share it
    if shared:
        _share(monkeypatch, tmp_path)
    else:
        _shared_gid(monkeypatch, None)
    path = tmp_path / "r.git"
    path.mkdir()
    os.chmod(path, before)
    with caplog.at_level(logging.DEBUG, logger="loom.code_index.bootstrap"):
        restrict_dir(str(path))
    assert stat.S_IMODE(path.stat().st_mode) == after
    assert [r.levelname for r in caplog.records if "set to mode" in r.getMessage()] == [level]


def test_with_no_shared_gid_a_lock_file_is_the_owners_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.code.app.index.mirror_locks import try_lock

    _shared_gid(monkeypatch, None)
    lock = tmp_path / "r.git.fetch-lock"
    fd = try_lock(str(lock))
    assert fd is not None
    os.close(fd)
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600


def test_a_directory_outside_the_shared_group_stays_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2770 opens a directory to its own group: one whose group is not the shared gid
    (a parent without the setgid bit gave it the creator's) stays 0700."""
    from server.code.app.index.bootstrap import restrict_dir

    path = tmp_path / "r.git"
    path.mkdir()
    os.chmod(path, 0o755)
    _shared_gid(monkeypatch, path.stat().st_gid + 1)
    restrict_dir(str(path))
    assert stat.S_IMODE(path.stat().st_mode) == 0o700


@pytest.mark.asyncio
async def test_a_polled_lock_file_is_group_writable_with_a_shared_gid(
    tmp_path: Path, under_repo_umask: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.code.app.index.mirror_locks import lock_file

    _share(monkeypatch, tmp_path)

    lock = tmp_path / "r.git.fetch-lock"
    fd = await lock_file(str(lock))
    os.close(fd)
    assert stat.S_IMODE(lock.stat().st_mode) == 0o660


@pytest.mark.parametrize(("shared", "mode"), [(True, 0o660), (False, 0o600)])
def test_try_lock_is_group_writable_only_in_a_directory_of_the_shared_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, under_repo_umask: None, shared: bool, mode: int
) -> None:
    """A lock in a directory of another group (a team's setgid directory) gives that
    group nothing, as a mirror there would."""
    from server.code.app.index.mirror_locks import try_lock

    gid = tmp_path.stat().st_gid
    _shared_gid(monkeypatch, gid if shared else gid + 1)
    lock = tmp_path / "r.git.fetch-lock"
    fd = try_lock(str(lock))
    assert fd is not None
    os.close(fd)
    assert stat.S_IMODE(lock.stat().st_mode) == mode


def test_the_shared_gid_setting_refuses_a_negative_gid() -> None:
    from pydantic import ValidationError

    from server.settings import Settings

    with pytest.raises(ValidationError):
        Settings(_env_file=None, code_repos_shared_gid=-1)  # type: ignore[call-arg]


def test_a_checkout_under_another_groups_setgid_parent_stays_private(
    tmp_path: Path, under_repo_umask: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A team's setgid directory gives a checkout its group, but that is not the shared
    gid: the checkout stays the owner's alone."""
    from server.code.app.index.compiler_tier import resolve_checkout

    bare = _bare_with_a_commit(tmp_path)
    os.chmod(tmp_path, 0o2775)
    _shared_gid(monkeypatch, tmp_path.stat().st_gid + 1)
    cfg = repo_config(
        git_dir=str(bare), scip_checkout_dir=str(tmp_path / "checkout"), scip_producer="go"
    )
    checkout, _bare, missing = resolve_checkout(cfg, "go")
    assert missing is None
    assert stat.S_IMODE(checkout.stat().st_mode) == 0o700


def test_with_no_shared_gid_a_private_directory_with_a_setgid_bit_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """2700 gives nobody but the owner anything; an older build left checkouts so under
    a setgid root, and the other uid could not chmod them anyway."""
    import logging

    from server.code.app.index.bootstrap import restrict_dir

    _shared_gid(monkeypatch, None)
    path = tmp_path / "checkout"
    path.mkdir()
    os.chmod(path, 0o2700)
    with caplog.at_level(logging.DEBUG, logger="loom.code_index.bootstrap"):
        restrict_dir(str(path))
    assert stat.S_IMODE(path.stat().st_mode) == 0o2700
    assert not caplog.records
