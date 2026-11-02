"""The additive arms and helper legs of memory.search
(server/memory/app/retrieval/, memory/app/search/,
memory/store/, memory/app/deep_evidence.py): the chunk
leg's stages, the lexical union fallback, the key-match arm, graph / triple
/ derived / summary / state / temporal fetches, the keyed memory reads."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from server.core.tag_sql import TagFilter
from server.identity.domain.read_scope import WHOLE_NAMESPACE, ReadFilters
from server.memory.domain.search_types import RowKey
from tests.analyzer.cases._common import AGENT, MODEL, NS, ORG, VEC, chunk_cand, rows_by_substring
from tests.analyzer.cases.search import _q
from tests.analyzer.corpus import Case, RecordingCH

MS = "server.memory.app.retrieval"
HS = "server.memory.app.search.sql"
MR = "server.memory.store"
DE = "server.memory.app.deep_evidence"

_AS_OF = datetime(2026, 1, 1, tzinfo=UTC)


def _stage2_row(session: str, index: int) -> dict[str, Any]:
    from tests.unit.test_chunk_two_stage import _stage2_row as real

    return dict(real(session, index))


_CHUNK_ROWS = rows_by_substring(
    {
        "system.columns": [{"n": 1}],
        "concat('chunk:'": [_stage2_row("s1", 3), _stage2_row("s2", 0)],
        "loom.memory_chunks": [chunk_cand("s1", 3), chunk_cand("s2", 0)],
    }
)


async def _chunks(ch: RecordingCH, c: dict[str, Any]) -> None:
    from server.memory.app.retrieval.chunks import _search_memory_chunks

    q = _q(
        entity_filter=["nate"] if c["entities"] else [],
        query_text="binoculars trip" if c["words"] else "42",
        top_k=5,
        session_ids=["s1"] if c["session_ids"] else [],
        as_of=_AS_OF if c["as_of"] else None,
        user_id="alice" if c["user_id"] else None,
    )
    await _search_memory_chunks(ch, q, {"q": q.query_vec})


async def _lexical(ch: RecordingCH, c: dict[str, Any]) -> None:
    from server.memory.app.retrieval.side_legs import _lexical_recall_fallback

    q = _q(
        query_text="grace hash join bucket spilling bug",
        top_k=5,
        scope=c["scope"],
        memory_type="preference" if c["memory_type"] else None,
        user_id="alice" if c["user_id"] else None,
        tags=["kind:lesson"] if c["tags"] else [],
    )
    await _lexical_recall_fallback(ch, q, limit=50, max_distance=0.9)


def _leg_input(c: dict[str, Any]) -> Any:
    """A request as the handler's leg predicates read it (agent, run, tags)."""
    from types import SimpleNamespace

    return SimpleNamespace(
        org=ORG,
        namespace=NS,
        agent=AGENT if c.get("agent") else "*",
        run_id="r" if c.get("run_id") else "",
        tags=["kind:lesson"] if c.get("tags") else [],
        tags_soft=False,
        include_duplicates=None,
    )


async def _anchor_pool(ch: RecordingCH, c: dict[str, Any]) -> None:
    """The entity / temporal anchor pool, with the tag and agent filters."""
    from server.memory.app.search.sql import _anchor_pool_where, anchor_pool_sql

    params: dict[str, Any] = {"org": ORG, "ns": NS, "limit": 30, "qv": VEC, "model": MODEL}
    where = _anchor_pool_where(_leg_input(c), params)
    preds = []
    if c["entities"]:
        params["ents"] = ["nate"]
        preds.append("hasAny(e.entities, {ents:Array(String)})")
    if c["dates"]:
        params["anchor_dates"] = ["2026-01-01"]
        preds.append(
            "arrayExists(d -> (e.temporal_anchor IS NOT NULL AND abs(dateDiff('day', e.temporal_anchor, "
            "parseDateTimeBestEffortOrNull(d))) <= 14) OR (e.valid_at IS NOT NULL AND "
            "abs(dateDiff('day', e.valid_at, parseDateTimeBestEffortOrNull(d))) <= 14), {anchor_dates:Array(String)})"
        )
    if not preds:
        return
    await ch.query_all(
        anchor_pool_sql(" AND ".join(where) + " AND (" + " OR ".join(preds) + ")"), params
    )


