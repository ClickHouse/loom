"""Native diagnostics preserve signals, private transport and process ownership."""

from __future__ import annotations

import hashlib
import json
import os
import resource
import runpy
import select
import signal
import stat
import subprocess
import sys
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest

from tests import _native_worker as native


def _mock_core_pattern(monkeypatch, value):
    original = Path.read_text

    def read_text(path, *args, **kwargs):
        if path == Path("/proc/sys/kernel/core_pattern"):
            if isinstance(value, OSError):
                raise value
            return value
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)


@pytest.fixture
def session(tmp_path, monkeypatch):
    _mock_core_pattern(monkeypatch, "core.%p\n")
    monkeypatch.setattr(resource, "getrlimit", lambda _: (0, 0))
    monkeypatch.setattr(native.shutil, "which", lambda _: "/synthetic/gdb")
    monkeypatch.delenv(native.CONTEXT, raising=False)
    return native.NativeSession(tmp_path)


def test_context_uses_bounded_offset_independent_anonymous_fd(monkeypatch):
    descriptor = os.memfd_create("native-context-test", os.MFD_CLOEXEC)
    try:
        data = b'{"arguments":["private-context-canary"]}'
        os.write(descriptor, data)
        monkeypatch.setenv(native.CONTEXT, str(descriptor))
        assert native.read_context() == {"arguments": ["private-context-canary"]}
        assert os.lseek(descriptor, 0, os.SEEK_CUR) == len(data)
        os.ftruncate(descriptor, native.MAX_CONTEXT_BYTES + 1)
        with pytest.raises(ValueError, match="size"):
            native.read_context()
    finally:
        os.close(descriptor)


def test_records_are_private_bounded_and_never_overwrite(tmp_path):
    path = tmp_path / "record.json"
    native.write_record(path, {"kind": "exit", "code": 139})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        native.write_record(path, {"kind": "signal", "signal": 11})
    assert native.read_record(path) == {"kind": "exit", "code": 139}
    with pytest.raises(ValueError, match="too large"):
        native.write_record(tmp_path / "large.json", {"value": "x" * native.MAX_BYTES})
    assert not (tmp_path / "large.json").exists()
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(OSError):
        native.read_record(link)
    path.chmod(0o644)
    with pytest.raises(ValueError, match="unsafe"):
        native.read_record(path)
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("reason", ["core", "debugger", "environment"])
def test_unavailable_capture_refuses_before_creating_session(tmp_path, monkeypatch, reason):
    _mock_core_pattern(monkeypatch, "core.%p\n")
    monkeypatch.setattr(resource, "getrlimit", lambda _: (1 if reason == "core" else 0, 1))
    monkeypatch.setattr(native.shutil, "which", lambda _: None if reason == "debugger" else "gdb")
    monkeypatch.delenv(native.CONTEXT, raising=False)
    if reason == "environment":
        monkeypatch.setenv(native.CONTEXT, "occupied")
    monkeypatch.setattr(
        subprocess, "Popen", lambda *a, **k: pytest.fail("refusal must not start a fallback")
    )
    with pytest.raises(ValueError):
        native.NativeSession(tmp_path)
    assert not (tmp_path / "native").exists()


@pytest.mark.parametrize(
    "pattern",
    ["core.%p\n", "|/usr/bin/core-collector %p\n", PermissionError("core pattern unreadable")],
)
def test_core_dump_policy_is_checked_before_creating_session(tmp_path, monkeypatch, pattern):
    _mock_core_pattern(monkeypatch, pattern)
    monkeypatch.setattr(resource, "getrlimit", lambda _: (0, 0))
    monkeypatch.setattr(native.shutil, "which", lambda _: "/synthetic/gdb")
    monkeypatch.delenv(native.CONTEXT, raising=False)
    if isinstance(pattern, OSError):
        with pytest.raises(OSError) as refused:
            native.NativeSession(tmp_path)
        assert refused.value is pattern
        assert not (tmp_path / "native").exists()
    elif pattern.startswith("|"):
        with pytest.raises(ValueError, match="piped core dumps"):
            native.NativeSession(tmp_path)
        assert not (tmp_path / "native").exists()
    else:
        session = native.NativeSession(tmp_path)
        assert session.root.is_dir()


