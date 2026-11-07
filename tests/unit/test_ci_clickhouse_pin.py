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


def test_both_docker_smoke_architectures_take_one_release() -> None:
    job = _jobs("docker-smoke.yml")["quickstart"]
    assert "clickhouse" in _needs(job) and RESOLVED in _env(job).values()


def test_the_release_bundle_and_its_smoke_take_the_checked_release() -> None:
    jobs = _jobs("release.yml")
    for name in ("build", "docker-smoke"):
        assert "clickhouse" in _needs(jobs[name]), name
        assert RESOLVED in _env(jobs[name]).values(), name


def test_a_failed_lookup_fails_its_step() -> None:
    """`echo "image=$(…)"` exits 0 when the lookup fails; an assignment does not."""
    for name in ("ci.yml", "release.yml", "docker-smoke.yml"):
        for line in (WORKFLOWS / name).read_text().splitlines():
            if '>> "$GITHUB_OUTPUT"' in line:
                assert "$(" not in line, (name, line)


def test_the_release_checks_its_clickhouse_before_pinning_it() -> None:
    job = _jobs("release.yml")["clickhouse"]
    script = "\n".join(step.get("run", "") for step in job["steps"])
    assert "server.core.schema.migrate" in script and "make schema-check" in script
    assert "clickhouse" in _needs(_jobs("release.yml")["verify"])


def test_the_release_clickhouse_job_sees_the_candidate_tags() -> None:
    """Without the tags, `git tag --points-at HEAD` finds no candidate and a final
    resolves `latest` instead of its soaked release."""
    job = _jobs("release.yml")["clickhouse"]
    (checkout,) = [s for s in job["steps"] if s.get("uses", "").startswith("actions/checkout@")]
    assert checkout["with"]["fetch-depth"] == 0


def test_a_dispatch_names_its_clickhouse_through_the_environment() -> None:
    """The input reaches the shell as data; the script checks it
    (test_release_clickhouse.py)."""
    (step,) = [s for s in _jobs("release.yml")["clickhouse"]["steps"] if s.get("id") == "resolve"]
    assert step["env"]["CLICKHOUSE_IMAGE"] == "${{ inputs.clickhouse_image }}"
    assert "inputs." not in step["run"]
    assert "scripts/release/release_clickhouse.sh" in step["run"]
