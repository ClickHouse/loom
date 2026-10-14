"""The boot probe lets `/v1/health` report the sandbox before any repository job runs."""

from __future__ import annotations

import pytest

from server import boot
from server.code.app.index import sandbox
from server.settings import settings


@pytest.mark.asyncio
@pytest.mark.parametrize("ingest", [True, False], ids=["ingest-on", "ingest-off"])
async def test_boot_probes_the_sandbox_only_when_code_ingest_runs(monkeypatch, ingest) -> None:
    probes: list[bool] = []

    def probe() -> bool:
        probes.append(True)
        return False

    monkeypatch.setattr(settings, "code_ingest_enabled", ingest)
    monkeypatch.setattr(sandbox, "_available", None)
    monkeypatch.setattr(sandbox, "_probe", probe)

    await boot.probe_sandbox()

    assert probes == ([True] if ingest else [])
    assert sandbox.sandbox_state() is (False if ingest else None)
