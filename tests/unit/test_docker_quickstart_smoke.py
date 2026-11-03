"""The Docker gate uses reproducible sources and cleans only its own stack."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from scripts.gates import docker_code_smoke, docker_quickstart_smoke


class _Seeding:
    """The seed's collaborators: every fixture's git_head lands on ingest pass
    `seeded_on`, as a background clone a few passes behind its registration does."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, seeded_on: int) -> None:
        self.heads: dict[str, str] = {}
        self.wanted: dict[str, str] = {}
        self.passes = 0
        self.tiers: list[int] = []
        self.seeded_on = seeded_on
        monkeypatch.setattr(docker_code_smoke, "ROOT", tmp_path)
        monkeypatch.setattr(docker_code_smoke, "MANIFEST", tmp_path / "docker-smoke.json")
        monkeypatch.setattr(docker_code_smoke, "SEED_POLL_S", 0)
        monkeypatch.setattr(docker_code_smoke, "fixture", self.fixture)
        monkeypatch.setattr(docker_code_smoke, "_register", self.register)
        monkeypatch.setattr(docker_code_smoke.ingest, "run_pass", self.ingest_pass)
        for tier in (
            docker_code_smoke.scip_py_delta,
            docker_code_smoke.scip_go_delta,
            docker_code_smoke.scip_ts_delta,
        ):
            monkeypatch.setattr(tier, "run_pass", self.tier_pass)

    def fixture(self, name: str, root: Path) -> tuple[Path, str]:
        return root / name, f"head-{name}"

    async def register(self, *, namespace: str, remote_head: str, **_: Any) -> None:
        self.wanted[namespace] = remote_head

    async def ingest_pass(self) -> Any:
        self.passes += 1
        if self.passes == self.seeded_on:
            self.heads.update(self.wanted)
        return SimpleNamespace(errors=[], note="cloning")

    async def tier_pass(self) -> Any:
        self.tiers.append(len(self.heads))
        return SimpleNamespace(errors=[], skipped=[], modules_skipped=[])

    async def get_state(self, org: str, namespace: str, key: str) -> str:
        assert (org, key) == ("dev", "git_head")
        return self.heads.get(namespace, "")


async def test_the_compiler_tiers_wait_for_every_fixtures_first_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeding = _Seeding(monkeypatch, tmp_path, seeded_on=8)
    entries = await docker_code_smoke.seed(seeding)  # type: ignore[arg-type]
    assert seeding.passes == 8
    assert seeding.tiers == [len(entries)] * 3


async def test_a_fixture_never_indexed_fails_the_seed_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeding = _Seeding(monkeypatch, tmp_path, seeded_on=0)
    monkeypatch.setattr(docker_code_smoke, "SEED_TIMEOUT_S", 0)
    with pytest.raises(AssertionError, match="git_head not seeded: .*code-docker-smoke-python"):
        await docker_code_smoke.seed(seeding)  # type: ignore[arg-type]
    assert seeding.tiers == []


def test_compiler_fixture_is_a_real_git_repository_with_only_source_files(tmp_path: Path) -> None:
    source, head = docker_code_smoke.fixture("python", tmp_path)
    assert len(head) == 40
    files = docker_code_smoke.run(source, "git", "ls-files").splitlines()
    assert files == ["example.py", "pyproject.toml"]
    assert 'return greet("Loom")' in (source / "example.py").read_text()


def test_failed_compose_boot_cleans_only_the_generated_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands = []

    def invoke(*args, **kwargs):
        commands.append(args)
        if args[0] == "sh":
            stack = Path(kwargs["env"]["LOOM_INSTALL_DIR"])
            stack.mkdir()
            (stack / ".env").write_text("COMPOSE_PROJECT_NAME=unit-smoke\n")
            return
        raise subprocess.CalledProcessError(1, args)

    def cleanup(args, **kwargs):
        commands.append(tuple(args))

    monkeypatch.setattr(docker_quickstart_smoke, "run", invoke)
    monkeypatch.setattr(docker_quickstart_smoke.subprocess, "run", cleanup)
    with pytest.raises(subprocess.CalledProcessError):
        docker_quickstart_smoke.smoke(
            "loom:unit-smoke",
            "loom-extractor:unit-smoke",
            "clickhouse/clickhouse-server:26.9.18.11",
        )
    cleanup_command = commands[-1]
    assert cleanup_command[-3:] == ("down", "--volumes", "--remove-orphans")
    project = Path(cleanup_command[3])
    assert project.name == "stack" and project.parent.name.startswith("loom-docker-smoke-")
    assert commands[1][:8] == cleanup_command[:8]


