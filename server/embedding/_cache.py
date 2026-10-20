"""In-process LRU cache for embedding results, with lazy TTL eviction.

Keyed by (model, normalized_text) only — never identity — so shared corpus
text is cached once across tenants. Process-local and single-event-loop;
no locks (asyncio guarantees no mutation between awaits).
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable

from server.core.background import TaskSet
from server.embedding.normalize import normalize_whitespace, query_text
from server.llm import customer
from server.settings import settings

# The key hashes the whitespace-normalised text, and the provider sees the
# same text. No lowercasing — case matters for some embedders.
_normalize = normalize_whitespace

# Key-space version for normalised-text vectors. Changing it orphans every
# loom.embedding_cache row (one cold miss per text).
_KEY_VERSION = b"ws1\0"


def _key(model: str, text: str) -> str:
    h = hashlib.sha1()
    h.update(model.encode())
    h.update(b"\0")
    h.update(_KEY_VERSION)
    h.update(_normalize(text).encode())
    return h.hexdigest()


def batch_key(model: str, text: str) -> str:
    """Process-cache key for a batch (`embed_many`) text: the raw text in its
    own key space, since the batch path embeds it unnormalised and must never
    serve, or be served by, a query's vector."""
    return hashlib.sha1(b"raw\0" + model.encode() + b"\0" + text.encode()).hexdigest()


class _LRU:
    def __init__(self, max_size: int, ttl_s: float) -> None:
        self.max_size = max_size
        self.ttl_s = ttl_s
        self._store: OrderedDict[str, tuple[float, list[float]]] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> list[float] | None:
        entry = self._store.get(key)
        if entry is None:
            self.misses += 1
            return None
        expires_at, vec = entry
        if expires_at < time.monotonic():
            del self._store[key]
            self.misses += 1
            return None
        self._store.move_to_end(key)
        self.hits += 1
        return vec

    def put(self, key: str, vec: list[float]) -> None:
        if key in self._store:
            self._store.move_to_end(key)
        self._store[key] = (time.monotonic() + self.ttl_s, vec)
        while len(self._store) > self.max_size:
            self._store.popitem(last=False)


_cache = _LRU(max_size=settings.embedding_cache_size, ttl_s=settings.embedding_cache_ttl_s)

# key -> the future of the provider call embedding that text right now.
_INFLIGHT: dict[str, asyncio.Future[list[float]]] = {}


async def cached_embed(
    model: str,
    text: str,
    fetch: Callable[[str], Awaitable[list[float]]],
    *,
    dim: int,
) -> tuple[list[float], bool]:
    """Return `(embedding, was_cache_hit)`. `dim` is the embedder's output
    width: a shared-tier row of another width (the same model before a
    `LOOM_EMBEDDING_DIM` change) is not this embedder's vector.

    Checks the call's customer first (`server.llm.customer.require`): a hit
    or a joined flight must not hide a path that names none, and one caller's
    refusal must never reach another's waiter.

    The hit flag is returned rather than inferred from the `hits`/`misses`
    counters: those are process-wide, so diffing them across an `await` races
    with every other concurrent embed. The caller needs it to distinguish a
    served-from-cache embedding (no upstream tokens, no cost) from a billed one
    on its telemetry span.
    """
    await customer.admit()
    k = _key(model, text)
    hit = _cache.get(k)
    if hit is not None:
        _last_tier.set("process")
        return hit, True
    # One provider call per text in flight. loom_search's
    # memory, knowledge, conversation and profile legs embed the same query
    # at once; each missed the cache and paid its own call. A waiter shares
    # the owner's outcome: its vector, or the provider failure the owner's
    # retries ended on (the same call again would only repeat them). Only an
    # owner cancelled by its own caller leaves the waiters to embed themselves.
    loop = asyncio.get_running_loop()
    pending = _INFLIGHT.get(k)
    if pending is not None and pending.get_loop() is loop and not pending.done():
        try:
            vec = await asyncio.shield(pending)
        except _OwnerGone:
            return await _embed_uncached(model, k, text, fetch, dim)
        _last_tier.set("inflight")
        return vec, True
    fut: asyncio.Future[list[float]] = loop.create_future()
    fut.add_done_callback(_consume_exception)
    _INFLIGHT[k] = fut
    try:
        vec, was_hit = await _embed_uncached(model, k, text, fetch, dim)
    except BaseException as e:
        fut.set_exception(e if isinstance(e, Exception) else _OwnerGone(repr(e)))
        raise
    else:
        fut.set_result(vec)
        return vec, was_hit
    finally:
        if _INFLIGHT.get(k) is fut:
            del _INFLIGHT[k]


