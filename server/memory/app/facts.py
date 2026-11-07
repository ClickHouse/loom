"""Facts plane handlers — query the (subject, relation, object) triple graph."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from server.core.ch import get_client
from server.memory.api.models_facts import (
    FactRow,
    FactsExpireByAdapterInput,
    FactsExpireByAdapterOutput,
    FactsListInput,
    FactsListOutput,
    FactsPathInput,
    FactsPathOutput,
    FactsRelatedInput,
    FactsRelatedOutput,
    FactsTraverseInput,
    FactsTraverseOutput,
    SourceRef,
    _PathEdge,
    _RelatedEntity,
    _TraversalNode,
)
from server.memory.store.fact_graph import (
    _HUB_DEGREE_SQL,
    STRONGEST_PROVENANCE_SQL,
    _edge_set_sql,
    _traverse_hop_sql,
)
from server.settings import settings

log = logging.getLogger(__name__)


def _query_relation(raw: str) -> str:
    """Caller-supplied relation, normalized EXACTLY like the write path
    (memory_projections.write_entity_triples: canonicalize_verb -> canonical_relation).
    Falls back to plain lowercase when canonicalization empties the verb
    (pure copula) so an odd query still filters on something."""
    from server.memory.domain.graph_quality import canonical_relation
    from server.memory.domain.search_text import canonicalize_verb

    return canonical_relation(canonicalize_verb(raw)).verb or raw.strip().lower()


async def handle_facts_list(input: FactsListInput) -> FactsListOutput:
    ch = get_client()
    where = [
        "org = {org:String}",
        "namespace = {ns:String}",
    ]
    params: dict[str, Any] = {
        "org": input.org,
        "ns": input.namespace,
        "lim": input.limit + 1,
    }
    # `entity` is a convenience that ORs across subject + object.
    # When set, it overrides subject/object so the caller can't
    # accidentally write a contradictory filter set.
    if input.entity is not None:
        where.append("(subject IN {ent:Array(String)} OR object IN {ent:Array(String)})")
        params["ent"] = _stored_spellings(input.entity)
    else:
        if input.subject is not None:
            where.append("subject IN {subj:Array(String)}")
            params["subj"] = _stored_spellings(input.subject)
        if input.object is not None:
            where.append("object IN {obj:Array(String)}")
            params["obj"] = _stored_spellings(input.object)
    if input.relation is not None:
        where.append("relation = {rel:String}")
        # Stored relations went through canonicalize_verb + canonical_relation
        # at write time ("works_at" -> "work_at"); a read that only lowercases
        # can NEVER match an inflected predicate. Reads and writes must share
        # one predicate space, so apply the same normalization here.
        params["rel"] = _query_relation(input.relation)
    # min_co_occur compares against the read-time sum, so it must live
    # in HAVING: in WHERE, ClickHouse resolves `co_occur` to the SELECT
    # alias (an aggregate) and rejects the query with ILLEGAL_AGGREGATION.
    # The HAVING must reference the alias, not repeat sum(co_occur) —
    # alias substitution would nest the aggregate (also ILLEGAL_AGGREGATION).
    having = ""
    if input.min_co_occur > 1:
        having = "HAVING co_occur >= {min_co:UInt32}"
        params["min_co"] = int(input.min_co_occur)
    if input.as_of is not None:
        where.append(
            "(valid_from IS NULL OR valid_from <= {as_of:DateTime}) "
            "AND (valid_to IS NULL OR valid_to >= {as_of:DateTime})"
        )
        params["as_of"] = input.as_of
    else:
        where.append("(valid_to IS NULL OR valid_to >= now())")
    if input.provenance:
        # An empty provenance reads as inferred.
        tiers: set[str] = set(input.provenance)
        if "inferred" in tiers:
            tiers.add("")
        where.append("provenance IN {prov:Array(String)}")
        params["prov"] = sorted(tiers)

    order_clause = (
        "ORDER BY co_occur DESC, last_seen DESC"
        if input.sort_by == "co_occur"
        else "ORDER BY last_seen DESC, co_occur DESC"
    )

    # entity_triples is plain MergeTree: every observation lands as its own row (co_occur=1
    # per insert; the engine never collapses them), so the per-triple total is computed at
    # read time via sum() + GROUP BY. The aggregation has to stay read-time: valid_from /
    # valid_to vary per row, and the validity filter must apply to the raw rows before the sum.
    sql = f"""
        SELECT
            subject, relation, object,
            sum(co_occur) AS co_occur,
            max(last_seen) AS last_seen,
            -- The strongest tier behind the aggregated triple.
            -- Not `AS provenance`: with the provenance filter in WHERE the
            -- analyzer resolves the alias into the aggregate (code 184).
            {STRONGEST_PROVENANCE_SQL} AS provenance_tier
        FROM loom.entity_triples
        WHERE {" AND ".join(where)}
        GROUP BY subject, relation, object
        {having}
        {order_clause}
        LIMIT {{lim:UInt32}}
    """
    rows = await ch.query_all(sql, params=params)
    has_more = len(rows) > input.limit
    page = rows[: input.limit]

    sources: dict[tuple[str, str, str], list[str]] = {}
    if (input.include_sources or input.include_source_status) and page:
        sources = await _source_keys_for(ch, input, page)
    status_of: dict[str, str] = {}
    if input.include_source_status and sources:
        status_of = await _source_statuses_for(ch, input, sources)

    def _row(r: dict[str, Any]) -> FactRow:
        key = (str(r["subject"]), str(r["relation"]), str(r["object"]))
        keys = sources.get(key, [])
        refs = (
            [SourceRef(memory_key=k, status=status_of.get(k, "")) for k in keys]
            if input.include_source_status
            else []
        )
        return FactRow(
            provenance=str(r.get("provenance_tier") or ""),
            subject=key[0],
            relation=key[1],
            object=key[2],
            co_occur=int(r["co_occur"]),
            last_seen=r["last_seen"],
            source_keys=keys if input.include_sources else [],
            sources=refs,
            stale=triple_is_stale([ref.status for ref in refs]),
        )

    return FactsListOutput(rows=[_row(r) for r in page], truncated=has_more)


async def _source_keys_for(
    ch: Any, input: FactsListInput, page: list[dict[str, Any]]
) -> dict[tuple[str, str, str], list[str]]:
    """Source-key fan-out: one query joining the page's triples to
    `entity_triple_memories`, bounded to 10 keys per triple. Read without FINAL (GROUP BY
    the plain table) this read's bytes fell 1.6-2.1x but wall rose 1-3 ms and the ten keys
    served at the cap changed on 25/200 calls, so the streaming merge stays: wall time
    matters more than bytes here."""
    triple_tuples = [(str(r["subject"]), str(r["relation"]), str(r["object"])) for r in page]
    src_rows = await ch.query_all(
        """
        SELECT subject, relation, object,
               groupArray(10)(memory_key) AS source_keys
        FROM loom.entity_triple_memories FINAL
        WHERE org = {org:String}
          AND namespace = {ns:String}
          AND subject IN {subjects:Array(String)}
          AND relation IN {relations:Array(String)}
          AND object IN {objects:Array(String)}
        GROUP BY subject, relation, object
        """,
        params={
            "org": input.org,
            "ns": input.namespace,
            "subjects": list({t[0] for t in triple_tuples}),
            "relations": list({t[1] for t in triple_tuples}),
            "objects": list({t[2] for t in triple_tuples}),
        },
    )
    return {
        (str(r["subject"]), str(r["relation"]), str(r["object"])): list(r.get("source_keys") or [])
        for r in src_rows
    }


async def _source_statuses_for(
    ch: Any, input: FactsListInput, sources: dict[tuple[str, str, str], list[str]]
) -> dict[str, str]:
    """The rows behind the triples, by status. A triple `facts.list` returned is open; when
    every row behind it is superseded the planes disagree (a shape that shows up as multi-hop
    recall misses)."""
    all_keys = sorted({k for ks in sources.values() for k in ks})
    if not all_keys:
        return {}
    st_rows = await ch.query_all(
        """
        SELECT memory_key, groupUniqArray(status) AS statuses
        FROM loom.memory_embeddings FINAL
        WHERE org = {org:String}
          AND namespace = {ns:String}
          AND memory_key IN {keys:Array(String)}
        GROUP BY memory_key
        """,
        params={"org": input.org, "ns": input.namespace, "keys": all_keys},
    )
    return {str(r["memory_key"]): source_status(list(r.get("statuses") or [])) for r in st_rows}


_LIVE_STATUSES = frozenset({"active", "conflicted"})


def source_status(statuses: list[str]) -> str:
    """One status for a key that has several rows (one per session): live
    wins over retired, so a key with any active row reads `active`."""
    ss = [str(x) for x in statuses if x]
    if not ss:
        return ""
    for live in ("active", "conflicted"):
        if live in ss:
            return live
    for retired in ("superseded", "duplicate", "deleted"):
        if retired in ss:
            return retired
    return ss[0]


def triple_is_stale(statuses: list[str]) -> bool:
    """An open triple whose every known source row is retired: the planes
    disagree. No sources, or an unknown status, is not evidence either way."""
    known = [s for s in statuses if s]
    return bool(known) and not any(s in _LIVE_STATUSES for s in known)


async def handle_facts_related(input: FactsRelatedInput) -> FactsRelatedOutput:
    """Return the entities that co-occur with `input.entity` via any
    relation, ranked by total co_occur (DESC).

    Walks `entity_triples` in both directions — subject=entity and
    object=entity — and unions the neighbor sets. Each neighbor
    carries which direction it appeared in (`role`) so callers can
    distinguish "alice WORKS_AT acme" (acme is object) from
    "acme EMPLOYS alice" (alice is object).
    """

    ch = get_client()
    spellings = _stored_spellings(input.entity)
    validity_predicate = ""
    params: dict[str, Any] = {
        "org": input.org,
        "ns": input.namespace,
        "ent": spellings,
        "lim": input.top_k * 2,  # widen so we can collapse role=both
    }
    if input.as_of is not None:
        validity_predicate = """
              AND (valid_from IS NULL OR valid_from <= {as_of:DateTime})
              AND (valid_to IS NULL OR valid_to >= {as_of:DateTime})
        """
        params["as_of"] = input.as_of
    else:
        validity_predicate = """
              AND (valid_to IS NULL OR valid_to >= now())
        """
    rows = await ch.query_all(
        f"""
        SELECT neighbor, role, sum(co_occur) AS co_occur
        FROM (
            SELECT object AS neighbor, 'object' AS role, sum(co_occur) AS co_occur
            FROM loom.entity_triples
            WHERE org = {{org:String}}
              AND namespace = {{ns:String}}
              AND subject IN {{ent:Array(String)}}
              {validity_predicate}
            GROUP BY object
            UNION ALL
            SELECT subject AS neighbor, 'subject' AS role, sum(co_occur) AS co_occur
            FROM loom.entity_triples
            WHERE org = {{org:String}}
              AND namespace = {{ns:String}}
              AND object IN {{ent:Array(String)}}
              {validity_predicate}
            GROUP BY subject
        )
        WHERE neighbor NOT IN {{ent:Array(String)}}
        GROUP BY neighbor, role
        ORDER BY co_occur DESC
        LIMIT {{lim:UInt32}}
        """,
        params=params,
    )
    # Collapse the two role rows per neighbor: if `alice→acme` AND
    # `acme→alice` both exist, role='both' on a single row with the
    # summed co_occur.
    by_neighbor: dict[str, dict[str, Any]] = {}
    for r in rows:
        n = str(r["neighbor"])
        if n in by_neighbor:
            by_neighbor[n]["co_occur"] += int(r["co_occur"])
            by_neighbor[n]["role"] = "both"
        else:
            by_neighbor[n] = {"co_occur": int(r["co_occur"]), "role": str(r["role"])}
    related = sorted(
        (
            _RelatedEntity(entity=n, co_occur=info["co_occur"], role=info["role"])
            for n, info in by_neighbor.items()
        ),
        key=lambda x: -x.co_occur,
    )[: input.top_k]

    return FactsRelatedOutput(entity=spellings[0] if spellings else "", related=related)


async def _after_own_projections(org: str, ns: str) -> tuple[datetime | None, datetime]:
    """Wait (at most `memory_facts_projection_wait_s`) for the graph projections of
    (org, ns) in flight, so a walk sees the edges a write just made. Returns
    `graph_write_marks.last_write`'s (last write, server clock)."""
    import time

    from server.memory.app import after_write
    from server.memory.app import graph_write_marks as marks

    t0 = time.monotonic()
    await after_write.settle(org, ns, timeout_s=settings.memory_facts_projection_wait_s)
    return await marks.last_write(
        org, ns, wait_s=settings.memory_facts_projection_wait_s - (time.monotonic() - t0)
    )


