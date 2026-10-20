"""An identical prompt is answered once.

`CachedLLMClient` wraps any `LLMClient`. Before the provider call it reads
`loom.llm_response_cache` by `(model, key)` where `key` is the sha256 of
everything that decides the answer at temperature 0: the system prompt, the
user prompt, `max_tokens`, the response format and any extra keyword the
caller passed (a temperature, a tool schema), and the org the client serves:
one org is never served another's answer, nor learns from a fast reply that
another asked the same, and a client built with no org calls the provider.
After a provider call it writes the row fire-and-forget. The evidence a
synthesis read is inside the user prompt, so a memory that changes, arrives or
leaves the window changes the key — staleness is impossible by construction, not by TTL (a
question-keyed cache could serve an answer its evidence no longer supports).

Same shape as the shared embedding cache: a primary-key point read
per call, an async insert per miss, every step failing open to the
provider — a cache that can raise adds an outage mode to every ask.

The wrapper is per request (the handler builds one around the routed
client), so its `hits` / `misses` are the request's own and land on the
ask's output; the module counters are the process totals for `/health`.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import contextvars
import hashlib
import json
import logging
import time
from collections.abc import Iterable, Iterator
from contextvars import ContextVar
from typing import Any

from server.core.background import TaskSet
from server.llm.client import LLMClient, LLMResult

log = logging.getLogger(__name__)

# Process-lifetime totals (mirrors server/embedding/_cache.py's shared tier).
hits = 0
misses = 0
errors = 0
saved_usd = 0.0
_hit_ms: collections.deque[float] = collections.deque(maxlen=1000)
_TASKS = TaskSet()
PUT_QUERY_TAG = "llm_response_cache.put"
# Stop reasons that mean the provider ended the reply early (its text is partial).
_PARTIAL_STOPS = frozenset({"content_filter", "refusal", "length", "max_tokens"})


# The memory keys of the window a cached call rendered ride the
# row (`window_keys`) so a `user_id` erasure and `memory.redact`
# can delete cached text by key. `ask()` publishes the synthesis pool once it
# is final; `_synthesize` narrows it to the rows it renders (the block plus the
# memories opened past it) for the duration of its provider call; the judge and
# the selector run under the pool. Empty outside an ask — a row written with
# no window has no key axis and leaves with the TTL.
_WINDOW: ContextVar[tuple[str, ...]] = ContextVar("loom_llm_cache_window", default=())


_BYPASS: contextvars.ContextVar[bool] = contextvars.ContextVar("llm_cache_bypass", default=False)


@contextlib.contextmanager
def bypass() -> Iterator[None]:
    """Calls inside skip the response cache (read and write): for a caller
    that found a cached reply unusable and needs the provider's own answer."""
    token = _BYPASS.set(True)
    try:
        yield
    finally:
        _BYPASS.reset(token)


def set_window_keys(keys: Iterable[Any]) -> None:
    """Publish the window for every cached call that follows in this task."""
    _WINDOW.set(_dedup(keys))


@contextlib.contextmanager
def window_keys_scope(keys: Iterable[Any]) -> Iterator[None]:
    """Narrow the window for one call; the enclosing value returns afterwards."""
    token = _WINDOW.set(_dedup(keys))
    try:
        yield
    finally:
        _WINDOW.reset(token)


def current_window_keys() -> tuple[str, ...]:
    return _WINDOW.get()


