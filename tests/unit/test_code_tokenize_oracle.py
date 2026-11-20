"""The tokenizer properties (`tokenize_oracle.check`) over generated input, the oracle
catching a broken tokenizer, and the one confirmed failure as a regression."""

from __future__ import annotations

import itertools
import random
import string

import pytest

from server.code.domain.extract import tokenize
from tests.unit.tokenize_oracle import check

# Identifier-shaped pieces the tokenizer splits on, plus separators and non-ASCII.
_PIECES = (
    "parse",
    "Quota",
    "KEY",
    "HTTPServer",
    "sha256sum",
    "u64",
    "_",
    "__",
    "x",
    "X1",
    "aB",
    "ABc",
    "9",
    "0x1F",
    "é",
    "Ünïcode",
    "日本",
    " ",
    "\n",
    ".",
    "->",
    "::",
    "()",
)


def _generated(seed: int, count: int) -> list[str]:
    rng = random.Random(seed)
    alphabet = string.ascii_letters + string.digits + "_ .-:\n\té"
    out: list[str] = []
    for _ in range(count):
        if rng.random() < 0.5:
            out.append("".join(rng.choice(_PIECES) for _ in range(rng.randint(0, 40))))
        else:
            out.append("".join(rng.choice(alphabet) for _ in range(rng.randint(0, 200))))
    return out


@pytest.mark.parametrize("seed", range(4))
def test_the_properties_hold_over_generated_input(seed: int) -> None:
    for text in _generated(seed, 500):
        check(text)


@pytest.mark.parametrize(
    "text",
    ["", "a", "__", "_a_", "HTTPServer", "aaAA", "x" * 5000, "a1" * 300, "日本語"],
    ids=["empty", "a", "underscores", "_a_", "HTTPServer", "aaAA", "x*5000", "a1*300", "cjk"],
)
def test_the_properties_hold_on_edge_cases(text: str) -> None:
    check(text)


def test_a_broken_tokenizer_is_caught() -> None:
    """The controlled failing oracle: a tokenizer that repeats a token, or one whose
    lexemes drift from its tokens, fails `check`."""

    def repeats(text: str) -> list[str]:
        toks = tokenize.code_tokens(text)
        return toks + toks[:1]

    with pytest.raises(AssertionError, match="deduplicated"):
        check("parseQuotaKey", tokens_of=repeats)
    with pytest.raises(AssertionError, match="space-joined"):
        check("parseQuotaKey", lexemes_of=lambda t: "  ".join(tokenize.code_tokens(t)))
    with pytest.raises(AssertionError, match="lowercase"):
        check("parseQuotaKey", tokens_of=lambda t: ["Parse"])


def _cap_input() -> str:
    """511 distinct three-letter words that do not split, then one camelCase
    identifier: the minimized input of the cap failure."""
    words = ("".join(p) for p in itertools.product(string.ascii_lowercase, repeat=3))
    return " ".join(itertools.islice(words, tokenize._MAX_TOKENS - 1)) + " alphaBetaGammaDelta"


def test_the_cap_is_exceeded_by_the_last_identifiers_subtokens() -> None:
    """`code_tokens` checks `_MAX_TOKENS` after an identifier's subtokens are added,
    so the identifier that reaches the cap adds all of them: 511 words and
    `alphaBetaGammaDelta` give 516 tokens. Pinned as it is; the docstring's
    "capped at _MAX_TOKENS" holds only up to one identifier's subtokens."""
    tokens = tokenize.code_tokens(_cap_input())
    assert len(tokens) == tokenize._MAX_TOKENS + 4
    assert tokens[-5:] == ["alphabetagammadelta", "alpha", "beta", "gamma", "delta"]
    with pytest.raises(AssertionError, match="cap"):
        check(_cap_input(), strict_cap=True)