class _WalkMemo:
    """Walks remembered per seed and arguments, checked against the graph-write marks.
    Entries are (expires at, read started at, walk), on the ClickHouse server's clock:
    the marks are shared across processes and stamped by that clock too, and the
    start is read before the walk is."""

    def __init__(self) -> None:
        self.cache: dict[tuple[Any, ...], tuple[datetime, datetime, FactsTraverseOutput]] = {}

    async def walk(self, input: FactsTraverseInput) -> FactsTraverseOutput:

        from server.core.ch import sequential_reads, sequential_reads_active
        from server.memory.store import read_your_writes_enabled
        from server.settings import settings

        org, ns = input.org, input.namespace
        try:
            # A write in the walk's own millisecond retires it (`>` below).
            wrote, started = await _after_own_projections(org, ns)
        except Exception as e:  # noqa: BLE001 — a walk never fails on its memo's marks
            log.warning("facts.traverse: graph marks unread (%s); walking without the memo", e)
            return await _handle_facts_traverse_uncached(input)
        key = _traverse_key(input)
        hit = self.cache.get(key)
        if hit is not None and hit[0] > started and (wrote is None or hit[1] > wrote):
            return hit[2].model_copy(deep=True)
        window = timedelta(seconds=float(settings.ch_read_your_writes_window_s))
        settling = wrote is not None and started - wrote < window
        if settling and read_your_writes_enabled() and not sequential_reads_active():
            with sequential_reads():
                out = await _handle_facts_traverse_uncached(input)
        else:
            out = await _handle_facts_traverse_uncached(input)
        if settling:
            return out
        if len(self.cache) > 512:
            self.cache.clear()
        expires = started + timedelta(seconds=settings.memory_facts_traverse_ttl_s)
        self.cache[key] = (expires, started, out.model_copy(deep=True))
        return out


