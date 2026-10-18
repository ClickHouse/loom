"""Deployment-wide claims and leases held in process memory, for tests whose
ClickHouse fake has no lock table: `memory_claims(monkeypatch)` for claims
(`server.core.leases.holding`), `held_leases(monkeypatch)` for `server.core.leases.take`,
`fake_key_locks(monkeypatch)` for the memory keys' write locks."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from server.core import leases


class MemoryClaims:
    def __init__(self) -> None:
        self.held: set[str] = set()
        self.taken: list[str] = []

    async def take(self, name: str, *, wait_s: float = 0.0, **_: Any) -> Any:
        until = time.monotonic() + wait_s
        while name in self.held:
            if time.monotonic() >= until:
                return None
            await asyncio.sleep(0.01)
        self.held.add(name)
        self.taken.append(name)
        return _MemoryClaim(self, name)


class _MemoryClaim:
    def __init__(self, claims: MemoryClaims, name: str) -> None:
        self.claims = claims
        self.name = name
        self.renewed_at = time.monotonic()

    async def release(self) -> None:
        self.claims.held.discard(self.name)


def memory_claims(monkeypatch: Any) -> MemoryClaims:
    claims = MemoryClaims()
    monkeypatch.setattr(leases, "take", claims.take)
    return claims


class HeldLease:
    """A lease that stays held until a test sets `lost`."""

    def __init__(self, name: str = "lease") -> None:
        self.name = name
        self.holder = f"test-host:1:{name}"
        self.lost = False
        self.released = False
        self.renewed_at = time.monotonic()

    @property
    def held(self) -> bool:
        return not self.lost

    async def release(self) -> None:
        self.released = True

    def keep_renewed(self, on_lost: Any = None) -> None:
        return None

    @asynccontextmanager
    async def renewed(self) -> AsyncIterator[HeldLease]:
        try:
            yield self
        finally:
            await self.release()


def held_leases(monkeypatch: Any) -> list[HeldLease]:
    """Every `leases.take` answers a fresh `HeldLease`; the list holds them in order."""
    taken: list[HeldLease] = []

    async def take(name: str, **_: Any) -> HeldLease:
        taken.append(HeldLease(name))
        return taken[-1]

    monkeypatch.setattr(leases, "take", take)
    return taken


class FakeLockTable:
    """The lease table as ClickHouse keeps it, for `server.core.leases`:
    `loom._migration_lock` under strict mode (an INSERT of a held key fails "Node
    exists", an UPDATE evaluates its WHERE on the current key). Expiry is judged on
    `now()`, which `advance` moves. While `down`, every statement but the engine read
    fails."""

    def __init__(self) -> None:
        self.keys: dict[str, dict[str, Any]] = {}
        self.sql: list[str] = []
        self.offset = 0.0
        self.down = False

    def now(self) -> float:
        import time

        return time.monotonic() + self.offset

    def advance(self, s: float) -> None:
        self.offset += s

    def seed(self, name: str, holder: str, *, ttl: int = 60, stale: bool = False) -> None:
        self.keys[name] = {
            "holder": holder,
            "renewed": self.now() - (ttl + 1 if stale else 0),
            "ttl": ttl,
        }

    def holder(self, name: str) -> str | None:
        key = self.keys.get(name)
        return None if key is None else str(key["holder"])

    def _stale(self, key: dict[str, Any]) -> bool:
        return bool(key["renewed"] + key["ttl"] < self.now())

    def _fail(self, sql: str) -> None:
        self.sql.append(sql)
        if self.down:
            raise RuntimeError("Code: 999. Coordination error: connection loss (KEEPER_EXCEPTION)")

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> Any:
        rows = await self.query_all(sql, params)
        return rows[0] if rows else None

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> Any:
        p = params or {}
        if "FROM system.tables" in sql:
            return [{"engine": "KeeperMap"}] if p.get("table") == "_migration_lock" else []
        self._fail(sql)
        if "FROM loom._migration_lock" in sql:
            key = self.keys.get(p["name"])
            if key is None:
                return []
            return [{"holder": key["holder"], "ttl_s": key["ttl"], "stale": self._stale(key)}]
        raise AssertionError(sql)

    async def execute(self, sql: str, params: dict[str, Any] | None = None, **_: Any) -> None:
        self._fail(sql)
        p = params or {}
        key = self.keys.get(p.get("name", ""))
        if sql.startswith("INSERT INTO loom._migration_lock"):
            if key is not None:
                raise RuntimeError(
                    "Code: 999. Transaction failed (Node exists). (KEEPER_EXCEPTION)"
                )
            self.keys[p["name"]] = {"holder": p["me"], "renewed": self.now(), "ttl": p["ttl"]}
        elif " UPDATE holder = " in sql:
            if key and key["holder"] == p["old"] and self._stale(key):
                self.keys[p["name"]] = {"holder": p["me"], "renewed": self.now(), "ttl": p["ttl"]}
        elif " UPDATE renewed_at = " in sql:
            if key and key["holder"] == p["me"]:
                key["renewed"] = self.now()
        elif "DELETE WHERE name = " in sql:
            if key and key["holder"] == p["me"]:
                del self.keys[p["name"]]
        elif "ALTER TABLE loom._migration_lock DELETE" in sql:  # the sweep
            for name in [
                n for n, k in self.keys.items() if k["renewed"] + k["ttl"] + 3600 < self.now()
            ]:
                del self.keys[name]
        elif "ALTER TABLE loom.graph_projections DELETE" in sql:  # the sweep's other table
            pass
        else:
            raise AssertionError(sql)


def fake_key_locks(monkeypatch: Any) -> None:
    """The keys' write locks (Keeper leases) over a `FakeLockTable`, for a test that
    takes one, as an Undo does."""
    from server.memory.store import key_write_lock as kl

    locks = kl.KeyWriteLocks(FakeLockTable(), wait_s=0.3)  # type: ignore[arg-type]
    monkeypatch.setattr(kl, "key_write_locks", locks)


def no_erasure(monkeypatch: Any) -> None:
    """No org under erasure: the projection repair reads it first."""
    from server.memory.store import conflict_resolver as queue

    async def no(org: str, **kw: Any) -> bool:
        return False

    monkeypatch.setattr(queue, "under_erasure", no)
