"""Recorder control flow uses mocks; these tests start no product or browser."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import signal
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

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


@pytest.mark.parametrize(
    "method,path", [("GET", "/status"), ("POST", "/session"), ("DELETE", "/session/test")]
)
def test_foreign_driver_receives_no_request(recorder, method, path):
    module, _ = recorder
    state = {
        "driver": object(),
        "driver_port": 33333,
        "url": "http://127.0.0.1:33333",
        "driver_http_status": 200,
        "driver_error": "unknown error",
    }
    with (
        mock.patch.object(module, "owned_listener", return_value=False),
        mock.patch.object(module, "urlopen") as foreign,
    ):
        with pytest.raises(ValueError, match="driver_listener_not_owned"):
            module.driver_call(state, method, path)
    foreign.assert_not_called()
    assert state["driver_http_status"] == 0
    assert state["driver_error"] == "unavailable"


def test_foreign_success_cannot_admit_failed_driver_bind(recorder, tmp_path):
    module, _ = recorder
    state = {}

    def launch(state, key, *_args, **_kwargs):
        state[key] = object()

    with (
        mock.patch.object(module.shutil, "which", return_value="/inert/not-executed"),
        mock.patch.object(module.socket, "socket") as sockets,
        mock.patch.object(module, "start_owned", side_effect=launch),
        mock.patch.object(module, "owned_listener", return_value=False),
        mock.patch.object(module.time, "monotonic", side_effect=[0, 6]),
        mock.patch.object(module, "urlopen") as foreign,
    ):
        sockets.return_value.__enter__.return_value.getsockname.return_value = ("127.0.0.1", 33333)
        foreign.return_value.__enter__.return_value.read.return_value = b'{"value":{"ready":true}}'
        try:
            with pytest.raises(ValueError, match="driver_unavailable"):
                module.start_capture(state, tmp_path)
        finally:
            state["driver_log"].close()
    foreign.assert_not_called()


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

    def fail(state, _private):
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
        "http_status": 0,
        "driver_error": "unavailable",
    }
    assert secret not in raw
    assert status["video"] is None


@pytest.mark.parametrize(
    "error_name,expected",
    [("session not created", "session not created"), ("private-marker", "unrecognized")],
)
def test_driver_http_failure_exports_only_allowlisted_reason(recorder, error_name, expected):
    module, _ = recorder
    body = json.dumps({"value": {"error": error_name, "message": "private-message"}}).encode()
    failure = HTTPError("http://private.invalid", 500, "private-reason", {}, io.BytesIO(body))
    state = {"driver": object(), "driver_port": 33333, "url": "http://127.0.0.1:33333"}
    with (
        mock.patch.object(module, "owned_listener", return_value=True),
        mock.patch.object(module, "urlopen", side_effect=failure),
    ):
        with pytest.raises(HTTPError) as raised:
            module.driver_call(state, "POST", "/session")
    assert raised.value is failure
    assert state["driver_http_status"] == 500
    assert state["driver_error"] == expected
    diagnostics = {k: v for k, v in state.items() if k.startswith("driver_")}
    assert "private-message" not in json.dumps(diagnostics)


def test_driver_http_diagnostic_body_is_bounded(recorder):
    module, _ = recorder
    body = mock.Mock()
    body.read1.return_value = b"x" * 4097
    failure = HTTPError("http://private.invalid", 500, "private-reason", {}, body)
    state = {"driver": object(), "driver_port": 33333, "url": "http://127.0.0.1:33333"}
    with (
        mock.patch.object(module, "owned_listener", return_value=True),
        mock.patch.object(module, "urlopen", side_effect=failure),
    ):
        with pytest.raises(HTTPError):
            module.driver_call(state, "POST", "/session")
    body.read1.assert_called_once_with(4097)
    assert state["driver_http_status"] == 500
    assert state["driver_error"] == "unavailable"


def test_capture_stage_identifies_session_creation_failure(recorder, tmp_path):
    module, _ = recorder
    state = {}

    def launch(state, key, *_args, **_kwargs):
        state[key] = object()

    with (
        mock.patch.object(module.shutil, "which", return_value="/inert/not-executed"),
        mock.patch.object(module.socket, "socket") as sockets,
        mock.patch.object(module, "start_owned", side_effect=launch),
        mock.patch.object(module, "owned_listener", return_value=True),
        mock.patch.object(module, "driver_call", side_effect=[{}, ValueError("private-message")]),
    ):
        sockets.return_value.__enter__.return_value.getsockname.return_value = ("127.0.0.1", 33333)
        try:
            with pytest.raises(ValueError, match="private-message"):
                module.start_capture(state, tmp_path)
        finally:
            state["driver_log"].close()
    assert state["capture_stage"] == 5


@pytest.mark.parametrize(
    "failure,stage,kind,code",
    [
        ("receipt", 11, "value", "unavailable"),
        ("stop", 14, "value", "unavailable"),
        ("export", 16, "value", "browser_export_invalid"),
        ("probe", 19, "io", "unavailable"),
        ("metadata", 20, "key", "unavailable"),
        ("duration", 21, "value", "encoded_video_duration_or_audio_invalid"),
    ],
)
def test_finalization_failure_keeps_fixed_stage_and_kind(
    recorder, tmp_path, failure, stage, kind, code
):
    module, _ = recorder
    receipt = tmp_path / "synthetic.json"
    receipt.write_text(json.dumps(module.EXPECTED_RECEIPT))
    if failure == "receipt":
        receipt.write_text("not-json private-value")
    state = {"receipt": receipt, "active": True, "started": 0.0}

    def javascript(_state, script, *_args):
        if script == "window.finishCapture();" and failure == "stop":
            raise ValueError("private-browser-error")
        if "captureReady" in script:
            return True
        if "captureBase64" in script:
            return None if failure == "export" else "YQ=="

    tracks = (
        [{"@type": "General"}]
        if failure == "metadata"
        else [{"@type": "General", "Duration": "10" if failure == "duration" else "35"}]
    )
    probe = (
        OSError("private-probe-error")
        if failure == "probe"
        else json.dumps({"media": {"track": tracks}}).encode()
    )
    with (
        mock.patch.object(module, "display"),
        mock.patch.object(module, "javascript", side_effect=javascript),
        mock.patch.object(
            module,
            "probe_metadata",
            side_effect=probe if isinstance(probe, Exception) else None,
            return_value=probe,
        ),
        mock.patch.object(module.time, "monotonic", side_effect=[0.0, 40.0, 40.0]),
    ):
        with pytest.raises(Exception) as raised:
            module.finish_capture(state, tmp_path, 0)
    diagnostic = module.capture_failure(state, raised.value)
    assert diagnostic == {
        "stage": stage,
        "kind": kind,
        "code": code,
        "http_status": 0,
        "driver_error": "unavailable",
    }
    assert "private" not in json.dumps(diagnostic)


def test_finalization_exception_is_in_the_public_status(recorder):
    module, _ = recorder

    def start(state, _private):
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
        "http_status": 0,
        "driver_error": "unavailable",
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


def test_event_failure_survives_later_diagnostics_and_cleanup(recorder):
    module, _ = recorder

    def start(state, _private):
        state["capture_stage"] = 8

    def javascript(state, *_args):
        state["driver_http_status"] = 500
        state["driver_error"] = "unknown error"
        raise HTTPError("http://private.invalid", 500, "private-message", {}, None)

    def finish(state, *_args):
        module.capture_phase(state, 12)
        module.display(state, "synthetic event")
        module.capture_phase(state, 13)
        raise ValueError("capture_event_stream_incomplete")

    with (
        mock.patch.object(module, "start_capture", side_effect=start),
        mock.patch.object(module, "run_native", return_value=0),
        mock.patch.object(module, "javascript", side_effect=javascript),
        mock.patch.object(module, "finish_capture", side_effect=finish),
    ):
        assert module.main() == 0
    raw = (
        Path(os.environ["RUNNER_TEMP"]) / "pipecat-video-public/recording-status.json"
    ).read_text()
    assert json.loads(raw)["capture_failure"] == {
        "stage": 12,
        "kind": "http",
        "code": "unavailable",
        "http_status": 500,
        "driver_error": "unknown error",
    }
    assert "private" not in raw


@pytest.mark.parametrize(
    "failure", [PermissionError("private-path"), RuntimeError("private-state")]
)
def test_child_census_read_refusal_is_unavailable(recorder, failure):
    module, _ = recorder
    with mock.patch.object(module.Path, "open", side_effect=failure):
        assert module.post_cleanup_children(True) == "unavailable"
