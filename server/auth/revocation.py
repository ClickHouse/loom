"""Token revocation, backed by `loom.revoked_tokens`.

Rotating LOOM_JWT_ACTIVE_KID kills every token of that kid; this is the
per-credential path. A row keyed by a `jti` revokes one token, keyed by a
`sub` every token of that principal — or, with an `issued_before` cutoff,
only those whose `iat` precedes it, so a re-mint after the cutoff survives.
The table is ReplacingMergeTree ORDER BY jti: the newest row per key governs.

- `revoke` is the one write path (`loom revoke-token`, the admin endpoint)
  and takes effect in this process at once, even when the re-read after
  the insert fails; `list_active` is its read.
- Every process that verifies tokens holds its own snapshot: `load_at_boot`
  fills it, and the supervisor worker (lease holder) or `start_refresh_loop`
  (every other process) re-reads it each `jwt_revocation_cache_ttl_seconds`.
- `is_revoked` is a frozenset/dict lookup — no I/O, safe from sync verify.
- Taking a grant away (`write_grant_cutoffs`) writes a cutoff row keyed by
  `grant_claim_key`: tokens of that subject minted before it no longer reach
  the namespace through their own claim (`authz.claim_withdrawn`).
- A cutoff is kept `NEVER_EXPIRES_DAYS`: sign-in leaves out an owned
  namespace with any cutoff, ever, so a lapsed cutoff would hand it back.
  Its key is per namespace and subject spelling, and a later cutoff
  replaces it, so the table holds at most one row per pair that ever lost
  a grant, however often; a service account whose token lost its grants is
  a subject too. Past `SNAPSHOT_WARN_ROWS` a process logs one
  warning.

Failure semantics: until the first successful read `loaded()` is False and
`verify_token` refuses every token (503, retryable) — an empty set would let
revoked tokens through. After it, a failed refresh keeps the last good
snapshot for up to `jwt_revocation_max_stale_seconds`, so a short ClickHouse
outage does not take authentication down with it; past that bound every
token is refused again (503) until a refresh lands.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import time
from datetime import UTC, datetime, timedelta
from typing import Any

log = logging.getLogger(__name__)

# A token minted without `exp` never expires on its own, so its revocation
# must not either: the row's `expires_at` gates both the active filter
# (`expires_at > now()`) and the table TTL (10 days past it). The TTL casts
# `expires_at` to DateTime, whose range ends in 2106; a later value wraps to a
# past date and ClickHouse drops the row, so the revocation silently never
# lands. Every expiry is therefore clamped below that range.
NEVER_EXPIRES_DAYS = 3650
_LATEST_EXPIRY = datetime(2105, 12, 31)
# The snapshot size the refresh warns past: about 10 MB of keys per process,
# re-read on every refresh.
SNAPSHOT_WARN_ROWS = 100_000
# Whether this process has warned of the size once.
_warned_size = False


def revocation_expiry(expires_at: datetime | None, now: datetime) -> datetime:
    """The `expires_at` to store for a revocation: the token's own expiry (naive
    UTC), `NEVER_EXPIRES_DAYS` from `now` when it has none, and never past the
    table TTL's DateTime range."""
    if expires_at is None:
        expires_at = now + timedelta(days=NEVER_EXPIRES_DAYS)
    elif expires_at.tzinfo is not None:
        expires_at = expires_at.astimezone(UTC).replace(tzinfo=None)
    return min(expires_at, _LATEST_EXPIRY)


# Last-known-good revocation set. Replaced wholesale by `refresh`;
# read by `is_revoked` without a lock since frozenset is immutable and
# the global rebind is atomic on CPython.
_revoked_jtis: frozenset[str] = frozenset()
# Keys revoked only for tokens issued before the cutoff (unix seconds);
# replaced together with `_revoked_jtis` on every refresh.
_revoked_before: dict[str, int] = {}
# What this process wrote and when (time.monotonic()), folded into every
# snapshot for `_WRITTEN_KEEP_S`: a refresh whose read began before the write,
# or reached a replica the write had not, would otherwise drop it.
_written: dict[str, tuple[int | None, float]] = {}
_WRITTEN_KEEP_S = 300.0
# False until one refresh has succeeded in this process.
_loaded = False
# time.monotonic() of the last successful refresh; 0.0 = never.
_loaded_at = 0.0


