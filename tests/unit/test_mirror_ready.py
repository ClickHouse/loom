"""mirror_ready: a host without a repo's bare mirror clones it from the remote
ClickHouse names, once per mirror however many reads ask, and does not retry a
failed clone on every read."""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest

from server.code.app.index import bootstrap, mirror_locks


class _Cfg:
    def __init__(self, git_dir: str, git_remote: str = "https://github.com/o/r.git") -> None:
        self.org, self.namespace, self.label = "o", "code-r", "o/code-r"
        self.git_dir, self.git_remote = git_dir, git_remote
        self.git_branch, self.github_token, self.classification = "main", "", "standard"
        self.source = "config"


@pytest.fixture(autouse=True)
def _no_clones(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bootstrap, "_MIRROR_JOBS", {})
    monkeypatch.setattr(bootstrap, "_HAVE", set())
    monkeypatch.setattr(bootstrap, "_FOREIGN", {})


def _clones(monkeypatch: pytest.MonkeyPatch, *, reason: str = "", delay_s: float = 0.0):
    calls: list[str] = []

    async def _clone(cfg: Any, *, pin: bool = False) -> str:
        calls.append(cfg.git_dir)
        await asyncio.sleep(delay_s)
        if not reason:
            os.makedirs(cfg.git_dir)
        return reason

    monkeypatch.setattr(bootstrap, "clone_mirror", _clone)
    return calls


@pytest.mark.asyncio
async def test_a_present_mirror_is_ready_without_a_clone(monkeypatch, tmp_path) -> None:
    calls = _clones(monkeypatch)
    assert await bootstrap.mirror_ready(_Cfg(str(tmp_path))) is True
    assert calls == []


@pytest.mark.asyncio
async def test_concurrent_reads_of_a_missing_mirror_start_one_clone(monkeypatch, tmp_path) -> None:
    calls = _clones(monkeypatch, delay_s=0.05)
    cfg = _Cfg(str(tmp_path / "r.git"))
    got = await asyncio.gather(*(bootstrap.mirror_ready(cfg) for _ in range(5)))
    # The read that started the clone waited for it; the others answered at once.
    assert sorted(got) == [False] * 4 + [True] and calls == [cfg.git_dir]
    assert await bootstrap.mirror_ready(cfg) is True and len(calls) == 1


@pytest.mark.asyncio
async def test_a_slow_clone_is_waited_on_bounded_and_keeps_running(monkeypatch, tmp_path) -> None:
    calls = _clones(monkeypatch, delay_s=0.2)
    cfg = _Cfg(str(tmp_path / "r.git"))
    assert await bootstrap.mirror_ready(cfg, wait_s=0.01) is False
    assert await bootstrap.mirror_ready(cfg, wait_s=2.0) is False  # running: no second wait
    await asyncio.sleep(0.3)
    assert await bootstrap.mirror_ready(cfg) is True and len(calls) == 1


@pytest.mark.asyncio
async def test_a_failed_clone_is_not_retried_within_the_retry_interval(
    monkeypatch, tmp_path
) -> None:
    calls = _clones(monkeypatch, reason="fatal: repository not found")
    cfg = _Cfg(str(tmp_path / "r.git"))
    assert await bootstrap.mirror_ready(cfg) is False
    assert await bootstrap.mirror_ready(cfg) is False and len(calls) == 1

    task, started = bootstrap._MIRROR_JOBS["clone", cfg.git_dir]
    bootstrap._MIRROR_JOBS["clone", cfg.git_dir] = (task, started - bootstrap._MIRROR_RETRY_S)
    assert await bootstrap.mirror_ready(cfg) is False and len(calls) == 2


@pytest.mark.asyncio
async def test_a_cfg_without_a_remote_is_not_ready(monkeypatch, tmp_path) -> None:
    calls = _clones(monkeypatch)
    assert await bootstrap.mirror_ready(_Cfg(str(tmp_path / "r.git"), git_remote="")) is False
    assert calls == []


