"""Attempt limits for the auth routes that run before a token exists to
rate-limit by: sign-up, sign-in, OIDC sign-in starts and workload-token
exchanges.

The attempts are rows of `loom.auth_attempts`, so every Loom process of the
deployment counts the same ones. A key is stored as an HMAC keyed by the root
secret, never as the email or address it is: a plain hash of an IPv4 address
is reversed by hashing all of them.

What a refused attempt costs is bounded per process: a key this process saw
over its limit is refused without a query for `_KNOWN_OVER_S`, and at most
`auth_attempts_concurrency` checks run at once (`slot`), so a burst waits,
then is refused (429; 503 for an exchange), instead of taking the ClickHouse
pool every route shares.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import time
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from server.core.ch import sequential_reads
from server.core.ch_writes import batched_insert_settings
from server.core.repo_base import _default_client

TABLE = "loom.auth_attempts"
# Seconds a key seen over its limit is refused here without a query.
_KNOWN_OVER_S = 10.0
# Keys remembered at most; a spray of distinct keys must not grow it unbounded.
_KNOWN_OVER_MAX = 10_000
# Seconds a check waits for a free slot before it is refused.
_SLOT_WAIT_S = 1.0


class Busy(Exception):
    """Every auth-check slot of this process stayed taken for `_SLOT_WAIT_S`."""


@dataclass(frozen=True)
class Attempts:
    """At most `limit` attempts of `kind` per key within `window_s` seconds."""

    kind: str
    limit: int
    window_s: int

    async def allow(self, key: str) -> bool:
        """Admit an attempt that counts whatever its outcome (a sign-up). An
        admitted one is recorded before its recount, so concurrent attempts
        each count the others that landed first and at most `limit` are
        admitted. A caller refused at the first count is not recorded, so one
        that keeps retrying is let in again as its attempts age out; one
        refused at the recount (it raced others past the first count) stays
        recorded. A process too busy to check refuses."""
        try:
            async with slot():
                return await _admit([(self, key)], released=False) is not None
        except Busy:
            return False

    async def blocked(self, key: str) -> bool:
        """Whether `key` is at its limit, for a limit that counts failures only
        (a wrong device code, a refused callback): checked before the attempt,
        `record`ed after one that failed. A process too busy to check refuses."""
        known = (self.kind, _digest(key))
        if _known_over.get(known, 0.0) > _clock():
            return True
        try:
            async with slot():
                [n] = await _counts([(self, key)], released=False)
        except Busy:
            return True
        if n >= self.limit:
            _remember_over(known)
        return n >= self.limit

    async def record(self, key: str) -> None:
        """Count one failed attempt of `key`."""
        await _insert([(self.kind, key)], _at())


@dataclass(frozen=True)
class Admission:
    """An admitted attempt: its gates and the time its rows carry, which its
    release repeats so the two leave the window together."""

    gates: list[tuple[Attempts, str]]
    at: datetime


async def admit(gates: list[tuple[Attempts, str]]) -> Admission | None:
    """Record one attempt that counts as a failure until `release`d (a
    sign-in) under every (limit, key) and say whether it may go on. As in
    `Attempts.allow`, the recount after the record bounds a concurrent
    burst. One refused at the recount is released under every gate, so a
    burst that raced past the first count does not lock a key longer, nor
    spend another gate's room. Run it in a `slot`."""
    return await _admit(gates, released=True)


async def release(admission: Admission) -> None:
    """An admitted attempt that succeeded, or never reached its check."""
    await _insert([(f"{g.kind}.ok", key) for g, key in admission.gates], admission.at)


@asynccontextmanager
async def slot() -> AsyncIterator[None]:
    """One of this process's `auth_attempts_concurrency` auth checks; raises
    Busy when none frees within `_SLOT_WAIT_S`."""
    sem = _semaphore()
    try:
        await asyncio.wait_for(sem.acquire(), _SLOT_WAIT_S)
    except TimeoutError as e:
        raise Busy from e
    try:
        yield
    finally:
        sem.release()


_sem: tuple[Any, asyncio.Semaphore] | None = None
# (kind, key digest) → clock time until which it is known over its limit,
# oldest first.
_known_over: OrderedDict[tuple[str, str], float] = OrderedDict()


def _semaphore() -> asyncio.Semaphore:
    """The slots of the running event loop (a test runs several loops)."""
    from server.settings import settings

    global _sem
    loop = asyncio.get_running_loop()
    if _sem is None or _sem[0] is not loop:
        _sem = (loop, asyncio.Semaphore(settings.auth_attempts_concurrency))
    return _sem[1]


