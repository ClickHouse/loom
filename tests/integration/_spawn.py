"""A private Loom server process for tests that need non-default settings.

The suite-wide server keeps its configuration; a test that needs CORS, OAuth,
OIDC or RS256 starts its own on a kernel-assigned port, waits for /v1/health
with a bounded deadline, and always stops it: terminate, then kill if it
outlives the grace period. The server log goes to a temporary file that is
printed, redacted, on teardown (pytest shows it for a failing test) and then
removed.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from tests.integration._eventually import wait_until
from tests.integration._ports import free_port
from tests.integration._redact import redact
from tests.integration._resources import ch_target

ROOT = Path(__file__).resolve().parents[2]
_STOP_GRACE_S = 10.0
_LOG_TAIL = 4000


def _tail(path: Path) -> str:
    return redact(path.read_text(errors="replace")[-_LOG_TAIL:])


@contextmanager
def loom_server(
    name: str,
    env: Mapping[str, str],
    *,
    # Above the carry barrier's 30 s: a boot waits that long for a process (this
    # suite's own) whose write was cut off before it acknowledged.
    ready_timeout: float = 45.0,
    log_level: str = "warning",
    port: int | None = None,
) -> Iterator[str]:
    """Run `server.main:app` with `env` layered over this process's
    environment; yield its base URL. `port` is kernel-assigned unless the
    caller must know it before the server starts (an OAuth issuer URL).
    Skips without an explicit ClickHouse target: the child reads LOOM_CH_*
    itself, past the in-process guard."""
    ch_target()
    port = port or free_port()
    base = f"http://127.0.0.1:{port}"
    full_env = {**os.environ, **env}
    full_env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT), str(ROOT / "sdk" / "python"), os.environ.get("PYTHONPATH", "")]
    )
    fd, log_name = tempfile.mkstemp(prefix=f"loom-it-{name}-", suffix=".log")
    log_path = Path(log_name)
    with os.fdopen(fd, "w") as log_f:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "server.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--loop",
                "asyncio",
                "--log-level",
                log_level,
            ],
            env=full_env,
            cwd=str(ROOT),
            stdout=log_f,
            stderr=subprocess.STDOUT,
        )
    try:

        def _up_or_exited() -> bool:
            if proc.poll() is not None:
                return True
            return httpx.get(f"{base}/v1/health", timeout=1.0).status_code == 200

        try:
            wait_until(_up_or_exited, max_wait=ready_timeout, interval=0.2, description=name)
        except AssertionError as e:
            pytest.fail(f"{name} server did not come up: {e}\n{_tail(log_path)}", pytrace=False)
        if proc.returncode is not None:
            pytest.fail(
                f"{name} server exited with {proc.returncode}\n{_tail(log_path)}", pytrace=False
            )
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=_STOP_GRACE_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=_STOP_GRACE_S)
        print(f"=== {name} server log (tail) ===\n{_tail(log_path)}")
        log_path.unlink(missing_ok=True)
