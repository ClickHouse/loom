"""The repository-job sandbox's pieces that need no bubblewrap: the seccomp
program, the mounts a job gets, and the fallback when the host has no sandbox."""

from __future__ import annotations

import struct
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
    for field in ("code_repos_root", "blob_dir", "code_scip_checkout_dir"):
        monkeypatch.setattr(settings, field, type(settings).model_fields[field].default)
    for d in ("var/bin", "var/blobs", "var/code_repos/self", "home/bin", "srv/bin", "opt/x/bin"):
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


def test_the_probe_says_no_without_bwrap(monkeypatch) -> None:
    monkeypatch.setattr(sandbox.shutil, "which", lambda _name: None)
    assert sandbox._probe() is False


def test_prlimit_sets_the_rlimits_in_front_of_bwrap(monkeypatch) -> None:
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
        "--nproc=64:64",
        "--fsize=2097152:2097152",
        "--",
    ]


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
    """Blobs, the env file and var/ under the server's python prefix: each mount that
    shows them is followed by an empty one over them; the job's own checkout inside
    the blob store is still mounted after it."""
    from server.settings import settings

    prefix = tmp_path / "py"
    for d in ("blobs/b", "var", "bin"):
        (prefix / d).mkdir(parents=True)
    (prefix / ".env").write_text("SECRET=1")
    monkeypatch.chdir(prefix)
    monkeypatch.setattr(sys, "prefix", str(prefix))
    monkeypatch.setattr(sys, "base_prefix", str(prefix))
    monkeypatch.setattr(settings, "blob_dir", str(prefix / "blobs"))
    monkeypatch.setenv("LOOM_ENV_FILE", str(prefix / ".env"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    checkout = prefix / "blobs" / "b"
    readonly = sandbox._readonly_paths({"PATH": "/usr/bin"}, [])
    assert prefix in readonly
    argv = sandbox._binds([checkout], readonly, "")

    def at(*args: str) -> int:
        return next(i for i in range(len(argv)) if argv[i : i + len(args)] == list(args))

    shown = at("--ro-bind", str(prefix), str(prefix))
    assert shown < at("--tmpfs", str(prefix / "blobs")) < at("--bind", str(checkout))
    assert shown < at("--tmpfs", str(prefix / "var"))
    assert shown < at("--ro-bind", "/dev/null", str(prefix / ".env"))
    assert str(prefix / "bin") not in argv


def test_a_path_holding_no_loom_data_gets_no_cover(tmp_path: Path, monkeypatch) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    monkeypatch.chdir(tmp_path)
    assert sandbox._hidden([str(tools), "/usr"]) == []
