"""Per-entry compiler-tier locks: a full pass and a delta never work on one
entry at once, other entries go on, and shell and Python lock the same file."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from server.code.app.index import compiler_tier
from server.code.app.index.entry_lock import EntryLockHeld, entry_lock, entry_lock_path
from tests.unit.claims_fake import FakeLockTable, HeldLease, held_leases
from tests.unit.code_fixtures import repo_config


@pytest.fixture(autouse=True)
def _lock_dir(monkeypatch, tmp_path, request):
    from server.settings import settings

    monkeypatch.setattr(settings, "code_scip_lock_dir", str(tmp_path / "locks"))
    if not {"keeper", "no_clickhouse"} & set(request.fixturenames):
        # The deployment-wide lease is tested below against a fake lock table.
        held_leases(monkeypatch)


def _Cfg(namespace: str) -> Any:
    return repo_config(org="acme", namespace=namespace, scip_producer="clang")


def _hold(path: Path) -> int:
    """Take the lock through a separate open file, as another process would."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def test_lock_path_is_one_file_per_entry(tmp_path) -> None:
    p = entry_lock_path("acme", "code-web")
    assert p == tmp_path / "locks" / "scip.acme.code-web.lock"
    # Dots and slashes are encoded, so no two entries share a file.
    assert entry_lock_path("a.b", "c") != entry_lock_path("a", "b.c")
    assert "/" not in entry_lock_path("acme", "x/y").name


@pytest.mark.asyncio
async def test_non_blocking_raises_when_held_and_releases_on_exit() -> None:
    fd = _hold(entry_lock_path("acme", "code-web"))
    try:
        with pytest.raises(EntryLockHeld):
            async with entry_lock("acme", "code-web", wait=False):
                pass
        # A different entry is free.
        async with entry_lock("acme", "code-api", wait=False):
            pass
    finally:
        os.close(fd)
    async with entry_lock("acme", "code-web", wait=False) as path:
        assert path.exists()
    async with entry_lock("acme", "code-web", wait=False):
        pass  # released by the previous block


@pytest.mark.asyncio
async def test_run_pass_skips_the_held_entry_and_runs_the_others() -> None:
    from server.code.workers import scip_delta as w

    fd = _hold(entry_lock_path("acme", "code-busy"))
    run_repo = AsyncMock()
    try:
        with (
            patch("server.code.store.index.CodeRepo", return_value=AsyncMock()),
            patch(
                "server.code.domain.index.repos.configured_repos",
                return_value=[_Cfg("code-busy"), _Cfg("code-free")],
            ),
            patch.object(compiler_tier, "worker_owns", AsyncMock(return_value=True)),
            patch.object(w, "_run_repo", run_repo),
        ):
            res = await w.run_pass()
    finally:
        os.close(fd)
    assert res.repos_checked == 2
    assert res.skipped == ["code-busy: entry lock held (full pass running)"]
    assert [c.args[1].namespace for c in run_repo.await_args_list] == ["code-free"]
    assert not res.errors


@pytest.mark.asyncio
async def test_run_pass_holds_the_entry_lock_while_it_works() -> None:
    from server.code.workers import scip_delta as w

    seen: list[bool] = []

    async def run_repo(repo, cfg, result):
        try:
            async with entry_lock(cfg.org, cfg.namespace, wait=False):
                seen.append(False)
        except EntryLockHeld:
            seen.append(True)

    with (
        patch("server.code.store.index.CodeRepo", return_value=AsyncMock()),
        patch(
            "server.code.domain.index.repos.configured_repos",
            return_value=[_Cfg("code-web")],
        ),
        patch.object(compiler_tier, "worker_owns", AsyncMock(return_value=True)),
        patch.object(w, "_run_repo", run_repo),
    ):
        await w.run_pass()
    assert seen == [True]


@pytest.mark.asyncio
async def test_scip_full_waits_for_a_delta_on_its_entry() -> None:
    from server.cli import code_scip

    fd = _hold(entry_lock_path("acme", "code-web"))
    locked = AsyncMock(return_value=0)
    args = argparse.Namespace(org="acme", namespace="code-web")
    with (
        patch("server.core.ch.get_client", return_value=AsyncMock()),
        patch.object(code_scip, "_repo_cfg", return_value=_Cfg("code-web")),
        patch.object(code_scip, "_scip_full_locked", locked),
    ):
        task = asyncio.create_task(code_scip._scip_full(args))
        await asyncio.sleep(0.3)
        assert not locked.await_count, "the full pass ran while a delta held its entry"
        os.close(fd)
        assert await asyncio.wait_for(task, timeout=10) == 0
    assert locked.await_count == 1


