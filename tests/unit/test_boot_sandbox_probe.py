"""A process probes the sandbox once a repository job can be owed, so `/v1/health` reports
it before any repository job runs here: at start, and on the registry timer when the
first repository is added through another replica. With no repository nothing spawns."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.code.app.index import repo_registry, sandbox
from server.settings import settings


@pytest.fixture
def probes(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """An unprobed process with no operator entry; each probe is recorded and fails."""
    made: list[bool] = []

    def probe() -> bool:
        made.append(True)
        return False

    monkeypatch.setattr(settings, "code_ingest_enabled", True)
    monkeypatch.setattr(settings, "code_sandbox_required", False)
    monkeypatch.setattr(settings, "code_self_service_enabled", False)
    monkeypatch.setattr(settings, "code_repos_json", "")
    monkeypatch.setattr(settings, "code_repo", "")
    monkeypatch.setattr(settings, "code_org", "acme")
    monkeypatch.setattr(settings, "code_namespace", "code-widget")
    monkeypatch.setattr(sandbox, "_available", None)
    monkeypatch.setattr(sandbox, "_probe", probe)
    return made


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
async def test_the_sandbox_is_probed_only_where_a_repository_job_is_owed(
    monkeypatch, probes: list[bool], ingest: bool, repo: str, required: bool, probed: bool
) -> None:
    monkeypatch.setattr(settings, "code_ingest_enabled", ingest)
    monkeypatch.setattr(settings, "code_sandbox_required", required)
    monkeypatch.setattr(settings, "code_repo", repo)

    await sandbox.probe_where_owed()
    await sandbox.probe_where_owed()  # once per process

    assert probes == ([True] if probed else [])
    assert sandbox.sandbox_state() is (False if probed else None)


@pytest.mark.asyncio
async def test_a_replica_probes_when_another_adds_the_first_repository(
    monkeypatch: pytest.MonkeyPatch, probes: list[bool]
) -> None:
    import server.code.store.index as store

    class _Repo:
        rows: list[dict[str, Any]] = []

        def __init__(self, ch: Any = None) -> None:
            pass

        async def registry_rows(self) -> list[dict[str, Any]]:
            return self.rows

        async def state_rows_with_prefix(self, prefix: str) -> list[dict[str, Any]]:
            return []  # no tier-health record

    monkeypatch.setattr(store, "CodeRepo", _Repo)
    monkeypatch.setattr(settings, "code_self_service_enabled", True)
    monkeypatch.setattr(settings, "code_repos_refresh_seconds", 0)
    repo_registry.reset_for_tests()
    try:
        task = await repo_registry.start_refresher()
        assert probes == [] and sandbox.sandbox_state() is None  # no repository at start
        _Repo.rows = [  # `code.repo.add` on another replica
            {
                "org": "acme",
                "namespace": "code-widget",
                "repo": "acme/widget",
                "branch": "main",
                "active": True,
            }
        ]
        for _ in range(100):
            if probes:
                break
            await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert probes == [True] and sandbox.sandbox_state() is False
    finally:
        repo_registry.reset_for_tests()


def test_the_default_deployment_polls_and_configures_no_repository() -> None:
    from server.settings import Settings

    defaults = Settings.model_fields
    assert defaults["code_ingest_enabled"].default is True
    assert defaults["code_repos_json"].default == ""
    assert defaults["code_repo"].default == ""