def _dedup(keys: Iterable[Any]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for k in keys:
        key = str(k or "")
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return tuple(out)


def table_name() -> str:
    from server.settings import settings

    return settings.llm_response_cache_table


def cache_key(
    system: str,
    user: str,
    max_tokens: int,
    response_format: dict[str, str] | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    """sha256 over every input that decides the completion. Canonical JSON so
    the key does not depend on dict order; `None` extras are dropped so a
    caller that omits `temperature` and one that passes `temperature=None`
    (the providers' default) key alike."""
    parts = [
        system or "",
        user or "",
        int(max_tokens),
        response_format or None,
        {k: v for k, v in sorted((extra or {}).items()) if v is not None} or None,
    ]
    blob = json.dumps(parts, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def scoped_key(
    system: str,
    user: str,
    max_tokens: int,
    response_format: dict[str, str] | None = None,
    extra: dict[str, Any] | None = None,
    *,
    org: str,
    key_scope: str = "",
) -> str:
    """The key a `CachedLLMClient` for `org` reads and writes by."""
    scoped = {**(extra or {}), "_org": org}
    if key_scope:
        scoped["_scope"] = key_scope
    return cache_key(system, user, max_tokens, response_format, scoped)


def read_sql(table: str | None = None) -> str:
    """The point read. No FINAL: the newest row for the key wins in ORDER BY
    (the embedding cache's shape); the ReplacingMergeTree collapses the rest on merge."""
    return (
        f"SELECT text, tokens_in, tokens_out FROM {table or table_name()} "
        "WHERE model = {model:String} AND key = {key:String} "
        "ORDER BY created_at DESC LIMIT 1"
    )


class CachedLLMClient:
    """An LLMClient that answers a repeated prompt from the shared table.

    `provider` / `model` are the inner client's, so callers that label by
    model keep working; a served hit carries `from_cache=True` on the result
    and the inner client's model name (the row was written under it).
    """

    def __init__(
        self,
        inner: LLMClient,
        *,
        org: str = "",
        namespace: str = "",
        ch: Any = None,
        table: str | None = None,
        key_scope: str = "",
        deterministic_only: bool = False,
    ) -> None:
        self.inner = inner
        self.provider = inner.provider
        self.model = inner.model
        self.org = org
        self.namespace = namespace
        self._ch = ch
        self._table = table
        # `key_scope` folds into the key (code.ask scopes its pre-synthesis calls per
        # namespace); '' leaves the key unscoped. `deterministic_only` sends a call with
        # a non-zero temperature straight to the provider: a sampled completion is not
        # an answer to replay.
        self._key_scope = key_scope
        self._deterministic_only = deterministic_only
        self.hits = 0
        self.misses = 0
        self.errors = 0
        self.saved_usd = 0.0

    # -- read ---------------------------------------------------------------

    def _client(self) -> Any:
        if self._ch is not None:
            return self._ch
        from server.core.ch import get_client

        return get_client()

    async def _get(self, key: str) -> dict[str, Any] | None:
        global errors
        try:
            row = await self._client().query_one(
                read_sql(self._table), params={"model": self.model, "key": key}
            )
            return dict(row) if row else None
        except Exception as e:  # noqa: BLE001 — fail open to the provider
            errors += 1
            self.errors += 1
            log.debug("llm response cache read failed (%s); calling the provider", str(e)[:120])
            return None

    # -- write --------------------------------------------------------------

    def _put(self, key: str, res: LLMResult) -> None:
        """Fire-and-forget insert; never awaited by the request. Nothing in
        here may raise into the answer: a stub result without token counts,
        a client that is not connected, a missing table all cost one
        provider call later, not the ask."""
        global errors
        try:
            asyncio.get_running_loop()
            from server.core.datetimes import naive_utc_now

            try:
                from server.observability.genai import current_stage

                stage = current_stage()
            except Exception:  # noqa: BLE001 — telemetry label only
                stage = ""
            row = {
                "model": self.model,
                "key": key,
                "org": self.org or "",
                "namespace": self.namespace or "",
                "stage": stage or "",
                "text": str(res.text),
                "tokens_in": max(0, int(getattr(res, "tokens_in", 0) or 0)),
                "tokens_out": max(0, int(getattr(res, "tokens_out", 0) or 0)),
                "created_at": naive_utc_now(),
                # The key axis a user erasure deletes by.
                "window_keys": list(current_window_keys()),
            }
            table = self._table or table_name()

            async def _bg() -> None:
                global errors
                try:
                    from server.core.ch import set_query_tag

                    set_query_tag(PUT_QUERY_TAG)  # detached: no request tag to inherit
                    await self._client().insert(
                        table, [row], settings={"async_insert": 1, "wait_for_async_insert": 0}
                    )
                except Exception as e:  # noqa: BLE001 — a lost write costs one provider call later
                    errors += 1
                    log.debug("llm response cache write failed: %s", str(e)[:120])

            _TASKS.spawn(_bg(), name="llm-response-cache-put", detached=True)
        except Exception as e:  # noqa: BLE001 — the write is optional, the answer is not
            errors += 1
            log.debug("llm response cache write skipped: %s", str(e)[:120])

    # -- the contract -------------------------------------------------------

    async def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 1024,
        response_format: dict[str, str] | None = None,
        **kw: Any,
    ) -> LLMResult:
        global hits, misses, saved_usd
        if (
            _BYPASS.get()
            or not self.org
            or (self._deterministic_only and float(kw.get("temperature") or 0.0) != 0.0)
        ):
            return await self.inner.complete(
                system, user, max_tokens=max_tokens, response_format=response_format, **kw
            )
        key = scoped_key(
            system, user, max_tokens, response_format, kw, org=self.org, key_scope=self._key_scope
        )
        t0 = time.perf_counter()
        row = await self._get(key)
        if row and str(row.get("text") or ""):
            ms = (time.perf_counter() - t0) * 1000.0
            _hit_ms.append(ms)
            hits += 1
            self.hits += 1
            tokens_in = int(row.get("tokens_in") or 0)
            tokens_out = int(row.get("tokens_out") or 0)
            try:
                from server.observability.cost_accumulator import llm_cost_usd

                usd = llm_cost_usd(self.model, tokens_in, tokens_out)
                saved_usd += usd
                self.saved_usd += usd
            except Exception:  # noqa: BLE001 — accounting only
                pass
            _record(self.model, "hit")
            return LLMResult(
                text=str(row["text"]),
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                provider=self.provider,
                model=self.model,
                cached_input_tokens=0,
                from_cache=True,
            )
        misses += 1
        self.misses += 1
        _record(self.model, "miss")
        res = await self.inner.complete(
            system, user, max_tokens=max_tokens, response_format=response_format, **kw
        )
        # Only what THIS model said is stored under its key: a runtime
        # fallback (server/llm/fallback.py) answers with another model, and an
        # empty completion is a failure to retry, not an answer to memo.
        # A reply the provider cut (a content filter, a refusal, the output cap)
        # is partial: serving it again would replay the cut for every retry.
        complete = str(getattr(res, "stop_reason", "") or "") not in _PARTIAL_STOPS
        if (
            getattr(res, "text", "")
            and complete
            and str(getattr(res, "model", "") or self.model) == self.model
        ):
            self._put(key, res)
        return res


# code.ask's retrieval legs (the code.search HyDE and concept
# expansions) build their own clients deep inside code.search; the ask
# publishes its (org, namespace) here for the duration of the call and those
# legs wrap their client with `wrap_scoped`. Unset (plain code.search, a
# no_cache ask, a bench) = the provider is called directly.
_SCOPE: ContextVar[tuple[str, str] | None] = ContextVar("loom_llm_cache_scope", default=None)


@contextlib.contextmanager
def exact_cache_scope(org: str, namespace: str) -> Iterator[None]:
    """Route the scoped pre-synthesis calls of this task through the cache."""
    token = _SCOPE.set((org, namespace))
    try:
        yield
    finally:
        _SCOPE.reset(token)


def wrap_scoped(llm: Any) -> Any:
    """`llm` behind the exact-prompt cache when a scope is published, else
    `llm` unchanged. Never raises: a client the wrapper cannot take (no
    provider / model) is returned as is."""
    scope = _SCOPE.get()
    if scope is None or llm is None or isinstance(llm, CachedLLMClient):
        return llm
    try:
        org, ns = scope
        return CachedLLMClient(
            llm, org=org, namespace=ns, key_scope=f"{org}/{ns}", deterministic_only=True
        )
    except Exception:  # noqa: BLE001 — the cache is optional, the call is not
        return llm


def _record(model: str, outcome: str) -> None:
    try:
        from server.observability import metrics as _metrics

        _metrics.record_llm_response_cache(model=model, outcome=outcome)
    except Exception:  # noqa: BLE001 — telemetry must not block the answer
        pass


def stats() -> dict[str, Any]:
    """Process totals for `/health?include=llm_cache`."""
    lat = sorted(_hit_ms)
    p50 = lat[len(lat) // 2] if lat else None
    return {
        "hits": hits,
        "misses": misses,
        "errors": errors,
        "saved_usd": round(saved_usd, 4),
        "hit_ms_p50": round(p50, 2) if p50 is not None else None,
        "hit_ms_max": round(lat[-1], 2) if lat else None,
        "table": table_name(),
    }


def reset_stats() -> None:
    """Test hook."""
    global hits, misses, errors, saved_usd
    hits = misses = errors = 0
    saved_usd = 0.0
    _hit_ms.clear()
