"""Token revocation, backed by `loom.revoked_tokens`.

Retiring a signing key (`loom signing-key retire`) kills every token of that
kid; this is the per-credential path. A row keyed by a `jti` revokes one token, keyed by a
`sub` every token of that principal — or, with an `issued_before` cutoff,
only those whose `iat` precedes it, so a re-mint after the cutoff survives.
The table is ReplacingMergeTree ORDER BY jti: the newest row per key governs.

- `revoke` is the write path (`loom revoke-token`, the admin endpoint) and
  takes effect in this process at once, even when the re-read after the
  insert fails; `list_active` is its read. `cut_off` writes an operator
  action's cutoff (adopt, a bot replaced or disabled) under its own key
  beside the subject's, so neither ever replaces the other; `scim_revoke`
  writes SCIM's revocation of a subject under another (`SCIM_PREFIX`).
- Every process that verifies tokens holds its own snapshot: `load_at_boot`
  fills it, and `start_refresh_loop` re-reads it each
  `jwt_revocation_cache_ttl_seconds`.
- `is_revoked` is a frozenset/dict lookup — no I/O, safe from sync verify.

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

from server.core import readiness

log = logging.getLogger(__name__)

# A token minted without `exp` never expires on its own, so its revocation
# must not either: the row's `expires_at` gates both the active filter
# (`expires_at > now()`) and the table TTL (10 days past it). The TTL casts
# `expires_at` to DateTime, whose range ends in 2106; a later value wraps to a
# past date and ClickHouse drops the row, so the revocation silently never
# lands. Every expiry is therefore clamped below that range.
_LATEST_EXPIRY = datetime(2105, 12, 31)

# How far another replica's clock may run ahead of this one's: a token it
# issues as of a later second than the cutoff would outlive the revocation.
CLOCK_SKEW_S = 5


def from_now_on() -> datetime:
    """A cutoff past every token issued so far, on any replica: revocation
    compares whole seconds, and the token's `iat` is another clock's."""
    return datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=1 + CLOCK_SKEW_S)


async def wait_out(cutoff: datetime) -> None:
    """Return once every replica's clock is past `cutoff`: one running behind
    this one stamps an earlier `iat`."""
    await asyncio.sleep(max(0.0, cutoff.timestamp() + CLOCK_SKEW_S - time.time()))


def revocation_expiry(expires_at: datetime | None) -> datetime:
    """The `expires_at` to store for a revocation: the token's own expiry (naive
    UTC), never past the table TTL's DateTime range, which is also where one
    without an expiry lands."""
    if expires_at is None:
        # A token that never expires stays revoked as long as the table can hold it.
        return _LATEST_EXPIRY
    if expires_at.tzinfo is not None:
        expires_at = expires_at.astimezone(UTC).replace(tzinfo=None)
    return min(expires_at, _LATEST_EXPIRY)


# Last-known-good revocation set. Replaced wholesale by `refresh`;
# read by `is_revoked` without a lock since frozenset is immutable and
# the global rebind is atomic on CPython.
_revoked_jtis: frozenset[str] = frozenset()
# Keys revoked only for tokens issued before the cutoff (unix seconds);
# replaced together with `_revoked_jtis` on every refresh.
_revoked_before: dict[str, int] = {}
# Operator cutoffs (adopt, a bot replaced or disabled) are stored under
# CUTOFF_PREFIX + subject, beside the subject's own row, so neither replaces
# the other. The NUL keeps the key apart from every subject: no command line
# can pass one.
CUTOFF_PREFIX = "\x00cutoff:"
# Their snapshot, read beside `_revoked_before`.
_cut_off: dict[str, int] = {}
# SCIM's revocation of a subject (server/auth/scim.py) is stored under
# SCIM_PREFIX + subject, also beside the subject's own row, so an operator's
# revocations and SCIM's never replace each other. Every reader takes it as it
# takes the subject's own row, and a subject is revoked while either says so.
# The snapshot holds it in `_revoked_jtis` / `_revoked_before` under that key.
SCIM_PREFIX = "\x00scim:"
# Writes folded into the snapshot here, kept a while: every refresh lays them
# over what it read, so neither a read already in flight nor one from a replica
# behind drops them. One is laid back only while the read holds no newer row
# for its key: a narrowing written elsewhere since wins over an outright
# revoke made here. (kind, key, cutoff, when, version): kind "revoke" or
# "cut_off"; version is the row's.
_recent: list[tuple[str, str, int | None, float, int]] = []
_RECENT_S = 300.0  # far longer than a read takes
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


def token_state_ready(loaded: bool, age_s: float, what: str) -> readiness.CheckResult:
    """Readiness of state every token check reads: loaded, and no older than
    `jwt_revocation_max_stale_seconds`. Always ready with auth off."""
    from server.settings import settings

    if not settings.auth_actually_required():
        return readiness.CheckResult(True, "auth off")
    if not loaded:
        return readiness.CheckResult(False, f"{what} not loaded")
    max_stale = float(settings.jwt_revocation_max_stale_seconds)
    if age_s > max_stale:
        return readiness.CheckResult(False, f"{what} {age_s:.0f} s old (> {max_stale:.0f} s)")
    return readiness.CheckResult(True)


def readiness_check() -> readiness.CheckResult:
    return token_state_ready(loaded(), snapshot_age_seconds(), "revocation snapshot")


def is_revoked(jti: str, *, issued_at: int | None = None) -> bool:
    """True if `jti` (a jti or a sub) is revoked outright, or revoked for
    tokens issued before a cutoff and `issued_at` (the token's `iat`, unix
    seconds) is before it. A cutoff row never matches a caller that passes
    no `issued_at`. SCIM's revocation of `jti` counts as its own row does.
    Sync, hot-path safe."""
    if not jti:
        return False
    keys = (jti, SCIM_PREFIX + jti)
    if any(key in _revoked_jtis for key in keys):
        return True
    if issued_at is None:
        return False
    cutoff = max(_cut_off.get(jti, 0), *(_revoked_before.get(key, 0) for key in keys))
    return issued_at < cutoff


async def revoked_outright(jti: str) -> bool:
    """`is_revoked` read from loom.revoked_tokens itself, for a check that
    cannot wait for this process's copy to catch up with another's write."""
    from server.core.ch import get_client, own_write_reads

    if is_revoked(jti):
        return True
    with own_write_reads():  # written a moment ago, maybe through another replica
        row = await get_client().query_one(
            """
            SELECT 1 FROM loom.revoked_tokens FINAL
            WHERE jti IN {keys:Array(String)} AND expires_at > now() AND issued_before IS NULL
            UNION ALL
            SELECT 1 FROM loom.oauth_state FINAL
            WHERE record_type = 'revocation_owed' AND id = {j:String}
              AND expires_at > toUInt32(now())
            """,
            params={"j": jti, "keys": [jti, SCIM_PREFIX + jti]},
        )
    return row is not None


async def refresh() -> None:
    """Re-read the revocation set from CH and replace the in-memory copy."""
    from server.core.ch import get_client

    global _revoked_jtis, _revoked_before, _cut_off, _loaded, _loaded_at
    try:
        ch = get_client()
        # A session whose revocation could not be written gets a
        # revocation_owed row instead (oauth_store.owe_revocation), which
        # stands in for it as long as a session revocation lasts (30 days).
        rows = await ch.query_all(
            """
            SELECT jti, issued_before, version FROM loom.revoked_tokens FINAL
            WHERE expires_at > now()
            UNION ALL
            SELECT id AS jti, NULL AS issued_before, toUInt64(0) AS version
            FROM loom.oauth_state FINAL
            WHERE record_type = 'revocation_owed' AND expires_at > toUInt32(now())
            """,
        )
        full: set[str] = set()
        before: dict[str, int] = {}
        operator: dict[str, int] = {}
        versions: dict[str, int] = {}
        for r in rows:
            key, cutoff = str(r["jti"]), _cutoff_seconds(r.get("issued_before"))
            versions[key] = max(versions.get(key, 0), int(r.get("version") or 0))
            if key.startswith(CUTOFF_PREFIX):
                operator[key.removeprefix(CUTOFF_PREFIX)] = cutoff or 0
            elif cutoff is None:
                full.add(key)
            else:
                before[key] = cutoff
        # Writes made here lately, which the read (in flight, or behind) may lack.
        _forget_old()
        for kind, key, cutoff, _, version in _recent:
            stored = CUTOFF_PREFIX + key if kind == "cut_off" else key
            if versions.get(stored, 0) > version:
                continue  # the read holds a newer row for it, which governs
            if kind == "cut_off":
                operator[key] = max(operator.get(key, 0), cutoff or 0)
            elif cutoff is None:
                full.add(key)
                before.pop(key, None)
            else:  # the newest write of its key: it replaces what was there
                full.discard(key)
                before[key] = cutoff
        _revoked_jtis = frozenset(full)
        _revoked_before, _cut_off = before, operator
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
    reread: bool = True,
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
    `exp`) keeps the row as long as the table can. Returns the row as
    written. Raises on a CH failure (the caller decides how to report it; a
    revocation that did not land must not look landed).
    """
    token_id = str(token_id or "").strip()
    if not token_id:
        raise ValueError("revoke: token_id (a jti or a sub) is required")
    if "\x00" in token_id:  # no subject has one; the prefixed keys are cut_off's and SCIM's
        raise ValueError("revoke: a jti or sub may not contain a NUL character")
    return await _write(
        token_id,
        expires_at=expires_at,
        reason=reason,
        revoked_by=revoked_by,
        issued_before=issued_before,
        reread=reread,
    )