@pytest.mark.asyncio
async def test_a_cancelled_wait_for_the_host_lock_leaves_it_free(monkeypatch) -> None:
    from server.code.app.index import entry_lock as el

    monkeypatch.setattr(el, "_POLL_S", 0.05)
    fd = _hold(entry_lock_path("acme", "code-web"))
    waiter = asyncio.create_task(entry_lock("acme", "code-web", wait=True).__aenter__())
    await asyncio.sleep(0.2)
    waiter.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await waiter
    os.close(fd)
    await asyncio.sleep(0.2)  # a thread still blocked in flock would take it now
    async with entry_lock("acme", "code-web", wait=False):
        pass


@pytest.mark.asyncio
async def test_scip_full_holds_the_lock_for_the_whole_entry() -> None:
    from server.cli import code_scip

    async def locked(args, org, ns, cfg):
        with pytest.raises(EntryLockHeld):
            async with entry_lock(org, ns, wait=False):
                pass
        return 0

    args = argparse.Namespace(org="acme", namespace="code-web")
    with (
        patch("server.core.ch.get_client", return_value=AsyncMock()),
        patch.object(code_scip, "_repo_cfg", return_value=_Cfg("code-web")),
        patch.object(code_scip, "_scip_full_locked", locked),
    ):
        assert await code_scip._scip_full(args) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("producer", "extra"),
    [("go", ()), ("typescript", ()), ("python", ("typescript",))],
)
async def test_scip_full_skips_an_entry_another_tier_owns(producer, extra, capsys) -> None:

    from server.cli import code_scip

    cfg = repo_config(
        org="acme", namespace="code-web", scip_producer=producer, scip_extra_producers=extra
    )
    locked = AsyncMock(return_value=0)
    args = argparse.Namespace(org="acme", namespace="code-web")
    with (
        patch("server.core.ch.get_client", return_value=AsyncMock()),
        patch.object(code_scip, "_repo_cfg", return_value=cfg),
        patch.object(code_scip, "_scip_full_locked", locked),
    ):
        assert await code_scip._scip_full(args) == 0
    assert not locked.await_count, "scip-clang ran on an entry another compiler tier owns"
    assert f"its compiler tier is {producer}" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_scip_full_runs_a_clang_entry() -> None:

    from server.cli import code_scip

    cfg = repo_config(org="acme", namespace="code-web", scip_producer="clang")
    locked = AsyncMock(return_value=0)
    args = argparse.Namespace(org="acme", namespace="code-web")
    with (
        patch("server.core.ch.get_client", return_value=AsyncMock()),
        patch.object(code_scip, "_repo_cfg", return_value=cfg),
        patch.object(code_scip, "_scip_full_locked", locked),
    ):
        assert await code_scip._scip_full(args) == 0
    assert locked.await_count == 1


def test_cli_prints_the_path_the_workers_lock(capsys) -> None:
    from server.cli import code

    args = argparse.Namespace(org="acme", namespace="code-web")
    assert code._print_scip_lock_path(args) == 0
    assert capsys.readouterr().out.strip() == str(entry_lock_path("acme", "code-web").resolve())


@pytest.mark.skipif(shutil.which("flock") is None, reason="flock(1) not installed")
def test_shell_flock_and_python_exclude_each_other() -> None:
    import time

    from server.code.app.index.entry_lock import _acquire

    path = entry_lock_path("acme", "code-web")
    fd = asyncio.run(_acquire(path, wait=False))
    assert fd is not None
    try:
        held = subprocess.run(["flock", "-n", str(path), "true"], check=False)
        assert held.returncode == 1
    finally:
        os.close(fd)
    free = subprocess.run(["flock", "-n", str(path), "true"], check=False)
    assert free.returncode == 0
    # Waits for the lock: a probe below may hold it for an instant as it starts.
    proc = subprocess.Popen(["flock", "-w", "10", str(path), "sleep", "30"])
    try:
        # wait until the shell side holds it: a fixed sleep raced a slow spawn
        deadline = time.monotonic() + 10.0
        while subprocess.run(["flock", "-n", str(path), "true"], check=False).returncode == 0:
            assert proc.poll() is None, "flock exited before taking the lock"
            assert time.monotonic() < deadline, "the shell never took the lock"
            time.sleep(0.01)
        assert asyncio.run(_acquire(path, wait=False)) is None
    finally:
        proc.kill()
        proc.wait()


