"""The Python SDK README states the package facts pyproject and the code hold."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from loom.sync import _CLIENT_ATTRS

SDK_ROOT = Path(__file__).resolve().parents[2] / "sdk" / "python"
README = (SDK_ROOT / "README.md").read_text()
PROJECT = tomllib.loads((SDK_ROOT / "pyproject.toml").read_text())["project"]


def test_readme_states_the_python_floor_and_runtime_deps() -> None:
    m = re.search(r"^Python (\d+\.\d+)\+\. Runtime deps: (.+)$", README, re.M)
    assert m, "README lost its 'Python X.Y+. Runtime deps: ...' line"
    assert PROJECT["requires-python"] == f">={m.group(1)}"
    declared = {re.split(r"[<>=!~;\s]", dep, maxsplit=1)[0] for dep in PROJECT["dependencies"]}
    assert set(re.findall(r"`([^`]+)` \(", m.group(2))) == declared


def test_readme_lists_every_agent_client() -> None:
    m = re.search(r"The `Agent` exposes (\d+) client modules: (.+?)\. Each", README)
    assert m, "README lost its client-module sentence"
    listed = re.findall(r"`(\w+)`", m.group(2))
    assert int(m.group(1)) == len(listed)
    assert sorted(listed) == sorted(_CLIENT_ATTRS)
