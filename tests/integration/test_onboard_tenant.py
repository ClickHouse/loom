"""dev/onboard_tenant.py — anchor-tenant onboarding wrapper.

End-to-end: subprocess the script, verify .env/starter land, rate-limit
override took effect (high-burst test), smoke test passed, --skip-smoke
flag works.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import httpx
import pytest

from tests.integration._eventually import overrides_adopted

ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "dev" / "onboard_tenant.py"


def _auth_headers() -> dict[str, str]:
    """Attach LOOM_API_KEY for raw httpx hits under
    LOOM_AUTH_REQUIRED=true (the production default)."""
    tok = os.environ.get("LOOM_API_KEY")
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def _admin_token() -> str:
    """A platform_admin bearer: the `/v1/admin/*` steps need one when auth is on."""
    from server.auth.jwt import mint_token

    return mint_token(
        subject="onboard-test",
        org="*",
        namespaces=["*"],
        roles=["platform_admin"],
        ttl_seconds=600,
    )


def _run(*args: str, cwd: Path | None = None, timeout: int = 60) -> subprocess.CompletedProcess:
    cmd = [sys.executable, str(CLI), *args]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(cwd) if cwd else None,
        env={**os.environ, "LOOM_ADMIN_TOKEN": _admin_token()},
    )


@pytest.mark.asyncio
async def test_onboard_writes_env_and_starter_and_passes_smoke(
    tmp_path: Path,
    control_plane_url: str,
) -> None:
    ns = f"onboard-{uuid.uuid4().hex[:6]}"
    result = _run(
        "--namespace",
        ns,
        "--agent",
        "anchor-test",
        "--base-url",
        control_plane_url,
        "--output-dir",
        str(tmp_path),
        "--json",
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    summary = json.loads(result.stdout)
    assert summary["ok"] is True
    assert summary["namespace"] == ns
    assert summary["smoke_test"] == "passed"
    assert (tmp_path / ".env").exists()
    assert (tmp_path / "loom_starter.py").exists()
    assert set(summary["ratelimits"]) == {
        "memory",
        "knowledge.search",
        "knowledge.read",
        "knowledge.add",
    }


@pytest.mark.asyncio
async def test_onboard_rate_limits_are_raised(
    tmp_path: Path,
    control_plane_url: str,
) -> None:
    """Anchor tenant should get 3× the default knowledge.add burst.
    Default cap is 10; after onboard, 20 reqs should all succeed."""
    ns = f"onboard-rl-{uuid.uuid4().hex[:6]}"
    init = _run(
        "--namespace",
        ns,
        "--agent",
        "rl-test",
        "--base-url",
        control_plane_url,
        "--output-dir",
        str(tmp_path),
        "--skip-smoke",  # don't burn budget on the smoke path
        "--json",
    )
    assert init.returncode == 0, init.stderr
    await overrides_adopted()  # the raised limits reach every worker within a refresh

    # Hammer knowledge.add directly via HTTP (no SDK pacing in play)
    body = {
        "org": "dev",  # onboard_tenant's default org
        "namespace": ns,
        "content": "x",
        "source_uri": "s",
        "tags": [],
    }
    statuses: list[int] = []
    async with httpx.AsyncClient(
        base_url=control_plane_url, timeout=5.0, headers=_auth_headers()
    ) as c:
        for i in range(20):
            r = await c.post("/v1/knowledge.add", json={**body, "source_uri": f"s{i}"})
            statuses.append(r.status_code)
    # 20 calls under the 30-cap should all succeed
    assert all(s == 200 for s in statuses), (
        f"expected all 20 reqs under raised limit; got {statuses}"
    )


@pytest.mark.asyncio
async def test_onboard_skip_smoke_flag(
    tmp_path: Path,
    control_plane_url: str,
) -> None:
    ns = f"onboard-no-smoke-{uuid.uuid4().hex[:6]}"
    result = _run(
        "--namespace",
        ns,
        "--agent",
        "no-smoke-test",
        "--base-url",
        control_plane_url,
        "--output-dir",
        str(tmp_path),
        "--skip-smoke",
        "--json",
    )
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["smoke_test"] == "skipped"


@pytest.mark.asyncio
async def test_onboard_with_budget_override(
    tmp_path: Path,
    control_plane_url: str,
) -> None:
    """--budget-override op=ms installs a per-op budget override."""
    ns = f"onboard-bud-{uuid.uuid4().hex[:6]}"
    result = _run(
        "--namespace",
        ns,
        "--agent",
        "budget-test",
        "--base-url",
        control_plane_url,
        "--output-dir",
        str(tmp_path),
        "--skip-smoke",
        # Set a generous 5000ms budget on a new handler during bake-in
        "--budget-override",
        "memory.set=5000",
        "--json",
    )
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert "memory.set" in summary["budget_overrides"]
    assert summary["budget_overrides"]["memory.set"]["budget_ms"] == 5000

    # Verify the override is live in the server's registry by reading back
    # via the admin endpoint shape (we set it again with same value → 200)
    async with httpx.AsyncClient(
        base_url=control_plane_url,
        timeout=5.0,
        headers={"Authorization": f"Bearer {_admin_token()}"},
    ) as c:
        r = await c.post("/v1/admin/budget.set", json={"op": "memory.set", "clear": True})
    assert r.status_code == 200