class _OwnerGone(RuntimeError):
    """The task embedding a text was cancelled; its waiters embed it themselves."""


def _consume_exception(fut: asyncio.Future[list[float]]) -> None:
    if not fut.cancelled():
        fut.exception()  # a failure nobody awaited is not a loop warning


async def _embed_uncached(
    model: str,
    k: str,
    text: str,
    fetch: Callable[[str], Awaitable[list[float]]],
    dim: int,
) -> tuple[list[float], bool]:
    shared = await _shared_get(model, k, text, dim)
    if shared is not None:
        _cache.put(k, shared)
        _last_tier.set("shared")
        return shared, True
    # The provider embeds the text the key hashes.
    vec = await fetch(query_text(text))
    _cache.put(k, vec)
    _last_tier.set("miss")
    _shared_put(model, k, text, vec)
    return vec, False


# Which tier served the last `cached_embed` on this task: process | shared |
# miss. A ContextVar so concurrent embeds on one loop do not read each other's.
_last_tier: contextvars.ContextVar[str] = contextvars.ContextVar("embedding_cache_tier", default="")


def last_tier() -> str:
    return _last_tier.get()


# --- shared tier ---------------------------------------------------------
#
# loom.embedding_cache: (model, key) -> embedding. The key is
# the same sha1 the process tier uses, so the text itself is never stored.
# Every step fails open to the provider: a cache that can raise is a cache
# that adds an outage mode to every search.

SHARED_TABLE = "loom.embedding_cache"
PUT_QUERY_TAG = "embedding_cache.put"
shared_hits = 0
shared_misses = 0
shared_errors = 0
_SHARED_TASKS = TaskSet()


def _shared_enabled(text: str) -> bool:
    cap = settings.embedding_cache_shared_max_chars
    return settings.embedding_cache_shared and (cap <= 0 or len(text) <= cap)


async def _shared_get(model: str, key: str, text: str, dim: int) -> list[float] | None:
    global shared_hits, shared_misses, shared_errors
    if not _shared_enabled(text):
        return None
    try:
        from server.core.ch import get_client

        row = await get_client().query_one(
            f"SELECT embedding FROM {SHARED_TABLE} WHERE model = {{model:String}} "
            "AND key = {key:String} AND dim = {dim:UInt16} ORDER BY created_at DESC LIMIT 1",
            params={"model": model, "key": key, "dim": dim},
        )
    except Exception:  # noqa: BLE001 — fail open to the provider
        shared_errors += 1
        return None
    if not row or not row.get("embedding"):
        shared_misses += 1
        return None
    shared_hits += 1
    return [float(x) for x in row["embedding"]]


def _shared_put(model: str, key: str, text: str, vec: list[float]) -> None:
    """Fire-and-forget insert of a provider result; never awaited by the read."""
    if not _shared_enabled(text) or not vec:
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    from server.core.datetimes import naive_utc_now

    row = {
        "model": model,
        "key": key,
        "dim": len(vec),
        "embedding": [float(x) for x in vec],
        "created_at": naive_utc_now(),
    }

    async def _bg() -> None:
        global shared_errors
        try:
            from server.core.ch import get_client, set_query_tag

            set_query_tag(PUT_QUERY_TAG)  # detached: no request tag to inherit
            await get_client().insert(
                SHARED_TABLE, [row], settings={"async_insert": 1, "wait_for_async_insert": 0}
            )
        except Exception:  # noqa: BLE001 — a lost cache write costs one provider call later
            shared_errors += 1

    _SHARED_TASKS.spawn(_bg(), name="embedding-cache-put", detached=True)


def clear_for_model_transition() -> None:
    """Only under drained admission: model labels can hide changed endpoints."""
    if any(not future.done() for future in _INFLIGHT.values()):
        raise RuntimeError("Embedding calls have not drained")
    _cache._store.clear()


def stats() -> dict[str, int]:
    return {
        "size": len(_cache._store),
        "hits": _cache.hits,
        "misses": _cache.misses,
        "shared_hits": shared_hits,
        "shared_misses": shared_misses,
        "shared_errors": shared_errors,
    }