async def _digest_descent(ch: RecordingCH, c: dict[str, Any]) -> None:
    """The digest descent's per-session read, with the tag filter."""
    from server.memory.app.search.sql import _digest_descent_where, digest_descent_sql

    params: dict[str, Any] = {"org": ORG, "ns": NS, "sid": "s1", "k": 5, "qv": VEC, "model": MODEL}
    where = _digest_descent_where(_leg_input(c), params)
    await ch.query_all(digest_descent_sql(" AND ".join(where)), params)


_KEY_WHERE = " AND ".join(
    [
        "e.org = {org:String}",
        "e.namespace = {ns:String}",
        "e.embedding_model = {model:String}",
        "e.embedding_dim > 0",
        "e.tombstoned_at IS NULL",
        "e.status IN ('active', 'duplicate', 'conflicted')",
        "(e.agent = {agent:String} OR e.scope = 'shared')",
    ]
)
_KEY_PARAMS = {
    "org": ORG,
    "ns": NS,
    "model": MODEL,
    "agent": AGENT,
    "run_id": "r",
    "stems": ["task", "worktre", "path"],
    "limit": 30,
    "min_strength": 0.5,
    "qv": [0.0] * 8,
    "pool_keys": ["user_task_worktree", "user_task_status"],
}


async def _key_match(ch: RecordingCH, c: dict[str, Any]) -> None:
    from server.memory.app.search.sql import key_match_component_sql, key_match_sql

    where = _KEY_WHERE + (" AND e.run_id = {run_id:String}" if c["run_id"] else "")
    if c["tags"]:
        where += " AND hasAny(e.tags, {tags:Array(String)})"
    fn = key_match_component_sql if c["component"] else key_match_sql
    await ch.query_all(fn(where), {**_KEY_PARAMS, "tags": ["kind:lesson"]})


async def _key_match_precheck(ch: RecordingCH, c: dict[str, Any]) -> None:
    from server.memory.app.search.sql import key_match_precheck_sql

    await ch.query_all(
        key_match_precheck_sql(), {"org": ORG, "ns": NS, "stems": _KEY_PARAMS["stems"]}
    )


async def _graph_legs(ch: RecordingCH, c: dict[str, Any]) -> None:
    from server.memory.app.retrieval import (
        fetch_derived_aggregate,
        fetch_graph_neighbors,
        fetch_triple_matched_memories,
    )

    acc = ["n", "n2"] if c["accessible"] else None
    await fetch_graph_neighbors(
        ch,
        org=ORG,
        namespace=NS,
        seed_entities=["clickhouse", "nate"],
        hops=c["hops"],
        hub_skip_degree=0 if c["triples"] else 500,
        accessible_namespaces=acc,
    )
    await fetch_triple_matched_memories(
        ch,
        org=ORG,
        namespace=NS,
        query_triples=[("user", "likes", "tea")],
        expand_synonyms=c["triples"],
        as_of=_AS_OF if c["accessible"] else None,
    )
    await fetch_derived_aggregate(
        ch,
        org=ORG,
        namespace=NS,
        subject="user",
        predicate="likes" if c["triples"] else None,
        category="food" if c["hops"] == 2 else None,
        object_like="tea" if c["triples"] else None,
        subject_canonical_expanded=["user", "me"] if c["accessible"] else None,
        since=_AS_OF if c["accessible"] else None,
    )


async def _summary_legs(ch: RecordingCH, c: dict[str, Any]) -> None:
    from server.memory.app.retrieval import (
        fetch_current_state_observation,
        fetch_entity_summaries,
        fetch_session_summaries,
        fetch_temporal_diff,
    )

    user = "alice" if c["user_id"] else None
    as_of = _AS_OF if c["as_of"] else None
    await fetch_session_summaries(
        ch,
        org=ORG,
        namespace=NS,
        agent=AGENT,
        user_id=user,
        query_vec=VEC,
        embedding_model=MODEL,
        as_of=as_of,
        as_of_basis=c["basis"],
        run_id="run-1" if c["user_id"] else None,
    )
    await fetch_entity_summaries(
        ch,
        org=ORG,
        namespace=NS,
        agent=AGENT,
        user_id=user,
        query_vec=VEC,
        embedding_model=MODEL,
        entity_filter=["nate"],
        as_of=as_of,
        as_of_basis=c["basis"],
    )
    await fetch_current_state_observation(
        ch,
        org=ORG,
        namespace=NS,
        agent=AGENT,
        user_id=user,
        query_vec=VEC,
        embedding_model=MODEL,
        entity_filter=["nate"],
        as_of=as_of,
        as_of_basis=c["basis"],
    )
    await fetch_temporal_diff(
        ch,
        org=ORG,
        namespace=NS,
        agent=AGENT,
        user_id=user,
        operand_vecs=[VEC, VEC],
        embedding_model=MODEL,
        observation_date="2026-01-01" if c["as_of"] else None,
        to_observation=c["as_of"],
        run_id="run-1" if c["user_id"] else "",
    )