def _git_with(monkeypatch: pytest.MonkeyPatch, have: set[str]) -> list[str]:
    """`cat-file -e` answers from `have`; a fetch adds "new" to it. Returns the fetches."""
    from server.code.app.index import gitrepo

    fetched: list[str] = []

    async def _run(git_dir: str, *args: str, **_kw: Any) -> str:
        if args[-1].split("^")[0] not in have:
            raise gitrepo.GitError("missing")
        return ""

    async def _fetch(cfg: Any) -> str:
        fetched.append(cfg.git_dir)
        await asyncio.sleep(0.05)
        have.add("new")
        return ""

    monkeypatch.setattr(gitrepo, "run", _run)
    monkeypatch.setattr(bootstrap, "fetch_mirror", _fetch)
    return fetched


@pytest.mark.asyncio
async def test_a_mirror_behind_the_indexed_head_answers_at_once_and_fetches_once(
    monkeypatch, tmp_path
) -> None:
    """A mirror cloned on a web host falls behind; a read that wants the indexed head
    is not held for the fetch: it starts one in the background, once however many
    reads ask, and the mirror answers as it is meanwhile."""
    from server.code.app.index import gitrepo

    have = {"old"}
    fetched = _git_with(monkeypatch, have)
    cfg = _Cfg(str(tmp_path))
    assert await bootstrap.mirror_ready(cfg, want="old") is True and fetched == []
    got = await asyncio.wait_for(
        asyncio.gather(*(bootstrap.mirror_ready(cfg, want="new") for _ in range(3))), 0.04
    )
    assert got == [False] * 3 and fetched == [cfg.git_dir]
    await asyncio.sleep(0.1)
    assert await bootstrap.mirror_ready(cfg, want="new") is True
    monkeypatch.setattr(gitrepo, "run", None)  # the head is known present: no git call
    assert await bootstrap.mirror_ready(cfg, want="new") is True


@pytest.mark.asyncio
async def test_a_head_that_is_not_the_branchs_is_not_fetched(monkeypatch, tmp_path) -> None:
    """A PR head is not on the configured branch: fetching the branch cannot bring it,
    so it is not fetched, and not asked about again within the retry interval."""
    fetched = _git_with(monkeypatch, {"old"})
    asked: list[str] = []

    async def _not_indexed() -> bool:
        asked.append("x")
        return False

    cfg = _Cfg(str(tmp_path))
    for _ in range(3):
        assert await bootstrap.mirror_ready(cfg, want="pr", fetchable=_not_indexed) is False
    assert fetched == [] and asked == ["x"]


@pytest.mark.asyncio
async def test_a_lesson_read_fetches_only_the_indexed_head(monkeypatch, tmp_path) -> None:
    from server.code.app import memory_links

    fetched = _git_with(monkeypatch, {"old"})
    cfg = _Cfg(str(tmp_path))
    monkeypatch.setattr(memory_links, "repo_for_namespace", lambda org, ns: cfg)

    async def _indexed(org: str, ns: str) -> str:
        return "new"

    monkeypatch.setattr(memory_links, "indexed_head", _indexed)
    assert await memory_links.mirror_here("o", "code-r", "pr-head") is False and fetched == []
    assert await memory_links.mirror_here("o", "code-r", "new") is False
    assert fetched == [cfg.git_dir]


@pytest.mark.asyncio
async def test_a_failed_job_is_logged_once_not_by_every_read(monkeypatch, tmp_path, caplog) -> None:
    _clones(monkeypatch, reason="fatal: repository not found")
    cfg = _Cfg(str(tmp_path / "r.git"))
    with caplog.at_level("WARNING", logger="loom.code_index.bootstrap"):
        for _ in range(4):
            await bootstrap.mirror_ready(cfg)
    assert sum("repository not found" in r.getMessage() for r in caplog.records) == 1


@pytest.mark.asyncio
async def test_clone_on_read_off_clones_nothing(monkeypatch, tmp_path) -> None:
    from server.settings import settings

    calls = _clones(monkeypatch)
    monkeypatch.setattr(settings, "code_history_clone_on_read", False)
    assert await bootstrap.mirror_ready(_Cfg(str(tmp_path / "r.git"))) is False and calls == []


