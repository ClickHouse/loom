"""Blacklist enforcement ("Refuse").

Active rows are cached in-process so the hot path is memory-only.
Cross-replica invalidation rides a version watermark in CH
(loom.blacklist_version): add/remove inserts a row with a fresh version,
replicas refresh on their next staleness check when the watermark moved.
find_match returns the FIRST match, and the cache holds the most specific
scope first, so a namespace pattern beats an org pattern beats a global one.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException

from server.core.caller_regex import compile_caller_regex
from server.core.ch import get_client, sequential_reads
from server.core.datetimes import naive_utc_now_seconds
from server.core.errors import ValidationError
from server.memory.store import blacklist as _repo
from server.settings import settings

log = logging.getLogger(__name__)

# A regex runs on every write its scope covers, on RE2: linear in the value. Its cost
# per character still grows with the program (a counted class such as `[ab]{400}`
# defeats RE2's DFA cache), so the program and the count per scope are capped.
REGEX_MAX_LENGTH = 256
REGEX_MAX_PROGRAM = 300  # not a setting: the bound that keeps a pattern cheap per character
REGEXES_PER_SCOPE = 50  # not a setting: with the program cap, bounds what one write runs
# Regex matching runs on its own few threads (RE2 releases the GIL), so a long value
# under many patterns waits for these, never the event loop or the shared pool.
_MATCHERS = ThreadPoolExecutor(max_workers=4, thread_name_prefix="loom-blacklist")


def compile_regex(pattern: str) -> Any:
    """The RE2 pattern a regex row runs as, or ValidationError naming why it is
    refused: over `REGEX_MAX_LENGTH`, not a regex RE2 can run (no backreferences
    or lookaround), or a program over `REGEX_MAX_PROGRAM`."""
    if len(pattern) > REGEX_MAX_LENGTH:
        raise ValidationError(f"regex longer than {REGEX_MAX_LENGTH} characters ({len(pattern)})")
    compiled = compile_caller_regex(pattern, dot_nl=True)
    if compiled.programsize > REGEX_MAX_PROGRAM:
        raise ValidationError(
            f"regex compiles to {compiled.programsize} RE2 instructions (at most "
            f"{REGEX_MAX_PROGRAM}): a counted repeat such as {{400}} or a long non-ASCII "
            "literal (use pattern_type=substring for that)"
        )
    return compiled


async def count_regexes(scope: str, org: str, namespace: str) -> int:
    """The active regex patterns stored at exactly this scope, from the cache."""
    await _ensure_loaded()
    return sum(
        1
        for p in _cache
        if p.pattern_type == "regex" and (p.scope, p.org, p.namespace) == (scope, org, namespace)
    )


@dataclass(frozen=True)
class BlacklistPattern:
    pattern_id: str
    org: str
    namespace: str
    pattern: str
    pattern_type: str  # 'substring' | 'regex'
    scope: str  # 'namespace' | 'org' | 'global'
    reason: str
    compiled: Any = None  # the RE2 pattern, set for regex patterns


# The first match wins, so the cache holds the most specific scope first; the
# refusal then cites the pattern (id, reason) closest to the write.
_SCOPE_PRECEDENCE = {"namespace": 0, "org": 1, "global": 2}

_cache: list[BlacklistPattern] = []
_dirty = True
_cached_version: tuple[int, int] = (-1, -1)
_last_version_check: float = 0.0  # monotonic; throttles the version-watermark query
_loaded_at: float = 0.0  # monotonic; when the rows were last read (the TTL backstop)
_lock = threading.Lock()


async def _current_version() -> tuple[int, int]:
    with sequential_reads():  # a bump another replica just wrote
        return await _repo.version_watermark(ch=get_client())


async def _bump_version(updated_by: str = "") -> None:
    """Move the watermark for every replica: one insert, no read, so two
    concurrent add/remove calls cannot write the same version."""
    await _repo.insert_version(time.time_ns(), updated_by=updated_by, ch=get_client())


def invalidate_cache() -> None:
    """Mark the local cache dirty. Callers must also _bump_version() so
    other replicas refresh on their next staleness check."""
    global _dirty
    with _lock:
        _dirty = True


async def _is_stale() -> bool:
    # Cheap path first: in-process dirty flag, then a hard TTL backstop, then
    # a version watermark read at most once per check interval. The throttle
    # bounds the CH read rate under busy memory.set traffic: an unchanged
    # version restarts the interval, so a write does not pay for the read.
    global _last_version_check
    if _dirty:
        return True
    now = time.monotonic()
    if (now - _loaded_at) > settings.blacklist_cache_ttl_seconds:
        return True
    if (now - _last_version_check) >= settings.blacklist_version_check_interval_seconds:
        current = await _current_version()
        if current != _cached_version:
            return True
        with _lock:
            _last_version_check = now
    return False


async def _refresh_cache() -> None:
    """Load every non-tombstoned blacklist row into the in-process cache."""
    global _cache, _dirty, _cached_version, _last_version_check, _loaded_at
    # The watermark is read BEFORE the rows: add/remove writes its pattern row
    # and then bumps, so a bump this read missed moves the watermark past the
    # cached one and the next check refreshes; one it saw was preceded by a
    # row write the read below sees. Both read with sequential consistency: on a
    # lagging replica the rows could otherwise predate the watermark they follow.
    with sequential_reads():
        new_version = await _current_version()
        rows = await _repo.active_patterns(ch=get_client())
    new_cache: list[BlacklistPattern] = []
    for r in rows:
        pattern_type = r["pattern_type"]
        compiled: Any = None
        if pattern_type == "regex":
            # A row written past add-time validation is dropped from the hot
            # path rather than run on every write; the refusal is logged so
            # the operator can remove it.
            try:
                compiled = compile_regex(r["pattern"])
            except ValidationError as e:
                log.warning(
                    "blacklist: dropping regex pattern %s from the cache: %s", r["pattern_id"], e
                )
                continue
        new_cache.append(
            BlacklistPattern(
                pattern_id=str(r["pattern_id"]),
                org=r["org"],
                namespace=r["namespace"],
                pattern=r["pattern"],
                pattern_type=pattern_type,
                scope=r["scope"],
                reason=r["reason"],
                compiled=compiled,
            )
        )
    new_cache.sort(key=lambda p: _SCOPE_PRECEDENCE.get(p.scope, len(_SCOPE_PRECEDENCE)))
    with _lock:
        _cache = new_cache
        _dirty = False
        _cached_version = new_version
        _last_version_check = _loaded_at = time.monotonic()


async def _ensure_loaded() -> None:
    if await _is_stale():
        await _refresh_cache()


def _applies_to(p: BlacklistPattern, org: str, namespace: str) -> bool:
    if p.scope == "global":
        return True
    if p.scope == "org":
        return p.org == org
    return p.org == org and p.namespace == namespace


def _matches(p: BlacklistPattern, value: str) -> bool:
    if p.pattern_type == "regex":
        return p.compiled is not None and bool(p.compiled.search(value))
    return p.pattern in value


async def find_match(
    *,
    org: str,
    namespace: str,
    value: str,
) -> BlacklistPattern | None:
    """Return the first applicable + matching pattern, or None."""
    await _ensure_loaded()
    applicable = [p for p in _cache if _applies_to(p, org, namespace)]
    if not any(p.pattern_type == "regex" for p in applicable):
        return _first_match(applicable, value)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_MATCHERS, _first_match, applicable, value)


def _first_match(patterns: list[BlacklistPattern], value: str) -> BlacklistPattern | None:
    # RE2 reads UTF-8: a lone surrogate (an unpaired JSON escape) is read as `?`.
    value = value.encode("utf-8", "replace").decode("utf-8")
    return next((p for p in patterns if _matches(p, value)), None)


# Refusals per pattern since its last hit-count write, and when that was.
# One ALTER UPDATE per refusal would make a retrying client a mutation flood on
# loom.blacklist; hits are coalesced and written at most once per pattern per
# interval (bounded by the number of patterns), the rest at shutdown.
_HIT_FLUSH_INTERVAL_S = 30.0
_pending_hits: dict[str, int] = {}
_last_hit_flush: dict[str, float] = {}


async def note_hit(pattern_id: str) -> None:
    """Count one refusal against `pattern_id`. The counter lands in
    loom.blacklist at most once per `_HIT_FLUSH_INTERVAL_S` per pattern;
    recording never blocks or fails the refusal it accompanies."""
    with _lock:
        _pending_hits[pattern_id] = _pending_hits.get(pattern_id, 0) + 1
        now = time.monotonic()
        if now - _last_hit_flush.get(pattern_id, 0.0) < _HIT_FLUSH_INTERVAL_S:
            return
        _last_hit_flush[pattern_id] = now
        n = _pending_hits.pop(pattern_id, 0)
    await _write_hits(pattern_id, n)


async def _write_hits(pattern_id: str, n: int) -> None:
    if n <= 0:
        return
    try:
        ch = get_client()
        await ch.execute(
            "ALTER TABLE loom.blacklist "
            "UPDATE hit_count = hit_count + {n:UInt64}, last_hit_at = {at:DateTime} "
            "WHERE pattern_id = {pid:UUID} SETTINGS mutations_sync=0",
            # bound: a replicated table refuses now() in a mutation
            params={"pid": pattern_id, "n": int(n), "at": naive_utc_now_seconds()},
        )
    except Exception:  # noqa: BLE001 — hit counting must not block the refusal
        pass


async def flush_hits() -> int:
    """Write every coalesced hit count now (shutdown). Returns the patterns written."""
    with _lock:
        pending = dict(_pending_hits)
        _pending_hits.clear()
    for pid, n in pending.items():
        await _write_hits(pid, n)
    return len(pending)


async def enforce_on_insert(
    *,
    org: str,
    namespace: str,
    value: str,
) -> None:
    """Raise 422 BLACKLISTED if `value` matches any applicable pattern."""
    hit = await find_match(org=org, namespace=namespace, value=value)
    if hit is None:
        return
    await note_hit(hit.pattern_id)
    from server.identity.app.request_scope import request_narrowed

    # Under a narrowed token the pattern may be another user's: its id and reason are theirs.
    if request_narrowed():
        detail = {
            "code": "BLACKLISTED",
            "message": f"value matches a blacklist pattern (scope={hit.scope})",
            "scope": hit.scope,
        }
    else:
        detail = {
            "code": "BLACKLISTED",
            "message": (
                f"value matches blacklist pattern {hit.pattern_id!r} "
                f"(scope={hit.scope}, reason={hit.reason!r})"
            ),
            "scope": hit.scope,
            "pattern_id": hit.pattern_id,
            "reason": hit.reason,
        }
    raise HTTPException(status_code=422, detail=detail)