def test_worker_configuration_rejects_unsupported_specs_without_partial_changes(session):
    valid = SimpleNamespace(popen=True, via=None, python=None)
    invalid = SimpleNamespace(popen=True, via=None, python="custom-python")
    with pytest.raises(ValueError):
        session.configure([valid, invalid])
    assert valid.python is None
    with pytest.raises(ValueError, match="limit"):
        session.configure([valid] * (native.MAX_WORKERS + 1))
    assert valid.python is None


@pytest.mark.parametrize("state", ["registered", "refused", "parent_changed"])
def test_parent_death_registration_never_grants_access_or_signals_another_process(
    monkeypatch, state
):
    calls = []

    class Prctl:
        def __call__(self, *arguments):
            calls.append(arguments)
            return -1 if state == "refused" else 0

    monkeypatch.setattr(native.ctypes, "CDLL", lambda *a, **k: SimpleNamespace(prctl=Prctl()))
    monkeypatch.setattr(native.ctypes, "get_errno", lambda: 1)
    monkeypatch.setattr(os, "getppid", lambda: 123 if state != "parent_changed" else 321)
    monkeypatch.setattr(os, "getpid", lambda: 456)
    signals = []
    monkeypatch.setattr(os, "kill", lambda *arguments: signals.append(arguments))
    if state == "refused":
        with pytest.raises(OSError) as refused:
            native.parent_death_signal(123)
        assert refused.value.errno == 1
    else:
        native.parent_death_signal(123)
    assert calls == [(1, signal.SIGKILL, 0, 0, 0)]
    assert signals == ([(456, signal.SIGKILL)] if state == "parent_changed" else [])


@pytest.mark.parametrize("code", [0, 7, 139])
def test_launcher_preserves_normal_exit_codes_and_keeps_arguments_private(
    session, monkeypatch, code
):
    canary = "private-argument-canary"

    class Debugger:
        def __init__(self, command, **kwargs):
            assert kwargs["stdin"] is subprocess.PIPE
            assert kwargs["stdout"] is subprocess.DEVNULL
            assert kwargs["stderr"] is subprocess.DEVNULL
            assert canary not in command
            descriptor = int(kwargs["env"][native.CONTEXT])
            assert descriptor in kwargs["pass_fds"]
            context = json.loads(os.pread(descriptor, os.fstat(descriptor).st_size, 0))
            assert context["arguments"] == ["-c", "raise SystemExit(139)", canary]
            self.directory = Path(context["directory"])

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def wait(self):
            native.write_record(self.directory / "result.json", {"kind": "exit", "code": code})
            return 0

    monkeypatch.setattr(subprocess, "Popen", Debugger)
    assert native.launch(session.root, ["-c", "raise SystemExit(139)", canary]) == code
    assert all(canary not in path.read_text() for path in session.root.rglob("*.json"))


def test_exhausted_launch_slots_never_start_an_unobserved_worker(session, monkeypatch):
    for slot in range(native.MAX_WORKERS):
        native.write_record(session.root / f"slot-{slot}.json", {"pid": slot + 2})
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("slot exhaustion"))
    assert native.launch(session.root, ["-c", "raise SystemExit(0)"]) == 125
    assert len(list(session.root.glob("slot-*.json"))) == native.MAX_WORKERS


@pytest.mark.parametrize("start_offset", [0, 1])
def test_forged_same_uid_records_cannot_authorize_cleanup(session, monkeypatch, start_offset):
    launcher = {"pid": 100001, "ppid": 2, "start": 100, "uid": os.getuid()}
    debugger = {**launcher, "pid": 100002, "ppid": launcher["pid"], "start": 101}
    inferior = {**launcher, "pid": 100003, "ppid": debugger["pid"], "start": 102}
    native.write_record(session.root / "slot-0.json", launcher)
    directory = session.root / str(launcher["pid"])
    directory.mkdir(mode=0o700)
    native.write_record(directory / "debugger.json", debugger)
    native.write_record(directory / "inferior.json", inferior)
    owners = {debugger["pid"]: debugger, inferior["pid"]: inferior}
    monkeypatch.setattr(
        native,
        "identity",
        lambda pid: {**owners[pid], "start": owners[pid]["start"] + start_offset, "zombie": 0},
    )
    monkeypatch.setattr(
        os, "pidfd_open", lambda *args: pytest.fail("disk records cannot authorize cleanup")
    )
    monkeypatch.setattr(
        os, "kill", lambda *args: pytest.fail("disk records cannot authorize signals")
    )
    monkeypatch.setattr(
        signal,
        "pidfd_send_signal",
        lambda *args: pytest.fail("disk records cannot authorize signals"),
    )
    result = session.finish()
    assert result["remaining"] == (2 if start_offset == 0 else 0)
    assert session.finish() == {"event": "native_cleanup", "already_finished": True}


