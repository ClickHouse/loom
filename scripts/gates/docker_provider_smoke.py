"""Exercise real dashboard provider actions and storage recovery in Docker.

Validation uses deterministic HTTP fixtures, not paid-provider credentials.
"""

from __future__ import annotations

import json
import subprocess
import time
import urllib.error
from collections.abc import Callable
from pathlib import Path

from scripts.gates.docker_quickstart_smoke import request, run

ROOT = Path(__file__).resolve().parents[2]


def wait_status(base: str, predicate: Callable[[dict], bool], *, timeout: float = 240) -> dict:
    deadline = time.monotonic() + timeout
    latest = {}
    while time.monotonic() < deadline:
        latest = request(base, "/v1/admin/models")
        if predicate(latest):
            return latest
        pending = latest.get("pending")
        if pending and pending["state"] == "failed":
            raise AssertionError(f"Provider rebuild failed: {pending}")
        time.sleep(1)
    raise AssertionError(f"Timed out waiting for provider activation: {latest}")


def configure(base: str, model: str, dimensions: int, key: str | None = None) -> dict:
    status = request(base, "/v1/admin/models")
    payload = {
        "expected_version": status["version"],
        "llm": {"provider": "openai", "model": "gpt-6-luna", "verification_models": []},
        "embedding": {
            "provider": "compatible",
            "model": model,
            "dimensions": dimensions,
            "base_url": "http://provider-smoke:8080/v1",
        },
        # The LLM transport alone is redirected in the test's Compose override.
        # Server-side validation, authorization and encrypted persistence are real.
        "keys": {"compatible": key, "openai": "docker-smoke-llm-key"} if key is not None else {},
    }
    return request(base, "/v1/admin/models", payload)


def control(compose: list[str], failed_model: str) -> None:
    script = (
        "import urllib.request; "
        "urllib.request.urlopen(urllib.request.Request('http://localhost:8080/control', "
        f"data={json.dumps({'failed_model': failed_model}).encode()!r}, "
        "headers={'Content-Type':'application/json'})).read()"
    )
    run(*compose, "exec", "-T", "provider-smoke", "python", "-c", script)


def data_check(compose: list[str], *args: str) -> None:
    source = (ROOT / "scripts/gates/docker_provider_data.py").read_text()
    run(*compose, "exec", "-T", "loom", "python", "-", *args, input=source, text=True)


def dashboard_action(base: str, action: str, *args: str) -> dict:
    script = ROOT / "frontend/scripts/docker-provider-ui.mjs"
    result = subprocess.run(
        ["node", str(script), base, action, *args],
        capture_output=True,
        text=True,
        timeout=90,
    )
    if result.returncode:
        # Only fixture credentials are used here. Keep the browser's locator
        # diagnostic visible instead of replacing it with a bare exit code.
        raise RuntimeError(f"Dashboard smoke action failed:\n{result.stderr[-4000:]}")
    return json.loads(result.stdout.strip())


def wait_admission(base: str) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            saved = request(
                base,
                "/v1/memory.get",
                {
                    "org": "dev",
                    "namespace": "local",
                    "agent": "docker-smoke",
                    "memory_key": "docker/smoke",
                },
            )
            assert saved["found"] and saved["value"] == "Docker memory persists", saved
            return
        except urllib.error.HTTPError as error:
            if error.code != 503:
                raise
        time.sleep(1)
    raise AssertionError("Provider committed but data-plane admission did not resume")


