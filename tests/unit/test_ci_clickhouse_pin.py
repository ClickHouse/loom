"""Every CI job that runs ClickHouse takes the one release the run resolved.

The unit job copies its clickhouse binary out of that image, so the
clickhouse-local checks and the integration server parse the same SQL, and
`latest` moving mid-run cannot split them.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CI = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
RESOLVED = "${{ needs.clickhouse.outputs.image }}"


def _runs_clickhouse(job: dict) -> bool:
    return any(
        "ci_start_clickhouse" in s.get("run", "") or "LOOM_CLICKHOUSE_IMAGE" in s.get("run", "")
        for s in job["steps"]
    )


def test_jobs_that_run_clickhouse_take_the_resolved_release() -> None:
    jobs = {name: job for name, job in CI["jobs"].items() if _runs_clickhouse(job)}
    assert {"unit", "schema-check", "integration"} <= set(jobs)
    for name, job in jobs.items():
        assert "clickhouse" in job.get("needs", []) or job.get("needs") == "clickhouse", name
        assert job["env"]["LOOM_CLICKHOUSE_IMAGE"] == RESOLVED, name


def test_no_job_names_a_clickhouse_image_itself() -> None:
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "clickhouse/clickhouse-server:" not in text