class FakeGDB:
    VERSION = "synthetic-gdb"
    TYPE_CODE_VOID = 0

    class error(Exception):
        pass

    class SignalEvent:
        def __init__(self, received):
            self.stop_signal = received

    class Value:
        def __init__(self, value):
            self.value = value
            self.type = SimpleNamespace(code=0 if value is None else 1)

        def __int__(self):
            return self.value

    def __init__(self):
        self.commands, self.value_reads = [], []
        self.pending = deque()
        self.stop_handlers, self.exit_handlers = [], []
        self.events = SimpleNamespace(
            stop=SimpleNamespace(connect=self.stop_handlers.append),
            exited=SimpleNamespace(connect=self.exit_handlers.append),
        )
        self.inferior = SimpleNamespace(pid=1)
        self.on_command = {}
        self.exit_code = self.exit_signal = None

    def post_event(self, callback):
        self.pending.append(callback)

    def execute(self, command, **kwargs):
        self.commands.append(command)
        action = self.on_command.get(command)
        if isinstance(action, Exception):
            raise action
        if action is not None:
            action()
        return ""

    def selected_inferior(self):
        return self.inferior

    def selected_thread(self):
        raise self.error("private-unwind-canary")

    def parse_and_eval(self, expression):
        self.value_reads.append(expression)
        return self.Value(
            {"$_exitcode": self.exit_code, "$_exitsignal": self.exit_signal}[expression]
        )

    def stop(self, received="SIGSEGV"):
        for callback in self.stop_handlers:
            callback(self.SignalEvent(received))

    def exit(self, *, code=None, received=None):
        self.inferior.pid = 0
        for callback in self.exit_handlers:
            callback(SimpleNamespace(inferior=self.inferior))
        # GDB updates terminal convenience variables after the native callback returns.
        self.exit_code, self.exit_signal = code, received

    def drain(self):
        while self.pending:
            self.pending.popleft()()


@pytest.fixture
def debugger(session, monkeypatch):
    fake = FakeGDB()
    directory = session.root / "synthetic-debugger"
    directory.mkdir(mode=0o700)
    native.write_record(directory / "inferior.json", {"pid": 1})
    monkeypatch.setattr(native, "read_context", lambda: {"directory": str(directory)})
    monkeypatch.setitem(sys.modules, "gdb", fake)
    monkeypatch.setitem(sys.modules, "_native_worker", native)
    monkeypatch.syspath_prepend(str(Path(native.__file__).parent))
    module = runpy.run_path(str(Path(native.__file__).with_name("_native_gdb.py")))
    return SimpleNamespace(api=fake, collector=module["collector"], directory=directory)


def test_debugger_permission_refusal_is_reported_without_fallback(debugger):
    fake = debugger.api
    fake.on_command["run &"] = fake.error("ptrace: Operation not permitted private-error-canary")
    fake.drain()
    assert fake.commands[-2:] == ["run &", "quit"]
    assert fake.value_reads == []
    result = native.read_record(debugger.directory / "result.json")
    assert result == {
        "kind": "unavailable",
        "reason": "trace_refused",
        "observed_fatal_stops": 0,
        "captured_stops": 0,
        "trap_stops": 0,
        "gdb_version": "synthetic-gdb",
    }
    assert all(
        "private-error-canary" not in path.read_text() for path in debugger.directory.iterdir()
    )