async def scim_revoke(
    subject: str,
    *,
    reason: str,
    revoked_by: str,
    issued_before: datetime | None = None,
    reread: bool = True,
) -> dict[str, Any]:
    """Write SCIM's revocation of `subject` under its own key: outright, or with
    `issued_before` only the tokens issued before it. It never lapses, and it
    leaves the subject's own row (an operator's) as it is."""
    return await _write(
        SCIM_PREFIX + subject,
        expires_at=None,
        reason=reason,
        revoked_by=revoked_by,
        issued_before=issued_before,
        reread=reread,
    )


async def _write(
    key: str,
    *,
    expires_at: datetime | None,
    reason: str,
    revoked_by: str,
    issued_before: datetime | None,
    reread: bool,
) -> dict[str, Any]:
    from server.core.ch import get_client

    now = datetime.now(UTC).replace(microsecond=0, tzinfo=None)
    expires_at = revocation_expiry(expires_at)
    if issued_before is not None and issued_before.tzinfo is not None:
        issued_before = issued_before.astimezone(UTC).replace(tzinfo=None)
    row: dict[str, Any] = {
        "jti": key,
        "revoked_at": now,
        "expires_at": expires_at,
        "reason": reason or "",
        "revoked_by": revoked_by or "",
        "issued_before": issued_before,
        "version": await _next_version(key),
    }
    await get_client().insert("loom.revoked_tokens", [row])
    if reread:  # a caller writing several re-reads once, after them
        await refresh()
    _apply_locally(key, _cutoff_seconds(issued_before), row["version"])
    return row


