"""`loom code backfill --workers N`.

The hand-out (`Workers`) is tested against workers on real pipes run by threads:
batches come back in batch order, the header batches are written before a source
batch starts, nothing is handed out after a hold, and a worker that dies names its
batch. The rest runs the real command, `loom code backfill`, in spawned processes on
stubbed stores: a worker runs its batches under the same bindings as the command's own
batch, it stops inside its batch and then retires when the command, its workers or
their whole process group is signalled or killed, and a worker that cannot start or
retire fails the command. They need no ClickHouse and no server, so they are unit
tests; each starts three interpreters and takes a few seconds."""

from __future__ import annotations

import contextvars
import dataclasses
import functools
import json
import logging
import multiprocessing
import os
import signal
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from server.cli import code_backfill_workers as pool

# --- the hand-out, against threads on real pipes -------------------------------------


class _Exited:
    """A worker process that has exited with `code` (its sentinel reads at once)."""

    def __init__(self, code: int) -> None:
        self._r, w = os.pipe()
        os.close(w)
        self.exitcode = code

    @property
    def sentinel(self) -> int:
        return self._r

    def join(self) -> None:
        return None


def _thread_workers(
    n: int, log: list[str], *, slow: str = "", held: str = "", dies: str = ""
) -> pool.Workers:
    """N workers, each a thread on its own pipe: it logs when each batch starts and ends,
    counts a file per path, holds the batch whose first path is `held`, and exits (code
    9) in the batch whose first path is `dies`."""
    lock = threading.Lock()
    workers = []
    for i in range(1, n + 1):
        mine, theirs = multiprocessing.Pipe()

        def _serve(conn: Any = theirs) -> None:
            while True:
                try:
                    _kind, group, paths = conn.recv()
                except EOFError:
                    return
                with lock:
                    log.append(f"start {paths[0]} {group}")
                if paths[0] == dies:
                    conn.close()
                    return
                if paths[0] == slow:
                    time.sleep(0.3)
                with lock:
                    log.append(f"end {paths[0]}")
                conn.send(("done", {"files": len(paths)}, "held" if paths[0] == held else ""))

        threading.Thread(target=_serve, daemon=True).start()
        workers.append(pool._Worker(number=i, process=_Exited(9), conn=mine))
    return pool.Workers(workers)


async def _index(workers: pool.Workers, batches: list[tuple[int, list[str]]]) -> list[Any]:
    return [(b, c["files"], h) async for b, _p, c, h in workers.index_batches(batches)]


def test_batches_come_back_in_batch_order_and_headers_are_written_first() -> None:
    import asyncio

    log: list[str] = []
    workers = _thread_workers(3, log, slow="b.h")
    batches = [(0, ["a.h"]), (1, ["b.h", "c.h"]), (3, ["x.cpp"]), (4, ["y.cpp", "z.cpp"])]
    assert asyncio.run(_index(workers, batches)) == [(0, 1, ""), (1, 2, ""), (3, 1, ""), (4, 2, "")]
    sources = [i for i, e in enumerate(log) if e.startswith(("start x.cpp", "start y.cpp"))]
    assert min(sources) > log.index("end b.h")  # the slow header batch ended first
    # each batch names its group, so a worker knows when to read the headers again
    assert {e.split()[2] for e in log if e.startswith("start")} == {"headers", "sources"}


def test_nothing_is_handed_out_after_a_held_batch_and_what_ran_beside_it_is_counted() -> None:
    import asyncio

    log: list[str] = []
    workers = _thread_workers(2, log, slow="a.py", held="b.py")
    batches = [(i, [f"{c}.py"]) for i, c in enumerate("abcdef")]
    # b is held while a still runs: a comes back first, then b, and nothing else ran
    assert asyncio.run(_index(workers, batches)) == [(0, 1, ""), (1, 1, "held")]
    assert sorted(e for e in log if e.startswith("start")) == [
        "start a.py sources",
        "start b.py sources",
    ]


def test_a_worker_that_exits_in_a_batch_names_the_batch_and_its_exit_code() -> None:
    import asyncio

    workers = _thread_workers(2, [], dies="c.py")
    batches = [(0, ["a.py"]), (200, ["c.py", "d.py"])]
    with pytest.raises(SystemExit, match=r"exited 9 during the index batch at 200 \(2 files\)"):
        asyncio.run(_index(workers, batches))


def test_the_edge_pass_takes_its_batches_in_any_order() -> None:
    import asyncio

    log: list[str] = []
    workers = _thread_workers(2, log, slow="a.py")
    got = asyncio.run(workers.edge_batches([(0, ["a.py"]), (1, ["b.py", "c.py"])]))
    assert sorted(c["files"] for c in got) == [1, 2]
    assert {e.split()[2] for e in log if e.startswith("start")} == {"edges"}


