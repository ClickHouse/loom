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
- Taking a grant away (`end_grant_claims`) writes a cutoff row keyed by
  `grant_claim_key`: tokens of that subject minted before it no longer reach
  the namespace through their own claim (`authz.withdrawn_claims`).
- The snapshot therefore grows with every grant ever taken away: one row
  per namespace and spelling of the subject, kept `NEVER_EXPIRES_DAYS` (a
  sign-in mints past any cutoff, so none may lapse while the owner row
  stands). Each is a short key and an int, and every refresh re-reads them
  all; past `SNAPSHOT_WARN_ROWS` a process logs one warning so an operator
  sees the read and the memory grow before they matter.

Failure semantics: until the first successful read `loaded()` is False and
`verify_token` refuses every token (503, retryable) — an empty set would let
revoked tokens through. After it, a failed refresh keeps the last good
snapshot for up to `jwt_revocation_max_stale_seconds`, so a short ClickHouse
outage does not take authentication down with it; past that bound every
token is refused again (503) until a refresh lands.
"""

from __future__ import annotations

import asyncio
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
    """Fold revocations this process just wrote into its snapshot. The
    re-read after the insert can fail, or land on a replica the insert has
    not reached; either way the old snapshot would keep admitting the token
    here until the next refresh. The written rows are the newest for their
    keys, so they replace any earlier state of those keys. `loaded()` is left
    as it is: rows this process wrote are not a snapshot."""
    global _revoked_jtis, _revoked_before
    written = set(keys)
    before = {k: v for k, v in _revoked_before.items() if k not in written}
    if cutoff is None:
        _revoked_jtis = _revoked_jtis | written
    else:
        _revoked_jtis = _revoked_jtis - written
        before.update(dict.fromkeys(written, cutoff))
    _revoked_before = before


_GRANT_CLAIM_PREFIX = "grant:"


def grant_claim_key(org: str, namespace: str, subject: str) -> str:
    """The revocation key that withdraws (org, namespace) from `subject`'s
    token claims. Never a jti (a hex id) or a subject verify checks."""
    return f"{_GRANT_CLAIM_PREFIX}{org}/{namespace}/{subject}"


def is_grant_claim_key(key: str) -> bool:
    return key.startswith(_GRANT_CLAIM_PREFIX)


def claim_ended(org: str, namespace: str, subject: str, issued_at: int) -> bool:
    """True if `subject`'s grant on (org, namespace) was taken away after a
    token issued at `issued_at` (unix seconds) claimed it. Sync, no I/O."""
    return is_revoked(grant_claim_key(org, namespace, subject), issued_at=issued_at)


async def end_grant_claims(
    org: str, namespaces: list[str], subjects: list[str], *, reason: str, revoked_by: str
) -> None:
    """Withdraw each of `namespaces` from the claims of every token `subjects`
    hold that was minted before now, under each spelling of each subject.

    Called before a grant is revoked or lowered: a grant row is replaced by
    the subject's next one (a join request, a self-join), so only a row of
    its own keeps the revocation. A later grant does not lift it. The cutoff
    is the next whole second, so a token minted in the second of the
    revocation is cut too. Raises on a ClickHouse failure, so the caller's
    grant write does not land without it."""
    from server.auth.aliases import subject_aliases
    from server.core.ch import get_client

    spellings = await asyncio.gather(*(subject_aliases(org, s, strict=True) for s in subjects))
    keys = sorted(
        {
            grant_claim_key(org, ns, alias)
            for ns in namespaces
            for group in spellings
            for alias in group
        }
    )
    if not keys:
        return
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    cutoff = now + timedelta(seconds=1)
    rows = [
        {
            "jti": key,
            "revoked_at": now,
            "expires_at": revocation_expiry(None, now),
            "reason": reason,
            "revoked_by": revoked_by,
            "issued_before": cutoff,
        }
        for key in keys
    ]
    await get_client().insert("loom.revoked_tokens", rows)
    await refresh()
    _apply_locally(keys, _cutoff_seconds(cutoff))


async def ended_claims(
    org: str, subject: str, namespaces: list[str], *, issued_at: int | None
) -> set[str]:
    """The `namespaces` a grant of `subject`'s was taken away on after
    `issued_at` (unix seconds; None: at any time), read from ClickHouse.

    For the paths that mint a token (sign-in, OAuth code exchange, refresh):
    the token's iat is past every cutoff, so a cutoff this process's snapshot
    has not caught up with yet would never apply to it.

    A namespace the subject was given a live admin grant on since its cutoff
    is not ended: only another admin can give that (a self-join stops below
    it, a restore grants no admin over an existing namespace), and a claim
    confers no more than it, so an owner an operator reinstated is not kept
    out of their claim. An admin row granted before the cutoff is the one the
    revoke is replacing (the cutoff lands first), so it lifts nothing."""
    from server.auth.aliases import subject_aliases

    spellings = await subject_aliases(org, subject, strict=True)
    by_key = {grant_claim_key(org, ns, s): ns for ns in namespaces for s in spellings}
    if not by_key:
        return set()
    rows = await _read_revocations(sorted(by_key))
    ended: dict[str, int] = {}  # namespace -> its latest cutoff
    for r in rows:
        cutoff = _cutoff_seconds(r.get("issued_before"))
        if cutoff is not None and (issued_at is None or issued_at < cutoff):
            ns = by_key[str(r["jti"])]
            ended[ns] = max(cutoff, ended.get(ns, 0))
    if not ended:
        return set()
    regranted = await _admin_granted_since(org, spellings, ended)
    return {ns for ns in ended if ns not in regranted}


async def _admin_granted_since(org: str, spellings: list[str], cutoffs: dict[str, int]) -> set[str]:
    """The namespaces of `cutoffs` one of the subject's `spellings` holds a
    live admin grant on, granted at or after that namespace's cutoff (unix
    seconds; grant rows are stamped to the second, the cutoff is the second
    after its write). Read as of every write that landed: a lagging replica
    must not show a grant a revoke already ended."""
    from server.core.ch import get_client, own_write_reads
    from server.core.grant_sql import LIVE_GRANT

    with own_write_reads():
        rows = await get_client().query_all(
            f"""
            SELECT namespace, toUnixTimestamp(max(granted_at)) AS granted
            FROM loom.namespace_grants FINAL
            WHERE org = {{org:String}} AND namespace IN {{nss:Array(String)}}
              AND lower(subject) IN {{subs:Array(String)}} AND role = 'admin' AND {LIVE_GRANT}
            GROUP BY namespace
            """,
            params={
                "org": org,
                "nss": sorted(cutoffs),
                "subs": [s.lower() for s in spellings],
            },
        )
    return {
        str(r["namespace"])
        for r in rows
        if int(r["granted"]) >= cutoffs.get(str(r["namespace"]), 2**62)
    }


async def subject_revoked(subject: str, *, issued_at: int) -> bool:
    """`is_revoked` for a subject, read from ClickHouse: a token minting path
    cannot wait for this process's snapshot to see a `revoke-token`."""
    for r in await _read_revocations([subject]):
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


async def list_active() -> list[dict[str, Any]]:
    """The unexpired revocations, newest first — the same rows the refresh
    worker reads every `jwt_revocation_cache_ttl_seconds`, so this read is
    already paid for at that cadence. Most rows are grant cutoffs, one per
    namespace and spelling of each subject a grant was taken from."""
    from server.core.ch import get_client

    rows = await get_client().query_all(
        """
        SELECT jti, revoked_at, expires_at, reason, revoked_by, issued_before
        FROM loom.revoked_tokens FINAL
        WHERE expires_at > now()
        ORDER BY revoked_at DESC
        """,
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
