"""This host's mirrors: the history gate, warm-up, the sweep, unpinnable ledger
merges and what /v1/health and `loom doctor` say about them."""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import pytest

from server.code.app.index import bootstrap, gitrepo, mirror_locks, mirrors, pass_fetch
from server.settings import settings


class _Cfg:
    def __init__(self, git_dir: str, ns: str = "code-r", source: str = "config") -> None:
        self.org, self.namespace, self.label = "o", ns, f"o/{ns}"
        self.git_dir, self.git_remote = git_dir, "https://github.com/o/r.git"
        self.git_branch, self.github_token, self.classification = "main", "", "standard"
        self.source, self.git_fetch_pr_heads = source, True
        self.scip_extra_producers: tuple[str, ...] = ()
        self.scip_checkout_dir = ""


@pytest.fixture(autouse=True)
def _fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bootstrap, "_MIRROR_JOBS", {})
    monkeypatch.setattr(mirrors, "_QUEUED", set())
    monkeypatch.setattr(mirrors, "_LISTED", asyncio.Event())
    monkeypatch.setattr(pass_fetch, "_UNPINNED", {})
    monkeypatch.setattr(pass_fetch, "_REFUSED_ONCE", set())
    monkeypatch.setattr(settings, "code_intel_enabled", True)


def _repos(monkeypatch: pytest.MonkeyPatch, cfgs: list[Any]) -> None:
    from server.code.domain.index import repos

    monkeypatch.setattr(repos, "configured_repos", lambda: cfgs)
    monkeypatch.setattr(repos, "config_error_count", lambda: 0)  # another test's bad JSON


@pytest.mark.asyncio
async def test_a_read_on_a_host_still_cloning_names_the_entry(monkeypatch, tmp_path) -> None:
    started = asyncio.Event()

    async def _slow(cfg: Any) -> str:
        started.set()
        await asyncio.sleep(1)
        return ""

    monkeypatch.setattr(bootstrap, "clone_and_pin", _slow)
    cfg = _Cfg(str(tmp_path / "r.git"))
    assert await mirrors.history_dir(cfg) == ("", "no_git_mirror: cloning o/code-r")
    await asyncio.wait_for(started.wait(), 1)  # the read started the clone
    assert await mirrors.history_dir(cfg) == ("", "no_git_mirror: cloning o/code-r")
    os.makedirs(cfg.git_dir)
    assert await mirrors.history_dir(cfg) == (cfg.git_dir, "")


