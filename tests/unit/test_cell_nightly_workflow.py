"""Cell acceptance runs nightly without extending every pull request."""

from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"


def test_cell_acceptance_has_a_daily_and_manual_entrypoint() -> None:
    workflow = yaml.safe_load((WORKFLOWS / "cell-nightly.yml").read_text())
    triggers = workflow[True]
    assert set(triggers) == {"schedule", "workflow_dispatch"}
    minute, hour, *days = triggers["schedule"][0]["cron"].split()
    assert 0 <= int(minute) < 60 and 0 <= int(hour) < 24
    assert days == ["*", "*", "*"]
    job = workflow["jobs"]["cell-trigger-acceptance"]
    assert "if" not in job
    assert job["needs"] == "clickhouse"
    assert job["timeout-minutes"] == 150
    steps = job["steps"]
    acceptance = next(step for step in steps if "real API writes" in step.get("name", ""))
    assert "--clickhouse-image" in acceptance["run"]
    cleanup = next(step for step in steps if "--cleanup-only" in step.get("run", ""))
    assert cleanup["if"] == "always()"
    artifact = next(step for step in steps if "upload-artifact@" in step.get("uses", ""))
    assert artifact["if"] == "always()"
    assert workflow["permissions"] == {"contents": "read"}


def test_docker_smoke_keeps_its_pr_checks_and_weekly_schedule() -> None:
    workflow = yaml.safe_load((WORKFLOWS / "docker-smoke.yml").read_text())
    assert "pull_request" in workflow[True]
    assert workflow[True]["schedule"] == [{"cron": "17 4 * * 1"}]
    assert "cell-trigger-acceptance" not in workflow["jobs"]
    assert {"quickstart", "clickhouse"} <= set(workflow["jobs"])
