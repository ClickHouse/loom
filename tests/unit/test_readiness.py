"""Readiness (`/v1/health/ready`), liveness, the SIGTERM drain and the boot
warm-up: what a load balancer reads to route around a replica."""

from __future__ import annotations

import asyncio
import signal
import threading

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

import server.boot as boot
from server import health
from server.core import readiness
from server.core.readiness import CheckResult
from server.settings import settings


@pytest.fixture(autouse=True)
def _clean_registry():
    readiness._reset_for_tests()
    yield
    readiness._reset_for_tests()


def _client() -> TestClient:
    app = FastAPI()
    health.attach(app)
    return TestClient(app)


def test_live_answers_without_clickhouse(monkeypatch) -> None:
    def _no_ch():
        raise AssertionError("liveness must not touch ClickHouse")

    monkeypatch.setattr("server.core.ch.get_client", _no_ch)
    r = _client().get("/v1/health/live")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_ready_is_503_with_the_failing_check_and_200_when_all_pass() -> None:
    state = {"ok": False}
    readiness.register("keyring", lambda: readiness.result(state["ok"], "keyring not loaded"))
    readiness.register("gate", lambda: CheckResult(False, "closed"), required=False)
    r = _client().get("/v1/health/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["failing"] == ["keyring"]
    assert body["checks"]["keyring"] == {"ok": False, "reason": "keyring not loaded"}
    assert body["checks"]["gate"]["required"] is False
    state["ok"] = True
    assert _client().get("/v1/health/ready").status_code == 200


def test_draining_makes_the_process_unready() -> None:
    readiness.begin_drain()
    r = _client().get("/v1/health/ready")
    assert r.status_code == 503 and r.json()["failing"] == ["draining"]


@pytest.mark.asyncio
async def test_a_raising_or_hanging_check_fails_instead_of_erroring(monkeypatch) -> None:
    monkeypatch.setattr(settings, "readiness_check_timeout_s", 0.05)

    def _boom() -> CheckResult:
        raise RuntimeError("Code: 60. Unknown table loom.grants (ch-internal-7:9440)")

    async def _hang() -> CheckResult:
        await asyncio.sleep(5)
        return CheckResult(True)

    readiness.register("boom", _boom)
    readiness.register("hang", _hang)
    state = await readiness.evaluate()
    assert not state.ready and sorted(state.failing) == ["boom", "hang"]
    # `/v1/health/ready` answers without a token: the kind, not the driver's text.
    assert state.checks["boom"].reason == "RuntimeError"


async def test_a_raising_check_is_logged_by_name_once_a_minute(caplog) -> None:
    """Probes poll readiness without a token: the text is logged, named, not per poll."""

    def _boom() -> CheckResult:
        raise RuntimeError("Code: 60. Unknown table loom.grants (ch-internal-7:9440)")

    readiness.register("grants", _boom)
    with caplog.at_level("WARNING", logger="server.core.readiness"):
        for _ in range(3):
            await readiness.evaluate()
    lines = [r.getMessage() for r in caplog.records if "failed" in r.getMessage()]
    assert len(lines) == 1 and "grants" in lines[0]


@pytest.mark.asyncio
async def test_a_cached_check_runs_once_per_ttl() -> None:
    calls: list[int] = []

    async def _ping() -> CheckResult:
        calls.append(1)
        return CheckResult(True)

    readiness.register("clickhouse", _ping, ttl_s=60)
    await asyncio.gather(*(readiness.evaluate() for _ in range(5)))
    assert len(calls) == 1


def test_the_app_serves_the_probes_ahead_of_the_dashboard() -> None:
    """The dashboard catch-all attaches last; the probes are API routes."""
    from server.main import app

    r = TestClient(app).get("/v1/health/ready")
    assert r.headers["content-type"].startswith("application/json")
    assert "ready" in r.json()
    assert TestClient(app).get("/v1/health/live").json() == {"status": "ok"}


def test_the_model_gate_lets_the_probes_through() -> None:
    from server.transport.model_maintenance import _control_request

    for path in ("/v1/health/live", "/v1/health/ready"):
        assert _control_request({"path": path, "method": "GET"})


def test_boot_registers_every_check(monkeypatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "ready_requires_mirrors", False)
    boot.register_readiness_checks(object())
    assert set(readiness.names()) == {
        "clickhouse",
        "migrations",
        "revocation",
        "keyring",
        "grant_watermark",
        "tracker_registry",
        "code_registry",
        "runtime_overrides",
        "calibration",
        "config_drift",
        "sandbox",
        "model_work_gate",
    }
    monkeypatch.setattr(settings, "ready_requires_mirrors", True)
    boot.register_readiness_checks(object())
    assert "mirrors" in readiness.names()


def test_the_assistant_check_is_registered_only_where_one_is_configured(monkeypatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "ready_requires_mirrors", False)
    monkeypatch.setattr(settings, "assistant_librechat_url", "")
    monkeypatch.setattr(readiness, "_CHECKS", {})
    boot.register_readiness_checks(object())
    assert "assistant" not in readiness.names()
    monkeypatch.setattr(settings, "assistant_librechat_url", "http://librechat:3080")
    boot.register_readiness_checks(object())
    # Reported, never required: a deployment whose assistant is down still serves.
    assert "assistant" in readiness.names()
    assert readiness._CHECKS["assistant"].required is False


def test_a_stale_grant_watermark_is_reported_but_not_required(monkeypatch) -> None:
    import time

    from server.core import grant_cache

    grant_cache.clear()
    assert not grant_cache.readiness_check().ok
    monkeypatch.setattr(grant_cache, "_polled_at", time.monotonic())
    assert grant_cache.readiness_check().ok


def test_token_state_needs_a_loaded_fresh_snapshot(monkeypatch) -> None:
    from server.auth.revocation import token_state_ready
    from server.settings import settings

    monkeypatch.setattr(type(settings), "auth_actually_required", lambda self: True)
    monkeypatch.setattr(settings, "jwt_revocation_max_stale_seconds", 600)
    assert not token_state_ready(False, float("inf"), "keyring").ok
    assert "old" in token_state_ready(True, 900.0, "keyring").reason
    assert token_state_ready(True, 10.0, "keyring").ok
    monkeypatch.setattr(type(settings), "auth_actually_required", lambda self: False)
    assert token_state_ready(False, float("inf"), "keyring").ok


def test_a_keyring_of_another_root_is_unready_outside_a_deployment(monkeypatch) -> None:
    import dataclasses

    from server.auth import keyring
    from server.settings import settings

    monkeypatch.setattr(type(settings), "auth_actually_required", lambda self: True)
    monkeypatch.setattr(settings, "deployment", False)
    keyring._reset_for_tests(loaded=True)
    try:
        assert keyring.readiness_check().ok
        monkeypatch.setattr(keyring, "_view", dataclasses.replace(keyring._view, root_row="other"))
        assert "LOOM_JWT_SECRET" in keyring.readiness_check().reason
    finally:
        keyring._reset_for_tests(loaded=True)


def test_keyring_reports_its_age() -> None:
    from server.auth import keyring

    keyring._reset_for_tests(loaded=False)
    assert keyring.snapshot_age_seconds() == float("inf")
    keyring._reset_for_tests(loaded=True)
    assert keyring.snapshot_age_seconds() < 5


def test_sandbox_check_only_when_required(monkeypatch) -> None:
    from server.code.app.index import sandbox
    from server.settings import settings

    monkeypatch.setattr(settings, "code_sandbox_required", False)
    assert sandbox.readiness_check().ok
    monkeypatch.setattr(settings, "code_sandbox_required", True)
    monkeypatch.setattr(sandbox, "layout_refusal", lambda: "")
    monkeypatch.setattr(sandbox, "_available", None)
    assert sandbox.readiness_check().reason == "sandbox probe not finished"
    monkeypatch.setattr(sandbox, "_available", False)
    assert not sandbox.readiness_check().ok
    monkeypatch.setattr(sandbox, "_available", True)
    assert sandbox.readiness_check().ok


def test_drift_makes_the_process_unready(monkeypatch) -> None:
    from server.core import deployment_config

    monkeypatch.setattr(deployment_config, "drift", lambda: ["LOOM_EMBEDDING_MODEL differs"])
    assert deployment_config.readiness_check().reason == "LOOM_EMBEDDING_MODEL differs"
    monkeypatch.setattr(deployment_config, "drift", lambda: [])
    assert deployment_config.readiness_check().ok


def test_mirrors_wait_for_clones_and_not_for_failures(monkeypatch, tmp_path) -> None:
    from types import SimpleNamespace

    from server.code.app.index import mirrors
    from server.code.domain.index import repos
    from server.settings import settings

    def _entry(name: str) -> SimpleNamespace:
        return SimpleNamespace(
            git_dir=str(tmp_path / f"{name}.git"),
            label=name,
            git_remote="",
            source="config",
            namespace=name,
        )

    entries = [_entry("here"), _entry("queued"), _entry("broken")]
    (tmp_path / "here.git").mkdir()
    monkeypatch.setattr(settings, "git_history", "local")
    monkeypatch.setattr(repos, "configured_repos", lambda: entries)
    monkeypatch.setattr(mirrors, "_QUEUED", {entries[1].git_dir})
    monkeypatch.setattr(mirrors, "_LISTED", asyncio.Event())
    mirrors._LISTED.set()
    monkeypatch.setattr(
        mirrors,
        "job_state",
        lambda d: ("failed", "auth refused") if d == entries[2].git_dir else ("", ""),
    )
    status = mirrors.mirror_status()
    assert status["ready"] == 1 and status["failed"] == {"auth refused": 1}
    # `/v1/health/ready` answers without a token: a count, no entry's label.
    assert asyncio.run(mirrors.readiness_check()).reason == "mirrors cloning: 1"
    monkeypatch.setattr(mirrors, "_QUEUED", set())
    assert asyncio.run(mirrors.readiness_check()).ok


def test_mirrors_are_unready_until_the_warm_up_has_listed_its_clones(monkeypatch) -> None:
    from server.code.app.index import mirrors
    from server.code.domain.index import repos
    from server.settings import settings

    monkeypatch.setattr(settings, "code_intel_enabled", True)
    monkeypatch.setattr(settings, "git_history", "local")
    monkeypatch.setattr(settings, "code_mirror_warmup", True)
    monkeypatch.setattr(repos, "configured_repos", lambda: [])
    monkeypatch.setattr(mirrors, "_LISTED", asyncio.Event())
    got = asyncio.run(mirrors.readiness_check())
    assert not got.ok and "warm-up" in got.reason
    mirrors._LISTED.set()
    assert asyncio.run(mirrors.readiness_check()).ok
    mirrors._LISTED.clear()
    monkeypatch.setattr(settings, "code_mirror_warmup", False)  # nothing queues at boot
    assert asyncio.run(mirrors.readiness_check()).ok


def test_health_walks_the_mirrors_only_when_asked(monkeypatch) -> None:
    from server.code.app.index import mirrors
    from server.settings import settings

    async def _down() -> tuple[bool, str]:
        return False, "down"

    walked: list[int] = []
    monkeypatch.setattr(settings, "code_intel_enabled", True)
    monkeypatch.setattr(health, "_ping_clickhouse", _down)
    monkeypatch.setattr(mirrors, "mirror_status", lambda: walked.append(1) or {"ready": 0})
    assert "mirrors" not in _client().get("/v1/health").json() and not walked
    assert _client().get("/v1/health?include=mirrors").json()["mirrors"] == {"ready": 0}


@pytest.mark.asyncio
async def test_boot_waits_for_the_first_loads(monkeypatch) -> None:
    """The first tracker, override and bundle loads land before startup completes."""
    from server.core import runtime_overrides
    from server.tracker.app import registry_sources

    boot._WARM.clear()
    monkeypatch.setattr(boot, "_OVERRIDES_ADOPTED", False)
    loaded: list[str] = []

    async def _refresh(*, force=False, ch=None):
        await asyncio.sleep(0.05)
        loaded.append("tracker")
        return []

    async def _load(*, ch=None):
        await asyncio.sleep(0.05)
        loaded.append("overrides")
        return {}

    monkeypatch.setattr(registry_sources, "refresh_db_sources", _refresh)
    monkeypatch.setattr(runtime_overrides, "load", _load)
    monkeypatch.setattr("server.core.ratelimit.get_limiter", lambda: _Adopt())
    monkeypatch.setattr("server.observability.budgets.get_overrides", lambda: _Adopt())
    monkeypatch.setattr("server.tracker.app.contract.warm_at_boot", _noop)

    async def _publish(assets_dir, *, published_by="", ch=None):
        await asyncio.sleep(0.05)
        loaded.append("assets")
        return 0

    monkeypatch.setattr("server.storage.frontend_assets.publish", _publish)
    tasks = [boot.start_tracker_sources_refresher(object()), boot.start_runtime_overrides()]
    boot.publish_frontend_assets(object())
    try:
        names = ("tracker_registry", "runtime_overrides", "frontend_assets")
        late = await boot.await_warm_up(2.0, names)
        assert late == [] and sorted(loaded) == ["assets", "overrides", "tracker"]
        assert boot._OVERRIDES_ADOPTED
    finally:
        for t in tasks:
            t.cancel()
        boot.cancel_boot_tasks()


@pytest.mark.asyncio
async def test_warm_up_is_bounded() -> None:
    boot._WARM.clear()
    assert await boot.await_warm_up(0.05, ("tracker_registry",)) == ["tracker_registry"]


class _Adopt:
    def adopt(self, _snapshot) -> None:
        return None


async def _noop() -> None:
    return None


@pytest.mark.asyncio
async def test_sigterm_turns_unready_and_stops_uvicorn_after_the_delay(monkeypatch) -> None:
    """The first SIGTERM drains; uvicorn's handler runs after the delay."""
    from server.settings import settings

    if threading.current_thread() is not threading.main_thread():
        pytest.skip("signals need the main thread")
    monkeypatch.setattr(settings, "shutdown_ready_delay_s", 1.0)
    got: list[int] = []

    class _Server(uvicorn.Server):
        def handle_exit(self, sig, frame) -> None:
            got.append(sig)

    old = signal.signal(signal.SIGTERM, _Server(uvicorn.Config(app=_noop)).handle_exit)
    try:
        assert boot.install_drain_on_sigterm()
        signal.raise_signal(signal.SIGTERM)
        await asyncio.sleep(0.02)
        assert readiness.draining() and got == []
        await asyncio.sleep(1.5)
        assert got == [signal.SIGTERM]
    finally:
        signal.signal(signal.SIGTERM, old)


def test_not_installed_over_a_foreign_handler() -> None:
    if threading.current_thread() is not threading.main_thread():
        pytest.skip("signals need the main thread")
    old = signal.signal(signal.SIGTERM, signal.SIG_DFL)
    try:
        assert not asyncio.run(_install())
    finally:
        signal.signal(signal.SIGTERM, old)


async def _install() -> bool:
    return boot.install_drain_on_sigterm()


@pytest.mark.asyncio
async def test_installed_over_the_handler_of_the_installed_uvicorn() -> None:
    """The handler uvicorn's own Server puts on SIGTERM while it serves."""
    if threading.current_thread() is not threading.main_thread():
        pytest.skip("signals need the main thread")
    with uvicorn.Server(uvicorn.Config(app=_noop)).capture_signals():
        assert boot.install_drain_on_sigterm()
        assert signal.getsignal(signal.SIGTERM).__name__ == "_handler"


@pytest.mark.asyncio
async def test_usage_labels_drain_with_a_waited_insert(monkeypatch) -> None:
    """The shutdown drain lands buffered labels, and waits for the insert."""
    import inspect

    from server.core.ch_writes import async_insert_settings
    from server.memory.store import usage

    seen: list[dict] = []

    class _CH:
        async def insert(self, table, rows, *, settings=None):
            seen.append(settings)

    usage._reset_for_tests()
    usage._USAGE_BUFFER.append({"memory_id": "m"})
    monkeypatch.setattr(usage, "_default_client", lambda: _CH())
    await usage.drain()
    assert seen == [async_insert_settings(background=True)]
    assert "_usage.drain()" in inspect.getsource(boot.drain_writes)
    usage._reset_for_tests()


def test_shutdown_drains_share_a_budget_before_retirement() -> None:
    import inspect

    from server import main

    src = inspect.getsource(main._shut_down)
    budget = src.index("_shutdown_drains(lease, supervisor, emitter), timeout=left")
    assert (
        src.index("await _instances.stop(ch)") < budget < src.index("await boot.leave_deployment(")
    )
    life = inspect.getsource(main.lifespan)
    assert "await _shut_down(" in life and "ch.disconnect()" not in life


def test_the_lifespan_waits_for_warm_up_within_its_setting() -> None:
    import inspect

    from server import main

    src = inspect.getsource(main.lifespan)
    assert "settings.boot_warmup_timeout_s" in src and '"frontend_assets"' in src


def test_the_lifespan_starts_the_mirror_warm_up_itself() -> None:
    import inspect

    from server import main

    assert "start_warmup()" in inspect.getsource(main.lifespan)
    assert "start_warmup" not in inspect.getsource(boot.start_tracker_sources_refresher)


def test_the_code_registry_check_fails_until_the_first_read(monkeypatch) -> None:
    from server.code.app.index import repo_registry
    from server.code.domain.index import repos
    from server.settings import settings

    monkeypatch.setattr(settings, "code_self_service_enabled", True)
    monkeypatch.setattr(repos, "_registry_loaded", False)
    assert repo_registry.readiness_check() == readiness.CheckResult(
        False, "the self-service repository registry is not read"
    )
    monkeypatch.setattr(repos, "_registry_loaded", True)
    assert repo_registry.readiness_check().ok
