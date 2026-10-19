"""Local CPU embedder via `fastembed` (ONNX), no API key: `local` or
`local:<alias>`.

Opt-in. The models are lower-dim and weaker on paraphrase-heavy retrieval
than the hosted ones; the trade is no vendor and no network after the
first download. Each emits its own dim and cosine distribution, which
`LOOM_ADAPTIVE_THRESHOLDS` calibrates the search gates to, and switching to
or from one needs a full re-embed.

`fastembed` is the optional `local-embedder` extra, imported lazily.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Iterable
from functools import lru_cache
from typing import Protocol

from server.settings.catalog import DEFAULT_LOCAL_EMBEDDING_MODEL, LOCAL_EMBEDDING_MODELS

log = logging.getLogger(__name__)


class _Vector(Protocol):
    """One model output row (a numpy array in fastembed)."""

    def tolist(self) -> list[float]: ...


class _TextEmbedding(Protocol):
    """The part of `fastembed.TextEmbedding` this module calls."""

    def embed(self, documents: list[str]) -> Iterable[_Vector]: ...


@lru_cache(maxsize=1)
def _resolve_model(spec: str) -> tuple[str, int]:
    """`local` or `local:<alias>` -> (Hugging Face id, dim); ValueError on
    an unknown alias."""
    if spec == "local":
        alias = DEFAULT_LOCAL_EMBEDDING_MODEL
    elif spec.startswith("local:"):
        alias = spec.split(":", 1)[1].strip()
        if not alias:
            alias = DEFAULT_LOCAL_EMBEDDING_MODEL
    else:
        raise ValueError(f"not a local model spec: {spec!r}")
    if alias not in LOCAL_EMBEDDING_MODELS:
        raise ValueError(
            f"unknown local embedding model alias {alias!r}; supported: "
            f"{sorted(LOCAL_EMBEDDING_MODELS)}"
        )
    return LOCAL_EMBEDDING_MODELS[alias]


def onnx_threads() -> int:
    """This process's share of the CPUs: every web worker runs its own session."""
    from server.core.web_workers import cpu_limit
    from server.settings import settings

    return max(1, cpu_limit() // max(1, int(settings.web_workers)))


class LocalEmbedder:
    """Synchronous ONNX inference, run in `asyncio.to_thread`: one hop per
    query and one per batch."""

    def __init__(self, spec: str) -> None:
        try:
            from fastembed import TextEmbedding  # noqa: F401
        except ImportError as e:
            raise ImportError(
                "LOOM_EMBEDDING_MODEL=local* requires the `fastembed` "
                "package. Install with: pip install fastembed"
            ) from e

        hf_id, dim = _resolve_model(spec)
        self.model_name = spec
        self.hf_model_id = hf_id
        self.dim = dim
        self._embedder: _TextEmbedding | None = None  # lazy; loaded on first embed
        self._lock = asyncio.Lock()

    async def _ensure_loaded(self) -> None:
        if self._embedder is not None:
            return
        async with self._lock:
            if self._embedder is not None:  # another task loaded it
                return
            from fastembed import TextEmbedding

            def _load() -> _TextEmbedding:
                log.info(
                    "loading local embedder %s (HF: %s, dim=%d) — first call "
                    "downloads from HuggingFace",
                    self.model_name,
                    self.hf_model_id,
                    self.dim,
                )
                embedder: _TextEmbedding = TextEmbedding(
                    model_name=self.hf_model_id, threads=onnx_threads()
                )
                return embedder

            self._embedder = await asyncio.to_thread(_load)

    async def embed(self, text: str) -> list[float]:
        """Unit-norm vector of the whitespace-normalised query; empty -> zeros."""
        from server.embedding.normalize import query_text

        return (await self._embed([query_text(text)]))[0]

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        """One vector per raw text, in input order, from one inference call
        (fastembed batches internally) off the event loop."""
        return await self._embed(texts)

    async def _embed(self, texts: list[str]) -> list[list[float]]:
        out = [[0.0] * self.dim for _ in texts]
        live = [i for i, t in enumerate(texts) if t and t.strip()]
        if not live:
            return out
        await self._ensure_loaded()
        embedder = self._embedder
        assert embedder is not None  # _ensure_loaded set it or raised

        def _run() -> None:
            vectors = embedder.embed([texts[i] for i in live])
            for i, v in zip(live, vectors, strict=True):
                vec = v.tolist()
                # fastembed models are mostly unit-norm already; make it certain.
                n = math.sqrt(sum(x * x for x in vec)) or 1.0
                out[i] = [x / n for x in vec]

        await asyncio.to_thread(_run)
        return out
