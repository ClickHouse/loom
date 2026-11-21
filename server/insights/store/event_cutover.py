"""Confirm ordinary event storage contains no private rows before analyst grants.

The permanent CHECK fences new inserts. Old metadata can survive an ALTER, so the
barrier waits for the finite insert set observed after every writer confirms it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, replace
from typing import Any, Protocol, cast
from uuid import UUID

from server.core.ch import query_tag, statement_timeout
from server.core.ch_errors import error_code
from server.core.ch_names import database, quote
from server.core.ch_observer import PolicyObserver
from server.core.repo_base import _CHClient, _default_client
from server.core.sql_literal import sql_string
from server.insights.domain.analysis import ANALYST_USER
from server.insights.domain.observation_policy import observer_policies_allow
from server.settings import settings

CONSTRAINT = "ordinary_events_only"
ORDINARY = "mirror_marked = 0 AND NOT match(namespace, '^acl-[0-9a-f]{24}$')"
# not a setting: setup holds a 60s lease; this fixed bound leaves time for entity writes.
_BUDGET_S = 10
_LIVE = (
    " SETTINGS skip_unavailable_shards = 0, max_parallel_replicas = 1, "
    "max_execution_time = 10, "
    "use_query_cache = 0, enable_reads_from_query_cache = 0, "
    "enable_writes_to_query_cache = 0, read_overflow_mode = 'throw', "
    "read_overflow_mode_leaf = 'throw', result_overflow_mode = 'throw', "
    "timeout_overflow_mode = 'throw', timeout_overflow_mode_leaf = 'throw'"
)
_TOPOLOGY = (
    "SELECT shard_num, replica_num, host_name, host_address, port, user, default_database "
    "FROM system.clusters WHERE cluster = {cluster:String} "
    "ORDER BY shard_num, replica_num, host_name, port"
)


class _ObserverClient(_CHClient, Protocol):
    def policy_observers(self) -> AbstractAsyncContextManager[tuple[PolicyObserver, ...]]: ...


class EventCutoverUnavailable(RuntimeError):
    """Writer coverage, the CHECK or absence of private ordinary rows is unconfirmed."""

    def __init__(self) -> None:
        super().__init__("ordinary event storage is not confirmed private-free on every writer")


def _remote(inner: str, cluster: str) -> str:
    source = (
        f"clusterAllReplicas({sql_string(cluster)}, view({inner}))" if cluster else f"view({inner})"
    )
    return f"SELECT * FROM {source}{_LIVE}"


def _metadata() -> str:
    return (
        "SELECT toString(serverUUID()) AS server_uuid, "
        "(SELECT groupArray((name, toString(uuid), engine)) FROM system.tables "
        "WHERE database = {db:String} AND name IN ('agent_events', 'agent_events_private')) "
        "AS tables, "
        "(SELECT count() FROM system.constraints WHERE database = {db:String} "
        "AND table = 'agent_events' AND name = {constraint:String}) AS named, "
        "(SELECT count() FROM system.constraints WHERE database = {db:String} "
        "AND table = 'agent_events' AND name = {constraint:String} AND type = 'CHECK' "
        "AND formatQuerySingleLine(concat('SELECT ', expression)) = "
        "formatQuerySingleLine({expected:String})) AS correct"
    )


def _processes() -> str:
    # Internal async flushes must stay visible after their submitting client leaves.
    return (
        "SELECT toString(serverUUID()) AS server_uuid, groupArray((user, query_id)) AS inserts "
        "FROM system.processes WHERE query_kind = 'Insert'"
    )


def _census() -> str:
    return (
        "SELECT toString(serverUUID()) AS server_uuid, count() AS private "
        f"FROM {quote(database())}.agent_events WHERE NOT ({ORDINARY})"
    )


def _uuid(value: Any) -> str:
    parsed = UUID(str(value))
    if parsed.int == 0:
        raise EventCutoverUnavailable()
    return str(parsed)


def _responders(rows: list[dict[str, Any]], count: int) -> frozenset[str]:
    found = frozenset(_uuid(row["server_uuid"]) for row in rows)
    if len(rows) != count or len(found) != count:
        raise EventCutoverUnavailable()
    return found


def _identities(rows: list[dict[str, Any]]) -> tuple[tuple[str, str, str, str], ...]:
    found: list[tuple[str, str, str, str]] = []
    for row in rows:
        tables = {str(name): (_uuid(uid), str(engine)) for name, uid, engine in row["tables"]}
        if (
            len(row["tables"]) != 2
            or set(tables) != {"agent_events", "agent_events_private"}
            or len({uid for uid, _ in tables.values()}) != 2
            or any(
                engine not in {"MergeTree", "ReplicatedMergeTree", "SharedMergeTree"}
                for _, engine in tables.values()
            )
            or row["named"] != 1
            or row["correct"] != 1
        ):
            raise EventCutoverUnavailable()
        found.extend(
            (str(row["server_uuid"]), name, uid, engine) for name, (uid, engine) in tables.items()
        )
    return tuple(sorted(found))


def _inserts(rows: list[dict[str, Any]]) -> frozenset[tuple[str, str, str]]:
    found = set()
    for row in rows:
        for user, query_id in row["inserts"]:
            if (
                not isinstance(user, str)
                or not user
                or not isinstance(query_id, str)
                or not query_id
            ):
                raise EventCutoverUnavailable()
            found.add((str(row["server_uuid"]), user, query_id))
    return frozenset(found)


async def _topology(ch: _CHClient, cluster: str) -> tuple[tuple[Any, ...], ...]:
    if not cluster:
        return ()
    rows = await ch.query_all(_TOPOLOGY + _LIVE, {"cluster": cluster})
    fields = (
        "shard_num",
        "replica_num",
        "host_name",
        "host_address",
        "port",
        "user",
        "default_database",
    )
    values = tuple(tuple(row[key] for key in fields) for row in rows)
    if any(not row[2] or not isinstance(row[4], int) or row[4] <= 0 for row in values):
        raise EventCutoverUnavailable()
    return values


@dataclass(frozen=True)
class _Writers:
    configured: str
    topology: tuple[tuple[Any, ...], ...]
    servers: frozenset[str]
    tables: tuple[tuple[str, str, str, str], ...]
    observers: tuple[PolicyObserver, ...] = ()
    catalogs: tuple[tuple[str, ...], ...] = ()

    async def read(self, inner: str) -> list[dict[str, Any]]:
        rows = []
        for observer, catalog in zip(self.observers, self.catalogs, strict=True):
            if tuple(sorted(await observer.policies())) != catalog:
                raise EventCutoverUnavailable()
            topology_sql = (
                "SELECT toString(serverUUID()) AS server_uuid, "
                "(SELECT groupArray((shard_num, replica_num, host_name, host_address, port, "
                "user, default_database)) FROM (" + _TOPOLOGY + ")) AS topology"
            )
            observed = await observer.query_all(topology_sql + _LIVE, {"cluster": self.configured})
            if len(observed) != 1 or tuple(map(tuple, observed[0]["topology"])) != self.topology:
                raise EventCutoverUnavailable()
            rows.extend(await observer.query_all(inner + _LIVE, _params()))
        if _responders(rows, len(self.servers)) != self.servers:
            raise EventCutoverUnavailable()
        return rows

    async def verify(self) -> None:
        if _identities(await self.read(_metadata())) != self.tables:
            raise EventCutoverUnavailable()


def _params() -> dict[str, Any]:
    return {"db": database(), "constraint": CONSTRAINT, "expected": f"SELECT {ORDINARY}"}


async def _writers(ch: _CHClient) -> _Writers:
    configured = str(settings.ch_cluster)
    topology = await _topology(ch, configured)
    cluster = configured
    try:
        rows = await ch.query_all(_remote(_metadata(), cluster), _params())
    except Exception as exc:
        if not cluster or topology or error_code(exc) != 701:
            raise
        # The established single-node interpretation is allowed only at discovery.
        cluster = ""
        rows = await ch.query_all(_remote(_metadata(), cluster), _params())
    expected = len({(row[2], row[4]) for row in topology}) if cluster else 1
    servers = _responders(rows, expected)
    local = await ch.query_all("SELECT toString(serverUUID()) AS server_uuid" + _LIVE)
    if len(local) != 1 or _uuid(local[0]["server_uuid"]) not in servers:
        raise EventCutoverUnavailable()
    return _Writers(configured, topology, servers, _identities(rows))


@asynccontextmanager
async def _observed(writers: _Writers, ch: _ObserverClient) -> AsyncIterator[_Writers]:
    async with ch.policy_observers() as observers:
        if (
            len(observers) != len(writers.servers)
            or frozenset(observer.server_uuid for observer in observers) != writers.servers
            or any(
                observer.user == ANALYST_USER or ANALYST_USER in observer.roles
                for observer in observers
            )
        ):
            raise EventCutoverUnavailable()
        catalogs = []
        for observer in observers:
            catalog = tuple(sorted(await observer.policies()))
            if not observer_policies_allow(catalog, database()):
                raise EventCutoverUnavailable()
            catalogs.append(catalog)
        observed = replace(writers, observers=observers, catalogs=tuple(catalogs))
        await observed.verify()
        yield observed


async def confirm_ordinary_events(*, ch: _CHClient | None = None) -> None:
    """Refuse unknown writers or surviving private rows; never kill, copy or erase."""
    ch = ch or _default_client()
    try:
        with query_tag("analysis.event_cutover"), statement_timeout(_BUDGET_S):
            async with asyncio.timeout(_BUDGET_S):
                async with _observed(await _writers(ch), cast(_ObserverClient, ch)) as writers:
                    old = _inserts(await writers.read(_processes()))
                    while old:
                        await asyncio.sleep(0.05)
                        old &= _inserts(await writers.read(_processes()))
                    await writers.verify()
                    rows = await writers.read(_census())
                    if any(row["private"] != 0 for row in rows):
                        raise EventCutoverUnavailable()
                    await writers.verify()
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — an uncertain cutover must never grant raw access
        raise EventCutoverUnavailable() from None
