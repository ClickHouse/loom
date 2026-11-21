"""Flask's source, pytest environment and compiler-proof adapter."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

from examples.software_factory.evals.flask.historical import FrozenSource, HistoricalGrader, git
from examples.software_factory.factory.model import digest
from examples.software_factory.harbor.runtime import read, write

from .agent import ControlProvider, VisibleGrader
from .full_suite import regressions as full_suite_regressions
from .grading import validate_candidate
from .indexing import ScopeVerifier

__all__ = [
    "ScopeVerifier",
    "VisibleGrader",
    "ControlProvider",
    "validate_candidate",
    "full_suite_regressions",
    "task_inputs",
    "stage_task",
    "TASK_KIND",
    "create_assets",
    "build_dependencies",
    "materialize_cases",
    "prepare_compiler_artifacts",
    "plan_inputs",
    "freeze_plan",
]
TASK_KIND = "flask-e2e"
HERE = Path(__file__).resolve().parent
PACKAGE_ROOT = HERE.parents[1]


def _bootstrap(command, target, loom_python):
    root = HERE.parents[3]
    logs = target.parent / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    flag = "--output" if command == "assets" else "--assets"
    with (logs / (command + ".log")).open("wb") as stream:
        subprocess.run(
            [
                loom_python,
                "-m",
                "examples.software_factory.evals.flask.bootstrap",
                command,
                flag,
                str(target),
            ],
            cwd=root,
            check=True,
            stdout=stream,
            stderr=subprocess.STDOUT,
            env={
                "PATH": os.environ.get("PATH", os.defpath),
                "HOME": str(Path.home()),
                "HTTPS_PROXY": os.environ.get("HTTPS_PROXY", ""),
                "PYTHONPATH": str(root) + os.pathsep + str(root / "sdk/python"),
                "LOOM_ENV_FILE": "",
                "PYTHONDONTWRITEBYTECODE": "1",
            },
        )
    return read(target / ("assets.json" if command == "assets" else "images.json"))


def create_assets(output, *, loom_python):
    return _bootstrap("assets", Path(output), loom_python)


def build_dependencies(assets, *, loom_python):
    return _bootstrap("build", Path(assets), loom_python)


def materialize_cases(assets, output):
    from .memory.historical import materialize_cases as materialize

    return materialize(assets, output)


def prepare_compiler_artifacts(assets, materialization, output, *, loom_python, owner):
    from .compiler_setup import prepare_compiler_artifacts as compile_sources

    return compile_sources(assets, materialization, output, loom_python=loom_python, owner=owner)


def plan_inputs(assets, materialization, output):
    from .planning import inputs

    return inputs(assets, materialization, output)


def freeze_plan(prepared, setup, output, **kwargs):
    from .planning import freeze

    return freeze(prepared, setup, output, **kwargs)


def task_inputs(protocol, task_id, *, binding=None):
    config = protocol["tasks"][task_id]
    public = next(
        row for row in read(protocol["historical_manifest"])["tasks"] if row["task_id"] == task_id
    )
    source = FrozenSource(Path(config["source_git"]), public["base_commit"])
    namespace = "-" if binding is None else binding["code_namespace"]
    if (
        not isinstance(namespace, str)
        or not namespace
        or (binding is not None and namespace == "-")
    ):
        raise PermissionError("Treatment binding requires a code namespace")
    task = replace(source.task(public, namespace, config["symbol"]), kind=TASK_KIND)
    private = read(config["evaluation_file"])
    if private["base_commit"] != task.base_commit:
        raise PermissionError("Evaluator and task bases differ")
    return config, source, task, private


def stage_task(plan, task_id, output, build_image):
    config, source, task, private = task_inputs(plan, task_id)
    fixtures = source.fixtures()
    historical = HistoricalGrader(
        source,
        config["image"],
        config["visible_selectors"],
        config["hidden_selectors"],
        private["test_patch"],
        output / "unused",
    )
    spec = {
        "source_sha256": task.source_sha256,
        "dependency_image": config["image"],
        "grader_sha256": digest([historical.sha256, fixtures]),
        "fixtures_sha256": digest(fixtures),
        "visible_selectors": config["visible_selectors"],
        "hidden_selectors": config["hidden_selectors"],
        "visible_test_call_limit": plan["visible_test_call_limit"],
    }
    images = {}
    for kind in ("agent", "verifier"):
        build = output / "builds" / task_id / kind
        files = build / "factory"
        files.mkdir(parents=True)
        library = files / "lib/examples/software_factory"
        for name in (
            "factory/model.py",
            "factory/workspace.py",
            "harbor/cleanup.py",
            "evals/flask/historical.py",
            "evals/flask/full_suite.py",
        ):
            target = library / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(PACKAGE_ROOT / name, target)
            for parent in [target.parent, *target.parents]:
                if not parent.is_relative_to(files / "lib"):
                    break
                (parent / "__init__.py").touch()
        shutil.copyfile(HERE / "grading.py", files / "grader.py")
        base = dict(historical.base)
        if kind == "verifier":
            tree = build / "patched-base"
            tree.mkdir()
            for name, content in base.items():
                p = tree / name
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content)
            git(tree, "apply", "-", data=private["test_patch"].encode())
            base = {str(p.relative_to(tree)): p.read_text() for p in tree.rglob("*") if p.is_file()}
        else:
            spec_agent = {k: v for k, v in spec.items() if k != "hidden_selectors"}
        write(files / "base.json", base)
        write(files / "fixtures.json", fixtures)
        write(files / "spec.json", spec if kind == "verifier" else spec_agent)
        if kind == "verifier":
            tests = files / "tests"
            tests.mkdir()
            (tests / "test.sh").write_text(
                "#!/bin/sh\nset -eu\n"
                "python /factory/grader.py hidden /logs/artifacts/candidate.json\n"
            )
        images[kind] = build_image(build, config["image"])
    return spec, images