# -- the deployment-wide lease ---------------------------------------------------


@pytest.fixture
def no_clickhouse(monkeypatch):
    ch = FakeLockTable()
    ch.down = True
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)


@pytest.fixture
def keeper(monkeypatch):
    ch = FakeLockTable()
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    return ch


_NAME = "scip_entry:acme/code-web"


@pytest.mark.asyncio
async def test_another_hosts_pass_holds_the_entry_through_clickhouse(keeper) -> None:
    """The lock file is per host; two hosts exclude each other through the
    entry's key in the Keeper lock table."""
    keeper.seed(_NAME, "other-host:7:zz", ttl=120)
    with pytest.raises(EntryLockHeld):
        async with entry_lock("acme", "code-web", wait=False):
            pass
    assert keeper.holder(_NAME) == "other-host:7:zz"
    # The host's own file lock was released on the way out.
    os.close(_hold(entry_lock_path("acme", "code-web")))


@pytest.mark.asyncio
async def test_the_entry_key_is_held_for_the_block_and_deleted_after(keeper) -> None:
    async with entry_lock("acme", "code-web", wait=False):
        assert keeper.keys[_NAME]["ttl"] == 120
    assert keeper.keys == {}


@pytest.mark.asyncio
async def test_no_clickhouse_fails_closed(no_clickhouse) -> None:
    """The host's file excludes nothing on another host: no lease, no pass."""
    from server.code.app.index.entry_lock import EntryLockUnavailable

    with pytest.raises(EntryLockUnavailable):
        async with entry_lock("acme", "code-web", wait=False):
            pytest.fail("ran without the deployment lease")
    assert not issubclass(EntryLockUnavailable, EntryLockHeld)
    # The host's file lock was released on the way out.
    os.close(_hold(entry_lock_path("acme", "code-web")))


@pytest.mark.asyncio
async def test_a_refused_lock_statement_fails_closed(keeper, monkeypatch) -> None:
    """A refusal (not a Keeper error) used to run the pass on the host lock alone."""
    from server.code.app.index.entry_lock import EntryLockUnavailable

    class Refusing(FakeLockTable):
        async def execute(self, sql, params=None, **kw):
            raise RuntimeError("Code: 497. DB::Exception: Not enough privileges. (ACCESS_DENIED)")

    ch = Refusing()
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    with pytest.raises(EntryLockUnavailable, match="ACCESS_DENIED"):
        async with entry_lock("acme", "code-web", wait=False):
            pytest.fail("ran without the deployment lease")


@pytest.mark.asyncio
async def test_a_cancelled_wait_for_the_entry_takes_no_key(keeper) -> None:
    keeper.seed(_NAME, "other-host:7:zz", ttl=120)

    async def full_pass() -> None:
        async with entry_lock("acme", "code-web", wait=True):
            pytest.fail("ran while another host held the entry")

    task = asyncio.create_task(full_pass())
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    keeper.keys.clear()  # the other host finishes
    await asyncio.sleep(2.5)  # past the next poll a stray waiter would have made
    assert keeper.keys == {}


@pytest.mark.asyncio
async def test_a_lost_entry_lease_stops_the_pass(keeper, monkeypatch) -> None:
    from server.code.app.index import entry_lock as el

    monkeypatch.setattr(el, "ENTRY_LEASE_TTL_S", 0.6)
    ran_on = False
    with pytest.raises(el.EntryLockLost):
        async with entry_lock("acme", "code-web", wait=False):
            keeper.keys[_NAME]["holder"] = "other-host:7:zz"  # taken over meanwhile
            await asyncio.sleep(3)
            ran_on = True
    assert not ran_on
    with pytest.raises(el.EntryLockLost):
        lease = HeldLease()
        lease.lost = True
        token = el._current.set(lease)  # type: ignore[arg-type]
        try:
            el.check_held()
        finally:
            el._current.reset(token)


