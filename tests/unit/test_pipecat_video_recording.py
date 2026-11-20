"""Recorder control flow uses mocks; these tests start no product or encoder."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import runpy
import signal
from pathlib import Path
from unittest import mock
from urllib.error import URLError

import pytest

RECORDER = Path(__file__).resolve().parents[2] / "dev/pipecat_demo/record_pipecat_video.py"


@pytest.fixture
def recorder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    spec = importlib.util.spec_from_file_location("pipecat_video_recorder", RECORDER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = tmp_path / "frozen.txt"
    source.write_bytes(b"frozen source")
    module.FROZEN = {str(source): hashlib.sha256(source.read_bytes()).hexdigest()}
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("LOOM_PIPECAT_EVIDENCE", str(tmp_path / "receipt.json"))
    monkeypatch.setattr(module, "subreaper", mock.Mock(return_value=0))
    # Ordinary main tests must not inspect or signal the pytest worker's children.
    monkeypatch.setattr(
        module,
        "stop_adopted_children",
        mock.Mock(wraps=module.stop_adopted_children, return_value=None),
    )
    old_umask = os.umask(0o077)
    try:
        yield module, source
    finally:
        os.umask(old_umask)


@pytest.mark.parametrize("failure", ["hosted", "source", "receipt"])
def test_admission_refuses_before_work(recorder, monkeypatch, failure):
    module, source = recorder
    if failure == "hosted":
        monkeypatch.delenv("GITHUB_ACTIONS")
    elif failure == "source":
        source.write_text("changed")
    else:
        Path(os.environ["LOOM_PIPECAT_EVIDENCE"]).write_text("exists")
    with (
        mock.patch.object(module, "start_capture") as capture,
        mock.patch.object(module, "run_native") as native,
    ):
        assert module.main() == 2
    capture.assert_not_called()
    native.assert_not_called()


@pytest.mark.parametrize("code", [0, 7])
def test_capture_failure_preserves_native_result(recorder, code):
    module, _ = recorder
    with (
        mock.patch.object(module, "start_capture", side_effect=ValueError("capture unavailable")),
        mock.patch.object(module, "run_native", return_value=code) as native,
    ):
        assert module.main() == code
    native.assert_called_once()


@pytest.mark.parametrize("launch_error", [False, True])
def test_launch_defers_cancellation_until_child_registration(recorder, launch_error):
    module, _ = recorder
    state = {}
    child = mock.Mock(pid=os.getpid())

    def launch():
        assert state["registering"]
        state["cancelled_signal"] = signal.SIGTERM
        if launch_error:
            raise OSError("launch failed")
        return child

    with (
        mock.patch.object(module.subprocess, "Popen", side_effect=launch),
        mock.patch.object(module, "process_identity", return_value=(0, 0, 0, "birth", "S")),
    ):
        with pytest.raises(module.RecordingCancelled):
            module.start_owned(state, "native")
    assert state["registering"] is False
    assert state.get("native") is (None if launch_error else child)


def test_display_exports_only_allowlisted_status(recorder):
    module, _ = recorder
    assert module.allowed_stdout(b"1 passed in 4.93s") == "1 passed in 4.93s"
    assert module.allowed_stdout(b"authorization: private fixture value") is None
    assert module.allowed_stdout(b"OBSERVED arbitrary unvalidated value") is None


def test_occupied_server_refuses_before_capture_or_native(recorder, monkeypatch):
    module, _ = recorder
    monkeypatch.setattr(module.sys, "argv", ["recorder", "--with-server"])
    with (
        mock.patch.object(module, "subreaper", return_value=0),
        mock.patch.object(module.socket, "socket") as sockets,
        mock.patch.object(module.subprocess, "Popen") as launch,
        mock.patch.object(module, "start_capture") as capture,
        mock.patch.object(module, "run_native") as native,
    ):
        sockets.return_value.__enter__.return_value.bind.side_effect = OSError(98, "occupied")
        assert module.main() == 2
    sockets.return_value.__enter__.return_value.bind.assert_called_once_with(("127.0.0.1", 7778))
    launch.assert_not_called()
    capture.assert_not_called()
    native.assert_not_called()


def test_exited_server_refuses_before_health_probe(recorder, tmp_path):
    module, _ = recorder
    with (
        mock.patch.object(module, "refuse_occupied_server_port"),
        mock.patch.object(module, "start_owned", return_value=object()),
        mock.patch.object(
            module, "owned_listener", side_effect=[False, ValueError("owned server exited")]
        ),
        mock.patch.object(module, "urlopen") as health,
        mock.patch.object(module.time, "sleep"),
    ):
        state = {}
        try:
            with pytest.raises(ValueError, match="owned server exited"):
                module.start_server(state, tmp_path)
        finally:
            state["server_log"].close()
    health.assert_not_called()


def test_group_cleanup_keeps_anchor_until_descendants_quiesce(recorder):
    module, _ = recorder
    process = mock.Mock(pid=12345)
    # An exited group leader remains unreaped while its descendant resists TERM.
    live = {12345: (1, 12345, 12345, "leader", "Z"), 12346: (1, 12345, 12345, "child", "S")}
    quiet = {pid: (*value[:4], "Z") for pid, value in live.items()}
    with (
        mock.patch.object(module, "anchor_identity"),
        mock.patch.object(module, "group_members", side_effect=[live, quiet]),
        mock.patch.object(module.time, "monotonic", side_effect=[0, 4, 4]),
        mock.patch.object(module.os, "killpg") as send,
        mock.patch.object(module, "process_identity", return_value=quiet[12346]),
        mock.patch.object(module.os, "getpid", return_value=1),
        mock.patch.object(module.os, "waitpid") as reap,
    ):
        module.stop_owned(process)
    assert send.call_args_list == [
        mock.call(12345, signal.SIGTERM),
        mock.call(12345, signal.SIGKILL),
    ]
    reap.assert_called_once_with(12346, 0)
    process.wait.assert_called_once()


def test_changed_group_birth_refuses_signalling(recorder):
    module, _ = recorder
    process = mock.Mock(pid=12345, loom_birth="original")
    with (
        mock.patch.object(
            module, "process_identity", return_value=(1, 12345, 12345, "replacement", "S")
        ),
        mock.patch.object(module.os, "getpid", return_value=1),
        mock.patch.object(module.os, "killpg") as send,
    ):
        with pytest.raises(ValueError, match="owned_group_birth_changed"):
            module.stop_owned(process)
    send.assert_not_called()
    process.wait.assert_not_called()


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP, signal.SIGINT])
def test_probe_defers_pending_cancellation_until_registration(recorder, signum):
    module, _ = recorder
    state = {"mediainfo": "/inert/mediainfo"}
    child = mock.Mock(pid=12345)

    def launch(*_args, **_kwargs):
        assert state["registering"] and "probe" not in state
        state["cancelled_signal"] = signum
        return child

    with (
        mock.patch.object(module.subprocess, "Popen", side_effect=launch),
        mock.patch.object(module, "process_identity", return_value=(1, 12345, 12345, "birth", "S")),
        mock.patch.object(module.selectors, "DefaultSelector") as communicate,
    ):
        with pytest.raises(module.RecordingCancelled):
            module.probe_metadata(state, Path("inert.webm"))
    assert state["probe"] is child
    assert state["registering"] is False
    communicate.assert_not_called()


def test_capture_failure_reports_fixed_diagnostics_without_exception_text(recorder):
    module, _ = recorder
    secret = "private-capture-marker"

    def fail(state):
        state["capture_stage"] = 5
        raise ValueError(secret)

    with (
        mock.patch.object(module, "start_capture", side_effect=fail),
        mock.patch.object(module, "run_native", return_value=0),
    ):
        assert module.main() == 0
    output = Path(os.environ["RUNNER_TEMP"]) / "pipecat-video-public/recording-status.json"
    raw = output.read_text()
    status = json.loads(raw)
    assert status["reason"] == "capture_setup_unqualified"
    assert status["capture_failure"] == {
        "stage": 5,
        "kind": "value",
        "code": "unavailable",
        "io": {"kind": "unavailable", "errno": 0},
    }
    assert secret not in raw
    assert status["video"] is None


def test_finalization_exception_is_in_the_public_status(recorder):
    module, _ = recorder

    def start(state):
        state["capture_stage"] = 8

    def finish(state, *_args):
        state["capture_stage"] = 19
        raise ValueError("media_probe_timeout")

    with (
        mock.patch.object(module, "start_capture", side_effect=start),
        mock.patch.object(module, "run_native", return_value=0),
        mock.patch.object(module, "finish_capture", side_effect=finish),
    ):
        assert module.main() == 0
    status = json.loads(
        (Path(os.environ["RUNNER_TEMP"]) / "pipecat-video-public/recording-status.json").read_text()
    )
    assert status["capture_failure"] == {
        "stage": 19,
        "kind": "value",
        "code": "media_probe_timeout",
        "io": {"kind": "unavailable", "errno": 0},
    }
    assert status["video"] is None


@pytest.mark.parametrize(
    "children,changed,expected",
    [
        (b"", False, "empty"),
        (b"123", False, "remaining"),
        (b"123", True, "unavailable"),
        (b"1 " * 2049, False, "unavailable"),
    ],
)
def test_post_cleanup_child_census_checks_births_and_bounds(recorder, children, changed, expected):
    module, _ = recorder
    calls = 0

    def identity(pid):
        nonlocal calls
        if pid == os.getpid():
            return (0, 0, 0, "self-birth", "S")
        calls += 1
        return (os.getpid(), 123, 123, "changed" if changed and calls > 1 else "child-birth", "S")

    with (
        mock.patch.object(module.Path, "open", side_effect=lambda *_a, **_k: io.BytesIO(children)),
        mock.patch.object(module, "process_identity", side_effect=identity),
    ):
        assert module.post_cleanup_children(True) == expected


def test_child_census_requires_subreaper_and_never_exports_ids(recorder):
    module, _ = recorder
    with mock.patch.object(module.Path, "open") as read:
        assert module.post_cleanup_children(False) == "unavailable"
    read.assert_not_called()


@pytest.mark.parametrize(
    "failure", [PermissionError("private-path"), RuntimeError("private-state")]
)
def test_child_census_read_refusal_is_unavailable(recorder, failure):
    module, _ = recorder
    with mock.patch.object(module.Path, "open", side_effect=failure):
        assert module.post_cleanup_children(True) == "unavailable"


def test_cleanup_reaps_adopted_child_outside_launcher_group(recorder):
    module, _ = recorder
    alive = ["live"]
    sent = []
    parent = module.os.getpid()
    child = parent + 100000
    identity = (parent, child, child, "adopted-birth", "S")
    original_open, original_stat = Path.open, Path.stat
    original_close = os.close

    def open_path(path, *args, **kwargs):
        if str(path) == f"/proc/{parent}/task/{parent}/children":
            return io.BytesIO(str(child).encode() if alive[0] != "reaped" else b"")
        return original_open(path, *args, **kwargs)

    def stat_path(path, *args, **kwargs):
        if str(path) == f"/proc/{child}":
            return mock.Mock(st_uid=module.os.getuid())
        return original_stat(path, *args, **kwargs)

    def identify(pid):
        if pid == parent:
            return (0, parent, parent, "recorder-birth", "S")
        assert pid == child
        return (*identity[:4], "S" if alive[0] == "live" else "Z")

    def send(fd, signum):
        assert fd == 77
        sent.append(signum)
        alive[0] = "zombie"

    def reap(pid, flags):
        assert pid == child and flags == module.os.WNOHANG
        alive[0] = "reaped"
        return (child, 0)

    with (
        mock.patch.object(module, "start_capture", side_effect=OSError("private-renderer-failure")),
        mock.patch.object(module, "run_native", return_value=0),
        mock.patch.object(
            module, "subreaper", side_effect=lambda value=None: 1 if value is None else 0
        ),
        mock.patch.object(module, "process_identity", side_effect=identify),
        mock.patch.object(module.Path, "open", open_path),
        mock.patch.object(module.Path, "stat", stat_path),
        mock.patch.object(module.os, "pidfd_open", return_value=77) as pin,
        mock.patch.object(module.signal, "pidfd_send_signal", side_effect=send),
        mock.patch.object(module.os, "waitpid", side_effect=reap) as wait,
        mock.patch.object(
            module.os, "close", side_effect=lambda fd: None if fd == 77 else original_close(fd)
        ) as close,
        mock.patch.object(
            module,
            "post_cleanup_children",
            side_effect=lambda _active: "remaining" if alive[0] != "reaped" else "empty",
        ),
        mock.patch.object(module.time, "sleep"),
        mock.patch.object(module, "write_private") as write,
    ):
        # All child identity, descriptor, signaling and reaping calls above are inert.
        module.stop_adopted_children.return_value = mock.DEFAULT
        assert module.main() == 0
    status = json.loads(write.call_args.args[1])
    assert status["post_cleanup_children"] == "empty"
    assert status["native_code"] == 0
    assert sent == [signal.SIGTERM]
    pin.assert_called_once_with(child, 0)
    wait.assert_called_once_with(child, module.os.WNOHANG)
    assert close.call_args_list.count(mock.call(77)) == 1


@pytest.fixture
def adopted_child(recorder, monkeypatch):
    module, _ = recorder
    parent, child = 1111, 2222
    state = {
        "child": "S",
        "birth": "child-birth",
        "uid": module.os.getuid(),
        "ppid": parent,
        "time": 0.0,
        "term_resisted": False,
    }
    monkeypatch.setattr(module.os, "getpid", lambda: parent)
    monkeypatch.setattr(module, "subreaper", lambda: 1)

    def identify(pid):
        if pid == parent:
            return (0, parent, parent, "recorder-birth", "S")
        assert pid == child
        return (state["ppid"], child, child, state["birth"], state["child"])

    def send(_fd, signum):
        if signum == signal.SIGKILL or not state["term_resisted"]:
            state["child"] = "Z"

    def reap(pid, flags):
        assert pid == child and flags == module.os.WNOHANG
        state["child"] = None
        return (child, 0)

    def sleep(_seconds):
        state["time"] += 3.1 if state["child"] == "S" else 0.1

    monkeypatch.setattr(module, "process_identity", identify)
    original_open, original_stat = Path.open, Path.stat

    def open_path(path, *args, **kwargs):
        if str(path) == f"/proc/{parent}/task/{parent}/children":
            return io.BytesIO(str(child).encode() if state["child"] else b"")
        return original_open(path, *args, **kwargs)

    def stat_path(path, *args, **kwargs):
        if str(path) == f"/proc/{child}":
            return mock.Mock(st_uid=state["uid"])
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(module.Path, "open", open_path)
    monkeypatch.setattr(module.Path, "stat", stat_path)
    pin = mock.Mock(return_value=77)
    send_mock = mock.Mock(side_effect=send)
    close = mock.Mock()
    monkeypatch.setattr(module.os, "pidfd_open", pin)
    monkeypatch.setattr(module.signal, "pidfd_send_signal", send_mock)
    monkeypatch.setattr(module.os, "waitpid", reap)
    monkeypatch.setattr(module.os, "close", close)
    monkeypatch.setattr(module.time, "monotonic", lambda: state["time"])
    monkeypatch.setattr(module.time, "sleep", sleep)
    # Enable the wrapped implementation only after every process syscall is mocked.
    module.stop_adopted_children.return_value = mock.DEFAULT
    return module, state, pin, send_mock, close


def test_adopted_term_deadline_escalates_then_reaps(adopted_child):
    module, state, pin, send, close = adopted_child
    state["term_resisted"] = True
    module.stop_adopted_children()
    assert state["child"] is None
    assert send.call_args_list == [mock.call(77, signal.SIGTERM), mock.call(77, signal.SIGKILL)]
    pin.assert_called_once_with(2222, 0)
    close.assert_called_once_with(77)


@pytest.mark.parametrize("changed", ["ppid", "uid", "birth"])
def test_adopted_identity_change_refuses_signal_and_closes_fd(adopted_child, changed):
    module, state, pin, send, close = adopted_child

    def opened(*_args):
        state[changed] = "replacement" if changed == "birth" else 9999
        return 77

    pin.side_effect = opened
    with pytest.raises(ValueError, match="adopted_child_identity_changed"):
        module.stop_adopted_children()
    send.assert_not_called()
    close.assert_called_once_with(77)


def test_adopted_zombie_is_reaped_without_signal(adopted_child):
    module, state, _pin, send, close = adopted_child
    state["child"] = "Z"
    module.stop_adopted_children()
    assert state["child"] is None
    send.assert_not_called()
    close.assert_called_once_with(77)


@pytest.mark.parametrize("failure", [PermissionError("private-refusal"), OSError(38, "private")])
def test_adopted_pidfd_refusal_has_no_signal_fallback(adopted_child, failure):
    module, _state, pin, send, close = adopted_child
    pin.side_effect = failure
    with pytest.raises(OSError):
        module.stop_adopted_children()
    send.assert_not_called()
    close.assert_not_called()


@pytest.mark.parametrize(
    "error,kind,number",
    [
        (TimeoutError("private"), "timeout", 0),
        (URLError(TimeoutError("private")), "timeout", 0),
        (URLError(ConnectionRefusedError(111, "private")), "connection_refused", 111),
        (ConnectionResetError(104, "private"), "connection_reset", 104),
        (BrokenPipeError(32, "private"), "broken_pipe", 32),
        (OSError(5000, "private"), "io", 0),
        (URLError("private"), "unavailable", 0),
    ],
)
def test_io_diagnostic_exports_only_fixed_category_and_errno(recorder, error, kind, number):
    module, _ = recorder
    assert module.io_diagnostic(error) == {"kind": kind, "errno": number}
    assert "private" not in json.dumps(module.capture_failure({}, error))


def test_adopted_group_change_does_not_remove_ownership(adopted_child):
    module, state, pin, send, close = adopted_child
    original = module.process_identity

    def identify(pid):
        identity = original(pid)
        return (identity[0], 3333, 3333, *identity[3:]) if pid == 2222 and pin.called else identity

    with mock.patch.object(module, "process_identity", side_effect=identify):
        module.stop_adopted_children()
    assert state["child"] is None
    send.assert_called_once_with(77, signal.SIGTERM)
    close.assert_called_once_with(77)


def test_adopted_inactive_subreaper_refuses_before_pin(adopted_child):
    module, _state, pin, send, close = adopted_child
    with mock.patch.object(module, "subreaper", return_value=0):
        with pytest.raises(ValueError, match="owned_subreaper_inactive"):
            module.stop_adopted_children()
    pin.assert_not_called()
    send.assert_not_called()
    close.assert_not_called()


def test_adopted_snapshot_bound_refuses_before_pin(adopted_child):
    module, _state, pin, send, close = adopted_child
    with mock.patch.object(module.Path, "open", return_value=io.BytesIO(b"2222 " * 129)):
        with pytest.raises(ValueError, match="adopted_child_cleanup_bound"):
            module.stop_adopted_children()
    pin.assert_not_called()
    send.assert_not_called()
    close.assert_not_called()


def test_adopted_close_failure_still_closes_other_pinned_children(adopted_child):
    module, state, pin, _send, close = adopted_child
    original = module.process_identity
    state["term_resisted"] = True

    def identify(pid):
        return (1111, pid, pid, "other-child", "S") if pid in (3333, 4444) else original(pid)

    def opened(pid, _flags):
        if pid == 4444:
            raise PermissionError("private-refusal")
        return {2222: 77, 3333: 78}[pid]

    def closing(fd):
        if fd == 77:
            raise OSError("private-close-failure")

    pin.side_effect = opened
    close.side_effect = closing
    with (
        mock.patch.object(module.Path, "open", return_value=io.BytesIO(b"2222 3333 4444")),
        mock.patch.object(module.Path, "stat", return_value=mock.Mock(st_uid=state["uid"])),
        mock.patch.object(module, "process_identity", side_effect=identify),
    ):
        with pytest.raises(OSError):
            module.stop_adopted_children()
    assert close.call_args_list == [mock.call(77), mock.call(78)]


def test_new_adoption_during_kill_stage_receives_kill(adopted_child):
    module, state, pin, send, close = adopted_child
    state["term_resisted"] = True
    other = {"state": "S"}
    original_identify = module.process_identity
    original_send = send.side_effect
    original_reap = module.os.waitpid

    def identify(pid):
        return (
            (1111, 3333, 3333, "other-child", other["state"])
            if pid == 3333
            else original_identify(pid)
        )

    def sending(fd, signum):
        if fd == 78:
            assert signum == signal.SIGKILL
            other["state"] = "Z"
        else:
            original_send(fd, signum)

    def reap(pid, flags):
        if pid == 3333:
            assert flags == module.os.WNOHANG
            other["state"] = None
            return (pid, 0)
        return original_reap(pid, flags)

    def children(*_args, **_kwargs):
        return io.BytesIO(b"2222" if state["child"] else b"3333" if other["state"] else b"")

    pin.side_effect = lambda pid, _flags: 77 if pid == 2222 else 78
    send.side_effect = sending
    with (
        mock.patch.object(module.Path, "open", side_effect=children),
        mock.patch.object(module.Path, "stat", return_value=mock.Mock(st_uid=state["uid"])),
        mock.patch.object(module, "process_identity", side_effect=identify),
        mock.patch.object(module.os, "waitpid", side_effect=reap),
    ):
        module.stop_adopted_children()
    assert send.call_args_list == [
        mock.call(77, signal.SIGTERM),
        mock.call(77, signal.SIGKILL),
        mock.call(78, signal.SIGKILL),
    ]
    assert close.call_args_list == [mock.call(77), mock.call(78)]


def test_reused_reaped_pid_fails_closed_before_repin(adopted_child):
    module, state, pin, send, close = adopted_child

    def reap(pid, flags):
        assert pid == 2222 and flags == module.os.WNOHANG
        state["birth"] = "replacement"
        state["child"] = "S"
        return (pid, 0)

    state["child"] = "Z"
    with mock.patch.object(module.os, "waitpid", side_effect=reap):
        with pytest.raises(ValueError, match="adopted_child_identity_changed"):
            module.stop_adopted_children()
    pin.assert_called_once_with(2222, 0)
    send.assert_not_called()
    close.assert_called_once_with(77)


def test_cumulative_adoptions_remain_bounded_after_reaping(adopted_child):
    module, state, pin, send, close = adopted_child
    counter = {"pid": 2000}
    original = module.process_identity

    def identify(pid):
        if pid == 1111:
            return original(pid)
        return (1111, pid, pid, str(pid), "Z")

    def children(*_args, **_kwargs):
        counter["pid"] += 1
        return io.BytesIO(str(counter["pid"]).encode())

    pin.side_effect = lambda pid, _flags: pid
    with (
        mock.patch.object(module.Path, "open", side_effect=children),
        mock.patch.object(module.Path, "stat", return_value=mock.Mock(st_uid=state["uid"])),
        mock.patch.object(module, "process_identity", side_effect=identify),
        mock.patch.object(module.os, "waitpid", side_effect=lambda pid, _flags: (pid, 0)),
        mock.patch.object(module.time, "sleep"),
    ):
        with pytest.raises(ValueError, match="adopted_child_cleanup_bound"):
            module.stop_adopted_children()
    assert pin.call_count == close.call_count == 128
    send.assert_not_called()


def test_ordinary_main_fixture_cannot_sweep_other_test_children(recorder):
    module, _ = recorder
    parent, child = os.getpid(), os.getpid() + 100000
    original_open, original_stat, original_close = Path.open, Path.stat, os.close
    snapshots = iter([str(child).encode(), b""])

    def open_path(path, *args, **kwargs):
        if str(path) == f"/proc/{parent}/task/{parent}/children":
            return io.BytesIO(next(snapshots))
        return original_open(path, *args, **kwargs)

    def stat_path(path, *args, **kwargs):
        if str(path) == f"/proc/{child}":
            return mock.Mock(st_uid=os.getuid())
        return original_stat(path, *args, **kwargs)

    def identify(pid):
        if pid == parent:
            return (0, parent, parent, "worker-birth", "S")
        assert pid == child
        return (parent, child, child, "other-test-child", "Z")

    with (
        mock.patch.object(module, "start_capture", side_effect=ValueError("unavailable")),
        mock.patch.object(module, "run_native", return_value=0),
        mock.patch.object(module, "process_identity", side_effect=identify),
        mock.patch.object(module.Path, "open", open_path),
        mock.patch.object(module.Path, "stat", stat_path),
        mock.patch.object(module.os, "pidfd_open", return_value=77) as pin,
        mock.patch.object(module.signal, "pidfd_send_signal") as send,
        mock.patch.object(module.os, "waitpid", return_value=(child, 0)) as reap,
        mock.patch.object(
            module.os, "close", side_effect=lambda fd: None if fd == 77 else original_close(fd)
        ),
        mock.patch.object(module, "post_cleanup_children", return_value="empty"),
        mock.patch.object(module.time, "sleep"),
    ):
        assert module.main() == 0
    pin.assert_not_called()
    send.assert_not_called()
    reap.assert_not_called()


def test_visualization_records_actual_elapsed_events(recorder):
    module, _ = recorder
    state = {"active": True, "started": 10.0, "events": []}
    with mock.patch.object(module.time, "monotonic", return_value=12.75):
        module.display(state, "ACTUAL PROCESS EXIT: 0")
    assert state["events"] == [(2.75, "ACTUAL PROCESS EXIT: 0")]


def test_visualization_rejects_event_overflow(recorder):
    module, _ = recorder
    state = {"active": True, "started": 0.0, "events": [(0.0, "event")] * 32}
    module.display(state, "extra")
    assert state["active"] is False
    assert state["capture_failure"]["code"] == "capture_event_bound"
    assert len(state["events"]) == 32


def test_visualization_frame_uses_only_past_actual_events(recorder):
    module, _ = recorder
    events = [(0.0, "TEST RUNNING"), (3.5, "ACTUAL PROCESS EXIT: 0")]
    assert module.visible_events(events, 3.0) == ["[  0.0s] TEST RUNNING"]
    assert module.visible_events(events, 4.0)[-1] == "[  3.5s] ACTUAL PROCESS EXIT: 0"


def test_encoder_command_is_one_thread_no_audio_fixed_budget(recorder):
    module, _ = recorder
    args = module.encoder_command(Path("/verified/encoder"), Path("/private/out.webm"), 35.0)
    assert args[0] == "/verified/encoder"
    assert args[args.index("-threads") + 1] == "1"
    assert args[args.index("-frames:v") + 1] == "175"
    assert args[args.index("-fs") + 1] == "8388608"
    assert "-an" in args and "-nostdin" in args
    assert args[args.index("-t") + 1] == "35.0"
    assert "rawvideo" in args and "libvpx" in args


@pytest.mark.parametrize("duration", [0, 29.9, 55.1, float("nan"), float("inf")])
def test_encoder_command_refuses_outside_measured_window(recorder, duration):
    module, _ = recorder
    with pytest.raises(ValueError, match="native_completion_outside_video_window"):
        module.encoder_command(Path("/encoder"), Path("/out.webm"), duration)


@pytest.mark.parametrize("mutation", ["native", "extra", "numeric_bool", "observation"])
def test_receipt_validation_precedes_observed_values(recorder, tmp_path, mutation):
    module, _ = recorder
    receipt = json.loads(json.dumps(module.EXPECTED_RECEIPT))
    code = 1 if mutation == "native" else 0
    if mutation == "extra":
        receipt["private"] = "never display"
    elif mutation == "numeric_bool":
        receipt["stored_native_messages"] = True
    elif mutation == "observation":
        receipt["observations"]["recalled_seed"] = "not observed"
    path = tmp_path / "observed.json"
    path.write_text(json.dumps(receipt))
    state = {"receipt": path, "active": True, "events": [], "started": 0.0}
    with mock.patch.object(module, "display") as display:
        with pytest.raises(ValueError):
            module.finish_capture(state, tmp_path, code)
    display.assert_not_called()


def test_pinned_encoder_hash_refuses_before_launch(recorder, tmp_path):
    module, _ = recorder
    binary = tmp_path / module.ENCODER_FILE
    binary.write_bytes(b"not the verified binary")
    binary.chmod(0o700)
    with (
        mock.patch.object(module.platform, "system", return_value="Linux"),
        mock.patch.object(module.platform, "machine", return_value="x86_64"),
        mock.patch.object(module.importlib.metadata, "version", side_effect=["0.6.0", "13.0.0"]),
        mock.patch.object(module.importlib.resources, "files", return_value=tmp_path.parent),
        mock.patch.object(
            module.Path,
            "lstat",
            return_value=mock.Mock(st_mode=0o100700, st_uid=os.getuid(), st_size=79826272),
        ),
        mock.patch.object(module.os, "access", return_value=True),
        mock.patch.object(module, "digest", return_value="wrong"),
        mock.patch.object(module.subprocess, "Popen") as launch,
    ):
        with pytest.raises(ValueError, match="encoder_binary_unqualified"):
            module.packaged_encoder()
    launch.assert_not_called()


def test_rendering_window_refuses_late_native_completion(recorder, tmp_path):
    module, _ = recorder
    receipt = tmp_path / "receipt-valid.json"
    receipt.write_text(json.dumps(module.EXPECTED_RECEIPT))
    state = {"receipt": receipt, "active": True, "events": [], "started": 0.0}
    with (
        mock.patch.object(module.time, "monotonic", return_value=48.0),
        mock.patch.object(module, "encode_video") as encode,
    ):
        with pytest.raises(ValueError, match="native_completion_outside_video_window"):
            module.finish_capture(state, tmp_path, 0)
    encode.assert_not_called()


def test_renderer_uses_measured_hold_and_exact_receipt(recorder, tmp_path):
    module, _ = recorder
    receipt = tmp_path / "receipt-valid.json"
    receipt.write_text(json.dumps(module.EXPECTED_RECEIPT))
    state = {"receipt": receipt, "active": True, "events": [], "started": 0.0}
    clock = [4.0]

    def sleep(seconds):
        clock[0] += seconds

    def encode(_state, path, duration):
        assert clock[0] >= 35.0 and duration == 35.0
        path.write_bytes(b"qualified only by mocked probe")

    metadata = {"media": {"track": [{"@type": "General", "Duration": "35"}, {"@type": "Video"}]}}
    with (
        mock.patch.object(module.time, "monotonic", side_effect=lambda: clock[0]),
        mock.patch.object(module.time, "sleep", side_effect=sleep),
        mock.patch.object(module, "encode_video", side_effect=encode),
        mock.patch.object(module, "probe_metadata", return_value=json.dumps(metadata).encode()),
    ):
        module.finish_capture(state, tmp_path, 0)
    assert state["video"]["kind"] == "timestamped_execution_visualization"
    assert all(stamp == 4.0 for stamp, _ in state["events"])
    assert "not live API telemetry" in state["events"][0][1]


@pytest.mark.parametrize("failure", ["timeout", "video_bound", "stderr_bound", "broken_pipe"])
def test_owned_encoder_pipe_failures_are_bounded(recorder, tmp_path, failure):
    module, _ = recorder
    process = mock.Mock(pid=12345)
    for index, stream in enumerate((process.stdin, process.stdout, process.stderr), 10):
        stream.fileno.return_value = index
    state = {"encoder_binary": Path("/verified/encoder"), "events": []}
    clock = iter([0.0, 31.0] if failure == "timeout" else [0.0, 1.0])
    selector = mock.MagicMock()
    selector.get_map.return_value = {10: object()}
    key = mock.Mock(fd=10, data="input")
    if failure == "video_bound":
        key.data = "output"
    elif failure == "stderr_bound":
        key.data = "diagnostic"
    selector.select.return_value = [(key, 0)]
    code = "encoder_timeout" if failure == "timeout" else "encoder_output_bound"
    expected = BrokenPipeError if failure == "broken_pipe" else ValueError
    with (
        mock.patch.object(module, "start_owned", return_value=process),
        mock.patch.object(module, "owned_exited", return_value=False),
        mock.patch.object(module.os, "set_blocking"),
        mock.patch.object(module.os, "read", return_value=b"x" * 65537),
        mock.patch.object(module.os, "write", side_effect=BrokenPipeError("private")),
        mock.patch.object(module, "frame_bytes", return_value=b"x" * (1280 * 720 * 3)),
        mock.patch.object(module, "MAX_VIDEO_BYTES", 4),
        mock.patch.object(module.time, "monotonic", side_effect=lambda: next(clock)),
        mock.patch.object(module.selectors, "DefaultSelector") as selectors,
    ):
        selectors.return_value.__enter__.return_value = selector
        with pytest.raises(expected, match="private" if failure == "broken_pipe" else code):
            module.encode_video(state, tmp_path / "private.webm", 35.0)
    assert process.wait.call_count == 0  # The owning finally retains the anchor until cleanup.


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP, signal.SIGINT])
def test_encoder_registration_defers_cancellation(recorder, tmp_path, signum):
    module, _ = recorder
    state = {"encoder_binary": Path("/verified/encoder")}
    child = mock.Mock(pid=12345)

    def launch(*_args, **kwargs):
        assert state["registering"] and "encoder" not in state
        assert "LOOM_API_KEY" not in kwargs["env"]
        assert "PATH" not in kwargs["env"]
        state["cancelled_signal"] = signum
        return child

    with (
        mock.patch.object(module.subprocess, "Popen", side_effect=launch),
        mock.patch.object(module, "process_identity", return_value=(1, 12345, 12345, "birth", "S")),
        mock.patch.object(module.selectors, "DefaultSelector") as select,
    ):
        with pytest.raises(module.RecordingCancelled):
            module.encode_video(state, tmp_path / "out.webm", 35.0)
    assert state["encoder"] is child and not state["registering"]
    select.assert_not_called()


def test_unqualified_child_census_removes_completed_video(recorder):
    module, _ = recorder
    output = Path(os.environ["RUNNER_TEMP"]) / "pipecat-video-public"

    def finish(state, path, _code):
        (path / "pipecat-ordinary-text.webm").write_bytes(b"unqualified")
        state["video"] = {"file": "pipecat-ordinary-text.webm"}

    with (
        mock.patch.object(module, "start_capture"),
        mock.patch.object(module, "run_native", return_value=0),
        mock.patch.object(module, "finish_capture", side_effect=finish),
        mock.patch.object(module, "post_cleanup_children", return_value="remaining"),
    ):
        assert module.main() == 0
    assert not (output / "pipecat-ordinary-text.webm").exists()
    status = json.loads((output / "recording-status.json").read_text())
    assert status["reason"] == "capture_cleanup_unqualified"
    assert status["video"] is None and status["native_code"] == 0


def test_script_entrypoint_runs_hosted_admission_before_work(recorder, monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS")
    with pytest.raises(SystemExit) as stopped:
        runpy.run_path(str(RECORDER), run_name="__main__")
    assert stopped.value.code == 2


@pytest.mark.parametrize("native_code", [0, 7])
@pytest.mark.parametrize("capture_fault", ["overflow", "invalid_time"])
def test_capture_fault_preserves_actual_native_exit_and_stdout(
    recorder, native_code, capture_fault
):
    module, _ = recorder
    state = {
        "active": True,
        "started": 0.0,
        "events": [(0.0, "event")] * (32 if capture_fault == "overflow" else 0),
    }
    process = mock.Mock(pid=12345)
    selector = mock.MagicMock()
    entries = {1: object()}
    selector.get_map.side_effect = lambda: entries
    selector.unregister.side_effect = lambda _: entries.clear()
    selector.select.return_value = [(mock.Mock(fileobj=process.stdout), 0)]
    outcome = mock.Mock(si_code=module.os.CLD_EXITED, si_status=native_code)
    raw = b"private native diagnostic\n"
    with (
        mock.patch.object(module, "start_owned", return_value=process),
        mock.patch.object(module.os, "read", side_effect=[raw, b""]),
        mock.patch.object(module.os, "waitid", return_value=outcome),
        mock.patch.object(
            module.time, "monotonic", return_value=-1.0 if capture_fault == "invalid_time" else 0.0
        ),
        mock.patch.object(module.sys, "stdout"),
        mock.patch.object(module.selectors, "DefaultSelector") as selectors,
    ):
        selectors.return_value.__enter__.return_value = selector
        assert module.run_native(state) == native_code
    assert state["native_code"] == native_code and state["active"] is False
    assert state["stdout_sha256"] == hashlib.sha256(raw).hexdigest()
    assert state["omitted_stdout_lines"] == 1
    assert "private" not in json.dumps(state)


@pytest.mark.parametrize("capture_fault", ["near_event_limit", "backwards_clock"])
def test_receipt_display_fault_cannot_encode_a_truncated_video(recorder, tmp_path, capture_fault):
    module, _ = recorder
    receipt = tmp_path / "valid-native-receipt.json"
    receipt.write_text(json.dumps(module.EXPECTED_RECEIPT))
    events = (
        [(0.0, "measured event")] * 30
        if capture_fault == "near_event_limit"
        else [(5.0, "measured event")]
    )
    state = {"receipt": receipt, "active": True, "events": events, "started": 0.0, "native_code": 0}
    with (
        mock.patch.object(module.time, "monotonic", return_value=4.0),
        mock.patch.object(module, "encode_video") as encode,
        mock.patch.object(module, "probe_metadata") as probe,
        mock.patch.object(module.time, "sleep") as sleep,
    ):
        with pytest.raises(ValueError, match="capture_event_stream_incomplete"):
            module.finish_capture(state, tmp_path, 0)
    encode.assert_not_called()
    probe.assert_not_called()
    sleep.assert_not_called()
    assert state["native_code"] == 0 and state.get("video") is None
    assert state["active"] is False
