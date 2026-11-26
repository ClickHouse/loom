"""Opt-in ancestor debugger transport and exact owned-process cleanup for xdist."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

CONTEXT = "_PYTEST_NATIVE_CONTEXT"
MAX_WORKERS = 64
MAX_BYTES = 65536
MAX_CONTEXT_BYTES = 64 * 1024 * 1024
MAX_STOPS = 8
PIPE_REFUSAL = "native diagnostics refuse piped core dumps"


def pipe_refusal_context() -> dict[str, Any]:
    record: dict[str, Any] = {
        "event": "native_prerequisite_refused",
        "reason": "piped_core_handler",
    }
    try:
        system = os.uname()
        # Custom release suffixes can identify private builds; retain only their hash.
        if re.fullmatch(
            r"[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,5}(?:-[0-9]{1,6}-(?:generic|azure|aws))?",
            system.release,
        ):
            release = {"kernel_release": system.release}
        else:
            release = {"kernel_release_sha256": hashlib.sha256(system.release.encode()).hexdigest()}
        machine = system.machine if system.machine in ("x86_64", "aarch64") else "unknown"
        return {**record, **release, "machine": machine}
    except Exception:  # noqa: BLE001 — metadata must never mask the original privacy refusal
        return {**record, "kernel_identity": "unavailable"}


MAX_TERMINAL_BYTES = 2048


def write_terminal(descriptor: int, record: dict[str, Any]) -> None:
    data = json.dumps(record, ensure_ascii=True, separators=(",", ":")).encode()
    if len(data) > MAX_TERMINAL_BYTES:
        raise ValueError("native terminal record too large")
    if os.pwrite(descriptor, data, 0) != len(data):
        raise OSError("incomplete native terminal record")


def read_terminal(descriptor: int) -> dict[str, Any]:
    try:
        data = os.pread(descriptor, MAX_TERMINAL_BYTES + 1, 0)
        if len(data) <= MAX_TERMINAL_BYTES:
            record = json.loads(data)
            if isinstance(record, dict):
                return record
    except (OSError, ValueError, RecursionError):
        pass
    return {"kind": "unavailable", "reason": "terminal_status_unknown"}


def write_record(path: Path, record: dict[str, Any]) -> None:
    data = json.dumps(record, ensure_ascii=True, separators=(",", ":")).encode()
    if len(data) > MAX_BYTES:
        raise ValueError("native record too large")
    # A hard link publishes a complete record and refuses an existing destination.
    temporary = path.with_suffix(".tmp")
    created = False
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
        os.link(temporary, path)
    finally:
        if created:
            temporary.unlink(missing_ok=True)


def read_record(path: Path) -> dict[str, Any]:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_size > MAX_BYTES
        ):
            raise ValueError("unsafe native record")
        result = json.loads(source.read(MAX_BYTES + 1))
    if not isinstance(result, dict):
        raise ValueError("invalid native record")
    return result


def read_context() -> dict[str, Any]:
    descriptor = int(os.environ[CONTEXT])
    size = os.fstat(descriptor).st_size
    if not 0 < size <= MAX_CONTEXT_BYTES:
        raise ValueError("native context size is unsupported")
    result = json.loads(os.pread(descriptor, size, 0))
    if not isinstance(result, dict):
        raise ValueError("invalid native context")
    return result


def identity(pid: int) -> dict[str, int]:
    base = Path("/proc") / str(pid)
    fields = (base / "stat").read_text().rsplit(")", 1)[1].split()
    return {
        "pid": pid,
        "ppid": int(fields[1]),
        "start": int(fields[19]),
        "uid": base.stat().st_uid,
        "zombie": int(fields[0] == "Z"),
    }


def parent_death_signal(expected_parent: int) -> None:
    """Tie this helper's lifetime to its creator without granting tracing permissions."""
    libc = ctypes.CDLL(None, use_errno=True)
    prctl = libc.prctl
    prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    prctl.restype = ctypes.c_int
    # PR_SET_PDEATHSIG affects only this process; no tracing or access policy changes.
    if prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "native helper parent-death registration refused")
    if os.getppid() != expected_parent:
        os.kill(os.getpid(), signal.SIGKILL)


