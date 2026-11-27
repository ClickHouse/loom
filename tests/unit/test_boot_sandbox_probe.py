"""The boot probe lets `/v1/health` report the sandbox before any repository job runs,
and a deployment with no repository spawns no probe."""

from __future__ import annotations

import pytest

from server import boot
from server.code.app.index import sandbox
from server.settings import settings


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ingest", "repo", "required", "probed"),
    [
        (True, "acme/widget", False, True),
        (True, "", False, False),  # the default deployment: memory only
        (False, "acme/widget", False, False),  # trigger-only: the first job probes
        (False, "", True, True),  # the deployment asked for the sandbox: say so at once
    ],
    ids=["polled-repo", "no-repo", "trigger-only", "sandbox-required"],
)
async def test_boot_probes_the_sandbox_only_where_a_repository_job_is_owed(
    monkeypatch, ingest: bool, repo: str, required: bool, probed: bool
) -> None:
    probes: list[bool] = []

    def probe() -> bool:
        probes.append(True)
        return False

    monkeypatch.setattr(settings, "code_ingest_enabled", ingest)
    monkeypatch.setattr(settings, "code_sandbox_required", required)
    monkeypatch.setattr(settings, "code_self_service_enabled", False)
    monkeypatch.setattr(settings, "code_repos_json", "")
    monkeypatch.setattr(settings, "code_repo", repo)
    monkeypatch.setattr(settings, "code_org", "acme")
    monkeypatch.setattr(settings, "code_namespace", "code-widget")
    monkeypatch.setattr(sandbox, "_available", None)
    monkeypatch.setattr(sandbox, "_probe", probe)

    await boot.probe_sandbox()

    assert probes == ([True] if probed else [])
    assert sandbox.sandbox_state() is (False if probed else None)


def test_the_default_deployment_polls_and_configures_no_repository() -> None:
    from server.settings import Settings

    defaults = Settings.model_fields
    assert defaults["code_ingest_enabled"].default is True
    assert defaults["code_repo"].default == "" and defaults["code_repos_json"].default == ""
