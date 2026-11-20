"""Render measured native Pipecat events and validated synthetic observations.

This is a timestamped execution visualization, not a screen recording. The
optional renderer is separate from mandatory native qualification CI.
"""

from __future__ import annotations

import ctypes
import hashlib
import importlib.metadata
import importlib.resources
import json
import math
import os
import platform
import re
import selectors
import shutil
import signal
import socket
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

FILMING_CASE = "tests/integration/_pipecat_video_ci.py"
NATIVE_CASE = "tests/integration/_pipecat_demo_ci.py"
EXAMPLE = "sdk/python/examples/pipecat_text_memory.py"
COMMAND = ["pytest", "-q", FILMING_CASE]
FROZEN = {
    FILMING_CASE: "7f2b92559d508b4ec4ca36dcab670d5253550bf5a3a8302d19e3f374114160ce",
    NATIVE_CASE: "7c51a922a670353f65560ee750591f19c5b016fd3bc67c9ca01b21cfe738f72e",
    EXAMPLE: "18442d563120af0a8d61cd4ce80309efea8dc37381422e120c5574afd6ee09b3",
}
EXPECTED_OBSERVATIONS = {
    "recalled_seed": "I prefer aisle seats.",
    "persisted": [
        {"role": "assistant", "content": "I will use your stated preference for aisle seats."},
        {"role": "user", "content": "I prefer aisle seats."},
    ],
}
EXPECTED_RECEIPT = {
    "observations": EXPECTED_OBSERVATIONS,
    "result": "PASS",
    "pipecat": "1.16.0",
    "sdk_source": "checked-out candidate selected by tests/conftest.py",
    "real_agent": True,
    "auth_on": True,
    "fake_llm": True,
    "fake_embedding": True,
    "recalled_seed_in_native_context": True,
    "stored_native_messages": 2,
    "protected_family_audio_interruption_video_registry_qualified": False,
}
SUMMARY = re.compile(r"1 passed(?:, [0-9]+ warnings?)? in [0-9]+\.[0-9]+s")
PROGRESS = re.compile(r"\.[ ]+\[100%\]")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
ENCODER_SHA256 = "e7e7fb30477f717e6f55f9180a70386c62677ef8a4d4d1a5d948f4098aa3eb99"
ENCODER_FILE = "ffmpeg-linux-x86_64-v7.0.2"
WIDTH, HEIGHT, FPS = 1280, 720, 5
MAX_VIDEO_BYTES = 8 * 1024 * 1024
CAPTURE_FAILURE_CODES = frozenset(
    {
        "native_failed_no_qualified_video",
        "receipt_not_regular_small_file",
        "receipt_contract_not_met",
        "native_completion_outside_video_window",
        "capture_event_bound",
        "capture_event_stream_incomplete",
        "capture_event_invalid",
        "encoder_dependency_unqualified",
        "encoder_binary_unqualified",
        "encoder_timeout",
        "encoder_output_bound",
        "encoder_failed",
        "encoder_frame_incomplete",
        "video_size_invalid",
        "media_probe_timeout",
        "media_probe_output_too_large",
        "media_probe_failed",
        "encoded_video_duration_or_audio_invalid",
    }
)


def capture_phase(state: dict, stage: int) -> None:
    state["capture_stage"] = stage


def capture_failure(state: dict, error: Exception) -> dict:
    code = error.args[0] if isinstance(error, ValueError) and error.args else None
    return {
        "stage": state.get("capture_stage", 0),
        "kind": "io"
        if isinstance(error, OSError)
        else "value"
        if isinstance(error, ValueError)
        else "other",
        "code": code if isinstance(code, str) and code in CAPTURE_FAILURE_CODES else "unavailable",
        "io": io_diagnostic(error),
    }


