"""Blacklist enforcement ("Refuse").

Active rows are cached in-process so the hot path is memory-only.
Cross-replica invalidation rides a version watermark in CH
(loom.blacklist_version): add/remove inserts a row with a fresh version,
replicas refresh on their next staleness check when the watermark moved.
find_match returns the FIRST match, and the cache holds the most specific
scope first, so a namespace pattern beats an org pattern beats a global one.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass

from fastapi import HTTPException

from server.core.ch import get_client, sequential_reads
from server.core.datetimes import naive_utc_now_seconds
from server.memory.store import blacklist as _repo
from server.settings import settings

log = logging.getLogger(__name__)

# A regex pattern runs on every write the scope covers — for a global
# pattern, every tenant's — so its cost is bounded here, at add time and
# again when the cache loads a row. Length, then shape,
# then a timed probe; `re2` is not in the deployment, so Python `re` stays
# and the pattern is proven cheap before it is stored.
REGEX_MAX_LENGTH = 256
REGEX_PROBE_BUDGET_MS = 1.0
# Probe input lengths, growing slowly at first: a stage that finishes under
# the budget bounds the next — an exponential pattern with base b costs at
# most b² more two characters later — so a catastrophic pattern is caught at
# a few milliseconds, never at seconds.
_PROBE_LENGTHS = (4, 6, 8, 10, 12, 14, 16, 20, 24, 28, 32, 48, 64, 96, 128, 192, 256)
# A quantifier directly after a group whose body ends in a quantified atom:
# (a+)+, (a*)*, (.*)*, (a+){2,}, (?:a?){20}, (x+x+)+. Static shape check for
# the textbook exponential form; a group with a mandatory separator after
# the inner quantifier, (a+\\.)+, is not this shape and goes to the probe,
# as does every alternation.
_QUANTIFIER = r"(?:[*+?]|\{\d+(?:,\d*)?\})\??"
_NESTED_QUANTIFIER_RE = re.compile(r"\((?:\?[:=!<P]?)?[^()]*" + _QUANTIFIER + r"\)" + _QUANTIFIER)
_ESCAPE_RE = re.compile(r"\\.")
_CLASS_RE = re.compile(r"\[(?:\\.|[^\]])*\]")


def _strip_atoms(pattern: str) -> str:
    """Remove escapes and character classes so `_NESTED_QUANTIFIER_RE` sees
    only the grouping structure (`[+*]` and `\\(` are not quantifiers or groups)."""
    return _CLASS_RE.sub("x", _ESCAPE_RE.sub("x", pattern))


def _probe_inputs(pattern: str, n: int) -> list[str]:
    """Inputs a backtracking engine dislikes: runs of the pattern's own
    literal characters, with and without a trailing character that defeats
    the match at the end."""
    literals: list[str] = []
    for ch in _strip_atoms(pattern):
        if ch.isalnum() and ch not in literals:
            literals.append(ch)
        if len(literals) == 3:
            break
    if not literals:
        literals = ["a"]
    out: list[str] = []
    for ch in literals:
        out.append(ch * n)
        out.append(ch * n + "!")
    if len(literals) > 1:
        mixed = "".join(literals) * (n // len(literals) + 1)
        out.append(mixed[:n] + "!")
    return out


def validate_regex(pattern: str) -> str | None:
    """Reason the pattern is refused, or None when it may be stored.

    Refuses: over `REGEX_MAX_LENGTH`; a pattern `re` cannot compile; a
    quantified group whose body is itself quantified or an alternation; and
    a pattern whose search over any probe input exceeds
    `REGEX_PROBE_BUDGET_MS`. The probe grows the input in small steps so the
    check itself stays under a few tens of milliseconds for any pattern."""
    if len(pattern) > REGEX_MAX_LENGTH:
        return f"regex longer than {REGEX_MAX_LENGTH} characters ({len(pattern)})"
    try:
        compiled = re.compile(pattern)
    except re.error as e:
        return f"regex does not compile: {e}"
    if _NESTED_QUANTIFIER_RE.search(_strip_atoms(pattern)):
        return (
            "regex nests a quantifier inside a quantified group (catastrophic backtracking shape)"
        )
    budget_s = REGEX_PROBE_BUDGET_MS / 1000.0
    for n in _PROBE_LENGTHS:
        for text in _probe_inputs(pattern, n):
            # Best of two: a scheduler hiccup must not refuse a cheap pattern.
            spent = budget_s * 2
            for _ in range(2):
                t0 = time.perf_counter()
                compiled.search(text)
                spent = min(spent, time.perf_counter() - t0)
                if spent <= budget_s:
                    break
            if spent > budget_s:
                return (
                    f"regex search took {spent * 1000:.2f} ms on a {len(text)}-character "
                    f"probe (budget {REGEX_PROBE_BUDGET_MS:g} ms)"
                )
    return None


@dataclass(frozen=True)
class BlacklistPattern:
    pattern_id: str
    org: str
    namespace: str
    pattern: str
    pattern_type: str  # 'substring' | 'regex'
    scope: str  # 'namespace' | 'org' | 'global'
    reason: str
    compiled: re.Pattern[str] | None = None  # set for regex patterns


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
        compiled: re.Pattern[str] | None = None
        if pattern_type == "regex":
            # A row written past add-time validation is dropped from the hot
            # path rather than run on every write; the refusal is logged so
            # the operator can remove it.
            reason = validate_regex(r["pattern"])
            if reason is not None:
                log.warning(
                    "blacklist: dropping regex pattern %s from the cache: %s",
                    r["pattern_id"],
                    reason,
                )
                continue
            compiled = re.compile(r["pattern"])
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
    for p in _cache:
        if not _applies_to(p, org, namespace):
            continue
        if _matches(p, value):
            return p
    return None


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