@pytest.mark.asyncio
async def test_no_history_mode_reads_no_mirror(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(settings, "git_history", "none")
    assert await mirrors.history_dir(_Cfg(str(tmp_path))) == ("", mirrors.NO_GIT_HISTORY)
    assert mirrors.start_warmup() is None


@pytest.mark.asyncio
async def test_a_merge_the_remote_no_longer_serves_is_recorded_and_the_rest_fetched(
    monkeypatch, tmp_path
) -> None:
    gone, kept = "a" * 40, "b" * 40
    sent: list[list[str]] = []

    async def _fetch_shas(git_dir: str, remote: str, shas: list[str], *, token: str = "") -> int:
        sent.append(list(shas))
        if gone in shas:
            raise gitrepo.GitError(f"fatal: remote error: upload-pack: not our ref {gone}")
        return len(shas)

    monkeypatch.setattr(gitrepo, "fetch_shas", _fetch_shas)
    cfg = _Cfg(str(tmp_path))
    assert await pass_fetch.pin_merges(cfg, cfg.git_dir, [gone, kept]) == 1
    assert sent == [[gone, kept], [kept]]
    assert pass_fetch.unpinned(cfg.git_dir) == set()  # one refusal may be transient
    assert await pass_fetch.pin_merges(cfg, cfg.git_dir, [gone]) == 0 and len(sent) == 3
    assert await pass_fetch.pin_merges(cfg, cfg.git_dir, [gone]) == 0 and len(sent) == 3
    # kept in the mirror: a restarted process and the doctor read it
    pass_fetch._UNPINNED.clear()
    assert pass_fetch.unpinned(cfg.git_dir) == {gone}
    _repos(monkeypatch, [cfg])
    assert mirrors.mirror_status()["unpinned_merges"] == 1
    assert [n for n, *_ in mirrors.doctor_checks()] == [
        "code_mirrors",
        "code_mirror_unpinned_merges",
    ]


@pytest.mark.asyncio
async def test_any_other_fetch_failure_still_raises(monkeypatch, tmp_path) -> None:
    async def _fetch_shas(*_a: Any, **_k: Any) -> int:
        raise gitrepo.GitError("Could not resolve host: github.com")

    monkeypatch.setattr(gitrepo, "fetch_shas", _fetch_shas)
    with pytest.raises(gitrepo.GitError):
        await pass_fetch.pin_merges(_Cfg(str(tmp_path)), str(tmp_path), ["a" * 40])
    assert pass_fetch.unpinned(str(tmp_path)) == set()


def test_mirror_status_counts_and_names_no_entry(monkeypatch, tmp_path) -> None:
    """`/v1/health?include=mirrors` answers without a token: no org or namespace label."""
    ready = _Cfg(str(tmp_path / "a.git"), "code-a")
    os.makedirs(ready.git_dir)
    queued = _Cfg(str(tmp_path / "b.git"), "code-b")
    no_remote = _Cfg(str(tmp_path / "c.git"), "code-c")
    no_remote.git_remote = ""
    mirrors._QUEUED.add(queued.git_dir)
    _repos(monkeypatch, [ready, queued, no_remote])
    got = mirrors.mirror_status()
    assert got == {
        "ready": 1,
        "cloning": 1,
        "failed": {},
        "absent": {"names no remote to clone": 1},
        "unpinned_merges": 0,
    }


@pytest.mark.asyncio
async def test_warm_up_clones_indexed_entries_a_few_at_a_time(monkeypatch, tmp_path) -> None:
    from server.code.app.index import repo_registry
    from server.code.store import index as store

    cfgs = [_Cfg(str(tmp_path / f"{i}.git"), f"code-{i}") for i in range(4)]
    _repos(monkeypatch, cfgs)

    class _Repo:
        async def get_state(self, org: str, ns: str, key: str) -> str:
            return "" if ns == "code-3" else "a" * 40  # code-3 is still building

    async def _refresh(**_kw: Any) -> list[Any]:
        return []

    running, peak, cloned = 0, 0, []

    async def _clone(cfg: Any) -> str:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.02)
        running -= 1
        cloned.append(cfg.namespace)
        return ""

    monkeypatch.setattr(store, "CodeRepo", _Repo)
    monkeypatch.setattr(repo_registry, "refresh_db_repos", _refresh)
    monkeypatch.setattr(mirrors, "clone_and_pin", _clone)
    monkeypatch.setattr(mirrors, "_JITTER_S", 0.0)
    monkeypatch.setattr(settings, "code_mirror_warmup_concurrency", 2)
    await mirrors.warm_up()
    assert sorted(cloned) == ["code-0", "code-1", "code-2"] and peak == 2
    assert mirrors._QUEUED == set() and mirrors._LISTED.is_set()


@pytest.mark.asyncio
async def test_a_failed_warm_up_listing_is_retried(monkeypatch) -> None:
    calls: list[int] = []

    async def _warm_up() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("registry unreadable")
        mirrors._LISTED.set()

    monkeypatch.setattr(mirrors, "warm_up", _warm_up)
    monkeypatch.setattr(settings, "code_mirror_warmup_retry_s", 0.0)
    monkeypatch.setattr(settings, "code_mirror_warmup", True)
    monkeypatch.setattr(settings, "code_mirror_fetch_interval_s", 0.0)
    await mirrors._run()
    assert calls == [1, 1] and mirrors._LISTED.is_set()


@pytest.mark.asyncio
async def test_a_status_read_never_starts_a_clone(monkeypatch, tmp_path) -> None:
    started: list[str] = []

    async def _clone(cfg: Any) -> str:
        started.append(cfg.git_dir)
        return ""

    monkeypatch.setattr(mirrors, "clone_and_pin", _clone)
    monkeypatch.setattr(bootstrap, "clone_and_pin", _clone)
    cfg = _Cfg(str(tmp_path / "r.git"))
    assert await mirrors.present_dir(cfg) == ""
    await asyncio.sleep(0)
    assert started == []
    os.makedirs(cfg.git_dir)
    assert await mirrors.present_dir(cfg) == cfg.git_dir


