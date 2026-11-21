"""GDB entry point: retain native metadata, then deliver the original signal."""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import sys
from pathlib import Path
from typing import Any

import gdb

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _native_worker as native

MAX_THREADS = 8
MAX_FRAMES = 24
FATAL = {"SIGSEGV", "SIGBUS", "SIGILL", "SIGFPE", "SIGABRT"}


def integer(expression: str) -> int | None:
    value = gdb.parse_and_eval(expression)
    return None if value.type.code == gdb.TYPE_CODE_VOID else int(value)


def symbol_name(name: str | None) -> str | None:
    if name is None:
        return None
    if len(name) <= 160 and re.fullmatch(r"[A-Za-z_][A-Za-z_0-9.:<>~]*", name):
        return name
    return "sha256:" + hashlib.sha256(name.encode()).hexdigest()


def module_name(path: str) -> str:
    name = Path(path).name
    if re.fullmatch(
        r"(?:python[0-9.]*|lib(?:python|c|m|pthread|dl|gcc_s|stdc\+\+)[0-9.]*\.so[0-9.]*)", name
    ):
        return name
    return "sha256:" + hashlib.sha256(path.encode()).hexdigest()


def mappings() -> list[tuple[int, int, int, str]]:
    # Read a bounded mapping prefix privately; never retain paths or raw map text.
    with open(f"/proc/{gdb.selected_inferior().pid}/maps", "rb") as source:
        data = source.read(262144)
    result = []
    for line in data.decode(errors="replace").splitlines()[:-1]:
        columns = line.split(maxsplit=5)
        if len(columns) == 6:
            start, end = (int(value, 16) for value in columns[0].split("-"))
            result.append((start, end, int(columns[2], 16), columns[5]))
    return result


def frame_record(frame: Any, regions: list[tuple[int, int, int, str]]) -> dict[str, Any]:
    pc = int(frame.pc())
    result: dict[str, Any] = {"pc": pc, "symbol": symbol_name(frame.name())}
    for start, end, offset, filename in regions:
        if start <= pc < end:
            result["module"] = module_name(filename)
            result["file_offset"] = pc - start + offset
            for objfile in gdb.objfiles():
                if os.path.realpath(objfile.filename) == os.path.realpath(filename):
                    build_id = objfile.build_id
                    if build_id and re.fullmatch(r"[0-9a-fA-F]{1,128}", build_id):
                        result["build_id"] = build_id
                    break
            break
    try:
        block = gdb.block_for_pc(pc)
        while block is not None and block.function is None:
            block = block.superblock
        if block is not None:
            result["symbol_offset"] = pc - block.start
    except gdb.error:
        pass  # Stripped modules still have their PC and build identity when available.
    return result


def signature(received: str) -> dict[str, Any]:
    faulting = gdb.selected_thread()
    regions = mappings()
    result: dict[str, Any] = {"signal": received, "threads": [], "unwind_complete": True}
    for field, expression in (("si_code", "$_siginfo.si_code"),):
        try:
            result[field] = integer(expression)
        except gdb.error:
            result[field] = None
    if result["si_code"] is not None and result["si_code"] > 0:
        try:
            result["si_addr"] = integer("$_siginfo._sifields._sigfault.si_addr")
        except gdb.error:
            result["si_addr"] = None
    threads = [faulting, *(t for t in gdb.selected_inferior().threads() if t != faulting)]
    result["threads_truncated"] = len(threads) > MAX_THREADS
    try:
        for thread in threads[:MAX_THREADS]:
            thread.switch()
            entry: dict[str, Any] = {
                "faulting": thread == faulting,
                "lwp": thread.ptid[1],
                "frames": [],
            }
            result["threads"].append(entry)
            try:
                frame = gdb.newest_frame()
                for _ in range(MAX_FRAMES):
                    if frame is None:
                        break
                    entry["frames"].append(frame_record(frame, regions))
                    frame = frame.older()
                entry["frames_truncated"] = frame is not None
            except Exception:  # noqa: BLE001 — unwinding must never consume the original signal
                result["unwind_complete"] = False
    finally:
        faulting.switch()
    while len(json.dumps(result, ensure_ascii=True).encode()) > native.MAX_BYTES - 1024:
        # Preserve the faulting thread first when symbol names fill the byte budget.
        for entry in reversed(result["threads"]):
            if entry["frames"]:
                entry["frames"].pop()
                entry["frames_truncated"] = True
                break
    return result


