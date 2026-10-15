"""The Docker gate uses reproducible sources and cleans only its own stack."""

from __future__ import annotations

import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml

from scripts.gates import docker_code_smoke, docker_quickstart_smoke


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
        docker_quickstart_smoke.smoke("loom:unit-smoke", "clickhouse/clickhouse-server:26.9.18.11")
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
    assert steps[browser]["working-directory"] == "frontend"
    assert "npm ci" in steps[browser]["run"]
    assert job["timeout-minutes"] >= 30


def test_pinned_stack_disables_unsafe_vector_condition_cache():
    root = Path(__file__).resolve().parents[2]
    profile = ET.parse(root / "deploy/clickhouse-config/nlp_user_settings.xml")
    setting = profile.find("profiles/default/use_query_condition_cache")
    assert setting is not None and setting.text == "0"
