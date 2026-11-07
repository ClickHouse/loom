"""Two (or more) in-process "replicas" over one fake ClickHouse with a visibility lag.

A row inserted through any replica is seen by another replica's plain read only after
`lag_s` (ClickHouse Cloud shows a write on another node after up to ~3.6 s); a read
under `server.core.ch.sequential_reads()` sees every acknowledged insert, and the
writer sees its own insert at once. The KeeperMap lock table is linearizable, as Keeper
is. Only the statement shapes the cross-replica tests drive are answered; anything
else raises, so a test never passes on a statement the fake silently ignored.

    cluster = FakeCluster(lag_s=3.6)
    a, b = cluster.replica("a"), cluster.replica("b")
    install(monkeypatch, cluster, server.auth.self_signup)  # get_client() -> current replica
    with cluster.on(a): ...
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Iterator
from contextvars import ContextVar
from datetime import datetime
from typing import Any

_current: ContextVar[FakeReplica | None] = ContextVar("fake_replica", default=None)


class FakeCluster:
    def __init__(self, *, lag_s: float = 3.6) -> None:
        self.lag_s = lag_s
        self.offset = 0.0
        # table -> [(visible_at, writer, row)]
        self.rows: dict[str, list[tuple[float, str, dict[str, Any]]]] = {}
        self.locks: dict[str, dict[str, Any]] = {}
        self.statements: list[tuple[str, str]] = []

    def now(self) -> float:
        return time.monotonic() + self.offset

    def advance(self, s: float) -> None:
        self.offset += s

    def replica(self, name: str) -> FakeReplica:
        return FakeReplica(self, name)

    @contextlib.contextmanager
    def on(self, replica: FakeReplica) -> Iterator[FakeReplica]:
        token = _current.set(replica)
        try:
            yield replica
        finally:
            _current.reset(token)

    def table(self, name: str) -> list[dict[str, Any]]:
        """Every row of `name`, visible or not (assertions)."""
        return [r for _, _, r in self.rows.get(name, [])]


@contextlib.contextmanager
def in_request() -> Iterator[Any]:
    """An open request tail (what the ASGI middleware gives each request)."""
    from server.core import request_tail

    tail = request_tail._Tail()
    token = request_tail._tail.set(tail)
    try:
        yield tail
    finally:
        request_tail._tail.reset(token)


def current() -> FakeReplica:
    rep = _current.get()
    if rep is None:
        raise RuntimeError("no fake replica bound: use `with cluster.on(replica)`")
    return rep


def install(monkeypatch: Any, cluster: FakeCluster, *modules: Any) -> None:
    """`get_client()` answers the bound replica, in server.core.ch and in each module
    that imported the name."""
    import server.core.ch as ch_mod

    monkeypatch.setattr(ch_mod, "get_client", current)
    for m in modules:
        monkeypatch.setattr(m, "get_client", current)


class FakeReplica:
    def __init__(self, cluster: FakeCluster, name: str) -> None:
        self.cluster = cluster
        self.name = name

    # -- visibility --------------------------------------------------------

    def _visible(self, table: str) -> list[dict[str, Any]]:
        from server.core.ch import sequential_reads_active

        seq = sequential_reads_active()
        now = self.cluster.now()
        return [
            row
            for at, writer, row in self.cluster.rows.get(table, [])
            if seq or writer == self.name or at <= now
        ]

    async def insert(self, table: str, rows: list[dict[str, Any]], *a: Any, **k: Any) -> None:
        at = self.cluster.now() + self.cluster.lag_s
        for r in rows:
            self.cluster.rows.setdefault(table, []).append((at, self.name, dict(r)))
        self.cluster.statements.append((self.name, f"INSERT {table}"))

    # -- statements --------------------------------------------------------

    async def execute(self, sql: str, params: dict[str, Any] | None = None, **_: Any) -> None:
        p = params or {}
        self.cluster.statements.append((self.name, sql))
        locks = self.cluster.locks
        if sql.startswith("INSERT INTO loom._migration_lock"):
            if p["name"] in locks:
                raise RuntimeError("Code: 999. Coordination::Exception: Node exists")
            locks[p["name"]] = {"holder": p["me"], "renewed": self.cluster.now(), "ttl": p["ttl"]}
            return
        if "ALTER TABLE loom._migration_lock UPDATE holder" in sql:
            cur = locks.get(p["name"])
            if cur and cur["holder"] == p["old"] and self._stale(cur):
                locks[p["name"]] = {
                    "holder": p["me"],
                    "renewed": self.cluster.now(),
                    "ttl": p["ttl"],
                }
            return
        if "ALTER TABLE loom._migration_lock DELETE" in sql:
            cur = locks.get(p["name"])
            if cur and cur["holder"] == p["me"]:
                del locks[p["name"]]
            return
        raise NotImplementedError(f"fake replica: execute {sql[:80]!r}")

    def _stale(self, lock: dict[str, Any]) -> bool:
        return bool(lock["renewed"] + lock["ttl"] < self.cluster.now())

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> Any:
        rows = await self.query_all(sql, params)
        return rows[0] if rows else None

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> Any:
        p = params or {}
        self.cluster.statements.append((self.name, sql))
        if "FROM system.tables" in sql:
            return [{"engine": "KeeperMap"}] if p.get("table") == "_migration_lock" else []
        if "FROM loom._migration_lock" in sql:
            cur = self.cluster.locks.get(p["name"])
            return [] if cur is None else [{"holder": cur["holder"], "stale": self._stale(cur)}]
        if "lower(created_by)" in sql:
            # The sign-up reservation: each grant's latest version, held while unrevoked
            # (a pending join request included), a namespace it owns or a live token it made.
            latest: dict[tuple[str, str, str], dict[str, Any]] = {}
            for r in self._visible("loom.namespace_grants"):
                key = (r["org"], r["namespace"], r["subject"])
                if key not in latest or r.get("version", 0) >= latest[key].get("version", 0):
                    latest[key] = r
            held = any(
                (r["org"], r["subject"].lower()) == (p["org"], p["name"])
                and r.get("revoked_at") is None
                for r in latest.values()
            )
            owned = any(
                (r["org"], str(r.get("owner_email", "")).lower()) == (p["org"], p["name"])
                for r in self._visible("loom.namespaces")
            )
            made = any(
                (r["org"], str(r.get("created_by", "")).lower()) == (p["org"], p["name"])
                and r.get("revoked_at") is None
                for r in self._visible("loom.service_accounts")
            )
            return [{"held": 1}] if held or owned or made else []
        if "FROM loom.namespaces" in sql:
            return self._namespaces(sql, p)
        if "FROM loom.oauth_state" in sql:
            return self._oauth(sql, p)
        if "FROM loom.auth_attempts" in sql:
            return self._attempt_counts(p["pairs"])
        if "FROM loom.signing_keys" in sql:
            return self._signing_keys(p.get("kid"))
        if sql.startswith("SELECT now64(3) AS now"):
            return [{"now": datetime.now()}]
        if "FROM loom.idempotency" in sql:
            hits = [r for r in self._visible("loom.idempotency") if r["idempotency_key"] == p["k"]]
            return hits[-1:]
        raise NotImplementedError(f"fake replica: query {sql[:80]!r}")

    def _attempt_counts(self, pairs: list[tuple[str, str]]) -> list[dict[str, Any]]:
        """Every visible attempt counts: the tests run well inside the window."""
        n: dict[tuple[str, str], int] = {}
        for r in self._visible("loom.auth_attempts"):
            pair = (r["kind"], r["key_sha256"])
            if pair in pairs:
                n[pair] = n.get(pair, 0) + 1
        return [{"kind": k, "key_sha256": d, "n": c} for (k, d), c in n.items()]

    def _signing_keys(self, kid: str | None) -> list[dict[str, Any]]:
        """`FINAL`: the last visible row of each kid."""
        latest = {r["kid"]: r for r in self._visible("loom.signing_keys")}
        rows = [
            {**r, "retired": r["retired_at"] is not None, "server_now": datetime.now()}
            for k, r in latest.items()
            if kid is None or k == kid
        ]
        return sorted(rows, key=lambda r: r["activate_at"])

    def _namespaces(self, sql: str, p: dict[str, Any]) -> list[dict[str, Any]]:
        rows = [r for r in self._visible("loom.namespaces") if r["org"] == p.get("org")]
        if "lower(contact_email)" in sql:
            n = sum(
                1
                for r in rows
                if str(r.get("contact_email", "")).lower() == p["ce"]
                and r.get("active", True)
                and r.get("password_hash")
            )
            return [{"n": n}]
        rows = [r for r in rows if r["namespace"] == p.get("ns")]
        if not rows:
            return []
        last = max(rows, key=lambda r: r.get("updated_at") or datetime.min)
        return [{"owner": last.get("owner_email", ""), "present": 1, "x": 1, **last}]

    def _oauth(self, sql: str, p: dict[str, Any]) -> list[dict[str, Any]]:
        rtype = p.get("t") or ("client" if "'client'" in sql else "oidc_pending")
        versions = [
            r
            for r in self._visible("loom.oauth_state")
            if r["record_type"] == rtype and r["id"] == p["id"]
        ]
        if not versions:
            return []
        last = max(versions, key=lambda r: r["version"])
        return [] if last.get("consumed", 0) else [last]


__all__ = ["FakeCluster", "FakeReplica", "current", "in_request", "install"]