def smoke_providers(base: str, compose: list[str]) -> None:
    override = Path(compose[-1])
    configuration = json.loads(override.read_text())
    configuration["services"]["loom"]["entrypoint"] = [
        "python",
        "-m",
        "uvicorn",
        "docker_provider_app:app",
        "--host",
        "0.0.0.0",
        "--port",
        "7777",
        "--workers",
        "2",
    ]
    configuration["services"]["loom"]["volumes"] = [
        f"{ROOT / 'tests/fixtures/docker_provider_app.py'}:/app/docker_provider_app.py:ro"
    ]
    override.write_text(json.dumps(configuration))
    # The installer/default entrypoint has already passed the baseline smoke.
    # Only the provider phase substitutes deterministic upstream HTTP responses.
    run(*compose, "up", "-d", "--force-recreate", "--wait", "--wait-timeout", "180", "loom")
    environment = request(base, "/v1/admin/models")
    assert environment["source"] == "environment", environment
    data_check(compose, "seed")
    saved = dashboard_action(base, "apply", "docker-smoke-8", "8", "docker-smoke-embedding-key")
    assert saved["state"] == "queued", saved
    status = wait_status(base, lambda row: row["pending"] is None and row["source"] == "dashboard")
    assert status["embedding_model"] == "docker-smoke-8" and status["configured_keys"]["compatible"]
    wait_admission(base)
    data_check(compose, "docker-smoke-8", "8")

    # A key-only change needs neither rebuild nor container recreation.
    rotation = dashboard_action(base, "apply", "docker-smoke-8", "8", "docker-smoke-rotated-key")
    assert rotation["state"] == "active", rotation
    # The install is coded: a width the codec cannot take is refused before any rebuild.
    try:
        configure(base, "docker-smoke-12", 12)
    except urllib.error.HTTPError as error:
        assert error.code == 422 and b"unsupported_dimensions" in error.read(), error.code
    else:
        raise AssertionError("A coded install must refuse a 12-dimension model")
    control(compose, "docker-smoke-16")
    saved = dashboard_action(base, "apply", "docker-smoke-16", "16")
    assert saved["state"] == "queued", saved
    failed = wait_status(
        base, lambda row: bool(row["pending"] and row["pending"]["state"] == "failed")
    )
    assert failed["embedding_model"] == "docker-smoke-8", failed
    assert failed["pending"]["error_code"] == "rebuild_rebuild_failed", failed
    try:
        request(
            base,
            "/v1/memory.get",
            {
                "org": "dev",
                "namespace": "local",
                "agent": "docker-smoke",
                "memory_key": "docker/smoke",
            },
        )
    except urllib.error.HTTPError as error:
        assert error.code == 503, error.code
    else:
        raise AssertionError("Failed rebuild must keep data-plane maintenance closed")

    control(compose, "")
    dashboard_action(base, "retry")
    # SIGKILL exercises persistent process proofs and ambiguous-effect recovery,
    # not just an orderly shutdown. Restart only this generated stack's Loom.
    run(*compose, "kill", "-s", "SIGKILL", "loom")
    run(*compose, "up", "-d", "--wait", "--wait-timeout", "180", "loom")
    status = wait_status(base, lambda row: row["pending"] is None)
    assert status["embedding_model"] == "docker-smoke-16" and status["embedding_dimensions"] == 16
    wait_admission(base)
    data_check(compose, "docker-smoke-16", "16")
    run(*compose, "up", "-d", "--force-recreate", "--wait", "--wait-timeout", "180", "loom")
    status = request(base, "/v1/admin/models")
    assert status["source"] == "dashboard" and status["embedding_model"] == "docker-smoke-16"
    wait_admission(base)
    data_check(compose, "docker-smoke-16", "16")
    reset = dashboard_action(base, "reset")
    assert reset["state"] == "queued", reset
    status = wait_status(
        base, lambda row: row["pending"] is None and row["source"] == "environment"
    )
    assert status["embedding_model"] == environment["embedding_model"], status
    assert status["llm_model"] == environment["llm_model"], status
    wait_admission(base)
    data_check(compose, environment["embedding_model"], str(environment["embedding_dimensions"]))
    print(
        "PASS provider Docker flow: configuration, key rotation, failed rebuild, "
        "retry, two-worker coordination, crash/recreate persistence, environment reset"
    )
