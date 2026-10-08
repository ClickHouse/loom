"""Say when a `memory.set` value has a shape search cannot reach.

Three silent shapes: base64; a JSON document serialized INSIDE a JSON string (`json.dumps`
twice), whose escaped text is what gets embedded, with the claim field
sorted past the 1,200-char excerpt clip; and a value past the embedder's
input cap, which gets no embedding row at all. Both writes succeed (a value
is a value); the response carries one advisory line per shape so the agent
can correct itself in-band.

Cheap by construction: strings under 64 characters are never parsed, the
double-encoding check is one JSON parse of at most 256 KB, and the size
check is one `len()` on the serialization the write path already does.
"""

from __future__ import annotations

import json
from typing import Any

from server.memory.domain.memory_types import is_searchable_type
from server.memory.domain.value_encoding import encoded_value_advisory
from server.settings import settings

_MIN_LEN = 64
_MAX_JSON_PARSE = 256 * 1024
# server/memory/domain/excerpt.py EXCERPT_LIMIT: what the fact embedding sees.
_EXCERPT_CHARS = 1200

DOUBLE_ENCODED_ADVISORY = (
    "value is a JSON {kind} serialized inside a JSON string (double-encoded): the string "
    "form embeds escaped JSON and the claim can fall past the {excerpt}-char excerpt clip "
    "— send the object itself (memory.set accepts any JSON value) or, better, a plain-text "
    "claim with the structure in `tags` / `entities`"
)

OVERSIZED_ADVISORY = (
    "value is {size:,} characters: only the first {excerpt} are embedded as the fact, and a "
    "value over the embedder's input cap is not embedded at all — write the claim as the "
    "value and put the document in knowledge.add (chunked, hybrid-searchable)"
)


def _parsed_document(value: Any) -> dict[str, Any] | list[Any] | None:
    """The dict or list a string value parses to, else None — capped at 256 KB."""
    if not isinstance(value, str) or len(value) < _MIN_LEN:
        return None
    stripped = value.strip()
    if not stripped.startswith(("{", "[")) or len(stripped) > _MAX_JSON_PARSE:
        return None
    try:
        obj = json.loads(stripped)
    except ValueError:
        return None
    return obj if isinstance(obj, (dict, list)) else None


def double_encoded_json_advisory(value: Any) -> str | None:
    """One advisory line when `value` is a string holding a serialized JSON
    object or array, else None."""
    obj = _parsed_document(value)
    if obj is None:
        return None
    kind = "object" if isinstance(obj, dict) else "array"
    return DOUBLE_ENCODED_ADVISORY.format(kind=kind, excerpt=_EXCERPT_CHARS)


def oversized_value_advisory(value: Any, memory_type: str) -> str | None:
    """One advisory line when a searchable type's serialized value is longer
    than `memory_value_advisory_chars` (0 disables), else None."""
    cap = int(settings.memory_value_advisory_chars)
    if cap <= 0 or not is_searchable_type(memory_type):
        return None
    size = len(json.dumps(value))
    if size <= cap:
        return None
    return OVERSIZED_ADVISORY.format(size=size, excerpt=_EXCERPT_CHARS)


def value_shape_advisories(value: Any, memory_type: str) -> list[str]:
    """Every advisory a `memory.set` value earns: base64, double-encoded
    JSON, oversized. A base64 envelope sent as a JSON string is both of the
    first two; the base64 line wins there (unwrapping the string would still
    leave base64). Oversized can ride with either."""
    encoded = encoded_value_advisory(value)
    lines = [
        encoded,
        None if encoded else double_encoded_json_advisory(value),
        oversized_value_advisory(value, memory_type),
    ]
    return [line for line in lines if line]