async def cut_off(subject: str, *, issued_before: datetime, reason: str, revoked_by: str) -> None:
    """Revoke the tokens of `subject` issued before `issued_before`, leaving any
    outright revocation of it as it is."""
    from server.core.ch import get_client

    now = datetime.now(UTC).replace(microsecond=0, tzinfo=None)
    cutoff = issued_before.astimezone(UTC).replace(tzinfo=None)
    version = await _next_version(CUTOFF_PREFIX + subject)
    await get_client().insert(
        "loom.revoked_tokens",
        [
            {
                "jti": CUTOFF_PREFIX + subject,
                "revoked_at": now,
                "expires_at": revocation_expiry(None),
                "reason": reason,
                "revoked_by": revoked_by,
                "issued_before": cutoff,
                "version": version,
            }
        ],
    )
    await refresh()  # as `revoke` does; the fold below covers a re-read that missed it
    global _cut_off
    seconds = _cutoff_seconds(cutoff) or 0
    _cut_off = {**_cut_off, subject: max(_cut_off.get(subject, 0), seconds)}
    _remember("cut_off", subject, seconds, version)


async def _next_version(jti: str) -> int:
    from server.core.ch import get_client
    from server.core.versions import next_version

    return await next_version(get_client(), "loom.revoked_tokens", "jti", jti)


async def has_outright_row(jti: str) -> bool:
    """Whether loom.revoked_tokens itself revokes `jti` outright: what a cutoff
    written under that key replaces (an owed session revocation it does not)."""
    from server.core.ch import get_client, own_write_reads

    with own_write_reads():
        row = await get_client().query_one(
            """
            SELECT 1 AS x FROM loom.revoked_tokens FINAL
            WHERE jti = {j:String} AND expires_at > now() AND issued_before IS NULL
            """,
            params={"j": jti},
        )
    return row is not None


def revoked_here(jti: str) -> None:
    """Refuse `jti` in this process now, for a revocation written elsewhere
    (an owed one) that the next snapshot will carry."""
    _apply_locally(jti, None)


def _apply_locally(key: str, cutoff: int | None, version: int | None = None) -> None:
    """Fold a revocation this process just wrote into its snapshot. The
    re-read in `revoke` can fail, or land on a replica the insert has not
    reached; either way the old snapshot would keep admitting the token
    here until the next refresh. The written row is the newest for its key,
    so here it replaces any earlier state of that key (a refresh lays it back
    for `_RECENT_S`, unless it reads a newer row). `version` is the row's; one
    written elsewhere (an owed one) counts as written now. `loaded()` is left
    as it is: one known row is not a snapshot."""
    global _revoked_jtis, _revoked_before
    before = {k: v for k, v in _revoked_before.items() if k != key}
    if cutoff is None:
        _revoked_jtis = _revoked_jtis | {key}
    else:
        _revoked_jtis = _revoked_jtis - {key}
        before[key] = cutoff
    _revoked_before = before
    _remember("revoke", key, cutoff, version or time.time_ns())


def _remember(kind: str, key: str, cutoff: int | None, version: int) -> None:
    _forget_old()
    _recent.append((kind, key, cutoff, time.monotonic(), version))


