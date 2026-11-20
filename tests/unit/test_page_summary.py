"""A page's title and first sentence, which llms.txt and the documentation site both use."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.codegen.page_summary import first_sentence, page_key, page_summary, shorten


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


def test_a_long_link_target_does_not_push_the_cut_inside_the_link(tmp_path: Path) -> None:
    page = tmp_path / "page.md"
    target = "../" + "very-long-segment/" * 20 + "page.md"
    words = " ".join(["word"] * 40)
    page.write_text(
        f"# Page\n\nThe {words} [configuration reference]({target}) and more words follow.\n"
    )
    _title, summary = page_summary(page)
    assert "[" not in summary and "](" not in summary
    assert len(summary) <= 240


@pytest.mark.parametrize(
    ("text", "limit", "cut"),
    [
        ("one two three", 20, "one two three"),  # it fits
        ("one two three", 10, "one two…"),  # the slice ends inside "three"
        ("one two three four", 14, "one two three…"),  # the slice ends on a whole word
        ("one two three, four", 14, "one two three…"),  # a word that ends at a comma fits
        ("abcdefghij", 5, "abcd…"),  # a first word longer than the limit is cut
        ("one two/ three", 10, "one two…"),  # a trailing slash goes with the cut
    ],
)
def test_shorten_keeps_whole_words_from_the_start(text, limit, cut) -> None:
    assert shorten(text, limit) == cut


def test_page_key_orders_a_section_as_the_sidebar_does() -> None:
    uris = [
        "features/integrations/grep-shim.md",
        "features/zeta.md",
        "features/integrations/index.md",
        "features/index.md",
        "features/alpha.md",
        "features/code-index/overview.md",
        "features/code-index/freshness.md",
        "getting-started/evaluate.md",
        "getting-started/quickstart.md",
        "getting-started/overview.md",
    ]
    features = sorted((u for u in uris if u.startswith("features/")), key=page_key)
    assert features == [
        "features/index.md",
        "features/alpha.md",
        "features/zeta.md",
        # A folder without an index.md is led by its overview.md.
        "features/code-index/overview.md",
        "features/code-index/freshness.md",
        "features/integrations/index.md",
        "features/integrations/grep-shim.md",
    ]
    start = sorted((u for u in uris if u.startswith("getting-started/")), key=page_key)
    assert start == [
        "getting-started/overview.md",
        "getting-started/quickstart.md",
        "getting-started/evaluate.md",
    ]


def test_no_docs_folder_has_both_an_index_and_an_overview() -> None:
    # page_key leads a folder with either one; both would tie.
    docs = Path(__file__).resolve().parents[2] / "docs"
    both = [p.parent for p in docs.rglob("overview.md") if (p.parent / "index.md").exists()]
    assert both == []
