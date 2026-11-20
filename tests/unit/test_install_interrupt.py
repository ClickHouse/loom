"""Installer signal handling at child-registration boundaries, without an install."""

from __future__ import annotations

import os
import re
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def _function(source: str, name: str) -> str:
    match = re.search(rf"^  {name}\(\) \{{\n.*?^  \}}\n", source, re.M | re.S)
    assert match is not None, name
    return match.group()


def _identity(pid: int) -> tuple[int, int, str] | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        return Path(f"/proc/{pid}").stat().st_uid, int(fields[1]), fields[19]
    except (FileNotFoundError, ProcessLookupError):
        if Path("/proc").exists():
            return None
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "uid=,ppid=,lstart="],
        capture_output=True,
        text=True,
        check=False,
    )
    parts = result.stdout.strip().split(maxsplit=2)
    return (int(parts[0]), int(parts[1]), parts[2]) if len(parts) == 3 else None


class _OwnedProcess:
    def __init__(self, pid: int, parent: int) -> None:
        self.pid = pid
        identity = _identity(pid)
        assert identity is not None and identity[:2] == (os.getuid(), parent)
        self.identity = identity
        self.fd = os.pidfd_open(pid) if hasattr(os, "pidfd_open") else None
        assert _identity(pid) == self.identity

    def running(self) -> bool:
        identity = _identity(self.pid)
        # Reparenting is expected after the interrupted installer exits.
        return identity is not None and (identity[0], identity[2]) == (
            self.identity[0],
            self.identity[2],
        )

    def send(self, sig: int) -> None:
        if not self.running():
            return
        try:
            if self.fd is not None:
                signal.pidfd_send_signal(self.fd, sig)
            else:
                os.kill(self.pid, sig)
        except ProcessLookupError:
            pass

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)


def _await_file(path: Path, process: subprocess.Popen[bytes]) -> int:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if path.exists() and (value := path.read_text().strip()):
            return int(value)
        assert process.poll() is None, f"controller exited before {path.name}"
        time.sleep(0.01)
    raise AssertionError(f"controller did not reach {path.name}")


def _script(source: str, phase: str) -> str:
    bounded = _function(source, "bounded")
    if phase == "command":
        needle = "    cmd_pid=$!\n"
        injection = '    printf "%s\\n" "$!" > "$TRACE/target.pid"\n'
        injection += '    while [ ! -f "$TRACE/go" ]; do sleep 0.01; done\n'
        injection += '    kill -INT "$$"\n'
    elif phase == "watchdog":
        needle = "    dog_pid=$!\n"
        injection = '    printf "%s\\n" "$!" > "$TRACE/target.pid"\n'
        injection += '    while [ ! -f "$TRACE/go" ]; do sleep 0.01; done\n'
        injection += '    kill -INT "$$"\n'
    else:
        needle = "      nap=$!\n"
        injection = '      printf "%s\\n" "$!" > "$TRACE/target.pid"\n'
        injection += '      while [ ! -f "$TRACE/go" ]; do sleep 0.01; done\n'
        injection += '      kill -TERM "$(cat "$TRACE/watchdog.pid")"\n'
    assert bounded.count(needle) == 1
    bounded = bounded.replace(needle, injection + needle)
    bounded = bounded.replace(
        "    dog_pid=$!\n",
        '    dog_pid=$!\n    printf "%s\\n" "$dog_pid" > "$TRACE/watchdog.pid"\n',
    )
    bounded = bounded.replace(
        "      nap=$!\n", '      nap=$!\n      printf "%s\\n" "$nap" > "$TRACE/nap.pid"\n'
    )
    return (
        "set -eu\ncmd_pid=\ndog_pid=\n"
        + _function(source, "on_interrupt")
        + "trap on_interrupt HUP INT TERM\n"
        + bounded
        + 'bounded 30 sh "$TRACE/agent.sh"\n'
    )


class TestInstallerInterrupt(unittest.TestCase):
    def _check_interrupt(self, phase: str) -> None:
        source = (ROOT / "install.sh").read_text()
        with tempfile.TemporaryDirectory(prefix="installer-interrupt-") as directory:
            trace = Path(directory)
            (trace / "agent.sh").write_text(
                'trap "" HUP INT\nprintf "%s\\n" "$$" > "$TRACE/agent.pid"\nexec sleep 30\n'
            )
            script = trace / "probe.sh"
            script.write_text(_script(source, phase))
            log = trace / "output"
            observed: list[_OwnedProcess] = []
            with log.open("wb") as output:
                process = subprocess.Popen(
                    ["sh", str(script)],
                    env={"PATH": "/usr/bin:/bin", "TRACE": str(trace)},
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                try:
                    controller = _OwnedProcess(process.pid, os.getpid())
                    observed.append(controller)
                    target_pid = _await_file(trace / "target.pid", process)
                    parent = process.pid
                    if phase == "sleep":
                        parent = _await_file(trace / "watchdog.pid", process)
                        observed.append(_OwnedProcess(parent, process.pid))
                    target = _OwnedProcess(target_pid, parent)
                    observed.append(target)
                    if phase != "command":
                        agent_pid = _await_file(trace / "agent.pid", process)
                        observed.append(_OwnedProcess(agent_pid, process.pid))
                    if phase == "watchdog":
                        nap_pid = _await_file(trace / "nap.pid", process)
                        observed.append(_OwnedProcess(nap_pid, target_pid))
                    (trace / "go").touch()
                    if phase != "sleep":
                        self.assertEqual(process.wait(timeout=3), 130, log.read_text())
                    deadline = time.monotonic() + 2
                    while target.running() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertFalse(target.running(), f"{phase} child survives interruption")
                finally:
                    if observed:
                        observed[0].send(signal.SIGINT)
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        if observed:
                            observed[0].send(signal.SIGKILL)
                        process.wait(timeout=3)
                    for child in reversed(observed[1:]):
                        child.send(signal.SIGKILL)
                    for child in observed:
                        child.close()

    def test_interrupt_before_command_pid_is_registered(self) -> None:
        self._check_interrupt("command")

    def test_interrupt_before_watchdog_pid_is_registered(self) -> None:
        self._check_interrupt("watchdog")

    def test_stop_before_watchdog_sleep_pid_is_registered(self) -> None:
        self._check_interrupt("sleep")

    def test_bounded_keeps_the_command_exit_status(self) -> None:
        source = (ROOT / "install.sh").read_text()
        script = "set -eu\ncmd_pid=\ndog_pid=\n" + _function(source, "bounded")
        script += "status=0\nbounded 1 sh -c 'exit 7' || status=$?\nexit \"$status\"\n"
        result = subprocess.run(["sh", "-c", script], capture_output=True, timeout=3, check=False)
        self.assertEqual(result.returncode, 7, result.stderr)


class TestProcessIdentity(unittest.TestCase):
    def test_process_exit_during_stat_read_is_absent(self) -> None:
        with (
            patch.object(Path, "read_text", side_effect=ProcessLookupError),
            patch.object(Path, "exists", return_value=True),
        ):
            self.assertIsNone(_identity(os.getpid()))

    def test_permission_error_is_not_treated_as_process_exit(self) -> None:
        with (
            patch.object(Path, "read_text", side_effect=PermissionError),
            self.assertRaises(PermissionError),
        ):
            _identity(os.getpid())
