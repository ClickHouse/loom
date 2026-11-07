"""The strict reader of the floor tables in docs/architecture/document-extraction.md."""

from __future__ import annotations

import pytest

from tests.fixtures.floor_tables import read_floors

HEAD = "\n## Floors\n\n| Slice | Metric | Floor |\n| --- | --- | --- |\n"


@pytest.mark.parametrize(
    ("rows", "why"),
    [
        ("| identifiers | found at 1 | 1 |\n", "not a row"),
        ("| identifiers | found at 1 | 1.00 | extra |\n", "not a row"),
        ("| identifiers | found at 1 |\n", "not a row"),
        ("| identifiers | found at 1 | 1.00 |\n| identifiers | found at 1 | 0.90 |\n", "repeated"),
        ("", "no floors"),
    ],
)
def test_a_table_it_cannot_read_whole_is_an_error(rows: str, why: str) -> None:
    with pytest.raises(ValueError, match=why):
        read_floors(HEAD + rows, "## Floors", 3)


def test_a_row_is_read_as_its_key_and_floor_up_to_the_next_heading() -> None:
    page = HEAD + "| identifiers | found at 1 | 0.975 |\n\n### Other\n\n| a | b | c | d |\n"
    assert read_floors(page, "## Floors", 3) == {("identifiers", "found at 1"): 0.975}


def test_a_missing_heading_is_an_error() -> None:
    with pytest.raises(ValueError, match="no '## Floors' heading"):
        read_floors("\n## Other\n", "## Floors", 3)
