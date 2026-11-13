"""The one isolation boundary for repository jobs (clone, fetch, submodules,
dependency installs, compiler-tier harnesses): they run what a repository ships.
`run_job` runs each inside bubblewrap when the host supports it, else as a plain
child process; `sandbox_available()` says which. What the sandbox mounts, limits
and lets out is in docs/operations/code-index-operations.md (repository job sandbox).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import platform
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Required, TypedDict, Unpack

from server.code.domain.index.state import SANDBOX_REQUIRED_REFUSAL
from server.core import readiness
from server.settings import settings

logger = logging.getLogger("loom.code_index.sandbox")

# Inside the sandbox: the relay listens here and forwards to the egress proxy.
RELAY_PORT = 3128
RELAY_URL = f"http://127.0.0.1:{RELAY_PORT}"
_RELAY_SCRIPT = Path(__file__).with_name("egress_relay.py")
_RELAY_INSIDE = "/run/loom-relay.py"
_EGRESS_INSIDE = "/run/loom-egress"
_HOME_INSIDE = "/tmp/home"

# Output the parent holds: a `keep_stdout` job's stdout whole up to this (a big
# repository's `ls-remote` of PR heads is ~6 MB), past it the job is killed; any other
# stream keeps only its last _TAIL_BYTES. The child's rlimits do not bound the parent.
_STDOUT_LIMIT = 64 * 1024 * 1024  # not a setting: the parent's memory bound against a hostile job
_TAIL_BYTES = 1024 * 1024
_READ_CHUNK = 64 * 1024
# How long a killed job's pipes may stay open. Outside the sandbox a process that
# left the job's session (setsid) can hold them for as long as it runs.
_REAP_GRACE_S = 5.0

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
    """Whether repository jobs run inside the sandbox on this host (bwrap and
    prlimit installed, a seccomp table for the architecture, a trivial job runs).
    Probed once per process."""
    global _available
    if _available is None:
        _available = _probe()
        if not _available and settings.code_sandbox_required:
            logger.warning(
                "repository jobs are REFUSED: LOOM_CODE_SANDBOX_REQUIRED is set and the "
                "sandbox cannot run here (sandbox probe failed)"
            )
        elif not _available:
            logger.warning(
                "repository jobs run UNSANDBOXED: the sandbox cannot run here (sandbox "
                "probe failed); build-based tiers execute repository code on this host"
            )
    return _available


def sandbox_state() -> bool | None:
    """The probe's answer if this process has probed, else None (no probe here:
    `/v1/health` must not spawn processes)."""
    return _available


# What `/v1/health` and `/v1/health/ready` (no token) say of a layout refusal, which
# names a host path; `loom doctor` and the log name the path.
LAYOUT_REFUSED = "a sandbox mount is inside Loom's data (`loom doctor` names it)"


def readiness_check() -> readiness.CheckResult:
    """With LOOM_CODE_SANDBOX_REQUIRED, ready once the probe found a sandbox."""
    if not settings.code_sandbox_required:
        return readiness.CheckResult(True)
    if layout_refusal():
        return readiness.CheckResult(False, LAYOUT_REFUSED)
    if _available is None:
        return readiness.CheckResult(False, "sandbox probe not finished")
    return readiness.result(_available, "LOOM_CODE_SANDBOX_REQUIRED and no sandbox here")


def _probe() -> bool:
    global _disable_userns
    bwrap = shutil.which("bwrap")
    missing = [name for name in ("bwrap", "prlimit") if not shutil.which(name)]
    if not bwrap or missing or platform.machine() not in _AUDIT_ARCH:
        if missing:
            logger.warning("sandbox probe failed: %s not on PATH", " and ".join(missing))
        else:
            logger.warning("sandbox probe failed: no seccomp table for %s", platform.machine())
        return False
    try:
        help_text = subprocess.run(
            [bwrap, "--help"],
            capture_output=True,
            text=True,
            timeout=10,  # not a setting: a local binary's help
        ).stdout
        _disable_userns = "--disable-userns" in help_text
        done = _probe_job()
        if _disable_userns and b"max_user_namespaces" in done.stderr:
            # --disable-userns writes user.max_user_namespaces, which an AppArmor
            # profile may refuse (loom-repo-sandbox's /proc/sys rules do). It is
            # a second lock only: the seccomp filter already refuses the job new
            # user namespaces.
            logger.info(
                "bwrap --disable-userns refused here (%s); jobs run without it, the "
                "seccomp filter refusing new user namespaces",
                done.stderr.decode(errors="replace").strip()[-300:],
            )
            _disable_userns = False
            done = _probe_job()
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("sandbox probe failed: %s", e)
        return False
    if done.returncode != 0:
        logger.warning("sandbox probe failed: %s", done.stderr.decode(errors="replace")[-300:])
    return done.returncode == 0


def _probe_job() -> subprocess.CompletedProcess[bytes]:
    scratch = tempfile.mkdtemp(prefix="loom-probe-")
    try:
        fd = _seccomp_fd()
        try:
            argv = _bwrap_argv(["true"], scratch=scratch, binds=[], fd=fd)
            return subprocess.run(
                argv,
                pass_fds=(fd,),
                capture_output=True,
                timeout=10,  # not a setting: a probe that runs `true`
                env={"PATH": os.defpath},
            )
        finally:
            os.close(fd)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _seccomp_fd() -> int:
    """A file descriptor holding the seccomp program, for `--seccomp FD`."""
    if sys.platform != "linux":  # memfd_create is Linux-only; bwrap is too
        raise OSError("no memfd_create on this platform")
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
    # RLIMIT_NPROC is set here, inside bwrap's user namespace, where the kernel
    # counts only the job's own tasks; outside it would count every task of the
    # uid on the host. prlimit is under /usr, which the sandbox mounts.
    nproc = settings.code_sandbox_max_pids
    prlimit = shutil.which("prlimit") or "prlimit"
    return [*args, "--", prlimit, f"--nproc={nproc}:{nproc}", "--", *argv]


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
    checkouts and the env file."""
    from server.core.env_file import env_file_path

    named = [settings.code_repos_root, settings.code_scip_checkout_dir]
    return [Path(p).absolute() for p in [*named, env_file_path() or ""] if p]


