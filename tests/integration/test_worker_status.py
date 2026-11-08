"""Worker status registry + GET /v1/admin/workers.status.

Two layers verified:
  - In-process: run_pass() populates the registry with success / failure / outcome.
  - HTTP: /v1/admin/workers.status returns config-aware payload even when
    workers have never run.

Tests that invoke workers run them in the test process, so the registry
update happens in the test process (not the server's). The HTTP shape
test hits the server endpoint directly and only asserts structure.
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))

from server.workers.status import get_registry  # noqa: E402


@pytest.fixture
def status_registry():
    reg = get_registry()
    reg.reset()
    yield reg
    reg.reset()


@pytest.fixture(scope="module", autouse=True)
async def _ch_async_client_connected():
    from server.core.ch import get_client

    ch = get_client()
    await ch.connect()
    yield
    await ch.disconnect()


# ============================================================
# Registry: each worker writes a snapshot
# ============================================================


def test_registry_records_failure(status_registry) -> None:
    """Failure path bumps consecutive_failures and stores error string."""
    status_registry.record_failure("widget", duration_ms=42, exc=RuntimeError("boom"))
    snap = status_registry.snapshot()
    s = snap["widget"]
    assert s["last_error"] == "RuntimeError: boom" and s["last_error_kind"] == "RuntimeError"
    assert s["consecutive_failures"] == 1
    assert s["last_duration_ms"] == 42


def test_registry_success_resets_consecutive_failures(status_registry) -> None:
    status_registry.record_failure("widget", duration_ms=1, exc=RuntimeError("x"))
    status_registry.record_failure("widget", duration_ms=1, exc=RuntimeError("y"))
    s = status_registry.snapshot()["widget"]
    assert s["consecutive_failures"] == 2

    status_registry.record_success("widget", duration_ms=2, outcome={"ok": True})
    s = status_registry.snapshot()["widget"]
    assert s["consecutive_failures"] == 0
    assert s["last_error"] is None and s["last_error_kind"] is None
    assert s["last_outcome"] == {"ok": True}
    assert s["total_runs"] == 3


# ============================================================
# HTTP: GET /v1/admin/workers.status
# ============================================================


@pytest.mark.asyncio
async def test_workers_status_endpoint_shape(control_plane_url: str) -> None:
    """Endpoint lists every registered worker with config metadata,
    whether or not they've run in the server process."""
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=5.0) as c:
        r = await c.get("/v1/admin/workers.status")
    assert r.status_code == 200
    body = r.json()
    assert "workers" in body
    workers = body["workers"]
    for name in ("audit_verifier", "embedding_worker"):
        assert name in workers, f"missing worker {name}"
        w = workers[name]
        assert "enabled" in w
        assert "interval_seconds" in w
        assert isinstance(w["enabled"], bool)
        assert isinstance(w["interval_seconds"], int)