class NativeDebugger:
    def __init__(self) -> None:
        self.directory = Path(native.read_context()["directory"])
        self.inferior = gdb.selected_inferior()
        self.stops = 0
        self.trap_stops = 0
        self.finalized = False
        self.terminal_seen = False
        gdb.events.stop.connect(self.on_stop)
        gdb.events.exited.connect(self.on_exit)
        # Native async execution leaves exit-event dispatch with GDB's main loop.
        gdb.post_event(lambda: self.execute("run &"))

    def finish(self, result: dict[str, Any]) -> None:
        if self.finalized:
            return
        self.finalized = True
        result.update(
            observed_fatal_stops=self.stops,
            captured_stops=min(self.stops, native.MAX_STOPS),
            trap_stops=self.trap_stops,
            gdb_version=gdb.VERSION[:64],
        )
        try:
            native.write_record(self.directory / "result.json", result)
        finally:
            gdb.post_event(lambda: gdb.execute("quit", to_string=False))

    def on_exit(self, event: Any) -> None:
        if event.inferior == self.inferior:
            self.terminal_seen = True
            # Convenience variables are final only after the native exit event completes.
            gdb.post_event(self.terminal_result)

    def terminal_result(self) -> None:
        try:
            exit_code, exit_signal = integer("$_exitcode"), integer("$_exitsignal")
        except (gdb.error, ValueError, TypeError):
            self.finish({"kind": "unavailable", "reason": "terminal_status_unknown"})
            return
        if not (self.directory / "inferior.json").exists():
            result = {"kind": "unavailable", "reason": "inferior_not_started"}
        elif exit_signal is not None:
            result = {"kind": "signal", "signal": exit_signal}
        elif exit_code is not None:
            result = {"kind": "exit", "code": exit_code}
        else:
            result = {"kind": "unavailable", "reason": "terminal_status_unknown"}
        self.finish(result)

    def execute(self, command: str) -> None:
        if self.finalized or self.terminal_seen:
            return
        try:
            gdb.execute(command, to_string=False)
        except gdb.error as error:
            detail = str(error).lower()
            if self.stops and "register" in detail and "no such process" in detail:
                # A dead thread can precede the queued native process-exit event.
                # This marker proves no terminal outcome; never retry or infer a signal.
                try:
                    native.write_record(
                        self.directory / "awaiting-native-exit.json",
                        {"kind": "incomplete", "reason": "register_esrch"},
                    )
                except OSError:
                    pass
                return
            refused = any(
                token in detail for token in ("ptrace", "not permitted", "permission denied")
            )
            self.finish(
                {"kind": "unavailable", "reason": "trace_refused" if refused else "debugger_error"}
            )

    def on_stop(self, event: Any) -> None:
        if self.finalized or self.terminal_seen:
            return
        trap = isinstance(event, gdb.SignalEvent) and event.stop_signal == "SIGTRAP"
        if not isinstance(event, gdb.SignalEvent):
            # Some targets report a user SIGTRAP as StopEvent rather than SignalEvent.
            try:
                trap = integer("$_siginfo.si_signo") == signal.SIGTRAP
            except gdb.error:
                pass
        if trap:
            self.trap_stops += 1
        try:
            if isinstance(event, gdb.SignalEvent) and event.stop_signal in FATAL:
                self.capture(event.stop_signal)
        finally:
            command = "signal SIGTRAP" if trap else "continue &"
            gdb.post_event(lambda: self.execute(command))

    def capture(self, received: str) -> None:
        self.stops += 1
        if self.stops > native.MAX_STOPS:
            return
        path = self.directory / f"signature-{self.stops}.json"
        try:
            native.write_record(path, {"observed_stop": self.stops, **signature(received)})
        except Exception:  # noqa: BLE001 — a collector failure must still deliver the signal
            try:
                native.write_record(path, {"observed_stop": self.stops, "capture": "unavailable"})
            except OSError:
                pass


def install() -> NativeDebugger:
    # Preserve ASLR and delivered signals, including GDB's special defaults.
    for command in (
        "set pagination off",
        "set confirm off",
        "set follow-fork-mode parent",
        "set detach-on-fork on",
        "set follow-exec-mode same",
        "handle all nostop noprint pass",
        "handle SIGINT nostop noprint pass",
        # Internal exec traps stay with GDB; on_stop delivers actual inferior SIGTRAPs.
        "handle SIGTRAP stop print nopass",
        *(f"handle {name} stop print pass" for name in sorted(FATAL)),
    ):
        gdb.execute(command, to_string=True)
    return NativeDebugger()


collector = install()
