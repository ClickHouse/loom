"""Pluggable embedding providers behind the `Embedder` protocol.

`settings.embedding_model` selects one (`LOOM_EMBEDDING_PROVIDER` may pin it):
fake-ngram-* (default; deterministic, no network), text-embedding-* (OpenAI),
gemini-* (Gemini) and local / local:* (fastembed on CPU). `prefix:*` and
`ctx*:*` label the served model's vectors for a second code-search vector
leg; see `embedder.py`.
"""

from __future__ import annotations

from server.embedding.embedder import Embedder, get_embedder

__all__ = ["Embedder", "get_embedder"]