_memo = _WalkMemo()


def reset_traverse_cache() -> None:
    """Tests: forget remembered walks."""
    _memo.cache.clear()


def _traverse_key(input: FactsTraverseInput) -> tuple[Any, ...]:
    return (
        input.org,
        input.namespace,
        input.seed_entity.strip().lower(),
        int(input.max_hops),
        int(input.top_k_per_hop),
        int(input.min_co_occur),
        int(input.max_nodes),
        input.as_of.isoformat() if input.as_of is not None else "",
        tuple(sorted(_query_relation(r) for r in (input.relations or []) if r.strip())),
        bool(input.labeled),
        # The EFFECTIVE hub thresholds: a request that leaves them unset must not read a walk
        # cached under others.
        _hub_policy_for(input),
        _rank_by_for(input),
    )


async def handle_facts_traverse(input: FactsTraverseInput) -> FactsTraverseOutput:
    """A walk is remembered for a minute per seed and arguments. The Graph page re-walks the
    same seed on every hover and re-click, and a two-hop walk reads tens of MB of
    entity_triples (26–54 MB measured). Same seed, same minute, no graph write between: the
    same nodes.

    Read-your-writes, across the server's processes: a walk first waits (bounded) for the
    graph projections of its namespace that began before it and are still being written
    beside a `memory.set` response, in this process and in the others that share the marks
    (server/memory/app/graph_write_marks.py); one still in flight after the wait counts as a
    write. A remembered walk read before the namespace's last graph write is not served, a
    walk inside the replication window after that write reads with sequential consistency,
    and such a walk is not remembered — its edges may still be landing."""
    return await _memo.walk(input)


