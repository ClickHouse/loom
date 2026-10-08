"""Plumbing the HTTP embedding providers (OpenAI, Gemini) share: the retry
policy, the input-size guard, answer validation and the process-cache split
of a batch."""

from __future__ import annotations

import math
from collections.abc import Iterable
from functools import lru_cache
from typing import Any, Protocol

from tenacity import AsyncRetrying

from server.clients import _retry


class EmbedderInputTooLarge(ValueError):
    """Raised when input would silently truncate at the provider."""


class MalformedEmbedding(RuntimeError):
    """The provider answered 2xx with something that is not the vector asked for."""


def vector(values: Iterable[Any], width: int | None) -> list[float]:
    """The provider's vector as floats, refused when it is empty, of another
    `width` than the request pinned, or holds NaN/inf: any of those would be
    stored, cached and ranked as if it were an embedding of the text."""
    vec = [float(x) for x in values]
    if not vec:
        raise MalformedEmbedding("provider returned an empty embedding")
    if width and len(vec) != width:
        raise MalformedEmbedding(
            f"provider returned {len(vec)} dims; the request asked for {width}"
        )
    if not all(math.isfinite(x) for x in vec):
        raise MalformedEmbedding("provider returned a non-finite embedding value")
    return vec


def retrying() -> AsyncRetrying:
    """Three attempts, backoff capped at 10 s."""
    return _retry.retrying(attempts=3, max_wait_s=10)


class _TokenEncoder(Protocol):
    def encode(self, text: str) -> list[int]: ...


@lru_cache(maxsize=1)
def _encoder() -> _TokenEncoder | None:
    try:
        import tiktoken

        enc: _TokenEncoder = tiktoken.get_encoding("cl100k_base")
        return enc
    except Exception:  # noqa: BLE001 — tiktoken is optional; count approximately
        return None


def count_tokens(text: str) -> int:
    """cl100k token count, or ~4 chars/token when tiktoken is unavailable."""
    enc = _encoder()
    return len(enc.encode(text)) if enc is not None else len(text) // 4 + 1


def check_size(text: str, limit: int, model: str) -> None:
    n = count_tokens(text)
    if n > limit:
        raise EmbedderInputTooLarge(
            f"input is {n} tokens; {model} max is {limit}. "
            "Chunk the text or use a model with a larger context window."
        )


def split_cached(
    model: str, texts: list[str]
) -> tuple[list[list[float] | None], list[tuple[int, str]], list[str]]:
    """`(results, misses, hit_texts)` for a batch: `results[i]` is filled from
    the process cache where it can be, `misses` are the `(index, text)` pairs
    left for the provider."""
    from server.embedding._cache import _cache, batch_key

    results: list[list[float] | None] = [None] * len(texts)
    misses: list[tuple[int, str]] = []
    hits: list[str] = []
    for i, t in enumerate(texts):
        cached = _cache.get(batch_key(model, t))
        if cached is None:
            misses.append((i, t))
        else:
            results[i] = cached
            hits.append(t)
    return results, misses, hits


def store(
    model: str, results: list[list[float] | None], index: int, text: str, vec: list[float]
) -> None:
    """Put one validated provider vector in its batch slot and the process cache."""
    from server.embedding._cache import _cache, batch_key

    results[index] = vec
    _cache.put(batch_key(model, text), vec)


def filled(results: list[list[float] | None]) -> list[list[float]]:
    """The batch's vectors; a slot the provider's answer left empty (two
    entries naming one index) fails the batch rather than returning None."""
    out: list[list[float]] = []
    for r in results:
        if r is None:
            raise MalformedEmbedding("provider answer left a batch slot without a vector")
        out.append(r)
    return out