def loaded() -> bool:
    """True once this process holds a revocation snapshot read from CH."""
    return _loaded


def snapshot_age_seconds() -> float:
    """Seconds since this process last read the revocation list; inf before
    the first successful read. /v1/health reports a stale snapshot, because a
    token revoked since then still authenticates here."""
    return time.monotonic() - _loaded_at if _loaded else math.inf


def is_revoked(jti: str, *, issued_at: int | None = None) -> bool:
    """True if `jti` (a jti or a sub) is revoked outright, or revoked for
    tokens issued before a cutoff and `issued_at` (the token's `iat`, unix
    seconds) is before it. A cutoff row never matches a caller that passes
    no `issued_at`. Sync, hot-path safe."""
    if not jti:
        return False
    if jti in _revoked_jtis:
        return True
    cutoff = _revoked_before.get(jti)
    return cutoff is not None and issued_at is not None and issued_at < cutoff


async def refresh() -> None:
    """Re-read the revocation set from CH and replace the in-memory copy."""
    from server.core.ch import get_client

    global _revoked_jtis, _revoked_before, _loaded, _loaded_at, _warned_size
    try:
        ch = get_client()
        rows = await ch.query_all(
            """
            SELECT jti, issued_before FROM loom.revoked_tokens FINAL
            WHERE expires_at > now()
            """,
        )
        full: set[str] = set()
        before: dict[str, int] = {}
        for r in rows:
            cutoff = _cutoff_seconds(r.get("issued_before"))
            if cutoff is None:
                full.add(str(r["jti"]))
            else:
                before[str(r["jti"])] = cutoff
        _fold_written(full, before)
        _revoked_jtis = frozenset(full)
        _revoked_before = before
        if len(rows) > SNAPSHOT_WARN_ROWS and not _warned_size:
            _warned_size = True
            log.warning(
                "revocation snapshot holds %d rows (past %d), most of them grant cutoffs; "
                "every process re-reads them on each refresh",
                len(rows),
                SNAPSHOT_WARN_ROWS,
            )
        _loaded = True
        _loaded_at = time.monotonic()
    except Exception as e:  # noqa: BLE001 — keep the last revocation set on a CH glitch
        log.warning(
            "revocation list fetch failed: %s — keeping last set of %d entries.",
            e,
            len(_revoked_jtis),
        )


def _cutoff_seconds(raw: Any) -> int | None:
    """An `issued_before` cell as unix seconds; None for NULL/''."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        dt = raw if raw.tzinfo is not None else raw.replace(tzinfo=UTC)
        return int(dt.timestamp())
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


async def revoke(
    token_id: str,
    *,
    expires_at: datetime | None = None,
    reason: str = "",
    revoked_by: str = "",
    issued_before: datetime | None = None,
) -> dict[str, Any]:
    """Revoke every token whose `jti` OR `sub` equals `token_id` and refresh
    this process's snapshot so `is_revoked` answers True at once.

    `verify_token` checks both claims against the one `jti` column: pass a
    `jti` to retire exactly one token (every `loom mint-token` / endpoint
    token carries one), or the `sub` for every token minted for a
    principal. `issued_before` narrows a `sub` revocation to the tokens whose
    `iat` is before it — the shape for "the old
    token leaked, the re-mint from an hour ago stays". `expires_at` is when
    the revocation may lapse — the token's own `exp`; None (a token without
    `exp`) keeps the row for `NEVER_EXPIRES_DAYS`. Returns the row as
    written. Raises on a CH failure (the caller decides how to report it; a
    revocation that did not land must not look landed).
    """
    from server.core.ch import get_client

    token_id = str(token_id or "").strip()
    if not token_id:
        raise ValueError("revoke: token_id (a jti or a sub) is required")
    now = datetime.now(UTC).replace(microsecond=0, tzinfo=None)
    expires_at = revocation_expiry(expires_at, now)
    if issued_before is not None and issued_before.tzinfo is not None:
        issued_before = issued_before.astimezone(UTC).replace(tzinfo=None)
    row: dict[str, Any] = {
        "jti": token_id,
        "revoked_at": now,
        "expires_at": expires_at,
        "reason": reason or "",
        "revoked_by": revoked_by or "",
        "issued_before": issued_before,
    }
    await get_client().insert("loom.revoked_tokens", [row])
    await refresh()
    _apply_locally([token_id], _cutoff_seconds(issued_before))
    return row


def _apply_locally(keys: list[str], cutoff: int | None) -> None:
    """Apply revocations this process just wrote, now and in every refresh
    for a while (`_fold_written`). `loaded()` is left as it is: rows this
    process wrote are not a snapshot.

    Here they replace what the snapshot holds, as the rows just inserted
    replace theirs in ClickHouse: a cutoff narrows a full revocation of the
    same key. A refresh only tightens with them (a full revocation, or a
    later cutoff), since another process may have written a newer row since."""
    global _revoked_jtis, _revoked_before
    now = time.monotonic()
    full, before = set(_revoked_jtis), dict(_revoked_before)
    for key in keys:
        _written[key] = (cutoff, now)
        if cutoff is None:
            full.add(key)
            before.pop(key, None)
        else:
            full.discard(key)
            before[key] = cutoff
    _revoked_jtis, _revoked_before = frozenset(full), before


def _tighten_locally(keys: list[str], cutoff: int) -> None:
    """`_apply_locally` for grant cutoffs, which only move forward: a write
    acknowledged after a newer one must not pull this process's cutoff back."""
    global _revoked_before
    now = time.monotonic()
    before = dict(_revoked_before)
    for key in keys:
        newest = max(cutoff, before.get(key, cutoff))
        before[key] = newest
        _written[key] = (newest, now)
    _revoked_before = before