@pytest.mark.asyncio
async def test_scip_full_reports_a_lock_it_could_not_take_after_waiting(capsys) -> None:
    from server.cli import code_scip

    fd = _hold(entry_lock_path("acme", "code-web"))
    calls: list[bool] = []

    @contextlib.asynccontextmanager
    async def _never(org, ns, *, wait):
        calls.append(wait)
        raise EntryLockHeld(org, ns, "scip_entry:acme/code-web")
        yield  # pragma: no cover

    args = argparse.Namespace(org="acme", namespace="code-web")
    try:
        with (
            patch("server.core.ch.get_client", return_value=AsyncMock()),
            patch.object(code_scip, "_repo_cfg", return_value=_Cfg("code-web")),
            patch("server.code.app.index.entry_lock.entry_lock", _never),
        ):
            assert await code_scip._scip_full(args) == 1
    finally:
        os.close(fd)
    assert calls == [False, True]
    assert "entry lock:" in capsys.readouterr().err


@pytest.mark.parametrize("held", [False, True])
def test_entry_lock_wrapper_runs_the_command_or_reports_the_entry_held(held, tmp_path) -> None:
    import sys

    from server.cli import code_scip

    marker = tmp_path / "ran"
    args = argparse.Namespace(
        org="acme",
        namespace="code-web",
        tier="",
        command=["--", sys.executable, "-c", f"open({str(marker)!r}, 'w').write('x')"],
    )
    fd = _hold(entry_lock_path("acme", "code-web")) if held else None
    try:
        rc = asyncio.run(code_scip._entry_locked(args))
    finally:
        if fd is not None:
            os.close(fd)
    if held:
        assert rc == code_scip.ENTRY_HELD_EXIT and not marker.exists()
    else:
        assert rc == 0 and marker.exists()


def test_entry_lock_wrapper_runs_nothing_without_the_lease(no_clickhouse, tmp_path) -> None:
    import sys

    from server.cli import code_scip

    marker = tmp_path / "ran"
    args = argparse.Namespace(
        org="acme",
        namespace="code-web",
        tier="",
        command=[sys.executable, "-c", f"open({str(marker)!r}, 'w').write('x')"],
    )
    assert asyncio.run(code_scip._entry_locked(args)) == 1
    assert not marker.exists()


def test_entry_lock_wrapper_kills_the_command_group_when_the_lease_is_lost(
    keeper, monkeypatch, tmp_path
) -> None:
    """The command's own children die with it: a harness's scip-python and its
    uploader must write nothing after the wrapper reports the loss."""
    import threading
    import time

    from server.cli import code_scip
    from server.code.app.index import entry_lock as el

    monkeypatch.setattr(el, "ENTRY_LEASE_TTL_S", 0.6)
    monkeypatch.setattr(code_scip, "_ENTRY_STOP_GRACE_S", 60.0)
    pidfile = tmp_path / "child.pid"
    # A grandchild that ignores SIGTERM: killed at once, no grace to write in.
    script = f"trap '' TERM; sleep 60 & echo $! > {pidfile}; wait"
    args = argparse.Namespace(
        org="acme", namespace="code-web", tier="", command=["sh", "-c", script]
    )

    def take_over() -> None:
        while _NAME not in keeper.keys:
            time.sleep(0.01)
        keeper.keys[_NAME]["holder"] = "other-host:7:zz"

    threading.Thread(target=take_over, daemon=True).start()
    t0 = time.monotonic()
    assert asyncio.run(code_scip._entry_locked(args)) == 3
    assert time.monotonic() - t0 < 10
    grandchild = int(pidfile.read_text())
    for _ in range(50):
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("the command's child outlived the lost lease")


# -- a lost lease publishes nothing ------------------------------------------------


@contextlib.contextmanager
def _inside_lease(lease: HeldLease):
    from server.code.app.index import entry_lock as el

    token = el._current.set(lease)  # type: ignore[arg-type]
    try:
        yield
    finally:
        el._current.reset(token)


