"""The one-pass state census equals the per-invariant statements.

`_invariants_sql()` computes every invariant from one FINAL pass over
memory, memory_embeddings and entity_vectors. The CLIs (`integrity-repair`,
`erasure-re-verify`, the namespace census) still run one statement per
invariant. This test seeds copies of the nine tables with multi-version keys,
tombstones, versions in different partitions, vectors without memories,
memories without vectors, reordered and diverging entities, graph-only and
chunk-only namespaces, then runs both shapes against the copies and asserts
every count is equal (and non-zero, so no count passes by reading 0 on both
sides). Merges are stopped on the copies so the versions stay unmerged
(best-effort: a role without the grant still compares, on merged parts).
"""

from __future__ import annotations

import datetime as dt
import random
import re
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.settings import settings  # noqa: E402

TABLES = (
    "memory",
    "memory_embeddings",
    "entity_vectors",
    "entity_edges",
    "chunks",
    "chunk_supersessions",
    "namespaces",
)
COUNTS = (
    "embed_pending",
    "divergent_entities",
    "orphan_vectors",
    "orphan_entity_vectors",
    "orphan_graph_namespaces",
    "live_ns_unregistered",
    "live_rows_unregistered",
    "dim0_with_vector",
    "tombstone_status_drift",
    "active_past_valid_to",
    "high_with_embedding",
)


def _reference_sql() -> str:
    """One statement per invariant: the shared CLI statements plus the three
    memory <-> vector joins spelled inline."""
    from server.erasure.store.health import ORPHAN_ENTITY_VECTORS_SQL, ORPHAN_GRAPH_NAMESPACES_SQL
    from server.identity.store.namespace_census import UNREGISTERED_LIVE_SQL
    from server.memory.store.integrity import (
        ACTIVE_PAST_VALID_TO_SQL,
        DIM0_WITH_VECTOR_SQL,
        HIGH_WITH_EMBEDDING_SQL,
        TOMBSTONE_STATUS_DRIFT_SQL,
    )

    ident = ("org", "namespace", "agent", "user_id", "session_id", "memory_key")
    on = " AND ".join(f"m.{c} = e.{c}" for c in ident)
    cols = ", ".join(ident)
    return f"""
        SELECT
            (SELECT count() FROM loom.memory FINAL WHERE tombstoned_at IS NULL)
              - (SELECT count() FROM loom.memory_embeddings FINAL
                 WHERE tombstoned_at IS NULL AND embedding_dim > 0) AS embed_pending,
            (SELECT count() FROM (
                SELECT m.ents AS me, e.ents AS ee
                FROM (SELECT {cols}, arraySort(entities) AS ents
                      FROM loom.memory FINAL WHERE tombstoned_at IS NULL) AS m
                INNER JOIN (SELECT {cols}, arraySort(entities) AS ents
                            FROM loom.memory_embeddings FINAL
                            WHERE tombstoned_at IS NULL) AS e ON {on}
                WHERE m.ents != e.ents)) AS divergent_entities,
            (SELECT count() FROM (
                SELECT e.memory_key
                FROM (SELECT {cols} FROM loom.memory_embeddings FINAL
                      WHERE tombstoned_at IS NULL AND embedding_dim > 0) AS e
                LEFT ANTI JOIN (SELECT {cols} FROM loom.memory FINAL
                                WHERE tombstoned_at IS NULL) AS m ON {on})) AS orphan_vectors,
            (SELECT count() FROM ({ORPHAN_ENTITY_VECTORS_SQL})) AS orphan_entity_vectors,
            (SELECT count() FROM ({ORPHAN_GRAPH_NAMESPACES_SQL})) AS orphan_graph_namespaces,
            (SELECT count() FROM ({UNREGISTERED_LIVE_SQL})) AS live_ns_unregistered,
            (SELECT ifNull(sum(live_rows), 0) FROM ({UNREGISTERED_LIVE_SQL}))
                AS live_rows_unregistered,
            (SELECT count() FROM ({DIM0_WITH_VECTOR_SQL})) AS dim0_with_vector,
            (SELECT count() FROM ({TOMBSTONE_STATUS_DRIFT_SQL})) AS tombstone_status_drift,
            (SELECT count() FROM ({ACTIVE_PAST_VALID_TO_SQL})) AS active_past_valid_to,
            (SELECT count() FROM ({HIGH_WITH_EMBEDDING_SQL})) AS high_with_embedding
    """