def _fold_written(full: set[str], before: dict[str, int]) -> None:
    """Apply what this process wrote in the last `_WRITTEN_KEEP_S` to a
    snapshot being built, dropping older entries. A key never moves back: a
    newer cutoff, or a full revocation, from another process stands."""
    now = time.monotonic()
    for key, (cutoff, at) in list(_written.items()):
        if now - at > _WRITTEN_KEEP_S:
            del _written[key]
        elif cutoff is None:
            full.add(key)
            before.pop(key, None)
        elif key not in full:
            before[key] = max(cutoff, before.get(key, cutoff))


_GRANT_CLAIM_PREFIX = "grant:"


def grant_claim_key(org: str, namespace: str, subject: str) -> str:
    """The revocation key that withdraws (org, namespace) from `subject`'s
    token claims: `grant:["<org>","<namespace>","<subject hash>"]`. A JSON
    array, so an org or namespace holding `/` cannot make two scopes share a
    key. The subject is lower-cased, as alias lookup's SQL `lower()` matches
    it, and hashed so the key does not spell out an email; the hash is
    unsalted, so a guessed email can be tested against it. The `grant:` prefix
    keeps it apart from the jtis and subjects `verify_token` looks up."""
    digest = hashlib.sha256(subject.lower().encode()).hexdigest()[:32]
    return _GRANT_CLAIM_PREFIX + json.dumps([org, namespace, digest], separators=(",", ":"))


def grant_claim_prefix(org: str) -> str:
    """The start every one of `org`'s grant cutoff keys shares."""
    return f"{_GRANT_CLAIM_PREFIX}[{json.dumps(org)},"


def grant_cutoff_applies(org: str, namespace: str, subject: str, issued_at: int) -> bool:
    """Whether a cutoff for `subject` on (org, namespace) is later than a
    token issued at `issued_at` (unix seconds). Reads this process's
    snapshot: no I/O, for the request path."""
    return is_revoked(grant_claim_key(org, namespace, subject), issued_at=issued_at)


async def write_grant_cutoffs(
    org: str, namespaces: list[str], subjects: list[str], *, reason: str, revoked_by: str
) -> None:
    """Withdraw `namespaces` from the claims of every token `subjects` hold
    that was minted before now, under each spelling of each subject.

    Callers write this before the grant row it is about, so a failure fails
    their change. The grant write then runs `cut_where_lowered`, which stamps
    the cutoff again when the row landed after it."""
    await _write_cutoffs(
        org, [(ns, s) for ns in namespaces for s in subjects], reason=reason, revoked_by=revoked_by
    )


