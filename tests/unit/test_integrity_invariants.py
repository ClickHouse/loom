"""The integrity gauges must exist, read from the probe, and mean zero.

Silent integrity failures are the kind no level metric sees, for example:

  * memories stored and unsearchable while a backlog gauge that measures
    `embedding_dim = 0` reads 0, blind to a memory with no embedding row at all;
  * `memory.set` returning 200 before the row that makes a memory findable is
    durably visible;
  * caller-supplied `entities` written to `loom.memory_embeddings`, omitted from
    `loom.memory`, and then deleted by a worker that reads the omission as "not
    yet extracted".

These gauges assert a property that must hold for ALL rows, so they catch the
next instance of the class rather than one known bug.

The distinction matters for how they are read. A level gauge (`embed_pending`) is
expected to be non-zero and to drain. An invariant gauge is expected to be zero
forever, and a non-zero reading is a bug that has already happened.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

OTEL = ROOT / "server" / "observability" / "otel_probes.py"
METRICS = ROOT / "server" / "observability" / "metrics_gauges.py"

INVARIANT_GAUGES = {
    "loom.integrity.divergent_entities": "divergent_entities",
    "loom.integrity.orphan_vectors": "orphan_vectors",
    # Erasure residue outside the saga census.
    "loom.integrity.orphan_entity_vectors": "orphan_entity_vectors",
    "loom.integrity.orphan_graph_namespaces": "orphan_graph_namespaces",
    # Live memory with no registry row (owner, kind, policy).
    "loom.integrity.live_ns_unregistered": "live_ns_unregistered",
    "loom.integrity.live_rows_unregistered": "live_rows_unregistered",
    # Row-level invariants search and the readers silently skip.
    "loom.integrity.dim0_with_vector": "dim0_with_vector",
    "loom.integrity.tombstone_status_drift": "tombstone_status_drift",
    "loom.integrity.active_past_valid_to": "active_past_valid_to",
    "loom.integrity.high_with_embedding": "high_with_embedding",
}


def test_probe_returns_every_invariant_key() -> None:
    """`_probe_state` must return the keys the gauges read, or the gauges emit
    nothing at all — silently, because a missing key yields no Observation."""
    src = OTEL.read_text()
    for key in INVARIANT_GAUGES.values():
        assert f'"{key}": int(' in src, (
            f"_probe_state does not return {key!r}; the gauge reading it will "
            "emit no observations and the dashboard will look healthy"
        )
        assert f"AS {key}" in src, f"_probe_state SQL does not compute {key!r}"


def test_gauges_are_registered_against_those_keys() -> None:
    src = METRICS.read_text()
    for gauge, key in INVARIANT_GAUGES.items():
        assert f'name="{gauge}"' in src, f"{gauge} is not registered"
        assert f'_reading("{key}")' in src, f"{gauge} does not read {key!r} from the probe"


def _census_sql() -> str:
    from server.observability import otel_probes

    return otel_probes._invariants_sql()


def test_invariant_queries_scope_to_live_rows() -> None:
    """A tombstoned memory legitimately has no vector, and a tombstoned vector
    legitimately has no memory. Counting those would make both gauges read
    non-zero permanently, and a gauge that is always red gets ignored."""
    sql = _census_sql()
    # The vector and entity-vector sides read live rows only; the memory side
    # reads every FINAL row (the drift count needs the tombstoned ones) and
    # carries liveness as a flag every join-shaped count is gated on.
    assert sql.count("WHERE tombstoned_at IS NULL") == 2
    assert "tombstoned_at IS NULL AS live" in sql
    assert "sum(is_m AND live) AS m_live" in sql
    assert "if(m_live = 0, e_vec, 0) AS orphan_vec" in sql
    assert "if(live5 = 0, sum(ev_rows), 0) AS orphan_ev5" in sql


def test_divergence_compares_sorted_arrays() -> None:
    """`entities` is an unordered set in practice. Comparing raw arrays would
    report divergence on ordering alone, which is noise, and noise is how a
    correctness signal gets muted."""
    assert _census_sql().count("arraySort(entities)") >= 2, (
        "both sides of the entities comparison must be arraySort()ed"
    )


def test_joins_use_the_full_tenancy_identity() -> None:
    """The same memory_key legitimately exists under different agents, users and
    sessions. Keying on (org, namespace, memory_key) alone produces a cross
    product that inflates the divergent count many times over.

    A correctness signal that exaggerates is worse than none: the first person to
    check one of its findings by hand stops trusting the rest."""
    sql = _census_sql()
    # memory <-> vector counts group on the full identity; the entity-vector
    # anti join on its own identity (the table has no session_id).
    assert "GROUP BY org, namespace, agent, user_id, session_id, memory_key" in sql
    assert "GROUP BY org, namespace, agent, user_id, memory_key" in sql


def test_descriptions_say_the_expected_value_is_zero() -> None:
    """These read differently from level gauges and the description is the only
    place a 3am reader learns that."""
    src = METRICS.read_text()
    for gauge in INVARIANT_GAUGES:
        idx = src.index(f'name="{gauge}"')
        block = src[idx : idx + 600]
        assert "Should be 0" in block, (
            f"{gauge}'s description must say it should be 0, so a non-zero "
            "reading is not mistaken for a backlog that will drain"
        )