def packaged_encoder() -> Path:
    if (
        platform.system() != "Linux"
        or platform.machine() != "x86_64"
        or importlib.metadata.version("imageio-ffmpeg") != "0.6.0"
        or importlib.metadata.version("Pillow") != "13.0.0"
    ):
        raise ValueError("encoder_dependency_unqualified")
    path = Path(str(importlib.resources.files("imageio_ffmpeg").joinpath("binaries", ENCODER_FILE)))
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o022
        or not os.access(path, os.X_OK)
        or info.st_size != 79826272
        or digest(path) != ENCODER_SHA256
    ):
        raise ValueError("encoder_binary_unqualified")
    return path


def start_capture(state: dict) -> None:
    capture_phase(state, 1)
    state["encoder_binary"] = packaged_encoder()
    media_binary = shutil.which("mediainfo")
    if media_binary is None:
        raise ValueError("runner_tool_missing")
    state["mediainfo"] = media_binary
    state["started"] = time.monotonic()
    state["events"] = []


def display(state: dict, label: str) -> None:
    if not state.get("active"):
        return
    try:
        elapsed = time.monotonic() - state["started"]
        events = state["events"]
        if len(events) >= 32:
            raise ValueError("capture_event_bound")
        if (
            not isinstance(label, str)
            or len(label) > 160
            or "\n" in label
            or not math.isfinite(elapsed)
            or elapsed < 0
            or (events and elapsed < events[-1][0])
        ):
            raise ValueError("capture_event_invalid")
        events.append((elapsed, label))
    except Exception as error:  # noqa: BLE001 — visualization failure cannot replace the native result
        state.setdefault("capture_failure", capture_failure(state, error))
        state["active"] = False
        state["reason"] = "capture_event_stream_unqualified"


def visible_events(events: list, elapsed: float) -> list[str]:
    return [f"[{stamp:5.1f}s] {label}" for stamp, label in events if stamp <= elapsed]


def frame_bytes(events: list, elapsed: float) -> bytes:
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (WIDTH, HEIGHT), "#101820")
    draw = ImageDraw.Draw(image)
    heading = ImageFont.load_default(size=28)
    font = ImageFont.load_default(size=21)
    draw.text(
        (32, 28),
        "Loom + Pipecat | timestamped execution visualization",
        font=heading,
        fill="#75e3c0",
    )
    labels = [
        "Measured process events, rendered after execution; not a screen recording.",
        "Real Agent + Pipecat 1.16.0 | fake LLM/embeddings | text only, no audio",
        f"Measured elapsed time: {elapsed:.1f} s | observations are a post-run assertion receipt",
    ]
    for index, label in enumerate(labels):
        draw.text((32, 78 + index * 30), label, font=font, fill="#c9d5df")
    wrapped = [part for line in visible_events(events, elapsed) for part in textwrap.wrap(line, 96)]
    for index, line in enumerate(wrapped[-16:]):
        draw.text((32, 190 + index * 30), line, font=font, fill="white")
    return image.tobytes()


def encoder_command(binary: Path, destination: Path, duration: float) -> list[str]:
    if not math.isfinite(duration) or not 30 <= duration <= 55:
        raise ValueError("native_completion_outside_video_window")
    return [
        str(binary),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s:v",
        f"{WIDTH}x{HEIGHT}",
        "-r",
        str(FPS),
        "-i",
        "pipe:0",
        "-an",
        "-c:v",
        "libvpx",
        "-threads",
        "1",
        "-b:v",
        "300k",
        "-deadline",
        "realtime",
        "-frames:v",
        str(math.ceil(duration * FPS)),
        "-t",
        str(math.ceil(duration * FPS) / FPS),
        "-fs",
        str(MAX_VIDEO_BYTES),
        "-f",
        "webm",
        str(destination),
    ]


