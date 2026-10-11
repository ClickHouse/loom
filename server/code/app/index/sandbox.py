"""The one isolation boundary for repository jobs.

Clone, fetch, `git submodule update`, dependency installs and the compiler-tier
harnesses read a repository's files and run what it ships (a CMakeLists.txt is a
program). `run_job` runs each of them inside bubblewrap when the host supports it:

- its own user, pid, network, mount, ipc and uts namespaces, no capabilities, no
  new user namespaces, and a seccomp filter that refuses the syscalls a job never
  needs (`seccomp_filter`);
- only the system directories, the toolchains and the paths the caller names are
  mounted; the caller's work directories are writable, everything else read-only.
  /tmp and HOME are a fresh directory removed after the job. No server
  environment, credentials, host HOME or Docker socket;
- rlimits on CPU, memory, processes and file size, and a wall-clock timeout that
  kills the whole tree: when bwrap dies its pid namespace dies with it, so no
  process the job started survives it;
- no network unless the caller asks for it, and then the only way out is the
  egress proxy (`egress.py`), which allows public addresses only.

Without a usable bwrap the job runs as a plain child process, as it always has;
`sandbox_available()` says which, and the callers decide what may run then.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import platform
import resource
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

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
_MACHINES = ("x86_64", "aarch64")
_AUDIT_ARCH = {"x86_64": 0xC000003E, "aarch64": 0xC00000B7}
_X32_SYSCALL_BIT = 0x40000000
_BPF_LD_W_ABS = 0x20
_BPF_JEQ = 0x15
_BPF_JGE = 0x35
_BPF_RET = 0x06
SECCOMP_RET_KILL_PROCESS = 0x80000000
SECCOMP_RET_ERRNO_EPERM = 0x00050000 | 1
SECCOMP_RET_ALLOW = 0x7FFF0000


@dataclass(frozen=True)
class JobResult:
    returncode: int
    stdout: bytes
    stderr: bytes


def seccomp_filter(machine: str = "") -> bytes:
    """The classic-BPF program bwrap installs with `--seccomp`: kill a process
    calling with another architecture's numbers (and x32 ones on x86_64, which
    would bypass the list), EPERM for each denied syscall, allow the rest."""
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
    prog.append((_BPF_RET, 0, 0, SECCOMP_RET_ALLOW))
    return b"".join(struct.pack("=HBBI", *ins) for ins in prog)


_available: bool | None = None
_disable_userns = False


def sandbox_available() -> bool:
    """Whether repository jobs run inside the sandbox on this host: bwrap is
    installed, the architecture has a seccomp table, and a trivial job runs.
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
    if not bwrap or platform.machine() not in _AUDIT_ARCH:
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


def tool_root(path: Path) -> Path:
    """The directory to mount for a tool found in `path`: a `bin` directory's
    parent (node's lib, go's pkg sit beside it), `path` itself otherwise."""
    return path.parent if path.name in ("bin", ".bin") else path


def _readonly_paths(env: Mapping[str, str], readonly: Sequence[Path]) -> list[Path]:
    """The caller's read-only paths (absolute), the server's python (harness converters and
    the relay run it) and the toolchains on the job's PATH — except anything
    under the server's HOME, which may hold credentials."""
    home = Path.home()
    found = [Path(p) for p in env.get("PATH", "").split(":") if p]
    from_path = [tool_root(p) for p in found if p != home and not p.is_relative_to(home)]
    paths = [*readonly, Path(sys.prefix), Path(sys.base_prefix), *from_path]
    out: list[Path] = []
    for p in paths:
        p = p.absolute()
        system = any(p.is_relative_to(d) for d in _SYSTEM_PATHS)
        covered = system or p in out or p == Path("/")
        if not covered and p.exists():
            out.append(p)
    return out


def _binds(writable: Sequence[Path], readonly: Sequence[Path], egress_dir: str) -> list[str]:
    """Writable binds first, so a read-only path inside one (a checkout's .git)
    stays read-only."""
    out: list[str] = []
    for p in map(os.path.abspath, writable):
        out += ["--bind", p, p]
    for p in map(os.path.abspath, readonly):
        out += ["--ro-bind", p, p]
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


def _limits() -> Callable[[], None]:
    """The rlimits the child sets before it execs bwrap; the job inherits them."""
    mib = 1024 * 1024
    limits = (
        (resource.RLIMIT_CPU, settings.code_sandbox_cpu_seconds),
        (resource.RLIMIT_AS, settings.code_sandbox_memory_mb * mib),
        (resource.RLIMIT_NPROC, settings.code_sandbox_max_pids),
        (resource.RLIMIT_FSIZE, settings.code_sandbox_max_file_mb * mib),
    )

    def apply() -> None:
        for kind, value in limits:
            resource.setrlimit(kind, (value, value))

    return apply


