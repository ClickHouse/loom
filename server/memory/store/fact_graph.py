"""The statements the facts plane walks the triple graph with: one traversal
hop, the edge set a path search reads, and the hub degree check."""

from __future__ import annotations

# The strongest provenance tier among the rows behind one aggregated edge:
# extracted, then inferred (including an unset tier), then facet.
STRONGEST_PROVENANCE_SQL = (
    "argMin(provenance, multiIf(provenance = 'extracted', 0, provenance = 'facet', 2, 1))"
)

# One primary-key range over the SummingMergeTree the entity_degree MV feeds:
# the sum is the entity's total co-occurrence degree in the namespace (the
# same figure memory.search's expansion reads for `hub_skip_degree`). The
# aggregate alias is not the source column name (ILLEGAL_AGGREGATION). The
# rule is applied in Python, so one read serves both thresholds.
_HUB_DEGREE_SQL = """
    SELECT entity, sum(degree) AS d
    FROM loom.entity_degree
    WHERE org = {org:String}
      AND namespace = {ns:String}
      AND entity IN {ents:Array(String)}
    GROUP BY entity
"""


def _traverse_hop_sql(
    *, typed: bool, relation_predicate: str, validity_predicate: str, rank_by: str = "co_occur"
) -> str:
    """One hop of facts.traverse: the aggregated bidirectional edge set out
    of `frontier`, minus `visited`, top-K pairs per parent. The forward
    half is a primary-key range on (org, namespace, entity_a); the reverse
    half scans the namespace's edges once.

    `rank_by` is what the per-parent top-K orders by:
    `co_occur` orders by the pair total alone; `specificity` and
    `cosine` join the namespace's `entity_degree` (one PK range, summed
    per entity) onto the pair's neighbour and order by
    `via_co_occur / degree` or `via_co_occur / sqrt(degree)`, co-mentions
    then name as the tie-break, and return the joined `degree` so the
    hub policy needs no second read. A neighbour the degree table does
    not know ranks as fully specific (degree taken as the pair itself)."""
    if typed:
        edge_union = f"""
                SELECT subject AS src, object AS dst, co_occur,
                       relation, 'out' AS direction, provenance
                FROM loom.entity_triples
                WHERE org = {{org:String}}
                  AND namespace = {{ns:String}}
                  AND subject IN {{frontier:Array(String)}}
                  {relation_predicate}
                  {validity_predicate}
                UNION ALL
                SELECT object AS src, subject AS dst, co_occur,
                       relation, 'in' AS direction, provenance
                FROM loom.entity_triples
                WHERE org = {{org:String}}
                  AND namespace = {{ns:String}}
                  AND object IN {{frontier:Array(String)}}
                  {relation_predicate}
                  {validity_predicate}
        """
    else:
        # A retired edge (invalid_at set) is not walked.
        edge_union = f"""
                SELECT entity_a AS src, entity_b AS dst, co_occur,
                       '' AS relation, '' AS direction, '' AS provenance
                FROM loom.entity_edges
                WHERE org = {{org:String}}
                  AND namespace = {{ns:String}}
                  AND entity_a IN {{frontier:Array(String)}}
                  AND invalid_at IS NULL
                  {validity_predicate}
                UNION ALL
                SELECT entity_b AS src, entity_a AS dst, co_occur,
                       '' AS relation, '' AS direction, '' AS provenance
                FROM loom.entity_edges
                WHERE org = {{org:String}}
                  AND namespace = {{ns:String}}
                  AND entity_b IN {{frontier:Array(String)}}
                  AND invalid_at IS NULL
                  {validity_predicate}
        """
    pairs = f"""
        -- The aggregate alias must NOT be `co_occur`: shadowing the source
        -- column makes ClickHouse resolve the HAVING reference into an
        -- aggregate-in-aggregate and reject the query with ILLEGAL_AGGREGATION.
        SELECT src, dst, relation, direction,
               toUInt32(sum(co_occur)) AS pair_co_occur,
               {STRONGEST_PROVENANCE_SQL} AS provenance
        FROM (
{edge_union}
        )
        GROUP BY src, dst, relation, direction
        HAVING pair_co_occur >= {{min_co:UInt32}}
    """
    if rank_by == "co_occur":
        return f"""
    SELECT src, dst, relation, direction, pair_co_occur, provenance
    FROM (
{pairs}
    )
    WHERE NOT has({{visited:Array(String)}}, dst)
    ORDER BY src, pair_co_occur DESC, dst
    LIMIT {{top_k_per_hop:UInt32}} BY src
    """
    # `degree` is the neighbour's total co-occurrence degree (entity_degree, summed per
    # entity: one PK range over the namespace); an unknown neighbour scores as if its degree
    # were the pair itself.
    denominator = (
        "greatest(degree, pair_co_occur)"
        if rank_by == "specificity"
        else "sqrt(greatest(degree, pair_co_occur))"
    )
    return f"""
    SELECT src, dst, relation, direction, pair_co_occur, provenance, degree
    FROM (
        SELECT p.src AS src, p.dst AS dst, p.relation AS relation, p.direction AS direction,
               p.pair_co_occur AS pair_co_occur, p.provenance AS provenance,
               toUInt32(coalesce(deg.d, 0)) AS degree,
               toFloat64(p.pair_co_occur) / {denominator} AS rank_score
        FROM (
{pairs}
        ) AS p
        LEFT JOIN (
            SELECT entity, toUInt32(sum(degree)) AS d
            FROM loom.entity_degree
            WHERE org = {{org:String}}
              AND namespace = {{ns:String}}
            GROUP BY entity
        ) AS deg ON deg.entity = p.dst
    )
    WHERE NOT has({{visited:Array(String)}}, dst)
    ORDER BY src, rank_score DESC, pair_co_occur DESC, dst
    LIMIT {{top_k_per_hop:UInt32}} BY src
    """


def _edge_set_sql(
    *,
    typed: bool,
    relation_predicate: str,
    validity_predicate: str,
) -> str:
    """The bidirectional, validity-filtered edge union facts.traverse and
    facts.path share: one row per raw observation, aggregated per (src, dst)
    by the caller. Co-occurrence edges carry no relation; typed edges carry
    the label and the direction relative to `src`."""
    if typed:
        return f"""
                SELECT subject AS src, object AS dst, co_occur,
                       relation, 'out' AS direction, valid_from, valid_to
                FROM loom.entity_triples
                WHERE org = {{org:String}}
                  AND namespace = {{ns:String}}
                  {relation_predicate}
                  {validity_predicate}
                UNION ALL
                SELECT object AS src, subject AS dst, co_occur,
                       relation, 'in' AS direction, valid_from, valid_to
                FROM loom.entity_triples
                WHERE org = {{org:String}}
                  AND namespace = {{ns:String}}
                  {relation_predicate}
                  {validity_predicate}
        """
    # A retired edge (invalid_at set) is not walked.
    return f"""
                SELECT entity_a AS src, entity_b AS dst, co_occur,
                       '' AS relation, '' AS direction, valid_from, valid_to
                FROM loom.entity_edges
                WHERE org = {{org:String}}
                  AND namespace = {{ns:String}}
                  AND invalid_at IS NULL
                  {validity_predicate}
                UNION ALL
                SELECT entity_b AS src, entity_a AS dst, co_occur,
                       '' AS relation, '' AS direction, valid_from, valid_to
                FROM loom.entity_edges
                WHERE org = {{org:String}}
                  AND namespace = {{ns:String}}
                  AND invalid_at IS NULL
                  {validity_predicate}
        """
