"""The one isolation boundary for repository jobs.

Clone, fetch, `git submodule update`, dependency installs and the compiler-tier
harnesses read a repository's files and run what it ships (a CMakeLists.txt is a
program). `run_job` runs each of them inside bubblewrap when the host supports it:

- its own user, pid, network, mount, ipc and uts namespaces, no capabilities, no
  new user namespaces, and a seccomp filter that refuses the syscalls a job never
  needs (`seccomp_filter`);
- only the system directories, the toolchains (never a directory holding Loom's
  data, `tool_mounts`) and the paths the caller names are mounted; the caller's
  work directories are writable, everything else read-only. /tmp and HOME are a
  fresh directory removed after the job. No server environment, credentials, host
  HOME or Docker socket;
- rlimits on CPU, data memory, processes and file size, and a wall-clock timeout that
  kills the whole tree: when bwrap dies its pid namespace dies with it, so no
  process the job started survives it. The rlimits are per process (CPU, data
  memory, file size) and per uid (processes), not a cgroup: they bound each process
  of a job, not the job's total;
- no network unless the caller asks for it, and then the only way out is the
  egress proxy (`egress.py`), which allows public addresses only.

Without a usable bwrap (or without prlimit) the job runs as a plain child process, as it always has;
`sandbox_available()` says which, and the callers decide what may run then.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import shutil
import struct
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Required, TypedDict, Unpack

from server.code.app.index.gitrepo import communicate_or_kill
from server.settings import settings

logger = logging.getLogger("loom.code_index.sandbox")

# Inside the sandbox: the relay listens here and forwards to the egress proxy.
RELAY_PORT = 3128
RELAY_URL = f"http://127.0.0.1:{RELAY_PORT}"
_RELAY_SCRIPT = Path(__file__).with_name("egress_relay.py")
_RELAY_INSIDE = "/run/loom-relay.py"
_EGRESS_INSIDE = "/run/loom-egress"
_HOME_INSIDE = "/tmp/home"

# Read-only from the host: programs, libraries, CA certificates and the user
# database ssh and git look themselves up in. Nothing else of /etc.
_SYSTEM_PATHS = (
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib32",
    "/lib64",
    "/libx32",
    "/etc/alternatives",
    "/etc/ca-certificates",
    "/etc/ssl",
    "/etc/ld.so.cache",
    "/etc/ld.so.conf",
    "/etc/ld.so.conf.d",
    "/etc/passwd",
    "/etc/group",
    "/etc/nsswitch.conf",
)

# seccomp: syscalls a repository job never needs, as (x86_64, aarch64) numbers.
_DENIED_SYSCALLS = {
    "ptrace": (101, 117),
    "mount": (165, 40),
    "umount2": (166, 39),
    "pivot_root": (155, 41),
    "unshare": (272, 97),
    "setns": (308, 268),
    "keyctl": (250, 219),
    "add_key": (248, 217),
    "request_key": (249, 218),
    "bpf": (321, 280),
    "perf_event_open": (298, 241),
    "kexec_load": (246, 104),
    "init_module": (175, 105),
    "finit_module": (313, 273),
    "delete_module": (176, 106),
    "userfaultfd": (323, 282),
    "open_by_handle_at": (304, 265),
    "process_vm_readv": (310, 270),
    "process_vm_writev": (311, 271),
}
# clone is allowed unless its flags ask for a new user namespace (unshare's
# loophole); clone3 passes its flags in memory seccomp cannot read, so it gets
# ENOSYS and glibc falls back to clone.
_CLONE = (56, 220)
_CLONE3 = (435, 435)
CLONE_NEWUSER = 0x10000000
_MACHINES = ("x86_64", "aarch64")
_AUDIT_ARCH = {"x86_64": 0xC000003E, "aarch64": 0xC00000B7}
_X32_SYSCALL_BIT = 0x40000000
_BPF_LD_W_ABS = 0x20
_BPF_JEQ = 0x15
_BPF_JGE = 0x35
_BPF_JSET = 0x45
_BPF_RET = 0x06
_ARG0_LOW = 16  # seccomp_data.args[0], low 32 bits (little-endian)
SECCOMP_RET_KILL_PROCESS = 0x80000000
SECCOMP_RET_ERRNO_EPERM = 0x00050000 | 1
SECCOMP_RET_ERRNO_ENOSYS = 0x00050000 | 38
SECCOMP_RET_ALLOW = 0x7FFF0000


@dataclass(frozen=True)
class JobResult:
    returncode: int
    stdout: bytes
    stderr: bytes


def seccomp_filter(machine: str = "") -> bytes:
    """The classic-BPF program bwrap installs with `--seccomp`: kill a process
    calling with another architecture's numbers (and x32 ones on x86_64, which
    would bypass the list), EPERM for each denied syscall and for a clone with
    CLONE_NEWUSER, ENOSYS for clone3, allow the rest."""
    machine = machine or platform.machine()
    prog = [
        (_BPF_LD_W_ABS, 0, 0, 4),  # seccomp_data.arch
        (_BPF_JEQ, 1, 0, _AUDIT_ARCH[machine]),
        (_BPF_RET, 0, 0, SECCOMP_RET_KILL_PROCESS),
        (_BPF_LD_W_ABS, 0, 0, 0),  # seccomp_data.nr
    ]
    if machine == "x86_64":
        prog += [(_BPF_JGE, 0, 1, _X32_SYSCALL_BIT), (_BPF_RET, 0, 0, SECCOMP_RET_ERRNO_EPERM)]
    column = _MACHINES.index(machine)
    for numbers in _DENIED_SYSCALLS.values():
        nr = numbers[column]
        prog += [(_BPF_JEQ, 0, 1, nr), (_BPF_RET, 0, 0, SECCOMP_RET_ERRNO_EPERM)]
    prog += [(_BPF_JEQ, 0, 1, _CLONE3[column]), (_BPF_RET, 0, 0, SECCOMP_RET_ERRNO_ENOSYS)]
    prog += [
        (_BPF_JEQ, 0, 3, _CLONE[column]),
        (_BPF_LD_W_ABS, 0, 0, _ARG0_LOW),
        (_BPF_JSET, 0, 1, CLONE_NEWUSER),
        (_BPF_RET, 0, 0, SECCOMP_RET_ERRNO_EPERM),
    ]
    prog.append((_BPF_RET, 0, 0, SECCOMP_RET_ALLOW))
    return b"".join(struct.pack("=HBBI", *ins) for ins in prog)


_available: bool | None = None
_disable_userns = False


def sandbox_available() -> bool:
    """Whether repository jobs run inside the sandbox on this host: bwrap and
    prlimit are installed, the architecture has a seccomp table, and a trivial job runs.
    Probed once per process."""
    global _available
    if _available is None:
        _available = _probe()
        if not _available:
            logger.warning(
                "repository jobs run UNSANDBOXED: bubblewrap is missing or cannot create "
                "namespaces here; build-based tiers execute repository code on this host"
            )
    return _available


def sandbox_state() -> bool | None:
    """The probe's answer if this process has probed, else None (no probe here:
    `/v1/health` must not spawn processes)."""
    return _available


def _probe() -> bool:
    global _disable_userns
    bwrap = shutil.which("bwrap")
    if not bwrap or not shutil.which("prlimit") or platform.machine() not in _AUDIT_ARCH:
        return False
    try:
        help_text = subprocess.run(
            [bwrap, "--help"], capture_output=True, text=True, timeout=10
        ).stdout
        _disable_userns = "--disable-userns" in help_text
        scratch = tempfile.mkdtemp(prefix="loom-probe-")
        try:
            fd = _seccomp_fd()
            try:
                argv = _bwrap_argv(["true"], scratch=scratch, binds=[], fd=fd)
                done = subprocess.run(
                    argv, pass_fds=(fd,), capture_output=True, timeout=10, env={"PATH": os.defpath}
                )
            finally:
                os.close(fd)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
    except (OSError, subprocess.SubprocessError) as e:
        logger.info("sandbox probe failed: %s", e)
        return False
    if done.returncode != 0:
        logger.info("sandbox probe failed: %s", done.stderr.decode(errors="replace")[-300:])
    return done.returncode == 0


def _seccomp_fd() -> int:
    """A file descriptor holding the seccomp program, for `--seccomp FD`."""
    fd = os.memfd_create("loom-seccomp", 0)
    os.write(fd, seccomp_filter())
    os.lseek(fd, 0, os.SEEK_SET)
    return fd


def _system_binds() -> list[str]:
    out: list[str] = []
    for p in _SYSTEM_PATHS:
        if os.path.islink(p):
            out += ["--symlink", os.readlink(p), p]
        elif os.path.exists(p):
            out += ["--ro-bind", p, p]
    return out


def _bwrap_argv(argv: Sequence[str], *, scratch: str, binds: list[str], fd: int) -> list[str]:
    args = [
        shutil.which("bwrap") or "bwrap",
        "--unshare-user",
        "--unshare-pid",
        "--unshare-net",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-cgroup-try",
        "--die-with-parent",
        "--new-session",
        "--cap-drop",
        "ALL",
    ]
    if _disable_userns:
        args.append("--disable-userns")
    args += _system_binds()
    args += ["--proc", "/proc", "--dev", "/dev", "--bind", scratch, "/tmp", "--dir", _HOME_INSIDE]
    args += binds
    args += ["--seccomp", str(fd)]
    return [*args, "--", *argv]


def tool_mounts(path: Path) -> list[Path]:
    """What to mount for a tool at `path` (a binary or its directory): a `bin`
    directory's parent when that is safe (node's lib, go's pkg sit beside it),
    else `path` itself, else nothing (logged)."""
    candidates = [path.parent, path] if path.name in ("bin", ".bin") else [path]
    for candidate in candidates:
        if not _exposes_data(candidate):
            return [candidate]
    logger.info("toolchain %s not mounted: it holds or sits in Loom's data", path)
    return []


def _data_paths() -> list[Path]:
    """Where Loom keeps what no job may read: repository mirrors and
    checkouts, blobs and the env file."""
    from server.core.env_file import env_file_path

    named = [settings.code_repos_root, settings.blob_dir, settings.code_scip_checkout_dir]
    return [Path(p).absolute() for p in [*named, env_file_path() or ""] if p]


def _exposes_data(path: Path) -> bool:
    """Whether mounting `path` as a toolchain could show a job Loom's data: it
    is or holds a data path, var/, the install root or HOME, or it sits inside
    a data path. A directory may not sit in var/ or under HOME either (a file,
    such as var/bin/scip-clang, may)."""
    from server.core.paths import REPO_ROOT

    data = _data_paths()
    home = Path.home().absolute()
    var = [Path("var").absolute(), REPO_ROOT / "var"]
    for p in {path.absolute(), path.resolve()}:
        if any(q.is_relative_to(p) for q in [*data, *var, REPO_ROOT, home]):
            return True
        if any(p.is_relative_to(q) for q in data):
            return True
        if p.is_dir() and any(p.is_relative_to(q) for q in [*var, home]):
            return True
    return False


def _readonly_paths(env: Mapping[str, str], readonly: Sequence[Path]) -> list[Path]:
    """The caller's read-only paths, the server's python (harness converters and
    the relay run it) and the toolchains on the job's PATH (absolute entries
    only, through `tool_mounts`). Never the server's HOME or a directory above it."""
    home = Path.home()
    found = [Path(p) for p in env.get("PATH", "").split(":") if p.startswith("/")]
    from_path = [mount for p in found for mount in tool_mounts(p)]
    paths = [*readonly, Path(sys.prefix), Path(sys.base_prefix), *from_path]
    out: list[Path] = []
    for p in paths:
        p = p.absolute()
        holds_home = home.is_relative_to(p)
        system = any(p.is_relative_to(d) for d in _SYSTEM_PATHS)
        if not (holds_home or system or p in out) and p.exists():
            out.append(p)
    return out