class _IndexRepo:
    """The rows and watermarks a pass would publish; `on_read` runs at the
    pass's first read, after the loop's own checks."""

    supports_round_reads = True

    def __init__(self, on_read=lambda: None) -> None:
        self.on_read = on_read
        self.inserted: list[str] = []
        self.states: list[str] = []

    async def live_symbols_for_paths(self, *a, **k):
        self.on_read()
        return {}

    async def live_edges_for_paths(self, *a, **k):
        return {}

    async def chunk_spans_for_paths(self, *a, **k):
        return {}

    async def insert_rows(self, table, rows) -> None:
        self.inserted.append(table)

    async def set_state(self, org, ns, key, value, detail="") -> None:
        self.states.append(key)


def _delta() -> Any:
    from server.code.app.index.scip_delta import ScipDelta

    return ScipDelta.model_validate(
        {
            "commit": "c" * 40,
            "files": [
                {
                    "path": "src/a.cpp",
                    "symbols": [{"qualified_name": "f", "line_start": 1, "line_end": 2}],
                }
            ],
            "features": ["ctor_calls"],
        }
    )


@pytest.mark.asyncio
async def test_apply_delta_publishes_rows_and_watermarks_while_held(monkeypatch) -> None:
    from server.code.app.index import scip_delta

    monkeypatch.setattr(scip_delta, "_indexable_prefixes", lambda *a: ())
    monkeypatch.setattr(scip_delta, "_path_refusal", lambda *a: lambda p: False)
    repo = _IndexRepo()
    with _inside_lease(HeldLease()):
        await scip_delta.apply_delta(repo, "acme", "code-web", _delta())  # type: ignore[arg-type]
    assert repo.inserted and repo.states


@pytest.mark.asyncio
async def test_a_lease_lost_mid_round_publishes_no_row_or_watermark(monkeypatch) -> None:
    """The loss lands after the round's checks and reads; the insert still checks."""
    from server.code.app.index import scip_delta
    from server.code.app.index.entry_lock import EntryLockLost

    monkeypatch.setattr(scip_delta, "_indexable_prefixes", lambda *a: ())
    monkeypatch.setattr(scip_delta, "_path_refusal", lambda *a: lambda p: False)
    lease = HeldLease()
    repo = _IndexRepo(on_read=lambda: setattr(lease, "lost", True))
    with _inside_lease(lease), pytest.raises(EntryLockLost):
        await scip_delta.apply_delta(repo, "acme", "code-web", _delta())  # type: ignore[arg-type]
    assert repo.inserted == [] and repo.states == []


@pytest.mark.asyncio
async def test_the_base_unknown_watermark_reset_runs_under_the_entry_lease() -> None:
    """The reset used to run after the block, where no lease was checked."""
    from server.code.workers import scip_delta as w

    lease = HeldLease()
    repo = _IndexRepo()

    async def run_repo(repo, cfg, result):
        lease.lost = True  # lost while the harness ran
        raise compiler_tier.BaseUnknown("base gone")

    async def take(name, **_):
        return lease

    with (
        patch("server.core.leases.take", take),
        patch("server.code.store.index.CodeRepo", return_value=repo),
        patch(
            "server.code.domain.index.repos.configured_repos",
            return_value=[_Cfg("code-web")],
        ),
        patch.object(compiler_tier, "worker_owns", AsyncMock(return_value=True)),
        patch.object(w, "_run_repo", run_repo),
    ):
        res = await w.run_pass()
    assert repo.states == []
    assert res.errors and "EntryLockLost" in res.errors[0]


def test_entry_lock_wrapper_hands_its_command_the_lease_holder(keeper, tmp_path) -> None:
    """The command's uploads name the holder, so the server can fence them."""
    import sys

    from server.cli import code_scip
    from server.core import leases

    out = tmp_path / "holder"
    script = f"import os; open({str(out)!r}, 'w').write(os.environ['LOOM_ENTRY_LEASE_HOLDER'])"
    seen: list[bool] = []

    async def run() -> int:
        real = code_scip._run_while_held

        async def spy(command, **kw):
            rc = await real(command, **kw)
            seen.append(await leases.held_by(_NAME, out.read_text()))
            return rc

        with patch.object(code_scip, "_run_while_held", spy):
            args = argparse.Namespace(
                org="acme", namespace="code-web", tier="", command=[sys.executable, "-c", script]
            )
            return await code_scip._entry_locked(args)

    assert asyncio.run(run()) == 0
    assert seen == [True]  # the holder the command got is the one the store names
    assert not asyncio.run(leases.held_by(_NAME, out.read_text()))  # released after
