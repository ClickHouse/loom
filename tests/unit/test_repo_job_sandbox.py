"""The repository-job sandbox's pieces that need no bubblewrap: the seccomp
program, the mounts a job gets, and the fallback when the host has no sandbox."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from server.code.app.index import sandbox

EPERM = sandbox.SECCOMP_RET_ERRNO_EPERM
KILL = sandbox.SECCOMP_RET_KILL_PROCESS
ALLOW = sandbox.SECCOMP_RET_ALLOW


def _run_bpf(program: bytes, arch: int, nr: int, arg0: int = 0) -> int:
    """Evaluate the classic-BPF seccomp program for one (arch, syscall nr, first
    argument): the five instructions it uses — load word, jeq, jge, jset, ret."""
    ins = [struct.unpack("=HBBI", program[i : i + 8]) for i in range(0, len(program), 8)]
    data = {0: nr, 4: arch, 16: arg0}
    acc, pc = 0, 0
    while True:
        code, jt, jf, k = ins[pc]
        if code == 0x20:
            acc = data[k]
        elif code in (0x15, 0x35, 0x45):
            hit = {0x15: acc == k, 0x35: acc >= k, 0x45: bool(acc & k)}[code]
            pc += jt if hit else jf
        elif code == 0x06:
            return k
        else:
            raise AssertionError(f"unexpected BPF opcode {code:#x}")
        pc += 1


@pytest.mark.parametrize(("machine", "column"), [("x86_64", 0), ("aarch64", 1)])
def test_seccomp_refuses_each_denied_syscall_and_allows_the_rest(machine: str, column: int) -> None:
    program = sandbox.seccomp_filter(machine)
    arch = sandbox._AUDIT_ARCH[machine]
    for name, numbers in sandbox._DENIED_SYSCALLS.items():
        assert _run_bpf(program, arch, numbers[column]) == EPERM, name
    # read, write, openat, clone, execve stay allowed
    allowed = {"x86_64": (0, 1, 257, 56, 59), "aarch64": (63, 64, 56, 220, 221)}[machine]
    for nr in allowed:
        assert _run_bpf(program, arch, nr) == ALLOW


@pytest.mark.parametrize(
    ("machine", "clone", "clone3"), [("x86_64", 56, 435), ("aarch64", 220, 435)]
)
def test_seccomp_refuses_a_clone_into_a_new_user_namespace(
    machine: str, clone: int, clone3: int
) -> None:
    """unshare is denied, so clone(CLONE_NEWUSER) must be too; clone3's flags are
    out of seccomp's reach, so it reports ENOSYS and glibc falls back to clone."""
    program = sandbox.seccomp_filter(machine)
    arch = sandbox._AUDIT_ARCH[machine]
    fork_flags = 0x01200011  # CLONE_CHILD_SETTID | CLONE_CHILD_CLEARTID | SIGCHLD
    assert _run_bpf(program, arch, clone, fork_flags) == ALLOW
    assert _run_bpf(program, arch, clone, fork_flags | sandbox.CLONE_NEWUSER) == EPERM
    assert _run_bpf(program, arch, clone3) == sandbox.SECCOMP_RET_ERRNO_ENOSYS


def test_seccomp_kills_another_architecture_and_x32_calls() -> None:
    program = sandbox.seccomp_filter("x86_64")
    i386 = 0x40000003
    assert _run_bpf(program, i386, 26) == KILL  # ptrace by its i386 number
    x32_unshare = 0x40000000 | 272
    assert _run_bpf(program, sandbox._AUDIT_ARCH["x86_64"], x32_unshare) == EPERM


def test_seccomp_names_the_syscalls_the_plan_lists() -> None:
    assert set(sandbox._DENIED_SYSCALLS) == {
        "ptrace", "mount", "umount2", "pivot_root", "unshare", "setns", "keyctl",
        "add_key", "request_key", "bpf", "perf_event_open", "kexec_load", "init_module",
        "finit_module", "delete_module", "userfaultfd", "open_by_handle_at",
        "process_vm_readv", "process_vm_writev",
    }  # fmt: skip


def test_bwrap_isolates_every_namespace_and_drops_capabilities(monkeypatch) -> None:
    monkeypatch.setattr(sandbox, "_disable_userns", True)
    argv = sandbox._bwrap_argv(["true"], scratch="/tmp/s", binds=[], fd=7)
    for flag in (
        "--unshare-user",
        "--unshare-pid",
        "--unshare-net",
        "--unshare-ipc",
        "--unshare-uts",
        "--die-with-parent",
        "--new-session",
        "--disable-userns",
    ):
        assert flag in argv
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert argv[argv.index("--seccomp") + 1] == "7"
    assert argv[argv.index("--bind") + 1 : argv.index("--bind") + 3] == ["/tmp/s", "/tmp"]
    assert argv[-2:] == ["--", "true"]
    mounted = {argv[i + 1] for i, a in enumerate(argv) if a in ("--ro-bind", "--bind")}
    assert not any(m.startswith(("/home", "/root", "/var/run", "/run")) for m in mounted)
    assert "/etc" not in mounted and "/etc/shadow" not in mounted


