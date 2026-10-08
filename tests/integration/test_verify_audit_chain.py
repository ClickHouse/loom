"""dev/verify_audit_chain.py — chain integrity verifier."""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))

# The CLI reads LOOM_CH_* itself; the fixture skips without an explicit target.
pytestmark = pytest.mark.usefixtures("ch_target_required")

CLI = ROOT / "dev" / "verify_audit_chain.py"


# ch_client fixture comes from tests/conftest.py (env-aware so Cloud runs work)


def _run(*args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    cmd = [sys.executable, str(CLI), *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


@pytest.mark.asyncio
async def test_chain_clean_after_real_audited_writes(control_plane_url) -> None:
    """Emit some audited ops then verify the chain for that org."""
    from loom import Agent

    org = f"verify-{uuid.uuid4().hex[:6]}"
    ns = f"v-{uuid.uuid4().hex[:6]}"
    async with Agent(org=org, namespace=ns, agent="w", base_url=control_plane_url) as a:
        for i in range(3):
            await a.memory.set(f"k{i}", {"v": i}, memory_type="semantic")
            await a.memory.delete(f"k{i}")  # delete is audited

    result = _run("--org", org, "--json")
    assert result.returncode == 0, (
        f"chain expected clean for fresh writes; stderr={result.stderr!r}"
    )
    summary = json.loads(result.stdout)
    assert summary["ok"] is True
    assert summary["rows_checked"] >= 3
    assert summary["breaks"] == []


@pytest.mark.asyncio
async def test_chain_break_detected_after_tampering(control_plane_url, ch_client) -> None:
    """If an audit row's subject is tampered, verifier reports a row_hash
    mismatch."""
    from loom import Agent

    org = f"tamper-{uuid.uuid4().hex[:6]}"
    ns = f"t-{uuid.uuid4().hex[:6]}"
    async with Agent(org=org, namespace=ns, agent="w", base_url=control_plane_url) as a:
        await a.memory.set("k", {"v": 1}, memory_type="semantic")
        await a.memory.delete("k")  # audited

    # Tamper one row's `subject` field
    ch_client.command(
        """
        ALTER TABLE loom.audit_log
        UPDATE subject = '{"tampered": true}'
        WHERE org = {org:String}
        SETTINGS mutations_sync = 2
        """,
        parameters={"org": org},
    )

    result = _run("--org", org, "--json")
    assert result.returncode == 1, "chain should report broken after tampering"
    summary = json.loads(result.stdout)
    assert summary["ok"] is False
    assert len(summary["breaks"]) >= 1
    reasons = {b["reason"] for b in summary["breaks"]}
    assert any("row_hash" in r for r in reasons)


@pytest.mark.asyncio
async def test_verifier_emits_clean_summary_for_empty_org() -> None:
    """No rows → still exit 0 with 0 checks (vacuous truth, but defensible)."""
    result = _run("--org", f"empty-org-{uuid.uuid4().hex[:6]}", "--json")
    assert result.returncode == 0
    summary = json.loads(result.stdout)
    assert summary["ok"] is True
    assert summary["rows_checked"] == 0