async def run_sandboxed(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    timeout_s: float,
    writable: Sequence[Path] = (),
    readonly: Sequence[Path] = (),
    cwd: Path | None = None,
    network: bool = False,
    allow_remote_port: int | None = None,
    stdin: bytes | None = None,
    merge_stderr: bool = False,
) -> JobResult:
    """Run `argv` inside the sandbox. `writable` and `readonly` are host paths
    mounted at the same place; they must exist. With `network` the job reaches
    public hosts on 443, 22 and `allow_remote_port` through the egress proxy.
    The environment is exactly `env` (HOME and TMPDIR point into the job's
    scratch directory). TimeoutError past `timeout_s`, the tree killed."""
    from server.code.app.index import egress

    outer = await asyncio.to_thread(tempfile.mkdtemp, prefix="loom-job-")
    scratch = os.path.join(outer, "tmp")
    egress_dir = os.path.join(outer, "egress") if network else ""
    job_env = {**env, "HOME": _HOME_INSIDE, "TMPDIR": "/tmp"}
    proxy = None
    try:
        await asyncio.to_thread(os.mkdir, scratch, 0o700)
        if network:
            await asyncio.to_thread(os.mkdir, egress_dir, 0o700)
            proxy = await egress.start_proxy(f"{egress_dir}/proxy.sock", allow_remote_port)
            _network_env(job_env)
            relay = [sys.executable, "-I", _RELAY_INSIDE, "serve", str(RELAY_PORT)]
            argv = [*relay, f"{_EGRESS_INSIDE}/proxy.sock", "--", *argv]
        binds = _binds(writable, _readonly_paths(env, readonly), egress_dir)
        if cwd is not None:
            binds += ["--chdir", str(cwd)]
        fd = _seccomp_fd()
        try:
            proc = await asyncio.create_subprocess_exec(
                *_bwrap_argv(argv, scratch=scratch, binds=binds, fd=fd),
                env=job_env,
                stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT if merge_stderr else asyncio.subprocess.PIPE,
                pass_fds=(fd,),
                start_new_session=True,
                preexec_fn=_limits(),
            )
        finally:
            os.close(fd)
        out, err = await _communicate_or_kill(proc, stdin, timeout_s)
        return JobResult(proc.returncode or 0, out, err)
    finally:
        if proxy is not None:
            await proxy.close()
        await asyncio.to_thread(shutil.rmtree, outer, True)


async def _run_plain(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    timeout_s: float,
    cwd: Path | None,
    stdin: bytes | None,
    merge_stderr: bool,
) -> JobResult:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        env=dict(env),
        cwd=cwd,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT if merge_stderr else asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    out, err = await _communicate_or_kill(proc, stdin, timeout_s)
    return JobResult(proc.returncode or 0, out, err)


async def _communicate_or_kill(
    proc: asyncio.subprocess.Process, stdin: bytes | None, timeout_s: float
) -> tuple[bytes, bytes]:
    """Output of a child started in its own session; on timeout, cancellation or
    any error its whole process group is killed and the child reaped."""
    try:
        talk = proc.communicate(stdin) if stdin is not None else proc.communicate()
        out, err = await asyncio.wait_for(talk, timeout=timeout_s)
    except BaseException:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        await proc.wait()
        raise
    return out or b"", err or b""


async def run_job(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    timeout_s: float,
    writable: Sequence[Path] = (),
    readonly: Sequence[Path] = (),
    cwd: Path | None = None,
    network: bool = False,
    allow_remote_port: int | None = None,
    stdin: bytes | None = None,
    merge_stderr: bool = False,
) -> JobResult:
    """A repository job: inside the sandbox when the host has one, else a plain
    child process with the same environment, timeout and process-group kill."""
    if sandbox_available():
        return await run_sandboxed(
            argv,
            env=env,
            timeout_s=timeout_s,
            writable=writable,
            readonly=readonly,
            cwd=cwd,
            network=network,
            allow_remote_port=allow_remote_port,
            stdin=stdin,
            merge_stderr=merge_stderr,
        )
    return await _run_plain(
        argv, env=env, timeout_s=timeout_s, cwd=cwd, stdin=stdin, merge_stderr=merge_stderr
    )


__all__ = [
    "RELAY_URL",
    "JobResult",
    "run_job",
    "run_sandboxed",
    "sandbox_available",
    "sandbox_state",
    "seccomp_filter",
    "tool_root",
]