# --- the real command, in spawned processes ------------------------------------------


def _note(path: str, **event: Any) -> None:
    with open(path, "a") as f:
        f.write(json.dumps({"pid": os.getpid(), **event}) + "\n")


def _events(path: Path) -> list[dict[str, Any]]:
    return [json.loads(ln) for ln in path.read_text().splitlines()] if path.exists() else []


def _plain(value: Any) -> Any:
    """A context value as it can be compared across processes."""
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, tuple | list):
        return [_plain(v) for v in value]
    if type(value) is dict:
        return {str(k): _plain(v) for k, v in value.items()}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    return type(value).__qualname__  # a span, a decimal context: per process by nature


def _context_probe(run: Any) -> dict[str, Any]:
    """What a batch runs under: every context variable, and the process-wide state the
    command sets up (logging, the SIGTERM handler, the umask, the wired seams, the
    enrollment and model-work admission)."""
    from server.auth.directory import directory
    from server.core import retirement
    from server.core.model_work_gate import get_model_work_gate

    umask = os.umask(0)
    os.umask(umask)
    try:
        directory()
        wired = True
    except Exception:  # noqa: BLE001 — unwired is what the probe reports
        wired = False
    root = logging.getLogger()
    return {
        "context": {
            v.name: _plain(x) for v, x in contextvars.copy_context().items() if v.name != "_task"
        },
        "log_level": root.level,
        "stderr_handler": any(getattr(h, "stream", None) is sys.stderr for h in root.handlers),
        "sigterm": getattr(signal.getsignal(signal.SIGTERM), "__name__", ""),
        "umask": umask,
        "wired": wired,
        "admitted": (get_model_work_gate().active, retirement.get_gate().active),
        "namespace": run.cfg.namespace,
        "model": run.model,
    }