def _retarget(sql: str, prefix: str) -> str:
    pattern = r"\bloom\.(" + "|".join(TABLES) + r")\b"
    return re.sub(pattern, lambda m: f"loom.{prefix}{m.group(1)}", sql)


def _seed(ch: Any, prefix: str, rnd: random.Random) -> None:
    now = dt.datetime.now().replace(microsecond=0)
    months = [now - dt.timedelta(days=40), now - dt.timedelta(days=5), now]
    ents = ["alpha", "beta", "gamma", "delta"]
    width = settings.embedding_dim  # a coded column takes this length only
    mem: list[list[Any]] = []
    emb: list[list[Any]] = []
    ev: list[list[Any]] = []
    for org in ("o1", "o2"):
        for ns in ("ns0", "ns1", "ns2", "ns3"):
            for agent in ("a1", "a2"):
                for user in ("u1", ""):
                    for sess in ("", "s1"):
                        for k in range(6):
                            key = [org, ns, agent, user, sess, f"k{k}"]
                            last: list[str] = rnd.sample(ents, rnd.randint(0, 3))
                            if rnd.random() > 0.08:
                                for v in range(rnd.randint(1, 3)):
                                    tomb = now if rnd.random() < 0.25 else None
                                    status = (
                                        "tombstoned"
                                        if tomb and rnd.random() < 0.5
                                        else (rnd.choice(["active", "active", "superseded"]))
                                    )
                                    valid_to = None
                                    if rnd.random() < 0.2:
                                        valid_to = now + dt.timedelta(days=rnd.choice([-1, 9]))
                                    last = rnd.sample(ents, rnd.randint(0, 3))
                                    mem.append(
                                        key
                                        + [
                                            "v",
                                            100 + v * 10 + rnd.randint(0, 3),
                                            tomb,
                                            rnd.choice(months),
                                            status,
                                            valid_to,
                                            "high" if rnd.random() < 0.1 else "",
                                            last,
                                        ]
                                    )
                            for model in ("m1", "m2")[: rnd.randint(1, 2)]:
                                if rnd.random() < 0.12:
                                    continue
                                for v in range(rnd.randint(1, 2)):
                                    dim = 0 if rnd.random() < 0.2 else width
                                    vec = [0.5 if dim or rnd.random() < 0.4 else 0.0] * width
                                    r = rnd.random()
                                    e = (
                                        rnd.sample(ents, 2)
                                        if r < 0.15
                                        else (list(reversed(last)) if r < 0.3 else list(last))
                                    )
                                    emb.append(
                                        key
                                        + [
                                            "private",
                                            "x",
                                            model,
                                            dim,
                                            vec,
                                            [0.0] * width,
                                            now if rnd.random() < 0.2 else None,
                                            rnd.choice(months),
                                            100 + v * 10 + rnd.randint(0, 3),
                                            e,
                                        ]
                                    )
                            if rnd.random() < 0.3:
                                ev.append(
                                    [
                                        org,
                                        ns,
                                        agent,
                                        user,
                                        rnd.choice(ents),
                                        f"k{k}",
                                        now,
                                        now if rnd.random() < 0.2 else None,
                                        [0.0] * width,
                                    ]
                                )
    for i in range(20):
        ev.append(["o1", "ns1", "a9", "u9", "alpha", f"gone{i}", now, None, [0.0] * width])
    mem_cols = [
        "org", "namespace", "agent", "user_id", "session_id", "memory_key", "value", "version",
        "tombstoned_at", "created_at", "status", "valid_to", "sensitivity", "entities",
    ]  # fmt: skip
    emb_cols = [
        "org", "namespace", "agent", "user_id", "session_id", "memory_key", "scope",
        "content_excerpt", "embedding_model", "embedding_dim", "embedding",
        "memory_key_embedding", "tombstoned_at",
        "created_at", "version", "entities",
    ]  # fmt: skip
    ev_cols = [
        "org", "namespace", "agent", "user_id", "entity_text", "memory_key", "created_at",
        "tombstoned_at", "entity_embedding",
    ]  # fmt: skip
    # Several inserts per table: several parts, versions of one key split across them.
    for rows, table, cols in ((mem, "memory", mem_cols), (emb, "memory_embeddings", emb_cols)):
        rnd.shuffle(rows)
        for i in range(0, len(rows), 150):
            ch.insert(f"loom.{prefix}{table}", rows[i : i + 150], column_names=cols)
    ch.insert(f"loom.{prefix}entity_vectors", ev, column_names=ev_cols)
    edges = [
        [org, ns, "alpha", "beta", 1, now]
        for org in ("o1", "o2", "o3")
        for ns in ("ns0", "ns1", "graph_only", "chunks_only", "superseded_only")
    ]
    ch.insert(
        f"loom.{prefix}entity_edges",
        edges,
        column_names=["org", "namespace", "entity_a", "entity_b", "co_occur", "last_seen"],
    )
    ch.insert(
        f"loom.{prefix}chunks",
        [
            ["o1", "chunks_only", "u://1", 0, "c", "m1", 1, None, now, [0.0] * width],
            ["o3", "chunks_only", "u://1", 0, "c", "m1", 1, None, now, [0.0] * width],
            ["o1", "superseded_only", "u://2", 0, "c", "m1", 1, None, now, [0.0] * width],
            ["o2", "superseded_only", "u://3", 0, "c", "m1", 1, now, now, [0.0] * width],
        ],
        column_names=[
            "org",
            "namespace",
            "source_uri",
            "chunk_idx",
            "content",
            "embedding_model",
            "doc_version",
            "doc_superseded_at",
            "created_at",
            "embedding",
        ],  # fmt: skip
    )
    ch.insert(
        f"loom.{prefix}chunk_supersessions",
        [["o1", "superseded_only", "m1", "u://2", 1, now]],
        column_names=[
            "org",
            "namespace",
            "embedding_model",
            "source_uri",
            "doc_version",
            "superseded_at",
        ],  # fmt: skip
    )
    ch.insert(
        f"loom.{prefix}namespaces",
        [["o1", "ns0", now], ["o1", "ns0", now], ["o2", "ns0", now], ["o2", "ns1", now]],
        column_names=["org", "namespace", "updated_at"],
    )