def test_a_read_only_path_inside_a_writable_one_stays_read_only(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    binds = sandbox._binds([checkout], [checkout / ".git"], "")
    assert binds == ["--bind", str(checkout), str(checkout)] + [
        "--ro-bind",
        str(checkout / ".git"),
        str(checkout / ".git"),
    ]


def test_a_writable_checkout_under_a_read_only_parent_stays_writable(tmp_path: Path) -> None:
    """bwrap mounts in order: a read-only parent after the checkout would hide it."""
    checkout = tmp_path / "tools" / "repos" / "checkout"
    binds = sandbox._binds([checkout], [tmp_path / "tools", checkout / ".git"], "")
    assert binds == [
        *("--ro-bind", str(tmp_path / "tools"), str(tmp_path / "tools")),
        *("--bind", str(checkout), str(checkout)),
        *("--ro-bind", str(checkout / ".git"), str(checkout / ".git")),
    ]


@pytest.fixture
def loom_dirs(tmp_path: Path, monkeypatch) -> Path:
    """A server run from `tmp_path` with the default data settings."""
    from server.settings import settings

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LOOM_ENV_FILE", str(tmp_path / "srv" / ".env"))
    for field in ("code_repos_root", "code_scip_checkout_dir"):
        monkeypatch.setattr(settings, field, type(settings).model_fields[field].default)
    for d in ("var/bin", "var/code_repos/self", "home/bin", "srv/bin", "opt/x/bin"):
        (tmp_path / d).mkdir(parents=True)
    (tmp_path / "var/bin/scip-clang").write_text("")
    (tmp_path / "srv/.env").write_text("")
    return tmp_path


def test_a_toolchain_is_never_mounted_with_loom_data(loom_dirs: Path) -> None:
    root = loom_dirs
    # the default scip-clang is mounted as that one file, never var/ or var/bin
    assert sandbox.tool_mounts(root / "var/bin/scip-clang") == [root / "var/bin/scip-clang"]
    assert sandbox.tool_mounts(root / "var/bin") == []
    assert sandbox.tool_mounts(root / "var/code_repos/self/checkout-x/node_modules/.bin") == []
    assert sandbox.tool_mounts(root / "home/bin") == []
    # srv/ holds the env file: only its bin directory is mounted
    assert sandbox.tool_mounts(root / "srv/bin") == [root / "srv/bin"]
    assert sandbox.tool_mounts(root / "srv/.env") == []
    assert sandbox.tool_mounts(root / "opt/x/bin") == [root / "opt/x"]
    assert sandbox.tool_mounts(root / "opt/x/bin" / ".." / ".." / "..") == []


def test_toolchains_on_path_are_mounted_but_nothing_under_the_server_home(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    tools = tmp_path / "opt" / "node" / "bin"
    for d in (home / ".local" / "bin", tools):
        d.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    paths = sandbox._readonly_paths({"PATH": f"{home}/.local/bin:{tools}:/usr/bin"}, [])
    assert tools.parent in paths  # a bin directory's parent: node's lib sits beside it
    assert not any(p.is_relative_to(home) for p in paths)
    assert Path(sys.prefix).absolute() in paths or Path(sys.prefix).is_relative_to("/usr")


def test_network_jobs_reach_the_relay_only(monkeypatch) -> None:
    env: dict[str, str] = {}
    sandbox._network_env(env)
    assert env["HTTPS_PROXY"] == env["https_proxy"] == sandbox.RELAY_URL
    from tests.unit.test_gitrepo_token_env import _config

    assert _config(env)["http.proxy"] == sandbox.RELAY_URL
    assert "ProxyCommand=" in env["GIT_SSH_COMMAND"]
    assert "connect %h %p" in env["GIT_SSH_COMMAND"]


async def test_without_the_sandbox_a_job_runs_as_a_plain_child(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(sandbox, "_available", False)
    done = await sandbox.run_job(
        ["sh", "-c", "echo $ONLY; echo err >&2"],
        env={"PATH": "/usr/bin:/bin", "ONLY": "given"},
        timeout_s=10,
        cwd=tmp_path,
    )
    assert (done.returncode, done.stdout, done.stderr) == (0, b"given\n", b"err\n")


async def test_a_job_past_its_timeout_is_killed(monkeypatch) -> None:
    monkeypatch.setattr(sandbox, "_available", False)
    with pytest.raises(TimeoutError):
        await sandbox.run_job(["sleep", "30"], env={"PATH": "/usr/bin:/bin"}, timeout_s=0.2)


async def test_a_detached_process_holding_stdout_does_not_outlast_the_timeout(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sandbox, "_available", False)
    monkeypatch.setattr(sandbox, "_REAP_GRACE_S", 0.3)
    pidfile = tmp_path / "pid"
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        with pytest.raises(TimeoutError):
            await sandbox.run_job(
                ["sh", "-c", f"setsid sleep 30 & echo $! > {pidfile}; echo hi; sleep 30"],
                env={"PATH": "/usr/bin:/bin"},
                timeout_s=0.2,
            )
        assert loop.time() - started < 5
    finally:
        # outside the sandbox nothing kills a process that left the session
        with contextlib.suppress(OSError, ValueError):
            os.kill(int(pidfile.read_text()), signal.SIGKILL)


def test_the_probe_says_no_without_bwrap(monkeypatch) -> None:
    monkeypatch.setattr(sandbox.shutil, "which", lambda _name: None)
    assert sandbox._probe() is False


def test_prlimit_sets_the_rlimits_in_front_of_bwrap_and_nproc_inside(monkeypatch) -> None:
    """No Python runs between fork and exec (preexec_fn can deadlock a threaded
    server). Memory is RLIMIT_DATA: node reserves far more address space than
    RLIMIT_AS would allow."""
    from server.settings import settings

    monkeypatch.setattr(settings, "code_sandbox_cpu_seconds", 60)
    monkeypatch.setattr(settings, "code_sandbox_memory_mb", 1)
    monkeypatch.setattr(settings, "code_sandbox_max_pids", 64)
    monkeypatch.setattr(settings, "code_sandbox_max_file_mb", 2)
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert sandbox._prlimit_argv() == [
        "/usr/bin/prlimit",
        "--cpu=60:60",
        "--data=1048576:1048576",
        "--fsize=2097152:2097152",
        "--",
    ]
    # RLIMIT_NPROC inside bwrap's user namespace: there it counts only the job's tasks.
    argv = sandbox._bwrap_argv(["true"], scratch="/tmp/s", binds=[], fd=7)
    assert argv[-5:] == ["--", "/usr/bin/prlimit", "--nproc=64:64", "--", "true"]


_SYSCTL_REFUSED = b"bwrap: sysctl user.max_user_namespaces = 1: Permission denied"


@pytest.mark.parametrize(
    ("help_text", "stderr", "available", "tried"),
    [
        # An AppArmor profile may refuse the sysctl write --disable-userns makes;
        # the seccomp filter still refuses the job new user namespaces.
        ("--disable-userns", {True: _SYSCTL_REFUSED}, True, [True, False]),
        ("--disable-userns", {}, True, [True]),
        ("--disable-userns", {True: _SYSCTL_REFUSED, False: b"bwrap: x"}, False, [True, False]),
        # any other failure is not the flag's: no retry
        ("--disable-userns", {True: b"bwrap: mount: Permission denied"}, False, [True]),
        ("--unshare-user", {}, True, [False]),
    ],
)
def test_the_probe_retries_without_disable_userns_when_the_sysctl_is_refused(
    monkeypatch, help_text: str, stderr: dict[bool, bytes], available: bool, tried: list[bool]
) -> None:
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(sandbox.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        sandbox.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=f"  {help_text}  ..."),
    )
    runs = []

    def job():
        runs.append(sandbox._disable_userns)
        err = stderr.get(sandbox._disable_userns, b"")
        return subprocess.CompletedProcess([], int(bool(err)), b"", err)

    monkeypatch.setattr(sandbox, "_probe_job", job)
    monkeypatch.setattr(sandbox, "_disable_userns", False)
    assert sandbox._probe() is available
    assert runs == tried and sandbox._disable_userns is tried[-1]


def test_the_probe_says_no_without_prlimit(monkeypatch) -> None:
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: None if name == "prlimit" else name)
    assert sandbox._probe() is False


def test_a_relative_or_home_holding_path_entry_mounts_nothing(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home" / "loom"
    home.mkdir(parents=True)
    (tmp_path / "home" / "bin").mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    paths = sandbox._readonly_paths({"PATH": f".:bin:{tmp_path}/home/bin:/usr/bin"}, [home])
    # `.` is the server's cwd; /home/bin's tool root /home holds HOME
    assert tmp_path not in paths and tmp_path / "home" not in paths
    assert home not in paths


def test_without_the_sandbox_the_ingest_pass_skips_self_registered_network_entries(
    monkeypatch,
) -> None:
    from types import SimpleNamespace

    from server.code.app.index import compiler_tier, ingest_pass
    from server.code.app.index.pass_budget import CodeIngestResult

    entries = [
        SimpleNamespace(org="o", namespace=ns, source=source, git_remote=remote)
        for ns, source, remote in [
            ("operator-net", "config", "https://github.com/a/b.git"),
            ("self-net", "registry", "https://evil.example:8123/a.git"),
            ("self-ssh", "registry", "git@evil.example:a/b.git"),
            ("self-local", "registry", "/srv/mirrors/a.git"),
        ]
    ]
    monkeypatch.setattr(sandbox, "_available", False)
    result = CodeIngestResult()
    kept = ingest_pass._without_unsandboxed(entries, result)
    assert [c.namespace for c in kept] == ["operator-net", "self-local"]
    assert result.note == f"self-ssh: {compiler_tier.UNSANDBOXED_NETWORK_SKIP}"
    monkeypatch.setattr(sandbox, "_available", True)
    assert ingest_pass._without_unsandboxed(entries, CodeIngestResult()) == entries


async def test_a_required_sandbox_skips_every_entry_before_any_fetch(
    tmp_path: Path, monkeypatch
) -> None:
    """Two entries sharing a mirror would be fetched once in prefetch_mirrors;
    with the sandbox required and missing, the pass skips them first instead of
    the fetch's SandboxRequiredError ending the pass."""
    from server.code.app.index import compiler_tier, gitrepo, ingest_pass, pass_fetch
    from server.code.app.index.pass_budget import CodeIngestResult
    from server.settings import settings
    from tests.unit.code_fixtures import repo_config

    mirror = tmp_path / "shared.git"
    mirror.mkdir()
    entries = [
        repo_config(
            namespace=ns,
            git_dir=str(mirror),
            git_remote="https://github.com/a/b.git",
            git_branch=branch,
        )
        for ns, branch in [("code-main", "main"), ("code-rel", "release")]
    ]
    monkeypatch.setattr(sandbox, "_available", False)
    monkeypatch.setattr(settings, "code_sandbox_required", True)
    fetched = []

    async def fetch_many(*a, **k):
        fetched.append(a)

    monkeypatch.setattr(gitrepo, "fetch_many", fetch_many)
    result = CodeIngestResult()
    kept = ingest_pass._without_unsandboxed(entries, result)
    assert kept == [] and result.note.endswith(compiler_tier.SANDBOX_REQUIRED_SKIP)
    assert await pass_fetch.prefetch_mirrors(kept, result) == 0 and fetched == []
    # the operator entries are skipped too, not only self-registered ones
    assert compiler_tier.unsandboxed_skip(entries[0], "code_scip_delta", runs_repo_code=True)


async def test_a_refused_clone_records_an_error_stage(tmp_path: Path, monkeypatch) -> None:
    from unittest.mock import AsyncMock

    from server.code.app.index import bootstrap
    from server.settings import settings
    from tests.unit.code_fixtures import repo_config

    monkeypatch.setattr(sandbox, "_available", False)
    monkeypatch.setattr(settings, "code_sandbox_required", True)
    cfg = repo_config(git_dir=str(tmp_path / "m.git"), git_remote=str(tmp_path / "src.git"))
    repo = AsyncMock()
    repo.get_state.return_value = ""  # not indexed yet: the card shows the stage
    assert await bootstrap.clone_if_missing(cfg, repo) is False
    stage = repo.set_state.await_args_list[-1]
    assert stage.args[2:4] == (bootstrap.STAGE_KEY, "error")
    assert "LOOM_CODE_SANDBOX_REQUIRED" in stage.kwargs["detail"]


async def test_a_parsed_stdout_past_its_limit_kills_the_job(monkeypatch) -> None:
    monkeypatch.setattr(sandbox, "_available", False)
    monkeypatch.setattr(sandbox, "_STDOUT_LIMIT", 4096)
    done = await sandbox.run_job(
        ["sh", "-c", "head -c 1000000 /dev/zero; sleep 30"],
        env={"PATH": "/usr/bin:/bin"},
        timeout_s=20,
        keep_stdout=True,
    )
    assert done.returncode != 0 and done.stdout == b""
    assert b"stdout passed 4096 bytes" in done.stderr


async def test_diagnostic_output_keeps_a_bounded_tail(monkeypatch) -> None:
    monkeypatch.setattr(sandbox, "_available", False)
    monkeypatch.setattr(sandbox, "_TAIL_BYTES", 100)
    flood = "head -c 1000000 /dev/zero | tr '\\0' x"
    done = await sandbox.run_job(
        ["sh", "-c", f"{flood}; echo out-end; {flood} >&2; echo err-end >&2"],
        env={"PATH": "/usr/bin:/bin"},
        timeout_s=20,
    )
    assert done.returncode == 0
    assert len(done.stdout) == 100 and done.stdout.endswith(b"out-end\n")
    assert len(done.stderr) == 100 and done.stderr.endswith(b"err-end\n")


async def test_stdin_reaches_the_job_and_stdout_is_kept_whole(monkeypatch) -> None:
    monkeypatch.setattr(sandbox, "_available", False)
    monkeypatch.setattr(sandbox, "_TAIL_BYTES", 10)
    payload = b"refs/heads/main\n" * 1000
    done = await sandbox.run_job(
        ["cat"], env={"PATH": "/usr/bin:/bin"}, timeout_s=10, stdin=payload, keep_stdout=True
    )
    assert (done.returncode, done.stdout) == (0, payload)


def test_loom_data_inside_a_mounted_python_prefix_is_hidden(tmp_path: Path, monkeypatch) -> None:
    """Checkouts, the env file and var/ under the server's python prefix: each mount that
    shows them is followed by an empty one over them; the job's own checkout inside
    the checkout directory is still mounted after it."""
    from server.settings import settings

    prefix = tmp_path / "py"
    for d in ("checkouts/b", "var", "bin"):
        (prefix / d).mkdir(parents=True)
    (prefix / ".env").write_text("SECRET=1")
    monkeypatch.chdir(prefix)
    monkeypatch.setattr(sys, "prefix", str(prefix))
    monkeypatch.setattr(sys, "base_prefix", str(prefix))
    monkeypatch.setattr(settings, "code_scip_checkout_dir", str(prefix / "checkouts"))
    monkeypatch.setenv("LOOM_ENV_FILE", str(prefix / ".env"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    checkout = prefix / "checkouts" / "b"
    readonly = sandbox._readonly_paths({"PATH": "/usr/bin"}, [])
    assert prefix in readonly
    argv = sandbox._binds([checkout], readonly, "")

    def at(*args: str) -> int:
        return next(i for i in range(len(argv)) if argv[i : i + len(args)] == list(args))

    shown = at("--ro-bind", str(prefix), str(prefix))
    assert shown < at("--tmpfs", str(prefix / "checkouts")) < at("--bind", str(checkout))
    assert shown < at("--tmpfs", str(prefix / "var"))
    assert shown < at("--ro-bind", "/dev/null", str(prefix / ".env"))
    assert str(prefix / "bin") not in argv


def test_a_path_holding_no_loom_data_gets_no_cover(tmp_path: Path, monkeypatch) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    monkeypatch.chdir(tmp_path)
    assert sandbox._hidden([str(tools), "/usr"]) == []


@pytest.mark.parametrize("where", ["checkouts", "checkouts/py"])
def test_a_python_prefix_that_is_or_sits_in_loom_data_is_refused(
    where: str, tmp_path: Path, monkeypatch
) -> None:
    from server.settings import settings

    (tmp_path / "checkouts" / "py").mkdir(parents=True)
    monkeypatch.setattr(settings, "code_scip_checkout_dir", str(tmp_path / "checkouts"))
    monkeypatch.setattr(sys, "prefix", str(tmp_path / where))
    with pytest.raises(PermissionError):
        sandbox._readonly_paths({"PATH": "/usr/bin"}, [])


def test_a_system_path_inside_loom_data_is_refused(tmp_path: Path, monkeypatch) -> None:
    from server.settings import settings

    (tmp_path / "checkouts" / "usr").mkdir(parents=True)
    monkeypatch.setattr(settings, "code_scip_checkout_dir", str(tmp_path / "checkouts"))
    monkeypatch.setattr(sandbox, "_SYSTEM_PATHS", (str(tmp_path / "checkouts" / "usr"),))
    with pytest.raises(PermissionError):
        sandbox._readonly_paths({"PATH": "/usr/bin"}, [])


async def test_a_job_that_closes_its_output_still_dies_at_its_timeout(monkeypatch) -> None:
    monkeypatch.setattr(sandbox, "_available", False)
    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(TimeoutError):
        await sandbox.run_job(
            ["sh", "-c", "exec 1>&- 2>&-; sleep 30"], env={"PATH": "/usr/bin:/bin"}, timeout_s=0.3
        )
    assert loop.time() - started < 5


async def test_cancelling_a_job_that_closed_its_output_kills_and_reaps_it(monkeypatch) -> None:
    monkeypatch.setattr(sandbox, "_available", False)
    pids: list[int] = []
    real_exec = asyncio.create_subprocess_exec

    async def spy(*args, **kwargs):
        proc = await real_exec(*args, **kwargs)
        pids.append(proc.pid)
        return proc

    monkeypatch.setattr(sandbox.asyncio, "create_subprocess_exec", spy)
    job = asyncio.ensure_future(
        sandbox.run_job(
            ["sh", "-c", "exec 1>&- 2>&-; sleep 30"], env={"PATH": "/usr/bin:/bin"}, timeout_s=30
        )
    )
    await asyncio.sleep(0.3)
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    with pytest.raises(ProcessLookupError):  # killed and reaped: no such pid any more
        os.kill(pids[0], 0)


def test_a_refused_layout_is_a_health_problem(tmp_path: Path, monkeypatch) -> None:
    from server.observability.problems import HealthState, collect_problems
    from server.settings import settings

    (tmp_path / "checkouts").mkdir()
    monkeypatch.setattr(settings, "code_scip_checkout_dir", str(tmp_path / "checkouts"))
    monkeypatch.setattr(settings, "code_ingest_enabled", True)
    assert sandbox.layout_refusal() == ""
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "checkouts"))
    refusal = sandbox.layout_refusal()
    assert "inside Loom's data" in refusal
    problems = collect_problems(
        settings, HealthState(repo_sandbox=True, repo_sandbox_refusal=refusal)
    )
    assert [p.code for p in problems if p.code.startswith("repo_")] == [
        "repo_sandbox_layout_refused"
    ]


def _layout_codes(monkeypatch, tmp_path: Path, probed, bwrap_on_path: bool):
    from server.observability import problems as problems_mod
    from server.observability.problems import HealthState, collect_problems
    from server.settings import settings

    monkeypatch.setattr(settings, "code_ingest_enabled", True)
    found = "/usr/bin/bwrap" if bwrap_on_path else None
    monkeypatch.setattr(problems_mod.shutil, "which", lambda name: found)
    state = HealthState(repo_sandbox=probed, repo_sandbox_refusal="sandbox: /x is inside")
    return [(p.code, p.severity) for p in collect_problems(settings, state) if "sandbox" in p.code]


def test_a_refused_layout_before_the_probe_is_an_error_only_with_bwrap(
    tmp_path: Path, monkeypatch
) -> None:
    error = [("repo_sandbox_layout_refused", "error")]
    warn = [("repo_sandbox_layout_refused", "warn")]
    assert _layout_codes(monkeypatch, tmp_path, None, bwrap_on_path=True) == error
    # No bwrap: jobs will run unsandboxed and succeed, so health is not degraded.
    assert _layout_codes(monkeypatch, tmp_path, None, bwrap_on_path=False) == warn
    assert _layout_codes(monkeypatch, tmp_path, True, bwrap_on_path=False) == error


def test_the_layout_check_and_the_mount_refusal_read_one_list(tmp_path: Path, monkeypatch) -> None:
    from server.settings import settings

    (tmp_path / "checkouts").mkdir()
    monkeypatch.setattr(settings, "code_scip_checkout_dir", str(tmp_path / "checkouts"))
    monkeypatch.setattr(sandbox, "_own_mounts", lambda: [tmp_path / "checkouts"])
    assert "inside Loom's data" in sandbox.layout_refusal()
    with pytest.raises(PermissionError):
        sandbox._readonly_paths({"PATH": "/usr/bin"}, [])


async def test_a_required_sandbox_refuses_the_job_instead_of_running_it_plain(
    tmp_path: Path, monkeypatch
) -> None:
    from server.settings import settings

    monkeypatch.setattr(sandbox, "_available", False)
    marker = tmp_path / "ran"
    argv = ["sh", "-c", f"touch {marker}"]
    env = {"PATH": "/usr/bin:/bin"}
    monkeypatch.setattr(settings, "code_sandbox_required", True)
    with pytest.raises(sandbox.SandboxRequiredError, match="LOOM_CODE_SANDBOX_REQUIRED"):
        await sandbox.run_job(argv, env=env, timeout_s=10)
    assert not marker.exists()
    monkeypatch.setattr(settings, "code_sandbox_required", False)
    assert (await sandbox.run_job(argv, env=env, timeout_s=10)).returncode == 0
    assert marker.exists()


@pytest.mark.parametrize(("required", "severity"), [(True, "error"), (False, "warn")])
def test_a_missing_sandbox_is_an_error_only_when_required(
    monkeypatch, required: bool, severity: str
) -> None:
    from server.observability.problems import HealthState, collect_problems
    from server.settings import settings

    monkeypatch.setattr(settings, "code_ingest_enabled", True)
    monkeypatch.setattr(settings, "code_sandbox_required", required)
    problems = collect_problems(settings, HealthState(repo_sandbox=False))
    assert [(p.code, p.severity) for p in problems if p.code == "repo_jobs_unsandboxed"] == [
        ("repo_jobs_unsandboxed", severity)
    ]


@pytest.mark.parametrize(("required", "rc", "ran"), [(True, 1, False), (False, 1, True)])
async def test_scip_full_runs_its_harness_as_a_gated_repository_job(
    tmp_path: Path, monkeypatch, required: bool, rc: int, ran: bool
) -> None:
    """The weekly full pass runs the repository's CMake: through run_harness
    (the sandbox), and not at all where the sandbox is required and missing."""
    import argparse
    from unittest.mock import AsyncMock

    from server.cli import code_scip
    from server.code.app.index import compiler_tier, scip_full
    from server.code.store import index as store_index
    from server.settings import settings
    from tests.unit.code_fixtures import repo_config

    for d in ("checkout", "bare.git"):
        (tmp_path / d).mkdir()
    (tmp_path / "scip-clang").touch()
    monkeypatch.setattr(settings, "code_scip_clang_path", str(tmp_path / "scip-clang"))
    monkeypatch.setattr(settings, "code_sandbox_required", required)
    monkeypatch.setattr(sandbox, "_available", False)
    repo = AsyncMock()
    repo.get_state.return_value = "a" * 40
    monkeypatch.setattr(store_index, "CodeRepo", lambda: repo)
    monkeypatch.setattr(scip_full, "move_checkout", AsyncMock())
    harness = AsyncMock(return_value=(1, "harness output\n"))
    monkeypatch.setattr(compiler_tier, "run_harness", harness)
    cfg = repo_config(
        git_dir=str(tmp_path / "bare.git"), scip_checkout_dir=str(tmp_path / "checkout")
    )
    args = argparse.Namespace(checkout="", jobs=0, apply_delta="", keep_delta="")
    assert await code_scip._scip_full_locked(args, "o", "code-x", cfg) == rc
    assert harness.await_count == int(ran)
    if ran:
        assert harness.await_args.args[2] == ["--full", "", "-"]


@pytest.mark.asyncio
async def test_scip_full_runs_in_the_clang_tree_when_clang_is_an_extra_tier(
    tmp_path: Path, monkeypatch
) -> None:
    import argparse
    from unittest.mock import AsyncMock

    from server.cli import code_scip
    from server.code.app.index import compiler_tier, scip_full
    from server.code.store import index as store_index
    from server.settings import settings
    from tests.unit.code_fixtures import repo_config

    for d in ("checkout", "checkout.clang", "bare.git"):
        (tmp_path / d).mkdir()
    (tmp_path / "scip-clang").touch()
    monkeypatch.setattr(settings, "code_scip_clang_path", str(tmp_path / "scip-clang"))
    monkeypatch.setattr(sandbox, "_available", True)
    repo = AsyncMock()
    repo.get_state.return_value = "a" * 40
    monkeypatch.setattr(store_index, "CodeRepo", lambda: repo)
    monkeypatch.setattr(scip_full, "move_checkout", AsyncMock())
    harness = AsyncMock(return_value=(1, "harness output\n"))
    monkeypatch.setattr(compiler_tier, "run_harness", harness)
    cfg = repo_config(
        git_dir=str(tmp_path / "bare.git"),
        scip_checkout_dir=str(tmp_path / "checkout"),
        scip_producer="python",
        scip_extra_producers=("clang",),
    )
    args = argparse.Namespace(checkout="", jobs=0, apply_delta="", keep_delta="")
    await code_scip._scip_full_locked(args, "o", "code-x", cfg)
    assert harness.await_args.args[1] == (tmp_path / "checkout.clang").resolve()


@pytest.mark.asyncio
async def test_scip_full_moves_its_checkout_as_sandboxed_jobs(tmp_path: Path, monkeypatch) -> None:
    """The fetch reads the bare clone, which the sandbox mounts only when named,
    and the submodule pins come from the network with the repository's token, so
    a private submodule of the repository's owner is fetched."""
    from server.code.app.index import compiler_tier, scip_full
    from tests.unit.code_fixtures import repo_config

    calls: list[tuple[tuple[str, ...], dict]] = []

    async def _git(cwd: Path, *args: str, **kw: object) -> None:
        calls.append((args, kw))

    monkeypatch.setattr(compiler_tier, "git", _git)
    bare = tmp_path / "bare.git"
    cfg = repo_config(github_token="ghs_test", git_remote="https://github.com/o/private")
    await scip_full.move_checkout(tmp_path / "checkout", bare, "a" * 40, cfg)
    fetch, _checkout, submodules = calls
    assert fetch[0][0] == "fetch" and fetch[1]["local_remote"] == bare
    assert "token" not in fetch[1]
    assert submodules[0][0] == "submodule" and submodules[1]["network"] is True
    assert submodules[1]["token"] == "ghs_test"
    assert submodules[1]["remote"] == "https://github.com/o/private"


@pytest.mark.asyncio
async def test_a_networked_compiler_tier_git_authenticates_to_the_repository_owner(
    tmp_path: Path, monkeypatch
) -> None:
    """`git(..., token=, remote=)` hands the job the token as an extraheader for the
    remote's owner, never in argv; without a token the job carries no credential."""
    from server.code.app.index import compiler_tier

    envs: list[dict[str, str]] = []
    argvs: list[list[str]] = []

    async def _run_job(argv: list[str], **kw: object) -> sandbox.JobResult:
        argvs.append(argv)
        envs.append(kw["env"])  # type: ignore[arg-type]
        return sandbox.JobResult(0, b"", b"")

    monkeypatch.setattr(compiler_tier, "run_job", _run_job)
    args = ("submodule", "update", "--init")
    await compiler_tier.git(
        tmp_path, *args, network=True, token="ghs_test", remote="https://github.com/o/r"
    )
    await compiler_tier.git(tmp_path, *args, network=True)
    with_token, without = envs

    def config(env: dict[str, str]) -> dict[str, str]:
        n = int(env.get("GIT_CONFIG_COUNT", "0"))
        return {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(n)}

    header = config(with_token)["http.https://github.com/o/.extraheader"]
    assert header.startswith("AUTHORIZATION: basic ")
    assert not any("ghs_test" in a for a in argvs[0])
    assert not any(k.endswith(".extraheader") for k in config(without))


@pytest.mark.asyncio
async def test_a_local_remote_entry_keeps_the_file_transport_off_its_submodules(
    tmp_path: Path, monkeypatch
) -> None:
    """An entry whose remote is a local mirror path must not open `file://`
    `.gitmodules` URLs to its networked submodule update."""
    from server.code.app.index import compiler_tier

    envs: list[dict[str, str]] = []

    async def _run_job(argv: list[str], **kw: object) -> sandbox.JobResult:
        envs.append(kw["env"])  # type: ignore[arg-type]
        return sandbox.JobResult(0, b"", b"")

    monkeypatch.setattr(compiler_tier, "run_job", _run_job)
    await compiler_tier.git(tmp_path, "submodule", "update", network=True, remote="/srv/m.git")
    await compiler_tier.git(
        tmp_path, "submodule", "update", network=True, token="t", remote="/srv/m.git"
    )
    for env in envs:
        n = int(env.get("GIT_CONFIG_COUNT", "0"))
        assert "protocol.file.allow" not in {env[f"GIT_CONFIG_KEY_{i}"] for i in range(n)}


@pytest.mark.asyncio
async def test_a_scip_full_harness_timeout_is_reported_as_one(tmp_path: Path, monkeypatch) -> None:

    from server.code.app.index import compiler_tier, scip_full

    async def _timed_out(*_a: object, **_k: object) -> tuple[int, str]:
        raise RuntimeError("harness timed out after 21600s")

    monkeypatch.setattr(compiler_tier, "run_harness", _timed_out)
    rc, _ = await scip_full.run_full_harness(
        tmp_path, tmp_path / "scip-clang", tmp_path / "delta.json", timeout_s=1
    )
    assert rc == 124


@pytest.mark.asyncio
async def test_scip_full_hands_the_sandbox_an_absolute_checkout(
    tmp_path: Path, monkeypatch
) -> None:
    """A relative --checkout names a directory from the caller's working
    directory, which bwrap leaves: every path the job gets is absolute."""
    import argparse
    from unittest.mock import AsyncMock

    from server.cli import code_scip
    from server.code.app.index import compiler_tier, scip_full
    from server.code.store import index as store_index
    from server.settings import settings
    from tests.unit.code_fixtures import repo_config

    for d in ("checkout", "bare.git", "elsewhere"):
        (tmp_path / d).mkdir()
    (tmp_path / "scip-clang").touch()
    monkeypatch.chdir(tmp_path / "elsewhere")
    monkeypatch.setattr(settings, "code_scip_clang_path", str(tmp_path / "scip-clang"))
    monkeypatch.setattr(settings, "code_sandbox_required", False)
    monkeypatch.setattr(sandbox, "_available", False)
    repo = AsyncMock()
    repo.get_state.return_value = "a" * 40
    monkeypatch.setattr(store_index, "CodeRepo", lambda: repo)
    move = AsyncMock()
    monkeypatch.setattr(scip_full, "move_checkout", move)
    harness = AsyncMock(return_value=(1, ""))
    monkeypatch.setattr(compiler_tier, "run_harness", harness)
    cfg = repo_config(git_dir=str(tmp_path / "bare.git"), scip_checkout_dir="")
    args = argparse.Namespace(checkout="../checkout", jobs=0, apply_delta="", keep_delta="")
    await code_scip._scip_full_locked(args, "o", "code-x", cfg)
    checkout = (tmp_path / "checkout").resolve()
    assert move.await_args.args[0] == checkout
    assert harness.await_args.args[1] == checkout