@pytest.mark.parametrize("failure", ["unwind", "write"])
@pytest.mark.parametrize("exit_code,exit_signal", [(None, 11), (139, None)])
def test_collector_failure_still_continues_and_preserves_terminal_event(
    debugger, monkeypatch, failure, exit_code, exit_signal
):
    fake = debugger.api
    fake.on_command["run &"] = fake.stop
    fake.on_command["continue &"] = lambda: fake.exit(code=exit_code, received=exit_signal)
    original_write = native.write_record

    def write(path, record):
        if failure == "write" and path.name.startswith("signature-"):
            raise OSError("private-write-canary")
        original_write(path, record)

    monkeypatch.setattr(native, "write_record", write)
    fake.drain()
    result = native.read_record(debugger.directory / "result.json")
    if exit_signal is not None:
        assert result["kind"] == "signal" and result["signal"] == exit_signal
    else:
        assert result["kind"] == "exit" and result["code"] == exit_code
    assert result["observed_fatal_stops"] == 1
    assert "handle SIGSEGV stop print pass" in fake.commands
    assert fake.commands[-3:] == ["run &", "continue &", "quit"]
    assert all("canary" not in path.read_text() for path in debugger.directory.iterdir())


def test_queued_resume_is_ignored_after_native_exit_event(debugger):
    fake = debugger.api
    fake.drain()  # Dispatch startup, then deliver a stop followed by the native exit event.
    fake.stop()
    fake.exit(received=11)
    fake.drain()
    assert "continue &" not in fake.commands
    assert fake.commands[-1] == "quit"
    assert debugger.collector.terminal_seen is True
    result = native.read_record(debugger.directory / "result.json")
    assert result["kind"] == "signal" and result["signal"] == 11
    fake.stop()
    fake.drain()
    assert fake.commands.count("quit") == 1
    assert "continue &" not in fake.commands


@pytest.mark.parametrize("failure", ["gdb", "value"])
def test_failed_terminal_variable_read_stays_unknown_and_quits(debugger, monkeypatch, failure):
    fake = debugger.api
    fake.drain()

    def unavailable(expression):
        error = fake.error if failure == "gdb" else ValueError
        raise error("private-terminal-read-canary")

    monkeypatch.setattr(fake, "parse_and_eval", unavailable)
    fake.exit(received=11)
    fake.drain()
    result = native.read_record(debugger.directory / "result.json")
    assert result["kind"] == "unavailable"
    assert result["reason"] == "terminal_status_unknown"
    assert "signal" not in result and "code" not in result
    assert debugger.collector.finalized is True
    assert fake.commands[-1] == "quit"
    assert "continue &" not in fake.commands
    assert all(
        "private-terminal-read-canary" not in path.read_text()
        for path in debugger.directory.iterdir()
    )


def test_other_inferior_exit_event_does_not_finalize_worker(debugger):
    fake = debugger.api
    fake.drain()
    for callback in fake.exit_handlers:
        callback(SimpleNamespace(inferior=SimpleNamespace(pid=2)))
    assert not fake.pending
    assert fake.value_reads == []
    assert debugger.collector.terminal_seen is False
    assert not (debugger.directory / "result.json").exists()


def test_register_esrch_without_exit_event_is_incomplete_not_a_terminal_signal(debugger):
    fake = debugger.api
    fake.on_command["run &"] = fake.stop
    fake.on_command["continue &"] = fake.error(
        "Could not read registers: No such process private-error-canary"
    )
    fake.drain()
    assert fake.commands[-2:] == ["run &", "continue &"]
    assert fake.commands.count("continue &") == 1
    assert not fake.pending
    assert fake.value_reads == []
    assert debugger.collector.finalized is False
    assert debugger.collector.terminal_seen is False
    assert not (debugger.directory / "result.json").exists()
    assert native.read_record(debugger.directory / "awaiting-native-exit.json") == {
        "kind": "incomplete",
        "reason": "register_esrch",
    }
    assert all(
        "private-error-canary" not in path.read_text() for path in debugger.directory.iterdir()
    )