@pytest.mark.asyncio
async def test_a_fresh_clone_pins_the_ledger_merges_it_lacks(monkeypatch, tmp_path) -> None:
    """A merge on a staging branch upstream deleted is not in a fresh clone: it is fetched
    by sha, page by page, in batches."""
    from server.code.app.index import gitrepo
    from server.code.domain.index import repos
    from server.code.store import index as store
    from server.settings import settings

    cfg = _Cfg(str(tmp_path / "r.git"))
    ledger = [(p, f"{p:040x}") for p in range(7, 0, -1)]
    pages: list[int] = []

    class _Repo:
        async def merge_shas_page(self, org: str, ns: str, *, below_pr: int = 0, limit: int = 0):
            pages.append(below_pr)
            rows = [r for r in ledger if not below_pr or r[0] < below_pr]
            return rows[:limit]

    async def _missing(git_dir: str, shas: list[str]) -> list[str]:
        return [s for s in shas if int(s, 16) % 2]

    sent: list[list[str]] = []

    async def _fetch_shas(git_dir: str, remote: str, shas: list[str], *, token: str = "") -> int:
        sent.append(list(shas))
        return len(shas)

    monkeypatch.setattr(store, "CodeRepo", _Repo)
    monkeypatch.setattr(repos, "configured_repos", lambda: [cfg])
    monkeypatch.setattr(gitrepo, "missing_commits", _missing)
    monkeypatch.setattr(gitrepo, "fetch_shas", _fetch_shas)
    monkeypatch.setattr(settings, "code_git_ledger_merge_page", 3)
    monkeypatch.setattr(settings, "code_git_ledger_merge_batch", 1)
    await bootstrap._pin_ledger_merges(cfg)
    assert pages == [0, 5, 2]
    assert [int(s[0], 16) for s in sent] == [7, 5, 3, 1]


@pytest.mark.asyncio
async def test_a_clone_waits_for_another_process_cloning_the_same_mirror(
    monkeypatch, tmp_path
) -> None:
    """The processes of one host share the mirror directory: the second clone
    takes the lock after the first and finds the mirror there."""
    import fcntl

    cfg = _Cfg(str(tmp_path / "r.git"))
    pinned: list[str] = []

    async def _pin(c: Any) -> None:
        pinned.append(c.git_dir)

    monkeypatch.setattr(bootstrap, "_pin_ledger_merges", _pin)
    held = await mirror_locks.lock_file(cfg.git_dir + ".clone-lock")
    clone = asyncio.create_task(bootstrap.clone_mirror(cfg, pin=True))
    await asyncio.sleep(0.05)
    assert not clone.done()  # waiting on the lock
    os.makedirs(cfg.git_dir)  # the other process's clone landed
    fcntl.flock(held, fcntl.LOCK_UN)
    os.close(held)
    assert await asyncio.wait_for(clone, 5) == ""
    assert pinned == []  # the other process's clone pinned its own merges


@pytest.mark.asyncio
async def test_a_killed_clones_temp_directory_is_removed_by_the_next_clone(
    monkeypatch, tmp_path
) -> None:
    from types import SimpleNamespace

    from server.code.app.index import sandbox

    cfg = _Cfg(str(tmp_path / "r.git"))
    os.makedirs(cfg.git_dir + ".partial-1234-5678")  # left by a SIGKILL mid-clone

    async def _job(argv: list[str], **_kw: Any) -> Any:
        os.makedirs(os.path.join(argv[-1], "objects"))
        return SimpleNamespace(returncode=0, stderr=b"")

    pinned: list[str] = []

    async def _pin(c: Any) -> None:
        pinned.append(c.git_dir)

    monkeypatch.setattr(sandbox, "run_job", _job)
    monkeypatch.setattr(bootstrap, "_pin_ledger_merges", _pin)
    assert await bootstrap.clone_and_pin(cfg) == ""
    assert (tmp_path / "r.git").is_dir() and pinned == [cfg.git_dir]
    assert not [p for p in tmp_path.iterdir() if ".partial-" in p.name]


