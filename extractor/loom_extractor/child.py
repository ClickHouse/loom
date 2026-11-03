"""One extraction in a process of its own: `python -P -m loom_extractor.child
<limits JSON>`, with the request as the first line of stdin (`{"format",
"name", "options"}`) and the source bytes after it. Before it reads stdin
the process sets no-new-privileges, makes itself non-dumpable, sets a seccomp
filter under which it makes no socket, and limits itself (address space, CPU
seconds, file size, no core dump, no new process); it writes the artifact
JSON to stdout and exits 0, or writes `{"error": {"code", "message"}}` and
exits 3.

The service starts it in `/` with only `PYTHONPATH`, `PATH` and `LC_ALL` in
its environment, so a parser does not see the service's token, and with the
request off its command line, so no other local user sees the file's name.
Linux only.
"""

from __future__ import annotations

import ctypes
import errno
import json
import logging
import platform
import resource
import struct
import sys
from typing import Any

from loom_extractor.source import ExtractionError, Options, extract

EXIT_REFUSED = 3
_PR_SET_DUMPABLE = 4
_PR_SET_NO_NEW_PRIVS = 38
_PR_SET_SECCOMP = 22
_SECCOMP_MODE_FILTER = 2


def _prctl(option: int, value: int, name: str) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(option, value, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), f"prctl({name}, {value}) failed")


def no_new_privileges() -> None:
    """No exec from this process or its children gains privileges (setuid, file
    capabilities). Kept across fork and exec."""
    _prctl(_PR_SET_NO_NEW_PRIVS, 1, "PR_SET_NO_NEW_PRIVS")


def no_dump() -> None:
    """Mark this process non-dumpable: a process of the same user can then not
    read its environment, memory or open files through /proc or ptrace. A
    child starts dumpable again after exec, so each one calls this itself."""
    _prctl(_PR_SET_DUMPABLE, 0, "PR_SET_DUMPABLE")


# The audit arch and the syscalls that make a socket (io_uring can too) on each
# architecture the image is built for; seccomp filters see raw syscall numbers.
_NO_SOCKETS = {
    "x86_64": (0xC000003E, (41, 53, 425)),  # socket, socketpair, io_uring_setup
    "aarch64": (0xC00000B7, (198, 199, 425)),
}
_X32_BIT = 0x40000000  # an x86_64 process can still call the x32 ABI's numbers


def _bpf(code: int, k: int, jt: int = 0, jf: int = 0) -> bytes:
    return struct.pack("=HBBI", code, jt, jf, k)


def no_sockets() -> None:
    """A seccomp filter on this process and all it starts: making a socket fails
    with EPERM, so an extraction a hostile source has taken over reaches no
    address, the Loom server on its network included. Needs no-new-privileges
    (set first) and no privilege. Another architecture refuses to extract."""
    machine = platform.machine()
    if machine not in _NO_SOCKETS:
        raise OSError(f"no socket filter for {machine}: the extractor runs on x86_64 or aarch64")
    arch, calls = _NO_SOCKETS[machine]
    ld_w_abs, jeq, jge, ret = 0x20, 0x15, 0x35, 0x06
    # Each check jumps to deny when it matches, else on to the next one.
    checks = [(jge, _X32_BIT)] if machine == "x86_64" else []
    checks += [(jeq, nr) for nr in calls]
    # [0 load arch] [1 arch?] [2 load nr] [3.. checks] [allow] [deny] [kill]
    allow = 3 + len(checks)
    deny, kill = allow + 1, allow + 2
    prog = [_bpf(ld_w_abs, 4), _bpf(jeq, arch, 0, kill - 2), _bpf(ld_w_abs, 0)]
    prog += [_bpf(op, k, deny - (3 + i) - 1, 0) for i, (op, k) in enumerate(checks)]
    prog += [
        _bpf(ret, 0x7FFF0000),  # SECCOMP_RET_ALLOW
        _bpf(ret, 0x00050000 | errno.EPERM),  # SECCOMP_RET_ERRNO
        _bpf(ret, 0x80000000),  # SECCOMP_RET_KILL_PROCESS
    ]
    filters = ctypes.create_string_buffer(b"".join(prog))
    fprog = struct.pack("@HP", len(prog), ctypes.addressof(filters))  # struct sock_fprog
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_SECCOMP, _SECCOMP_MODE_FILTER, ctypes.c_char_p(fprog), 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_SECCOMP) failed")


def _limit(memory_bytes: int, cpu_s: int) -> None:
    resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s + 1))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    # No process or thread of its own (the kernel holds every user to it but root and
    # CAP_SYS_RESOURCE or CAP_SYS_ADMIN): nothing a parser starts outlives its extraction.
    resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))


def _refuse(code: str, message: str) -> int:
    sys.stdout.buffer.write(json.dumps({"error": {"code": code, "message": message}}).encode())
    return EXIT_REFUSED


def main(argv: list[str]) -> int:
    limits = json.loads(argv[1])
    no_new_privileges()
    no_dump()
    no_sockets()
    _limit(limits["memory_bytes"], limits["cpu_s"])
    # sqlglot warns once per statement it falls back on; coverage records those.
    logging.getLogger("sqlglot").setLevel(logging.ERROR)
    req: dict[str, Any] = json.loads(sys.stdin.buffer.readline())
    data = sys.stdin.buffer.read()
    opts = req["options"]
    try:
        artifact = extract(
            data,
            req["name"],
            Options(
                format=req["format"],
                encoding=opts.get("encoding", ""),
                sql_dialect=opts.get("sql_dialect", ""),
            ),
        )
        out = artifact.to_json()
    except ExtractionError as e:
        return _refuse(e.code, str(e))
    except MemoryError:
        return _refuse("resource_limit", "the extraction ran out of its memory limit")
    except ImportError:
        raise  # a parser's dependency missing from this install: the service's fault
    except Exception as e:  # noqa: BLE001 — a parser's defect is this source's failure, and repeats
        return _refuse("corrupt", f"the parser failed on this file: {type(e).__name__}")
    sys.stdout.buffer.write(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