def encode_video(state: dict, path: Path, duration: float) -> None:
    # The new renderer has one 30-second encoding budget, separate from playback time.
    deadline = time.monotonic() + 30
    environment = {name: os.environ[name] for name in ("LANG", "LC_ALL") if name in os.environ}
    command = encoder_command(state["encoder_binary"], Path("pipe:1"), duration)
    process = start_owned(
        state,
        "encoder",
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        start_new_session=True,
    )
    total = diagnostics = frame_index = offset = 0
    frame = b""
    frames = math.ceil(duration * FPS)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as out:
        with selectors.DefaultSelector() as selector:
            for stream, mode, label in (
                (process.stdin, selectors.EVENT_WRITE, "input"),
                (process.stdout, selectors.EVENT_READ, "output"),
                (process.stderr, selectors.EVENT_READ, "diagnostic"),
            ):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, mode, label)
            while selector.get_map() or not owned_exited(process):
                if time.monotonic() >= deadline:
                    raise ValueError("encoder_timeout")
                for key, _ in selector.select(timeout=0.1):
                    if key.data == "input":
                        if not frame:
                            frame = frame_bytes(state["events"], frame_index / FPS)
                            if len(frame) != WIDTH * HEIGHT * 3:
                                raise ValueError("encoder_frame_incomplete")
                        offset += os.write(key.fd, memoryview(frame)[offset : offset + 65536])
                        if offset == len(frame):
                            frame_index += 1
                            frame, offset = b"", 0
                            if frame_index == frames:
                                selector.unregister(key.fileobj)
                                process.stdin.close()
                    else:
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                        elif key.data == "output":
                            total += len(chunk)
                            if total > MAX_VIDEO_BYTES:
                                raise ValueError("encoder_output_bound")
                            out.write(chunk)
                        else:
                            diagnostics += len(chunk)
                            if diagnostics > 65536:
                                raise ValueError("encoder_output_bound")
    outcome = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT)
    if outcome.si_code != os.CLD_EXITED or outcome.si_status != 0 or frame_index != frames:
        raise ValueError("encoder_failed")
    if not 0 < total <= MAX_VIDEO_BYTES:
        raise ValueError("video_size_invalid")


def validated_receipt(state: dict, native_code: int) -> dict:
    if native_code != 0:
        raise ValueError("native_failed_no_qualified_video")
    receipt = state["receipt"]
    metadata = receipt.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 4096:
        raise ValueError("receipt_not_regular_small_file")
    payload = json.loads(receipt.read_text())
    if set(payload) != set(EXPECTED_RECEIPT) or any(
        type(payload[key]) is not type(value) or payload[key] != value
        for key, value in EXPECTED_RECEIPT.items()
    ):
        raise ValueError("receipt_contract_not_met")
    state["receipt_sha256"] = digest(receipt)
    return payload


def finish_capture(state: dict, output: Path, native_code: int) -> None:
    capture_phase(state, 11)
    payload = validated_receipt(state, native_code)
    display(state, "POST-RUN ASSERTION RECEIPT (not live API telemetry):")
    display(state, "Auth-on real Agent; fake server LLM and embedding providers.")
    observations = payload["observations"]
    display(state, "OBSERVED native developer context: " + observations["recalled_seed"])
    for row in observations["persisted"]:
        display(state, "OBSERVED persisted " + row["role"] + ": " + row["content"])
    display(state, "Python main() opened the fresh application pointer (executed).")
    display(state, "NOT qualified: separate CLI, resumed/protected restore, audio.")
    capture_phase(state, 13)
    target_end = max(state["started"] + 35, time.monotonic() + 8)
    duration = target_end - state["started"]
    if duration > 55:
        raise ValueError("native_completion_outside_video_window")
    display(state, "READING HOLD: test finished; the recorder makes no new memory calls.")
    if not state.get("active"):
        raise ValueError("capture_event_stream_incomplete")
    while time.monotonic() < target_end:
        time.sleep(0.1)
    capture_phase(state, 14)
    path = output / "pipecat-ordinary-text.webm"
    encode_video(state, path, duration)
    capture_phase(state, 19)
    metadata = probe_metadata(state, path)
    tracks = json.loads(metadata)["media"]["track"]
    general = next(x for x in tracks if x.get("@type") == "General")
    duration = float(general["Duration"])
    videos = [x for x in tracks if x.get("@type") == "Video"]
    if (
        not 30 <= duration <= 60
        or len(videos) != 1
        or any(x.get("@type") == "Audio" for x in tracks)
    ):
        raise ValueError("encoded_video_duration_or_audio_invalid")
    state["video"] = {
        "file": path.name,
        "sha256": digest(path),
        "bytes": path.stat().st_size,
        "duration_seconds": duration,
        "kind": "timestamped_execution_visualization",
    }
    capture_phase(state, 22)