async def _write_cutoffs(
    org: str, pairs: list[tuple[str, str]], *, reason: str, revoked_by: str
) -> None:
    """One cutoff row per spelling of each (namespace, subject), in one insert."""
    from server.auth.aliases import cutoff_spellings
    from server.core.ch import get_client

    # A release cuts every member at once; read their spellings a few at a time.
    limit = asyncio.Semaphore(8)

    async def spellings_of(subject: str) -> list[str]:
        async with limit:
            return await cutoff_spellings(org, subject)

    subjects = sorted({s for _, s in pairs})
    reads = [asyncio.create_task(spellings_of(s)) for s in subjects]
    try:
        spellings = dict(zip(subjects, await asyncio.gather(*reads), strict=True))
    except BaseException:
        # Stop the other reads; a TaskGroup would wrap the refusal callers catch.
        for read in reads:
            read.cancel()
        await asyncio.gather(*reads, return_exceptions=True)
        raise
    keys = sorted({grant_claim_key(org, ns, alias) for ns, s in pairs for alias in spellings[s]})
    if not keys:
        return
    rows, cutoff = _cutoff_rows(keys, reason, revoked_by)
    await get_client().insert("loom.revoked_tokens", rows)
    # This process applies them now; every other at its next refresh.
    _tighten_locally(keys, cutoff)


async def cut_where_lowered(org: str, pairs: list[tuple[str, str]], *, revoked_by: str) -> None:
    """Cut each (namespace, subject) whose grant now ranks below a role it
    held since its last cutoff, in one write.

    Run after a grant write that read the grant first. It finds a raise that
    landed between that read and the write, and a lowering whose own cutoff
    was stamped before the row landed: a token minted in between still claims
    the old role. Every op that wrote through `grant_writes` runs it again on
    a retry, so a cut that failed here is written then."""
    from server.core.ch import get_client, own_write_reads

    namespaces = sorted({ns for ns, _ in pairs})
    subjects = sorted({s for _, s in pairs})
    with own_write_reads():
        rows = await get_client().query_all(
            """
            SELECT namespace, subject, role, status, revoked_at, version
            FROM loom.namespace_grant_log
            WHERE org = {org:String} AND namespace IN {ns:Array(String)}
              AND subject IN {subs:Array(String)}
            ORDER BY namespace, subject, version
            """,
            params={"org": org, "ns": namespaces, "subs": subjects},
        )
    keys = {pair: grant_claim_key(org, *pair) for pair in pairs}
    cutoffs = {
        str(r["jti"]): _cutoff_seconds(r.get("issued_before"))
        for r in await _read_revocations(sorted(keys.values()))
    }
    history: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in rows:
        history.setdefault((str(r["namespace"]), str(r["subject"])), []).append(r)
    lowered = [
        pair for pair in pairs if _lowered_since(history.get(pair, []), cutoffs.get(keys[pair]))
    ]
    if lowered:
        await _write_cutoffs(org, lowered, reason="grant lowered", revoked_by=revoked_by)


def _lowered_since(rows: list[dict[str, Any]], cutoff: int | None) -> bool:
    """Whether the last of `rows`, one subject's grant history oldest first,
    ranks below the row in force at `cutoff` (unix seconds) or any later one."""
    from server.operations.base import grant_rank

    if not rows:
        return False
    ranks = [
        grant_rank(r["role"]) if r["revoked_at"] is None and r["status"] == "active" else 0
        for r in rows
    ]
    first = 0
    if cutoff is not None:
        after = [i for i, r in enumerate(rows) if int(r["version"]) >= cutoff * 1_000_000_000]
        first = max(after[0] - 1, 0) if after else len(rows) - 1
    return max(ranks[first:]) > ranks[-1]


def _cutoff_rows(keys: list[str], reason: str, revoked_by: str) -> tuple[list[dict[str, Any]], int]:
    """One cutoff per key at the next whole second, so a token minted in the
    second of the change is cut too; and that second as unix seconds."""
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    issued_before = now + timedelta(seconds=1)
    cutoff = int(issued_before.replace(tzinfo=UTC).timestamp())
    rows = [
        {
            "jti": key,
            "revoked_at": now,
            "expires_at": revocation_expiry(None, now),
            "reason": reason,
            "revoked_by": revoked_by,
            "issued_before": issued_before,
            # The newest cutoff wins in ClickHouse too, however late its insert lands.
            "version": cutoff * 1_000_000_000,
        }
        for key in keys
    ]
    return rows, cutoff


