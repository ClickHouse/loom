"""A definition's names: its `term` and `aliases`, compared in their folded form. Pure.

A name is folded by dropping format characters (Unicode category Cf, such as a pasted
byte-order mark), then `canonical_document_text` (NFKC, trimmed, every run of whitespace
made one space), then `str.casefold`, so `SAT`, `Sat` and `sat ` are one name.
"""

from __future__ import annotations

import json
import unicodedata
from typing import Any

from server.core.hashing import canonical_document_text

KEY_PREFIX = "definition/"


def fold_name(name: str) -> str:
    """`name` in the form two names are compared in."""
    kept = "".join(c for c in name if unicodedata.category(c) != "Cf")
    return canonical_document_text(kept).casefold()


def definition_names(value: Any) -> set[str] | None:
    """The folded term and aliases of a definition's value (a JSON object, or its text),
    or None when it is not one: no object, no non-empty string `term`, or `aliases`
    present and not a list of strings (it may be left out)."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    if not isinstance(value, dict) or not isinstance(value.get("term"), str):
        return None
    aliases = value.get("aliases", [])
    if not isinstance(aliases, list) or not all(isinstance(a, str) for a in aliases):
        return None
    term = fold_name(value["term"])
    if not term:
        return None
    return ({term} | {fold_name(a) for a in aliases}) - {""}