async def _handle_facts_traverse_uncached(input: FactsTraverseInput) -> FactsTraverseOutput:
    """N-hop entity graph walk, one bounded ClickHouse query per hop.

    The walk trims BEFORE it expands. Each hop sends the current frontier (at most
    `top_k_per_hop` entities per parent of the previous hop) as an IN list, aggregates the
    bidirectional edge set per (src, dst) pair, drops anything already visited, and keeps the
    top-K strongest pairs per parent with `LIMIT K BY src`. Only those K children become the
    next frontier, so a hop-h frontier has at most K^h entities and the whole walk at most
    `max_nodes` rows. A single WITH RECURSIVE CTE that expands every qualifying neighbour and
    trims afterwards returns tens of thousands of nodes for a two-hop walk from a hub, and
    exceeds the ClickHouse memory limit at three hops on a graph of ~1.7M edges.

    entity_edges is plain MergeTree holding one raw row per observation
    (the write path inserts co_occur=1 each time; the engine never
    collapses them), so the edge set aggregates sum(co_occur) per
    (src, dst) pair at read time: `min_co_occur` compares against the
    pair's observation total, `via_co_occur` reports that total. The
    validity filter applies to the raw rows BEFORE the sum — valid_from /
    valid_to vary per row, so an engine-level merge could not honor the
    `as_of` window.

    The visited set is global to the walk (an entity is reported once, at
    the first hop that reaches it, via its strongest parent), so `path` is
    the shortest chain the walk found and a reader can draw the result as
    a tree.
    """
    from server.memory.domain.graph_quality import canonical_entity_text

    ch = get_client()
    # The seed is canonicalised the way the WRITE path stores entities (articles and
    # possessives stripped, head noun singularised, first person -> "user"), exactly as
    # facts.path does: a bare lower() would make "Alice's" and "The user" miss the rows
    # written for "alice" and "user", and memory.ask's expansion step passes seeds in the
    # question's own words.
    raw = input.seed_entity.strip().lower()
    seed = canonical_entity_text(input.seed_entity) or raw
    params: dict[str, Any] = {
        "org": input.org,
        "ns": input.namespace,
        "top_k_per_hop": int(input.top_k_per_hop),
        "min_co": int(input.min_co_occur),
    }
    if input.as_of is not None:
        validity_predicate = """
                  AND (valid_from IS NULL OR valid_from <= {as_of:DateTime})
                  AND (valid_to IS NULL OR valid_to >= {as_of:DateTime})
        """
        params["as_of"] = input.as_of
    else:
        validity_predicate = """
                  AND (valid_to IS NULL OR valid_to >= now())
        """
    # Typed walk: the edge set comes from entity_triples, carrying the
    # relation and its direction, with an optional label filter. Same
    # aggregation, same trim — only the edge source changes.
    relation_predicate = ""
    if input.relations:
        # The stored relation is the write path's canonical verb (`_query_relation`), as
        # facts.path and facts.list filter it; a plain lower-case form would miss every
        # inflected verb (`works_at` is stored `work_at`).
        params["rels"] = [_query_relation(r) for r in input.relations if r.strip()]
        relation_predicate = "AND relation IN {rels:Array(String)}"
    sql = _traverse_hop_sql(
        typed=bool(input.labeled or input.relations),
        relation_predicate=relation_predicate,
        validity_predicate=validity_predicate,
        rank_by=_rank_by_for(input),
    )

    async def _hop(frontier: list[str], visited: list[str]) -> list[dict[str, Any]]:
        return await ch.query_all(
            sql, params={**params, "frontier": list(frontier), "visited": list(visited)}
        )

    hub_degree, hub_share = _hub_policy_for(input)
    seed, nodes, truncated, hubs = await _walk(
        _hop,
        seed_variants=_seed_variants(seed, raw),
        max_hops=int(input.max_hops),
        max_nodes=int(input.max_nodes),
        hub_fn=(
            _hub_lookup(ch, input.org, input.namespace, hub_degree, hub_share)
            if (hub_degree or hub_share)
            else None
        ),
    )
    seed_cid = await _colour_by_community(ch, input.org, input.namespace, seed, nodes)
    return FactsTraverseOutput(
        seed=seed, seed_community_id=seed_cid, nodes=nodes, truncated=truncated, hubs=hubs
    )