async def read_grant_cutoffs(
    org: str, subject: str, namespaces: list[str], *, issued_at: int | None
) -> set[str]:
    """Which of `namespaces` have a cutoff for `subject` later than
    `issued_at` (unix seconds; None means any cutoff counts).

    Token mints call this instead of `grant_cutoff_applies`: it reads
    ClickHouse, not this process's snapshot, which may not have the newest
    cutoff yet. A later grant does not remove a cutoff."""
    from server.auth.aliases import cutoff_spellings

    spellings = await cutoff_spellings(org, subject)
    by_key = {grant_claim_key(org, ns, s): ns for ns in namespaces for s in spellings}
    if not by_key:
        return set()
    rows = await _read_revocations(sorted(by_key))
    ended: set[str] = set()
    for r in rows:
        cutoff = _cutoff_seconds(r.get("issued_before"))
        if cutoff is not None and (issued_at is None or issued_at < cutoff):
            ended.add(by_key[str(r["jti"])])
    return ended


async def subjects_revoked(subjects: list[str], *, issued_at: int) -> bool:
    """`is_revoked` for any of `subjects`, read from ClickHouse in one query:
    a token minting path cannot wait for this process's snapshot to see a
    `revoke-token`."""
    if not subjects:
        return False
    for r in await _read_revocations(sorted(set(subjects))):
        cutoff = _cutoff_seconds(r.get("issued_before"))
        if cutoff is None or issued_at < cutoff:
            return True
    return False


async def _read_revocations(keys: list[str]) -> list[dict[str, Any]]:
    """The unexpired revocation rows for `keys`, read as of every write that
    landed, on whichever replica the read reaches."""
    from server.core.ch import get_client, own_write_reads

    with own_write_reads():
        return await get_client().query_all(
            """
            SELECT jti, issued_before FROM loom.revoked_tokens FINAL
            WHERE jti IN {keys:Array(String)} AND expires_at > now()
            """,
            params={"keys": keys},
        )


async def list_active(*, include_grant_cutoffs: bool = False) -> list[dict[str, Any]]:
    """The unexpired revocations, newest first. Grant cutoffs, most of the
    table, are left out unless asked for."""
    from server.core.ch import get_client

    rows = await get_client().query_all(
        """
        SELECT jti, revoked_at, expires_at, reason, revoked_by, issued_before
        FROM loom.revoked_tokens FINAL
        WHERE expires_at > now() AND ({cutoffs:Bool} OR NOT startsWith(jti, {prefix:String}))
        ORDER BY revoked_at DESC
        """,
        params={"cutoffs": include_grant_cutoffs, "prefix": _GRANT_CLAIM_PREFIX},
    )
    return [dict(r) for r in rows]


async def load_at_boot() -> None:
    """One refresh at process start so `is_revoked` has a real set from the
    first request. If it fails, tokens are refused as not verifiable until a
    later refresh succeeds."""
    await refresh()


def start_refresh_loop(
    interval_s: float, *, name: str = "loom-revocation-refresh"
) -> asyncio.Task[None]:
    """Refresh this process's snapshot every `interval_s` seconds until the
    task is cancelled. For processes that do not hold the supervisor lease
    (every web process under `LOOM_BACKGROUND_WORKERS=off`, and the
    non-leader processes under `uvicorn --workers N`); the leader's
    supervisor worker does the same job there. `refresh` swallows CH
    errors, so the loop only ends on cancellation."""
    period = max(1.0, float(interval_s))

    async def _run() -> None:
        from server.core.ch import set_query_tag

        set_query_tag("worker:jwt_revocation_refresh")
        while True:
            await asyncio.sleep(period)
            try:
                await refresh()
            except Exception:  # pragma: no cover — refresh keeps its own guard
                log.exception("revocation refresh loop: unexpected error; continuing")

    return asyncio.get_running_loop().create_task(_run(), name=name)