def io_diagnostic(error: Exception) -> dict:
    cause = error.reason if isinstance(error, URLError) else error
    if isinstance(cause, TimeoutError):
        kind = "timeout"
    elif isinstance(cause, ConnectionRefusedError):
        kind = "connection_refused"
    elif isinstance(cause, ConnectionResetError):
        kind = "connection_reset"
    elif isinstance(cause, BrokenPipeError):
        kind = "broken_pipe"
    else:
        kind = "io" if isinstance(cause, OSError) else "unavailable"
    number = cause.errno if isinstance(cause, OSError) else None
    return {"kind": kind, "errno": number if type(number) is int and 0 < number < 4096 else 0}


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def write_private(path: Path, data: bytes) -> None:
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as out:
        out.write(data)


def process_identity(pid: int) -> tuple[int, int, int, str, str]:
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return int(fields[1]), int(fields[2]), int(fields[3]), fields[19], fields[0]


def subreaper(value: int | None = None) -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    prior = ctypes.c_int()
    if libc.prctl(37, ctypes.byref(prior), 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "get child subreaper refused")
    if value is not None and libc.prctl(36, value, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "set child subreaper refused")
    return prior.value


def child_tree(parent: int | None = None) -> dict[int, tuple]:
    parent = os.getpid() if parent is None else parent
    result = {}
    children = Path(f"/proc/{parent}/task/{parent}/children").read_text().split()
    for child in children:
        pid = int(child)
        try:
            identity = process_identity(pid)
            if identity[0] != parent:
                continue
            result[pid] = identity
            result.update(child_tree(pid))
        except FileNotFoundError:
            continue
    return result


def post_cleanup_children(subreaper_active: bool) -> str:
    """Observe owned descendants after cleanup, without signaling or exporting IDs."""
    if not subreaper_active:
        return "unavailable"
    try:
        parent = os.getpid()
        birth = process_identity(parent)[3]
        path = Path(f"/proc/{parent}/task/{parent}/children")
        deadline = time.monotonic() + 1

        def snapshot() -> dict[int, str]:
            with path.open("rb") as source:
                raw = source.read(4097)
            values = raw.split()
            if len(raw) > 4096 or len(values) > 128:
                raise ValueError("child_census_bound")
            children = {}
            for value in values:
                if time.monotonic() >= deadline or not value.isdigit():
                    raise ValueError("child_census_unavailable")
                pid = int(value)
                identity = process_identity(pid)
                if identity[0] != parent or pid in children:
                    raise ValueError("child_census_identity_changed")
                children[pid] = identity[3]
            return children

        first = snapshot()
        if (
            snapshot() != first
            or process_identity(parent)[3] != birth
            or time.monotonic() >= deadline
        ):
            return "unavailable"
        return "remaining" if first else "empty"
    except Exception:  # noqa: BLE001 — optional census failure must not interrupt remaining cleanup
        return "unavailable"


def anchor_identity(process: subprocess.Popen) -> tuple:
    identity = process_identity(process.pid)
    if identity[:3] != (os.getpid(), process.pid, process.pid):
        raise ValueError("owned_group_anchor_changed")
    if identity[3] != process.loom_birth:
        raise ValueError("owned_group_birth_changed")
    return identity