class NativeSession:
    def __init__(self, root: Path) -> None:
        if sys.platform != "linux":
            raise ValueError("native diagnostics require Linux")
        import resource

        if resource.getrlimit(resource.RLIMIT_CORE)[0] != 0:
            raise ValueError("native diagnostics require an existing zero core limit")
        # Piped kernel handlers receive raw dumps even when the core limit is zero.
        if Path("/proc/sys/kernel/core_pattern").read_text().startswith("|"):
            error = ValueError(PIPE_REFUSAL)
            error.add_note("[native-prerequisite] " + json.dumps(pipe_refusal_context()))
            raise error
        if CONTEXT in os.environ:
            raise ValueError("native diagnostic environment is already in use")
        debugger = shutil.which("gdb")
        if debugger is None:
            raise ValueError("native diagnostics require gdb")
        self.root = root / "native"
        self.root.mkdir(mode=0o700)
        self.finished = False
        write_record(self.root / "config.json", {"debugger": debugger})

    def configure(self, specs: list[Any], max_restarts: int | str | None = None) -> None:
        restarts = 4 * len(specs) if max_restarts is None else int(max_restarts)
        if restarts < 0 or len(specs) + restarts > MAX_WORKERS:
            raise ValueError("native diagnostic worker limit exceeded")
        for spec in specs:
            if not spec.popen or spec.via or spec.python:
                raise ValueError("native diagnostics require local default-interpreter workers")
        for spec in specs:
            spec.python = shlex.join(
                [sys.executable, str(Path(__file__).resolve()), "--launch", str(self.root)]
            )

    def observe(self, pid: int) -> dict[str, Any]:
        result: dict[str, Any] = {"transport": "ancestor_debugger_launcher"}
        directory = self.root / str(pid)
        try:
            result["inferior_pid"] = read_record(directory / "inferior.json")["pid"]
        except (OSError, ValueError, KeyError):
            pass
        for filename in ("result", *(f"signature-{i}" for i in range(1, MAX_STOPS + 1))):
            try:
                result["signature" if filename.startswith("signature-") else filename] = (
                    read_record(directory / f"{filename}.json")
                )
            except (OSError, ValueError):
                pass  # The launcher may still be running or may have been killed.
        if "signature" in result:
            signature = result["signature"]
            threads = signature.get("threads", [])
            result["signature"] = {
                key: signature[key]
                for key in ("observed_stop", "signal", "si_code", "si_addr", "capture")
                if key in signature
            }
            result["signature"]["faulting_thread"] = (
                {**threads[0], "frames": threads[0]["frames"][:3]} if threads else None
            )
        return result

    def finish(self) -> dict[str, Any]:
        if self.finished:
            return {"event": "native_cleanup", "already_finished": True}
        self.finished = True
        remaining = unavailable = 0
        reasons: dict[str, int] = {}
        # Read only bounded private registrations. They never authorize a signal.
        for slot in range(MAX_WORKERS):
            try:
                launcher = read_record(self.root / f"slot-{slot}.json")
                if type(launcher["pid"]) is not int or launcher["pid"] < 2:
                    raise ValueError("invalid launcher")
                directory = self.root / str(launcher["pid"])
            except FileNotFoundError:
                continue
            except (OSError, ValueError, KeyError, TypeError):
                unavailable += 1
                continue
            for filename in ("inferior", "debugger"):
                try:
                    expected = read_record(directory / f"{filename}.json")
                    found = identity(expected["pid"])
                    remaining += not found["zombie"] and all(
                        found[key] == expected[key] for key in ("pid", "start", "uid")
                    )
                except FileNotFoundError:
                    continue
                except (OSError, ValueError, KeyError, TypeError):
                    unavailable += 1
            try:
                reason = read_record(directory / "result.json").get("reason")
                if reason in (
                    "trace_refused",
                    "debugger_error",
                    "inferior_not_started",
                    "terminal_status_unknown",
                    "helper_refused",
                ):
                    reasons[reason] = reasons.get(reason, 0) + 1
            except (OSError, ValueError):
                unavailable += 1
        return {
            "event": "native_cleanup",
            "remaining": remaining,
            "unavailable": unavailable,
            "reasons": reasons,
        }