@pytest.mark.asyncio
async def test_a_cancelled_wait_for_the_clone_lock_holds_nothing(tmp_path) -> None:
    import fcntl

    path = str(tmp_path / "r.git.clone-lock")
    held = await mirror_locks.lock_file(path)
    waiter = asyncio.create_task(mirror_locks.lock_file(path))
    await asyncio.sleep(0.05)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    fcntl.flock(held, fcntl.LOCK_UN)
    os.close(held)
    again = await asyncio.wait_for(mirror_locks.lock_file(path), 2)  # nobody kept it
    os.close(again)


@pytest.mark.asyncio
async def test_a_lesson_read_starts_the_clone_without_waiting_for_it(monkeypatch, tmp_path) -> None:
    from server.code.app import memory_links

    calls = _clones(monkeypatch, delay_s=0.05)
    cfg = _Cfg(str(tmp_path / "r.git"))
    monkeypatch.setattr(memory_links, "repo_for_namespace", lambda org, ns: cfg)
    assert await memory_links.mirror_here("o", "code-r", "") is False and calls == [cfg.git_dir]
    await asyncio.sleep(0.1)
    assert await memory_links.mirror_here("o", "code-r", "") is True and len(calls) == 1
    monkeypatch.setattr(memory_links, "repo_for_namespace", lambda org, ns: None)
    assert await memory_links.mirror_here("o", "code-x", "") is False


@pytest.mark.asyncio
async def test_a_history_reads_clone_wait_is_capped_by_the_ops_budget(
    monkeypatch, tmp_path
) -> None:
    """code.bisect_candidates has 1.5 s: a read that has spent it waits for no clone."""
    import time

    import server.operations.registry  # noqa: F401 — imported once, as a server has it
    from server.code.app import blame
    from server.core import request_work

    calls = _clones(monkeypatch, delay_s=0.5)
    cfg = _Cfg(str(tmp_path / "r.git"))
    request_work.begin().began = time.perf_counter() - 1.4  # 0.1 s of the budget left
    t0 = time.monotonic()
    assert await blame.history_git_dir(cfg, "code.bisect_candidates") == (
        "",
        "no_git_mirror: cloning o/code-r",
    )
    assert time.monotonic() - t0 < 0.4 and calls == [cfg.git_dir]


@pytest.mark.asyncio
async def test_a_blame_read_fetches_toward_the_head_it_read(monkeypatch, tmp_path) -> None:
    from server.code.app import blame

    fetched = _git_with(monkeypatch, {"old"})
    cfg = _Cfg(str(tmp_path))
    assert await blame.history_git_dir(cfg, "code.blame_range", head="new") == (str(tmp_path), "")
    await asyncio.sleep(0.01)
    assert fetched == [cfg.git_dir]


@pytest.mark.asyncio
async def test_a_viewer_added_network_remote_is_neither_cloned_nor_fetched_unsandboxed(
    monkeypatch, tmp_path
) -> None:
    """The worker's rule on the read path: without the sandbox no plain process
    reaches a remote a viewer registered (no egress proxy)."""
    from server.code.app.index import sandbox

    monkeypatch.setattr(sandbox, "_available", False)
    calls = _clones(monkeypatch)
    fetched = _git_with(monkeypatch, {"old"})
    cfg = _Cfg(str(tmp_path / "r.git"))
    cfg.source = "registry"
    assert await bootstrap.mirror_ready(cfg) is False and calls == []
    present = _Cfg(str(tmp_path))
    present.source = "registry"
    assert await bootstrap.mirror_ready(present, want="new") is False and fetched == []


@pytest.mark.asyncio
async def test_a_mirror_job_does_not_carry_the_requests_query_tag(monkeypatch, tmp_path) -> None:
    from server.core.ch import current_query_tag, set_query_tag

    seen: list[str] = []

    async def _clone(cfg: Any) -> str:
        seen.append(current_query_tag())
        os.makedirs(cfg.git_dir)
        return ""

    monkeypatch.setattr(bootstrap, "clone_and_pin", _clone)
    set_query_tag("http:code.blame_range")
    assert await bootstrap.mirror_ready(_Cfg(str(tmp_path / "r.git"))) is True
    assert seen == ["code.mirror_clone"]


