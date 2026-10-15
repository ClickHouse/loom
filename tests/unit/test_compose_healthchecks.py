"""Every Loom container's healthcheck and the redeploy swap gate on readiness,
not on `/v1/health` (which stays 200 while keys, config or warm-up are wrong)."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _loom_healthchecks() -> list[tuple[str, dict]]:
    out = []
    for path in sorted((ROOT / "dev").glob("docker-compose*.yml")):
        doc = yaml.safe_load(path.read_text())
        blocks = [doc.get("x-colour-common"), doc.get("x-loom-replica")]
        blocks += list((doc.get("services") or {}).values())
        for block in blocks:
            check = (block or {}).get("healthcheck")
            if check and "urlopen" in " ".join(map(str, check["test"])):
                out.append((path.name, check))
    return out


def test_every_loom_healthcheck_reads_readiness_with_room_for_boot() -> None:
    checks = _loom_healthchecks()
    assert len(checks) >= 5
    for name, check in checks:
        assert "/v1/health/ready'" in " ".join(check["test"]), name
        assert check["start_period"] == "900s", name


def test_redeploy_waits_for_readiness() -> None:
    script = (ROOT / "redeploy.sh").read_text()
    body = script[script.index("wait_ready() {") : script.index("swap_to() {")]
    assert ".readiness.ready == true" in body