def _rank_by_for(input: FactsTraverseInput) -> str:
    """What this walk's trim ranks by: the request's `rank_by` when given, else the
    deployment default."""
    if input.rank_by:
        return str(input.rank_by)
    from server.settings import settings

    return settings.facts_traverse_rank_by


def _hub_policy_for(input: FactsTraverseInput) -> tuple[int, float]:
    """The hub thresholds this walk applies: the request's values when given,
    else the deployment default share (the absolute `hub_degree` rule runs only
    when the request asks for it). (0, 0.0) = expand all."""
    from server.settings import settings

    degree = max(0, int(input.hub_degree or 0))
    if input.hub_share is not None:
        share = float(input.hub_share)
    else:
        share = max(0.0, float(settings.facts_traverse_hub_share))
    return degree, share


def is_hub(pair_co_occur: int, degree: int, *, hub_degree: int, hub_share: float) -> bool:
    """The hub decision for one reached node: its degree is at or above `hub_degree`, or the
    edge that reached it is at most `hub_share` of that degree. A node the degree table does
    not know (degree 0) is never a hub. Pure."""
    if degree <= 0:
        return False
    if hub_degree > 0 and degree >= hub_degree:
        return True
    return hub_share > 0 and (float(pair_co_occur) / float(degree)) <= hub_share