def _binds(writable: Sequence[Path], readonly: Sequence[Path], egress_dir: str) -> list[str]:
    """Parents before children (bwrap mounts in order, and a later mount hides
    what it covers): a read-only parent cannot hide a writable checkout, and a
    read-only path inside a writable one (a checkout's .git) stays read-only.
    At equal depth a read-only bind comes last and wins."""
    binds = [("--bind", os.path.abspath(p)) for p in writable]
    binds += [("--ro-bind", os.path.abspath(p)) for p in readonly]
    binds.sort(key=lambda bind: len(Path(bind[1]).parts))
    out: list[str] = []
    for flag, p in binds:
        out += [flag, p, p]
    if egress_dir:
        out += [
            "--ro-bind",
            egress_dir,
            _EGRESS_INSIDE,
            "--ro-bind",
            str(_RELAY_SCRIPT),
            _RELAY_INSIDE,
        ]
    return out


def _network_env(env: dict[str, str]) -> None:
    """Point git, curl, go, npm and ssh at the relay; plain http has no route."""
    from server.code.app.index.gitrepo import add_git_config

    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY"):
        env[key] = RELAY_URL
    env["NO_PROXY"] = env["no_proxy"] = ""
    add_git_config(env, "http.proxy", RELAY_URL)
    proxy = f"{sys.executable} -I {_RELAY_INSIDE} connect %h %p {_EGRESS_INSIDE}/proxy.sock"
    env["GIT_SSH_COMMAND"] = (
        f"ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new "
        f"-o UserKnownHostsFile={_HOME_INSIDE}/known_hosts -o 'ProxyCommand={proxy}'"
    )


