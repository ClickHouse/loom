"""A definition's names: its `term` and `aliases`, compared in their folded form. Pure.

A name is folded by dropping format characters (Unicode category Cf, such as a pasted
byte-order mark), then `canonical_document_text` (NFKC, trimmed, every run of whitespace
made one space), then `str.casefold`, so `SAT`, `Sat` and `sat ` are one name.
"""

from __future__ import annotations

import unicodedata
from typing import Any

from server.core.hashing import canonical_document_text

DEFINITION_KEY_PREFIX = "definition/"


def fold_name(name: str) -> str:
    """`name` in the form two names are compared in."""
    kept = "".join(c for c in name if unicodedata.category(c) != "Cf")
    return canonical_document_text(kept).casefold()


def definition_names(value: Any) -> set[str] | None:
    """The folded term and aliases of a decoded definition value, or None when it is not
    one: no object, no non-empty string `term`, or `aliases` present and not a list of
    strings (it may be left out)."""
    if not isinstance(value, dict) or not isinstance(value.get("term"), str):
        return None
    aliases = value.get("aliases", [])
    if not isinstance(aliases, list) or not all(isinstance(a, str) for a in aliases):
        return None
    return names_of(value["term"], aliases) or None


def names_of(term: str, aliases: list[str]) -> set[str]:
    """The folded term and aliases, an empty set when the term folds to nothing."""
    folded = fold_name(term)
    if not folded:
        return set()
    return ({folded} | {fold_name(a) for a in aliases}) - {""}