def _hub_lookup(ch: Any, org: str, namespace: str, hub_degree: int, hub_share: float) -> Any:
    """Hub policy: which of the entities a hop
    reached are hubs, and every one's degree. One read per hop, over the
    hop's new nodes only. Fails open — a namespace whose degree table is
    missing or unreadable walks as if the policy were off, and says so in
    the log."""

    async def _hubs(
        pairs: list[tuple[str, int, int | None]],
    ) -> tuple[set[str], dict[str, int]]:
        # A hop ranked by specificity already joined the degrees; only the nodes that arrived
        # without one are read.
        degree_of = {e: int(d) for e, _, d in pairs if d is not None}
        missing = [e for e, _, d in pairs if d is None]
        if missing:
            try:
                rows = await ch.query_all(
                    _HUB_DEGREE_SQL, params={"org": org, "ns": namespace, "ents": missing}
                )
            except Exception as e:  # noqa: BLE001 — the policy is additive
                log.warning("facts.traverse: hub degree lookup failed (open): %s", e)
                rows = []
            degree_of.update({str(r["entity"]): int(r["d"] or 0) for r in rows})
        held = {
            e
            for e, w, _ in pairs
            if is_hub(int(w), degree_of.get(e, 0), hub_degree=hub_degree, hub_share=hub_share)
        }
        return held, degree_of

    return _hubs


def _stored_spellings(raw: str) -> list[str]:
    """The spellings a stored node can have for `raw`, canonical first: its canonical form,
    which every writer stores, and `raw` lowered, a spelling stored rows of a "commit:" or
    "error:" concept facet can hold. None for a name with no canonical form."""
    from server.memory.domain.graph_quality import canonical_entity_text

    node = canonical_entity_text(raw)
    return list(dict.fromkeys((node, raw.strip().lower()))) if node else []


def _seed_variants(seed: str, raw: str) -> list[str]:
    """A graph may hold the entity under its raw or plural/singular spelling; "pr" in the
    taxonomy is "prs" in the edges. Canonical form first, then the cheap
    variants; the walk starts from the first that has an edge."""
    out = [seed]
    plural = seed + "s" if not seed.endswith("s") else ""
    singular = seed[:-1] if seed.endswith("s") and len(seed) > 3 else ""
    for v in (raw, plural, singular):
        if v and v not in out:
            out.append(v)
    return out


def _new_nodes(rows: list[dict[str, Any]], *, seen: Mapping[str, float]) -> list[dict[str, Any]]:
    """One row per entity a hop reached for the first time: the strongest
    edge that reached it wins, strongest first, name as the tie-break."""
    best: dict[str, dict[str, Any]] = {}
    for r in rows:
        dst = str(r["dst"])
        if dst in seen:
            continue
        cur = best.get(dst)
        if cur is None or int(r["pair_co_occur"]) > int(cur["pair_co_occur"]):
            best[dst] = r
    return sorted(best.values(), key=lambda r: (-int(r["pair_co_occur"]), str(r["dst"])))


async def _walk(
    hop_fn: Any,
    *,
    seed_variants: list[str],
    max_hops: int,
    max_nodes: int,
    hub_fn: Any = None,
) -> tuple[str, list[Any], bool, list[str]]:
    """The hop loop of facts.traverse: probe the seed spellings, then expand
    frontier by frontier until max_hops, an empty frontier, or the node
    ceiling. Returns (seed walked, nodes ordered by hop then weight,
    truncated, hubs).

    `hub_fn`, when given, takes the hop's new (entity, via_co_occur) pairs and names which
    are hubs, with every node's degree; hubs are reported like any other node but leave the
    frontier, so the walk does not continue through them. The seed is never asked about: the
    caller chose it. None = every node expands."""

    seed = seed_variants[0]
    rows: list[dict[str, Any]] = []
    for v in seed_variants:
        rows = await hop_fn([v], [v])
        if rows:
            seed = v
            break
    nodes: list[Any] = []
    hubs: list[str] = []
    visited: list[str] = [seed]
    weight_of: dict[str, float] = {seed: 1.0}
    path_of: dict[str, list[str]] = {seed: [seed]}
    hop = 1
    capped = False
    while True:
        new_rows = _new_nodes(rows, seen=weight_of)
        room = max_nodes - len(nodes)
        capped = len(new_rows) > room
        kept = new_rows[: max(0, room)]
        held: set[str] = set()
        # Degrees the hop SQL joined; None = not joined.
        degree_of: dict[str, int] = {
            str(r["dst"]): int(r["degree"]) for r in kept if r.get("degree") is not None
        }
        if hub_fn is not None and kept:
            held, more = await hub_fn(
                [
                    (str(r["dst"]), int(r["pair_co_occur"]), degree_of.get(str(r["dst"])))
                    for r in kept
                ]
            )
            held = set(held)
            degree_of.update(more)
        frontier: list[str] = []
        for r in kept:
            parent, dst = str(r["src"]), str(r["dst"])
            cw = weight_of.get(parent, 1.0) + 1.0 / float(hop)
            path = [*path_of.get(parent, [parent]), dst]
            weight_of[dst] = cw
            path_of[dst] = path
            visited.append(dst)
            held_here = dst in held
            if held_here:
                hubs.append(dst)
            else:
                frontier.append(dst)
            nodes.append(
                _TraversalNode(
                    entity=dst,
                    hop=hop,
                    cumulative_weight=cw,
                    via_co_occur=int(r["pair_co_occur"]),
                    via_relation=str(r.get("relation") or ""),
                    via_direction=str(r.get("direction") or ""),
                    via_provenance=str(r.get("provenance") or ""),
                    hub=held_here,
                    degree=int(degree_of.get(dst, 0)),
                    path=path,
                )
            )
        if capped or not frontier or hop >= max_hops:
            break
        hop += 1
        rows = await hop_fn(frontier, visited)
    nodes.sort(key=lambda n: (n.hop, -n.cumulative_weight, n.entity))
    return seed, nodes, capped, hubs


