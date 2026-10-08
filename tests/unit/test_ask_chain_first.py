"""A multi-hop question's first row hop follows the chain's own rows: the row
whose subject the question names, then the rows that row names, before an
unrelated top hit's objects take the hop's prefix slots."""

from __future__ import annotations

from types import SimpleNamespace as Row

from server.memory.app.ask.completion import _key_subject, chain_first, row_object_prefixes

Q = "What is the capital city of the country where the sport associated with John Harkes originated?"


def _rows() -> list[Row]:
    return [
        Row(
            memory_key="fc_vorskla_poltava_sport",
            content_excerpt="FC Vorskla Poltava is associated with basketball in Great Britain.",
        ),
        Row(
            memory_key="association_football_created_in",
            content_excerpt="Association football was created in the country of Italy.",
        ),
        Row(
            memory_key="john_harkes_sport",
            content_excerpt="John Harkes is associated with the sport of association football.",
        ),
    ]


def test_the_chain_rows_come_first_in_chain_order() -> None:
    keys = [r.memory_key for r in chain_first(_rows(), Q)]
    assert keys == [
        "john_harkes_sport",
        "association_football_created_in",
        "fc_vorskla_poltava_sport",
    ]


def test_the_hop_takes_the_chain_object_before_an_unrelated_one() -> None:
    assert row_object_prefixes(chain_first(_rows(), Q), max_prefixes=1) == ["italy"]


def test_key_subject_is_the_longest_named_prefix() -> None:
    text = "john harkes is associated with the sport of association football"
    assert _key_subject("association_football_created_in", text) == "association football"
    assert _key_subject("italy_capital", text) == ""
