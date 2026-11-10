"""The SDK READMEs state the package facts pyproject, package.json and the code hold."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest
from loom.sync import _CLIENT_ATTRS

from tests.unit.test_doc_links import _anchors, _links

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


def test_readme_installs_the_published_package() -> None:
    assert re.search(rf"^pip install {re.escape(PROJECT['name'])}$", README, re.M)


def test_the_first_example_compiles_as_a_module() -> None:
    """The example a reader copies first is a whole program: it compiles as a
    module, with no `await` outside a function."""
    use = README[README.index("\n## Use\n") :]
    example = use.split("```python\n", 1)[1].split("\n```", 1)[0]
    compile(example, "README.md", "exec")
    assert "asyncio.run(" in example


def test_the_typescript_readme_installs_the_published_package() -> None:
    ts_root = SDK_ROOT.parent / "typescript"
    name = json.loads((ts_root / "package.json").read_text())["name"]
    readme = (ts_root / "README.md").read_text()
    assert re.search(rf"^npm install {re.escape(name)}$", readme, re.M)


@pytest.mark.parametrize("sdk", ["python", "typescript"])
def test_the_package_readme_links_resolve_on_the_registry(sdk: str) -> None:
    """The README is the package's page on PyPI or npm, where a relative link has
    nothing to resolve against: every link is absolute, and one into this
    repository names a path that exists."""
    targets = _links(SDK_ROOT.parent / sdk / "README.md")
    assert [t for t in targets if not re.match(r"https?://|#", t)] == []
    repo = "https://github.com/ClickHouse/loom/"
    for target in targets:
        if target.startswith(repo):
            path, _, anchor = re.sub(r"^(?:blob|tree)/main/", "", target[len(repo) :]).partition(
                "#"
            )
            dest = SDK_ROOT.parents[1] / path
            assert dest.exists(), target
            if anchor and dest.suffix == ".md":
                assert anchor.lower() in _anchors(dest), target