def bootstrap() -> None:
    context = read_context()
    os.close(int(os.environ.pop(CONTEXT)))
    os.close(context["terminal_fd"])
    directory = Path(context["directory"])
    try:
        parent_death_signal(read_record(directory / "debugger.json")["pid"])
    except OSError:
        write_record(directory / "result.json", {"kind": "unavailable", "reason": "helper_refused"})
        raise SystemExit(125) from None
    write_record(directory / "inferior.json", identity(os.getpid()))
    for target, saved in enumerate(context["stdio"]):
        os.dup2(saved, target)
        os.close(saved)
    # GDB may add terminal variables; restore exactly the original worker environment.
    os.environ.clear()
    os.environ.update(context["environment"])
    os.execv(context["python"], [context["python"], *context["arguments"]])


def launch(root: Path, arguments: list[str]) -> int:
    directory = root / str(os.getpid())
    directory.mkdir(mode=0o700)
    for slot in range(MAX_WORKERS):
        try:
            write_record(root / f"slot-{slot}.json", identity(os.getpid()))
            break
        except FileExistsError:
            continue
    else:
        return 125
    descriptors = [os.dup(fd) for fd in range(3)]
    terminal_fd = os.memfd_create("pytest-native-terminal", os.MFD_CLOEXEC)
    terminal_reader = os.dup(terminal_fd)
    descriptors.append(terminal_fd)
    context = {
        "directory": str(directory),
        "launcher_pid": os.getpid(),
        "terminal_fd": terminal_fd,
        "stdio": descriptors[:3],
        "python": sys.executable,
        "arguments": arguments,
        "environment": dict(os.environ),
    }
    data = json.dumps(context, ensure_ascii=True).encode()
    if len(data) > MAX_CONTEXT_BYTES:
        raise ValueError("native context size is unsupported")
    context_fd = os.memfd_create("pytest-native-context", os.MFD_CLOEXEC)
    descriptors.append(context_fd)
    with os.fdopen(os.dup(context_fd), "wb") as output:
        output.write(data)
    environment = {**os.environ, CONTEXT: str(context_fd)}
    debugger = read_record(root / "config.json")["debugger"]
    command = [
        debugger,
        "-q",
        "-nx",
        "-nh",
        "-iex",
        "set auto-load off",
        "-iex",
        "set debuginfod enabled off",
        "-ex",
        "set disable-randomization off",
        "-ex",
        "set startup-with-shell off",
        "-x",
        str(Path(__file__).with_name("_native_gdb.py")),
        "--args",
        sys.executable,
        str(Path(__file__).resolve()),
        "--bootstrap",
    ]
    try:
        with subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--debugger-bootstrap", *command],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            pass_fds=descriptors,
            env=environment,
        ) as process:
            for descriptor in descriptors:
                os.close(descriptor)
            descriptors.clear()
            process.wait()
        result = read_terminal(terminal_reader)
        if (
            result.get("kind") == "exit"
            and type(result.get("code")) is int
            and 0 <= result["code"] <= 255
        ):
            return result["code"]
        if (
            result.get("kind") == "signal"
            and type(result.get("signal")) is int
            and result["signal"] in signal.valid_signals()
            and result["signal"]
            not in (
                signal.SIGSTOP,
                signal.SIGTSTP,
                signal.SIGTTIN,
                signal.SIGTTOU,
                signal.SIGCONT,
                signal.SIGCHLD,
                signal.SIGURG,
                signal.SIGWINCH,
            )
        ):
            received = result["signal"]
            if received != signal.SIGKILL:
                signal.signal(received, signal.SIG_DFL)
            os.kill(os.getpid(), received)
        return 125
    finally:
        os.close(terminal_reader)
        for descriptor in descriptors:
            os.close(descriptor)


if __name__ == "__main__":
    if sys.argv[1] == "--bootstrap":
        bootstrap()
    elif sys.argv[1] == "--debugger-bootstrap":
        context = read_context()
        directory = Path(context["directory"])
        try:
            parent_death_signal(context["launcher_pid"])
            write_record(directory / "debugger.json", identity(os.getpid()))
            os.execv(sys.argv[2], sys.argv[2:])
        except OSError:
            write_record(
                directory / "result.json", {"kind": "unavailable", "reason": "helper_refused"}
            )
            raise SystemExit(125) from None
    else:
        raise SystemExit(launch(Path(sys.argv[2]), sys.argv[3:]))