def _rlimits() -> list[tuple[str, int]]:
    """(prlimit option, value) for each job rlimit. Per process (CPU, data, file
    size) and per uid (processes): one job's processes are bounded each, not in
    total. Memory is RLIMIT_DATA, not RLIMIT_AS: node (scip-typescript,
    scip-python, npm) reserves far more address space than it uses."""
    mib = 1024 * 1024
    return [
        ("--cpu", settings.code_sandbox_cpu_seconds),
        ("--data", settings.code_sandbox_memory_mb * mib),
        ("--nproc", settings.code_sandbox_max_pids),
        ("--fsize", settings.code_sandbox_max_file_mb * mib),
    ]


def _prlimit_argv() -> list[str]:
    """`prlimit` (util-linux, in the image; the probe requires it) setting the
    rlimits before it execs bwrap; the job inherits them."""
    prlimit = shutil.which("prlimit") or "prlimit"
    return [prlimit, *(f"{option}={value}:{value}" for option, value in _rlimits()), "--"]


class JobOptions(TypedDict, total=False):
    """`run_job`'s options. `writable` and `readonly` are host paths mounted at
    the same place; they must exist. With `network` the job reaches public
    hosts on 443, 22 and `allow_remote_port` through the egress proxy. The
    environment is exactly `env` (inside the sandbox HOME and TMPDIR point into
    the job's scratch directory). `timeout_s` kills the tree (TimeoutError)."""

    env: Required[Mapping[str, str]]
    timeout_s: Required[float]
    writable: Sequence[Path]
    readonly: Sequence[Path]
    cwd: Path | None
    network: bool
    allow_remote_port: int | None
    stdin: bytes | None
    merge_stderr: bool