def owned_exited(process: subprocess.Popen) -> bool:
    # WNOWAIT retains the direct child's PID as the group's numeric reuse guard.
    anchor_identity(process)
    return os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None


def group_members(process: subprocess.Popen) -> dict[int, tuple]:
    anchor_identity(process)
    members = {
        pid: value
        for pid, value in child_tree().items()
        if value[1:3] == (process.pid, process.pid)
    }
    if process.pid not in members:
        raise ValueError("owned_group_anchor_not_in_child_tree")
    return members


def stop_owned(process: subprocess.Popen | None) -> None:
    if process is None:
        return
    anchor_identity(process)
    # The unreaped direct child reserves this PGID even after its own exit.
    os.killpg(process.pid, signal.SIGTERM)
    for signum in (None, signal.SIGKILL):
        if signum is not None:
            anchor_identity(process)
            os.killpg(process.pid, signum)
        until = time.monotonic() + 3
        while True:
            members = group_members(process)
            if all(value[4] == "Z" for value in members.values()):
                break
            if time.monotonic() >= until:
                break
            time.sleep(0.02)
        if all(value[4] == "Z" for value in members.values()):
            break
    if any(value[4] != "Z" for value in members.values()):
        raise ValueError("owned_group_not_quiescent")
    # Subreaper adoption makes orphaned grandchildren exact children to reap.
    for pid, identity in members.items():
        if pid == process.pid:
            continue
        try:
            current = process_identity(pid)
        except FileNotFoundError:
            continue
        if current != identity or current[0] != os.getpid():
            raise ValueError("owned_reap_identity_changed")
        os.waitpid(pid, 0)
    process.wait()


def stop_adopted_children() -> None:
    """Clean only pidfd-pinned direct children adopted by this recorder."""
    if subreaper() != 1:
        raise ValueError("owned_subreaper_inactive")
    parent, uid = os.getpid(), os.getuid()
    parent_birth = process_identity(parent)[3]
    path = Path(f"/proc/{parent}/task/{parent}/children")
    pinned, seen = {}, {}

    def identify(pid, expected=None):
        identity = process_identity(pid)
        if (
            identity[0] != parent
            or Path(f"/proc/{pid}").stat().st_uid != uid
            or process_identity(parent)[3] != parent_birth
            or (expected is not None and (identity[0], identity[3]) != expected)
        ):
            raise ValueError("adopted_child_identity_changed")
        return identity

    try:
        for signum in (signal.SIGTERM, signal.SIGKILL):
            # These additional three-second stages are shared by all adopted children.
            deadline = time.monotonic() + 3
            signalled = set()
            while time.monotonic() < deadline:
                with path.open("rb") as source:
                    raw = source.read(4097)
                values = raw.split()
                if len(raw) > 4096 or len(values) > 128:
                    raise ValueError("adopted_child_cleanup_bound")
                if process_identity(parent)[3] != parent_birth:
                    raise ValueError("recorder_birth_changed")
                children = set()
                expired = False
                for value in values:
                    if time.monotonic() >= deadline:
                        expired = True
                        break
                    if not value.isdigit():
                        raise ValueError("adopted_child_identity_unavailable")
                    pid = int(value)
                    if pid in children:
                        raise ValueError("adopted_child_identity_changed")
                    children.add(pid)
                    identity = identify(pid, seen.get(pid))
                    if pid not in pinned:
                        if len(seen) >= 128:
                            raise ValueError("adopted_child_cleanup_bound")
                        fd = os.pidfd_open(pid, 0)
                        pinned[pid] = fd
                        seen[pid] = (identity[0], identity[3])
                    current = identify(pid, seen[pid])
                    if time.monotonic() >= deadline:
                        expired = True
                        break
                    if current[4] == "Z":
                        # PPID and birth remain verified immediately before numeric reaping.
                        identify(pid, seen[pid])
                        if os.waitpid(pid, os.WNOHANG)[0] != pid:
                            raise ValueError("adopted_child_reap_incomplete")
                        os.close(pinned.pop(pid))
                    elif pid not in signalled:
                        signal.pidfd_send_signal(pinned[pid], signum)
                        signalled.add(pid)
                if expired:
                    break
                if not children:
                    return
                time.sleep(0.02)
        raise ValueError("adopted_child_not_quiescent")
    finally:
        close_error = None
        for fd in pinned.values():
            try:
                os.close(fd)
            except OSError as error:
                close_error = error
        if close_error is not None:
            raise close_error


