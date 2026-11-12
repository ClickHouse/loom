"""README.zh-CN.md carries the English README's measured numbers and install command."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _results_table(readme: str) -> str:
    section = readme.split("## Measured results", 1)[1]
    return section.split("\n\n", 2)[1]


def test_every_measured_number_appears_in_the_chinese_readme() -> None:
    english = (ROOT / "README.md").read_text()
    chinese = (ROOT / "README.zh-CN.md").read_text()
    numbers = set(re.findall(r"\d+(?:\.\d+)?%?", _results_table(english)))
    assert numbers, "no numbers found in the README's measured-results table"
    missing = sorted(n for n in numbers if n not in chinese)
    assert not missing, f"README.zh-CN.md lacks the README's measured numbers {missing}"


def test_the_chinese_readme_shows_the_same_install_command() -> None:
    command = re.compile(r"curl -fsSL \S+install\.sh[^\n]*")
    english = command.findall((ROOT / "README.md").read_text())
    chinese = command.findall((ROOT / "README.zh-CN.md").read_text())
    assert english and chinese == english[:1]