def _prepare(log: str, opts: dict[str, Any]) -> None:
    """The stores and providers of a spawned command or worker: a client that only
    opens, a registry holding acme/code-x, an active configuration whose model is
    `stored-model`, a checkout of 1000 paths, and a run whose batches note the context
    they run in. The workers this process starts are prepared the same way."""
    import asyncio

    from server.cli import _ch, code_backfill
    from server.code.app.index import bootstrap, repo_registry
    from server.code.store.index import CodeRepo
    from server.core import ch as ch_mod
    from server.core import pg_profile, settings_overrides
    from server.embedding import embedder
    from server.insights.app import provider_command
    from server.settings import settings

    role = "worker" if opts.get("is_worker") else "command"
    settings.code_self_service_enabled = True
    settings.code_repos_root = opts["root"]
    os.makedirs(repo_registry.git_dir_for("acme/x"), exist_ok=True)
    row = {"org": "acme", "namespace": "code-x", "repo": "acme/x", "active": True}

    class _Client:
        connected = False

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

    client = _Client()
    saved = Path(opts["root"]) / "state.json"  # the namespace's state rows, kept across runs
    state: dict[str, str] = json.loads(saved.read_text()) if saved.exists() else {}

    async def _noop(*_a: object, **_k: object) -> None:
        return None

    async def _true(*_a: object, **_k: object) -> bool:
        return True

    async def _rows(self: object) -> list[dict[str, Any]]:
        if opts.get("unread") and role == "worker":
            raise RuntimeError("registry unreachable")
        _note(log, role=role, event="registry read")
        return [row]

    async def _enroll() -> None:
        from server.core import retirement

        _note(log, role=role, event="enrolled")
        retirement.get_gate().enrolled = True

    async def _load() -> None:
        embedder._cached = SimpleNamespace(model_name="stored-model")

    async def _retire(*_a: object) -> bool:
        _note(log, role=role, event="retired")
        return not (opts.get("retire_fails") and role == "worker")

    async def _get_state(self: object, org: str, ns: str, key: str) -> str:
        return state.get(key, "")

    async def _set_state(self: object, org: str, ns: str, key: str, value: str, **_k: Any) -> None:
        state[key] = value
        saved.write_text(json.dumps(state))
        _note(log, role=role, event="state", key=key, value=value)

    async def _close_index(_repo: object, counters: dict[str, int], *_a: object, **k: Any) -> None:
        _note(log, role=role, event="closed", files=counters["files"])
        if k.get("republish", True):
            await _republish(_repo)

    async def _republish(*_a: object) -> None:
        _note(log, role=role, event="republished")

    class _Run:
        def __init__(self, repo: Any, cfg: Any, args: Any, kw: dict[str, Any]) -> None:
            self.repo, self.cfg, self.args, self.org, self.ns = repo, cfg, args, kw["org"], kw["ns"]
            emb = kw["embedder"]
            self.model = self.model_name = emb.model_name if emb else ""
            self.counters = code_backfill.index_counters()
            self.edges = code_backfill.edge_counters()

        async def batch(self, paths: list[str]) -> str:
            _note(log, role=role, event="batch", first=paths[0], probe=_context_probe(self))
            if opts.get("live"):
                await _write_and_resolve(self, paths)
            if opts.get("block"):
                try:
                    await asyncio.sleep(120)
                finally:
                    _note(log, role=role, event="cancelled")
                    # what a round writes on its way out (`RoundReads.__aexit__`)
                    await asyncio.sleep(1.0)
                    _note(log, role=role, event="flushed")
            if paths[0] == opts.get("slow"):
                await asyncio.sleep(1.0)
            if paths[0] == opts.get("held"):
                return "acme's model calls are switched off"
            self.counters["files"] += len(paths)
            return ""

        async def edge_batch(self, paths: list[str]) -> None:
            live = await _resolve(self, paths[0]) if opts.get("live") else None
            _note(log, role=role, event="edges", first=paths[0], live=live)
            self.edges["files"] += len(paths)
            if paths[0] == opts.get("edge_fails"):
                self.edges["errors"] += 1  # a file's edge write failed in this batch

    written = Path(opts["root"]) / "written.txt"

    async def _live_path_set(self: object, org: str, ns: str) -> list[str]:
        return written.read_text().split() if written.exists() else []

    async def _resolve(run: Any, path: str) -> int:
        # the import resolver's own oracle, as a file's ingest reads it
        from server.code.app.index.ingest_resolve import _live_paths

        live = await _live_paths(run.repo, "acme", "code-x", SimpleNamespace(path=path))
        return len(live or ())

    async def _write_and_resolve(run: Any, paths: list[str]) -> None:
        await _resolve(run, paths[0])  # this batch's files resolve against the set as read
        _append(paths)  # and then land in the index
        await asyncio.sleep(0.3)

    def _append(paths: list[str]) -> None:
        with written.open("a") as f:
            f.write("\n".join(paths) + "\n")

    async def _open(cls: object, repo: object, cfg: Any, args: Any, head: str, **kw: Any) -> Any:
        return _Run(repo, cfg, args, kw)

    async def _head(*_a: object) -> str:
        return "f" * 40

    async def _paths(*_a: object) -> list[str]:
        return [f"f{i}.py" for i in range(opts.get("files", 1000))]

    ch_mod.get_client = lambda: client
    pg_profile.check_configured = _noop
    _ch.check_acceptance = _noop
    settings_overrides.refresh = _noop
    CodeRepo.registry_rows = _rows
    CodeRepo.get_state = _get_state
    CodeRepo.set_state = _set_state
    CodeRepo.live_path_set = _live_path_set
    provider_command.register_process = _enroll
    provider_command.load_active_configuration = _load
    provider_command.drain_task_sets = _true
    provider_command.flush_registered_buffers = _true
    provider_command._retire_until = _retire
    code_backfill.connect_code_plane = _noop
    code_backfill._backfill_head = _head
    code_backfill._backfill_paths = _paths
    code_backfill._close_index = _close_index
    code_backfill._republish_counts = _republish
    bootstrap.record_backfill_stamps = _noop
    code_backfill._Run.open = classmethod(_open)  # type: ignore[method-assign,assignment]
    if not opts.get("is_worker"):
        # The workers this command starts run `_worker`, which stubs them the same way.
        worker = functools.partial(_worker, log, {**opts, "is_worker": True})
        pool._worker_main = worker  # type: ignore[assignment]


def _worker(log: str, opts: dict[str, Any], command: Any) -> None:
    """A worker process on the stubbed stores, entered as the pool enters one."""
    _prepare(log, opts)
    pool._worker_main(command)


def _command(argv: list[str], log: str, opts: dict[str, Any]) -> None:
    """`loom <argv>` in this process, on the stubbed stores; in its own process group
    when `group` is set, so a test can signal the command and its workers together."""
    if opts.get("group"):
        os.setpgid(0, 0)
    _prepare(log, opts)
    from server.cli.app import main

    sys.exit(main(argv))


def _spawn(tmp_path: Path, workers: int, **opts: Any) -> tuple[Any, Path]:
    log = tmp_path / f"events-{workers}.jsonl"
    argv = ["code", "backfill", "--org", "acme", "--namespace", "code-x", "--embed"]
    argv += ["--no-fetch", "--workers", str(workers)]
    proc = multiprocessing.get_context("spawn").Process(
        target=_command, args=(argv, str(log), {"root": str(tmp_path / "repos"), **opts})
    )
    proc.start()
    return proc, log


