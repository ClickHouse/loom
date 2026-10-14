"""Every workflow job that runs ClickHouse takes the one release the run resolved.

The unit job copies its clickhouse binary out of that image, so the
clickhouse-local checks and the integration server parse the same SQL, and
`latest` moving mid-run cannot split them. A release bundle names the release
its run migrated, schema-checked and smoke-tested.
"""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
RESOLVED = "${{ needs.clickhouse.outputs.image }}"


def _jobs(name: str) -> dict[str, dict]:
    return yaml.safe_load((WORKFLOWS / name).read_text())["jobs"]


def _needs(job: dict) -> list[str]:
    needs = job.get("needs", [])
    return [needs] if isinstance(needs, str) else needs


def _env(job: dict) -> dict[str, str]:
    """The job's env and every step's, merged."""
    env = dict(job.get("env", {}))
    for step in job["steps"]:
        env.update(step.get("env", {}))
    return env


def test_ci_jobs_that_run_clickhouse_take_the_resolved_release() -> None:
    jobs = {
        name: job
        for name, job in _jobs("ci.yml").items()
        if any(
            "ci_start_clickhouse" in s.get("run", "") or "LOOM_CLICKHOUSE_IMAGE" in s.get("run", "")
            for s in job["steps"]
        )
    }
    assert {"unit", "schema-check", "integration"} <= set(jobs)
    for name, job in jobs.items():
        assert "clickhouse" in _needs(job), name
        assert _env(job)["LOOM_CLICKHOUSE_IMAGE"] == RESOLVED, name


def test_no_ci_job_names_a_clickhouse_image_itself() -> None:
    assert "clickhouse/clickhouse-server:" not in (WORKFLOWS / "ci.yml").read_text()


def test_the_release_bundle_and_its_smoke_take_the_checked_release() -> None:
    jobs = _jobs("release.yml")
    for name in ("build", "docker-smoke"):
        assert "clickhouse" in _needs(jobs[name]), name
        assert RESOLVED in _env(jobs[name]).values(), name
