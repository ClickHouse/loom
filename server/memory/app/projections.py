"""Memory projections: the derived rows a memory write fans out to.

A memory write lands its memory row first; its projections are the rows
derived from it afterwards — verbatim conversation chunks
(loom.memory_chunks, written by server/memory/app/chunk_projection.py),
entity edges, entity triples (with their interval repairs and supersession
closes) and entity vectors. They are best-effort:
a projection that fails is logged to loom.memory_projection_failures and retried
from server.memory.app.projection_failures, which calls these same writers.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from server.core.datetimes import naive_utc_now, parse_iso_dt
from server.memory.api.models_bulk import MemoryBulkSetInput, _BulkSetResult
from server.memory.app import conflicts as _conflicts

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from server.memory.domain.graph_quality import IntervalRepair


def resolved_entity_list(
    raw_entities: list[str],
    entity_map: dict[str, str] | None = None,
) -> list[str]:
    """Canonicalize + apply namespace-local entity resolution."""

    from server.memory.app.entity_resolution import apply_entity_resolution

    out: list[str] = []
    seen: set[str] = set()
    for raw in raw_entities:
        resolved = apply_entity_resolution(str(raw), entity_map)
        if len(resolved) < 2 or resolved in seen:
            continue
        seen.add(resolved)
        out.append(resolved)
    return out


async def resolve_projection_entity_map(
    input: MemoryBulkSetInput,
    entity_texts: list[str],
    *,
    ch: Any,
) -> dict[str, str]:
    if not entity_texts:
        return {}
    try:
        from server.memory.app.entity_resolution import resolve_entities_for_write
        from server.observability.otel import traced

        with traced("entity.resolve", mentions=len(entity_texts)):
            return await resolve_entities_for_write(
                ch,
                org=input.org,
                namespace=input.namespace,
                entity_texts=entity_texts,
            )
    except Exception as e:  # noqa: BLE001 — rows write without entity ids
        log.warning("entity projection resolution failed (non-fatal): %s", e)
        return {}


def graph_insert_settings(table: str, *, wait: bool, dedup_token: str) -> dict[str, Any]:
    """Insert settings for the plain-MergeTree graph tables, where every row
    counts as an observation. A replay (`dedup_token`, the projection
    repair) inserts synchronously under a per-table deduplication token, so
    a replay that ran and then failed to resolve does not count its rows
    twice (ClickHouse drops a block whose token it has seen)."""
    if dedup_token:
        return {
            "async_insert": 0,
            "insert_deduplicate": 1,
            "insert_deduplication_token": f"{dedup_token}:{table}",
        }
    return {"async_insert": 1, "wait_for_async_insert": 1 if wait else 0}


async def write_entity_edges(
    input: MemoryBulkSetInput,
    results: list[_BulkSetResult],
    *,
    ch: Any,
    entity_map: dict[str, str] | None = None,
    wait_visible: bool = False,
    dedup_token: str = "",
) -> None:
    """Emit pairwise entity edges + edge-to-memory rows per entry.

    Per memory with N entities, emits C(N, 2) canonical pairs
    (entity_a <= entity_b lexically), capped at _MAX_PAIRS_PER_MEMORY.
    Always-on side effect of writes; read-side (search_mode=graph) is opt-in.
    Fail-open. `wait_visible` acks only once the async-insert buffer has
    flushed, so the rows are readable when the call returns: for a caller off
    the response path, which pays nothing for the wait.
    """
    now = naive_utc_now()
    edges: list[dict[str, Any]] = []
    edge_memories: list[dict[str, Any]] = []
    _MAX_PAIRS_PER_MEMORY = 100
    if entity_map is None:
        entity_map = await resolve_projection_entity_map(
            input,
            [str(e) for entry in input.entries for e in (entry.entities or [])],
            ch=ch,
        )

    from server.memory.domain.graph_quality import canonical_entity_text, is_lexical_junk_entity

    for entry, result in zip(input.entries, results, strict=True):
        # A pronoun, interjection or contraction fragment the
        # extractor emitted as an entity or topic is never a graph node.
        ents = [
            e
            for e in resolved_entity_list(entry.entities or [], entity_map)
            if not is_lexical_junk_entity(e)
        ]
        # Facet nodes join the co-occurrence plane as ordinary entity nodes,
        # canonicalized identically so write and read land on one node:
        # code-native files + concepts (coding facts), and the extractor's
        # `topics` — the concept phrases a fact is ABOUT — for every fact,
        # so two memories about one idea connect even when
        # they name different entities.
        facet_nodes: list[str] = []
        for raw in [
            *(getattr(entry, "files", None) or []),
            *(getattr(entry, "concepts", None) or []),
            *(getattr(entry, "topics", None) or []),
        ]:
            node = (
                str(raw).lower()
                if str(raw).lower().startswith(("commit:", "error:"))
                else canonical_entity_text(str(raw))
            )
            if (
                len(node) >= 2
                and node not in ents
                and node not in facet_nodes
                and not is_lexical_junk_entity(node)
            ):
                facet_nodes.append(node)
        ents = sorted(set(ents) | set(facet_nodes))
        if len(ents) < 2:
            continue
        # Edge validity window from the memory's valid_at / valid_to (extractor
        # populates valid_to on closed-window state changes). NULL = still valid.
        entry_valid_from = parse_iso_dt(getattr(entry, "valid_at", "") or "")
        entry_valid_to = parse_iso_dt(getattr(entry, "valid_to", "") or "")
        # Enumerate pairs by OFFSET, not by first index. Both orders visit the
        # same C(N,2) pairs, but the cap truncates, and `ents` is sorted — so
        # i-major order would spend the whole budget on the alphabetically-first
        # entities, making a node's co-occurrence weight a function of its
        # spelling. Offset-major gives
        # every entity ~2 edges per completed offset, so a truncated
        # neighbourhood is merely smaller instead of skewed.
        n = len(ents)
        pair_count = 0
        for offset in range(1, n):
            if pair_count >= _MAX_PAIRS_PER_MEMORY:
                break
            for i in range(n - offset):
                if pair_count >= _MAX_PAIRS_PER_MEMORY:
                    break
                a, b = ents[i], ents[i + offset]  # sorted, so a <= b
                edges.append(
                    {
                        "org": input.org,
                        "namespace": input.namespace,
                        "entity_a": a,
                        "entity_b": b,
                        "co_occur": 1,
                        "last_seen": now,
                        "valid_from": entry_valid_from,
                        "valid_to": entry_valid_to,
                        # Supersession axis: a fresh observation
                        # is never born superseded; _repair_contradictory_edges
                        # stamps it on the OLDER pair. NULL = current.
                        "invalid_at": None,
                    }
                )
                edge_memories.append(
                    {
                        "org": input.org,
                        "namespace": input.namespace,
                        "entity_a": a,
                        "entity_b": b,
                        "memory_key": result.memory_key,
                        "seen_at": now,
                    }
                )
                pair_count += 1

    if not edges:
        return

    # Plain MergeTree: every row is one observation (co_occur = 1); readers
    # aggregate (sum(co_occur) GROUP BY pair) at query time.
    # Concurrent because these are two independent tables with no ordering
    # requirement between them, and against ClickHouse Cloud each insert is a
    # ~50ms round trip on the `memory.set` path.
    from server.observability.otel import traced as _traced

    with _traced("ch.insert.entity_edges", pairs=len(edges)):
        await asyncio.gather(
            ch.insert(
                "loom.entity_edges",
                edges,
                settings=graph_insert_settings(
                    "entity_edges", wait=wait_visible, dedup_token=dedup_token
                ),
            ),
            ch.insert(
                "loom.entity_edge_memories",
                edge_memories,
                settings=graph_insert_settings(
                    "entity_edge_memories", wait=wait_visible, dedup_token=dedup_token
                ),
            ),
        )


def entity_triple_row(
    org: str,
    namespace: str,
    subject: str,
    relation: str,
    obj: str,
    *,
    provenance: str,
    confidence: float,
    adapter_name: str,
    last_seen: datetime,
    valid_from: Any = None,
    valid_to: Any = None,
) -> dict[str, Any]:
    """One `loom.entity_triples` row as every triple writer inserts it. A fresh
    triple is never born superseded (`invalid_at` NULL): the contradiction
    repair stamps the older row."""
    return {
        "org": org,
        "namespace": namespace,
        "subject": subject,
        "relation": relation,
        "object": obj,
        "provenance": provenance,
        "co_occur": 1,
        "last_seen": last_seen,
        "confidence": confidence,
        "adapter_name": adapter_name,
        "valid_from": valid_from,
        "valid_to": valid_to,
        "invalid_at": None,
    }


async def write_entity_triples(
    input: MemoryBulkSetInput,
    results: list[_BulkSetResult],
    *,
    ch: Any,
    entity_map: dict[str, str] | None = None,
    dedup_token: str = "",
) -> None:
    """Emit typed (subject, relation, object) triples.

    Mirrors write_entity_edges: shared canonicalizer, fail-open, async
    insert. Empty `relations` produce zero rows. Subject + object canonicalize;
    relation is lowercased + space→underscore.
    """
    from server.memory.app.entity_resolution import apply_entity_resolution
    from server.memory.domain.graph_quality import canonical_relation
    from server.memory.domain.search_text import canonicalize_verb
    from server.settings import settings as _settings

    # adapter_name = the extractor model, so an operator can roll back triples
    # from a specific extractor. No per-relation confidence yet; carry the
    # parent memory's through.
    _adapter = _settings.llm_model

    now = naive_utc_now()
    triples: list[dict[str, Any]] = []
    triple_memories: list[dict[str, Any]] = []
    _MAX_TRIPLES_PER_MEMORY = 8
    if entity_map is None:
        relation_entities: list[str] = []
        for entry in input.entries:
            for rel in list(entry.relations or []):
                if isinstance(rel, (list, tuple)) and len(rel) == 3:
                    relation_entities.extend([str(rel[0]), str(rel[2])])
        entity_map = await resolve_projection_entity_map(
            input,
            relation_entities,
            ch=ch,
        )

    from server.memory.domain import triple_provenance as _tp
    from server.memory.domain.graph_quality import (
        canonical_entity_text,
        classify_code_concept,
        code_facet_relation,
    )

    for entry, result in zip(input.entries, results, strict=True):
        rels = list(entry.relations or [])
        entry_files = [str(p) for p in (getattr(entry, "files", None) or []) if str(p).strip()]
        entry_concepts = [
            str(c) for c in (getattr(entry, "concepts", None) or []) if str(c).strip()
        ]
        entry_topics = [str(t) for t in (getattr(entry, "topics", None) or []) if str(t).strip()]
        if not rels and not entry_files and not entry_concepts and not entry_topics:
            continue
        # Parent-memory confidence flows to each derived triple (default 1.0).
        _conf = float(getattr(entry, "confidence", 1.0) or 1.0)
        # Triple validity from the parent's valid_at/valid_to, falling back to
        # temporal_anchor (an event triple is valid from the date it's ABOUT,
        # and non-pipeline writers often carry only the anchor). NULL = always.
        _entry_valid_from = parse_iso_dt(getattr(entry, "valid_at", "") or "") or parse_iso_dt(
            getattr(entry, "temporal_anchor", "") or ""
        )
        _entry_valid_to = parse_iso_dt(getattr(entry, "valid_to", "") or "")
        kept = 0

        # Bind per-iteration values as defaults so the closure captures THIS
        # entry's window/result, not the loop's last (B023).
        # The fact's own prose, normalised once, decides whether
        # an extractor relation is `extracted` (both endpoints in the text) or
        # `inferred`; loom-minted facet edges are `facet`.
        _carrier_norm = _tp.normalize(_tp.carrier_text(getattr(entry, "value", "")))

        def _append_triple(
            s: str,
            v: str,
            o: str,
            conf: float,
            *,
            provenance: str = "",
            _cn: str = _carrier_norm,
            _vf: Any = _entry_valid_from,
            _vt: Any = _entry_valid_to,
            _mk: Any = result.memory_key,
        ) -> None:
            nonlocal kept
            if kept >= _MAX_TRIPLES_PER_MEMORY:
                return
            if len(s) < 2 or len(v) < 2 or len(o) < 1:
                return
            if not provenance:
                provenance = _tp.classify(s, o, _cn)
            triples.append(
                entity_triple_row(
                    input.org,
                    input.namespace,
                    s,
                    v,
                    o,
                    provenance=provenance,
                    confidence=conf,
                    adapter_name=_adapter,
                    last_seen=now,
                    valid_from=_vf,
                    valid_to=_vt,
                )
            )
            triple_memories.append(
                {
                    "org": input.org,
                    "namespace": input.namespace,
                    "subject": s,
                    "relation": v,
                    "object": o,
                    "memory_key": _mk,
                    "seen_at": now,
                    "confidence": conf,
                    "adapter_name": _adapter,
                }
            )
            kept += 1

        for rel in rels:
            if kept >= _MAX_TRIPLES_PER_MEMORY:
                break
            if not isinstance(rel, (list, tuple)) or len(rel) != 3:
                continue
            s = apply_entity_resolution(str(rel[0]), entity_map)
            relation = canonical_relation(canonicalize_verb(str(rel[1])))
            o = apply_entity_resolution(str(rel[2]), entity_map)
            _append_triple(s, relation.verb, o, min(_conf, relation.confidence))

        # Code-native facet edges: files + concepts become typed
        # (subject -> code-node) triples on entity_triples so facts.traverse
        # answers "what touches this file". Subject is the fact's actor.
        if entry_files or entry_concepts:
            fact_verbs = [str(r[1]) for r in rels if isinstance(r, (list, tuple)) and len(r) == 3]
            subject = canonical_entity_text(input.agent or "user") or "user"
            for path in entry_files:
                node = canonical_entity_text(path) or path.lower()
                verb = code_facet_relation("file", fact_verbs)
                _append_triple(subject, verb, node, _conf, provenance=_tp.FACET)
            for concept in entry_concepts:
                kind = classify_code_concept(concept)
                node = (
                    concept.lower()
                    if concept.lower().startswith(("commit:", "error:"))
                    else (canonical_entity_text(concept) or concept.lower())
                )
                verb = code_facet_relation(kind, fact_verbs)
                _append_triple(subject, verb, node, _conf, provenance=_tp.FACET)

        # Topic nodes: `<actor> about <topic>`, so
        # facts.traverse from a topic finds who/what is about it, and a
        # topic never masquerades as a code library or function.
        if entry_topics:
            subject = canonical_entity_text(input.agent or "user") or "user"
            for topic in entry_topics:
                node = canonical_entity_text(topic) or topic.lower()
                _append_triple(subject, "about", node, _conf, provenance=_tp.FACET)

    if not triples:
        return

    # Insert FIRST, then repair. The repair SELECT scans open
    # (valid_to IS NULL) rows, so this batch's triples must be visible to
    # it: that lets a batch close intervals it contradicts within itself,
    # and closes the cross-request race where the previous write's
    # fire-and-forget insert was still invisible to this write's repair.
    # wait_for_async_insert=1 keeps server-side batching but acks only
    # once rows are queryable (the read-your-writes the repair contract
    # needs). Ordering safety: should_invalidate_interval only closes
    # intervals STRICTLY OLDER than the new fact, and same-object rows —
    # including the row just inserted for this very triple — are skipped,
    # so repairing against a set that contains this batch is sound.
    await asyncio.gather(
        *(
            ch.insert(
                f"loom.{t}",
                r,
                settings=graph_insert_settings(t, wait=True, dedup_token=dedup_token),
            )
            for t, r in (("entity_triples", triples), ("entity_triple_memories", triple_memories))
        ),
    )

    try:
        repairs = await _repair_contradictory_triples(input, triples, ch=ch)
        if repairs:
            await _repair_contradictory_edges(input, repairs, ch=ch)
            # Under the namespace's `newest_wins` policy the
            # memory rows that carried the closed triples retire with them.
            await _retire_memories_behind_repairs(input, repairs, triple_memories, ch=ch)
    except Exception as e:  # noqa: BLE001 — a failed interval repair must not fail the write
        log.warning("entity graph interval repair failed (non-fatal): %s", e)


# ---------------------------------------------------------------------------
# One mutation per table per write. One `ALTER TABLE … UPDATE` per supersession
# would rewrite the table over and over, in a mutation queue shared with erasure.
# The closes of one write are folded into one UPDATE per table: the row's key is
# matched by `IN` and its own valid_to is looked up positionally with
# arrayElement/indexOf.
# ---------------------------------------------------------------------------
_GRAPH_KEY_SEP = "\x1f"
_TRIPLE_KEY_EXPR = "concat(subject, char(31), relation, char(31), object)"
_EDGE_KEY_EXPR = (
    "concat(entity_a, char(31), entity_b, char(31), "
    "ifNull(toString(toUnixTimestamp(valid_from)), ''))"
)


def _edge_key(entity_a: str, entity_b: str, valid_from: datetime | None) -> str:
    import calendar

    vf = "" if valid_from is None else str(calendar.timegm(valid_from.timetuple()))
    return _GRAPH_KEY_SEP.join((entity_a, entity_b, vf))


async def close_triples(
    ch: Any,
    *,
    org: str,
    namespace: str,
    closes: list[tuple[str, str, str, datetime, datetime | None]],
) -> int:
    """Close open (subject, relation, object) intervals at each one's own
    valid_to — one UPDATE per table, two when only some closes carry an
    invalid_at (transaction time). Returns the number of mutations issued."""
    issued = 0
    for with_invalid_at in (True, False):
        group = [c for c in closes if (c[4] is not None) is with_invalid_at]
        if not group:
            continue
        keys = [_GRAPH_KEY_SEP.join((s, r, o)) for s, r, o, _, _ in group]
        idx = f"indexOf({{keys:Array(String)}}, {_TRIPLE_KEY_EXPR})"
        set_cols = f"valid_to = arrayElement({{valid_tos:Array(DateTime)}}, {idx})"
        params: dict[str, Any] = {
            "org": org,
            "ns": namespace,
            "keys": keys,
            "valid_tos": [c[3] for c in group],
        }
        if with_invalid_at:
            set_cols += f", invalid_at = arrayElement({{invalid_ats:Array(DateTime)}}, {idx})"
            params["invalid_ats"] = [c[4] for c in group]
        await ch.execute(
            f"""
            ALTER TABLE loom.entity_triples
            UPDATE {set_cols}
            WHERE org = {{org:String}}
              AND namespace = {{ns:String}}
              AND {_TRIPLE_KEY_EXPR} IN {{keys:Array(String)}}
              AND valid_to IS NULL
            """,
            params=params,
        )
        issued += 1
    if issued:
        # A mutation is no INSERT, so the client's graph-write mark does not see it.
        from server.memory.app.graph_write_marks import marks as _graph_marks

        _graph_marks().note_write(org, namespace)
    return issued


async def _repair_contradictory_triples(
    input: MemoryBulkSetInput,
    new_triples: list[dict[str, Any]],
    *,
    ch: Any,
) -> list[IntervalRepair]:
    """Close open intervals contradicted by new single-valued triples.

    The namespace's declared relation cardinalities are
    consulted first (seed list second); an undated new fact orders by this
    write's transaction time; an equal-date tie closes the existing row; a
    late-arriving older value closes the new row; and every close stamps
    `invalid_at` so the supersession axis records when loom learned it.
    """

    from server.identity.app.namespace_ontology import load_relation_cardinality
    from server.memory.domain.graph_quality import (
        is_single_valued_relation,
        triple_interval_repairs,
    )

    declared = await load_relation_cardinality(input.org, input.namespace)
    # Candidates: dated triples (any relation — the gate inside
    # triple_interval_repairs decides) plus UNDATED triples on single-valued
    # relations, which order by arrival time.
    dated = [
        t
        for t in new_triples
        if t.get("valid_from") is not None
        or is_single_valued_relation(str(t.get("relation") or ""), declared)
    ]
    if not dated:
        return []
    now = naive_utc_now()
    subjects = sorted({str(t["subject"]) for t in dated})
    relations = sorted({str(t["relation"]) for t in dated})
    # select_sequential_consistency: on CH Cloud (SharedMergeTree) this
    # forces a fresh metadata snapshot so the rows this request just
    # inserted (wait_for_async_insert=1) are guaranteed visible even when
    # the SELECT lands on a different replica. No-op on plain MergeTree.
    existing = await ch.query_all(
        """
        SELECT subject, relation, object, valid_from, valid_to
        FROM loom.entity_triples
        WHERE org = {org:String}
          AND namespace = {ns:String}
          AND subject IN {subjects:Array(String)}
          AND relation IN {relations:Array(String)}
          AND valid_to IS NULL
        SETTINGS select_sequential_consistency = 1
        """,
        params={
            "org": input.org,
            "ns": input.namespace,
            "subjects": subjects,
            "relations": relations,
        },
    )
    all_repairs: list[IntervalRepair] = []
    for i, triple in enumerate(dated):
        # A later triple in this same write for the same subject + relation
        # is the value that should survive; leave its row to its own repair.
        later_objects = {
            str(t["object"])
            for t in dated[i + 1 :]
            if str(t["subject"]) == str(triple["subject"])
            and str(t["relation"]) == str(triple["relation"])
            and str(t["object"]) != str(triple["object"])
        }
        repairs = triple_interval_repairs(
            existing=existing,
            new_subject=str(triple["subject"]),
            new_relation=str(triple["relation"]),
            new_object=str(triple["object"]),
            new_valid_at=triple.get("valid_from"),
            superseded_at=now,
            declared=declared,
            arrival_at=now,
            later_batch_objects=later_objects,
        )
        all_repairs.extend(repairs)
    # One close per row, at its EARLIEST end: a batch carrying the whole
    # history of a single-valued relation would otherwise close the oldest
    # value once per newer value (the later ALTERs would no-op on
    # `valid_to IS NULL`, but they are still mutations).
    earliest: dict[tuple[str, str, str], IntervalRepair] = {}
    for repair in all_repairs:
        key = (repair.subject, repair.relation, repair.old_object)
        prior = earliest.get(key)
        if prior is None or repair.valid_to < prior.valid_to:
            earliest[key] = repair
    all_repairs = list(earliest.values())
    # valid_to is the older fact's stated end; invalid_at (when the repair
    # carries it) is the transaction time loom learned the fact was stale.
    await close_triples(
        ch,
        org=input.org,
        namespace=input.namespace,
        closes=[
            (r.subject, r.relation, r.old_object, r.valid_to, r.invalid_at) for r in all_repairs
        ],
    )
    return all_repairs


def retirement_victims(
    repairs: list[IntervalRepair],
    links: list[dict[str, Any]],
    memories: list[dict[str, Any]],
    *,
    new_keys: set[str],
) -> list[dict[str, Any]]:
    """Pure: which memory rows retire behind repaired triples.

    `links` are entity_triple_memories rows (subject, relation, object,
    memory_key); `memories` are live loom.memory rows for the linked keys. A
    memory retires when it carried a CLOSED value (subject, relation,
    old_object) and did not also carry the surviving value, was not written by
    this batch (`new_keys`) and is not pinned. `superseded_by` is the memory
    that carried the new object, when one is known, else empty."""
    closed = {(r.subject, r.relation, r.old_object) for r in repairs}
    survivors: dict[tuple[str, str], str] = {}
    for r in repairs:
        survivors[(r.subject, r.relation)] = getattr(r, "new_object", "") or ""
    by_key: dict[str, dict[str, Any]] = {str(m["memory_key"]): m for m in memories}
    carried_closed: dict[str, tuple[str, str]] = {}
    carried_survivor: set[str] = set()
    survivor_key_for: dict[tuple[str, str], str] = {}
    for ln in links:
        key = str(ln.get("memory_key") or "")
        trip = (str(ln.get("subject")), str(ln.get("relation")), str(ln.get("object")))
        if trip in closed:
            carried_closed.setdefault(key, trip[:2])
        elif survivors.get(trip[:2]) and trip[2] == survivors[trip[:2]]:
            carried_survivor.add(key)
            survivor_key_for.setdefault(trip[:2], key)
    out: list[dict[str, Any]] = []
    for key, sr in carried_closed.items():
        if key in new_keys or key in carried_survivor:
            continue
        m = by_key.get(key)
        if m is None or bool(m.get("pinned")):
            continue
        sk = survivor_key_for.get(sr, "")
        by = by_key.get(sk) if sk else None
        out.append(
            {
                "memory_id": str(m["memory_id"]),
                "namespace": str(m["namespace"]),
                "agent": str(m["agent"]),
                "user_id": str(m["user_id"]),
                "session_id": str(m["session_id"]),
                "memory_key": key,
                "superseded_by": str(by["memory_id"]) if by else "",
                "superseded_by_key": sk if by else "",
            }
        )
    return out


async def _retire_memories_behind_repairs(
    input: MemoryBulkSetInput,
    repairs: list[IntervalRepair],
    triple_memories: list[dict[str, Any]],
    *,
    ch: Any,
) -> int:
    """`newest_wins`: the memory rows that carried a closed
    single-valued value retire (status superseded, superseded_by, invalid_at)
    through the same version re-emit dedup uses, so recall drops them and the
    audit trail keeps them. Only under the namespace policy; never pinned rows,
    never this batch's own rows. Best effort."""
    if not repairs:
        return 0
    policy = await _conflicts.namespace_conflict_policy(input.org, input.namespace, ch=ch)
    if policy != "newest_wins":
        return 0
    from server.memory.store import supersede_duplicates

    subjects = sorted({r.subject for r in repairs})
    relations = sorted({r.relation for r in repairs})
    # select_sequential_consistency: the link rows for THIS batch were inserted
    # moments ago; on CH Cloud a read that lands on another replica would miss
    # them and the closed carrier would look unlinked (the repair read above
    # sets it for the same reason). No-op on plain MergeTree.
    links = await ch.query_all(
        """
        SELECT subject, relation, object, memory_key
        FROM loom.entity_triple_memories
        WHERE org = {org:String} AND namespace = {ns:String}
          AND subject IN {subjects:Array(String)} AND relation IN {relations:Array(String)}
        SETTINGS select_sequential_consistency = 1
        """,
        params={
            "org": input.org,
            "ns": input.namespace,
            "subjects": subjects,
            "relations": relations,
        },
    )
    keys = sorted({str(ln["memory_key"]) for ln in links if ln.get("memory_key")})
    if not keys:
        return 0
    memories = await ch.query_all(
        """
        SELECT toString(memory_id) AS memory_id, namespace, agent, user_id, session_id,
               memory_key, pinned
        FROM loom.memory FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
          AND memory_key IN {keys:Array(String)} AND tombstoned_at IS NULL
          AND status IN ('active', 'duplicate', 'conflicted')
        SETTINGS select_sequential_consistency = 1
        """,
        params={"org": input.org, "ns": input.namespace, "keys": keys},
    )
    new_keys = {str(tm.get("memory_key") or "") for tm in triple_memories}
    victims = retirement_victims(repairs, links, memories, new_keys=new_keys)
    if not victims:
        log.debug(
            "newest_wins: %d repaired triples, %d links, %d live carriers, nothing to retire",
            len(repairs),
            len(links),
            len(memories),
        )
        return 0
    from server.memory.app.auto_resolution import (
        NEWEST_WINS_CONTRADICTION,
        RetirementTracker,
        record_auto_supersessions,
    )
    from server.memory.domain.lock import refuses_retirement

    # The tracker also refuses a row retired since the read (it is not re-pointed).
    tracker = RetirementTracker(refuses_retirement)
    try:
        n = await supersede_duplicates(
            input.org, victims, ch=ch, skip=tracker.skip, on_written=tracker.written
        )
    finally:
        # What reached loom.memory, even when the embeddings mirror then failed.
        await record_auto_supersessions(
            org=input.org,
            namespace=input.namespace,
            retired=tracker.retired(victims),
            reason=NEWEST_WINS_CONTRADICTION,
        )
    log.info("newest_wins retired %d memories behind %d repaired triples", n, len(repairs))
    return n