async def _colour_by_community(
    ch: Any, org: str, namespace: str, seed: str, nodes: list[Any]
) -> int | None:
    """Colour by community: one lookup for the seed and every node, fail-open; a namespace
    the communities worker has not visited answers with None everywhere."""
    if not nodes:
        return None
    try:
        from server.memory.app.entity_communities import community_assignment

        assignment = await community_assignment(
            ch, org=org, namespaces=[namespace], entities=[seed, *(n.entity for n in nodes)]
        )
        for n in nodes:
            n.community_id = assignment.get(n.entity)
        return assignment.get(seed)
    except Exception as e:  # noqa: BLE001 — communities are additive
        log.debug("facts.traverse: community lookup skipped: %s", e)
        return None


async def handle_facts_path(input: FactsPathInput) -> FactsPathOutput:
    """A path between two entities: bidirectional bounded BFS.

    Each step fetches ONE frontier from ClickHouse — the neighbours of the
    smaller side's frontier over the aggregated, validity-filtered edge set
    facts.traverse walks (sum of raw observation rows per pair, `min_co_occur`
    against the total, per-src top-k trim) — and the meeting test runs in
    Python (memory/domain/graph_path). `max_hops` bounds the total path length, so
    each side explores at most ceil(max_hops / 2) hops: the explored set is
    ~2·b^(d/2) instead of b^d.

    Validity mirrors facts.traverse exactly: `as_of` = interval containment;
    default = edges whose valid_to is open or in the future. Both entities are
    canonicalised the way the write path stores them (canonical_entity_text),
    so "Alice's" finds "alice" and "error:Timeouts" finds "error:timeouts".
    """
    from server.memory.domain import graph_path as gp
    from server.memory.domain.graph_quality import canonical_entity_text

    a_text = canonical_entity_text(input.entity_a) or input.entity_a.strip().lower()
    b_text = canonical_entity_text(input.entity_b) or input.entity_b.strip().lower()
    if a_text == b_text:
        return FactsPathOutput(
            entity_a=a_text,
            entity_b=b_text,
            found=True,
            path=[a_text],
            edges=[],
            hops=0,
            truncated=False,
        )

    try:
        await _after_own_projections(input.org, input.namespace)
    except Exception as e:  # noqa: BLE001 — a path never fails on the graph marks
        log.warning("facts.path: graph marks unread (%s); walking now", e)
    ch = get_client()
    params: dict[str, Any] = {
        "org": input.org,
        "ns": input.namespace,
        "top_k_per_hop": int(input.top_k_per_hop),
        "min_co": int(input.min_co_occur),
    }
    if input.as_of is not None:
        validity_predicate = """
                  AND (valid_from IS NULL OR valid_from <= {as_of:DateTime})
                  AND (valid_to IS NULL OR valid_to >= {as_of:DateTime})
        """
        params["as_of"] = input.as_of
    else:
        validity_predicate = """
                  AND (valid_to IS NULL OR valid_to >= now())
        """
    typed = bool(input.labeled or input.relations)
    relation_predicate = ""
    if input.relations:
        params["rels"] = [_query_relation(r) for r in input.relations if r.strip()]
        relation_predicate = "AND relation IN {rels:Array(String)}"
    edge_union = _edge_set_sql(
        typed=typed, relation_predicate=relation_predicate, validity_predicate=validity_predicate
    )
    # One frontier: neighbours of every entity in {frontier}, one row per
    # (src, dst[, relation, direction]) pair with the pair's observation total
    # and its validity envelope, trimmed to top-k per src by that total.
    # `degree` is the untrimmed count so the caller can tell a trim happened.
    frontier_sql = f"""
    SELECT src, dst, relation, direction, pair_co_occur, valid_from, valid_to, degree
    FROM (
        SELECT src, dst, relation, direction, pair_co_occur, valid_from, valid_to,
               row_number() OVER (PARTITION BY src ORDER BY pair_co_occur DESC, dst) AS rn,
               count() OVER (PARTITION BY src) AS degree
        FROM (
            SELECT src, dst, relation, direction,
                   toUInt32(sum(co_occur)) AS pair_co_occur,
                   min(valid_from) AS valid_from,
                   if(countIf(valid_to IS NULL) > 0, NULL, max(valid_to)) AS valid_to
            FROM (
{edge_union}
            )
            WHERE src IN {{frontier:Array(String)}}
            GROUP BY src, dst, relation, direction
            HAVING pair_co_occur >= {{min_co:UInt32}}
        )
    )
    WHERE rn <= {{top_k_per_hop:UInt32}}
    ORDER BY src, rn
    """

    a = gp.Side(root=a_text)
    b = gp.Side(root=b_text)
    trimmed = False
    hops_explored = 0
    while a.depth + b.depth < int(input.max_hops):
        side = gp.pick_side(a, b)
        if not side.frontier:
            break
        rows = await ch.query_all(frontier_sql, params={**params, "frontier": list(side.frontier)})
        if any(int(r.get("degree") or 0) > int(input.top_k_per_hop) for r in rows):
            trimmed = True
        new = gp.absorb(side, rows)
        hops_explored += 1
        other = b if side is a else a
        hit = gp.meet(a, b, (n for n in new if n in other.visited))
        if hit is not None:
            nodes, edges = hit
            return FactsPathOutput(
                entity_a=a_text,
                entity_b=b_text,
                found=True,
                path=nodes,
                edges=[
                    _PathEdge(
                        entity_a=e.entity_a,
                        entity_b=e.entity_b,
                        relation=e.relation,
                        direction=e.direction,
                        co_occur=e.co_occur,
                        valid_from=e.valid_from,
                        valid_to=e.valid_to,
                    )
                    for e in edges
                ],
                hops=len(edges),
                truncated=False,
            )
    exhausted_budget = bool(a.frontier and b.frontier)
    return FactsPathOutput(
        entity_a=a_text,
        entity_b=b_text,
        found=False,
        path=[],
        edges=[],
        hops=hops_explored,
        truncated=exhausted_budget or trimmed,
    )