async def run_sandboxed(argv: Sequence[str], **job: Unpack[JobOptions]) -> JobResult:
    """Run `argv` inside the sandbox."""
    from server.code.app.index import egress

    network = job.get("network", False)
    outer = await asyncio.to_thread(tempfile.mkdtemp, prefix="loom-job-")
    scratch = os.path.join(outer, "tmp")
    egress_dir = os.path.join(outer, "egress") if network else ""
    job_env = {**job["env"], "HOME": _HOME_INSIDE, "TMPDIR": "/tmp"}
    proxy = None
    try:
        await asyncio.to_thread(os.mkdir, scratch, 0o700)
        if network:
            await asyncio.to_thread(os.mkdir, egress_dir, 0o700)
            proxy = await egress.start_proxy(
                f"{egress_dir}/proxy.sock", job.get("allow_remote_port")
            )
            _network_env(job_env)
            relay = [sys.executable, "-I", _RELAY_INSIDE, "serve", str(RELAY_PORT)]
            argv = [*relay, f"{_EGRESS_INSIDE}/proxy.sock", "--", *argv]
        readonly = _readonly_paths(job["env"], job.get("readonly", ()))
        binds = _binds(job.get("writable", ()), readonly, egress_dir)
        if (cwd := job.get("cwd")) is not None:
            binds += ["--chdir", str(cwd)]
        fd = _seccomp_fd()
        try:
            proc = await asyncio.create_subprocess_exec(
                *_prlimit_argv(),
                *_bwrap_argv(argv, scratch=scratch, binds=binds, fd=fd),
                env=job_env,
                pass_fds=(fd,),
                **_pipes(job),
            )
        finally:
            os.close(fd)
        return await _result(proc, job)
    finally:
        if proxy is not None:
            await proxy.close()
        await asyncio.to_thread(shutil.rmtree, outer, True)


def _pipes(job: JobOptions) -> dict[str, Any]:
    """The child's stdio, and its own session so a kill reaches its whole tree."""
    pipe = asyncio.subprocess.PIPE
    stdin = pipe if job.get("stdin") is not None else asyncio.subprocess.DEVNULL
    stderr = asyncio.subprocess.STDOUT if job.get("merge_stderr") else pipe
    return {"stdin": stdin, "stdout": pipe, "stderr": stderr, "start_new_session": True}


async def _result(proc: asyncio.subprocess.Process, job: JobOptions) -> JobResult:
    out, err = await communicate_or_kill(
        proc, job.get("stdin"), timeout_s=job["timeout_s"], group=True
    )
    return JobResult(proc.returncode or 0, out or b"", err or b"")


async def run_job(argv: Sequence[str], **job: Unpack[JobOptions]) -> JobResult:
    """A repository job: inside the sandbox when the host has one, else a plain
    child process with the same environment, timeout and process-group kill."""
    if sandbox_available():
        return await run_sandboxed(argv, **job)
    proc = await asyncio.create_subprocess_exec(
        *argv, env=dict(job["env"]), cwd=job.get("cwd"), **_pipes(job)
    )
    return await _result(proc, job)


__all__ = [
    "RELAY_URL",
    "JobOptions",
    "JobResult",
    "run_job",
    "run_sandboxed",
    "sandbox_available",
    "sandbox_state",
    "seccomp_filter",
    "tool_mounts",
]