def test_release_gate_installs_browser_dependencies_before_running_provider_flow():
    root = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/release.yml").read_text())
    job = workflow["jobs"]["docker-smoke"]
    steps = job["steps"]
    setup = next(
        index
        for index, step in enumerate(steps)
        if step.get("uses", "").startswith("actions/setup-node@")
    )
    browser = next(
        index
        for index, step in enumerate(steps)
        if "playwright install --with-deps chromium" in step.get("run", "")
    )
    smoke = next(
        index
        for index, step in enumerate(steps)
        if "scripts/gates/docker_quickstart_smoke.py" in step.get("run", "")
    )
    assert setup < browser < smoke
    assert '--extractor-image "ghcr.io/clickhouse/loom-extractor:$VERSION"' in steps[smoke]["run"]
    images = workflow["jobs"]["docker"]["strategy"]["matrix"]["include"]
    assert [i["image"] for i in images] == [
        "ghcr.io/clickhouse/loom",
        "ghcr.io/clickhouse/loom-extractor",
    ]
    # Tagged together after both scans, so a version never names one image alone.
    assert "docker-tag" in job["needs"] and workflow["jobs"]["docker-tag"]["needs"] == ["docker"]
    assert "docker-tag" in workflow["jobs"]["github-release"]["needs"]
    assert steps[browser]["working-directory"] == "frontend"
    assert "npm ci" in steps[browser]["run"]
    assert job["timeout-minutes"] >= 30


def test_the_extractor_image_must_carry_the_servers_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    """compose.yaml runs both images at LOOM_VERSION, so another tag would not run."""

    def invoke(*args, **kwargs):
        if args[0] == "sh":
            stack = Path(kwargs["env"]["LOOM_INSTALL_DIR"])
            stack.mkdir()
            (stack / ".env").write_text("COMPOSE_PROJECT_NAME=unit-smoke\n")

    monkeypatch.setattr(docker_quickstart_smoke, "run", invoke)
    with pytest.raises(AssertionError, match="is not tagged smoke"):
        docker_quickstart_smoke.smoke(
            "loom:smoke", "loom-extractor:other", "clickhouse/clickhouse-server:26.9.18.11"
        )


def test_both_smoke_workflows_build_or_pull_the_extractor_beside_the_server() -> None:
    root = Path(__file__).resolve().parents[2]
    smoke = yaml.safe_load((root / ".github/workflows/docker-smoke.yml").read_text())
    steps = smoke["jobs"]["quickstart"]["steps"]
    built = [s["with"]["tags"] for s in steps if s.get("uses", "").startswith("docker/build-push")]
    assert built == ["loom:docker-smoke", "loom-extractor:docker-smoke"]
    run = next(s["run"] for s in steps if "docker_quickstart_smoke.py" in s.get("run", ""))
    assert "--extractor-image loom-extractor:docker-smoke" in run


def test_docker_tag_tags_both_scanned_images_and_refuses_one(tmp_path: Path) -> None:
    """The release's tag step, run against two scans' artifacts and a stub docker."""
    root = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/release.yml").read_text())
    steps = workflow["jobs"]["docker-tag"]["steps"]
    script = next(s["run"] for s in steps if s.get("name") == "Tag the scanned images")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text('#!/bin/sh\necho "$@" >> "$CALLS"\n')
    (bin_dir / "docker").chmod(0o755)
    for name, image, pin, digest, tags in (
        (
            "server",
            "ghcr.io/clickhouse/loom",
            "image.digest",
            "sha256:aa",
            "loom:1.2.3\nloom:1.2\n\n",
        ),
        (
            "extractor",
            "ghcr.io/clickhouse/loom-extractor",
            "extractor.image.digest",
            "sha256:bb",
            "x:1.2.3\n",
        ),
    ):
        d = tmp_path / f"scanned-{name}"
        d.mkdir()
        for file, value in (("image", image), ("pin", pin), ("digest", digest), ("tags", tags)):
            (d / file).write_text(value if file == "tags" else value + "\n")
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "CALLS": str(tmp_path / "calls"),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
    }

    def run() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", "-e", "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True
        )

    assert run().returncode == 0
    calls = sorted((tmp_path / "calls").read_text().splitlines())
    assert calls == [
        "buildx imagetools create -t loom:1.2.3 -t loom:1.2 ghcr.io/clickhouse/loom@sha256:aa",
        "buildx imagetools create -t x:1.2.3 ghcr.io/clickhouse/loom-extractor@sha256:bb",
    ]
    summary = (tmp_path / "summary").read_text()
    assert (
        "`image.digest: sha256:aa`" in summary and "`extractor.image.digest: sha256:bb`" in summary
    )
    shutil.rmtree(tmp_path / "scanned-extractor")
    (tmp_path / "calls").unlink()
    failed = run()
    assert (
        failed.returncode != 0
        and "expected the server's and the extractor's scans" in failed.stdout
    )
    assert not (tmp_path / "calls").exists()  # nothing tagged
