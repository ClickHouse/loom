"""README.zh-CN.md carries the English README's measured numbers and install command."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NUMBER = re.compile(r"\d+(?:\.\d+)?%?")


def _results_table(readme: str, heading: str) -> str:
    table = readme.split(heading, 1)[1].split("\n\n", 2)[1]
    assert table.startswith("|"), f"no table right under {heading!r}"
    return table


def test_the_chinese_results_table_has_the_english_numbers_in_order() -> None:
    english = _results_table((ROOT / "README.md").read_text(), "## Measured results")
    chinese = _results_table((ROOT / "README.zh-CN.md").read_text(), "## 实测结果")
    assert NUMBER.findall(chinese) == NUMBER.findall(english)


def test_the_chinese_readme_shows_the_same_install_command() -> None:
    command = re.compile(r"curl -fsSL \S+install\.sh[^\n]*")
    english = command.findall((ROOT / "README.md").read_text())
    chinese = command.findall((ROOT / "README.zh-CN.md").read_text())
    assert english and chinese == english[:1]
