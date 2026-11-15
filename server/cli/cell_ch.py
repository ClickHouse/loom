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

TAG = "cell.scan"  # system.query_log's log_comment of the scan's statements

# A `clusterAllReplicas` read: every replica answers (one that does not is unverified, not
# absent), and the initiator reads its own host locally, never a second time over a
# connection as the cluster's configured user.
_CLUSTER = "skip_unavailable_shards = 0, prefer_localhost_replica = 1"
# Every read the scan and the checks make throws past a limit rather than return what it has
# read so far: a profile's `break` would shorten a list or a scan silently.
WHOLE = {
    "timeout_overflow_mode": "throw",
    "timeout_overflow_mode_leaf": "throw",
    "read_overflow_mode": "throw",
    "read_overflow_mode_leaf": "throw",
    "result_overflow_mode": "throw",
    "distinct_overflow_mode": "throw",
    "set_overflow_mode": "throw",
}
_WHOLE = ", ".join(f"{k} = '{v}'" for k, v in WHOLE.items())
WHOLE_SQL = f"SETTINGS {_WHOLE}"
_CLUSTER_SQL = f"SETTINGS {_CLUSTER}, {_WHOLE}"


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


async def host_names(ch: Any, topology: Topology) -> dict[str, str]:
    """Each answering host's OS hostname (an opaque group key) and the one configured name
    it answers for in the cluster; Refused unless there is one group per listed host, each
    with exactly one name, the names together the listing. The topology's own refusals (two
    servers under one host name, several shards) come first, before any read."""
    if refusals := topology.refusals():
        raise Refused(refusals)
    with query_tag(TAG):
        rows = await ch.query_all(
            "SELECT hostName() AS h, groupArrayIf(host_name, is_local) AS names "
            "FROM clusterAllReplicas({c:String}, system.clusters) WHERE cluster = {c:String} "
            f"GROUP BY h {_CLUSTER_SQL}",
            {"c": topology.cluster},
        )
    where = f"cluster {topology.cluster}"
    refusals = []
    out: dict[str, str] = {}
    for r in rows:
        names = sorted(str(n) for n in r["names"])
        if not names:
            refusals.append(
                Refusal(
                    f"host {r['h']}",
                    f"this host's address in {where} does not resolve to a local interface",
                )
            )
        elif len(names) > 1:
            refusals.append(
                Refusal(
                    f"host {r['h']}",
                    f"this host answers for several entries in {where} ({', '.join(names)}): "
                    "two local entries, or two hosts with one OS hostname",
                )
            )
        else:
            out[str(r["h"])] = names[0]
    named = sorted(out.values())
    if not refusals and (len(named) != len(set(named)) or set(named) != topology.hosts):
        missing = sorted(topology.hosts - set(named))
        refusals.append(
            Refusal(
                where,
                "the answering hosts are not the listed ones"
                + (f" (no answer for {', '.join(missing)})" if missing else ""),
            )
        )
    if refusals:
        raise Refused(refusals)
    return out


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
    from `host_names()`)."""
    m = re.search(r"\{source:(\w+)\}", sql)
    assert m, sql
    table = f"system.{m.group(1)}"
    if not topology.multi:
        local = sql.replace(m.group(0), table) + " " + WHOLE_SQL
        return {"": await ch.query_all(local, params or {})}
    rows = await ch.query_all(
        sql.replace(m.group(0), f"clusterAllReplicas({{c:String}}, {table})") + " " + _CLUSTER_SQL,
        {**(params or {}), "c": topology.cluster},
    )
    out: dict[str, list[dict[str, Any]]] = {name: [] for name in names.values()}
    for r in rows:
        name = names.get(str(r["h"]))
        if name is None:
            raise Refused([Refusal(f"host {r['h']}", "answered without a configured name")])
        out[name].append(r)
    return out
