"""Unit tests for the cheap suffix-strip lemmatizer.

The English coverage is intentionally lossy — we cover the
high-frequency inflections (-s, -es, -ing, -ed, -ier) and accept
imperfection on the long tail. These tests pin the cases that
materially affect memory.search recall."""

from __future__ import annotations

import pytest

from server.memory.domain.lemmatize import lemma_tokens, lemmatize


@pytest.mark.parametrize(
    ("text", "lemma"),
    [
        pytest.param("deploys", "deploy", id="plural to singular"),
        pytest.param("dogs", "dog", id="plural to singular, short stem"),
        pytest.param("deploying", "deploy", id="gerund to stem"),
        pytest.param("deployed", "deploy", id="past tense to stem"),
        pytest.param("queries", "query", id="ies to y"),
        pytest.param("strategies", "strategy", id="ies to y, long stem"),
        pytest.param("happier", "happy", id="ier to y"),
        pytest.param("Deploys", "deploy", id="case insensitive"),
        pytest.param("DEPLOYING", "deploy", id="case insensitive, all caps"),
        # The tokenizer drops punctuation but joins the tokens with spaces, so
        # substring search still works.
        pytest.param("Deploys, Tuesdays.", "deploy tuesday", id="punctuation passes as spaces"),
        # Children doesn't follow the -s rule; it passes through.
        pytest.param("children", "children", id="irregular word passes through"),
        # 3-char words skip suffix-strip ("the" doesn't become "th").
        pytest.param("the", "the", id="short word unchanged"),
        # Single-char tokens are filtered by the token regex (need >=2 chars).
        pytest.param("a", "", id="single char dropped"),
        # Intentional imperfection: "plan" and "plann" both overlap on the "plan"
        # prefix that BM25 and substring matchers use.
        pytest.param("planned", "plann", id="double letter kept"),
        pytest.param("", "", id="empty"),
    ],
)
def test_lemmatize_strips_the_suffixes_of_each_token(text: str, lemma: str) -> None:
    assert lemmatize(text) == lemma


def test_lemma_tokens_dedups() -> None:
    """Repeated forms compress to one canonical token."""
    tokens = lemma_tokens("Deploys deploying deployed deploys")
    assert tokens == ["deploy"]


def test_lemma_tokens_filters_too_short() -> None:
    """Single-char tokens get filtered (no signal value)."""
    assert "a" not in lemma_tokens("a deploy")


def test_lemma_tokens_of_empty_input_is_empty() -> None:
    assert lemma_tokens("") == []
