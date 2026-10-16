"""Communities over the entity co-occurrence graph.

The graph in `loom.entity_edges` says which entities are mentioned together
and how often. This module finds its clusters — deterministic weighted
label propagation, pure Python, no networkx — and writes one row per
entity to `loom.entity_communities` with a community id, its size and a
human-readable label (the highest-degree member). Readers use the latest
pass per namespace.

Why label propagation and not Girvan–Newman or Leiden: Girvan–Newman is
O(m²·n) and only works on toy graphs; Leiden needs a native dependency for one algorithm. Weighted
LPA is O(iterations × m), deterministic under a fixed node order with
smallest-label tie-breaking, and good enough for the two consumers here:
diversifying graph expansion and naming a namespace's topics. A hierarchy
(level > 0) is left for a later pass; the table already carries `level`.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from server.core.datetimes import naive_utc_now
from server.workers import pass_context

log = logging.getLogger(__name__)

TABLE = "loom.entity_communities"


class _CHClient(Protocol):
    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]: ...

    async def insert(
        self, table: str, rows: list[dict[str, Any]], settings: dict[str, Any] | None = None
    ) -> None: ...


@dataclass(frozen=True)
class Community:
    community_id: int
    label: str
    size: int
    members: tuple[str, ...]  # degree-descending


def label_propagation(
    edges: list[tuple[str, str, float]],
    *,
    max_iterations: int = 30,
) -> dict[str, int]:
    """node -> community id for an undirected weighted edge list.

    Asynchronous updates in sorted node order; a node takes the label with
    the largest total incident weight among its neighbours, ties broken by
    the smallest label. Stops when a full sweep changes nothing or after
    `max_iterations`. Community ids are renumbered by size descending, then
    by the smallest member name, so id 0 is the largest cluster and the
    output is stable across runs on the same graph.
    """
    adj: dict[str, dict[str, float]] = {}
    for a, b, w in edges:
        if not a or not b or a == b or w <= 0:
            continue
        adj.setdefault(a, {})[b] = adj.get(a, {}).get(b, 0.0) + float(w)
        adj.setdefault(b, {})[a] = adj.get(b, {}).get(a, 0.0) + float(w)
    nodes = sorted(adj)
    if not nodes:
        return {}
    label = {n: i for i, n in enumerate(nodes)}
    for _ in range(max_iterations):
        changed = False
        for n in nodes:
            score: dict[int, float] = {}
            for m, w in adj[n].items():
                score[label[m]] = score.get(label[m], 0.0) + w
            if not score:
                continue
            best = max(score.values())
            new = min(lbl for lbl, s in score.items() if s == best)
            if new != label[n]:
                label[n] = new
                changed = True
        if not changed:
            break
    # Renumber: size desc, then smallest member name for determinism.
    groups: dict[int, list[str]] = {}
    for n, lbl in label.items():
        groups.setdefault(lbl, []).append(n)
    ordered = sorted(groups.values(), key=lambda ms: (-len(ms), min(ms)))
    return {n: cid for cid, members in enumerate(ordered) for n in members}


def summarize_communities(assignment: dict[str, int], degree: dict[str, int]) -> list[Community]:
    """Group an assignment into Community records, members degree-descending,
    label = highest-degree member (ties: lexical)."""
    groups: dict[int, list[str]] = {}
    for n, cid in assignment.items():
        groups.setdefault(cid, []).append(n)
    out: list[Community] = []
    for cid in sorted(groups):
        members = sorted(groups[cid], key=lambda n: (-int(degree.get(n, 0)), n))
        out.append(
            Community(community_id=cid, label=members[0], size=len(members), members=tuple(members))
        )
    return out


@dataclass(frozen=True)
class EntityCommunitiesConfig:
    max_edges_per_namespace: int = 200_000
    min_co_occur: int = 1
    max_iterations: int = 30
    org_filter: str | None = None
    namespace_filter: str | None = None
    # A namespace whose graph was walked (facts.traverse / facts.related / facts.path /
    # namespace.taxonomy in loom.agent_events within walked_days) joins the
    # consolidation_enabled set — the dashboard's Graph view and the Explorer's community facet
    # read communities for any namespace a user walks, not only the consolidation
    # opt-ins. 0 = the opt-ins only.
    walked_days: int = 14
    # Drop the top fraction of entities by degree before propagating; the extractor's
    # generic tokens otherwise fuse the graph.
    hub_drop_fraction: float = 0.002
    # When the partition at hub_drop_fraction leaves one community holding more than
    # giant_share of the entities, the fraction doubles and the namespace is partitioned
    # again, up to hub_drop_max_fraction (four doublings from the default). A dense
    # graph (a code corpus: 15k entities, 200k pairs) fuses at 0.002 and parts at 0.016;
    # a conversational graph parts at 0.004. giant_share 0 = never escalate (a single
    # pass at hub_drop_fraction).
    giant_share: float = 0.5
    hub_drop_max_fraction: float = 0.032


@dataclass
class EntityCommunitiesResult:
    namespaces: int = 0
    rows_written: int = 0
    truncated: dict[str, bool] = field(default_factory=dict)  # "org/ns" -> strongest N kept
    per_namespace: dict[str, int] = field(default_factory=dict)  # "org/ns" -> communities
    hubs_dropped: dict[str, list[str]] = field(default_factory=dict)  # "org/ns" -> entities
    # "org/ns" -> the hub fraction the partition was taken at,
    # recorded only when it escalated past cfg.hub_drop_fraction; `giant`
    # lists the namespaces whose largest community still holds more than
    # giant_share of the entities at hub_drop_max_fraction (written anyway).
    hub_fraction_used: dict[str, float] = field(default_factory=dict)
    giant: list[str] = field(default_factory=list)


# The graph reads that render communities for any namespace.
WALK_OPERATIONS: tuple[str, ...] = (
    "facts.traverse",
    "facts.related",
    "facts.path",
    "namespace.taxonomy",
)


def _scope_where(cfg: EntityCommunitiesConfig, alias: str) -> tuple[list[str], dict[str, Any]]:
    """The org / namespace filters, qualified with `alias`: the statement
    joins loom.namespaces and both tables carry the columns."""
    where = ["1 = 1"]
    params: dict[str, Any] = {}
    q = f"{alias}."
    if cfg.org_filter is not None:
        where.append(f"{q}org = {{org:String}}")
        params["org"] = cfg.org_filter
    if cfg.namespace_filter is not None:
        where.append(f"{q}namespace = {{ns:String}}")
        params["ns"] = cfg.namespace_filter
    return where, params


async def _opted_in_namespaces(
    ch: _CHClient, cfg: EntityCommunitiesConfig
) -> list[tuple[str, str]]:
    where, params = _scope_where(cfg, "e")
    # The namespaces that asked for offline consolidation
    # are the ones whose outline and profile fold read communities.
    sql = (
        "SELECT DISTINCT e.org AS org, e.namespace AS namespace "
        "FROM loom.entity_edges AS e "
        "INNER JOIN loom.namespaces AS n FINAL ON e.org = n.org AND e.namespace = n.namespace "
        f"WHERE n.active = true AND n.consolidation_enabled = true AND {' AND '.join(where)} "
        "ORDER BY org, namespace"
    )
    rows = await ch.query_all(sql, params=params)
    return [(str(r["org"]), str(r["namespace"])) for r in rows]


async def _walked_namespaces(ch: _CHClient, cfg: EntityCommunitiesConfig) -> list[tuple[str, str]]:
    """Active namespaces with a successful graph read in the
    last `walked_days`. The ts range leads the predicate: agent_events is
    partitioned by month, so the window prunes to at most two partitions
    before the operation filter reads the rest (a 14-day read over a few
    thousand namespaces of events stays under 100 ms)."""
    where, params = _scope_where(cfg, "a")
    params.update({"days": int(cfg.walked_days), "ops": list(WALK_OPERATIONS)})
    sql = (
        "SELECT DISTINCT a.org AS org, a.namespace AS namespace "
        "FROM loom.agent_events AS a "
        "INNER JOIN loom.namespaces AS n FINAL ON a.org = n.org AND a.namespace = n.namespace "
        "WHERE a.ts >= now() - INTERVAL {days:UInt32} DAY "
        "AND a.operation IN {ops:Array(String)} AND a.success = true "
        f"AND n.active = true AND {' AND '.join(where)} "
        "ORDER BY org, namespace"
    )
    rows = await ch.query_all(sql, params=params)
    return [(str(r["org"]), str(r["namespace"])) for r in rows]


async def _namespaces_with_edges(
    ch: _CHClient, cfg: EntityCommunitiesConfig
) -> list[tuple[str, str]]:
    """The pass covers the union of the opted-in namespaces and the walked
    ones. A walked namespace without edges costs one empty edge read."""
    out = await _opted_in_namespaces(ch, cfg)
    if cfg.walked_days > 0:
        seen = set(out)
        out.extend(pair for pair in await _walked_namespaces(ch, cfg) if pair not in seen)
    return out


async def _edges(
    ch: _CHClient, org: str, ns: str, cfg: EntityCommunitiesConfig
) -> list[dict[str, Any]]:
    # Current edges only (valid_to open / not superseded); weight = summed
    # co_occur. Strongest pairs first: the pass keeps the top max_edges
    # of an oversized graph (the +1 row says whether anything was cut); the
    # name tiebreak makes the cut inside a weight band the same every pass.
    return await ch.query_all(
        """
        SELECT entity_a, entity_b, sum(co_occur) AS w
        FROM loom.entity_edges
        WHERE org = {org:String} AND namespace = {ns:String}
          AND (valid_to IS NULL OR valid_to >= now())
          AND invalid_at IS NULL
        GROUP BY entity_a, entity_b
        HAVING w >= {min_co:UInt32}
        ORDER BY w DESC, entity_a, entity_b
        LIMIT {lim:UInt32}
        """,
        params={
            "org": org,
            "ns": ns,
            "min_co": int(cfg.min_co_occur),
            "lim": int(cfg.max_edges_per_namespace) + 1,
        },
    )


def drop_hubs(
    edges: list[tuple[str, str, float]], fraction: float
) -> tuple[list[tuple[str, str, float]], list[str]]:
    """The top `floor(fraction × entities)` entities by degree
    leave the graph before propagation, with every edge they touch. A hub
    that co-occurs with half the namespace says nothing about which half;
    left in, label propagation hands it the whole graph. Returns the
    remaining edges and the dropped entities, degree-descending; nothing is
    dropped when the fraction is 0 or the graph is too small for one."""
    if fraction <= 0 or not edges:
        return edges, []
    degree: dict[str, int] = {}
    for a, b, _w in edges:
        degree[a] = degree.get(a, 0) + 1
        degree[b] = degree.get(b, 0) + 1
    n_drop = int(len(degree) * fraction)
    if n_drop <= 0:
        return edges, []
    hubs = [e for e, _d in sorted(degree.items(), key=lambda kv: (-kv[1], kv[0]))[:n_drop]]
    hub_set = set(hubs)
    kept = [(a, b, w) for a, b, w in edges if a not in hub_set and b not in hub_set]
    return kept, hubs


def rows_for_namespace(
    org: str,
    ns: str,
    edges: list[tuple[str, str, float]],
    *,
    computed_at: datetime,
    max_iterations: int = 30,
) -> tuple[list[dict[str, Any]], list[Community]]:
    """Pure: edges -> insert rows + Community records (for logging/tests)."""
    assignment = label_propagation(edges, max_iterations=max_iterations)
    degree: dict[str, int] = {}
    for a, b, _w in edges:
        degree[a] = degree.get(a, 0) + 1
        degree[b] = degree.get(b, 0) + 1
    comms = summarize_communities(assignment, degree)
    by_id = {c.community_id: c for c in comms}
    rows = [
        {
            "org": org,
            "namespace": ns,
            "level": 0,
            "entity": n,
            "community_id": int(cid),
            "label": by_id[cid].label,
            "size": by_id[cid].size,
            "degree": int(degree.get(n, 0)),
            "computed_at": computed_at,
        }
        for n, cid in sorted(assignment.items())
    ]
    return rows, comms


async def run_entity_communities(
    cfg: EntityCommunitiesConfig | None = None, ch: _CHClient | None = None
) -> EntityCommunitiesResult:
    """One pass: for every selected namespace (opted-in + walked),
    recompute from its strongest edges and write."""
    cfg = cfg or EntityCommunitiesConfig()
    if ch is None:
        from server.core.ch import get_client

        ch = get_client()
    result = EntityCommunitiesResult()
    now = naive_utc_now()
    for org, ns in await _namespaces_with_edges(ch, cfg):
        pass_context.check_stop()
        raw = await _edges(ch, org, ns, cfg)
        if len(raw) > cfg.max_edges_per_namespace:
            # Keep the strongest max_edges pairs (the read is
            # weight-descending), as the Graph page does.
            raw = raw[: cfg.max_edges_per_namespace]
            result.truncated[f"{org}/{ns}"] = True
            log.info(
                "entity_communities: %s/%s over %d pairs; strongest %d kept (min weight %s)",
                org,
                ns,
                cfg.max_edges_per_namespace,
                len(raw),
                raw[-1]["w"] if raw else None,
            )
        edges = [(str(r["entity_a"]), str(r["entity_b"]), float(r["w"] or 0)) for r in raw]
        # Hub drop + propagation (escalated up to ~5 partitions) is seconds
        # of CPU on a max_edges graph: run it in a thread so the loop keeps
        # serving requests and other workers. Only this coroutine touches
        # `result`, and it is suspended while the thread writes to it.
        rows, comms = await asyncio.to_thread(_partition, org, ns, edges, cfg, result, now)
        pass_context.check_stop()  # the thread ran on past a lost lease
        if not rows:
            continue
        await ch.insert(TABLE, rows, settings={"async_insert": 0, "wait_for_async_insert": 1})
        result.namespaces += 1
        result.rows_written += len(rows)
        result.per_namespace[f"{org}/{ns}"] = len(comms)
    return result


def largest_share(comms: list[Community]) -> float:
    """Share of the assigned entities in the largest community; 0 for none."""
    total = sum(c.size for c in comms)
    return max((c.size for c in comms), default=0) / total if total else 0.0


def _partition(
    org: str,
    ns: str,
    edges: list[tuple[str, str, float]],
    cfg: EntityCommunitiesConfig,
    result: EntityCommunitiesResult,
    now: datetime,
) -> tuple[list[dict[str, Any]], list[Community]]:
    """Hub drop and propagation, escalated:
    while the largest community holds more than `giant_share` of the
    entities and the fraction is under `hub_drop_max_fraction`, double the
    fraction and partition again from the same edges. A partition where one
    community is most of the graph is not a partition; a fixed fraction that
    parts a conversational graph fuses a code corpus."""
    key = f"{org}/{ns}"
    start = fraction = float(cfg.hub_drop_fraction)
    ceiling = max(float(cfg.hub_drop_max_fraction), fraction)
    used = start
    hubs: list[str] = []
    while True:
        pass_context.check_stop()  # in the thread: a stopped pass partitions no further
        kept, new_hubs = drop_hubs(edges, fraction)
        if fraction > start and len(new_hubs) == len(hubs):
            break  # the step dropped no further hub: the partition is the same
        hubs, used = new_hubs, fraction
        rows, comms = rows_for_namespace(
            org, ns, kept, computed_at=now, max_iterations=cfg.max_iterations
        )
        share = largest_share(comms)
        if cfg.giant_share <= 0 or share <= cfg.giant_share or fraction >= ceiling:
            break
        fraction = min(fraction * 2 if fraction > 0 else ceiling, ceiling)
    fraction = used
    if hubs:
        result.hubs_dropped[key] = hubs
    if not hubs:
        # Too small for one hub at any fraction: one cluster
        # is its partition, nothing escalated and nothing to report.
        return rows, comms
    if fraction != start:
        result.hub_fraction_used[key] = fraction
        log.info(
            "entity_communities: %s fused at hub fraction %g; partitioned at %g "
            "(%d hubs, largest community %.1f%% of %d entities)",
            key,
            cfg.hub_drop_fraction,
            fraction,
            len(hubs),
            share * 100,
            len(rows),
        )
    if cfg.giant_share > 0 and share > cfg.giant_share:
        result.giant.append(key)
        log.info(
            "entity_communities: %s still fused at hub fraction %g "
            "(largest community %.1f%% of %d entities); written as is",
            key,
            fraction,
            share * 100,
            len(rows),
        )
    return rows, comms


# ------------------------------------------------------------------- readers


async def fetch_communities(
    ch: _CHClient, *, org: str, namespace: str, top_n: int = 12, members_per: int = 8
) -> list[dict[str, Any]]:
    """Largest communities of the latest pass with their top members
    (degree-descending). Empty when the worker has never run.

    Two statements, no FINAL. A pass writes one row per entity at one `computed_at`, so
    `computed_at = latest` is already one row per entity — FINAL would only merge every
    pass's rows first (tens of thousands of rows read for a dozen communities). The
    first statement ranks community ids by size on two small columns; the second reads
    the members of the ids it keeps."""
    ids = await ch.query_all(
        """
        WITH (
            SELECT max(computed_at) FROM loom.entity_communities
            WHERE org = {org:String} AND namespace = {ns:String} AND level = 0
        ) AS latest
        SELECT community_id, any(size) AS size, latest AS computed_at
        FROM loom.entity_communities
        WHERE org = {org:String} AND namespace = {ns:String} AND level = 0
          AND computed_at = latest
        GROUP BY community_id
        ORDER BY size DESC, community_id ASC
        LIMIT {lim:UInt32}
        """,
        params={"org": org, "ns": namespace, "lim": int(top_n)},
    )
    if not ids:
        return []
    latest = ids[0].get("computed_at")
    order = [int(r["community_id"]) for r in ids]
    members = await ch.query_all(
        """
        SELECT community_id, any(label) AS label, any(size) AS size,
               arraySlice(arrayMap(x -> x.2, arrayReverseSort(x -> x.1,
                   groupArray((degree, entity)))), 1, {mp:UInt32}) AS top_entities
        FROM loom.entity_communities
        WHERE org = {org:String} AND namespace = {ns:String} AND level = 0
          AND computed_at = {latest:DateTime}
          AND community_id IN {ids:Array(UInt32)}
        GROUP BY community_id
        """,
        params={
            "org": org,
            "ns": namespace,
            "mp": int(members_per),
            "latest": latest,
            "ids": order,
        },
    )
    by_id = {int(r["community_id"]): r for r in members}
    out: list[dict[str, Any]] = []
    for cid in order:
        r = by_id.get(cid)
        if r is None:
            continue
        out.append(
            {
                "community_id": cid,
                "label": str(r["label"] or ""),
                "size": int(r["size"] or 0),
                "top_entities": [str(e) for e in (r["top_entities"] or [])],
                "computed_at": latest,
            }
        )
    return out


async def community_assignment(
    ch: _CHClient, *, org: str, namespaces: list[str], entities: list[str]
) -> dict[str, int | None]:
    """entity -> community id (latest pass, first namespace that knows the
    entity). Entities the graph has not clustered map to None."""
    if not entities:
        return {}
    rows = await ch.query_all(
        """
        SELECT entity, community_id
        FROM loom.entity_communities FINAL
        WHERE org = {org:String} AND namespace IN {nss:Array(String)} AND level = 0
          AND entity IN {ents:Array(String)}
          AND (namespace, computed_at) IN (
              SELECT namespace, max(computed_at) FROM loom.entity_communities
              WHERE org = {org:String} AND namespace IN {nss:Array(String)} AND level = 0
              GROUP BY namespace
          )
        ORDER BY namespace
        """,
        params={"org": org, "nss": list(namespaces), "ents": list(entities)},
    )
    out: dict[str, int | None] = {e: None for e in entities}
    for r in rows:
        e = str(r["entity"])
        if out.get(e) is None:
            out[e] = int(r["community_id"])
    return out