async def _memory_reads(ch: RecordingCH, c: dict[str, Any]) -> None:
    from server.memory import store as M
    from server.memory.store import Identity

    ident = Identity(org=ORG, namespace=NS, agent=c["agent"], user_id=c["user_id"], session_id="-")
    as_of = _AS_OF if c["as_of"] else None
    await M.get_many_by_keys(ident, ["k1", "k2"], ch=ch, scope=WHOLE_NAMESPACE)
    await M.get_by_key(
        ident,
        "k1",
        ch=ch,
        as_of=as_of,
        include_tombstoned=c["as_of"],
        include_usage_totals=c["as_of"],
        scope=WHOLE_NAMESPACE,
    )
    await M.get_by_id(
        "00000000-0000-0000-0000-000000000001",
        org=ORG,
        ch=ch,
        as_of=as_of,
        include_usage_totals=c["as_of"],
        scope=WHOLE_NAMESPACE,
    )
    await M.list_by_namespace(
        ident,
        memory_type="preference" if c["as_of"] else None,
        pinned_only=c["as_of"],
        key_prefix="user_" if c["as_of"] else None,
        order_by=c["order_by"],
        as_of=as_of,
        # memory.list's tag match (every tag, both sides lowercased), or memory.search's exact one.
        tag_filter=(
            TagFilter(("kind:lesson", "kind:fact"), "folded_all")
            if c["as_of"]
            else TagFilter(("kind:lesson",), "exact_any")
        ),
        include_tombstoned=c["as_of"],
        exclude_high_sensitivity=c["as_of"],
        ch=ch,
        include_duplicates=c["as_of"],
        exclude_transcripts=not c["as_of"],
        exclude_memory_types=[] if c["as_of"] else ["episodic", "reflection"],
        scope=WHOLE_NAMESPACE,
    )
    await M.count_by_namespace(
        ident,
        memory_type="preference" if c["as_of"] else None,
        key_prefix="user_" if c["as_of"] else None,
        source_trace="s" if c["as_of"] else None,
        tag_filter=TagFilter(("kind:lesson", "kind:fact"), "folded_all") if c["as_of"] else None,
        as_of=as_of,
        created_since=as_of,
        ch=ch,
        include_duplicates=c["as_of"],
        scope=WHOLE_NAMESPACE,
    )
    await M.list_history(ident, "k1", ch=ch, scope=WHOLE_NAMESPACE)
    await M.find_related_by_entities(ident, "k1", ch=ch, scope=WHOLE_NAMESPACE)