def _exposes_data(path: Path) -> bool:
    """Whether mounting `path` as a toolchain could show a job Loom's data: it
    is or holds a data path, var/, the install root or HOME, or it sits inside
    a data path. A directory may not sit in var/ or under HOME either (a file,
    such as var/bin/scip-clang, may)."""
    from server.core.paths import REPO_ROOT

    data = _data_paths()
    no_dirs_under = _var_and_home()
    for p in {path.absolute(), path.resolve()}:
        if any(q.is_relative_to(p) for q in [*data, *no_dirs_under, REPO_ROOT]):
            return True
        if any(p.is_relative_to(q) for q in data):
            return True
        if p.is_dir() and any(p.is_relative_to(q) for q in no_dirs_under):
            return True
    return False


def _readonly_paths(env: Mapping[str, str], readonly: Sequence[Path]) -> list[Path]:
    """The caller's read-only paths, the server's python (harness converters and
    the relay run it) and the toolchains on the job's PATH (absolute entries
    only, through `tool_mounts`). Never the server's HOME or a directory above it."""
    home = Path.home()
    found = [Path(p) for p in env.get("PATH", "").split(":") if p.startswith("/")]
    from_path = [mount for p in found for mount in tool_mounts(p)]
    _refuse_in_data(_own_mounts())
    paths = [*readonly, *_python_mounts(), *from_path]
    out: list[Path] = []
    for p in paths:
        p = p.absolute()
        holds_home = home.is_relative_to(p)
        system = any(p.is_relative_to(d) for d in _SYSTEM_PATHS)
        if not (holds_home or system or p in out) and p.exists():
            out.append(p)
    return out


def _python_mounts() -> list[Path]:
    """The server's python (harness converters and the relay run it)."""
    return [Path(sys.prefix), Path(sys.base_prefix)]


def _own_mounts() -> list[Path]:
    """The mounts Loom adds on its own: the server's python and the system paths."""
    return [*_python_mounts(), *map(Path, _SYSTEM_PATHS)]


def _refuse_in_data(paths: Sequence[Path]) -> None:
    """A mount Loom adds on its own (the server's python, the system paths) that
    is or sits inside a data path would show that data, and no empty mount over
    it can help: such a host layout runs no sandboxed job. The job's own checkout
    and mirror sit there by design and are not passed here."""
    data = [q for d in _data_paths() for q in {d, d.resolve()}]
    for path in paths:
        for p in {path.absolute(), path.resolve()}:
            if any(p.is_relative_to(q) for q in data):
                raise PermissionError(
                    f"sandbox: {path} is inside Loom's data; refusing to mount it"
                )


def layout_refusal() -> str:
    """Why this host's layout runs no sandboxed job ('' when it may): path checks
    only, so `/v1/health` can ask."""
    try:
        _refuse_in_data(_own_mounts())
    except PermissionError as e:
        return str(e)
    return ""


