"""The statement builders of memory.search's graph expansion
(`server/memory/app/retrieval/graph.py`): which predicates every branch of a
hop carries. The hop is proximity-only: no typed-triple branch. Pure string
checks."""

from __future__ import annotations

import re

from server.memory.app.retrieval import graph as g


def _norm(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()


def test_edge_validity_always_drops_retired_edges() -> None:
    assert g._edge_validity(False, None) == " AND invalid_at IS NULL"
    assert g._edge_validity(True, "2026-01-01") == " AND invalid_at IS NULL"  # AS-OF: dated
    assert g._edge_validity(True, "  ") == (
        " AND invalid_at IS NULL AND (valid_to IS NULL OR valid_to >= now())"
    )
    assert g._edge_validity(True, None).endswith("valid_to >= now())")


def test_one_hop_reads_every_edge_source_in_both_directions() -> None:
    validity = g._edge_validity(True, None)
    sql = _norm(g._one_hop_sql(validity))
    assert sql.startswith("SELECT neighbor, sum(w) AS weight, argMax(seed, w) AS via_seed")
    assert sql.count("FROM loom.entity_edges") == 2
    assert sql.count("FROM loom.operator_edges FINAL") == 2
    assert "loom.entity_triples" not in sql
    # Every branch is tenant- and scope-bound.
    branches = sql.count("FROM loom.")
    assert sql.count("org = {org:String}") == branches
    assert sql.count("namespace IN {ns_scope:Array(String)}") == branches
    assert sql.count("co_occur >= {min_co:UInt32}") == 2
    assert sql.count("valid_to >= now()") == 2  # the two co-occurrence branches
    assert "GROUP BY neighbor" in sql


def test_one_hop_without_validity_carries_no_expiry() -> None:
    sql = _norm(g._one_hop_sql(""))
    assert "loom.entity_triples" not in sql
    assert "valid_to >= now()" not in sql