async def _memory_finders(ch: RecordingCH, c: dict[str, Any]) -> None:
    from server.memory import store as M

    user = "alice" if c["user_id"] else None
    vec = VEC if c["vector"] else None
    await M.find_by_content_terms(
        org=ORG,
        namespace=NS,
        terms=["deploy", "window"],
        scope=c["scope"],
        agent=AGENT,
        user_id=user,
        cues=["friday"] if c["vector"] else None,
        ch=ch,
    )
    await M.find_by_identifier(
        org=ORG,
        namespace=NS,
        identifiers=["PR-1"],
        query_embedding=vec,
        scope=c["scope"],
        agent=AGENT,
        user_id=user,
        case_insensitive=c["vector"],
        order="cosine" if c["vector"] else "recent",
        ch=ch,
    )
    await M.find_by_key_facet(
        org=ORG, namespace=NS, facets=["deploy"], scope=c["scope"], agent=AGENT, user_id=user, ch=ch
    )
    await M.find_by_key_prefix(
        org=ORG,
        namespace=NS,
        prefixes=["user_"],
        scope=c["scope"],
        agent=AGENT,
        user_id=user,
        ch=ch,
    )
    await M.find_chunks_by_identifier(
        org=ORG,
        namespace=NS,
        identifiers=["PR-1"],
        query_embedding=vec,
        scope=c["scope"],
        agent=AGENT,
        case_insensitive=c["vector"],
        markers=["fixed"] if c["vector"] else None,
        order="cosine" if c["vector"] else "recent",
        ch=ch,
    )
    await M.find_chunks_by_session_date(
        org=ORG,
        namespace=NS,
        start_date="2026-01-01",
        end_date="2026-01-31",
        query_embedding=vec,
        scope=c["scope"],
        agent=AGENT,
        ch=ch,
    )
    await M.find_row_by_key_namespace(ORG, NS, "k1", ch=ch)
    await M.find_history_target(ORG, "00000000-0000-0000-0000-000000000001", ch=ch)
    await M.get_canonical_row(ORG, "00000000-0000-0000-0000-000000000001", ch=ch)
    await M.consolidated_dependents_of(ORG, NS, "k1", ch=ch)
    await M.live_source_count(ORG, NS, ["k1"], ch=ch)
    await M.earliest_version_in_namespace(ORG, NS, ch=ch)
    await M.fetch_version_rows(ORG, NS, [("k1", 1)], ch=ch)
    await M.rollback_change_rows(ORG, NS, 5, memory_keys=["k1"] if c["vector"] else None, ch=ch)
    await M.find_duplicate_clusters(ORG, NS, ch=ch)
    await M.find_orphan_duplicates(ORG, NS, ch=ch)
    # The served-duplicate truth read, the canonical read, the collapse worklist
    # The user_id axis drives both forms: a read bounded to one user and the whole namespace.
    dup_scope = (
        ReadFilters(scope="own_user", user_id="u1", session_id=None)
        if c["user_id"]
        else WHOLE_NAMESPACE
    )
    rows = [RowKey(NS, "a", "-", "-", "k1"), RowKey(NS, "a", "-", "-", "k2")]
    await M.status_by_rows(ORG, rows, scope=dup_scope, ch=ch)
    await M.canonical_rows_by_ids(
        ORG, [NS], ["00000000-0000-0000-0000-000000000001"], scope=dup_scope, ch=ch
    )
    await M.superseded_successors(ORG, rows[:1], scope=dup_scope, ch=ch)
    await M.live_ids_in_scope(
        ORG, [NS], ["00000000-0000-0000-0000-000000000001"], scope=dup_scope, ch=ch
    )
    await M.find_duplicate_pairs(ORG, NS, key_prefix="user_" if c["vector"] else "", ch=ch)
    await M.embedding_mirror_for_keys(ORG, NS, ["k1"], ch=ch)
    await M.divergent_namespaces(ORG, since_days=7 if c["vector"] else 0, ch=ch)
    await M.live_embedding_rows_for_key(
        ch, org=ORG, namespace=NS, agent=AGENT, user_id="-", session_id="-", memory_key="k1"
    )
    await M.live_shared_embedding_rows_for_key(ch, org=ORG, namespace=NS, memory_key="k1")


_STUB_KEY = {
    "org": ORG,
    "namespace": NS,
    "agent": AGENT,
    "user_id": "-",
    "session_id": "s1",
    "scope": "private",
    "embedding_model": MODEL,
    "memory_key": "k1",
}
# A candidate stub and its confirmation, so the selection reaches its FINAL read.
_STUB_ROWS = rows_by_substring(
    {"HAVING argMax(embedding_dim": [_STUB_KEY], "PREWHERE embedding_dim": [_STUB_KEY]}
)


async def _embedding_stubs(ch: RecordingCH, c: dict[str, Any]) -> None:
    """The embedding worker's reads exclude held rows before their limits."""
    from server.memory import store as M

    await M.find_embedding_stubs(batch_size=10, max_attempts=3, ch=ch)