def _mechanism_start(root, source, arguments=(), *, environment=None):
    root.mkdir(mode=0o700)
    session = native.NativeSession(root)
    program = root / "inferior.py"
    program.write_text(source)
    process = subprocess.Popen(
        [
            sys.executable,
            str(Path(native.__file__).resolve()),
            "--launch",
            str(session.root),
            str(program),
            *arguments,
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
    )
    return session, process


def _mechanism_case(root, source, arguments=(), *, payload=b"", environment=None):
    session, process = _mechanism_start(root, source, arguments, environment=environment)
    try:
        output, error = process.communicate(payload, timeout=20)
        records = [native.read_record(path) for path in session.root.rglob("*.json")]
        return process.returncode, output, error, records
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        session.finish()


def _mechanism_parent_death(root):
    session, process = _mechanism_start(
        root, "import os, signal\nos.write(1, b'ready\\n')\nsignal.pause()\n"
    )
    descriptors = []
    try:
        assert select.select([process.stdout], [], [], 20)[0], "worker did not start"
        assert process.stdout.readline() == b"ready\n"
        directory = session.root / str(process.pid)
        debugger = native.read_record(directory / "debugger.json")
        inferior = native.read_record(directory / "inferior.json")
        assert debugger["ppid"] == process.pid
        assert inferior["ppid"] == debugger["pid"]
        for expected in (debugger, inferior):
            descriptor = os.pidfd_open(expected["pid"])
            descriptors.append(descriptor)
            found = native.identity(expected["pid"])
            assert all(found[key] == expected[key] for key in ("pid", "start", "uid"))
        process.kill()  # Only our direct Popen; the kernel owns both descendant edges.
        assert process.wait(timeout=5) == -signal.SIGKILL
        for descriptor in descriptors:
            observer = select.poll()
            observer.register(descriptor, select.POLLIN)
            assert observer.poll(5000), "owned descendant survived launcher death"
        process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        for descriptor in descriptors:
            os.close(descriptor)
        session.finish()


def _mechanism_xdist(root):
    repository = str(Path(native.__file__).resolve().parents[1])
    environment = {
        **os.environ,
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTEST_ADDOPTS": "",
        "PYTHONPATH": repository,
    }
    for crash in (False, True):
        outcomes = []
        for enabled in (False, True):
            directory = root / f"xdist-{crash}-{enabled}"
            directory.mkdir(mode=0o700)
            (directory / "pytest.ini").write_text("[pytest]\n")
            source = (
                "import os, signal\ndef test_crash(): os.kill(os.getpid(), signal.SIGSEGV)\n"
                if crash
                else "def test_pass(): assert 1 == 1\n"
                "def test_assertion(): assert False, 'original assertion'\n"
                "def test_exception(): raise ValueError('original exception')\n"
            )
            (directory / "test_case.py").write_text(source)
            command = [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-p",
                "xdist.plugin",
                "-n",
                "1",
                "--max-worker-restart=0",
                "--junitxml=result.xml",
            ]
            if enabled:
                command += ["-p", "tests._worker_status", "--native-worker-signatures"]
            process = subprocess.run(
                command, cwd=directory, env=environment, capture_output=True, timeout=25
            )
            assert process.returncode == 1, (
                crash,
                enabled,
                process.returncode,
                process.stdout,
                process.stderr,
            )
            cases = ElementTree.parse(directory / "result.xml").findall(".//testcase")
            outcomes.append(
                sorted((case.get("name"), case.find("failure") is not None) for case in cases)
            )
            if enabled:
                files = list((directory / ".pytest_cache/worker-status").glob("*/status.jsonl"))
                assert len(files) == 1
                records = [json.loads(line) for line in files[0].read_text().splitlines()]
                statuses = [record for record in records if record["event"] == "worker_status"]
                after = next(record for record in statuses if record["stage"] == "after_teardown")
                assert after["os_returncode"] == (-11 if crash else 0)
                observations = [record for record in records if record["event"] == "native_worker"]
                terminal = next(
                    record["result"] for record in reversed(observations) if "result" in record
                )
                assert terminal["kind"] == ("signal" if crash else "exit")
                assert terminal["signal" if crash else "code"] == (11 if crash else 0)
                if crash:
                    assert any(
                        record.get("signature", {}).get("signal") == "SIGSEGV"
                        for record in observations
                    )
        assert outcomes[0] == outcomes[1], outcomes


def mechanism_checks(root):
    """Run explicitly under GDB; absence or refusal fails instead of skipping."""
    canary = "private-argv-and-environment-canary"
    environment = {**os.environ, "NATIVE_TEST_CANARY": canary}
    for code in (0, 7, 139):
        result, _, error, records = _mechanism_case(
            root / f"exit-{code}", f"raise SystemExit({code})\n"
        )
        assert result == code, (result, error, records)
        terminals = [record for record in records if record.get("kind") == "exit"]
        assert len(terminals) == 1 and terminals[0]["code"] == code
        assert terminals[0]["observed_fatal_stops"] == 0
    result, _, error, records = _mechanism_case(
        root / "caught-then-native-fault",
        "import ctypes, os, signal\n"
        "signal.signal(signal.SIGSEGV, lambda *_: None)\n"
        "os.kill(os.getpid(), signal.SIGSEGV)\n"
        "signal.signal(signal.SIGSEGV, signal.SIG_DFL)\n"
        "ctypes.string_at(0)\n",
        (canary,),
        environment=environment,
    )
    assert result == -signal.SIGSEGV, (result, error, records)
    signatures = sorted(
        (record for record in records if record.get("signal") == "SIGSEGV"),
        key=lambda record: record["observed_stop"],
    )
    assert [record["observed_stop"] for record in signatures] == [1, 2]
    assert signatures[0]["si_code"] <= 0 < signatures[1]["si_code"]
    assert signatures[1]["si_addr"] == 0
    assert canary not in json.dumps(records)
    result, _, error, records = _mechanism_case(
        root / "bounded-stops",
        "import os, signal\n"
        "signal.signal(signal.SIGSEGV, lambda *_: None)\n"
        f"for _ in range({native.MAX_STOPS + 2}): os.kill(os.getpid(), signal.SIGSEGV)\n"
        "raise SystemExit(7)\n",
    )
    assert result == 7, (result, error, records)
    assert sum(record.get("signal") == "SIGSEGV" for record in records) == native.MAX_STOPS
    terminal = next(record for record in records if record.get("kind") == "exit")
    assert terminal["observed_fatal_stops"] == native.MAX_STOPS + 2
    assert terminal["captured_stops"] == native.MAX_STOPS
    payload = b"wire\x00\xff"
    large_environment = {
        **environment,
        "NATIVE_LARGE_A": "private-large-a-" + "a" * (70 * 1024),
        "NATIVE_LARGE_B": "private-large-b-" + "b" * (70 * 1024),
        "NATIVE_NON_UTF8": os.fsdecode(b"\xff"),
    }
    for label, transport_environment in (
        ("transport", environment),
        ("large-environment", large_environment),
    ):
        environment_hash = hashlib.sha256(
            json.dumps(transport_environment, sort_keys=True).encode()
        ).hexdigest()
        result, output, error, records = _mechanism_case(
            root / label,
            "import hashlib, json, os, sys\n"
            f"assert hashlib.sha256(json.dumps(dict(os.environ), sort_keys=True).encode()).hexdigest() == {environment_hash!r}, 'worker environment mismatch'\n"
            f"assert sys.argv[1] == {canary!r}\n"
            "assert os.environ['NATIVE_TEST_CANARY'] == sys.argv[1]\n"
            f"assert {native.CONTEXT!r} not in os.environ\n"
            "os.write(1, os.read(0, 6))\n",
            (canary,),
            payload=payload,
            environment=transport_environment,
        )
        assert (result, output, error) == (0, payload, b""), (label, result, output, error, records)
        serialized = json.dumps(records)
        assert all(
            value not in serialized for value in (canary, "private-large-a-", "private-large-b-")
        )
    _mechanism_parent_death(root / "parent-death")
    _mechanism_xdist(root)
    for received in (signal.SIGSEGV, signal.SIGTRAP):
        for caught in (False, True):
            source = "import os, signal\n"
            if caught:
                source += f"signal.signal({int(received)}, lambda *_: None)\n"
            source += f"os.kill(os.getpid(), {int(received)})\nraise SystemExit(139)\n"
            result, _, error, records = _mechanism_case(
                root / f"signal-{received}-{caught}", source, (canary,), environment=environment
            )
            assert result == (139 if caught else -received), (
                int(received),
                caught,
                result,
                error,
                records,
            )
            terminals = [record for record in records if "kind" in record]
            assert len(terminals) == 1
            expected = (
                {"kind": "exit", "code": 139} if caught else {"kind": "signal", "signal": received}
            )
            assert all(terminals[0].get(key) == value for key, value in expected.items())
            assert canary not in json.dumps(records)
            if received == signal.SIGSEGV:
                signatures = [record for record in records if record.get("signal") == "SIGSEGV"]
                assert len(signatures) == 1 and signatures[0]["threads"], records
    print("native mechanism cases passed")


if __name__ == "__main__":
    import tempfile

    if sys.argv[1:] != ["--mechanism"]:
        raise SystemExit("expected --mechanism")
    with tempfile.TemporaryDirectory(prefix="native-worker-mechanism-") as temporary:
        mechanism_checks(Path(temporary))
