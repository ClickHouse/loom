"""scripts/gates/sdk_extra_requirements.py prints an SDK extra's requirements
for `make test-sdk-adapters`, and refuses an extra the SDK does not declare.
The Makefile's adapter target type-checks with the dev mypy pin, and every
extra in SDK_ADAPTER_EXTRAS maps to one module and one test file."""

from __future__ import annotations

import tomllib

import pytest

from scripts.gates import sdk_extra_requirements as gate


def test_prints_each_named_extras_requirements(capsys: pytest.CaptureFixture[str]) -> None:
    declared = tomllib.loads(gate.PYPROJECT.read_text())["project"]["optional-dependencies"]
    assert gate.main(["langchain"]) == 0
    assert capsys.readouterr().out.splitlines() == declared["langchain"]


def test_an_unknown_extra_fails(capsys: pytest.CaptureFixture[str]) -> None:
    assert gate.main(["langchain", "no-such-framework"]) == 1
    assert "no-such-framework" in capsys.readouterr().err


def test_the_adapter_target_type_checks_with_the_dev_mypy() -> None:
    root = gate.PYPROJECT.parents[2]
    dev = tomllib.loads((root / "pyproject.toml").read_text())["project"]["optional-dependencies"]
    (pin,) = [r for r in dev["dev"] if r.startswith("mypy==")]
    target = (
        (root / "Makefile").read_text().split("test-sdk-adapter-%:\n", 1)[1].split("\n\n", 1)[0]
    )
    assert f" {pin}" in target


def _adapter_modules() -> set[str]:
    makefile = (gate.PYPROJECT.parents[2] / "Makefile").read_text()
    (line,) = [ln for ln in makefile.splitlines() if ln.startswith("SDK_ADAPTER_EXTRAS :=")]
    return {extra.replace("-", "_") for extra in line.split(":=", 1)[1].split()}


def test_every_adapter_extra_has_its_tests_and_module() -> None:
    root = gate.PYPROJECT.parents[2]
    for module in sorted(_adapter_modules()):
        assert (root / "sdk" / "python" / "tests" / f"test_{module}.py").is_file(), module
        assert (root / "sdk" / "python" / "loom" / "integrations" / f"{module}.py").is_file(), (
            module
        )


def test_every_sdk_test_file_runs_under_an_adapter_extra() -> None:
    """Each venv runs only its adapter's file, so a test file no extra names never runs."""
    root = gate.PYPROJECT.parents[2]
    modules = _adapter_modules()
    tests = {
        p.stem.removeprefix("test_") for p in (root / "sdk" / "python" / "tests").glob("test_*.py")
    }
    assert tests <= modules, sorted(tests - modules)
