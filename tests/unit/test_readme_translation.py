"""README.zh-CN.md carries the English README's measured numbers, commands and caveat code names."""

from __future__ import annotations

import re
from collections import Counter
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


def test_the_chinese_readme_shows_the_same_commands() -> None:
    english = (ROOT / "README.md").read_text()
    chinese = (ROOT / "README.zh-CN.md").read_text()
    install = re.compile(r"curl -fsSL \S+install\.sh[^\n]*")
    assert install.findall(english) and install.findall(chinese) == install.findall(english)[:1]
    for lines in (
        re.compile(r"^(?:claude|codex) plugin [^\n]+$", re.MULTILINE),
        re.compile(r"^docker compose [^\n]+$", re.MULTILINE),
    ):
        assert lines.findall(english) and lines.findall(chinese) == lines.findall(english)


def _results_prose(readme: str, heading: str) -> str:
    return readme.split(heading, 1)[1].split("\n\n", 2)[2].split("\n## ", 1)[0]


def test_the_results_caveats_carry_the_same_numbers_and_code_names() -> None:
    english = _results_prose((ROOT / "README.md").read_text(), "## Measured results")
    chinese = _results_prose((ROOT / "README.zh-CN.md").read_text(), "## 实测结果")
    # The sentences order their numbers differently, so they compare as multisets.
    assert Counter(NUMBER.findall(chinese)) == Counter(NUMBER.findall(english))
    code_spans = re.compile(r"`([a-z0-9.-]+)`")
    assert set(code_spans.findall(chinese)) == set(code_spans.findall(english))
