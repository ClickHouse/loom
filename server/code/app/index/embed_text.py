"""Bound what the code plane sends to the embedding provider.

OpenAI's text-embedding-3 models reject inputs over 8,191 tokens with a 400.
Code and data tokenise densely (2.5–3.5 chars/token), so 20,000 chars is a
safe ceiling. A chunk that large (a generated table, a JSON fixture) is still
worth a vector of its head, not a stub row that the recovery worker retries
into the same 400, flipping the provider health flag each time.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

EMBED_MAX_CHARS = 20_000
EMBED_MAX_TOKENS = 8_000  # provider limit 8,191; leave headroom for the marker
_MARK = "\n…[truncated for embedding]"


def clip_for_embedding(
    text: str,
    limit: int = EMBED_MAX_CHARS,
    *,
    count_tokens: Callable[[str], int] | None = None,
    max_tokens: int = EMBED_MAX_TOKENS,
) -> str:
    """The text the provider actually sees: unchanged when short, else its
    head plus a marker. Never changes what is STORED — only what is embedded.

    The char limit alone is not enough: number-dense SQL and JSON fixtures
    tokenise at ~1.5 chars/token, so 20k chars can still be a 400. When the
    embedder can count tokens (OpenAIEmbedder._count_tokens via tiktoken),
    shrink proportionally until the count fits; the char cap stays as the
    floor for embedders that cannot."""
    if len(text) > limit:
        text = text[: limit - len(_MARK)] + _MARK
    if count_tokens is None:
        return text
    for _ in range(6):
        try:
            n = count_tokens(text)
        except Exception:  # noqa: BLE001 — counting is best effort
            return text
        if n <= max_tokens or len(text) <= 256:
            return text
        keep = max(256, int(len(text) * max_tokens / n * 0.9))
        text = text[:keep] + _MARK
    return text


# httpx's HTTPStatusError carries only "Client error '400 Bad Request' for
# url ..." — the provider's "maximum context length" body never reaches the
# message. An embeddings endpoint's 400 is, in practice, a length rejection
# (the model name and key are fixed per process), so treat it as one: a
# genuinely different 400 shrinks a few times and then raises unchanged.
_TOO_LONG_MARKERS = (
    "context length",
    "maximum context",
    "too large",
    "tokens",
    "400 bad request",
)


def looks_too_long(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(m in msg for m in _TOO_LONG_MARKERS)


async def embed_shrinking(embedder: Any, text: str, *, attempts: int = 5) -> list[float]:
    """One stored vector for `text` (`embed_many`: the raw text, as ingest
    stores it; `embed` is the query path and normalises), halving the input on
    a provider "too long" rejection. Provider-driven on purpose: the local token counter degrades to
    len//4 without tiktoken and undercounts hex-heavy SQL threefold, so only
    the provider knows the true length. Non-length errors propagate at once."""
    last: BaseException | None = None
    for _ in range(attempts):
        try:
            vecs: list[list[float]] = await embedder.embed_many([text])
            if len(vecs) != 1:
                raise RuntimeError(f"embed_many returned {len(vecs)} vectors for 1 input")
            return vecs[0]
        except Exception as e:
            if not looks_too_long(e) or len(text) <= 256:
                raise
            last = e
            text = text[: max(256, len(text) // 2)] + _MARK
    assert last is not None
    raise last
