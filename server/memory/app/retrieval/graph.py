"""Graph legs of memory.search: entity-graph neighbours of the query's
entities and the memories that carry a matched triple, with the per-memory
triple weights the graph rank reads.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from server.memory.app.search_llm import expand_verb_synonyms
from server.memory.domain.graph_paths import extend_paths
from server.memory.domain.graph_quality import graph_node_text

log = logging.getLogger("server.memory.app.retrieval")


def _edge_validity(exclude_expired: bool, query_date: str | None) -> str:
    """The validity predicate every graph branch carries."""
    # NULL valid_to = open interval = always current. Skipped for the AS-OF
    # branch (query_date set), where the interval is already date-bounded.
    if exclude_expired and not (query_date or "").strip():
        _not_expired = " AND (valid_to IS NULL OR valid_to >= now())"
    else:
        _not_expired = ""
    # An edge retired on the supersession axis (a supersession close, or a junk
    # retirement) is not a neighbour, on the triple and co-occurrence branches
    # alike.
    return " AND invalid_at IS NULL" + _not_expired


def _one_hop_sql(not_expired: str) -> str:
    """One expansion hop: entity_edges (co-occurrence) + operator_edges (authored), both
    directions."""
    return f"""
    SELECT neighbor, sum(w) AS weight,
           argMax(seed, w) AS via_seed
    FROM (
        SELECT entity_b AS neighbor, co_occur AS w,
               entity_a AS seed
        FROM loom.entity_edges
        WHERE org = {{org:String}}
          AND namespace IN {{ns_scope:Array(String)}}
          AND entity_a IN {{seeds:Array(String)}}
          AND co_occur >= {{min_co:UInt32}}
          -- NULL bounds = always valid; query_date set = AS-OF.
          AND (
            {{qd:String}} = '' OR (
              (valid_from IS NULL OR valid_from <= parseDateTimeBestEffortOrNull({{qd:String}}))
              AND (valid_to IS NULL OR valid_to >= parseDateTimeBestEffortOrNull({{qd:String}}))
            )
          ){not_expired}
        UNION ALL
        SELECT entity_a AS neighbor, co_occur AS w,
               entity_b AS seed
        FROM loom.entity_edges
        WHERE org = {{org:String}}
          AND namespace IN {{ns_scope:Array(String)}}
          AND entity_b IN {{seeds:Array(String)}}
          AND co_occur >= {{min_co:UInt32}}
          AND (
            {{qd:String}} = '' OR (
              (valid_from IS NULL OR valid_from <= parseDateTimeBestEffortOrNull({{qd:String}}))
              AND (valid_to IS NULL OR valid_to >= parseDateTimeBestEffortOrNull({{qd:String}}))
            )
          ){not_expired}
        UNION ALL
        SELECT to_key AS neighbor,
               toUInt32(greatest(weight, 1.0)) AS w,
               from_key AS seed
        FROM loom.operator_edges FINAL
        WHERE org = {{org:String}}
          AND namespace IN {{ns_scope:Array(String)}}
          AND from_key IN {{seeds:Array(String)}}
          AND tombstoned_at IS NULL
        UNION ALL
        SELECT from_key AS neighbor,
               toUInt32(greatest(weight, 1.0)) AS w,
               to_key AS seed
        FROM loom.operator_edges FINAL
        WHERE org = {{org:String}}
          AND namespace IN {{ns_scope:Array(String)}}
          AND to_key IN {{seeds:Array(String)}}
          AND tombstoned_at IS NULL
    )
    WHERE neighbor NOT IN {{exclude:Array(String)}}
    GROUP BY neighbor
    ORDER BY weight DESC
    LIMIT {{lim:UInt32}}
    """


async def fetch_graph_neighbors(
    ch: Any,
    *,
    org: str,
    namespace: str,
    seed_entities: list[str],
    min_co_occur: int = 1,
    max_neighbors: int = 50,
    hops: int = 1,
    hub_skip_degree: int = 500,
    accessible_namespaces: list[str] | None = None,
    query_date: str | None = None,
    exclude_expired: bool = True,
    paths: dict[str, list[str]] | None = None,
) -> list[str]:
    """N-hop expansion of `seed_entities` via loom.entity_edges.

    `paths`, when a dict is passed, is filled with neighbour ->
    [seed,..., neighbour] for every neighbour returned:
    each hop records which frontier entity carried the heaviest vote for
    the neighbour (`argMax(seed, weight)`), so a graph-mode hit can say
    which query entity it hangs off and through what. Returned list is
    unchanged.

    Returns the top-`max_neighbors` neighbors by co-occurrence weight.
    Bidirectional — checks both entity_a and entity_b since pairs are
    canonicalized at write (entity_a <= entity_b).

    `hops` controls expansion depth (2-hop = neighbors-of-neighbors); each
    additional hop re-expands the previous hop's results, deduped against
    all prior hops and the seeds, and capped.

    `hub_skip_degree` drops seeds whose total co-occurrence degree (per
    loom.entity_degree MV) exceeds the threshold before expansion — hubs
    ("user", "i") expand to half the namespace and dump noise.

    `exclude_expired` (default True) drops edges whose `valid_to` is
    already past now() — supersession must not leak stale relations onto
    the default read path. Set False for historical queries wanting the
    full interval history; a no-op when `query_date` is set (the AS-OF
    filter already bounds validity).

    Returns [] on any error (graph mode degrades to cosine-only ranking).
    """
    if not seed_entities:
        return []
    seeds_canon = sorted({node for s in seed_entities if s and (node := graph_node_text(s))})
    if not seeds_canon:
        return []

    # UNION across all JWT-granted namespaces when supplied: cross-NS hop
    # only widens within already-authorized scope.
    ns_scope = sorted(set(accessible_namespaces)) if accessible_namespaces else [namespace]

    _not_expired = _edge_validity(exclude_expired, query_date)

    try:
        # Hub pruning: drop seeds whose degree exceeds the threshold.
        if hub_skip_degree > 0:
            degree_rows = await ch.query_all(
                """
                SELECT entity, sum(degree) AS d
                FROM loom.entity_degree
                WHERE org = {org:String}
                  AND namespace IN {ns_scope:Array(String)}
                  AND entity IN {seeds:Array(String)}
                GROUP BY entity
                HAVING d > {thresh:UInt32}
                """,
                params={
                    "org": org,
                    "ns_scope": ns_scope,
                    "seeds": seeds_canon,
                    "thresh": int(hub_skip_degree),
                },
            )
            hub_entities = {str(r["entity"]) for r in degree_rows}
            seeds_canon = [s for s in seeds_canon if s not in hub_entities]
            if not seeds_canon:
                return []

        async def _one_hop(
            current_seeds: list[str], exclude: set[str]
        ) -> list[tuple[str, str, float]]:
            # The per-row weight is `w`: an alias named `weight` on the inner rows
            # would be shadowed by the outer `sum(weight) AS weight` (the
            # analyzer rejected that shape with code 184).
            # entity_edges (co-occurrence) + operator_edges (authored), both
            # directions. operator weight (Float32) cast to UInt32 so 1.0 == 1
            # co-occurrence vote on entity_edges.co_occur scale.
            rows = await ch.query_all(
                _one_hop_sql(_not_expired),
                params={
                    "org": org,
                    "ns_scope": ns_scope,
                    "seeds": current_seeds,
                    "exclude": sorted(exclude),
                    "min_co": int(min_co_occur),
                    "lim": int(max_neighbors),
                    "qd": query_date or "",
                },
            )
            out: list[tuple[str, str, float]] = []
            for r in rows:
                n = str(r["neighbor"])
                out.append((n, str(r.get("via_seed") or ""), float(r.get("weight") or 1.0)))
            return out

        # Hop 1 uses the seeds; later hops use the prior hop's neighbors.
        # Cumulative `seen` prevents re-expansion.
        seen: set[str] = set(seeds_canon)
        current = seeds_canon
        all_neighbors: list[str] = []
        chains: dict[str, list[str]] = {}
        for _hop in range(max(1, int(hops))):
            hop_rows = await _one_hop(current, seen)
            if not hop_rows:
                break
            extend_paths(chains, [(n, via) for n, via, _w in hop_rows], seeds_canon)
            new_hop = [n for n, _via, _w in hop_rows]
            for n in new_hop:
                if n not in seen:
                    seen.add(n)
                    all_neighbors.append(n)
            current = new_hop
            if len(all_neighbors) >= max_neighbors:
                break
        kept = all_neighbors[:max_neighbors]
        if paths is not None:
            for n in kept:
                if n in chains:
                    paths[n] = list(chains[n])
        return kept
    except Exception as e:  # noqa: BLE001 — the graph leg is additive; fail open
        # Fail-open (additive leg), but LOG it — a silent `return []` here can
        # hide a real query bug (such as an alias shadowing a column) for an
        # entire leg. A cold/absent plane is normal; an exception is not.
        log.warning("fetch_graph_neighbors failed (fail-open): %s", e)
        return []


async def fetch_triple_matched_memories(
    ch: Any,
    *,
    org: str,
    namespace: str,
    query_triples: list[tuple[str, str, str]],
    max_keys: int = 50,
    expand_synonyms: bool = False,
    as_of: datetime | None = None,
    on_llm_call: Any = None,
) -> list[str]:
    """For each query triple, look up memory_keys in
    loom.entity_triple_memories whose stored triple is currently valid
    (or valid at `as_of`).

    '?' wildcards on subject or object are translated to "any value"
    via omitted WHERE clauses.

    With `expand_synonyms=True` (off by default), each query verb is widened
    via `expand_verb_synonyms` so stored triples with semantically
    equivalent verbs ("likes" / "prefers" / "enjoys") all match.
    Synonyms are extracted via one LLM call per unique verb, cached
    process-wide.

    Returns deduplicated memory_keys, capped at `max_keys`, ordered by
    match count desc (stronger triples first). Closed intervals are
    excluded from default current reads so stale facts do not receive
    the typed-triple rank boost.
    """
    if not query_triples:
        return []
    # Pre-expand all query verbs in parallel; misses fan out to concurrent
    # LLM calls.
    if expand_synonyms:
        verb_sets = await asyncio.gather(
            *[expand_verb_synonyms(v, on_llm_call=on_llm_call) for (_, v, _) in query_triples[:6]]
        )
    else:
        verb_sets = [[v] for (_, v, _) in query_triples[:6]]

    params: dict[str, Any] = {"org": org, "ns": namespace}
    if as_of is not None:
        validity_predicate = (
            "AND (t.valid_from IS NULL OR t.valid_from <= {as_of:DateTime}) "
            "AND (t.valid_to IS NULL OR t.valid_to >= {as_of:DateTime})"
        )
        params["as_of"] = as_of
    else:
        validity_predicate = "AND (t.valid_to IS NULL OR t.valid_to >= now())"
    selects: list[str] = []
    for i, ((s, _v, o), vset) in enumerate(
        zip(query_triples[:6], verb_sets, strict=True),
    ):
        where = [
            "tm.org = {org:String}",
            "tm.namespace = {ns:String}",
            f"tm.relation IN {{rels{i}:Array(String)}}",
        ]
        params[f"rels{i}"] = list(vset)
        if s and s != "?":
            where.append(f"tm.subject = {{sub{i}:String}}")
            params[f"sub{i}"] = s
        if o and o != "?":
            where.append(f"tm.object = {{obj{i}:String}}")
            params[f"obj{i}"] = o
        selects.append(
            "SELECT tm.memory_key "
            "FROM loom.entity_triple_memories AS tm "
            "INNER JOIN loom.entity_triples AS t "
            "ON t.org = tm.org "
            "AND t.namespace = tm.namespace "
            "AND t.subject = tm.subject "
            "AND t.relation = tm.relation "
            "AND t.object = tm.object "
            "WHERE " + " AND ".join(where) + " " + validity_predicate
        )
    # Aggregate match count across triples. No line comment may be spliced
    # into this string: a `-- ...` inside a one-line statement swallows the
    # closing parenthesis and ClickHouse answers "Unmatched parentheses" —
    # fail-open, so graph mode would never see a triple-matched memory.
    sql = " UNION ALL ".join(selects)
    sql = (
        "SELECT memory_key, count() AS matches FROM ("
        + sql
        + ") GROUP BY memory_key ORDER BY matches DESC, memory_key "
        + f"LIMIT {int(max_keys)}"
    )
    try:
        rows = await ch.query_all(sql, params=params)
        return [str(r["memory_key"]) for r in rows]
    except Exception as e:  # noqa: BLE001 — the triple leg is additive; fail open
        log.warning("fetch_triple_matched_memories failed (fail-open): %s", e)
        return []
