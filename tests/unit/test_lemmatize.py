"""Unit tests for the cheap suffix-strip lemmatizer.

The English coverage is intentionally lossy — we cover the
high-frequency inflections (-s, -es, -ing, -ed, -ier) and accept
imperfection on the long tail. These tests pin the cases that
materially affect memory.search recall."""

from __future__ import annotations

from server.memory.domain.lemmatize import lemma_tokens, lemmatize


def test_plural_to_singular() -> None:
    assert lemmatize("deploys") == "deploy"
    assert lemmatize("dogs") == "dog"


def test_gerund_to_stem() -> None:
    assert lemmatize("deploying") == "deploy"


def test_past_tense_to_stem() -> None:
    assert lemmatize("deployed") == "deploy"


def test_ies_to_y() -> None:
    assert lemmatize("queries") == "query"
    assert lemmatize("strategies") == "strategy"


def test_ier_to_y() -> None:
    assert lemmatize("happier") == "happy"


def test_case_insensitive() -> None:
    assert lemmatize("Deploys") == "deploy"
    assert lemmatize("DEPLOYING") == "deploy"


def test_punctuation_passes_through_as_spaces() -> None:
    # Tokenizer doesn't preserve punctuation but should still join
    # tokens with spaces so substring search works.
    assert lemmatize("Deploys, Tuesdays.") == "deploy tuesday"


def test_irregular_words_pass_through() -> None:
    # Children doesn't follow the -s rule; we accept passing through.
    assert lemmatize("children") == "children"


def test_short_words_unchanged() -> None:
    # 3-char words skip suffix-strip ("the" doesn't become "th").
    assert lemmatize("the") == "the"
    # Single-char tokens are filtered by the token regex (need >=2 chars).
    assert lemmatize("a") == ""


def test_double_letters_intentional_imperfection() -> None:
    """planned → plann (we accept the noise; "plan" + "plann" both
    overlap on the "plan" prefix that BM25/substring matchers use)."""
    # This documents the intentional behavior; not changing it.
    assert lemmatize("planned") == "plann"


def test_lemma_tokens_dedups() -> None:
    """Repeated forms compress to one canonical token."""
    tokens = lemma_tokens("Deploys deploying deployed deploys")
    assert tokens == ["deploy"]


def test_lemma_tokens_filters_too_short() -> None:
    """Single-char tokens get filtered (no signal value)."""
    assert "a" not in lemma_tokens("a deploy")


def test_empty_input() -> None:
    assert lemmatize("") == ""
    assert lemma_tokens("") == []
