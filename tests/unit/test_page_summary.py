"""A page's title and first sentence, which llms.txt and the documentation site both use."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.codegen.page_summary import first_sentence, page_summary


@pytest.mark.parametrize(
    ("text", "first"),
    [
        ("Turns it on. Read the guide.", "Turns it on."),
        ("Sets a label (e.g. `iso:x`). Every process sets one.", "Sets a label (e.g. `iso:x`)."),
        ("E.g. `ci` or `iso`. Every process sets one.", "E.g. `ci` or `iso`."),
        ("Reads one value, i.e. `x`. Then more.", "Reads one value, i.e. `x`."),
        ("Rows, keys, etc. Writes stay grant-driven.", "Rows, keys, etc."),
    ],
)
def test_first_sentence_skips_e_g_and_i_e_but_may_end_at_etc(text, first) -> None:
    assert first_sentence(text) == first


def test_the_summary_skips_tables_lists_and_code_and_drops_link_targets(tmp_path: Path) -> None:
    page = tmp_path / "page.md"
    page.write_text(
        "# The `thing` page\n\n| a | b |\n\n- a list\n\n```\ncode. More.\n```\n\n"
        "The [thing](other.md) answers one question. Then more.\n"
    )
    assert page_summary(page) == ("The thing page", "The thing answers one question.")
