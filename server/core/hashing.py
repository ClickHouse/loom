"""Canonical SHA-256 helpers for memory value hashes.

Two call sites need a hash of a memory value:
- The handler that writes / reads memory rows (hash of a Python value).
- The conflict resolver (hash of an already-serialized JSON string).

Both must agree byte-for-byte — they're compared on the wire as the
`if_match_sha256` / `content_sha256` precondition. Canonicalization
(`sort_keys=True`, compact separators) lives here so the two sides
can't drift.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def sha256_text(text: str) -> str:
    """Hex SHA-256 of `text` as UTF-8."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_value(value: Any) -> str:
    """Hash of `json.dumps(value, sort_keys=True, compact)`."""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8"),
    ).hexdigest()


def sha256_value_json(value_json: str) -> str:
    """Hash of an already-serialized JSON string.

    Re-parses + re-serializes with sort_keys so a row written without
    sort_keys still produces the same hash as the client-side computation.
    Falls back to hashing the raw bytes when the string isn't valid JSON
    (shouldn't happen in practice; defensive).
    """
    try:
        return sha256_value(json.loads(value_json))
    except json.JSONDecodeError:
        return hashlib.sha256(value_json.encode("utf-8")).hexdigest()


def served_content_sha256(value: Any) -> str:
    """The content hash memory.get serves for a decoded value: '' for none."""
    return "" if value is None else sha256_value_json(json.dumps(value, default=str))


def canonical_document_text(text: str) -> str:
    """Canonicalize a document for hash dedup: NFKC, strip, collapse whitespace.

    Callers pass `f"{title}\n{content}"` for a document and the bare text for
    a chunk."""
    import unicodedata

    norm = unicodedata.normalize("NFKC", text.strip())
    return " ".join(norm.split())


def sha256_document_text(text: str) -> str:
    """The `content_sha256` of a loom.documents / loom.chunks row: hex SHA-256
    of the canonicalized text. Empty in → empty out (callers short-circuit
    dedup queries on content-less rows)."""
    if not text:
        return ""
    return sha256_text(canonical_document_text(text))


def hash_text(value: object) -> str:
    """A content hash as read back from ClickHouse, as text.

    `content_sha256` columns are FixedString(64); the native client returns
    them as `bytes`, and `str(b"…")` is the repr `"b'…'"`, which never equals
    the hex the write path computed. Every read that compares a stored hash
    goes through here."""
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", "replace").rstrip("\x00")
    return str(value)
