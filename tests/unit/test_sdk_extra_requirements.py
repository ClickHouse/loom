"""scripts/gates/sdk_extra_requirements.py prints an SDK extra's requirements
for `make test-sdk-adapters`, and refuses an extra the SDK does not declare."""

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