@pytest.mark.asyncio
async def test_index_as_of_on_a_host_without_the_mirror_clones_nothing(
    monkeypatch, tmp_path
) -> None:
    from server.code.app import common
    from server.code.domain.index import repos

    cfg = _Cfg(str(tmp_path / "r.git"))

    async def _no_clone(*_a: Any, **_k: Any) -> bool:
        raise AssertionError("a status read started a clone")

    class _Repo:
        async def all_state(self, org: str, ns: str) -> dict[str, Any]:
            return {"git_head": {"value": "a" * 40}}

    monkeypatch.setattr(mirrors, "mirror_ready", _no_clone)
    monkeypatch.setattr(repos, "repo_for_namespace", lambda org, ns: cfg)
    common.invalidate_index_as_of("o", "code-r")
    block = await common.index_as_of(_Repo(), "o", "code-r")  # type: ignore[arg-type]
    assert block["commit"] == "a" * 40


def _old(path: Any) -> None:
    os.makedirs(path)
    t = time.time() - 2 * mirrors._SWEEP_MIN_AGE_S
    os.utime(path, (t, t))


def _old_file(path: Any) -> None:
    path.write_text("")
    t = time.time() - 2 * mirrors._SWEEP_MIN_AGE_S
    os.utime(path, (t, t))


@pytest.mark.asyncio
async def test_the_sweep_removes_what_no_active_entry_names(monkeypatch, tmp_path) -> None:
    from server.code.store import index as store

    root = tmp_path / "self"
    monkeypatch.setattr(settings, "code_repos_root", str(root))
    monkeypatch.setattr(settings, "code_self_service_enabled", True)
    for name in ("o__kept.git", "o__removed.git", "checkout-code-kept", "checkout-code-gone"):
        _old(root / name)
    _old(root / "o__kept.git.partial-1234-5678")  # a half-done clone
    (root / "o__young.git").mkdir()  # cloned a moment ago for an entry just added
    for lock in ("o__kept.git.fetch-lock", "o__removed.git.fetch-lock", "o__gone.git.clone-lock"):
        _old_file(root / lock)

    class _Repo:
        async def registry_rows(self) -> list[dict[str, Any]]:
            return [{"org": "o", "namespace": "code-kept", "repo": "o/kept", "active": True}]

    monkeypatch.setattr(store, "CodeRepo", _Repo)
    _repos(monkeypatch, [])
    removed = {os.path.basename(p) for p in await mirrors.sweep()}
    assert removed == {
        "o__removed.git",
        "checkout-code-gone",
        "o__kept.git.partial-1234-5678",
        "o__removed.git.fetch-lock",
        "o__gone.git.clone-lock",
    }
    assert {p.name for p in root.iterdir() if p.is_file()} == {
        "o__kept.git.fetch-lock",
        "o__kept.git.clone-lock",
        "o__removed.git.clone-lock",  # taken to remove the mirror: a later sweep's
        ".sweep-lock",
    }
    assert sorted(p.name for p in root.iterdir() if p.is_dir()) == [
        "checkout-code-kept",
        "o__kept.git",
        "o__young.git",
    ]


@pytest.mark.asyncio
async def test_a_mirror_named_like_a_temp_dir_is_swept_as_a_mirror(monkeypatch, tmp_path) -> None:
    """A self-service name never holds a dot, so such a mirror is no entry's; it
    goes under its own clone lock, and its clone's temp dir under the same."""
    from server.code.store import index as store

    root = tmp_path / "self"
    monkeypatch.setattr(settings, "code_repos_root", str(root))
    monkeypatch.setattr(settings, "code_self_service_enabled", True)
    for name in ("o__x.git.partial-1.git", "o__x.git.partial-1.git.partial-3-4"):
        _old(root / name)

    class _Repo:
        async def registry_rows(self) -> list[dict[str, Any]]:
            return []

    monkeypatch.setattr(store, "CodeRepo", _Repo)
    _repos(monkeypatch, [])
    removed = {os.path.basename(p) for p in await mirrors.sweep()}
    assert removed == {"o__x.git.partial-1.git", "o__x.git.partial-1.git.partial-3-4"}
    locks = {p.name for p in root.iterdir() if p.name.endswith(".clone-lock")}
    assert locks == {"o__x.git.partial-1.git.clone-lock"}