async def _keys_and_fence(ch: RecordingCH, c: dict[str, Any]) -> None:
    from server.memory import store as M

    # memory.keys' one read of the namespace, and the write fence's read before an insert
    await M.live_keys(ORG, NS, scope=WHOLE_NAMESPACE, limit=10, ch=ch)
    row = {"agent": AGENT, "user_id": "-", "session_id": "-", "memory_key": "k", "version": 1}
    await M.versions_above(ORG, NS, [row], ch=ch)
    # memory.import's read of each key's newest version in every session of the identity
    pinned = ReadFilters(scope="own_user", user_id="u1", session_id=None)
    for user_id, scope in (("-", WHOLE_NAMESPACE), ("u1", pinned)):
        await M.key_heads_across_sessions(ORG, NS, AGENT, user_id, ["k1", "k2"], scope=scope, ch=ch)
    # and its read of the category links already stored for its keys
    from server.memory.store import categories

    await categories.link_states(ORG, NS, ["k1", "k2"], ch=ch)


# The citation read's answer: one row, so the keyed value read runs.
_CITED_ROWS = rows_by_substring(
    {
        "AS id, version, sensitivity": [
            {
                "memory_key": "k",
                "id": "00000000-0000-0000-0000-000000000001",
                "version": 1,
                "sensitivity": "",
                "value_len": 10,
                "agent": AGENT,
                "user_id": "-",
                "session_id": "-",
                "memory_type": "semantic",
                "source_class": "tool",
                "updated_at": _AS_OF,
            }
        ],
    }
)


async def _live_revisions(ch: RecordingCH, c: dict[str, Any]) -> None:
    from server.memory.store.reads import live_revisions

    # namespace.audit / namespace.validate's citation read, by id and by identity
    await live_revisions(
        ORG,
        NS,
        scope=WHOLE_NAMESPACE,
        memory_ids=["00000000-0000-0000-0000-000000000001"],
        identities=[(AGENT, "-", "-", "k")],
        hash_max_bytes=1 << 16,
        ch=ch,
    )


async def _memory_workers(ch: RecordingCH, c: dict[str, Any]) -> None:
    from server.memory import store as M

    org = ORG if c["filters"] else None
    ns = NS if c["filters"] else None
    since = 3600 if c["filters"] else None
    await M.find_embedding_stubs(
        batch_size=10, max_attempts=3, org_filter=org, namespace_filter=ns, ch=ch
    )
    # whom a job's call over a session is for: its newest live version's writer
    await M.session_writers([(ORG, NS, AGENT, "-", "s1")], ch=ch)
    await M.find_memories_without_embedding_rows(
        batch_size=10, org_filter=org, namespace_filter=ns, since_seconds=since, ch=ch
    )
    # memory.reindex's read, with and without the missing-model filter
    await M.find_memories_to_reindex(
        ORG, NS, embedding_model="m", force=not c["filters"], limit=10, ch=ch
    )
    await M.find_unentitied_keys(
        batch_size=10, org_filter=org, namespace_filter=ns, since_seconds=since, ch=ch
    )
    await M.find_unentitied_memories(
        batch_size=10, org_filter=org, namespace_filter=ns, since_seconds=since, ch=ch
    )
    await M.find_expired_candidates(org_filter=org, namespace_filter=ns, ch=ch)
    await M.reconcile_embedding_status(ORG, NS, dry_run=not c["filters"], ch=ch)
    await M.heal_divergent_entities(ch=ch)
    # The entity extractor's batched write and its exact-key read
    await M.memory_rows_by_identity_keys([(ORG, NS, AGENT, "-", "-", "k1")], ch=ch)
    # bulk_set's live-row lookup reads the same keys with sequential consistency
    await M.memory_rows_by_identity_keys([(ORG, NS, AGENT, "-", "-", "k1")], ch=ch, sequential=True)
    await M.apply_entities_batch(
        [
            {
                "org": ORG,
                "namespace": NS,
                "agent": AGENT,
                "user_id": "-",
                "session_id": "-",
                "memory_key": "k1",
                "entities": ["alice"],
            }
        ],
        ch=ch,
    )
    await ch.query_all(M.status_divergence_sql(), {})