def _binds(writable: Sequence[Path], readonly: Sequence[Path], egress_dir: str) -> list[str]:
    """Parents before children (bwrap mounts in order, and a later mount hides
    what it covers): a read-only parent cannot hide a writable checkout, and a
    read-only path inside a writable one (a checkout's .git) stays read-only.
    At equal depth a read-only bind comes last and wins. Loom's data under any
    mounted directory is covered by an empty mount (`_hidden`), placed by the
    same order: after the bind that shows it, before the job's own paths inside it."""
    sources = [(os.path.abspath(p), 1, "--bind") for p in writable]
    sources += [(os.path.abspath(p), 2, "--ro-bind") for p in readonly]
    mounts = [(dest, rank, [flag, dest, dest]) for dest, rank, flag in sources]
    system = [p for p in _SYSTEM_PATHS if not os.path.islink(p) and os.path.exists(p)]
    hidden = _hidden([*system, *(dest for dest, _, _ in sources)])
    mounts += [(dest, 0, args) for dest, args in hidden]
    mounts.sort(key=lambda mount: (len(Path(mount[0]).parts), mount[1]))
    out = [arg for _, _, args in mounts for arg in args]
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


def _var_and_home() -> list[Path]:
    from server.core.paths import REPO_ROOT

    return [Path("var").absolute(), REPO_ROOT / "var", Path.home().absolute()]


def _protected_paths() -> list[Path]:
    """What no job may read wherever it sits: Loom's data, var/ and the server's HOME.
    The install root is not covered: the server's python may run from it."""
    return [*_data_paths(), *_var_and_home()]


def _hidden(sources: Sequence[str]) -> list[tuple[str, list[str]]]:
    """(path, bwrap args) covering each protected path that lies strictly inside a
    mounted source (blobs inside a python prefix, var/ under /usr): an empty tmpfs
    over a directory, /dev/null over a file. A source that is itself inside a
    protected path (the job's checkout or mirror) is the job's own and stays."""
    out: dict[str, list[str]] = {}
    for protected in _protected_paths():
        for source in sources:
            dest = _inside(protected, Path(source))
            if dest is None or dest in out:
                continue
            if os.path.isdir(dest):
                out[dest] = ["--tmpfs", dest]
            elif os.path.exists(dest):
                out[dest] = ["--ro-bind", "/dev/null", dest]
    return list(out.items())


def _inside(protected: Path, source: Path) -> str | None:
    """Where `protected` shows inside the sandbox through a bind of `source`
    (mounted at its own path), if it lies strictly under it; symlinks either side."""
    for p in {protected.absolute(), protected.resolve()}:
        for s in {source.absolute(), source.resolve()}:
            if p != s and p.is_relative_to(s):
                return str(source.absolute() / p.relative_to(s))
    return None


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
    """(prlimit option, value) for each job rlimit. Memory is RLIMIT_DATA, not
    RLIMIT_AS: node (scip-typescript, scip-python, npm) reserves far more address
    space than it uses."""
    mib = 1024 * 1024
    return [
        ("--cpu", settings.code_sandbox_cpu_seconds),
        ("--data", settings.code_sandbox_memory_mb * mib),
        ("--fsize", settings.code_sandbox_max_file_mb * mib),
    ]


def _prlimit_argv() -> list[str]:
    """`prlimit` setting the rlimits before it execs bwrap; the job inherits them
    (all but RLIMIT_NPROC, which `_bwrap_argv` sets inside the sandbox)."""
    prlimit = shutil.which("prlimit") or "prlimit"
    return [prlimit, *(f"{option}={value}:{value}" for option, value in _rlimits()), "--"]


class JobOptions(TypedDict, total=False):
    """`run_job`'s options. `writable` and `readonly` are host paths mounted at
    the same place; they must exist. With `network` the job reaches public
    hosts on 443, 22 and `allow_remote_port` through the egress proxy. The
    environment is exactly `env` (inside the sandbox HOME and TMPDIR point into
    the job's scratch directory). `timeout_s` kills the tree (TimeoutError).
    `keep_stdout`: the caller parses stdout (ls-remote), so it is kept whole up to
    _STDOUT_LIMIT and the job killed past it; otherwise only its tail is kept."""

    env: Required[Mapping[str, str]]
    timeout_s: Required[float]
    writable: Sequence[Path]
    readonly: Sequence[Path]
    cwd: Path | None
    network: bool
    allow_remote_port: int | None
    stdin: bytes | None
    merge_stderr: bool
    keep_stdout: bool


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
    """Wait for the job with its output bounded. On the timeout, a cancellation or
    an error the whole process group is killed and reaped."""
    try:
        out, err = await asyncio.wait_for(_talk_and_wait(proc, job), timeout=job["timeout_s"])
    except BaseException:
        await _kill_group(proc)
        raise
    if out is None:
        logger.warning("repository job killed: stdout passed %d bytes", _STDOUT_LIMIT)
        reason = f"loom: job killed, its stdout passed {_STDOUT_LIMIT} bytes\n"
        return JobResult(-signal.SIGKILL, b"", err + reason.encode())
    return JobResult(proc.returncode or 0, out, err)


