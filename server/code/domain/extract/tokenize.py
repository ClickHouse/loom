"""Identifier-aware tokenization for the code lexical leg.

The BM25 leg indexes `code_chunks.lexemes`, an app-populated column of
space-joined tokens produced HERE — and the query side calls the SAME
function — so index-time and query-time tokenization agree byte-for-byte
(the lemmatize.py convention). The CH text index over `lexemes` then only
needs to split on spaces.

Splitting rules: an identifier contributes the whole lowercased identifier
plus its camelCase / snake_case / digit-boundary subtokens, so a query for
"parse quota" matches `parseQuotaKey` and `quota_key_parser` while an exact
`parseQuotaKey` query still matches whole. Digit-bearing tokens (`u64`,
`aes256`) survive because we never split on the digits themselves, only at
letter<->digit boundaries — and the whole identifier keeps them verbatim.
"""

from __future__ import annotations

import re

# Whole identifiers / words: letters, digits, underscores.
_IDENT_RE = re.compile(r"[A-Za-z0-9_]+")

# Subtoken boundaries inside one identifier:
#   snake_case          -> split on '_'
#   camelCase / Pascal  -> split before an upper following a lower/digit,
#                          and before the last upper of an acronym run
#                          (HTTPServer -> HTTP, Server)
#   letter<->digit edge -> split (sha256sum -> sha, 256, sum)
_CAMEL_RE = re.compile(
    r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+"
)

_MIN_TOKEN_LEN = 2
_MAX_TOKENS = 512


def _subtokens(ident: str) -> list[str]:
    parts: list[str] = []
    for piece in ident.split("_"):
        if not piece:
            continue
        for m in _CAMEL_RE.finditer(piece):
            tok = m.group(0)
            # letter<->digit boundary split within a run like "sha256"
            for sub in re.findall(r"[A-Za-z]+|[0-9]+", tok):
                parts.append(sub)
    return parts


def code_tokens(text: str) -> list[str]:
    """Tokenize code or a code-shaped query into lowercased lexemes.

    Returns whole identifiers plus subtokens, deduplicated in first-seen
    order, capped at _MAX_TOKENS (a chunk is at most a few KB; the cap only
    guards pathological minified input).
    """
    seen: set[str] = set()
    out: list[str] = []

    def _add(tok: str) -> None:
        tok = tok.lower()
        if len(tok) < _MIN_TOKEN_LEN:
            return
        if tok in seen:
            return
        seen.add(tok)
        out.append(tok)

    for m in _IDENT_RE.finditer(text):
        ident = m.group(0)
        _add(ident)
        subs = _subtokens(ident)
        # Only add subtokens when the identifier actually split — otherwise
        # they'd duplicate the whole token (already added above).
        if len(subs) > 1:
            for sub in subs:
                _add(sub)
        if len(out) >= _MAX_TOKENS:
            break
    return out


def lexemes_text(text: str) -> str:
    """The exact string stored in code_chunks.lexemes: space-joined tokens."""
    return " ".join(code_tokens(text))


def compound_identifiers(text: str) -> set[str]:
    """The lowercased WHOLE forms of the identifiers in `text` that split into
    more than one subtoken (`parseQuotaKey` -> {"parsequotakey"}). Query-side
    morphology (morphology.py) leaves these alone: the whole
    form of a camel/snake identifier is not a word, only its subtokens are."""
    out: set[str] = set()
    for m in _IDENT_RE.finditer(text):
        ident = m.group(0)
        if len(_subtokens(ident)) > 1:
            out.add(ident.lower())
    return out