def _wait_for(check: Any, timeout: float = 60.0) -> Any:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        got = check()
        if got:
            return got
        time.sleep(0.1)
    raise AssertionError("timed out")


def _gone(pid: int) -> bool:
    """Whether `pid` has exited (a zombie waiting for its reaper has)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    stat = Path(f"/proc/{pid}/stat")
    if not stat.exists():
        return False  # alive, and no /proc to tell a zombie: not gone
    return stat.read_text().rsplit(")", 1)[1].split()[0] == "Z"


def test_a_worker_runs_its_batches_under_the_bindings_the_command_s_own_batch_runs_under(
    tmp_path: Path,
) -> None:
    """The same command with one process and with two: every context variable and the
    process-wide setup a batch sees is the same in a worker, so a binding added to the
    command reaches the workers without a list to keep."""
    one, one_log = _spawn(tmp_path, 1, files=10)
    two, two_log = _spawn(tmp_path, 2, files=1000)
    one.join(120)
    two.join(120)
    assert (one.exitcode, two.exitcode) == (0, 0)
    [command] = [e["probe"] for e in _events(one_log) if e["event"] == "batch"]
    in_workers = [e for e in _events(two_log) if e["event"] == "batch"]
    assert {e["role"] for e in in_workers} == {"worker"} and len(in_workers) == 5
    assert len({e["pid"] for e in in_workers}) == 2
    for e in in_workers:
        assert e["probe"] == command
    assert command["context"]["loom_traffic_class"] == ["background", "cli:code"]
    assert (command["namespace"], command["model"], command["wired"]) == (
        "code-x",
        "stored-model",
        True,
    )
    # each worker read the registry once enrolled, and retired when the run closed
    per_worker: dict[int, list[str]] = {}
    for e in _events(two_log):
        if e["role"] == "worker" and e["event"] in ("enrolled", "registry read", "retired"):
            per_worker.setdefault(e["pid"], []).append(e["event"])
    assert list(per_worker.values()) == [["enrolled", "registry read", "retired"]] * 2


def test_a_held_batch_on_real_workers_starts_nothing_more_and_what_ran_beside_it_is_counted(
    tmp_path: Path,
) -> None:
    proc, log = _spawn(tmp_path, 2, slow="f0.py", held="f200.py")
    proc.join(120)
    assert proc.exitcode == 1
    events = _events(log)
    assert sorted(e["first"] for e in events if e["event"] == "batch") == ["f0.py", "f200.py"]
    cursor = [e["value"] for e in events if e.get("key") == "backfill_cursor:index"]
    assert cursor == ["200"]
    assert [e["files"] for e in events if e["event"] == "closed"] == [200]
    # the held run republishes once, after its last state write
    written = [e["event"] for e in events if e["event"] in ("state", "republished")]
    assert written[-1] == "republished" and written.count("republished") == 1
    # the edge pass the run owes stays recorded for the next run
    owed = [e["value"] for e in events if e.get("key") == "backfill_edges_owed:index"]
    assert owed == ["workers"]


def test_an_edge_batch_that_fails_a_write_leaves_the_pass_owed_for_a_resume_without_workers(
    tmp_path: Path,
) -> None:
    """A worker's edge batch returns with a failed file write. The run still ends (the
    cursor clears, `git_head` is written), but the pass stays owed, so a resume with one
    process runs it again and only a clean pass clears the record."""
    first, first_log = _spawn(tmp_path, 2, files=400, edge_fails="f200.py")
    first.join(120)
    assert first.exitcode == 0  # one failed file in 400 does not fail the run
    owed = [e["value"] for e in _events(first_log) if e.get("key") == "backfill_edges_owed:index"]
    assert owed == ["workers"]  # recorded, and never cleared
    resume, resume_log = _spawn(tmp_path, 1, files=400)
    resume.join(120)
    assert resume.exitcode == 0
    events = _events(resume_log)
    assert [e["first"] for e in events if e["event"] == "edges"] == ["f0.py", "f200.py"]
    owed = [e["value"] for e in events if e.get("key") == "backfill_edges_owed:index"]
    assert owed == [""]  # cleared once the pass ran without errors


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGKILL])
def test_a_terminated_or_killed_command_leaves_no_worker_running(
    tmp_path: Path, sig: signal.Signals
) -> None:
    """Both workers are in a two-minute batch. SIGTERM: the command stops them, they
    retire, and it exits 143 at once. SIGKILL: the command cannot, and its closed pipes
    stop them just the same."""
    proc, log = _spawn(tmp_path, 2, block=True)

    def _in_batches() -> list[int]:
        pids = [e["pid"] for e in _events(log) if e["event"] == "batch"]
        return pids if len(pids) == 2 else []

    pids = _wait_for(_in_batches)
    started = time.monotonic()
    os.kill(proc.pid, sig)
    proc.join(60)
    assert proc.exitcode == (143 if sig == signal.SIGTERM else -signal.SIGKILL)
    _wait_for(lambda: all(_gone(p) for p in pids))
    assert time.monotonic() - started < 60
    retired = {e["pid"] for e in _events(log) if e["event"] == "retired"}
    assert set(pids) <= retired


def test_a_worker_whose_retirement_fails_fails_the_command(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """The command still writes its durable end (the owed pass cleared, `git_head`,
    the counts) before it reports the workers that did not retire, as one process
    retires after its command has returned: a rerun repeats nothing."""
    proc, log = _spawn(tmp_path, 2, files=400, retire_fails=True)
    proc.join(120)
    assert proc.exitcode == 1
    assert "backfill workers did not retire cleanly (worker 1 exited 1" in capfd.readouterr().err
    events = [e for e in _events(log) if e["role"] == "command"]
    state = {e["key"]: e["value"] for e in events if e["event"] == "state"}
    assert state["git_head"] == "f" * 40 and state["backfill_edges_owed:index"] == ""
    kinds = [e["event"] for e in events]
    watermark = next(i for i, e in enumerate(events) if e.get("key") == "git_head")
    assert "republished" in kinds[watermark:]  # the counts carry the new watermark


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_a_signal_to_the_whole_group_stops_each_batch_before_its_worker_retires(
    tmp_path: Path, sig: signal.Signals
) -> None:
    """A terminal's Ctrl-C, `timeout` and a systemd stop signal every process of the
    group: each worker's own handler cancels its command, and the batch, awaited in
    that command's task, unwinds before the worker retires."""
    proc, log = _spawn(tmp_path, 2, block=True, group=True)

    def _in_batches() -> list[int]:
        pids = [e["pid"] for e in _events(log) if e["event"] == "batch"]
        return pids if len(pids) == 2 else []

    pids = _wait_for(_in_batches)
    os.killpg(proc.pid, sig)
    proc.join(60)
    assert proc.exitcode != 0
    _wait_for(lambda: all(_gone(p) for p in pids))
    for pid in pids:
        mine = [e["event"] for e in _events(log) if e["pid"] == pid]
        # cancelled once: the batch's own cleanup completes before the worker retires
        assert mine.index("cancelled") < mine.index("flushed") < mine.index("retired")