@pytest.mark.asyncio
async def test_an_unreadable_registry_sweeps_nothing(monkeypatch, tmp_path) -> None:
    from server.code.store import index as store

    root = tmp_path / "self"
    monkeypatch.setattr(settings, "code_repos_root", str(root))
    _old(root / "o__r.git")

    class _Down:
        async def registry_rows(self) -> list[dict[str, Any]]:
            raise ConnectionError("ClickHouse down")

    monkeypatch.setattr(store, "CodeRepo", _Down)
    _repos(monkeypatch, [])
    assert await mirrors.sweep() == [] and (root / "o__r.git").is_dir()


def test_the_doctor_names_an_operator_git_dir_that_is_missing(monkeypatch, tmp_path) -> None:
    _repos(
        monkeypatch,
        [_Cfg(str(tmp_path / "typo.git")), _Cfg(str(tmp_path / "s.git"), source="registry")],
    )
    [(name, grade, detail)] = mirrors.doctor_checks()
    assert (name, grade) == ("code_mirrors", "WARN")
    assert str(tmp_path / "typo.git") in detail and "s.git" not in detail


class _Published:
    """The refs the leader's pass left in ClickHouse."""

    def __init__(self, tips: dict[str, str], heads: dict[int, str]) -> None:
        self.tips, self.heads = tips, heads

    async def get_state(self, org: str, ns: str, key: str) -> str:
        return self.tips.get(ns, "") if key == "clone_head" else ""

    async def open_pr_heads(self, org: str, ns: str, **_k: Any) -> dict[int, str]:
        return self.heads if ns == "code-r" else {}

    async def merge_shas_page(self, *_a: Any, **_k: Any) -> list[Any]:
        return []


