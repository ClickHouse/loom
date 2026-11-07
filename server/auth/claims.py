"""Sign-in state that changes once, kept in ClickHouse Keeper.

Whatever a sign-in spends or moves lives here, one key each, in
loom.oauth_signin_state (KeeperMap, schemas/migrations/040_oauth_signin_state.sql):
a code, a device grant, a single-use mark per identity provider callback the
provider vouched for, an MCP approval awaiting its click, and the current
refresh token of each session. Every change is a compare-and-set (`swap`):
a strict-mode KeeperMap write that Keeper applies only while the key
still holds the value the request read. Of two requests spending one
credential exactly one wins, however slow either is and whichever ClickHouse
replica serves it; nothing here depends on time.

Callers make every value unique (a nonce in it), so reading our value back
proves our write landed. Each key carries the expiry of what it holds and is
swept once it passes; the table's key limit is a backstop against a flood of
unauthenticated sign-in starts, and /v1/health says when it is reached.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from typing import Any

from server.core import shielded

log = logging.getLogger(__name__)

_FULL_FOR_S = 900  # how long hitting the key limit stays a health problem
UNDO_S = 5  # an undo Keeper does not finish by then is left to the key's expiry
# A write the server gives up on rather than lands late, after a request that
# timed out has put its credential back or another has called it abandoned.
_BOUNDED = "max_execution_time = 5"

# When an insert last found the table at its key limit (monotonic seconds).
_full_at: float | None = None


class Busy(Exception):
    """Keeper cannot answer: the request should retry."""


class Changed(Busy):
    """Someone else changed the key since this request read it."""


def nonce() -> str:
    return secrets.token_hex(8)


def key_exists(e: BaseException) -> bool:
    """Keeper's strict-mode answer to an insert of a key that is there."""
    return "Node exists" in str(e)


def keeper_full() -> bool:
    """Whether a sign-in found the table at its key limit lately."""
    return _full_at is not None and time.monotonic() - _full_at < _FULL_FOR_S


def _lost_race(e: BaseException) -> bool:
    """Keeper's answer to a strict-mode write on a key someone else changed."""
    return key_exists(e) or "Bad version" in str(e) or "No node" in str(e)


async def get(ch: Any, key: str) -> str | None:
    try:
        row = await ch.query_one(
            "SELECT value FROM loom.oauth_signin_state WHERE claim_key = {k:String}",
            params={"k": key},
        )
    except Exception as e:  # Keeper unreadable: the request retries
        raise Busy(key) from e
    return str(row["value"]) if row else None


async def swap(ch: Any, key: str, old: str | None, new: str, expires_at: int) -> str | None:
    """Set `key` to `new`, kept until `expires_at` (epoch seconds), if it
    holds `old` (None: if it is absent). Returns what it holds afterwards:
    `new` if this call's write landed."""
    # A cancelled request does not stop ClickHouse running the write, so the
    # write and its read-back finish as one, and only then is a cancelled
    # write undone: an undo run first would find nothing and miss it.
    write = asyncio.ensure_future(_swap(ch, key, old, new, expires_at))
    try:
        return await asyncio.shield(write)
    except Busy:
        # The write may have landed before Keeper failed: undo it, so the
        # request told to retry finds the key as it read it.
        with contextlib.suppress(Busy, TimeoutError):
            await _undo(ch, key, old, new, expires_at)
        raise
    except asyncio.CancelledError:
        # Held to its end, before whoever cancelled puts the credential back.
        await shielded.shielded(
            _undo_once_settled(write, ch, key, old, new, expires_at), name="undo"
        )
        raise


async def _undo_once_settled(
    write: asyncio.Future[str | None], ch: Any, key: str, old: str | None, new: str, expires_at: int
) -> None:
    """Wait for the write, then undo it if it landed: both within UNDO_S."""
    try:
        async with asyncio.timeout(UNDO_S):
            try:
                landed = (await write) == new
            except Busy:
                landed = True  # unknown: the undo changes the key only if it holds `new`
            if landed:
                await _revert(ch, key, old, new, expires_at)
    except (Busy, TimeoutError) as e:  # left as it is; a spend then waits out its abandonment
        log.warning("oauth claims: could not undo a cancelled change of %s: %s", key, e)


async def _undo(ch: Any, key: str, old: str | None, new: str, expires_at: int) -> None:
    async with asyncio.timeout(UNDO_S):
        await _revert(ch, key, old, new, expires_at)


async def _revert(ch: Any, key: str, old: str | None, new: str, expires_at: int) -> None:
    """Put `key` back to `old` if it holds `new` (unique: a nonce in it)."""
    if old is None:
        await _drop(ch, key, new)
    else:
        await _swap(ch, key, new, old, expires_at)


async def _swap(ch: Any, key: str, old: str | None, new: str, expires_at: int) -> str | None:
    params = {"k": key, "v": new, "old": old or "", "e": expires_at}
    for attempt in range(3):
        try:
            if old is None:
                await ch.execute(
                    "INSERT INTO loom.oauth_signin_state (claim_key, value, expires_at) "
                    f"SETTINGS keeper_map_strict_mode = 1, {_BOUNDED} "
                    "VALUES ({k:String}, {v:String}, {e:UInt32})",
                    params=params,
                )
            else:
                await ch.execute(
                    "ALTER TABLE loom.oauth_signin_state UPDATE value = {v:String}, "
                    "expires_at = {e:UInt32} "
                    "WHERE claim_key = {k:String} AND value = {old:String} "
                    f"SETTINGS keeper_map_strict_mode = 1, {_BOUNDED}",
                    params=params,
                )
        except Exception as e:  # a lost race is an answer; Keeper failing is a retry
            if not _lost_race(e):
                _note_full(e)
                log.warning("oauth claims: Keeper refused %s: %s", key, e)
                raise Busy(key) from e
        now = await get(ch, key)
        if now != old:  # ours, or the value another request wrote first
            return now
        # Still `old`: the write has not run, or this read came from a Keeper
        # replica a moment behind it.
        await asyncio.sleep(0.05 * (attempt + 1))
    raise Busy(key)


def _note_full(e: BaseException) -> None:
    global _full_at
    if "LIMIT_EXCEEDED" in str(e):
        _full_at = time.monotonic()


async def _drop(ch: Any, key: str, value: str) -> None:
    try:
        await ch.execute(
            "ALTER TABLE loom.oauth_signin_state DELETE WHERE claim_key = {k:String} "
            "AND value = {v:String} "
            f"SETTINGS keeper_map_strict_mode = 1, {_BOUNDED}",
            params={"k": key, "v": value},
        )
    except Exception as e:  # someone else changed it since: theirs stands
        if not _lost_race(e):
            raise Busy(key) from e


async def sweep() -> None:
    """Drop keys whose credential has expired."""
    from server.core.ch import get_client

    ch = get_client()
    try:
        # Strict: a key renewed after this read it is not deleted.
        await ch.execute(
            "ALTER TABLE loom.oauth_signin_state DELETE WHERE expires_at < toUInt32(now()) "
            "SETTINGS keeper_map_strict_mode = 1"
        )
    # A key changed meanwhile: this sweep deletes nothing; the next one tries again.
    except Exception as e:
        if not _lost_race(e):
            raise
        log.info("oauth claims: a key changed during the sweep; left for the next one")
