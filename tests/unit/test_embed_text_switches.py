"""Embed-text construction: `json.dumps(value)[:budget] + " (#memory_key)"`.

The embedding VECTOR comes from the serialized value, clipped at
EXCERPT_LIMIT, with the key suffix. The stored content_excerpt is the same
text, so a re-embed of the stored column reproduces the write path. Stored
vectors were built this way, so the text must not drift.
"""

from __future__ import annotations

import json

from server.memory.domain.embed_text import _build_embed_text
from server.memory.domain.excerpt import build_excerpt

VALUE = "parts failed to move\nbetween disks"  # newline -> \n once json.dumps'd
SERIALIZED = json.dumps(VALUE)  # '"parts failed to move\\nbetween disks"'
KEY = "finding_h_pr112639"


def test_embed_text_is_json_plus_suffix() -> None:
    out = _build_embed_text(SERIALIZED, memory_key=KEY)
    assert out == f"{SERIALIZED} (#{KEY})"  # the JSON-escaped form, whitespace kept


def test_embed_text_is_clipped_at_the_excerpt_limit() -> None:
    long_val = json.dumps("y" * 5000)
    out = _build_embed_text(long_val, memory_key=KEY)
    assert len(out) == 1200 and out.endswith(f" (#{KEY})")
    assert _build_embed_text(long_val) == long_val[:1200]


def test_content_excerpt_is_the_embed_text() -> None:
    assert build_excerpt(SERIALIZED, memory_key=KEY) == _build_embed_text(
        SERIALIZED, memory_key=KEY
    )