def _clock() -> float:
    return time.time()


def _at() -> datetime:
    """Now on the ClickHouse server's clock, as the keyring last measured it."""
    from server.auth import keyring

    return keyring.server_now()


async def _admit(gates: list[tuple[Attempts, str]], *, released: bool) -> Admission | None:
    """`released`: the gates count failures (attempts less their releases)."""
    now = _clock()
    if any(_known_over.get((g.kind, _digest(key)), 0.0) > now for g, key in gates):
        return None
    if _over(gates, await _counts(gates, released=released), with_this_one=False):
        return None
    admission = Admission(gates, _at())
    await _insert([(g.kind, key) for g, key in gates], admission.at)
    if not _over(gates, await _counts(gates, released=released), with_this_one=True):
        return admission
    if released:
        await release(admission)
    return None


def _over(gates: list[tuple[Attempts, str]], counts: list[int], *, with_this_one: bool) -> bool:
    """Whether any gate's count leaves no room for this attempt, and remember
    each such key for `_KNOWN_OVER_S`."""
    over = False
    for n, (g, key) in zip(counts, gates, strict=True):
        if n > g.limit or (n == g.limit and not with_this_one):
            _remember_over((g.kind, _digest(key)))
            over = True
    return over


def _remember_over(key: tuple[str, str]) -> None:
    _known_over[key] = _clock() + _KNOWN_OVER_S
    _known_over.move_to_end(key)
    if len(_known_over) > _KNOWN_OVER_MAX:
        _known_over.popitem(last=False)


async def _counts(gates: list[tuple[Attempts, str]], *, released: bool) -> list[int]:
    """Each gate's attempts in its window, less its `.ok` releases when
    `released`."""
    windows = {g.window_s for g, _ in gates}
    if len(windows) != 1:
        raise ValueError("limits counted together share one window")
    kinds = [(g.kind, f"{g.kind}.ok") if released else (g.kind,) for g, _ in gates]
    pairs = [(k, _digest(key)) for ks, (_, key) in zip(kinds, gates, strict=True) for k in ks]
    # Sequential: on another replica a plain read may miss an acknowledged attempt.
    with sequential_reads():
        rows = await _default_client().query_all(
            f"SELECT kind, key_sha256, count() AS n FROM {TABLE} "
            "WHERE (kind, key_sha256) IN {pairs:Array(Tuple(String, String))} "
            "AND at > now64(3) - toIntervalSecond({window:UInt32}) "
            "GROUP BY kind, key_sha256",
            params={"pairs": pairs, "window": windows.pop()},
        )
    # key_sha256 is a FixedString: the driver hands it back as bytes.
    n = {(str(r["kind"]), _text(r["key_sha256"])): int(r["n"]) for r in rows}
    return [
        max(0, n.get((g.kind, _digest(key)), 0) - n.get((f"{g.kind}.ok", _digest(key)), 0))
        for g, key in gates
    ]


async def _insert(kinds_keys: list[tuple[str, str]], at: datetime) -> None:
    await _default_client().insert(
        TABLE,
        [{"kind": kind, "key_sha256": _digest(key), "at": at} for kind, key in kinds_keys],
        # Answered once written: the recount sees the row, and a burst shares parts.
        settings=batched_insert_settings(async_when_off=True),
    )


def _text(v: Any) -> str:
    return v.decode() if isinstance(v, bytes) else str(v)


def _digest(key: str) -> str:
    from server.settings import settings

    secret = hashlib.sha256(b"loom.auth_attempts:" + str(settings.jwt_secret).encode()).digest()
    return hmac.new(secret, key.encode(), hashlib.sha256).hexdigest()


def client_address(request: Any) -> str:
    """The address an attempt limit counts: the connection's peer. uvicorn has
    already replaced it with the X-Forwarded-For client when the peer is one
    of LOOM_TRUSTED_PROXIES, so a header from any other peer changes nothing."""
    return request.client.host if request.client else "?"


def address_key(address: str) -> str:
    """Who a per-address limit counts: the address, or its /64 for IPv6, where
    one host holds a whole /64 to spread requests over."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return address
    if ip.version == 6 and ip.ipv4_mapped:  # an IPv4 client of a dual-stack socket
        return str(ip.ipv4_mapped)
    if ip.version == 6:
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return address
