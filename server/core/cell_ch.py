"""What the first attach's org scan (`cell_scan`) and ClickHouse writer check
(`cell_writers`) share: refusals as data, the cluster as `system.clusters` lists it, the
per-host reads through `clusterAllReplicas`, and the settings every read carries."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from server.core import ch_names
from server.core.ch import query_tag
from server.settings import settings

# system.query_log's log_comment: the scan's, and the writer check's. Attach's own grant
# reads carry the tag of the check that runs them; the host names always carry the scan's.
TAG = "cell.scan"
WRITERS_TAG = "cell.writers"

# A `clusterAllReplicas` read: every replica answers (one that does not is unverified, not
# absent), and the initiator reads its own host locally, never a second time over a
# connection as the cluster's configured user.
_CLUSTER = "skip_unavailable_shards = 0, prefer_localhost_replica = 1"
# Every read the scan and the checks make returns its whole result, whatever a profile sets:
# it throws past a limit rather than return what it has read so far (a `break` would shorten
# a list or a scan silently), and no setting cuts, filters or samples its rows (`limit`,
# `offset`, the additional filters, `final` on a collapsing table, a parallel replica's
# share of the parts, a cached result older than the rows).
WHOLE = {
    "timeout_overflow_mode": "throw",
    "timeout_overflow_mode_leaf": "throw",
    "read_overflow_mode": "throw",
    "read_overflow_mode_leaf": "throw",
    "result_overflow_mode": "throw",
    "distinct_overflow_mode": "throw",
    "set_overflow_mode": "throw",
    "group_by_overflow_mode": "throw",
    "limit": "0",
    "offset": "0",
    "additional_table_filters": "{}",
    "additional_result_filter": "",
    "final": "0",
    "enable_parallel_replicas": "0",
    "parallel_replicas_count": "0",
    "parallel_replica_offset": "0",
    "use_query_cache": "0",
}
_WHOLE = ", ".join(f"{k} = '{v}'" for k, v in WHOLE.items())
WHOLE_SQL = f"SETTINGS {_WHOLE}"
CLUSTER_SQL = f"SETTINGS {_CLUSTER}, {_WHOLE}"


@dataclass(frozen=True)
class Refusal:
    """One reason attach stops: what it names (a table, a host, an org) and why."""

    subject: str
    reason: str

    def __str__(self) -> str:
        return f"{self.subject}: {self.reason}"


class Refused(Exception):
    """The scan or a check stops; `refusals` name why."""

    def __init__(self, refusals: Iterable[Refusal]) -> None:
        self.refusals = list(refusals)
        super().__init__("; ".join(str(r) for r in self.refusals))


def quote(name: str) -> str:
    """A ClickHouse identifier in backquotes."""
    return "`" + name.replace("\\", "\\\\").replace("`", "\\`") + "`"


def databases() -> list[str]:
    return sorted({ch_names.database(), ch_names.telemetry_database()})


# -- the ClickHouse cluster ------------------------------------------------------------


@dataclass(frozen=True)
class Topology:
    """`settings.ch_cluster` as `system.clusters` lists it: its entries as (host name,
    port), one per server, and its shard count (no entries when the cluster is unset or
    unlisted)."""

    cluster: str
    shards: int
    entries: tuple[tuple[str, int], ...] = ()

    @property
    def hosts(self) -> frozenset[str]:
        """The configured host names."""
        return frozenset(host for host, _ in self.entries)

    @property
    def multi(self) -> bool:
        """More than one server: reads go through `clusterAllReplicas`, statements that must
        reach every replica carry `ON CLUSTER`. Two servers on one machine are two."""
        return len(self.entries) > 1

    def refusals(self) -> list[Refusal]:
        out = []
        if self.shards > 1:
            out.append(
                Refusal(
                    f"cluster {self.cluster}",
                    f"{self.shards} shards: a replica holds only its shard's rows, so a scan "
                    "through one would miss the others'",
                )
            )
        # The checks tell servers apart by configured host name alone.
        ports: dict[str, list[int]] = {}
        for host, port in self.entries:
            ports.setdefault(host, []).append(port)
        for host, listed in sorted(ports.items()):
            if len(listed) > 1:
                out.append(
                    Refusal(
                        f"cluster {self.cluster}",
                        f"lists host {host} {len(listed)} times (ports "
                        f"{', '.join(str(p) for p in sorted(listed))}): give each server its "
                        "own host name",
                    )
                )
        return out


async def read_topology(ch: Any) -> Topology:
    cluster = settings.ch_cluster
    if not cluster:
        return Topology("", 0)
    with query_tag(TAG):
        rows = await ch.query_all(
            "SELECT host_name, port, shard_num FROM system.clusters WHERE cluster = {c:String} "
            + WHOLE_SQL,
            {"c": cluster},
        )
    return Topology(
        cluster,
        len({int(r["shard_num"]) for r in rows}),
        tuple(sorted((str(r["host_name"]), int(r["port"])) for r in rows)),
    )


async def per_host(
    ch: Any,
    topology: Topology,
    names: Mapping[str, str],
    sql: str,
    params: dict[str, Any] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """The rows of `sql`, which selects `hostName() AS h` from `{source:<table>}`, grouped
    by the configured name of the host each came from: system table `<table>` itself on one
    host (grouped under ''), through `clusterAllReplicas` on several (grouped by `names`,
    from `cell_catalog.host_names()`)."""
    m = re.search(r"\{source:(\w+)\}", sql)
    assert m, sql
    table = f"system.{m.group(1)}"
    if not topology.multi:
        local = sql.replace(m.group(0), table) + " " + WHOLE_SQL
        return {"": await ch.query_all(local, params or {})}
    rows = await ch.query_all(
        sql.replace(m.group(0), f"clusterAllReplicas({{c:String}}, {table})") + " " + CLUSTER_SQL,
        {**(params or {}), "c": topology.cluster},
    )
    out: dict[str, list[dict[str, Any]]] = {name: [] for name in names.values()}
    for r in rows:
        name = names.get(str(r["h"]))
        if name is None:
            raise Refused([Refusal(f"host {r['h']}", "answered without a configured name")])
        out[name].append(r)
    return out
