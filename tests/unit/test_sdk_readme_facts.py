"""The SDK READMEs state the package facts pyproject, package.json and the code hold."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest
from loom.sync import _CLIENT_ATTRS

from tests.unit.markdown_links import anchors, links
from tests.unit.tracked_files import tracked

ROOT = Path(__file__).resolve().parents[2]
SDK_ROOT = ROOT / "sdk" / "python"
# Tracked files and the directories that hold them, as a link into the repository names them.
TRACKED = {str(p) for f in tracked() for p in [Path(f), *Path(f).parents] if str(p) != "."}
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


@pytest.mark.parametrize(
    ("sdk", "install", "name"),
    [
        ("python", "pip install", PROJECT["name"]),
        (
            "typescript",
            "npm install",
            json.loads((ROOT / "sdk" / "typescript" / "package.json").read_text())["name"],
        ),
    ],
)
def test_the_readme_installs_the_published_package(sdk: str, install: str, name: str) -> None:
    readme = (ROOT / "sdk" / sdk / "README.md").read_text()
    assert re.search(rf"^{install} {re.escape(name)}$", readme, re.M)


def test_the_first_example_compiles_as_a_module() -> None:
    """The example a reader copies first is a whole program: it compiles as a
    module, with no `await` outside a function."""
    use = README[README.index("\n## Use\n") :]
    example = use.split("```python\n", 1)[1].split("\n```", 1)[0]
    compile(example, "README.md", "exec")
    assert "asyncio.run(" in example


@pytest.mark.parametrize("sdk", ["python", "typescript"])
def test_the_package_readme_links_resolve_on_the_registry(sdk: str) -> None:
    """The README is the package's page on PyPI or npm, where a relative link has
    nothing to resolve against: every link is absolute or an in-page anchor, and one
    to a file of this repository names `main` and a path, and an anchor, that exist."""
    targets = links(ROOT / "sdk" / sdk / "README.md")
    assert [t for t in targets if not re.match(r"https?://|#", t)] == []
    in_repo = re.compile(r"https://github\.com/ClickHouse/loom/(?:blob|tree)/([^/]+)/([^#]+)#?(.*)")
    for target in targets:
        if m := in_repo.fullmatch(target):
            ref, path, anchor = m.groups()
            dest = ROOT / path
            assert ref == "main" and path.rstrip("/") in TRACKED, target
            if anchor and dest.suffix == ".md":
                assert anchor.lower() in anchors(dest), target


def _what_loom_is(text: str) -> str:
    """The clause after "Loom, " up to the end of its sentence, with the link to Loom unwrapped."""
    m = re.search(r"Loom(?:\]\([^)]*\))?, (.+?\.)(?:\s|$)", text)
    assert m, f"no 'Loom, <what it is>.' clause in {text!r}"
    return m.group(1)


@pytest.mark.parametrize(
    ("sdk", "description"),
    [
        ("python", PROJECT["description"]),
        (
            "typescript",
            json.loads((ROOT / "sdk" / "typescript" / "package.json").read_text())["description"],
        ),
    ],
)
def test_the_readme_lead_says_what_the_package_description_says(sdk: str, description: str) -> None:
    """The README's first paragraph leads the package's registry page, under the
    description the registry shows as its summary: both say the same of Loom."""
    lead = (ROOT / "sdk" / sdk / "README.md").read_text().split("\n\n")[1]
    assert _what_loom_is(lead) == _what_loom_is(description)
