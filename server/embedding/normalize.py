"""The one whitespace normaliser for text that reaches an embedder.

`normalize_whitespace` is what the embedding cache keys on, and `query_text` is
the text a single-text `embed()` sends to the provider: the same normalised
text, so the vector is of the cache key's text and two callers spelling one
query with different whitespace get one vector. Batch embeds (`embed_many`: memory.set values,
extraction, code ingest) are the write path and send the raw text.
"""

from __future__ import annotations


def normalize_whitespace(text: str) -> str:
    """Collapse runs of whitespace (spaces, tabs, newlines) to one space and
    strip the ends. Nothing else: no case folding, no punctuation changes."""
    return " ".join(text.split())


def query_text(text: str) -> str:
    """The text a single-text embed hands the provider."""
    return normalize_whitespace(text)