async def _repair_contradictory_edges(
    input: MemoryBulkSetInput,
    repairs: list[IntervalRepair],
    *,
    ch: Any,
) -> None:
    """Close pairwise entity edges for repaired single-valued triples — one
    UPDATE per write, two when only some repairs carry an
    invalid_at. Each edge's own valid_to is looked up by its key."""

    from server.memory.domain.graph_quality import canonical_edge_pair

    seen: set[tuple[str, str, object, object]] = set()
    closes: list[tuple[str, str, datetime | None, datetime, datetime | None]] = []
    for repair in repairs:
        entity_a, entity_b = canonical_edge_pair(repair.subject, repair.old_object)
        dedupe_key = (entity_a, entity_b, repair.valid_from, repair.valid_to)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        closes.append((entity_a, entity_b, repair.valid_from, repair.valid_to, repair.invalid_at))
    for with_invalid_at in (True, False):
        group = [c for c in closes if (c[4] is not None) is with_invalid_at]
        if not group:
            continue
        keys = [_edge_key(a, b, vf) for a, b, vf, _, _ in group]
        idx = f"indexOf({{keys:Array(String)}}, {_EDGE_KEY_EXPR})"
        own_valid_to = f"arrayElement({{valid_tos:Array(DateTime)}}, {idx})"
        set_cols = f"valid_to = {own_valid_to}"
        params: dict[str, Any] = {
            "org": input.org,
            "ns": input.namespace,
            "keys": keys,
            "valid_tos": [c[3] for c in group],
        }
        if with_invalid_at:
            set_cols += f", invalid_at = arrayElement({{invalid_ats:Array(DateTime)}}, {idx})"
            params["invalid_ats"] = [c[4] for c in group]
        # Mirror the triple-plane supersession close onto the pairwise edge.
        # The key carries the edge's valid_from (unix seconds, '' for NULL), so
        # an edge is closed only at the interval the triple repair named, and
        # only when that interval starts before its close.
        await ch.execute(
            f"""
            ALTER TABLE loom.entity_edges
            UPDATE {set_cols}
            WHERE org = {{org:String}}
              AND namespace = {{ns:String}}
              AND {_EDGE_KEY_EXPR} IN {{keys:Array(String)}}
              AND valid_to IS NULL
              AND (valid_from IS NULL OR valid_from < {own_valid_to})
            """,
            params=params,
        )


