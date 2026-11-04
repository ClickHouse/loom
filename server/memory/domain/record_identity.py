"""Derived record identity: the memory_key a typed record gets when its
schema declares identity fields and the writer supplies no key.

Two extractions of the same invoice from two sessions must land on ONE
logical record, so the second supersedes the first (memory_records already
does that — keyed on memory_key). A caller-chosen key cannot give that
guarantee; a key derived from the record's own identity values can.

Canonicalisation is per field kind, because one global normaliser both
over- and under-merges:
  name-like fields (name, title, label, and *_name / *_title / *_label) — NFKD, strip
    combining marks, drop a leading article and a possessive, split on
    whitespace / '_' / '-', join with '_', uppercase: "The Vendor's name"
    and "vendor name" agree.
  every other field — NFKD, casefold, keep [a-z0-9] only: "INV-2024/001",
    "inv 2024 001" and "inv_2024_001" agree, and digits survive so
    "run-1" and "run-2" do not. A value with none of a-z, 0-9 (a scenario in
    Cyrillic, CJK or Arabic) keeps instead every letter, digit and combining
    mark of the raw value, after NFKC and casefold: no mark is stripped, as
    Indic vowel signs, Thai tone marks, the Japanese voicing mark and
    Arabic and Hebrew points tell words apart. Only such values take that
    form, so every key with an a-z, 0-9 form stays as it was; a value of
    punctuation, symbols or marks alone has neither and is empty.
Lists sort. The key is blake2b over the schema name plus the sorted
(field, canonical value) pairs, 16 hex characters, prefixed by the schema
name so keys never collide across schemas and read as what they are.

The same function must re-derive the key from a stored record_json: that
parity is the whole contract, and tests pin it.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any

_NAME_LIKE = ("name", "title", "label")
_ARTICLES = ("the ", "a ", "an ")
_POSSESSIVE = re.compile(r"['’]s\b", re.IGNORECASE)
_SPLIT = re.compile(r"[\s_\-]+")
_NON_ALNUM = re.compile(r"[^a-z0-9]")
KEY_HEX = 16


def is_name_like(field_name: str) -> bool:
    f = (field_name or "").lower()
    return f in _NAME_LIKE or any(f.endswith("_" + n) for n in _NAME_LIKE)


def _strip_marks(s: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKD", s) if not unicodedata.combining(ch))


def canonical_value(field_name: str, value: Any) -> str:
    """Canonical text for one identity value; "" when the value is empty."""
    if value is None:
        return ""
    if isinstance(value, list | tuple | set):
        parts = sorted(canonical_value(field_name, v) for v in value)
        return "|".join(p for p in parts if p)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        text = str(int(value)) if float(value).is_integer() else repr(float(value))
    else:
        text = str(value)
    raw = text
    text = _strip_marks(text).strip()
    if not text:
        return ""
    if is_name_like(field_name):
        low = text.lower()
        for a in _ARTICLES:
            if low.startswith(a):
                text = text[len(a) :]
                break
        text = _POSSESSIVE.sub("", text)
        return "_".join(p for p in _SPLIT.split(text) if p).upper()
    ascii_form = _NON_ALNUM.sub("", text.casefold())
    if ascii_form:
        return ascii_form
    # From the raw value, not the mark-stripped text: in these scripts a mark tells words
    # apart (かぎ and かき, ไม่ and ไม้, क्या and कया).
    folded = unicodedata.normalize("NFKC", raw).casefold()
    if not any(ch.isalnum() for ch in folded):
        return ""
    return "".join(ch for ch in folded if ch.isalnum() or unicodedata.category(ch).startswith("M"))


def canonical_record_key(schema_name: str, identity_values: dict[str, Any]) -> str:
    """`<schema>:<blake2b16>` over sorted (field, canonical value) pairs.
    Raises ValueError when any identity value canonicalises to "" — an
    identity is never repaired into existence."""
    pairs: list[tuple[str, str]] = []
    for field, value in sorted(identity_values.items()):
        c = canonical_value(field, value)
        if not c:
            raise ValueError(f"identity field {field!r} is empty")
        pairs.append((field, c))
    payload = json.dumps([schema_name, pairs], sort_keys=True, separators=(",", ":"))
    digest = hashlib.blake2b(payload.encode("utf-8"), digest_size=8).hexdigest()
    return f"{schema_name}:{digest[:KEY_HEX]}"
