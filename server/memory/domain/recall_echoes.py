"""Recall echoes: an extracted fact that only repeats a memory key the session was served."""

from __future__ import annotations

import re
from typing import Any

# A fact that names a memory key of the namespace is the
# assistant recalling that memory, not something the session learned. A recall
# turn's answer ("You live in Ujjain, saved in Loom as `where_i_live`") extracted
# as `user_home_location` would outrank the key it restated and keep answering
# the old value after the key was edited.
_KEY_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_])([A-Za-z][A-Za-z0-9]*(?:[_:][A-Za-z0-9]+)+)(?![A-Za-z0-9_])"
)


def key_tokens(text: str) -> list[str]:
    """Identifier-shaped tokens of `text` — a word with an underscore or colon
    joint, six or more characters — the shape a memory key has in prose."""
    out: list[str] = []
    for m in _KEY_TOKEN_RE.finditer(str(text or "")):
        tok = m.group(1)
        if len(tok) >= 6 and tok not in out:
            out.append(tok)
    return out[:32]


# Words a key carries that say nothing about its subject: `user_coffee_type`
# and `coffee_type` are the same key to a reader.
_GENERIC_KEY_WORDS = frozenset({"user", "users", "my", "the", "a", "an", "of", "is", "s", "info"})


def key_words(key: str) -> set[str]:
    """The subject words of a memory key: split on the joints, generic words
    dropped — `user_coffee_type` → {coffee, type}."""
    words = re.split(r"[_:./\-]+", str(key or "").lower())
    return {w for w in words if w and w not in _GENERIC_KEY_WORDS}


def recall_echoes(
    facts: list[dict[str, Any]],
    existing_keys: set[str],
    *,
    recalled_keys: frozenset[str] | set[str] = frozenset(),
) -> list[int]:
    """Indices of `facts` that echo a recall: the value names a key in
    `existing_keys`, or the session's own words named a key in
    `recalled_keys` and the fact's key carries that key's subject words —
    `user_coffee_type` after the reply said "saved under `coffee_type`".
    A fact that IS the key is a rewrite, not an echo, and is kept."""
    hits: list[int] = []
    for i, fact in enumerate(facts):
        own = str(fact.get("memory_key") or "")
        named = [
            t for t in key_tokens(str(fact.get("value") or "")) if t in existing_keys and t != own
        ]
        if named:
            hits.append(i)
            continue
        words = key_words(own)
        for k in recalled_keys:
            kw = key_words(k)
            if k != own and kw and kw <= words:
                hits.append(i)
                break
    return hits