async def _deep_evidence(ch: RecordingCH, c: dict[str, Any]) -> None:
    from server.memory.app.deep_evidence import fetch_raw_evidence, fetch_session_turns

    await fetch_raw_evidence(
        org=ORG,
        namespace=NS,
        memory_keys=["k1", "k2"],
        max_chars_per_memory=800,
        max_chars_total=4000,
        ch=ch,
    )
    await fetch_session_turns(
        org=ORG,
        namespace=NS,
        session_ids=["s1"],
        question="q",
        max_chars_per_session=800,
        max_chars_total=4000,
        ch=ch,
    )


async def _stale_links(ch: RecordingCH, c: dict[str, Any]) -> None:
    from server.memory.store.code_links import stale_of_served

    await stale_of_served(
        ORG,
        NS,
        ["changed", "missing"],
        ["k"] if c["keys"] else None,
        trunk="",
        tag_namespaces=[("repo:acme/a", "code-a")],
        readable=["code-a"],
        limit=5,
        ch=ch,
    )


async def _link_pages(ch: RecordingCH, c: dict[str, Any]) -> None:
    from server.memory.store import code_links

    after = (ORG, NS, AGENT, "-", "-", "k") if c["after"] else None
    await code_links.backfill_page(after, 5, ["code-a"], ch=ch)
    key = (ORG, "code-a", "p.py", "f", NS, "00000000-0000-0000-0000-000000000001")
    await code_links.sweep_page(key if c["after"] else None, 5, ch=ch)
    await code_links._heads(ch, [(ORG, NS, AGENT, "-", "-", "k")], ["code-a"])


def cases() -> list[Case]:
    return [
        Case(
            "chunk_leg",
            f"{MS}.chunks",
            _chunks,
            {
                "words": (True, False),
                "entities": (False, True),
                "session_ids": (False, True),
                "as_of": (False, True),
                "user_id": (False, True),
            },
            rows=_CHUNK_ROWS,
        ),
        Case(
            "lexical_fallback",
            f"{MS}.side_legs",
            _lexical,
            {
                "scope": ("private", "shared"),
                "memory_type": (False, True),
                "user_id": (False, True),
                "tags": (False, True),
            },
        ),
        Case(
            "key_match",
            HS,
            _key_match,
            {"component": (False, True), "run_id": (False, True), "tags": (False, True)},
        ),
        Case("key_match_precheck", HS, _key_match_precheck),
        # The arms that read loom.memory_embeddings outside the fact leg
        Case(
            "anchor_pool",
            HS,
            _anchor_pool,
            {
                "entities": (True, False),
                "dates": (False, True),
                "agent": (False, True),
                "run_id": (False, True),
                "tags": (False, True),
            },
        ),
        Case(
            "digest_descent",
            HS,
            _digest_descent,
            {"agent": (False, True), "run_id": (False, True), "tags": (False, True)},
        ),
        Case(
            "graph_legs",
            f"{MS}.graph",
            _graph_legs,
            {"hops": (1, 2), "triples": (False, True), "accessible": (False, True)},
        ),
        Case(
            "summary_legs",
            f"{MS}.summaries",
            _summary_legs,
            {
                "user_id": (False, True),
                "as_of": (False, True),
                "basis": ("valid_time", "knowledge_time"),
            },
        ),
        Case(
            "memory_repo.reads",
            MR,
            _memory_reads,
            {
                "agent": ("*", "bot"),
                "user_id": ("-", "alice"),
                "as_of": (False, True),
                "order_by": ("created_at_desc", "created_at_asc"),
            },
        ),
        Case(
            "memory_repo.finders",
            MR,
            _memory_finders,
            {"scope": ("private", "shared"), "user_id": (False, True), "vector": (False, True)},
        ),
        Case("memory_repo.workers", MR, _memory_workers, {"filters": (False, True)}),
        Case(
            "memory_repo.embedding_stubs",
            MR,
            _embedding_stubs,
            {},
            rows=_STUB_ROWS,
        ),
        Case("memory_repo.keys_and_fence", MR, _keys_and_fence, {}),
        Case("memory_repo.live_revisions", MR, _live_revisions, {}, rows=_CITED_ROWS),
        Case("deep_evidence", DE, _deep_evidence, {}),
        Case(
            "code_links.stale_of_served",
            "server.memory.store.code_links",
            _stale_links,
            {"keys": (False, True)},
        ),
        Case(
            "code_links.pages",
            "server.memory.store.code_links",
            _link_pages,
            {"after": (False, True)},
        ),
    ]