def _wire_host_fetch(monkeypatch: pytest.MonkeyPatch, published: _Published) -> list[Any]:
    from server.code.store import index as store

    sent: list[Any] = []

    async def _branches(git_dir: str) -> dict[str, str]:
        return {"main": "m1", "26.1": "r0"}

    async def _heads(git_dir: str) -> dict[int, str]:
        return {7: "h7"}

    async def _fetch_many(git_dir: str, remote: str, branches: list[str], **_k: Any) -> None:
        if branches:
            sent.append(("branches", branches))

    async def _fetch_heads(git_dir: str, remote: str, numbers: list[int], **_k: Any) -> int:
        if numbers:
            sent.append(("heads", numbers))
        return len(numbers)

    async def _no_ls_remote(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("a host does not ask the remote what changed")

    monkeypatch.setattr(gitrepo, "local_branch_shas", _branches)
    monkeypatch.setattr(gitrepo, "local_pull_heads", _heads)
    monkeypatch.setattr(gitrepo, "fetch_many", _fetch_many)
    monkeypatch.setattr(gitrepo, "fetch_pull_heads", _fetch_heads)
    monkeypatch.setattr(gitrepo, "remote_branches", _no_ls_remote)
    monkeypatch.setattr(gitrepo, "remote_pull_heads", _no_ls_remote)
    monkeypatch.setattr(store, "CodeRepo", lambda: published)
    monkeypatch.setattr(settings, "code_mirror_fetch_interval_s", 300.0)
    return sent


def _two_entries(tmp_path: Any) -> list[Any]:
    cfgs = [_Cfg(str(tmp_path / "r.git"), "code-r"), _Cfg(str(tmp_path / "r.git"), "code-r-26")]
    cfgs[1].git_branch = "26.1"
    os.makedirs(cfgs[0].git_dir)
    return cfgs


@pytest.mark.asyncio
async def test_a_host_fetches_only_the_refs_the_leader_saw_move(monkeypatch, tmp_path) -> None:
    cfgs = _two_entries(tmp_path)
    published = _Published({"code-r": "m1", "code-r-26": "r1"}, {7: "h7", 9: "h9"})
    sent = _wire_host_fetch(monkeypatch, published)
    await mirrors._fetch_round(cfgs[0].git_dir, cfgs)
    assert sent == [("branches", ["26.1"]), ("heads", [9])]
    await mirrors._fetch_round(cfgs[0].git_dir, cfgs)  # another process, same interval
    assert len(sent) == 2


@pytest.mark.asyncio
async def test_a_failed_pr_head_fetch_still_counts_the_round_and_pins_merges(
    monkeypatch, tmp_path
) -> None:
    cfgs = _two_entries(tmp_path)
    published = _Published({"code-r": "m1", "code-r-26": "r1"}, {9: "h9"})
    sent = _wire_host_fetch(monkeypatch, published)
    pages: list[str] = []

    async def _gone(*_a: Any, **_k: Any) -> int:
        raise gitrepo.GitError("fatal: couldn't find remote ref refs/pull/9/head")

    async def _merge_page(cfg: Any, git_dir: str, repo: Any) -> None:
        pages.append(cfg.namespace)

    monkeypatch.setattr(gitrepo, "fetch_pull_heads", _gone)
    monkeypatch.setattr(mirrors, "_merge_page", _merge_page)
    await mirrors._fetch_round(cfgs[0].git_dir, cfgs)
    assert sent == [("branches", ["26.1"])]
    assert pages == ["code-r", "code-r-26"]
    assert mirrors._mark_age_s(cfgs[0].git_dir, pass_fetch.FETCHED_MARK) < 60


@pytest.mark.asyncio
async def test_a_current_host_makes_no_network_call(monkeypatch, tmp_path) -> None:
    cfgs = _two_entries(tmp_path)
    sent = _wire_host_fetch(monkeypatch, _Published({"code-r": "m1", "code-r-26": "r0"}, {7: "h7"}))
    await mirrors._fetch_round(cfgs[0].git_dir, cfgs)
    assert sent == []


@pytest.mark.asyncio
async def test_the_pass_fetch_holds_the_hosts_fetch_lock_and_counts_as_its_round(
    monkeypatch, tmp_path
) -> None:
    from server.code.app.index.pass_budget import CodeIngestResult

    cfgs = _two_entries(tmp_path)
    git_dir = cfgs[0].git_dir
    sent = _wire_host_fetch(monkeypatch, _Published({"code-r-26": "r1"}, {}))
    held: list[bool] = []

    async def _fetch(*_a: Any, **_k: Any) -> None:
        fd = mirror_locks.try_lock(mirror_locks.fetch_lock_path(git_dir))
        held.append(fd is None)
        if fd is not None:
            os.close(fd)

    async def _nothing(*_a: Any, **_k: Any) -> int:
        return 0

    monkeypatch.setattr(gitrepo, "fetch", _fetch)
    for leg in ("fetch_extra_branches", "fetch_missing_merges", "fetch_pr_heads"):
        monkeypatch.setattr(pass_fetch, leg, _nothing)
    assert await pass_fetch.fetch_entry(cfgs[0], git_dir, object(), CodeIngestResult())
    assert held == [True]  # the host loop's round skips this mirror meanwhile
    await mirrors._fetch_round(git_dir, cfgs)
    assert sent == []  # the pass fetched this interval


@pytest.mark.asyncio
async def test_the_pass_waits_for_a_host_round_on_the_same_mirror(tmp_path) -> None:
    from server.code.app.index.pass_budget import CodeIngestResult

    cfg = _Cfg(str(tmp_path / "r.git"))
    os.makedirs(cfg.git_dir)
    fd = mirror_locks.try_lock(mirror_locks.fetch_lock_path(cfg.git_dir))
    task = asyncio.create_task(
        pass_fetch.fetch_entry(cfg, cfg.git_dir, object(), CodeIngestResult())
    )
    await asyncio.sleep(0.1)
    assert not task.done()
    assert fd is not None
    os.close(fd)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_gc_runs_when_due_and_not_again_until_the_interval(monkeypatch, tmp_path) -> None:
    import subprocess

    git_dir = tmp_path / "r.git"
    subprocess.run(["git", "init", "-q", "--bare", str(git_dir)], check=True)
    monkeypatch.setattr(settings, "code_mirror_gc_margin_gb", 0.0)
    monkeypatch.setattr(settings, "code_mirror_gc_interval_h", 168.0)
    asyncio.run(mirrors.gc_if_due(str(git_dir)))
    first = (git_dir / mirrors._GC_MARK).stat().st_mtime
    asyncio.run(mirrors.gc_if_due(str(git_dir)))
    assert (git_dir / mirrors._GC_MARK).stat().st_mtime == first


def test_gc_without_room_waits_an_interval_instead_of_retrying_each_round(
    monkeypatch, tmp_path
) -> None:
    import subprocess

    git_dir = tmp_path / "r.git"
    subprocess.run(["git", "init", "-q", "--bare", str(git_dir)], check=True)
    monkeypatch.setattr(settings, "code_mirror_gc_margin_gb", 1e9)
    monkeypatch.setattr(settings, "code_mirror_gc_interval_h", 168.0)
    asyncio.run(mirrors.gc_if_due(str(git_dir)))
    assert mirrors._mark_age_s(str(git_dir), mirrors._GC_MARK) < 60


def test_the_default_gc_margin_fits_a_small_disk() -> None:
    from server.settings import Settings

    assert Settings.model_fields["code_mirror_gc_margin_gb"].default <= 5.0


def test_warm_up_off_and_no_fetch_loop_start_nothing(monkeypatch) -> None:
    called: list[str] = []

    async def _warm_up() -> None:
        called.append("warm_up")

    monkeypatch.setattr(mirrors, "warm_up", _warm_up)
    monkeypatch.setattr(settings, "code_mirror_warmup", False)
    monkeypatch.setattr(settings, "code_mirror_fetch_interval_s", 0.0)
    asyncio.run(mirrors._run())  # returns: no warm-up, no loop
    assert called == []


@pytest.mark.asyncio
async def test_a_present_mirror_serves_and_fetches_toward_the_indexed_head(
    monkeypatch, tmp_path
) -> None:
    asked: list[str] = []

    async def _holds(cfg: Any, want: str, **_k: Any) -> bool:
        asked.append(want)
        return False

    monkeypatch.setattr(mirrors, "holds", _holds)
    cfg = _Cfg(str(tmp_path))
    assert await mirrors.history_dir(cfg, head="new") == (cfg.git_dir, "")
    assert await mirrors.history_dir(cfg) == (cfg.git_dir, "")
    assert asked == ["new"]


@pytest.mark.asyncio
async def test_bisect_marks_say_why_there_is_no_mirror_instead_of_running_git(
    monkeypatch, tmp_path
) -> None:
    from server.code.app import bisect, blame
    from server.tracker.domain import bisect as bi

    cfg = _Cfg(str(tmp_path / "r.git"))
    started: list[str] = []

    async def _clone(c: Any) -> str:
        started.append(c.git_dir)
        return "fatal: repository not found"

    async def _no_git(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("no git on a host without the mirror")

    class _Repo:
        async def pr_records_by_number(self, *_a: Any, **_k: Any) -> dict[int, Any]:
            return {}

    monkeypatch.setattr(bootstrap, "clone_and_pin", _clone)
    monkeypatch.setattr(blame, "repo_siblings", lambda org, ns: (cfg, [cfg]))
    monkeypatch.setattr(blame, "diff_kind", _no_git)
    pool = [bi.Candidate(pr_number=1, title="a", author="", merged_at="", minor_version="")]
    _out, stage = await bisect._mark_stage(_Repo(), "o", "code-r", pool, frames=[], paths=["a.c"])  # type: ignore[arg-type]
    assert stage.as_dict()["reason"].startswith("no_git_mirror: o/code-r: clone failed")
    assert stage.status == "skipped" and started == [cfg.git_dir]


@pytest.mark.asyncio
async def test_fix_blame_says_why_there_is_no_mirror_instead_of_a_git_error(
    monkeypatch, tmp_path
) -> None:
    from server.code.app import bisect_fix, blame

    cfg = _Cfg(str(tmp_path / "r.git"))

    async def _clone(c: Any) -> str:
        return "fatal: repository not found"

    class _Repo:
        async def merge_commit_of(self, *_a: Any) -> str:
            return "m" * 40

    monkeypatch.setattr(bootstrap, "clone_and_pin", _clone)
    monkeypatch.setattr(blame, "repo_siblings", lambda org, ns: (cfg, [cfg]))
    _out, summary = await bisect_fix._fix_blame_stage(_Repo(), "o", "code-r", [], 5)  # type: ignore[arg-type]
    assert summary["skipped"].startswith("no_git_mirror: o/code-r: clone failed")
