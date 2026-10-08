"""Tokenizer contract for the code lexical leg.

GATE: index-time and query-time tokenization share ONE function
(server/code/domain/extract/tokenize.py) — these tests pin its behavior so a
change that would silently break byte-parity between stored lexemes and
query tokens fails here first.
"""

from __future__ import annotations

from server.code.domain.extract.tokenize import code_tokens, lexemes_text


def test_whole_identifier_survives() -> None:
    toks = code_tokens("parseQuotaKey")
    assert "parsequotakey" in toks


def test_camel_case_subtokens() -> None:
    toks = code_tokens("parseQuotaKey")
    assert {"parse", "quota", "key"} <= set(toks)


def test_snake_case_subtokens() -> None:
    toks = code_tokens("quota_key_parser")
    assert {"quota", "key", "parser"} <= set(toks)
    assert "quota_key_parser" in toks


def test_acronym_run_splits() -> None:
    toks = code_tokens("HTTPServerConnection")
    assert {"http", "server", "connection"} <= set(toks)


def test_digit_bearing_tokens_survive_whole() -> None:
    # u64 / aes256 style identifiers must be findable verbatim: the WHOLE
    # lowercased identifier is always emitted alongside any subtokens.
    toks = code_tokens("UInt64 aes256gcm sha256sum")
    assert "uint64" in toks
    assert "aes256gcm" in toks
    # digit-boundary subtokens also emitted
    assert {"int", "64", "sha", "256", "sum", "gcm"} <= set(toks)


def test_dedup_first_seen_order() -> None:
    toks = code_tokens("foo foo bar foo")
    assert toks == ["foo", "bar"]


def test_short_tokens_dropped() -> None:
    assert "a" not in code_tokens("a b xy")
    assert "xy" in code_tokens("a b xy")


def test_lexemes_text_is_space_joined_tokens() -> None:
    text = "void insertFrom(const IColumn & src)"
    assert lexemes_text(text) == " ".join(code_tokens(text))


def test_query_side_matches_index_side() -> None:
    # The parity property the BM25 leg depends on: a natural query token
    # set intersects the stored lexemes through the SAME function.
    stored = set(code_tokens("bool tryReserveQuota(size_t bytes)"))
    query = set(code_tokens("reserve quota"))
    assert query <= stored