async def handle_facts_expire_by_adapter(
    input: FactsExpireByAdapterInput,
) -> FactsExpireByAdapterOutput:
    ch = get_client()
    conf_threshold = float(input.max_confidence) + 1e-6
    params = {
        "org": input.org,
        "ns": input.namespace,
        "adapter": input.adapter_name,
        "conf": conf_threshold,
    }

    cnt_row = await ch.query_one(
        """
        SELECT
            (SELECT count() FROM loom.entity_triples
             WHERE org = {org:String} AND namespace = {ns:String}
               AND adapter_name = {adapter:String}
               AND confidence < {conf:Float32}) AS t_count,
            (SELECT count() FROM loom.entity_triple_memories
             WHERE org = {org:String} AND namespace = {ns:String}
               AND adapter_name = {adapter:String}
               AND confidence < {conf:Float32}) AS tm_count
        """,
        params=params,
    )
    t_count = int(cnt_row["t_count"] if cnt_row else 0)
    tm_count = int(cnt_row["tm_count"] if cnt_row else 0)

    if t_count > 0:
        await ch.execute(
            "ALTER TABLE loom.entity_triples "
            "DELETE WHERE org = {org:String} "
            "  AND namespace = {ns:String} "
            "  AND adapter_name = {adapter:String} "
            "  AND confidence < {conf:Float32}",
            params=params,
        )
    if tm_count > 0:
        await ch.execute(
            "ALTER TABLE loom.entity_triple_memories "
            "DELETE WHERE org = {org:String} "
            "  AND namespace = {ns:String} "
            "  AND adapter_name = {adapter:String} "
            "  AND confidence < {conf:Float32}",
            params=params,
        )

    if t_count > 0 or tm_count > 0:
        # A mutation is no INSERT, so the client's graph-write mark does not see it; without
        # the mark a walk remembered before the delete keeps serving the expired triples.
        from server.memory.app import graph_write_marks as _graph_marks

        _graph_marks.note_write(input.org, input.namespace)

    return FactsExpireByAdapterOutput(
        expired_triples=t_count,
        expired_triple_memories=tm_count,
    )