def refuse_occupied_server_port() -> None:
    for family, address in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        with socket.socket(family) as reservation:
            if family == socket.AF_INET6:
                reservation.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            reservation.bind((address, 7778))


def owned_listener(process: subprocess.Popen, port: int = 7778) -> bool:
    if owned_exited(process):
        raise ValueError("owned_server_exited")
    listeners = set()
    for table in ("tcp", "tcp6"):
        for line in Path(f"/proc/net/{table}").read_text().splitlines()[1:]:
            fields = line.split()
            if int(fields[1].split(":")[1], 16) == port and fields[3] == "0A":
                listeners.add("socket:[" + fields[9] + "]")
    owners = set()
    for pid, identity in group_members(process).items():
        if identity[4] == "Z":
            continue
        try:
            matched = set()
            for fd in Path(f"/proc/{pid}/fd").iterdir():
                try:
                    target = os.readlink(fd)
                except FileNotFoundError:
                    continue
                if target in listeners:
                    matched.add(target)
            if process_identity(pid) == identity:
                owners.update(matched)
        except FileNotFoundError:
            continue
    # Refuse a concurrent found listener as well as a failed owned bind.
    return bool(listeners) and listeners == owners and not owned_exited(process)


def start_server(state: dict, runner_temp: Path) -> None:
    refuse_occupied_server_port()
    log = os.fdopen(
        os.open(runner_temp / "pipecat-server.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600),
        "wb",
    )
    state["server_log"] = log
    environment = {**os.environ, "LOOM_WEB_WORKERS": "3", "PORT": "7778"}
    process = start_owned(
        state,
        "server",
        ["sh", "scripts/serve.sh"],
        env=environment,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    until = time.monotonic() + 60
    while time.monotonic() < until:
        if owned_listener(process):
            try:
                with urlopen("http://localhost:7778/v1/health", timeout=2) as response:
                    healthy = response.status == 200
                if healthy and owned_listener(process):
                    return
            except OSError:
                pass
        time.sleep(0.1)
    raise ValueError("owned_server_not_ready")


class RecordingCancelled(BaseException):
    """Normal termination must unwind capture/native work into owned cleanup."""


def start_owned(state: dict, key: str, *args, **kwargs) -> subprocess.Popen:
    # Defer cancellation until the returned child has an owned cleanup handle.
    state["registering"] = True
    try:
        process = subprocess.Popen(*args, **kwargs)
        state[key] = process
        process.loom_birth = process_identity(process.pid)[3]
    finally:
        state["registering"] = False
        if state.get("cancelled_signal") is not None:
            raise RecordingCancelled
    return process


def admit_workload(state: dict) -> Path:
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise ValueError("hosted_context_required")
    runner_temp = os.environ.get("RUNNER_TEMP")
    if not runner_temp or not Path(runner_temp).is_dir():
        raise ValueError("runner_context_unavailable")
    for path, expected in FROZEN.items():
        if digest(Path(path)) != expected:
            raise ValueError("frozen_source_changed")
    receipt_value = os.environ.get("LOOM_PIPECAT_EVIDENCE")
    if not receipt_value:
        raise ValueError("receipt_path_required")
    receipt_path = Path(receipt_value)
    if receipt_path.exists() or receipt_path.is_symlink():
        raise ValueError("receipt_not_fresh")
    state["receipt"] = receipt_path
    return Path(runner_temp)


def allowed_stdout(line: bytes) -> str | None:
    text = ANSI.sub("", line.decode("utf-8", errors="replace")).strip()
    if SUMMARY.fullmatch(text) or PROGRESS.fullmatch(text):
        return text
    return None


def run_native(state: dict) -> int:
    # The filming case retains the native assertions and exports synthetic observations.
    capture_phase(state, 9)  # native output events
    display(state, "$ " + " ".join(COMMAND))
    process = start_owned(
        state,
        "native",
        COMMAND,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    assert process.stdout is not None
    pending = b""
    raw_hash = hashlib.sha256()
    omitted = 0
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        while selector.get_map():
            for key, _ in selector.select(timeout=0.1):
                chunk = os.read(key.fileobj.fileno(), 4096)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                # Preserve original diagnostics in the existing job log.
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
                raw_hash.update(chunk)
                pending += chunk
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    safe = allowed_stdout(line) if len(line) <= 512 else None
                    if safe is None:
                        omitted += 1
                    else:
                        display(state, "STDOUT: " + safe)
                if len(pending) > 8192:
                    pending = b""
                    omitted += 1
    outcome = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT)
    result = outcome.si_status if outcome.si_code == os.CLD_EXITED else -outcome.si_status
    state["stdout_sha256"] = raw_hash.hexdigest()
    state["omitted_stdout_lines"] = omitted
    state["native_ended"] = time.monotonic()
    state["native_code"] = result
    capture_phase(state, 10)  # native exit event
    display(state, "ACTUAL PROCESS EXIT: " + str(result))
    return result


def probe_metadata(state: dict, path: Path) -> bytes:
    command = [state["mediainfo"], "--Output=JSON", str(path)]
    deadline = time.monotonic() + 5
    process = start_owned(
        state,
        "probe",
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    output = bytearray()
    total = 0
    outcome = None
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        while selector.get_map() or outcome is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("media_probe_timeout")
            for key, _ in selector.select(timeout=min(0.1, remaining)):
                chunk = os.read(key.fileobj.fileno(), 4096)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                total += len(chunk)
                if total > 1024 * 1024:
                    raise ValueError("media_probe_output_too_large")
                if key.data == "stdout":
                    output.extend(chunk)
            outcome = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    if outcome.si_code != os.CLD_EXITED or outcome.si_status != 0:
        raise ValueError("media_probe_failed")
    return bytes(output)


def main() -> int:
    os.umask(0o077)
    state: dict = {"active": False, "reason": "not_started", "native_code": None}
    output = None
    prior_subreaper = None

    def cancel(signum, _frame):
        first = state.get("cancelled_signal") is None
        if first:
            state["cancelled_signal"] = signum
        state["native_code"] = 128 + state["cancelled_signal"]
        state["reason"] = "cancelled"
        if first and not state.get("cleaning_up") and not state.get("registering"):
            raise RecordingCancelled

    previous_handlers = {
        signum: signal.signal(signum, cancel)
        for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)
    }
    try:
        try:
            runner_temp = admit_workload(state)
            prior_subreaper = subreaper(1)
            if "--with-server" in sys.argv[1:]:
                start_server(state, runner_temp)
        except (OSError, ValueError):
            state["reason"] = "workload_admission_refused"
            state["native_code"] = 2
            return 2
        try:
            candidate = runner_temp / "pipecat-video-public"
            candidate.mkdir(mode=0o700, exist_ok=False)
            output = candidate
            start_capture(state)
            state["active"] = True
            state["reason"] = "recording"
        except Exception as error:  # noqa: BLE001 — setup must not skip the native check
            state["reason"] = "capture_setup_unqualified"
            state["capture_failure"] = capture_failure(state, error)
        try:
            if state.get("server") and not owned_listener(state["server"]):
                raise ValueError("owned_server_listener_lost")
            native_code = run_native(state)
        except FileNotFoundError:
            native_code = 127
        except (OSError, ValueError):
            native_code = 1
        state["native_code"] = native_code
        if state["active"]:
            try:
                assert output is not None
                finish_capture(state, output, native_code)
                state["reason"] = "recorded_requires_media_review"
            except Exception as error:  # noqa: BLE001 — capture failure cannot replace the native result
                state.setdefault("capture_failure", capture_failure(state, error))
                state["reason"] = "capture_receipt_or_encoding_unqualified"
                if output is not None:
                    try:
                        (output / "pipecat-ordinary-text.webm").unlink(missing_ok=True)
                    except OSError:
                        state["reason"] = "unqualified_video_removal_failed"
    except RecordingCancelled:
        # The handler records a non-success code before leaving the active work.
        pass
    finally:
        state["cleaning_up"] = True
        # Every cleanup target was started here. Failure of one does not skip the others.
        cleanup_failed = False
        for process in (state.get("probe"), state.get("encoder"), state.get("native")):
            try:
                stop_owned(process)
            except Exception:  # noqa: BLE001 — owned encoder cleanup still has to run
                cleanup_failed = True
        try:
            stop_owned(state.get("server"))
        except Exception:  # noqa: BLE001 — restore handlers even if server cleanup refuses
            cleanup_failed = True
        if prior_subreaper is not None and not cleanup_failed:
            try:
                stop_adopted_children()
            except Exception:  # noqa: BLE001 — unknown child ownership must fail cleanup closed
                cleanup_failed = True
        # Subreaper adoption keeps escaped process groups in this own-child census.
        # This is a post-cleanup observation, not continuous no-escape proof.
        state["post_cleanup_children"] = (
            post_cleanup_children(prior_subreaper is not None)
            if not cleanup_failed
            else "unavailable"
        )
        try:
            if state.get("probe"):
                state["probe"].stdout.close()
                state["probe"].stderr.close()
            if state.get("encoder"):
                for stream in (
                    state["encoder"].stdin,
                    state["encoder"].stdout,
                    state["encoder"].stderr,
                ):
                    if stream is not None:
                        stream.close()
            if state.get("server_log"):
                state["server_log"].close()
            if prior_subreaper is not None:
                subreaper(prior_subreaper)
        except Exception:  # noqa: BLE001 — cleanup status cannot replace the native result
            cleanup_failed = True
        if cleanup_failed:
            state["reason"] = "capture_cleanup_unqualified"
        if state.get("cancelled_signal") is not None:
            state["native_code"] = 128 + state["cancelled_signal"]
            state["reason"] = "cancelled"
        if state.get("video") and (
            state["reason"] != "recorded_requires_media_review"
            or state["post_cleanup_children"] != "empty"
        ):
            if state["reason"] == "recorded_requires_media_review":
                state["reason"] = "capture_cleanup_unqualified"
            state["video"] = None
            try:
                (output / "pipecat-ordinary-text.webm").unlink(missing_ok=True)
            except OSError:
                state["reason"] = "unqualified_video_removal_failed"
        if output is not None:
            # The status artifact exports no raw exception text, credentials or stdout.
            summary = {
                key: state.get(key)
                for key in (
                    "native_code",
                    "reason",
                    "stdout_sha256",
                    "omitted_stdout_lines",
                    "receipt_sha256",
                    "video",
                    "capture_failure",
                    "post_cleanup_children",
                    "events",
                )
            }
            summary["intended_public_artifact_synthetic_content_only"] = True
            summary["source_files"] = FROZEN
            try:
                write_private(
                    output / "recording-status.json", json.dumps(summary, indent=2).encode()
                )
            except OSError:
                print("Video UNQUALIFIED: private status could not be written.", flush=True)
        for signum, previous in previous_handlers.items():
            signal.signal(signum, previous)
    code = state["native_code"]
    if code is None:
        return 1
    return code if code >= 0 else 128 - code


if __name__ == "__main__":
    raise SystemExit(main())
