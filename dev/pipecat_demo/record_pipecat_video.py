"""Record one hosted text turn using synthetic content and fake server providers.

The optional filming workload is separate from mandatory qualification CI.
Original process diagnostics reach the job log; the browser receives only
allowlisted status and validated synthetic observations after all assertions.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import re
import selectors
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

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
DRIVER_ERRORS = frozenset(
    {
        "invalid argument",
        "invalid session id",
        "javascript error",
        "no such window",
        "session not created",
        "timeout",
        "unknown command",
        "unknown error",
        "unsupported operation",
    }
)
CAPTURE_FAILURE_KINDS = (
    (HTTPError, "http"),
    (OSError, "io"),
    (KeyError, "key"),
    (TypeError, "type"),
    (ValueError, "value"),
)
CAPTURE_FAILURE_CODES = frozenset(
    {
        "native_failed_no_qualified_video",
        "receipt_not_regular_small_file",
        "receipt_contract_not_met",
        "native_completion_outside_video_window",
        "capture_event_stream_incomplete",
        "browser_export_timeout",
        "browser_export_invalid",
        "video_size_invalid",
        "media_probe_timeout",
        "media_probe_output_too_large",
        "media_probe_failed",
        "encoded_video_duration_or_audio_invalid",
        "driver_listener_not_owned",
        "driver_response_too_large",
        "driver_command_refused",
    }
)


def capture_phase(state: dict, stage: int) -> None:
    state["capture_stage"] = stage
    state["driver_http_status"] = 0
    state["driver_error"] = "unavailable"


def capture_failure(state: dict, error: Exception) -> dict:
    code = error.args[0] if isinstance(error, ValueError) and error.args else None
    return {
        "stage": state.get("capture_stage", 0),
        "kind": next(
            (name for kind, name in CAPTURE_FAILURE_KINDS if isinstance(error, kind)), "other"
        ),
        "code": code
        if isinstance(code, str) and len(code) <= 64 and code in CAPTURE_FAILURE_CODES
        else "unavailable",
        "http_status": state.get("driver_http_status", 0),
        "driver_error": state.get("driver_error", "unavailable"),
    }


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_private(path: Path, data: bytes) -> None:
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as out:
        out.write(data)


def driver_error_name(value: object) -> str:
    name = value.get("error") if isinstance(value, dict) else None
    return name if isinstance(name, str) and name in DRIVER_ERRORS else "unrecognized"


def driver_call(state: dict, method: str, path: str, payload: dict | None = None):
    state["driver_http_status"] = 0
    state["driver_error"] = "unavailable"
    if not owned_listener(state["driver"], state["driver_port"]):
        raise ValueError("driver_listener_not_owned")
    body = None if payload is None else json.dumps(payload).encode()
    request = Request(state["url"] + path, data=body, method=method)
    request.add_header("Content-Type", "application/json")
    try:
        with urlopen(request, timeout=3) as response:
            state["driver_http_status"] = response.status
            raw = response.read(24 * 1024 * 1024 + 1)
    except HTTPError as error:
        state["driver_http_status"] = error.code
        try:
            diagnostic = error.read1(4097)
            if len(diagnostic) <= 4096:
                reply = json.loads(diagnostic)
                if isinstance(reply, dict):
                    state["driver_error"] = driver_error_name(reply.get("value"))
        except (OSError, ValueError, TypeError):
            pass
        raise
    if len(raw) > 24 * 1024 * 1024:
        raise ValueError("driver_response_too_large")
    value = json.loads(raw)["value"]
    if isinstance(value, dict) and "error" in value:
        state["driver_error"] = driver_error_name(value)
        raise ValueError("driver_command_refused")
    return value


def javascript(state: dict, script: str, args: list | None = None):
    return driver_call(
        state,
        "POST",
        "/session/" + state["session"] + "/execute/sync",
        {"script": script, "args": args or []},
    )


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


def start_capture(state: dict, private: Path) -> None:
    # Fixed stages identify setup failures without exporting browser logs or paths.
    state["capture_stage"] = 1  # tools
    driver_binary = shutil.which("chromedriver")
    media_binary = shutil.which("mediainfo")
    if driver_binary is None or media_binary is None:
        raise ValueError("runner_tool_missing")
    state["mediainfo"] = media_binary
    state["capture_stage"] = 2  # private driver endpoint
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    state["url"] = "http://127.0.0.1:" + str(port)
    state["driver_port"] = port
    # The browser and its driver receive no Loom or provider environment values.
    browser_env = {
        name: os.environ[name] for name in ("PATH", "LANG", "LC_ALL") if name in os.environ
    }
    log = os.fdopen(
        os.open(private / "driver.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb"
    )
    state["driver_log"] = log
    state["capture_stage"] = 3  # driver launch
    start_owned(
        state,
        "driver",
        [driver_binary, "--port=" + str(port)],
        env=browser_env,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    state["capture_stage"] = 4  # owned driver readiness
    ready_until = time.monotonic() + 5
    while True:
        if owned_listener(state["driver"], port):
            try:
                driver_call(state, "GET", "/status")
                break
            except OSError:
                pass
        if time.monotonic() >= ready_until:
            raise ValueError("driver_unavailable")
        time.sleep(0.1)
    state["capture_stage"] = 5  # browser session
    session = driver_call(
        state,
        "POST",
        "/session",
        {
            "capabilities": {
                "alwaysMatch": {
                    "browserName": "chrome",
                    "goog:chromeOptions": {
                        "args": [
                            "--headless=new",
                            "--no-first-run",
                            "--disable-background-networking",
                            "--user-data-dir=" + str(private / "chrome-profile"),
                        ]
                    },
                }
            }
        },
    )
    state["session"] = session["sessionId"]
    html = Path(__file__).with_name("terminal.html").resolve()
    state["capture_stage"] = 6  # local capture page
    driver_call(state, "POST", "/session/" + state["session"] + "/url", {"url": html.as_uri()})
    state["capture_stage"] = 7  # canvas encoder
    result = javascript(state, "return window.beginCapture();")
    if result != "recording":
        raise ValueError("browser_encoder_unavailable")
    state["started"] = time.monotonic()
    state["capture_stage"] = 8  # setup complete


def display(state: dict, label: str) -> None:
    if state.get("active"):
        try:
            javascript(state, "window.appendActualEvent(arguments[0]);", [label])
        except Exception as error:  # noqa: BLE001 — capture failure must not skip the native check
            state.setdefault("capture_failure", capture_failure(state, error))
            state["active"] = False
            state["reason"] = "browser_event_failed"


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


def finish_capture(state: dict, output: Path, native_code: int) -> None:
    capture_phase(state, 11)  # exact native receipt qualification
    if native_code != 0:
        raise ValueError("native_failed_no_qualified_video")
    receipt = state["receipt"]
    metadata = receipt.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 4096:
        raise ValueError("receipt_not_regular_small_file")
    payload = json.loads(receipt.read_text())
    # Exact types prevent True from masquerading as a numeric row count.
    if set(payload) != set(EXPECTED_RECEIPT) or any(
        type(payload[key]) is not type(value) or payload[key] != value
        for key, value in EXPECTED_RECEIPT.items()
    ):
        raise ValueError("receipt_contract_not_met")
    state["receipt_sha256"] = digest(receipt)
    capture_phase(state, 12)  # synthetic post-run receipt display
    display(state, "POST-RUN ASSERTION RECEIPT (not live API telemetry):")
    display(state, "Auth-on real Agent; fake server LLM and embedding providers.")
    observations = payload["observations"]
    display(state, "OBSERVED native developer context: " + observations["recalled_seed"])
    for row in observations["persisted"]:
        display(state, "OBSERVED persisted " + row["role"] + ": " + row["content"])
    display(state, "Python main() opened the fresh application pointer (executed).")
    display(state, "NOT qualified: separate CLI, resumed/protected restore, audio.")
    capture_phase(state, 13)  # duration window and reading hold
    target_end = max(state["started"] + 35, time.monotonic() + 8)
    if target_end > state["started"] + 55:
        raise ValueError("native_completion_outside_video_window")
    if not state.get("active"):
        raise ValueError("capture_event_stream_incomplete")
    display(state, "READING HOLD: test finished; the recorder makes no new memory calls.")
    if not state.get("active"):
        raise ValueError("capture_event_stream_incomplete")
    while time.monotonic() < target_end:
        time.sleep(0.1)
    capture_phase(state, 14)  # stop the browser encoder
    javascript(state, "window.finishCapture();")
    capture_phase(state, 15)  # bounded browser export readiness
    ready_until = time.monotonic() + 5
    while not javascript(state, "return window.captureReady;"):
        if time.monotonic() >= ready_until:
            raise ValueError("browser_export_timeout")
        time.sleep(0.1)
    capture_phase(state, 16)  # browser export value
    encoded = javascript(state, "return window.captureBase64;")
    if not isinstance(encoded, str) or len(encoded) > 12 * 1024 * 1024:
        raise ValueError("browser_export_invalid")
    capture_phase(state, 17)  # encoded bytes and size
    video = base64.b64decode(encoded, validate=True)
    if not 0 < len(video) <= 8 * 1024 * 1024:
        raise ValueError("video_size_invalid")
    path = output / "pipecat-ordinary-text.webm"
    capture_phase(state, 18)  # private video file
    write_private(path, video)
    capture_phase(state, 19)  # owned bounded media probe
    metadata = probe_metadata(state, path)
    capture_phase(state, 20)  # media metadata contract
    tracks = json.loads(metadata)["media"]["track"]
    general = next(x for x in tracks if x.get("@type") == "General")
    duration = float(general["Duration"])
    capture_phase(state, 21)  # encoded duration and audio qualification
    if not 30 <= duration <= 60 or any(x.get("@type") == "Audio" for x in tracks):
        path.unlink()
        raise ValueError("encoded_video_duration_or_audio_invalid")
    state["video"] = {
        "file": path.name,
        "sha256": digest(path),
        "bytes": len(video),
        "duration_seconds": duration,
    }
    capture_phase(state, 22)  # media ready for independent inspection


def main() -> int:
    os.umask(0o077)
    state: dict = {"active": False, "reason": "not_started", "native_code": None}
    output = None
    temporary = None
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
            output = runner_temp / "pipecat-video-public"
            output.mkdir(mode=0o700, exist_ok=False)
            temporary = tempfile.TemporaryDirectory(prefix="pipecat-video-", dir=runner_temp)
            start_capture(state, Path(temporary.name))
            state["active"] = True
            state["reason"] = "recording"
        except Exception as error:  # noqa: BLE001 — setup must not skip the native check
            state["reason"] = "capture_setup_unqualified"
            state["capture_failure"] = capture_failure(state, error)
            # Never write a status into a pre-existing output directory.
            if output is not None and temporary is None:
                output = None
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
        for process in (state.get("probe"), state.get("native")):
            try:
                stop_owned(process)
            except Exception:  # noqa: BLE001 — owned driver cleanup still has to run
                cleanup_failed = True
        if state.get("session"):
            try:
                driver_call(state, "DELETE", "/session/" + state["session"])
            except Exception:  # noqa: BLE001 — terminate the owned driver if quit fails
                cleanup_failed = True
        try:
            stop_owned(state.get("driver"))
        except Exception:  # noqa: BLE001 — close private logs and files even if the driver is gone
            cleanup_failed = True
        try:
            stop_owned(state.get("server"))
        except Exception:  # noqa: BLE001 — restore handlers even if server cleanup refuses
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
            if state.get("server_log"):
                state["server_log"].close()
            if prior_subreaper is not None:
                subreaper(prior_subreaper)
            if state.get("driver_log"):
                state["driver_log"].close()
            if temporary is not None:
                temporary.cleanup()
        except Exception:  # noqa: BLE001 — cleanup status cannot replace the native result
            cleanup_failed = True
        if cleanup_failed:
            state["reason"] = "capture_cleanup_unqualified"
        if state.get("cancelled_signal") is not None:
            state["native_code"] = 128 + state["cancelled_signal"]
            state["reason"] = "cancelled"
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