async def write_entity_vectors(
    input: MemoryBulkSetInput,
    results: list[Any],  # list[_BulkSetResult]
    embedder: Any,
    *,
    ch: Any,
    entity_map: dict[str, str] | None = None,
    entity_embeddings: dict[str, list[float]] | None = None,
) -> None:
    """Embed each unique entity once and insert one row per
    (entity, memory_key) pair into loom.entity_vectors.

    `entity_embeddings` are vectors the caller already has (
    bulk_set embeds the entity texts in its value/key batch); only entities
    missing from it are embedded here."""
    entity_pairs: list[tuple[str, str]] = []  # (entity_text, memory_key)
    seen: set[tuple[str, str]] = set()
    if entity_map is None:
        entity_map = await resolve_projection_entity_map(
            input,
            [str(e) for entry in input.entries for e in (entry.entities or [])],
            ch=ch,
        )
    for entry, result in zip(input.entries, results, strict=True):
        for ent_clean in resolved_entity_list(entry.entities or [], entity_map):
            if not ent_clean:
                continue
            pair = (ent_clean, result.memory_key)
            if pair in seen:
                continue
            seen.add(pair)
            entity_pairs.append(pair)
    if not entity_pairs:
        return

    # Embed each unique entity once (texts repeat across memory_keys): one
    # embed_many call per bulk_set instead of N serial round-trips.
    unique_entities = sorted({pair[0] for pair in entity_pairs})
    embeddings: dict[str, list[float]] = {
        ent: list(vec)
        for ent, vec in (entity_embeddings or {}).items()
        if ent in set(unique_entities) and vec
    }
    missing = [ent for ent in unique_entities if ent not in embeddings]
    if missing:
        embedding_list = await embedder.embed_many(missing)
        embeddings.update(zip(missing, embedding_list, strict=True))

    now_utc = naive_utc_now()
    rows = [
        {
            "org": input.org,
            "namespace": input.namespace,
            "agent": input.agent,
            "user_id": input.user_id,
            "entity_text": ent,
            "entity_embedding": embeddings[ent],
            "embedding_model": embedder.model_name,
            "embedding_dim": embedder.dim,
            "memory_key": memory_key,
            "tombstoned_at": None,
            "created_at": now_utc,
        }
        for ent, memory_key in entity_pairs
    ]
    await ch.insert(
        "loom.entity_vectors",
        rows,
        settings={"async_insert": 0, "wait_for_async_insert": 1},
    )
