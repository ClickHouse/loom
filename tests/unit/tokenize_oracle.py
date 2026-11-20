"""Properties the code tokenizer keeps for any input, shared by the unit tests and the
coverage-guided harness (`scripts/fuzz/tokenize_atheris.py`).

`check(text)` raises `AssertionError` naming the property that fails. Each property is
one the index relies on: the stored `lexemes` string and the query tokens come from
the same function, so they must agree byte for byte.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from server.code.domain.extract import tokenize

_TOKEN = re.compile(r"[a-z0-9_]+")

# Inputs are cut to this many characters before checking, so one case stays cheap.
INPUT_CEILING = 4096


def check(
    text: str,
    *,
    tokens_of: Callable[[str], list[str]] = tokenize.code_tokens,
    lexemes_of: Callable[[str], str] = tokenize.lexemes_text,
    strict_cap: bool = False,
) -> None:
    """Assert every property for `text`. `strict_cap` adds the documented cap
    (`len(tokens) <= _MAX_TOKENS`), which the current tokenizer can exceed: see
    `test_the_cap_is_exceeded_by_the_last_identifiers_subtokens`."""
    text = text[:INPUT_CEILING]
    tokens = tokens_of(text)
    assert tokens == tokens_of(text), "deterministic: two calls differ"
    assert len(tokens) == len(set(tokens)), "deduplicated: a token repeats"
    for tok in tokens:
        assert tok == tok.lower(), f"lowercase: {tok!r}"
        assert _TOKEN.fullmatch(tok), f"token alphabet: {tok!r}"
        assert len(tok) >= tokenize._MIN_TOKEN_LEN, f"minimum length: {tok!r}"
    lexemes = lexemes_of(text)
    assert lexemes == " ".join(tokens), "lexemes_text is the space-joined token list"
    assert (lexemes.split(" ") if lexemes else []) == tokens, "lexemes split back to tokens"
    if strict_cap:
        assert len(tokens) <= tokenize._MAX_TOKENS, f"cap: {len(tokens)} tokens"