def _row(ch: Any, sql: str) -> dict[str, int]:
    res = ch.query(sql)
    return {k: int(v or 0) for k, v in zip(res.column_names, res.result_rows[0], strict=True)}


@pytest.mark.parametrize("seed", [3, 17])
def test_one_pass_census_equals_the_per_invariant_statements(ch_client, seed: int) -> None:
    from server.observability import otel_probes

    prefix = f"census_eq_{uuid.uuid4().hex[:8]}_"
    created: list[str] = []
    try:
        for t in TABLES:
            ch_client.command(f"CREATE TABLE loom.{prefix}{t} AS loom.{t}")
            created.append(t)
            try:
                ch_client.command(f"SYSTEM STOP MERGES loom.{prefix}{t}")
            except Exception:  # noqa: BLE001 — no grant: compare on merged parts
                pass
        _seed(ch_client, prefix, random.Random(seed))
        expected = _row(ch_client, _retarget(_reference_sql(), prefix))
        got = _row(ch_client, _retarget(otel_probes._invariants_sql(), prefix))
        assert {k: got[k] for k in COUNTS} == expected
        assert all(expected[k] != 0 for k in COUNTS), expected
    finally:
        for t in created:
            ch_client.command(f"DROP TABLE IF EXISTS loom.{prefix}{t}")
