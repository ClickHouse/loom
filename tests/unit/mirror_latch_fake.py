"""Stateful deployment-store model; real CH sessions and Keeper need native proof."""

from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import asynccontextmanager

A = "11111111-1111-4111-8111-111111111111"
B = "22222222-2222-4222-8222-222222222222"


class KeeperWorld:
    def __init__(self, *, enabled=False, visible=False):
        self.enabled = enabled
        self.visible = {A: visible, B: visible}
        self.clients = {A: 101, B: 202}
        self.leases = {}
        self.calls = []
        self.counts = Counter()
        self.routes = {}
        self.fault = ""
        self.sessions = 0
        self.detached_stores = set()

    @asynccontextmanager
    async def coordination_session(self):
        self.sessions += 1
        yield

    def route(self, stage):
        self.calls.append(stage)
        self.counts[stage] += 1
        return self.routes.get((stage, self.counts[stage]), A)

    async def query_one(self, sql, params=None):
        if "FROM system.tables" in sql:
            assert isinstance(params, dict) and set(params) == {"database"}
            database = params["database"]
            assert isinstance(database, str) and database
            backend = self.route("engines")
            engines = [
                (
                    "knowledge_mirror_latch",
                    f"KeeperMap('/{database}/knowledge_mirror_latch') PRIMARY KEY id",
                ),
                ("_migration_lock", f"KeeperMap('/{database}/migration_lock') PRIMARY KEY name"),
            ]
            if self.fault == "wrong_engine":
                engines[0] = ("knowledge_mirror_latch", "MergeTree")
            elif self.fault == "wrong_ensemble":
                engines[0] = (
                    "knowledge_mirror_latch",
                    "KeeperMap('auxiliary:/loom/knowledge_mirror_latch') PRIMARY KEY id",
                )
            elif self.fault == "wrong_primary_key":
                engines[0] = (
                    "knowledge_mirror_latch",
                    f"KeeperMap('/{database}/knowledge_mirror_latch') PRIMARY KEY set_at",
                )
            return {"server_id": backend, "engines": engines}
        if "FROM loom.knowledge_mirror_latch" in sql:
            backend = self.route("latch")
            if self.fault == "latch_backend_error":
                raise RuntimeError("private backend details")
            if self.fault == "keeper_replaced" and self.counts["barrier"]:
                self.clients[A] += 1
            return {"server_id": backend, "enabled": int(self.visible[backend])}
        if "FROM system.zookeeper_connection" in sql:
            backend = self.route("keeper")
            clients = [(self.clients[backend], 0, 1)]
            if self.fault == "missing_keeper":
                clients = []
            elif self.fault == "expired_keeper":
                clients[0] = (self.clients[backend], 1, 1)
            return {"server_id": backend, "clients": clients}
        if "FROM loom._migration_lock" in sql:
            backend = self.route("nonce")
            lease = self.leases.get(params["name"])
            return {"server_id": backend, "holders": [lease] if lease else []}
        raise AssertionError(sql)

    async def execute(self, sql, params=None):
        if sql.startswith("INSERT INTO loom._migration_lock"):
            backend = self.route("barrier")
            assert "async_insert = 0" in sql and "keeper_map_strict_mode = 1" in sql
            assert "toString(serverUUID())" in sql and "{commit:String}" not in sql
            if self.fault == "foreign_duplicate":
                self.leases[params["name"]] = ("foreign-holder", 1, backend)
                raise RuntimeError("Node exists")
            assert params["name"] not in self.leases
            self.leases[params["name"]] = (
                params["me"],
                int(self.fault != "expired_nonce"),
                backend,
            )
            self.visible[backend] = self.enabled
            if self.fault == "ambiguous_write":
                raise RuntimeError("response lost after commit")
            if self.fault == "cancelled_write":
                raise asyncio.CancelledError()
            return
        if sql.startswith("ALTER TABLE loom._migration_lock DELETE"):
            self.route("release")
            assert "holder = {me:String}" in sql and "name = {name:String}" in sql
            if self.fault == "cleanup_error":
                raise RuntimeError("cleanup unavailable")
            current = self.leases.get(params["name"])
            if current and current[0] == params["me"]:
                del self.leases[params["name"]]
            return
        if sql.startswith("INSERT INTO loom.knowledge_mirror_latch"):
            backend = self.route("set")
            if "throwIf(toString(serverUUID()) != {backend:String}" in sql:
                if backend != params["backend"]:
                    raise RuntimeError("Session protection writer changed")
            if backend in self.detached_stores:
                self.visible[backend] = True
                return
            existed = self.enabled
            self.enabled = self.visible[backend] = True
            if existed or self.fault == "racing_set":
                raise RuntimeError("Node exists")
            return
        raise AssertionError(sql)
