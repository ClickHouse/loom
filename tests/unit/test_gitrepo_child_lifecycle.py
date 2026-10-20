"""A git child never outlives the call that started it: a timeout, the
caller's cancellation or any error kills and reaps it."""

from __future__ import annotations

import asyncio
import shutil

import pytest

from server.code.app.index import gitrepo

pytestmark = pytest.mark.skipif(shutil.which("sleep") is None, reason="needs sleep(1)")


def _sleeping_git(
    monkeypatch: pytest.MonkeyPatch, started: list, spawned: asyncio.Event | None = None
) -> None:
    """Every git spawn becomes `sleep 30`, recorded so the test can inspect it."""
    real = asyncio.create_subprocess_exec

    async def _exec(*argv, **kw):
        kw.pop("env", None)
        proc = await real("sleep", "30", **kw)
        started.append(proc)
        if spawned is not None:
            spawned.set()
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)


def test_a_cancelled_read_kills_and_reaps_its_git(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list = []

    async def _scenario() -> None:
        spawned = asyncio.Event()
        _sleeping_git(monkeypatch, started, spawned)
        task = asyncio.create_task(gitrepo.show_file("/tmp/none.git", "a" * 40, "x.py"))
        await spawned.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_scenario())
    [proc] = started
    assert proc.returncode is not None  # killed and waited for, not left running


def test_a_timed_out_read_kills_and_reaps_its_git(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list = []
    _sleeping_git(monkeypatch, started)
    with pytest.raises(gitrepo.GitError, match="timed out"):
        asyncio.run(gitrepo.run("/tmp/none.git", "log", timeout_s=0.1))
    [proc] = started
    assert proc.returncode is not None


def test_a_git_command_that_names_no_timeout_gets_the_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.settings import settings

    started: list = []
    _sleeping_git(monkeypatch, started)
    monkeypatch.setattr(settings, "code_git_timeout_s", 0.1)
    with pytest.raises(gitrepo.GitError, match="timed out after 0.1s"):
        asyncio.run(gitrepo.run("/tmp/none.git", "log"))
    seen: list[float] = []

    async def _git(git_dir: str, *args: str, timeout_s: float | None = None, **kw) -> str:
        seen.append(timeout_s)  # type: ignore[arg-type]
        return ""

    monkeypatch.setattr(gitrepo, "_git", _git)
    monkeypatch.setattr(settings, "code_git_fetch_timeout_s", 42.0)
    asyncio.run(gitrepo.fetch("/tmp/none.git", "https://example.invalid/r.git", "main"))
    assert seen == [42.0]