@pytest.mark.asyncio
async def test_a_head_no_fetch_brings_backs_off_instead_of_fetching_every_read(
    monkeypatch, tmp_path
) -> None:
    fetched: list[str] = []
    from server.code.app.index import gitrepo

    async def _run(git_dir: str, *args: str, **_kw: Any) -> str:
        raise gitrepo.GitError("missing")

    async def _fetch(cfg: Any) -> str:
        fetched.append(cfg.git_dir)
        return ""  # the fetch succeeds but the remote no longer has the head

    monkeypatch.setattr(gitrepo, "run", _run)
    monkeypatch.setattr(bootstrap, "fetch_mirror", _fetch)
    cfg = _Cfg(str(tmp_path))
    assert await bootstrap.mirror_ready(cfg, want="gone") is False
    await asyncio.sleep(0.01)
    # the job interval passes: the old code fetched again on every read after it
    task, started = bootstrap._MIRROR_JOBS["fetch", cfg.git_dir]
    bootstrap._MIRROR_JOBS["fetch", cfg.git_dir] = (task, started - bootstrap._MIRROR_AGAIN_S)
    for _ in range(3):
        assert await bootstrap.mirror_ready(cfg, want="gone") is False
    assert fetched == [cfg.git_dir]
    until, tries = bootstrap._FOREIGN[cfg.git_dir, "gone"]
    assert tries == 1 and until > 0


@pytest.mark.asyncio
async def test_an_older_builds_partial_clone_is_removed_too(monkeypatch, tmp_path) -> None:
    from types import SimpleNamespace

    from server.code.app.index import sandbox

    cfg = _Cfg(str(tmp_path / "r.git"))
    os.makedirs(cfg.git_dir + ".partial")  # no suffix: an older build's temp dir

    async def _job(argv: list[str], **_kw: Any) -> Any:
        os.makedirs(os.path.join(argv[-1], "objects"))
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(sandbox, "run_job", _job)
    assert await bootstrap.clone_mirror(cfg) == ""
    assert not (tmp_path / "r.git.partial").exists()


class _StateRepo:
    def __init__(self, head: str) -> None:
        self.head, self.writes = head, []

    async def get_state(self, org: str, ns: str, key: str) -> str:
        return self.head if key == "git_head" else ""

    async def set_state(self, org: str, ns: str, key: str, value: str, detail: str = "") -> None:
        self.writes.append((key, value))


@pytest.mark.asyncio
async def test_a_reclone_of_an_indexed_entry_leaves_its_stage_alone(monkeypatch, tmp_path) -> None:
    """The card of an indexed entry does not stick at `cloning` while it re-clones."""
    _clones(monkeypatch)
    repo = _StateRepo(head="a" * 40)
    assert await bootstrap.staged_clone(_Cfg(str(tmp_path / "r.git")), repo) == ""
    assert bootstrap.STAGE_KEY not in [k for k, _ in repo.writes]
    fresh = _StateRepo(head="")
    assert await bootstrap.staged_clone(_Cfg(str(tmp_path / "s.git")), fresh) == ""
    assert (bootstrap.STAGE_KEY, "cloning") in fresh.writes


@pytest.mark.asyncio
async def test_the_leaders_clone_runs_in_the_background(monkeypatch, tmp_path) -> None:
    _clones(monkeypatch, delay_s=0.2)
    cfg = _Cfg(str(tmp_path / "r.git"))
    present, failed = await asyncio.wait_for(
        bootstrap.clone_in_background(cfg, _StateRepo("a" * 40)), 0.1
    )
    assert (present, failed) == (False, "")
    await asyncio.sleep(0.3)
    assert await bootstrap.clone_in_background(cfg, _StateRepo("a" * 40)) == (True, "")