def _forget_old() -> None:
    now = time.monotonic()
    _recent[:] = [w for w in _recent if now - w[3] < _RECENT_S]


async def subjects_revoked(subjects: list[str], *, issued_at: int) -> bool:
    """`is_revoked` for any of `subjects`, read from ClickHouse in one query:
    a token minting path cannot wait for this process's snapshot to see a
    `revoke-token`."""
    if not subjects:
        return False
    for r in await _read_revocations(_keys_of(subjects)):
        cutoff = _cutoff_seconds(r.get("issued_before"))
        if cutoff is None or issued_at < cutoff:
            return True
    return False


async def cutoffs(subjects: list[str]) -> dict[str, int | None]:
    """Each of `subjects` that is revoked, read from ClickHouse as `refresh`
    reads it: None when outright, else the cutoff (unix seconds) a token must
    be issued at or after to live. A subject not revoked is left out."""
    found: dict[str, int | None] = {}
    if not subjects:
        return found
    for r in await _read_revocations(_keys_of(subjects)):
        key, cutoff = str(r["jti"]), _cutoff_seconds(r.get("issued_before"))
        subject = key.removeprefix(CUTOFF_PREFIX).removeprefix(SCIM_PREFIX)
        if cutoff is None and not key.startswith(CUTOFF_PREFIX):
            found[subject] = None  # its own row or SCIM's, outright
        elif found.get(subject, 0) is not None:
            found[subject] = max(found.get(subject) or 0, cutoff or 0)
    return found


async def scim_revocations(subjects: list[str]) -> dict[str, dict[str, Any]]:
    """SCIM's revocation of each of `subjects` that has one, read from
    ClickHouse: its `issued_before` (None when outright) and `revoked_by`."""
    if not subjects:
        return {}
    rows = await _read_revocations(sorted({SCIM_PREFIX + s for s in subjects}))
    return {str(r["jti"]).removeprefix(SCIM_PREFIX): r for r in rows}


def _keys_of(subjects: list[str]) -> list[str]:
    """Every key a revocation of `subjects` is stored under."""
    return sorted({k for s in subjects for k in (s, CUTOFF_PREFIX + s, SCIM_PREFIX + s)})


async def _read_revocations(keys: list[str]) -> list[dict[str, Any]]:
    """The unexpired revocation rows for `keys`, read as of every write that
    landed, on whichever replica the read reaches."""
    from server.core.ch import get_client, own_write_reads

    with own_write_reads():
        return await get_client().query_all(
            """
            SELECT jti, issued_before, revoked_by FROM loom.revoked_tokens FINAL
            WHERE jti IN {keys:Array(String)} AND expires_at > now()
            """,
            params={"keys": keys},
        )


async def list_active() -> list[dict[str, Any]]:
    """The unexpired revocations, newest first — the same rows the refresh
    worker reads every `jwt_revocation_cache_ttl_seconds`, so this read is
    already paid for at that cadence (the table holds well under 1000 rows).
    An operator cutoff's row says `kind: cutoff`, SCIM's `kind: scim`, and
    each names its subject."""
    from server.core.ch import get_client

    rows = await get_client().query_all(
        """
        SELECT jti, revoked_at, expires_at, reason, revoked_by, issued_before
        FROM loom.revoked_tokens FINAL
        WHERE expires_at > now()
        ORDER BY revoked_at DESC
        """,
    )
    out = []
    for r in rows:
        key = str(r["jti"])
        kind = "revocation"
        if key.startswith(CUTOFF_PREFIX):
            kind = "cutoff"
        elif key.startswith(SCIM_PREFIX):
            kind = "scim"
        subject = key.removeprefix(CUTOFF_PREFIX).removeprefix(SCIM_PREFIX)
        out.append({**r, "jti": subject, "kind": kind})
    return out


async def load_at_boot() -> None:
    """One refresh at process start so `is_revoked` has a real set from the
    first request. If it fails, tokens are refused as not verifiable until a
    later refresh succeeds."""
    await refresh()


def start_refresh_loop(
    interval_s: float, *, name: str = "loom-revocation-refresh"
) -> asyncio.Task[None]:
    """Refresh this process's snapshot every `interval_s` seconds until the
    task is cancelled. `refresh` swallows CH errors, so the loop only ends on
    cancellation."""
    period = max(1.0, float(interval_s))

    async def _run() -> None:
        from server.core.ch import set_query_tag

        set_query_tag("revocation.refresh")  # every process runs it, not a worker
        while True:
            await asyncio.sleep(period)
            try:
                await refresh()
            except Exception:  # pragma: no cover — refresh keeps its own guard
                log.exception("revocation refresh loop: unexpected error; continuing")

    return asyncio.get_running_loop().create_task(_run(), name=name)
