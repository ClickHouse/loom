"""Backup + restore CLI round-trip."""

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

# The CLIs read LOOM_CH_* themselves; the fixture skips without an explicit target.
pytestmark = [pytest.mark.usefixtures("ch_target_required"), pytest.mark.recovery]

BACKUP = ROOT / "dev" / "backup_loom.py"
RESTORE = ROOT / "dev" / "restore_loom.py"


# ch_client fixture comes from tests/conftest.py (env-aware so Cloud runs work)


def _run(*args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    cmd = [sys.executable, *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


@pytest.mark.asyncio
async def test_backup_writes_manifest_and_jsonl(
    tmp_path: Path,
    control_plane_url: str,
    ch_client,
) -> None:
    """Backup produces a manifest.json + per-table NDJSON files for an org."""
    from loom import Agent

    org = f"bkp-{uuid.uuid4().hex[:6]}"
    ns = f"bn-{uuid.uuid4().hex[:6]}"

    async with Agent(org=org, namespace=ns, agent="w", base_url=control_plane_url) as a:
        await a.memory.set("k1", {"v": 1}, memory_type="semantic")
        await a.memory.set("k2", {"v": 2}, memory_type="semantic")

    out = tmp_path / "bkp"
    result = _run(str(BACKUP), "--out-dir", str(out), "--org", org)
    assert result.returncode == 0, result.stderr

    manifest_path = out / "manifest.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text())
    assert manifest["org_filter"] == org

    memory_path = out / "memory.jsonl"
    assert memory_path.exists()
    lines = [ln for ln in memory_path.read_text().splitlines() if ln.strip()]
    assert len(lines) >= 2
    parsed = [json.loads(ln) for ln in lines]
    keys = {r["memory_key"] for r in parsed}
    assert {"k1", "k2"} <= keys


@pytest.mark.asyncio
async def test_round_trip_preserves_rows(
    tmp_path: Path,
    control_plane_url: str,
    ch_client,
) -> None:
    """Back up, truncate one org's rows, restore — count returns."""
    from loom import Agent

    org = f"bkp-rt-{uuid.uuid4().hex[:6]}"
    ns = f"rt-{uuid.uuid4().hex[:6]}"

    async with Agent(org=org, namespace=ns, agent="w", base_url=control_plane_url) as a:
        for i in range(3):
            await a.memory.set(f"k{i}", {"v": i}, memory_type="semantic")

    out = tmp_path / "bkp"
    r = _run(str(BACKUP), "--out-dir", str(out), "--org", org)
    assert r.returncode == 0

    # Delete our rows
    ch_client.command(
        "ALTER TABLE loom.memory DELETE WHERE org = {org:String} SETTINGS mutations_sync = 2",
        parameters={"org": org},
    )
    rows = ch_client.query(
        "SELECT count() FROM loom.memory WHERE org = {org:String}",
        parameters={"org": org},
    ).result_rows
    assert int(rows[0][0]) == 0

    # Restore
    r = _run(
        str(RESTORE),
        "--in-dir",
        str(out),
        "--tables",
        "memory",
        "--skip-idempotency",
        "--into-live",
    )
    assert r.returncode == 0, r.stderr

    rows = ch_client.query(
        "SELECT count() FROM loom.memory WHERE org = {org:String}",
        parameters={"org": org},
    ).result_rows
    assert int(rows[0][0]) >= 3


@pytest.mark.asyncio
async def test_backup_empty_org_safe(tmp_path: Path) -> None:
    out = tmp_path / "empty"
    r = _run(
        str(BACKUP),
        "--out-dir",
        str(out),
        "--org",
        f"never-existed-{uuid.uuid4().hex[:6]}",
    )
    assert r.returncode == 0, r.stderr
    manifest = json.loads((out / "manifest.json").read_text())
    # Org-scoped tables should be 0 rows for an org that never existed.
    # `idempotency` has no `org` column and is backed up unfiltered, so skip.
    for entry in manifest["tables"]:
        if "error" in entry or entry["name"] == "idempotency":
            continue
        assert entry["rows"] == 0, f"{entry['name']} had {entry['rows']} rows"


@pytest.mark.asyncio
async def test_restore_missing_manifest_fails_cleanly(tmp_path: Path) -> None:
    r = _run(str(RESTORE), "--in-dir", str(tmp_path))
    assert r.returncode != 0
    assert "manifest" in r.stderr.lower()
