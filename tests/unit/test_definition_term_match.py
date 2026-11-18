"""Where a query names a definition's term: the term leg's matching rules."""

from __future__ import annotations

import pytest

from server.core.hashing import query_text
from server.memory.domain.definitions import Name, joined_chars, name_start


def _start(query: str, name: str) -> int | None:
    chars = joined_chars(query_text(query))
    return name_start(chars, Name.of(name), "".join(chars).casefold())


def _names(query: str, name: str) -> bool:
    return _start(query, name) is not None


@pytest.mark.parametrize(
    ("query", "name", "hit"),
    [
        ("what is our ARR", "ARR", True),
        ("arrow keys", "ARR", False),
        ("what is our arr", "ARR", False),
        ("what is it", "IT", False),
        ("C++ style guide", "C++", True),
        ("C++ style guide", "C", False),
        ("C# and .NET", ".NET", True),
        ("what is our churn rate", "Churn Rate", True),
        ("what is our churn  rate?", "churn rate", True),
        ("GitHub issues", "GitHub", True),
        ("github issues", "GitHub", False),
        ("ARR_daily table", "ARR", False),
        ("ARR2 table", "ARR", False),
        ("(ARR)", "ARR", True),
        ("什么是MRR", "MRR", True),
        ("什么是ＭＲＲ", "MRR", True),
        ("MRR​の定義", "MRR", True),
        ("ARR은 무엇인가", "ARR", True),
        ("매출은 얼마야", "매출", True),
        ("판매출고 현황", "매출", False),
        ("простой вопрос", "рост", False),
        ("какой рост?", "рост", True),
        ("Рост выручки", "рост", True),
        ("рост2024", "рост", False),
        ("ARRоборот", "ARR", True),
        ("कीमत", "मत", False),
        ("कमी", "कम", False),
        ("बिक्रीकर", "बिक्री", False),
        ("बिक्री कितनी है", "बिक्री", True),
        ("ดูยอดขาย", "ยอดขาย", True),
        ("กิ", "ก", False),
        ("年度经常性收入是什么", "经常性收入", True),
        ("cafés nearby", "café", False),
        ("le café?", "café", True),
        ("le cafe\u0301?", "café", True),  # é as e and a combining accent
        ("ARR Growth this year", "ARR growth", True),
        ("arr growth this year", "ARR growth", False),
        ("Straße", "Straße", True),
        ("strasse", "Straße", False),  # a fold that changes length matches only as written
        ("", "ARR", False),
        ("ARR", "", False),
    ],
)
def test_a_query_names_a_term_only_as_a_whole_word(query: str, name: str, hit: bool) -> None:
    assert _names(query, name) is hit


def test_a_mark_joins_the_letter_before_it() -> None:
    assert joined_chars("कमी") == ["क", "मी"]
    assert joined_chars("́a") == ["́", "a"]


def test_the_start_is_the_first_place_the_name_stands_alone() -> None:
    assert _start("arrows and ARR, then ARR", "ARR") == 11