async def _talk_and_wait(
    proc: asyncio.subprocess.Process, job: JobOptions
) -> tuple[bytes | None, bytes]:
    """Output to EOF, then the exit: a job that closes its pipes and keeps
    running is still inside the deadline."""
    done = await _talk(proc, job.get("stdin"), keep_stdout=job.get("keep_stdout", False))
    await proc.wait()
    return done


async def _talk(
    proc: asyncio.subprocess.Process, stdin: bytes | None, *, keep_stdout: bool
) -> tuple[bytes | None, bytes]:
    """Feed stdin and read stdout and stderr to EOF, each into a bounded buffer.
    stdout is None when it passed _STDOUT_LIMIT (the job was killed then)."""
    feed = None
    if stdin is not None and proc.stdin is not None:
        feed = asyncio.create_task(_feed(proc.stdin, stdin))
    errs = asyncio.create_task(_read(proc, proc.stderr, keep_all=False)) if proc.stderr else None
    tasks = [task for task in (feed, errs) if task is not None]
    try:
        assert proc.stdout is not None
        out = await _read(proc, proc.stdout, keep_all=keep_stdout)
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        # a reader still pending would race `_kill_group`'s drain of the same pipe
        await asyncio.gather(*tasks, return_exceptions=True)
    return out, (errs.result() or b"") if errs else b""


async def _feed(pipe: asyncio.StreamWriter, data: bytes) -> None:
    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
        pipe.write(data)
        await pipe.drain()
    pipe.close()


async def _read(
    proc: asyncio.subprocess.Process, stream: asyncio.StreamReader, *, keep_all: bool
) -> bytes | None:
    """A stream to EOF: with `keep_all` all of it, else only its last _TAIL_BYTES.
    A `keep_all` stream past _STDOUT_LIMIT kills the job and gives None; the rest
    is read and dropped, as asyncio's wait() waits for the pipes to close."""
    buf = bytearray()
    overflowed = False
    while chunk := await stream.read(_READ_CHUNK):
        if overflowed:
            continue
        buf += chunk
        if keep_all and len(buf) > _STDOUT_LIMIT:
            overflowed = True
            buf.clear()
            _signal_group(proc)
        elif not keep_all and len(buf) > _TAIL_BYTES:
            del buf[:-_TAIL_BYTES]
    return None if overflowed else bytes(buf)


def _signal_group(proc: asyncio.subprocess.Process) -> None:
    """SIGKILL the job's process group (it runs in its own session)."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()


async def _kill_group(proc: asyncio.subprocess.Process) -> None:
    """Kill the job and reap it. Its pipes are drained first: a reader that
    stopped mid-flood leaves the pipe paused, and wait() needs it closed. Pipes
    still open after _REAP_GRACE_S are closed on our side."""
    _signal_group(proc)
    try:
        await asyncio.wait_for(_drain_and_wait(proc), _REAP_GRACE_S)
    except TimeoutError:
        logger.warning("repository job %d: a process outside its group holds its output", proc.pid)
        # asyncio has no public way to drop a child's pipes; close() is what wait() waits on
        proc._transport.close()  # type: ignore[attr-defined]
        await proc.wait()


async def _drain_and_wait(proc: asyncio.subprocess.Process) -> None:
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            while await stream.read(_READ_CHUNK):
                pass
    await proc.wait()


class SandboxRequiredError(RuntimeError):
    """A repository job refused: LOOM_CODE_SANDBOX_REQUIRED is set and this host
    cannot run the sandbox."""


async def run_job(argv: Sequence[str], **job: Unpack[JobOptions]) -> JobResult:
    """A repository job: inside the sandbox when the host has one, else a plain
    child process with the same environment, timeout and process-group kill,
    unless LOOM_CODE_SANDBOX_REQUIRED says to refuse instead. Without the
    sandbox a process that left the job's session (setsid) is not killed, and
    while it holds the job's output the job ends as a timeout."""
    if sandbox_available():
        return await run_sandboxed(argv, **job)
    if settings.code_sandbox_required:
        raise SandboxRequiredError(SANDBOX_REQUIRED_REFUSAL)
    proc = await asyncio.create_subprocess_exec(
        *argv, env=dict(job["env"]), cwd=job.get("cwd"), **_pipes(job)
    )
    return await _result(proc, job)


__all__ = [
    "RELAY_URL",
    "JobOptions",
    "JobResult",
    "SandboxRequiredError",
    "layout_refusal",
    "run_job",
    "run_sandboxed",
    "sandbox_available",
    "sandbox_state",
    "seccomp_filter",
    "tool_mounts",
]