def test_the_edge_pass_in_a_worker_resolves_imports_to_files_another_worker_wrote(
    tmp_path: Path,
) -> None:
    """The import resolver caches the namespace's live paths per process. Two workers
    write their batches; in the edge pass each one resolves against every file written,
    its own and the other's, as one process would."""
    proc, log = _spawn(tmp_path, 2, live=True)
    proc.join(120)
    assert proc.exitcode == 0
    edges = [e for e in _events(log) if e["event"] == "edges"]
    assert {e["role"] for e in edges} == {"worker"} and len({e["pid"] for e in edges}) == 2
    assert {e["live"] for e in edges} == {1000}


def test_a_worker_that_cannot_open_its_run_fails_the_command_with_its_reason(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    proc, log = _spawn(tmp_path, 2, unread=True)
    proc.join(120)
    assert proc.exitcode == 1
    err = capfd.readouterr().err
    assert "exited 1 before its run opened; its error is above" in err
    assert "BrokenPipeError" not in err  # the other worker leaves quietly
    assert "repository registry" in err.lower()  # the worker's own one-line reason
    assert not [e for e in _events(log) if e["event"] == "batch"]


def test_a_run_starts_no_more_workers_than_either_pass_has_batches() -> None:
    import argparse
    import contextlib

    from server.cli.code_backfill import _workers_for

    args = argparse.Namespace(workers=4)
    assert isinstance(_workers_for(args, "h", 1, 1), contextlib.nullcontext)
    assert isinstance(_workers_for(args, "h", 0, 0), contextlib.nullcontext)


def test_a_worker_killed_by_a_signal_is_named_so_not_sent_to_an_error_that_is_not_there() -> None:
    assert pool._ended(-9) + pool._above(-9) == "was killed by SIGKILL (out of memory?)"
    assert pool._ended(1) + pool._above(1) == "exited 1; its error is above"
